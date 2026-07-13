"""新闻源刷新、正文读取与 B 站插件 API 归一化。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from .feed_parser import FeedEntry,parse_feed,readable_text
from .http_client import HttpClient,HttpRequestError


class NewsService:
    def __init__(self,ctx:Any,store:Any,config:Any,http:HttpClient,logger:Any)->None:
        self.ctx=ctx; self.store=store; self.config=config; self.http=http; self.logger=logger

    def update_config(self,config:Any)->None:self.config=config

    @staticmethod
    def _source_id(source:Any)->str:
        configured=str(getattr(source,"source_id","") or "").strip()
        if configured:return configured[:120]
        seed=f"{getattr(source,'source_type','rss')}:{getattr(source,'name','')}:{getattr(source,'url','')}:{getattr(source,'api_name','')}"
        return "source-"+hashlib.sha1(seed.encode()).hexdigest()[:12]

    def _backoff(self,runtime:dict[str,Any])->float:
        failures=max(0,int(runtime.get("failure_count") or 0))
        minutes=min(int(self.config.information.maximum_backoff_minutes),
                    int(self.config.information.initial_backoff_minutes)*(2**min(failures,6)))
        return max(60,minutes*60)

    async def refresh_due(self,now:Any)->int:
        if not self.config.information.enabled or not self.config.news.enabled:return 0
        sources=[item for item in self.config.news.sources if item.enabled]
        if not sources:return 0
        semaphore=asyncio.Semaphore(int(self.config.news.max_concurrency))
        async def run(source:Any)->int:
            async with semaphore:return await self._refresh_one(source,now)
        results=await asyncio.gather(*(run(source) for source in sources),return_exceptions=True)
        total=0
        for source,result in zip(sources,results):
            if isinstance(result,Exception):
                self.logger.warning(f"[MaiLife] 新闻源任务异常 source={self._source_id(source)}: {str(result)[:200]}")
            else:total+=int(result)
        return total

    async def _refresh_one(self,source:Any,now:Any)->int:
        source_id=self._source_id(source); key=f"news:{source_id}"; runtime=await self.store.get_information_source_runtime(key)
        now_ts=now.timestamp(); refresh=int(self.config.news.refresh_minutes)*60
        if float(runtime.get("next_retry_at") or 0)>now_ts:return 0
        if float(runtime.get("last_success_at") or 0) and now_ts-float(runtime["last_success_at"])<refresh:return 0
        try:
            if str(source.source_type)=="bilibili_api":
                entries=await self._from_plugin_api(source)
                etag=last_modified=""
            else:
                entries,etag,last_modified=await self._from_feed(source,runtime)
            changed=await self._store_entries(source_id,entries,now)
            await self.store.save_information_source_runtime(
                key,now=now_ts,success=True,next_retry_at=now_ts+refresh,etag=etag,last_modified=last_modified,
            )
            return changed
        except Exception as exc:
            await self.store.save_information_source_runtime(
                key,now=now_ts,success=False,next_retry_at=now_ts+self._backoff(runtime),error=str(exc),
            )
            self.logger.info(f"[MaiLife] 新闻源暂不可用 source={source_id}，已进入退避: {str(exc)[:160]}")
            return 0

    async def _from_feed(self,source:Any,runtime:dict[str,Any])->tuple[list[FeedEntry],str,str]:
        headers={"Accept":"application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9"}
        if runtime.get("etag"):headers["If-None-Match"]=str(runtime["etag"])
        if runtime.get("last_modified"):headers["If-Modified-Since"]=str(runtime["last_modified"])
        response=await self.http.get(
            str(source.url),timeout=float(self.config.news.timeout_seconds),max_bytes=3_000_000,headers=headers,
        )
        if response.status==304:return [],str(runtime.get("etag") or ""),str(runtime.get("last_modified") or "")
        entries=parse_feed(response.body)[:int(self.config.news.max_items_per_source)]
        if not entries:raise HttpRequestError("订阅源没有可解析条目")
        if self.config.news.fetch_full_text:
            semaphore=asyncio.Semaphore(int(self.config.news.max_concurrency))
            async def enrich(entry:FeedEntry)->FeedEntry:
                if not entry.url:return entry
                async with semaphore:
                    try:
                        article=await self.http.get(entry.url,timeout=float(self.config.news.timeout_seconds),max_bytes=2_000_000,
                                                    headers={"Accept":"text/html,text/plain;q=0.9"})
                        content_type=article.headers.get("content-type","").lower()
                        if not ("text/html" in content_type or "text/plain" in content_type or not content_type):return entry
                        text=readable_text(article.text(),int(self.config.news.max_article_chars))
                        return FeedEntry(entry.entry_id,entry.title,entry.url,entry.summary,text or entry.content,entry.published_at)
                    except Exception:return entry
            entries=list(await asyncio.gather(*(enrich(entry) for entry in entries)))
        return entries,response.headers.get("etag",""),response.headers.get("last-modified","")

    async def _from_plugin_api(self,source:Any)->list[FeedEntry]:
        api_name=str(source.api_name or "").strip()
        if not api_name:raise ValueError("B 站来源缺少插件 API 名称")
        result=await self.ctx.api.call(api_name,version="1",limit=int(self.config.news.max_items_per_source))
        values=self._item_list(result)
        entries=[]
        for item in values[:int(self.config.news.max_items_per_source)]:
            if not isinstance(item,dict):continue
            title=str(item.get("title") or item.get("name") or item.get("dynamic_text") or "B 站动态")
            url=str(item.get("url") or item.get("link") or item.get("jump_url") or "")
            summary=str(item.get("summary") or item.get("description") or item.get("content") or "")
            entry_id=str(item.get("id") or item.get("dynamic_id") or url or title)
            try:published=float(item.get("published_at") or item.get("timestamp") or item.get("created_at") or 0)
            except (TypeError,ValueError):published=0
            entries.append(FeedEntry(entry_id,title,url,summary,"",published))
        if not entries:raise ValueError("B 站插件 API 未返回可识别条目")
        return entries

    @staticmethod
    def _item_list(result:Any)->list[Any]:
        if isinstance(result,list):return result
        if not isinstance(result,dict):return []
        for key in ("items","results","messages","updates","data"):
            value=result.get(key)
            if isinstance(value,list):return value
            if isinstance(value,dict):
                nested=NewsService._item_list(value)
                if nested:return nested
        return []

    async def _store_entries(self,source_id:str,entries:list[FeedEntry],now:Any)->int:
        changed=0; now_ts=now.timestamp(); retention=int(self.config.news.retention_days)*86400
        for entry in entries:
            stable=entry.entry_id or entry.url or f"{entry.title}:{entry.published_at}"
            item_id=hashlib.sha256(f"{source_id}:{stable}".encode()).hexdigest()
            content=(entry.content or "")[:int(self.config.news.max_article_chars)]
            digest=hashlib.sha256(json.dumps([entry.title,entry.summary,content],ensure_ascii=False).encode()).hexdigest()
            if await self.store.upsert_news_item({
                "id":item_id,"source_id":source_id,"title":entry.title,"url":entry.url,
                "summary":entry.summary,"content":content,"published_at":entry.published_at,
                "fetched_at":now_ts,"content_hash":digest,"expires_at":now_ts+retention,
            }):changed+=1
        return changed
