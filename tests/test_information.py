"""宿主统一联网（ctx.web.search / ctx.web.fetch）下的信息层测试。

覆盖：隐私清洗、宿主搜索重试与历史记录、联网工具、新闻阅读、主动探索与状态汇总。
自有 Provider 降级链已删除（v1.24.0），不再有协议归一化、Key 冷却与浏览器搜索用例。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime,timedelta,timezone
from typing import Any

from Mai_life.config import MaiLifeSettings,SocialGroupProfile,UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.information.host_search import HostSearchService,_error_class
from Mai_life.information.information_service import InformationService

TZ=timezone(timedelta(hours=8))
NOW=datetime(2026,7,13,10,0,tzinfo=TZ)


class DummyLogger:
    def __init__(self):self.messages=[]
    def __getattr__(self,name):return lambda *args,**kwargs:self.messages.append(" ".join(str(item) for item in args))


class OfflineLLM:
    """全任务不可用的模型替身：查询规划/摘要/关联全部走本地降级。"""
    def task_available(self,task:str)->bool:return False
    async def generate_json(self,*args:Any,**kwargs:Any)->dict[str,Any]:return {}


class PlanLLM(OfflineLLM):
    """只开放指定任务、返回固定查询规划的模型替身（探索用 search，新闻用 news）。"""
    def __init__(self,query:str,topic:str="发现",tasks:tuple[str,...]=("search",)):
        self.query=query; self.topic=topic; self.tasks=set(tasks)
    def task_available(self,task:str)->bool:return task in self.tasks
    async def generate_json(self,*args:Any,**kwargs:Any)->dict[str,Any]:
        return {"topic":self.topic,"query":self.query,"reason":"测试规划"}


class FakeHostWeb:
    """ctx.web 替身：search 按脚本逐次消费（Exception=抛错，dict=返回），fetch 返回固定正文。"""

    def __init__(self,script:list[Any]|None=None,results:list[dict[str,Any]]|None=None,
                 service:str="unit-search",fetch_content:str="正文内容"):
        self.script=list(script or []); self.results=list(results or [])
        self.service=service; self.fetch_content=fetch_content
        self.search_calls:list[dict[str,Any]]=[]; self.fetch_calls:list[str]=[]

    async def search(self,query:str,*,max_results:int=5)->dict[str,Any]:
        self.search_calls.append({"query":query,"max_results":max_results})
        if self.script:
            item=self.script.pop(0)
            if isinstance(item,Exception):raise item
            return item
        return {"query":query,"results":self.results,"content_kind":"web",
                "service":self.service,"retrieved_at":time.time()}

    async def fetch(self,url:str)->dict[str,Any]:
        self.fetch_calls.append(url)
        return {"url":url,"title":"页面","content":self.fetch_content,
                "content_kind":"article","service":"unit","retrieved_at":time.time()}


class DummyContext:
    def __init__(self,web:Any=None):
        self.web=web; self.logger=DummyLogger()


def host_rows(*rows:dict[str,Any])->list[dict[str,Any]]:return list(rows)


def row(title:str="标题",url:str="https://example.com/a",snippet:str="摘要内容",
        published_at:Any=None)->dict[str,Any]:
    return {"title":title,"url":url,"snippet":snippet,"summary":snippet,"published_at":published_at}


def base_config()->MaiLifeSettings:
    config=MaiLifeSettings()
    config.users.profiles=[UserProfile(user_id="10001",role="owner")]
    config.group.profiles=[SocialGroupProfile(group_id="20001",observe_enabled=True)]
    return config


async def make_service(web:Any,config:MaiLifeSettings|None=None,
                       llm:Any|None=None)->tuple[InformationService,LifeStore,tempfile.TemporaryDirectory]:
    tmp=tempfile.TemporaryDirectory(); store=LifeStore(tmp.name); await store.initialize()
    config=config or base_config()
    service=InformationService(DummyContext(web),store,config,llm or OfflineLLM(),DummyLogger())
    return service,store,tmp


class HostSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_sanitize_removes_private_terms_only_as_whole_tokens(self):
        web=FakeHostWeb()
        service,store,tmp=await make_service(web)
        try:
            config=service.config
            config.users.profiles=[UserProfile(user_id="10001",role="owner")]
            await store.sync_users(config.users.profiles)
            await store.update_user_display_name("10001","小甜")
            config.group.profiles=[SocialGroupProfile(group_id="20001")]
            cleaned=await service.search.sanitize_query("10001 小甜 20001 的明天天气怎么样")
            self.assertNotIn("10001",cleaned); self.assertNotIn("小甜",cleaned); self.assertNotIn("20001",cleaned)
            self.assertIn("明天天气怎么样",cleaned)  # 子串不被打散
            technical=await service.search.sanitize_query("Next.js 15 升级指南")
            self.assertEqual(technical,"Next.js 15 升级指南")
        finally:
            await store.close(); tmp.cleanup()

    async def test_search_retries_then_succeeds_with_single_history_row(self):
        web=FakeHostWeb(script=[RuntimeError("boom"),RuntimeError("boom again")],
                        results=[row()])
        service,store,tmp=await make_service(web)
        try:
            result=await service.search.search("测试查询",operation="tool_search",max_results=3)
            self.assertEqual(len(result["results"]),1)
            self.assertEqual(web.search_calls[0]["max_results"],3)
            self.assertEqual(len(web.search_calls),3)  # 两次失败 + 一次成功
            history=await store.recent_search_history(time.time(),10)
            self.assertEqual(len(history),1)  # 一次逻辑搜索一条历史
            self.assertTrue(history[0]["success"])
            summary=await store.search_api_summary(time.time()-60)
            self.assertEqual(sum(int(item["calls"]) for item in summary),1)
        finally:
            await store.close(); tmp.cleanup()

    async def test_search_permanent_failure_records_history_and_error_class(self):
        web=FakeHostWeb(script=[RuntimeError("upstream down")]*5)
        service,store,tmp=await make_service(web)
        try:
            service.config.network.max_attempts=3
            result=await service.search.search("查询",operation="news")
            self.assertEqual(result["results"],[])
            self.assertEqual(result["error_class"],"host_error")
            self.assertEqual(len(web.search_calls),3)
            history=await store.recent_search_history(time.time(),10)
            self.assertTrue(history and not history[0]["success"])
            self.assertEqual(history[0]["error_class"],"host_error")
        finally:
            await store.close(); tmp.cleanup()

    async def test_search_history_can_be_disabled(self):
        web=FakeHostWeb(results=[row()])
        service,store,tmp=await make_service(web)
        try:
            service.config.network.history_enabled=False
            await service.search.search("查询",operation="search")
            self.assertEqual(await store.recent_search_history(time.time(),10),[])
        finally:
            await store.close(); tmp.cleanup()

    async def test_fetch_article_returns_empty_on_failure_and_missing_web(self):
        class FailingWeb:
            async def fetch(self,url:str)->dict[str,Any]:raise RuntimeError("blocked")
        service,store,tmp=await make_service(FailingWeb())
        try:
            self.assertEqual(await service.search.fetch_article("https://example.com"),"")
        finally:
            await store.close(); tmp.cleanup()
        no_web,store2,tmp2=await make_service(None)
        try:
            self.assertEqual(await no_web.search.fetch_article("https://example.com"),"")
        finally:
            await store2.close(); tmp2.cleanup()

    def test_error_class_mapping(self):
        class NotEnabled(Exception):pass
        self.assertEqual(_error_class(NotEnabled("web.search 未启用")),"host_disabled")
        self.assertEqual(_error_class(TimeoutError("request timeout")),"timeout")
        self.assertEqual(_error_class(ValueError("max_results 超出上限")),"invalid_param")
        self.assertEqual(_error_class(RuntimeError("connection reset")),"host_error")


class ToolSearchTests(unittest.IsolatedAsyncioTestCase):
    async def _service(self,web:Any)->tuple[InformationService,LifeStore,tempfile.TemporaryDirectory]:
        service,store,tmp=await make_service(web)
        service.config.network.enabled=True
        return service,store,tmp

    async def test_gates_block_tool_before_search(self):
        service,store,tmp=await self._service(FakeHostWeb())
        try:
            service.config.network.enabled=False
            blocked=await service.search_for_tool("查询",NOW)
            self.assertFalse(blocked["success"])
            service.config.network.enabled=True
            service.config.network.tool_enabled=False
            disabled=await service.search_for_tool("查询",NOW)
            self.assertFalse(disabled["success"])
            empty=await service.search_for_tool("   ",NOW)
            self.assertFalse(empty["success"])
            self.assertEqual(service.search._web().search_calls,[])
        finally:
            await store.close(); tmp.cleanup()

    async def test_tool_formats_results_and_clamps_limit(self):
        web=FakeHostWeb(results=[row(title=f"结果{index}",url=f"https://e.com/{index}") for index in range(8)])
        service,store,tmp=await self._service(web)
        try:
            payload=await service.search_for_tool("查询",NOW,result_limit=9)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["provider_type"],"host_web_search")
            self.assertEqual(len(payload["results"]),5)  # 上限 5
            self.assertIn("（服务：unit-search）",payload["content"])
            self.assertNotIn("结果5",payload["content"])
        finally:
            await store.close(); tmp.cleanup()

    async def test_tool_failure_message_after_retries(self):
        web=FakeHostWeb(script=[RuntimeError("x")]*9)
        service,store,tmp=await self._service(web)
        try:
            payload=await service.search_for_tool("查询",NOW)
            self.assertFalse(payload["success"])
            self.assertIn("联网搜索失败",payload["content"])
            self.assertEqual(len(web.search_calls),3)  # 默认重试 3 次
        finally:
            await store.close(); tmp.cleanup()

    async def test_tool_sanitizes_query_before_host(self):
        web=FakeHostWeb(results=[row()])
        service,store,tmp=await self._service(web)
        try:
            await service.store.sync_users(service.config.users.profiles)
            await service.search_for_tool("10001 查一下天气",NOW)
            self.assertEqual(web.search_calls[0]["query"],"查一下天气")
        finally:
            await store.close(); tmp.cleanup()

    async def test_tool_reports_missing_host_capability(self):
        service,store,tmp=await self._service(None)
        try:
            payload=await service.search_for_tool("查询",NOW)
            self.assertFalse(payload["success"])
            self.assertIn("宿主统一联网能力不可用",payload["content"])
        finally:
            await store.close(); tmp.cleanup()


class NewsTests(unittest.IsolatedAsyncioTestCase):
    async def _ready(self,web:Any)->tuple[InformationService,LifeStore,tempfile.TemporaryDirectory]:
        service,store,tmp=await make_service(web,llm=PlanLLM("AI 新闻",topic="AI 新闻",tasks=("news",)))
        service.config.network.enabled=True
        service.config.network.news_enabled=True
        service.config.network.news_allowed_schedule_types=["leisure"]
        return service,store,tmp

    def _schedule(self)->dict[str,Any]:
        return {"current":{"kind":"leisure","summary":"闲暇"}}

    async def test_news_saves_dedups_and_reads_articles(self):
        web=FakeHostWeb(results=[row(title="新闻A",url="https://e.com/a"),
                                 row(title="新闻A",url="https://e.com/a"),  # 同 URL 同内容去重
                                 row(title="无链接",url="")])
        service,store,tmp=await self._ready(web)
        try:
            changed=await service.news.refresh_due(NOW,self._schedule(),"人设")
            self.assertEqual(changed,2)
            items=await store.recent_news_items(NOW.timestamp(),10)
            self.assertEqual(len(items),2)
            uncited=[item for item in items if "无外部链接" in item["summary"]]
            self.assertEqual(len(uncited),1)
            self.assertTrue(web.fetch_calls)  # 正文经宿主 web.fetch 读取
            self.assertTrue(all(url.startswith("https://e.com") for url in web.fetch_calls))
        finally:
            await store.close(); tmp.cleanup()

    async def test_news_drops_stale_published_results(self):
        old_ts=NOW.timestamp()-5*86400
        web=FakeHostWeb(results=[row(title="旧闻",url="https://e.com/old",published_at=old_ts),
                                 row(title="新文",url="https://e.com/new"),
                                 row(title="字符串时间",url="https://e.com/iso",published_at="2026-07-13T09:00:00+08:00")])
        service,store,tmp=await self._ready(web)
        try:
            changed=await service.news.refresh_due(NOW,self._schedule(),"人设")
            titles={item["title"] for item in await store.recent_news_items(NOW.timestamp(),10)}
            self.assertEqual(changed,2)
            self.assertNotIn("旧闻",titles)
            self.assertIn("字符串时间",titles)  # 非数值 published_at 保守保留
        finally:
            await store.close(); tmp.cleanup()

    async def test_news_skips_without_host_or_schedule_or_quota(self):
        service,store,tmp=await self._ready(None)
        try:
            self.assertEqual(await service.news.refresh_due(NOW,self._schedule(),"人设"),0)
        finally:
            await store.close(); tmp.cleanup()
        web=FakeHostWeb(results=[row()])
        service2,store2,tmp2=await self._ready(web)
        try:
            busy={"current":{"kind":"sleep","summary":"睡觉"}}
            self.assertEqual(await service2.news.refresh_due(NOW,busy,"人设"),0)
            self.assertEqual(web.search_calls,[])  # 日程不允许时不联网
            service2.config.network.news_daily_max=0
            self.assertEqual(await service2.news.refresh_due(NOW,self._schedule(),"人设"),0)
        finally:
            await store2.close(); tmp2.cleanup()


class ExploreTests(unittest.IsolatedAsyncioTestCase):
    async def _ready(self,web:Any,llm:Any|None=None)->tuple[InformationService,LifeStore,tempfile.TemporaryDirectory]:
        tmp=tempfile.TemporaryDirectory(); store=LifeStore(tmp.name); await store.initialize()
        config=base_config()
        config.network.enabled=True
        config.network.search_enabled=True
        config.network.association_enabled=False  # 规则基线关闭，分数确定性为 0
        service=InformationService(DummyContext(web),store,config,llm or OfflineLLM(),DummyLogger())
        return service,store,tmp

    async def test_explore_saves_note_with_sanitized_query_and_sources(self):
        web=FakeHostWeb(results=[row(title="发现",url="https://e.com/x")])
        service,store,tmp=await self._ready(web,llm=PlanLLM("10001 发现 新话题"))
        try:
            await store.sync_users(service.config.users.profiles)
            schedule={"current":{"kind":"leisure","summary":"闲暇"}}
            done=await service.explore_due(NOW,"人设",{"current_activity":"散步"},schedule,[])
            self.assertTrue(done)
            self.assertEqual(web.search_calls[0]["query"],"发现 新话题")  # 规划词已做隐私清洗
            notes=await store.recent_exploration_notes(NOW.timestamp(),10)
            self.assertEqual(len(notes),1)
            self.assertEqual(notes[0]["topic"],"发现")
            self.assertEqual(notes[0]["source_urls"],["https://e.com/x"])
            self.assertEqual(float(notes[0]["relevance_score"]),0.0)  # 关联关闭 → 不产生主动契机
        finally:
            await store.close(); tmp.cleanup()

    async def test_explore_skips_when_schedule_or_quota_or_host_missing(self):
        service,store,tmp=await self._ready(FakeHostWeb(results=[row()]))
        try:
            sleep_schedule={"current":{"kind":"sleep","summary":"睡觉"}}
            self.assertFalse(await service.explore_due(NOW,"人设",{},sleep_schedule,[]))
            service.config.network.news_daily_max=0
            self.assertFalse(await service.explore_due(NOW,"人设",{},{"current":{"kind":"leisure"}},[]))
            service.config.network.news_daily_max=1
            service.ctx.web=None
            self.assertFalse(await service.explore_due(NOW,"人设",{},{"current":{"kind":"leisure"}},[]))
        finally:
            await store.close(); tmp.cleanup()


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_sources_reflect_host_capability(self):
        service,store,tmp=await make_service(FakeHostWeb())
        try:
            service.config.network.enabled=True
            status=await service.status(NOW)
            self.assertEqual(status["sources"],1)
            service.ctx.web=None
            status=await service.status(NOW)
            self.assertEqual(status["sources"],0)
        finally:
            await store.close(); tmp.cleanup()
