from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("Mai_life")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("Mai_life", package)
config = types.ModuleType("Mai_life.config")
config.PLUGIN_VERSION = "test"
sys.modules.setdefault("Mai_life.config", config)

from Mai_life.information.http_client import _validate_public_url_sync


class PublicUrlValidationTests(unittest.TestCase):
    def test_prefers_ipv4_when_dns_returns_ipv6_and_ipv4(self) -> None:
        records = [
            (None, None, None, None, ("2606:4700:10::6814:18ac", 0, 0, 0)),
            (None, None, None, None, ("104.20.24.172", 0)),
        ]
        with patch("Mai_life.information.http_client.socket.getaddrinfo", return_value=records):
            _, pinned = _validate_public_url_sync("https://search.example/search")

        self.assertEqual(pinned, "104.20.24.172")


if __name__ == "__main__":
    unittest.main()
