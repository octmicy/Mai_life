"""宿主统一联网（ctx.web.search / ctx.web.fetch）薄封装。

所有联网搜索与网页正文读取都走宿主统一服务（SDK 2.17+，manifest 声明 web.search/web.fetch）。
插件不再维护自有 Provider 降级链；宿主失败按配置重试几次，仍失败则记录历史并返回空结果。
"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from typing import Any


class HostSearchService:
    def __init__(self,ctx:Any,store:Any,config:Any,logger:Any)->None:
        self.ctx=ctx; self.store=store; self.config=config; self.logger=logger

    def update_config(self,config:Any)->None:self.config=config

    def _web(self)->Any:
        return getattr(self.ctx,"web",None)

    def available(self)->bool:
        """宿主联网能力是否在位（explore/news 规划前的本地检查，避免白耗模型 Token）。"""
        return self._web() is not None

    async def _private_query_terms(self)->list[str]:
        """联网前移除 QQ 号及 Host 自动读取的昵称、群名、群成员昵称；展示名称从不参与身份匹配。"""
        terms=[str(item.user_id) for item in self.config.users.profiles]
        terms.extend(str(item.group_id) for item in self.config.group.profiles)
        for item in await self.store.list_users(include_disabled=True):terms.append(str(item.get("display_name") or ""))
        for item in await self.store.list_group_directory(500):terms.append(str(item.get("group_name") or ""))
        terms.extend(await self.store.list_group_member_names())
        return [term for term in terms if term]

    async def sanitize_query(self,query:str)->str:
        """隐私清洗：NFKC 归一后，按整词边界移除 QQ 号、昵称、群名等本地私有多词禁词。

        只删除作为独立词元出现的禁词。Python 的 \\w 含 CJK，一段连续中文被视为单一词元，
        所以 '明天天气怎么样' 里的 '天气' 不会命中，只有独立的 '天气'（两侧为空白/标点/异类字符）才删。

        已移除早年的正则清洗与 URL 解码：那套规则把技术名词当裸域名/邮箱整词删掉，
        实测 'Next.js 15 升级指南' 被清成 '15 升级指南' 后返回全错结果。
        """
        text=unicodedata.normalize("NFKC",str(query or ""))
        for term in sorted({str(item).strip() for item in await self._private_query_terms() if str(item).strip()},
                           key=len,reverse=True):
            # 整词边界匹配：仅当禁词独立成词时才删除，长词优先避免短词先破坏长词。
            text=re.sub(rf"(?<!\w){re.escape(term)}(?!\w)"," ",text,flags=re.I)
        return " ".join(text.replace("\x00","").split())[:100]

    async def search(self,query:str,*,operation:str="search",max_results:int=5)->dict[str,Any]:
        """执行一次宿主搜索：失败按 max_attempts 重试；每次逻辑搜索记录 usage 事件与搜索历史。

        返回 {"results":[{title,url,snippet,summary,published_at}], "service", "error_class"}；
        不抛异常——调用方按空结果处理，错误细节看搜索历史。
        """
        web=self._web()
        attempts=max(1,int(getattr(self.config.network,"max_attempts",3) or 3))
        limit=max(1,min(20,int(max_results or 5)))
        started=time.perf_counter(); now=time.time(); last_error="host_unavailable"
        if web is not None:
            for attempt in range(attempts):
                try:
                    result=await web.search(query,max_results=limit)
                    hits=result.get("results") if isinstance(result,dict) else None
                    if not isinstance(hits,list):hits=[]
                    rows=[{"title":str(hit.get("title") or "")[:240],
                           "url":str(hit.get("url") or "").strip(),
                           "snippet":str(hit.get("snippet") or hit.get("summary") or "")[:1200],
                           "summary":str(hit.get("summary") or "")[:2000],
                           "published_at":hit.get("published_at") or ""}
                          for hit in hits if isinstance(hit,dict)]
                    service=str(result.get("service") or "host") if isinstance(result,dict) else "host"
                    await self._record(operation,query,service,True,"",
                                       (time.perf_counter()-started)*1000,len(rows),rows,now)
                    return {"results":rows,"service":service,"error_class":""}
                except Exception as exc:
                    last_error=_error_class(exc)
                    if attempt<attempts-1:
                        await asyncio.sleep(min(0.5+attempt,2.0))  # 短退避后重试
        await self._record(operation,query,"host",False,last_error,
                           (time.perf_counter()-started)*1000,0,[],now)
        return {"results":[],"service":"","error_class":last_error}

    async def fetch_article(self,url:str)->str:
        """读取公网网页正文（宿主已提取）；失败返回空串，由调用方降级为摘要。"""
        web=self._web()
        if web is None or not str(url or "").strip():return ""
        try:
            result=await web.fetch(str(url).strip())
            return str(result.get("content") or "") if isinstance(result,dict) else ""
        except Exception as exc:
            self.logger.debug(f"[MaiLife] 网页正文读取失败 url={url[:120]}: {exc}")
            return ""

    async def _record(self,operation:str,query:str,service:str,success:bool,error_class:str,
                      latency_ms:float,result_count:int,rows:list[dict[str,Any]],event_time:float)->None:
        """usage 计数（search_api_events）+ 可选本地搜索历史；记录失败不影响搜索。"""
        try:
            await self.store.record_search_api_event(
                created_at=event_time,operation=operation[:40],provider_id="host",
                provider_type=service[:40] or "host",key_fingerprint="",success=success,
                status_code=0,latency_ms=latency_ms,result_count=int(result_count),
                error_class=error_class[:40],
            )
        except Exception as exc:
            self.logger.debug(f"[MaiLife] 搜索 usage 记录失败: {exc}")
        if not getattr(self.config.network,"history_enabled",True):return
        try:
            retention=max(1,min(365,int(getattr(self.config.network,"history_retention_days",30) or 30)))
            history=[{"title":row["title"],"url":row["url"][:2000],"snippet":row["snippet"][:900],
                      "provider_generated":False} for row in rows]
            await self.store.record_search_history(
                created_at=event_time,operation=operation[:40],query=str(query)[:300],
                provider_type=service[:40] or "host",success=bool(result_count),
                result_count=int(result_count),error_class=error_class[:40],
                duration_ms=latency_ms,results=history,
                expires_at=event_time+retention*86400,
            )
        except Exception as exc:
            self.logger.debug("[MaiLife] 搜索历史记录失败，不影响搜索主链")


def _error_class(exc:BaseException)->str:
    """把宿主异常粗分类为稳定的短码，供搜索历史与排查展示。"""
    text=f"{type(exc).__name__}: {exc}"[:200]
    lowered=text.casefold()
    if "not_enabled" in lowered or "未启用" in text or "not enable" in lowered:return "host_disabled"
    if "timeout" in lowered or "超时" in text:return "timeout"
    if "limit" in lowered or "上限" in text or "invalid" in lowered or "参数" in text:return "invalid_param"
    return "host_error"
