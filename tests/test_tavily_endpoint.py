import asyncio
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("Mai_life")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("Mai_life", package)
config = types.ModuleType("Mai_life.config")
config.PLUGIN_VERSION = "test"
sys.modules.setdefault("Mai_life.config", config)

from Mai_life.information.http_client import HttpResponse
from Mai_life.information.search_service import SearchService


class TavilyEndpointTests(unittest.TestCase):
    def test_uses_configured_compatible_endpoint(self) -> None:
        class FakeHttp:
            def __init__(self) -> None:
                self.request = None

            async def post_json(self, url, payload, **kwargs):
                self.request = (url, payload, kwargs)
                return HttpResponse(
                    200,
                    url,
                    {"content-type": "application/json"},
                    b'{"results":[{"title":"Result","url":"https://example.com","content":"Text"}]}',
                )

        http = FakeHttp()
        config = SimpleNamespace(search_api=SimpleNamespace(timeout_seconds=12.0, max_results=5))
        provider = SimpleNamespace(
            provider_type="tavily",
            endpoint="https://tavily.example/api/search",
            model="",
        )

        response = asyncio.run(
            SearchService(config, http, None, None)._request_provider(provider, "test-key", "whale sleep", "")
        )

        self.assertEqual(len(response.results), 1)
        url, payload, kwargs = http.request
        self.assertEqual(url, "https://tavily.example/api/search")
        self.assertEqual(payload["query"], "whale sleep")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_empty_endpoint_uses_official_url(self) -> None:
        self.assertEqual(SearchService._tavily_endpoint(""), "https://api.tavily.com/search")


if __name__ == "__main__":
    unittest.main()
