from pathlib import Path

import asyncio
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("Mai_life")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("Mai_life", package)

from Mai_life.core.storage import LifeStore


class ReplyTurnGuardTests(unittest.TestCase):
    def test_reserved_turn_is_visible_until_expiry(self) -> None:
        async def check() -> None:
            with tempfile.TemporaryDirectory() as directory:
                store = LifeStore(directory)
                await store.initialize()
                try:
                    self.assertTrue(await store.reserve_reply_turn("session", "message", 10.0, 20.0))
                    self.assertTrue(await store.has_reply_turn("session", "message", 19.0))
                    self.assertFalse(await store.has_reply_turn("session", "message", 20.0))
                finally:
                    await store.close()

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
