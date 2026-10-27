"""通过宿主统一联网服务读取低频新闻，并按需用 web.fetch 抓取正文。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any

from .host_search import HostSearchService


class NewsService:
    def __init__(self,store:Any,config:Any,search:HostSearchService,logger:Any,llm:Any,bot_name:str="麦麦")->None:
        self.store=store; self.config=config; self.search=search; self.logger=logger
        self.llm=llm; self.bot_name=bot_name

    def update_config(self,config:Any)->None:self.config=config

    def set_bot_name(self,name:str)->None:
        clean=str(name or "").strip()
        if clean:self.bot_name=clean

    @staticmethod
    def _day_bounds(now:Any)->tuple[float,float]:
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        return start.timestamp(),(start+timedelta(days=1)).timestamp()

    async def refresh_due(self,now:Any,schedule:dict[str,Any],personality:str)->int:
        """在允许的空闲日程由人格规划新闻词，经宿主统一联网执行一次搜索并保存去重结果。"""
        cfg=self.config.news
        if not self.config.information.enabled or not cfg.enabled or int(cfg.daily_max)<=0:return 0
        current=schedule.get("current") or {}
        if str(current.get("kind") or "") not in set(cfg.allowed_schedule_types):return 0
        start,end=self._day_bounds(now)
        if await self.store.search_attempt_count("news",start,end)>=int(cfg.daily_max):return 0
        if not self.search.available():return 0
        query=await self.search.sanitize_query(await self._plan_query(now,personality,schedule))
        if not query:
            return 0
        response=await self.search.search(
            query,operation="news",max_results=int(self.config.search_api.max_results),
        )
        results=self._recent_results(response["results"],now.timestamp())
        if not results:
            if response["results"]:
                self.logger.info("[MaiLife] 新闻搜索结果全部早于 3 天，未写入新闻库")
            return 0
        results=results[:min(5,int(self.config.search_api.max_results))]
        contents=await self._read_articles(results)
        changed=0; now_ts=now.timestamp(); retention=int(cfg.retention_days)*86400
        service=str(response.get("service") or "host")
        for index,result in enumerate(results):
            uncited=not result["url"]
            summary=result["snippet"]
            if uncited:summary="宿主结果、无外部链接："+summary
            # 去重键只由 url 或 title+snippet 决定：同一条新闻不会因服务名变化而重复入库。
            stable=result["url"] or hashlib.sha256(f"{result['title']}:{result['snippet']}".encode()).hexdigest()
            item_id=hashlib.sha256(stable.encode()).hexdigest()
            content=contents.get(index,"")[:int(cfg.max_article_chars)]
            digest=hashlib.sha256(json.dumps([result["title"],summary,content],ensure_ascii=False).encode()).hexdigest()
            source_id=f"host:{service}"+(":uncited" if uncited else "")
            if await self.store.upsert_news_item({
                "id":item_id,"source_id":source_id,"title":result["title"],"url":result["url"],
                "summary":summary,"content":content,"published_at":now_ts,"fetched_at":now_ts,
                "content_hash":digest,"expires_at":now_ts+retention,
            }):changed+=1
        return changed

    @staticmethod
    def _recent_results(rows:list[dict[str,Any]],now_ts:float)->list[dict[str,Any]]:
        """新闻只要近 3 天的：宿主返回的 published_at 可解析且早于阈值时丢弃，解析不了则保留。"""
        cutoff=now_ts-3*86400
        kept=[]
        for row in rows:
            published=row.get("published_at")
            if published:
                try:
                    if float(published)<cutoff:continue
                except (TypeError,ValueError):
                    pass  # ISO 字符串等非数值格式无法本地判新旧，保守保留
            kept.append(row)
        return kept

    async def _plan_query(self,now,personality:str,schedule:dict)->str:
        """让麦麦根据人格自主决定此刻想了解什么新闻；模型不可用时跳过。"""
        if not self.llm.task_available("news"):
            return ""
        current=schedule.get("current") or {}
        result=await self.llm.generate_json(
            f"你是{self.bot_name}。根据你的人设与当前状态，自主决定此刻最想了解的一类新闻，给出一个简短、"
            "适合网页搜索的查询词（不超过 40 字）。不要搜索用户身份、账号、群名、私密关系或聊天原句。\n"
            + json.dumps({
                "personality": personality[:1200],
                "current_activity": current.get("summary") or "",
            }, ensure_ascii=False)
            + "\n只返回JSON：{\"query\":\"\"}。",
            f"你自主决定{self.bot_name}此刻读什么新闻。", {"query": ""}, max_tokens=0,
            task_kind="news", request_type="news_query_planning",
        )
        if not isinstance(result,dict):
            return ""
        query=" ".join(str(result.get("query") or "").split())[:100]
        return query

    async def _read_articles(self,results:list[dict[str,Any]])->dict[int,str]:
        """并发经宿主 web.fetch 读取最多三篇正文；任一页面失败只降级为搜索摘要。"""
        limit=min(len(results),int(self.config.news.full_text_count),3)
        if limit<=0:return {}
        semaphore=asyncio.Semaphore(int(self.config.news.max_concurrency))
        async def read(index:int,result:dict[str,Any])->tuple[int,str]:
            if not result["url"]:return index,""
            async with semaphore:
                return index,await self.search.fetch_article(result["url"])
        values=await asyncio.gather(*(read(index,result) for index,result in enumerate(results[:limit])))
        max_chars=int(self.config.news.max_article_chars)
        return {index:content[:max_chars] for index,content in values if content}
