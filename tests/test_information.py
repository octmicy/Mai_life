from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from Mai_life.config import MaiLifeSettings,SearchProviderProfile,SocialGroupProfile,UserProfile
from Mai_life.core.storage import LifeStore,SCHEMA_VERSION
from Mai_life.information.feed_parser import readable_text
from Mai_life.information.http_client import HttpClient,HttpRequestError,HttpResponse
from Mai_life.information.information_service import InformationService
from Mai_life.information.search_service import SearchService
from Mai_life.plugin import MaiLifePlugin
import Mai_life.information.search_service as search_module


class DummyLogger:
    def __init__(self):self.messages=[]
    def __getattr__(self,name):return lambda *args,**kwargs:self.messages.append(" ".join(str(item) for item in args))


class OfflineLLM:
    def task_available(self,kind):return False


class BatchLLM:
    def __init__(self):self.calls=[]
    def task_available(self,kind):return kind in {"news","relevance"}
    async def generate_json(self,prompt,system,fallback,max_tokens=0,**kwargs):
        del prompt,system,max_tokens; kind=kwargs.get("request_type"); self.calls.append(kind)
        if kind=="news_batch_digest":return {"summary":"五条新闻的批量整理"}
        if kind=="external_self_association":return {"score":0.9,"reason":"相关","share_topic":"近期科技","motive":"想分享"}
        return fallback


class QueryLLM:
    def task_available(self,kind):return kind=="search"
    async def generate_json(self,prompt,system,fallback,max_tokens=0,**kwargs):
        del prompt,system,max_tokens
        if kwargs.get("request_type")=="search_query_planning":
            return {"topic":"隐私清洗测试","query":"10001 小麦 秘密群 test@example.com https://private.example/a 科技","reason":"测试"}
        return fallback


class DummyContext:pass


class LocalHandler(BaseHTTPRequestHandler):
    calls=[]
    def log_message(self,format,*args):pass

    def _key(self):
        auth=self.headers.get("Authorization","")
        return auth.removeprefix("Bearer ") or self.headers.get("X-API-Key","")

    def _record(self,body:dict|None=None):
        self.calls.append({"path":self.path.split("?",1)[0],"key":self._key(),"body":body or {},"query":self.path})

    def _error(self,key):
        if key=="bad-auth":self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"invalid api key"}'); return True
        if key=="rate":self.send_response(429); self.send_header("Retry-After","60"); self.end_headers(); self.wfile.write(b'{"error":"rate limit"}'); return True
        if key=="quota":self.send_response(429); self.end_headers(); self.wfile.write(b'{"error":"insufficient_quota"}'); return True
        if key=="server":self.send_response(503); self.end_headers(); self.wfile.write(b'unavailable'); return True
        return False

    def _json(self,payload):
        body=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        key=self._key(); self._record()
        if self._error(key):return
        if self.path.startswith("/you"):
            self._json({"hits":[] if key=="empty" else [{"title":"You 结果","url":"https://example.com/you","description":"You 摘要"}]}); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        length=int(self.headers.get("Content-Length","0") or 0); raw=self.rfile.read(length)
        try:body=json.loads(raw or b"{}")
        except json.JSONDecodeError:body={}
        key=self._key(); self._record(body)
        if self._error(key):return
        path=self.path.split("?",1)[0]
        if path=="/bocha":
            values=[] if key=="empty" else ([{
                "name":f"标题 {key}","url":f"https://example.com/?token={key}",
                "summary":f"摘要 {key}",
            }] if key=="echo-secret" else [
                {"name":f"博查结果 {index}","url":f"https://example.com/news/{index}","summary":f"第 {index} 条摘要"}
                for index in range(1,6)
            ])
            self._json({"code":200,"data":{"webPages":{"value":values}}}); return
        if path=="/tavily":
            self._json({"results":[] if key=="empty" else [{"title":"Tavily 结果","url":"https://example.com/tavily","content":"Tavily 摘要"}]}); return
        if path=="/v1/responses":
            content=[] if key=="empty" else [{"type":"output_text","text":"Responses 联网回答",
                "annotations":[{"type":"url_citation","url":"https://example.com/responses","title":"Responses 来源"}]}]
            self._json({"model":"grok-web","output":[{"type":"message","content":content}],
                        "usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}); return
        if path=="/v1/chat/completions":
            content="" if key=="empty" else "Chat 联网模型生成的回答，没有提供外部链接。"
            self._json({"model":"grok-online","choices":[{"message":{"content":content}}],
                        "usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}); return
        self.send_response(404); self.end_headers()


class InformationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        LocalHandler.calls=[]; self.server=ThreadingHTTPServer(("127.0.0.1",0),LocalHandler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_port}"
        self.old_endpoints=dict(search_module._ENDPOINTS)
        search_module._ENDPOINTS.update({"bocha":self.base+"/bocha","tavily":self.base+"/tavily","you":self.base+"/you"})
        self.now=datetime(2026,7,13,10,0,tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        search_module._ENDPOINTS.clear(); search_module._ENDPOINTS.update(self.old_endpoints)
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        await self.store.close(); self.tmp.cleanup()

    def _provider(self,kind,key="good"):
        custom=kind.startswith("openai_")
        return SearchProviderProfile(enabled=True,provider_type=kind,api_keys=[key],
            endpoint=self.base+"/v1" if custom else "",model="grok-web" if kind=="openai_responses" else "grok-online" if custom else "")

    async def test_all_five_provider_protocols_are_normalized(self):
        for kind in ("bocha","tavily","you","openai_responses","openai_chat"):
            with self.subTest(provider=kind):
                config=MaiLifeSettings(); config.search_api.providers=[self._provider(kind)]
                service=SearchService(config,HttpClient(DummyLogger()),self.store,DummyLogger())
                response=await service.search("人工智能",operation="search")
                self.assertEqual(response.provider_type,kind); self.assertTrue(response.results)
                if kind=="openai_chat":
                    self.assertFalse(response.cited); self.assertTrue(response.results[0].provider_generated)
                else:self.assertTrue(response.cited)
        responses_call=next(item for item in LocalHandler.calls if item["path"]=="/v1/responses")
        self.assertEqual(responses_call["body"]["tools"],[{"type":"web_search"}])
        usage=await self.store.usage_summary(0)
        self.assertEqual(sum(int(item["total_tokens"] or 0) for item in usage if item["source"]=="search_api_model"),25)

    async def test_auth_failure_switches_key_and_never_persists_raw_keys(self):
        logger=DummyLogger(); config=MaiLifeSettings()
        config.search_api.providers=[SearchProviderProfile(enabled=True,provider_type="bocha",api_keys=["bad-auth","good"])]
        service=SearchService(config,HttpClient(logger),self.store,logger)
        response=await service.search("科技"); self.assertTrue(response.results)
        health=await service.health_snapshot(); states={item["fingerprint"]:item["status"] for item in health[0]["keys"]}
        self.assertEqual(states[service.key_fingerprint("bad-auth")],"disabled")
        self.assertEqual([item["key"] for item in LocalHandler.calls],["bad-auth","good"])
        database_text=str([tuple(row) for row in self.store.conn.execute("SELECT * FROM search_key_runtime")])
        event_text=str([tuple(row) for row in self.store.conn.execute("SELECT * FROM search_api_events")])
        self.assertNotIn("bad-auth",database_text+event_text+str(logger.messages)); self.assertNotIn("good",database_text+event_text)
        LocalHandler.calls=[]
        restarted=SearchService(config,HttpClient(logger),self.store,logger)
        self.assertTrue((await restarted.search("科技")).results)
        self.assertEqual([item["key"] for item in LocalHandler.calls],["good"])
        restarted.update_config(config); LocalHandler.calls=[]
        self.assertTrue((await restarted.search("科技")).results)
        self.assertEqual([item["key"] for item in LocalHandler.calls],["bad-auth","good"])

    async def test_rate_limit_and_quota_apply_different_cooldowns(self):
        for key,minimum in (("rate",55),("quota",23*3600)):
            LocalHandler.calls=[]; config=MaiLifeSettings()
            config.search_api.providers=[SearchProviderProfile(enabled=True,provider_type="bocha",api_keys=[key,"good"])]
            service=SearchService(config,HttpClient(DummyLogger()),self.store,DummyLogger()); before=self.now.timestamp()
            self.assertTrue((await service.search("科技")).results)
            state=await self.store.get_search_key_runtime(service.providers()[0][0],service.key_fingerprint(key))
            self.assertEqual(state["status"],"cooldown")
            # 使用现实 time.time 写入，测试只验证相对当前墙钟的最小冷却。
            import time
            self.assertGreaterEqual(float(state["cooldown_until"])-time.time(),minimum)

    async def test_service_failure_skips_backup_key_and_fails_over_provider(self):
        config=MaiLifeSettings(); config.search_api.providers=[
            SearchProviderProfile(enabled=True,provider_type="bocha",api_keys=["server","must-not-run"]),
            SearchProviderProfile(enabled=True,provider_type="tavily",api_keys=["good"]),
        ]
        service=SearchService(config,HttpClient(DummyLogger()),self.store,DummyLogger())
        response=await service.search("科技")
        self.assertEqual(response.provider_type,"tavily")
        self.assertEqual([item["key"] for item in LocalHandler.calls],["server","good"])
        LocalHandler.calls=[]
        self.assertEqual((await service.search("科技")).provider_type,"tavily")
        self.assertEqual([item["key"] for item in LocalHandler.calls],["good"])

    async def test_empty_result_uses_next_provider_without_key_penalty(self):
        config=MaiLifeSettings(); config.search_api.providers=[
            SearchProviderProfile(enabled=True,provider_type="bocha",api_keys=["empty"]),
            SearchProviderProfile(enabled=True,provider_type="tavily",api_keys=["good"]),
        ]
        service=SearchService(config,HttpClient(DummyLogger()),self.store,DummyLogger())
        response=await service.search("科技"); self.assertEqual(response.provider_type,"tavily")
        state=await self.store.get_search_key_runtime(service.providers()[0][0],service.key_fingerprint("empty"))
        self.assertEqual(state["status"],"healthy"); self.assertEqual(state["failure_count"],0)

    async def test_provider_cannot_echo_raw_key_into_normalized_result(self):
        key="echo-secret"
        config=MaiLifeSettings(); config.search_api.providers=[self._provider("bocha",key)]
        service=SearchService(config,HttpClient(DummyLogger()),self.store,DummyLogger())
        response=await service.search("科技")
        self.assertNotIn(key,str(response)); self.assertEqual(response.results[0].url,""); self.assertFalse(response.cited)
        database=str([tuple(row) for row in self.store.conn.execute("SELECT * FROM search_key_runtime")])
        database+=str([tuple(row) for row in self.store.conn.execute("SELECT * FROM search_api_events")])
        self.assertNotIn(key,database)

    async def test_news_runs_once_keeps_five_reads_three_articles_and_batches_models(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.news.enabled=True
        config.information.association_threshold=0.2; config.search_api.providers=[self._provider("bocha")]
        llm=BatchLLM(); service=InformationService(DummyContext(),self.store,config,llm,DummyLogger())
        article_calls=[]
        async def article_get(url,**kwargs):
            article_calls.append(url)
            return HttpResponse(200,url,{"content-type":"text/html; charset=utf-8"},
                "<article><p>这是足够长的正文测试内容，用于验证正文读取数量上限。</p></article>".encode())
        service.http.get=article_get
        schedule={"current":{"kind":"leisure","summary":"休息"},"next":None}
        first=await service.tick(self.now,"喜欢科技",await self.store.get_state(),schedule,[])
        second=await service.tick(self.now+timedelta(minutes=10),"喜欢科技",await self.store.get_state(),schedule,[])
        items=await self.store.recent_news_items(self.now.timestamp(),10)
        self.assertEqual(first,{"news":5,"associated":5,"search":0}); self.assertEqual(second["news"],0)
        self.assertEqual(len(items),5); self.assertEqual(len(article_calls),3)
        self.assertEqual(llm.calls.count("news_batch_digest"),1)
        self.assertEqual(llm.calls.count("external_self_association"),1)
        news_calls=[item for item in LocalHandler.calls if item["path"]=="/bocha"]
        self.assertEqual(len(news_calls),1); self.assertIn("过去24小时",news_calls[0]["body"]["query"])

    async def test_custom_uncited_news_is_saved_with_explicit_marker(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.news.enabled=True
        config.search_api.providers=[self._provider("openai_chat")]
        service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        schedule={"current":{"kind":"rest"},"next":None}
        await service.tick(self.now,"人格",await self.store.get_state(),schedule,[])
        items=await self.store.recent_news_items(self.now.timestamp(),5)
        self.assertEqual(len(items),1); self.assertIn("Provider 生成、无外部引用",items[0]["summary"])
        self.assertEqual(items[0]["url"],""); self.assertTrue(items[0]["source_id"].endswith(":uncited"))

    async def test_active_search_scrubs_qq_auto_names_email_and_url(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.search.enabled=True
        config.search.include_chat_topics=True; config.search_api.providers=[self._provider("bocha")]
        config.users.profiles=[UserProfile(user_id="10001")]
        config.social.groups=[SocialGroupProfile(group_id="20001")]
        await self.store.sync_users(config.users.profiles); await self.store.update_user_display_name("10001","小麦")
        await self.store.upsert_group_directory("20001","秘密群","group-stream",self.now.timestamp())
        service=InformationService(DummyContext(),self.store,config,QueryLLM(),DummyLogger())
        schedule={"current":{"kind":"leisure"},"next":None}
        self.assertTrue(await service.explore_due(self.now,"人格",await self.store.get_state(),schedule,["秘密群里的话题"]))
        query=next(item["body"]["query"] for item in LocalHandler.calls if item["path"]=="/bocha")
        for private in ("10001","小麦","秘密群","test@example.com","private.example"):
            self.assertNotIn(private,query)

    async def test_web_search_tool_scrubs_private_terms_and_allows_repeated_calls(self):
        config=MaiLifeSettings(); config.information.enabled=True
        config.search_api.tool_enabled=True
        config.search_api.providers=[self._provider("bocha")]
        config.users.profiles=[UserProfile(user_id="10001")]
        config.social.groups=[SocialGroupProfile(group_id="20001")]
        await self.store.sync_users(config.users.profiles); await self.store.update_user_display_name("10001","小麦")
        await self.store.upsert_group_directory("20001","秘密群","group-stream",self.now.timestamp())
        service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())

        first=await service.search_for_tool(
            "10001 小麦 秘密群 test@example.com https://private.example/a 人工智能新闻",
            self.now,result_limit=2,freshness="day",
        )
        self.assertTrue(first["success"]); self.assertEqual(len(first["results"]),2)
        self.assertIn("外部联网搜索结果",first["content"]); self.assertIn("https://example.com/news/1",first["content"])
        for private in ("10001","小麦","秘密群","test@example.com","private.example"):
            self.assertNotIn(private,first["query"])
        call=next(item for item in LocalHandler.calls if item["path"]=="/bocha")
        self.assertEqual(call["body"].get("freshness"),"oneDay")

        second=await service.search_for_tool("第二次搜索",self.now+timedelta(seconds=1),result_limit=3)
        self.assertTrue(second["success"]); self.assertEqual(len(second["results"]),3)
        start=self.now.replace(hour=0,minute=0,second=0,microsecond=0)
        self.assertEqual(await self.store.search_attempt_count(
            "tool_search",start.timestamp(),(start+timedelta(days=1)).timestamp(),
        ),2)

    async def test_web_search_tool_respects_connected_discovery_switch(self):
        config=MaiLifeSettings(); config.search_api.providers=[self._provider("bocha")]
        service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        result=await service.search_for_tool("人工智能",self.now)
        self.assertFalse(result["success"]); self.assertIn("总开关未开启",result["content"])
        self.assertEqual(LocalHandler.calls,[])

    async def test_plugin_tool_entry_clamps_parameters_and_forwards_environment_time(self):
        class StubInformation:
            def __init__(self):self.calls=[]
            async def search_for_tool(self,query,now,**kwargs):
                self.calls.append((query,now,kwargs))
                return {"success":True,"content":"工具调用成功"}

        class StubEnvironment:
            def now(_self):return self.now

        config=MaiLifeSettings(); plugin=MaiLifePlugin()
        plugin.set_plugin_config(config.model_dump(mode="python"))
        information=StubInformation(); plugin._information=information; plugin._env=StubEnvironment()
        result=await plugin.tool_web_search(query="人工智能",result_limit=99,freshness="DAY")
        self.assertTrue(result["success"]); self.assertEqual(len(information.calls),1)
        query,called_at,options=information.calls[0]
        self.assertEqual(query,"人工智能"); self.assertEqual(called_at,self.now)
        self.assertEqual(options,{"result_limit":5,"freshness":"day"})

    async def test_all_services_failed_keeps_existing_notes_and_creates_no_fake_record(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.search.enabled=True
        config.search_api.providers=[SearchProviderProfile(enabled=True,provider_type="bocha",api_keys=["server"])]
        service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        await self.store.save_exploration_note({"id":"existing","topic":"旧记录","query":"旧查询","summary":"缓存",
            "source_urls":[],"created_at":self.now.timestamp()-60,"expires_at":self.now.timestamp()+3600})
        result=await service.explore_due(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},[])
        notes=await self.store.recent_exploration_notes(self.now.timestamp(),5)
        self.assertFalse(result); self.assertEqual([item["id"] for item in notes],["existing"])

    async def test_empty_results_consume_one_logical_daily_attempt_without_request_loop(self):
        config=MaiLifeSettings(); config.information.enabled=True
        config.news.enabled=True; config.search.enabled=True
        config.search_api.providers=[self._provider("bocha","empty")]
        service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        schedule={"current":{"kind":"leisure"},"next":None}
        first=await service.tick(self.now,"人格",await self.store.get_state(),schedule,[])
        second=await service.tick(self.now+timedelta(minutes=10),"人格",await self.store.get_state(),schedule,[])
        self.assertEqual(first,{"news":0,"associated":0,"search":0})
        self.assertEqual(second,{"news":0,"associated":0,"search":0})
        calls=[item for item in LocalHandler.calls if item["path"]=="/bocha"]
        self.assertEqual(len(calls),2)
        start=self.now.replace(hour=0,minute=0,second=0,microsecond=0)
        self.assertEqual(await self.store.search_attempt_count("news",start.timestamp(),(start+timedelta(days=1)).timestamp()),1)
        self.assertEqual(await self.store.search_attempt_count("search",start.timestamp(),(start+timedelta(days=1)).timestamp()),1)

    async def test_active_search_skips_query_model_without_available_provider(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.search.enabled=True
        service=InformationService(DummyContext(),self.store,config,QueryLLM(),DummyLogger())
        async def fail_if_planned(*args,**kwargs):
            del args,kwargs
            raise AssertionError("无可用搜索服务时不应规划查询词")
        service._plan_query=fail_if_planned
        result=await service.explore_due(
            self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},[],
        )
        self.assertFalse(result); self.assertEqual(LocalHandler.calls,[])

    async def test_default_disabled_and_ssrf_guard(self):
        config=MaiLifeSettings(); service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},[])
        self.assertEqual(result,{"news":0,"associated":0,"search":0}); self.assertEqual(LocalHandler.calls,[])
        with self.assertRaises(HttpRequestError):HttpClient.validate_public_url(self.base+"/article")
        self.assertEqual(readable_text("<script>bad()</script><p>useful article paragraph</p>",100),"useful article paragraph")

    def test_schema_v9(self):self.assertEqual(SCHEMA_VERSION,9)


if __name__=="__main__":unittest.main()
