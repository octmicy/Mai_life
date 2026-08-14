"""Playwright 搜索结果页解析与浏览器搜索客户端。"""
from __future__ import annotations

import base64
import binascii
import re
from html.parser import HTMLParser

from .http_client import HttpClient,HttpRequestError
from .search_models import SearchResult

_BING_REDIRECT=re.compile(r"(?:[?&])u=a1([^&]+)",re.I)


def _clean_text(value:str,limit:int)->str:
    return " ".join(value.split())[:limit]


def _validated_url(value:str)->str:
    url=value.strip()[:2000]
    try:HttpClient.validate_url(url)
    except HttpRequestError:return ""
    return url


def _bing_real_url(value:str)->str:
    """解码 Bing 结果的 u=a1<Base64URL> 重定向，失败时按普通链接校验。"""
    match=_BING_REDIRECT.search(value)
    if match:
        encoded=match.group(1)
        padding="="*(-len(encoded)%4)
        try:
            decoded=base64.urlsafe_b64decode(encoded+padding).decode("utf-8")
        except (UnicodeDecodeError,binascii.Error):decoded=value
        value=decoded
    return _validated_url(value)


class _SearchHTMLParser(HTMLParser):
    def __init__(self)->None:
        super().__init__(convert_charrefs=True)
        self._stack:list[str]=[]
        self._classes:list[str]=[]
        self.results:list[dict[str,str]]=[]
        self._current:dict[str,str]|None=None
        self._field=""

    @staticmethod
    def _class_value(attrs:list[tuple[str,str|None]])->str:
        return " ".join(str(value or "") for key,value in attrs if key=="class").casefold()

    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        self._stack.append(tag); self._classes.append(self._class_value(attrs))
        if self._current is not None:self._on_child(tag,self._classes[-1],attrs)

    def handle_endtag(self,tag:str)->None:
        if self._current is not None:self._close_child(tag)
        if self._stack:del self._stack[-1]
        if self._classes:del self._classes[-1]
        if tag==self._container_tag and self._current is not None:
            self._finish_result()

    def handle_data(self,data:str)->None:
        if self._current is None or not self._field:return
        self._current[self._field]=(self._current.get(self._field) or "")+data

    def parse(self,html:str,limit:int)->list[SearchResult]:
        """解析结果页 HTML，并按配置数量返回自然搜索结果。"""
        self.feed(html); self.close()
        return [SearchResult(title=item["title"],url=item["url"],snippet=item["snippet"])
                for item in self.results[:max(0,int(limit))]]

    def _finish_result(self)->None:
        result=self._current or {}
        title=_clean_text(result.get("title") or "",500)
        url=self._clean_url(result.get("url") or "")
        snippet=_clean_text(result.get("snippet") or "",3000)
        if title and url:self.results.append({"title":title,"url":url,"snippet":snippet})
        self._current=None; self._field=""

    def _close_child(self,tag:str)->None:
        if self._field and tag in {"a","p","span","div"}:self._field=""

    def _on_child(self,tag:str,classes:str,attrs:list[tuple[str,str|None]])->None:raise NotImplementedError
    def _clean_url(self,value:str)->str:return _validated_url(value)
    @property
    def _container_tag(self)->str:raise NotImplementedError


class BingSearchParser(_SearchHTMLParser):
    """解析 Bing 自然结果卡片，并还原重定向后的目标地址。"""

    def _on_child(self,tag:str,classes:str,attrs:list[tuple[str,str|None]])->None:
        if tag!="a" or "h2" not in self._stack:return
        href=next((str(value or "") for key,value in attrs if key=="href"),"")
        self._current["url"]=href; self._field="title"

    def _clean_url(self,value:str)->str:return _bing_real_url(value)
    @property
    def _container_tag(self)->str:return "li"

    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        classes=self._class_value(attrs)
        if tag=="li" and "b_algo" in classes.split():
            self._current={"title":"","url":"","snippet":""}
        super().handle_starttag(tag,attrs)

    def handle_data(self,data:str)->None:
        if self._current is not None and not self._field and any("b_caption" in item.split() for item in self._classes):
            self._field="snippet"
        super().handle_data(data)

    def _close_child(self,tag:str)->None:
        if self._field=="snippet" and tag=="p":self._field=""
        super()._close_child(tag)


class DuckDuckGoSearchParser(_SearchHTMLParser):
    """解析 DuckDuckGo HTML 结果页。"""

    def _on_child(self,tag:str,classes:str,attrs:list[tuple[str,str|None]])->None:
        words=classes.split()
        if tag=="a" and "result__a" in words:
            href=next((str(value or "") for key,value in attrs if key=="href"),"")
            self._current["url"]=href; self._field="title"
        elif tag in {"a","div","span"} and "result__snippet" in words:self._field="snippet"

    @property
    def _container_tag(self)->str:return "div"

    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        classes=self._class_value(attrs); words=classes.split()
        if tag=="div" and "result" in words and any(item.startswith("result__") or item=="results_links_normal" for item in words):
            self._current={"title":"","url":"","snippet":""}
        super().handle_starttag(tag,attrs)


