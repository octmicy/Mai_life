"""Playwright 搜索结果页解析与浏览器搜索客户端。"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any,Awaitable,Callable
from urllib.parse import urlencode

import asyncio
import base64
import binascii
import re

from .http_client import HttpClient,HttpRequestError
from .search_models import SearchBackendError,SearchResponse,SearchResult


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




class PlaywrightSearchClient:
    """用 Chromium 搜索并解析自然结果；浏览器实例在多次搜索间复用。"""

    _BING_FRESHNESS={"day":'ex1:"ez5_19700_19701"',"week":'ex1:"ez5_19700_19702"',
                     "month":'ex1:"ez5_19700_19704"',"year":'ex1:"ez5_19700_19705"'}
    _DUCK_FRESHNESS={"day":"d","week":"w","month":"m","year":"y"}

    @staticmethod
    def search_url(engine:str,query:str,freshness:str)->str:
        """生成固定搜索引擎地址；不接受任意自定义 URL。"""
        kind=str(engine or "").strip().casefold(); value=str(freshness or "any").strip().casefold()
        params={"q":str(query or "").strip()}
        if kind=="bing":
            if value in PlaywrightSearchClient._BING_FRESHNESS:params["filters"]=PlaywrightSearchClient._BING_FRESHNESS[value]
            return "https://www.bing.com/search?"+urlencode(params)
        if kind=="duckduckgo":
            if value in PlaywrightSearchClient._DUCK_FRESHNESS:params["df"]=PlaywrightSearchClient._DUCK_FRESHNESS[value]
            return "https://html.duckduckgo.com/html/?"+urlencode(params)
        raise SearchBackendError("不支持的浏览器搜索引擎",error_class="invalid_response")

    def __init__(self,logger:Any,*,headless:bool=True,
                 playwright_factory:Callable[[],Awaitable[Any]]|None=None)->None:
        self.logger=logger; self._headless=bool(headless)
        self._playwright_factory=playwright_factory or self._start_playwright
        self._init_lock=asyncio.Lock(); self._search_lock=asyncio.Lock()
        self._playwright:Any|None=None; self._browser:Any|None=None; self._context:Any|None=None

    @staticmethod
    async def _start_playwright()->Any:
        from playwright.async_api import async_playwright
        return await async_playwright().start()

    async def _ensure_browser(self)->Any:
        if self._context is not None:return self._context
        async with self._init_lock:
            if self._context is not None:return self._context
            try:playwright=await self._playwright_factory()
            except ImportError as exc:
                raise SearchBackendError("Playwright 依赖未安装",error_class="playwright_unavailable") from exc
            browser=None
            try:browser=await playwright.chromium.launch(headless=self._headless)
            except Exception as exc:
                if playwright:await playwright.stop()
                raise SearchBackendError("Chromium 浏览器不可用",error_class="browser_unavailable") from exc
            try:
                context=await browser.new_context(locale="zh-CN",viewport={"width":1366,"height":768})
            except Exception as exc:
                await browser.close(); await playwright.stop()
                raise SearchBackendError("浏览器上下文创建失败",error_class="browser_unavailable") from exc
            self._playwright=playwright; self._browser=browser; self._context=context
            return context

    async def search(self,query:str,*,engine:str,freshness:str,
                     timeout_seconds:float,max_results:int)->SearchResponse:
        """执行一次固定引擎搜索，并返回自然结果。"""
        cleaned=" ".join(str(query or "").split())
        if not cleaned:raise SearchBackendError("搜索词为空",error_class="invalid_response")
        url=self.search_url(engine,cleaned,freshness)
        parser=BingSearchParser() if engine=="bing" else DuckDuckGoSearchParser()
        async with self._search_lock:
            context=await self._ensure_browser(); page=await context.new_page()
            try:
                await page.goto(url,timeout=max(1000,int(timeout_seconds*1000)),wait_until="domcontentloaded")
                html=await page.content()
            except Exception as exc:
                message=str(exc).casefold()
                if "timeout" in message or isinstance(exc,TimeoutError):
                    raise SearchBackendError("浏览器搜索超时",error_class="network") from exc
                if any(marker in message for marker in ("err_name_not_resolved","err_connection","err_timed_out","failed to navigate")):
                    raise SearchBackendError("浏览器搜索网络失败",error_class="network") from exc
                raise SearchBackendError("浏览器搜索失败",error_class="browser_error") from exc
            finally:await page.close()
            results=parser.parse(html,max_results)
            if not results:
                lowered=html.casefold()
                if any(marker in lowered for marker in ('class="b_captcha"',"challenge-form","are you a human","unusual traffic")):
                    raise SearchBackendError("搜索页面被验证码或反爬拦截",error_class="blocked")
                raise SearchBackendError("浏览器搜索未返回自然结果",error_class="empty_result")
            return SearchResponse(results=results,provider_type="playwright",cited=True,model=str(engine))

    async def close(self)->None:
        """释放页面上下文、浏览器与 Playwright 运行时。"""
        async with self._search_lock:
            context,browser,playwright=self._context,self._browser,self._playwright
            self._context=None; self._browser=None; self._playwright=None
            if context is not None:await context.close()
            if browser is not None:await browser.close()
            if playwright is not None:await playwright.stop()
