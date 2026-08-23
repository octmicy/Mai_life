"""SearXNG 与通用 JSON 搜索连接器。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl,urlencode,urlsplit,urlunsplit

from .http_client import HttpClient,HttpRequestError


@dataclass(frozen=True)
class SearchResult:
    title:str
    url:str
    snippet:str


def _dotted(value:Any,path:str)->Any:
    current=value
    for part in str(path or "").split("."):
        if not part:continue
        if isinstance(current,dict):current=current.get(part)
        else:return None
    return current


class SearchService:
    def __init__(self,config:Any,http:HttpClient)->None:self.config=config; self.http=http
    def update_config(self,config:Any)->None:self.config=config

    @staticmethod
    def _with_query(endpoint:str,params:dict[str,str])->str:
        parts=urlsplit(endpoint); query=dict(parse_qsl(parts.query,keep_blank_values=True)); query.update(params)
        return urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),parts.fragment))

    async def search(self,query:str)->list[SearchResult]:
        cfg=self.config.search; endpoint=self.http.validate_url(str(cfg.endpoint))
        params={str(cfg.query_parameter or "q"):query}
        if cfg.connector=="searxng":params["format"]="json"
        headers={"Accept":"application/json"}
        if str(cfg.api_key or "").strip():headers["Authorization"]="Bearer "+str(cfg.api_key).strip()
        response=await self.http.get(
            self._with_query(endpoint,params),timeout=float(cfg.timeout_seconds),max_bytes=2_000_000,headers=headers,
        )
        try:payload=json.loads(response.text())
        except json.JSONDecodeError as exc:raise HttpRequestError("搜索接口未返回合法 JSON") from exc
        raw=_dotted(payload,str(cfg.results_path))
        if not isinstance(raw,list):raise HttpRequestError("搜索结果路径不是数组")
        results=[]
        for item in raw:
            if not isinstance(item,dict):continue
            title=str(item.get(str(cfg.title_field)) or "").strip()
            url=str(item.get(str(cfg.url_field)) or "").strip()
            snippet=str(item.get(str(cfg.snippet_field)) or "").strip()
            if not title and not snippet:continue
            if url:
                try:self.http.validate_url(url)
                except HttpRequestError:url=""
            results.append(SearchResult(title[:500] or "未命名结果",url[:2000],snippet[:3000]))
            if len(results)>=int(cfg.max_results):break
        if not results:raise HttpRequestError("搜索接口没有返回可用结果")
        return results
