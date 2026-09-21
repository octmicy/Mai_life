from __future__ import annotations

import random
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from Mai_life.config import MaiLifeSettings
from Mai_life.core.storage import LifeStore
from Mai_life.creation.creation_service import CreationService, _fallback_title
from Mai_life.life.life_state import LifeStateEngine


class DummyLogger:
    def __getattr__(self, name): return lambda *args, **kwargs: None


class DummyLLM:
    def task_available(self, kind): return False
    def task_for(self, kind): return kind
    async def generate(self, *args, **kwargs): return ""
    async def generate_json(self, *args, **kwargs): return {}


class DreamLLM(DummyLLM):
    """dream 任务可用：generate_json 返回固定 dict，验证 LLM 路径优先于变体池。"""
    def task_available(self, kind): return kind == "dream"
    async def generate_json(self, *args, **kwargs):
        return {"summary": "模型生成的一夜安眠。", "fragments": ["模型碎片一", "模型碎片二"], "mood": "warm"}


class OutlineLLM(DummyLLM):
    """creation_outline 任务可用：返回固定标题，验证 LLM 路径优先于形容词池。"""
    def task_available(self, kind): return kind == "creation_outline"
    async def generate_json(self, *args, **kwargs):
        return {"title": "模型拟定的标题", "premise": "模型前提",
                "sections": ["起点", "变化", "余韵"], "privacy": "public"}


class DummyContext:
    pass


class DreamFallbackTests(unittest.IsolatedAsyncioTestCase):
    """梦境兜底变体：同调性多套池随机取一，落库路径与碎片上限不变。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.engine = LifeStateEngine(self.store, self.config, DummyLLM(), DummyLogger())

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _dream(self):
        state = await self.store.get_state()
        await self.engine.generate_dream(state, time.time() - 8 * 3600, 8.0,
                                          datetime.now(timezone(timedelta(hours=8))))

    async def test_fallback_dream_varies_across_samples(self):
        """未配置模型时连续采样 10 次：摘要至少出现两种，且都保持 calm 余韵（mood_delta 为 0）。"""
        for _ in range(10): await self._dream()
        rows = self.store.conn.execute("SELECT content, mood_delta FROM dreams ORDER BY id").fetchall()
        self.assertEqual(len(rows), 10)
        summaries = {str(row[0]) for row in rows}
        self.assertGreater(len(summaries), 1)
        self.assertTrue(all(float(row[1]) == 0.0 for row in rows))

    async def test_fallback_dream_still_persists_with_fragment_cap(self):
        """变体仍走完整落库路径：latest_dream 有记录，碎片数受 dream_fragment_count 约束。"""
        self.config.memory.dream_fragment_count = 2
        await self._dream()
        dream = await self.store.latest_dream()
        self.assertTrue(dream)
        self.assertTrue(str(dream["content"]).strip())
        self.assertEqual(len(dream["fragments"]), 2)
        # 分享契机与心情余韵照常写入，说明只有文案在变、流程没变。
        self.assertTrue(await self.store.active_opportunities(time.time()))

    async def test_llm_dream_result_takes_priority_over_pool(self):
        """task_available=True 时 LLM 返回值优先，变体池不泄漏到模型路径。"""
        engine = LifeStateEngine(self.store, self.config, DreamLLM(), DummyLogger())
        state = await self.store.get_state()
        await engine.generate_dream(state, time.time() - 8 * 3600, 8.0)
        dream = await self.store.latest_dream()
        self.assertEqual(dream["content"], "模型生成的一夜安眠。")
        self.assertEqual(dream["fragments"], ["模型碎片一", "模型碎片二"])


class CreationFallbackTests(unittest.IsolatedAsyncioTestCase):
    """创作兜底变体：标题形容词池与正文结尾句式随机，LLM 路径不受影响。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.config.creation.enabled = True
        self.config.creation.plaintext_storage_acknowledged = True
        self.now = datetime(2026, 7, 13, 16, 0, tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _add_inspiration(self, index):
        return await self.store.add_creation_inspiration({
            "id": f"inspiration-{index}", "source_kind": "life", "source_ref": f"ref-{index}",
            "prompt_digest": "最近生活里留下的一点安静灵感", "privacy_ceiling": "public",
            "score": 0.8, "created_at": self.now.timestamp(), "expires_at": self.now.timestamp() + 86400,
        })

    async def test_fallback_titles_vary_across_ticks(self):
        """未配置模型时连续创作 5 次：固定随机种子保证确定性，标题至少出现两种（形容词池 >1）。"""
        self.config.creation.daily_max = 5
        service = CreationService(DummyContext(), self.store, self.config, DummyLLM(), DummyLogger())
        for index in range(5):
            await self._add_inspiration(index)
            # daily_max 上限为 5；用种子代替纯采样，避免小样本下偶发全同。
            random.seed(index)
            result = await service.tick(self.now, "人格", await self.store.get_state(),
                                        {"current": {"kind": "leisure"}}, force=True)
            self.assertEqual(result["status"], "archived")
        titles = [str(row[0]) for row in self.store.conn.execute(
            "SELECT title FROM bookshelf_documents WHERE doc_type='work' ORDER BY rowid")]
        self.assertEqual(len(titles), 5)
        self.assertGreater(len(set(titles)), 1)

    def test_fallback_title_adjective_pool_has_multiple_entries(self):
        """固定体裁直接采样标题生成：句式不变，形容词去重后 >1。"""
        titles = {_fallback_title("随笔") for _ in range(40)}
        self.assertGreater(len(titles), 1)
        self.assertTrue(all(item.startswith("一则") and item.endswith("随笔") for item in titles))

    async def test_fallback_body_tail_varies(self):
        """正文兜底保留模板结构（标题行/体裁句不变），结尾句式去重后 >1。"""
        service = CreationService(DummyContext(), self.store, self.config, DummyLLM(), DummyLogger())
        inspiration = {"prompt_digest": "digest", "source_kind": "life", "source_ref": "ref",
                       "privacy_ceiling": "public"}
        outline = {"title": "一则还没想好名字的随笔", "premise": "日常感受",
                   "sections": ["起点", "变化", "余韵"]}
        bodies = {await service._body("人格", inspiration, "essay", outline) for _ in range(40)}
        self.assertGreater(len(bodies), 1)
        self.assertTrue(all("《一则还没想好名字的随笔》" in item for item in bodies))
        self.assertTrue(all("这是一则从日常感受展开的随笔。" in item for item in bodies))

    async def test_llm_outline_title_takes_priority_over_pool(self):
        """task_available=True 时两次创作都使用模型拟定的标题，形容词池不生效。"""
        self.config.creation.daily_max = 5
        service = CreationService(DummyContext(), self.store, self.config, OutlineLLM(), DummyLogger())
        titles = []
        for index in range(2):
            await self._add_inspiration(index)
            result = await service.tick(self.now, "人格", await self.store.get_state(),
                                        {"current": {"kind": "leisure"}}, force=True)
            self.assertEqual(result["status"], "archived")
            titles.append(str(result["title"]))
        self.assertEqual(titles, ["模型拟定的标题", "模型拟定的标题"])


if __name__ == "__main__":
    unittest.main()
