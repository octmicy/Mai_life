from __future__ import annotations

import asyncio
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Mai_life.config import MaiLifeSettings, UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.life.memory_service import MemoryService
from Mai_life.plugin import MaiLifePlugin


class DummyLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class RecordingLogger:
    def __init__(self):
        self.records: list[tuple[str, tuple]] = []

    def _log(self, level, *args):
        self.records.append((level, args))

    def info(self, *args): self._log("info", *args)
    def warning(self, *args): self._log("warning", *args)
    def error(self, *args): self._log("error", *args)
    def debug(self, *args): self._log("debug", *args)

    def texts(self, level: str) -> list[str]:
        return [str(args[0]) if args else "" for lv, args in self.records if lv == level]


class DummyLLM:
    def task_available(self, kind): return False
    def task_for(self, kind): return "planner"
    async def generate(self, *args, **kwargs): return ""
    async def generate_json(self, *args, **kwargs): return {}


class SettleWarningTests(unittest.IsolatedAsyncioTestCase):
    """F2：重复发送确认应记 info（已结算过），只有真未命中才记 warning。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_duplicate_send_confirmation_logs_info_not_warning(self):
        from Mai_life.core.environment import EnvironmentService
        config = MaiLifeSettings()
        await self.store.sync_users([UserProfile(user_id="1", role="owner")])
        await self.store.set_user_stream("1", "s1")
        now = time.time()
        await self.store.add_opportunity({"id": "o1", "framework_id": "f", "topic": "t",
                                          "motive": "m", "weight": 0.8, "expires_at": now + 3600})
        await self.store.consume_opportunity("o1", "1", now)
        await self.store.add_proactive_pending("e1", "1", "o1", "s1", now, now + 180)
        await self.store.set_proactive_task_id("e1", "proactive:maibot-community.mai-life:1")
        logger = RecordingLogger()

        class Ctx:
            pass
        ctx = Ctx(); ctx.logger = logger
        plugin = MaiLifePlugin()
        plugin._set_context(ctx)
        plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store = self.store
        plugin._env = EnvironmentService(self.store, config, logger)
        plugin._rest = None
        # 第一次结算。
        await plugin.on_send_after(message={"session_id": "s1"}, sent=True,
                                   reply_message_id="proactive:maibot-community.mai-life:1")
        self.assertEqual((await self.store.get_user("1"))["proactive_count"], 1)
        # 重复确认：额度不翻倍，且日志是 info 而非 warning。
        await plugin.on_send_after(message={"session_id": "s1"}, sent=True,
                                   reply_message_id="proactive:maibot-community.mai-life:1")
        self.assertEqual((await self.store.get_user("1"))["proactive_count"], 1)
        self.assertTrue(any("重复确认" in text for text in logger.texts("info")),
                        f"未出现重复确认 info: {logger.records}")
        self.assertFalse(any("结算未命中" in text for text in logger.texts("warning")),
                         f"误报 warning: {logger.records}")


class DiaryFallbackTests(unittest.IsolatedAsyncioTestCase):
    """F3：无模型时日记标题也应多变（占位内容不复读）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_fallback_diary_titles_vary_across_days(self):
        from Mai_life.life.schedule_service import ScheduleService
        service = MemoryService(self.store, self.config, DummyLLM(), DummyLogger())
        sched = ScheduleService(self.store, self.config, DummyLLM(), ".", DummyLogger())
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 9, 20, 10, 0, tzinfo=tz)
        titles = set()
        for offset in range(10):
            day = (now - timedelta(days=offset + 1)).date()
            await self.store.replace_framework(day.isoformat(),
                                               sched._fallback(day.isoformat(), False))
            await service.ensure_daily(now - timedelta(days=offset))
            entry = await self.store.get_diary(day.isoformat())
            if entry:
                titles.add(str(entry["title"]))
        self.assertGreater(len(titles), 1, f"日记标题十天完全相同: {titles}")


class StatusNoteTests(unittest.IsolatedAsyncioTestCase):
    """F3：叙事任务不可用时 /麦麦状态 明确标注占位内容。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_status_notes_placeholder_when_narrative_tasks_missing(self):
        from Mai_life.core.environment import EnvironmentService
        from Mai_life.core.llm_service import LLMService
        from Mai_life.creation.creation_service import CreationService
        from Mai_life.creation.bookshelf_service import BookshelfService
        from Mai_life.information.information_service import InformationService
        from Mai_life.life.continuity import ContinuityService
        from Mai_life.life.memory_service import MemoryService
        from Mai_life.life.proactive import ProactiveEngine
        from Mai_life.life.rest_gate import RestGate
        from Mai_life.life.schedule_service import ScheduleService
        from Mai_life.life.life_state import LifeStateEngine
        from Mai_life.management.admin_service import AdminService
        from Mai_life.messaging.message_pipeline import MessageDebouncer
        from Mai_life.messaging.recall_service import RecallService
        from Mai_life.social.group_observer import GroupObserver
        from Mai_life.social.relay_service import RelayService
        config = MaiLifeSettings()
        await self.store.sync_users([UserProfile(user_id="1", role="owner")])
        await self.store.set_user_stream("1", "s1")
        logger = DummyLogger()

        class Ctx:
            pass
        ctx = Ctx(); ctx.logger = logger
        llm = LLMService(ctx, config, self.store)
        llm.available_tasks = set()  # 没有任何可用任务 → 叙事全缺
        llm.health_error = ""
        plugin = MaiLifePlugin()
        plugin._set_context(ctx)
        plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store = self.store
        plugin._env = EnvironmentService(self.store, config, logger)
        plugin._llm = llm
        plugin._state = LifeStateEngine(self.store, config, llm, logger)
        plugin._schedule = ScheduleService(self.store, config, llm, ".", logger)
        plugin._rest = RestGate(self.store, config, llm, plugin._state, logger)
        plugin._proactive = ProactiveEngine(ctx, self.store, config, plugin._env, logger)
        plugin._debouncer = MessageDebouncer(config, logger)
        plugin._recall = RecallService(ctx, self.store, config, logger)
        plugin._continuity = ContinuityService(self.store, config, llm, logger)
        plugin._memory = MemoryService(self.store, config, llm, logger)
        plugin._information = InformationService(ctx, self.store, config, llm, logger)
        plugin._group_observer = GroupObserver(self.store, config, llm, logger)
        plugin._relay = RelayService(ctx, self.store, config, logger)
        plugin._bookshelf = BookshelfService(self.store, config)
        plugin._creation = CreationService(ctx, self.store, config, llm, logger)
        plugin._admin = AdminService(self.store, config)
        text = await plugin._status_report()
        self.assertIn("占位内容", text)


class DocContractTests(unittest.TestCase):
    """F5：命令数与文档描述不许再脱节（沿用版本号契约测试的思路）。"""

    def test_command_count_matches_contributing_doc(self):
        root = Path(__file__).parents[1]
        source = (root / "plugin.py").read_text(encoding="utf-8-sig", errors="replace")
        count = len(re.findall(r"^\s*@Command\(", source, flags=re.MULTILINE))
        self.assertEqual(count, 22)
        doc = (root / "CONTRIBUTING.md").read_text(encoding="utf-8-sig", errors="replace")
        self.assertIn("22 Command", doc,
                      "CONTRIBUTING.md 的命令数与 plugin.py 实际不符，请同步")
        readme = (root / "README.md").read_text(encoding="utf-8-sig", errors="replace")
        for name in ("/麦麦帮助", "/麦麦立即创作", "/麦麦休息测试", "/麦麦重生日程"):
            self.assertIn(name, readme, f"README 指令表缺少 {name}")


if __name__ == "__main__":
    unittest.main()
