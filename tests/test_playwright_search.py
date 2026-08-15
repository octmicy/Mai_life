from __future__ import annotations

import asyncio
from pathlib import Path
import unittest

from Mai_life.information.playwright_search import BingSearchParser,DuckDuckGoSearchParser,PlaywrightSearchClient
from Mai_life.information.search_models import SearchBackendError


class SearchResultParserTests(unittest.TestCase):
    def test_bing_parser_extracts_organic_redirect_and_snippet(self):
        parser=BingSearchParser()
        html=Path("tests/fixtures/bing_search.html").read_text(encoding="utf-8")
        results=parser.parse(html,5)
        self.assertEqual([item.title for item in results[:2]],["人工智能 - Microsoft","人工智能报告"])
        self.assertEqual(results[0].url,"https://www.microsoft.com/ai")
        self.assertIn("人工智能技术",results[0].snippet)
        self.assertTrue(all("bing.com/ck/a" not in item.url for item in results))
        self.assertTrue(all(item.url.startswith("https://") for item in results))

    def test_bing_parser_respects_limit_and_filters_invalid_urls(self):
        results=BingSearchParser().parse(Path("tests/fixtures/bing_search.html").read_text(encoding="utf-8"),2)
        self.assertEqual(len(results),2)
        self.assertNotIn("无效协议",[item.title for item in results])

    def test_duckduckgo_parser_extracts_results_and_keeps_missing_snippet(self):
        results=DuckDuckGoSearchParser().parse(Path("tests/fixtures/duckduckgo_search.html").read_text(encoding="utf-8"),5)
        self.assertEqual(len(results),3)
        self.assertEqual(results[0].title,"DuckDuckGo 人工智能")
        self.assertEqual(results[0].url,"https://example.com/ddg")
        self.assertIn("搜索结果摘要",results[0].snippet)
        self.assertEqual(results[2].snippet,"")

    def test_duckduckgo_parser_respects_limit(self):
        results=DuckDuckGoSearchParser().parse(Path("tests/fixtures/duckduckgo_search.html").read_text(encoding="utf-8"),1)
        self.assertEqual([item.url for item in results],["https://example.com/ddg"])


class PlaywrightSearchUrlTests(unittest.TestCase):
    def test_search_url_encodes_query_and_filters(self):
        bing=PlaywrightSearchClient.search_url("bing","人工智能 新闻","day")
        duck=PlaywrightSearchClient.search_url("duckduckgo","人工智能 新闻","week")
        self.assertIn("q=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+%E6%96%B0%E9%97%BB",bing)
        self.assertIn("filters=ex1%3A%22ez5_19700_19701%22",bing)
        self.assertIn("q=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+%E6%96%B0%E9%97%BB",duck)
        self.assertIn("df=w",duck)

    def test_unknown_engine_has_explicit_error(self):
        with self.assertRaisesRegex(SearchBackendError,"不支持的浏览器搜索引擎"):
            PlaywrightSearchClient.search_url("google","人工智能","any")


class FakePage:
    def __init__(self,html:str,error:BaseException|None=None)->None:
        self.html=html; self.error=error; self.goto_calls:list[dict[str,object]]=[]; self.closed=False
    async def goto(self,url:str,**options:object)->None:
        self.goto_calls.append({"url":url,**options})
        if self.error is not None:raise self.error
    async def content(self)->str:return self.html
    async def close(self)->None:self.closed=True


class FakeContext:
    def __init__(self,page:FakePage)->None:self.page=page; self.closed=False
    async def new_page(self)->FakePage:return self.page
    async def close(self)->None:self.closed=True


class FakeBrowser:
    def __init__(self,context:FakeContext)->None:self.context=context; self.closed=False; self.launch_options:dict[str,object]={}
    async def new_context(self,**options:object)->FakeContext:self.context_options=options; return self.context
    async def close(self)->None:self.closed=True


class FakeChromium:
    def __init__(self,browser:FakeBrowser)->None:self.browser=browser
    async def launch(self,**options:object)->FakeBrowser:self.browser.launch_options=options; return self.browser


class FakePlaywright:
    def __init__(self,chromium:FakeChromium)->None:self.chromium=chromium; self.stopped=False
    async def stop(self)->None:self.stopped=True


class FakePlaywrightFactory:
    def __init__(self,page:FakePage)->None:
        context=FakeContext(page); browser=FakeBrowser(context)
        self.instance=FakePlaywright(FakeChromium(browser)); self.browser=browser; self.context=context; self.page=page
    async def __call__(self)->FakePlaywright:return self.instance


class MissingPlaywrightFactory:
    async def __call__(self)->None:
        raise ImportError("No module named 'playwright'")


class PlaywrightClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_parses_page_and_reuses_resources_until_close(self):
        page=FakePage(Path("tests/fixtures/bing_search.html").read_text(encoding="utf-8"))
        factory=FakePlaywrightFactory(page); client=PlaywrightSearchClient(None,headless=False,playwright_factory=factory)
        response=await client.search("人工智能",engine="bing",freshness="day",timeout_seconds=12,max_results=2)
        self.assertEqual(response.provider_type,"playwright"); self.assertEqual(response.model,"bing")
        self.assertTrue(response.cited); self.assertEqual(len(response.results),2)
        self.assertEqual(page.goto_calls[0]["url"],PlaywrightSearchClient.search_url("bing","人工智能","day"))
        self.assertEqual(page.goto_calls[0]["timeout"],12000)
        self.assertEqual(factory.browser.launch_options["headless"],False)
        self.assertTrue(page.closed)
        await client.close()
        self.assertTrue(factory.context.closed); self.assertTrue(factory.browser.closed); self.assertTrue(factory.instance.stopped)

    async def test_missing_dependency_has_explicit_error(self):
        client=PlaywrightSearchClient(None,playwright_factory=MissingPlaywrightFactory())
        with self.assertRaises(SearchBackendError) as caught:
            await client.search("人工智能",engine="bing",freshness="any",timeout_seconds=12,max_results=2)
        self.assertEqual(caught.exception.error_class,"playwright_unavailable")

    async def test_navigation_timeout_is_structured_network_error(self):
        page=FakePage("",TimeoutError("Timeout 12000ms exceeded"))
        client=PlaywrightSearchClient(None,playwright_factory=FakePlaywrightFactory(page))
        with self.assertRaises(SearchBackendError) as caught:
            await client.search("人工智能",engine="bing",freshness="any",timeout_seconds=12,max_results=2)
        self.assertEqual(caught.exception.error_class,"network")

    async def test_blocked_page_is_not_treated_as_empty_result(self):
        page=FakePage('<html><body><div class="b_captcha">验证码</div></body></html>')
        client=PlaywrightSearchClient(None,playwright_factory=FakePlaywrightFactory(page))
        with self.assertRaises(SearchBackendError) as caught:
            await client.search("人工智能",engine="bing",freshness="any",timeout_seconds=12,max_results=2)
        self.assertEqual(caught.exception.error_class,"blocked")


if __name__=="__main__":unittest.main()
