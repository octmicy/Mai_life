"""通过统一搜索 API 读取低频新闻并按需抓取正文。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any

from .feed_parser import readable_text
from .http_client import HttpClient
from .search_service import SearchResponse,SearchResult,SearchService


class NewsService:
    def __init__(self,store:Any,config:Any,search:SearchService,http:HttpClient,logger:Any)->None:
        self.store=store; self.config=config; self.search=search; self.http=http; self.logger=logger

    def update_config(self,config:Any)->None:self.config=config

    @staticmethod
    def _day_bounds(now:Any)->tuple[float,float]:
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        return start.timestamp(),(start+timedelta(days=1)).timestamp()

    async def refresh_due(self,now:Any,schedule:dict[str,Any])->int:
        """在允许的空闲日程轮换兴趣词，执行一次逻辑新闻搜索并保存去重结果。"""
        cfg=self.config.news
        if not self.config.information.enabled or not cfg.enabled or int(cfg.daily_max)<=0:return 0
        current=schedule.get("current") or {}
        if str(current.get("kind") or "") not in set(cfg.allowed_schedule_types):return 0
        start,end=self._day_bounds(now)
        if await self.store.search_attempt_count("news",start,end)>=int(cfg.daily_max):return 0
        completed=await self.store.search_success_count("news",start,end)
        topics=[str(item).strip() for item in cfg.interest_topics if str(item).strip()]
        if not topics:return 0
        topic=topics[(now.toordinal()+completed)%len(topics)]
        response=await self.search.search(
            f"{topic} 最新新闻 过去24小时",operation="news",freshness="day",event_at=now.timestamp(),
        )
        if not response.results:return 0
        results=response.results[:min(5,int(self.config.search_api.max_results))]
        contents=await self._read_articles(results)
        changed=0; now_ts=now.timestamp(); retention=int(cfg.retention_days)*86400
        for index,result in enumerate(results):
            uncited=bool(result.provider_generated and not result.url)
            summary=result.snippet
            if uncited:summary="Provider 生成、无外部引用："+summary
            stable=result.url or hashlib.sha256(f"{result.title}:{result.snippet}".encode()).hexdigest()
            item_id=hashlib.sha256(f"{response.provider_id}:{stable}".encode()).hexdigest()
            content=contents.get(index,"")[:int(cfg.max_article_chars)]
            digest=hashlib.sha256(json.dumps([result.title,summary,content],ensure_ascii=False).encode()).hexdigest()
            source_id=f"api:{response.provider_type}"+(":uncited" if uncited else "")
            if await self.store.upsert_news_item({
                "id":item_id,"source_id":source_id,"title":result.title,"url":result.url,
                "summary":summary,"content":content,"published_at":now_ts,"fetched_at":now_ts,
                "content_hash":digest,"expires_at":now_ts+retention,
            }):changed+=1
        return changed

    async def _read_articles(self,results:list[SearchResult])->dict[int,str]:
        """并发读取最多三篇公网正文；任一页面失败只降级为搜索摘要。"""
        limit=min(len(results),int(self.config.news.full_text_count),3)
        if limit<=0:return {}
        semaphore=asyncio.Semaphore(int(self.config.news.max_concurrency))
        async def read(index:int,result:SearchResult)->tuple[int,str]:
            if not result.url:return index,""
            async with semaphore:
                try:
                    response=await self.http.get(
                        result.url,timeout=float(self.config.search_api.timeout_seconds),max_bytes=2_000_000,
                        headers={"Accept":"text/html,text/plain;q=0.9"},public_only=True,
                    )
                    content_type=response.headers.get("content-type","").casefold()
                    if content_type and "text/html" not in content_type and "text/plain" not in content_type:return index,""
                    return index,readable_text(response.text(),int(self.config.news.max_article_chars))
                except Exception:return index,""
        values=await asyncio.gather(*(read(index,result) for index,result in enumerate(results[:limit])))
        return {index:content for index,content in values if content}
