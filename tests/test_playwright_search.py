from __future__ import annotations

import unittest
from pathlib import Path

from Mai_life.information.playwright_search import BingSearchParser,DuckDuckGoSearchParser


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


if __name__=="__main__":unittest.main()
