import unittest
import asyncio
from pathlib import Path
import sys
import types
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("Mai_life")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("Mai_life", package)
config = types.ModuleType("Mai_life.config")
config.PLUGIN_VERSION = "test"
sys.modules.setdefault("Mai_life.config", config)

from Mai_life.information.search_parsing import parse_standard
from Mai_life.information.http_client import HttpResponse
from Mai_life.information.search_service import SearchService


class SearXNGParsingTests(unittest.TestCase):
    def test_normalizes_instance_base_url_to_search_endpoint(self) -> None:
        self.assertEqual(
            SearchService._searxng_endpoint("https://search.example"),
            "https://search.example/search",
        )
        self.assertEqual(
            SearchService._searxng_endpoint("https://search.example/searx/search?lang=zh-CN"),
            "https://search.example/searx/search?lang=zh-CN",
        )

    def test_parses_searxng_json_results(self) -> None:
        response = parse_standard(
            "searxng",
            {
                "results": [
                    {
                        "title": "Example result",
                        "url": "https://example.com/result",
                        "content": "A result returned by SearXNG.",
                    }
                ]
            },
            5,
        )

        self.assertTrue(response.cited)
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "Example result")
        self.assertEqual(response.results[0].url, "https://example.com/result")

    def test_requests_json_endpoint_without_api_key(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.request = None

            async def get(self, url, **kwargs):
                self.request = (url, kwargs)
                return HttpResponse(
                    200,
                    url,
                    {"content-type": "application/json"},
                    b'{"results":[{"title":"Result","url":"https://example.com","content":"Text"}]}',
                )

        http = FakeHttp()
        config = SimpleNamespace(search_api=SimpleNamespace(timeout_seconds=12.0, max_results=5))
        provider = SimpleNamespace(provider_type="searxng", endpoint="https://search.example", model="")
        response = asyncio.run(
            SearchService(config, http, None, None)._request_provider(provider, "", "whale sleep", "week")
        )

        self.assertEqual(len(response.results), 1)
        url, kwargs = http.request
        self.assertEqual(url, "https://search.example/search?q=whale+sleep&format=json&time_range=week")
        self.assertEqual(kwargs["headers"], {"Accept": "application/json"})
        self.assertTrue(kwargs["public_only"])

    def test_accepts_href_when_url_is_omitted(self) -> None:
        response = parse_standard(
            "searxng",
            {"results": [{"title": "Fallback", "href": "https://example.com/fallback", "content": "Text"}]},
            5,
        )

        self.assertEqual(response.results[0].url, "https://example.com/fallback")


if __name__ == "__main__":
    unittest.main()
