"""v1.14.4 状态动力学修复测试：均衡恢复、深睡相位、离线补日记与日期/灵感池守卫。"""
from __future__ import annotations

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Mai_life.config import MaiLifeSettings, UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.life.life_state import LifeStateEngine
from Mai_life.life.memory_service import MemoryService
from Mai_life.life.schedule_service import ScheduleService

TZ = timezone(timedelta(hours=8))


class DummyLogger:
    def __getattr__(self, name): return lambda *args, **kwargs: None


class DummyLLM:
    def task_available(self, kind): return False
    def task_for(self, kind): return "planner"
    async def generate(self, *args, **kwargs): return ""
    async def generate_json(self, prompt, system, fallback, max_tokens=0, **kwargs): return fallback


class RecordingLogger:
    """记录各级别日志，供限流断言使用。"""

    def __init__(self): self.calls: list[tuple[str, str]] = []
    def info(self, message, *args, **kwargs): self.calls.append(("info", str(message)))
    def warning(self, message, *args, **kwargs): self.calls.append(("warning", str(message)))
    def error(self, message, *args, **kwargs): self.calls.append(("error", str(message)))
    def debug(self, message, *args, **kwargs): self.calls.append(("debug", str(message)))
    def messages(self, level): return [text for lv, text in self.calls if lv == level]
    def __getattr__(self, name): return lambda *args, **kwargs: None


class BalancedRestTests(unittest.IsolatedAsyncioTestCase):
    """P2-1：睡眠改为均衡恢复后，缺觉恢复多、充沛恢复少，不再固定 2.5/h 净亏。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.engine = LifeStateEngine(self.store, self.config, DummyLLM(), DummyLogger())
        self.base = datetime(2026, 9, 21, 23, 30, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _sleep(self, energy: float, hours: float, kind: str = "sleep"):
        state = await self.store.get_state()
        state["energy"] = energy
        state["last_updated_at"] = self.base.timestamp()
        await self.store.save_state(state)
        return await self.engine.advance(self.base + timedelta(hours=hours),
                                         {"kind": kind, "summary": "睡觉", "location": "卧室"}, None)

    async def test_heavy_day_recovers_toward_target(self):
        """精力 40 睡 8h：向 REST_TARGET=90 收敛，约 40+(90-40)*(1-e^-2.8)≈86.9。"""
        result = await self._sleep(40.0, 8.0)
        energy = float(result["state"]["energy"])
        self.assertAlmostEqual(energy, 40 + (90 - 40) * (1 - math.exp(-0.35 * 8)), delta=0.01)
        self.assertGreaterEqual(energy, 84)
        self.assertLessEqual(energy, 88)

    async def test_light_day_does_not_pin_at_ceiling(self):
        """精力 85 睡 8h：只恢复几个点且不超过 90，不会贴顶 100。"""
        result = await self._sleep(85.0, 8.0)
        energy = float(result["state"]["energy"])
        self.assertGreater(energy, 85)
        self.assertLessEqual(energy, 90)

    async def test_short_sleep_recovers_only_a_few_points(self):
        """睡前 70 只睡半小时：均衡恢复约 3 个点，远低于旧固定口径。"""
        result = await self._sleep(70.0, 0.5)
        self.assertAlmostEqual(float(result["state"]["energy"]),
                               70 + (90 - 70) * (1 - math.exp(-0.35 * 0.5)), delta=0.01)

    async def test_nap_keeps_fixed_small_recovery(self):
        """午休不走均衡恢复，仍按 1.4/h 固定小幅恢复。"""
        result = await self._sleep(70.0, 40 / 60, kind="nap")
        self.assertAlmostEqual(float(result["state"]["energy"]), 70 + 1.4 * (40 / 60), delta=0.01)


class DeepSleepPhaseTests(unittest.IsolatedAsyncioTestCase):
    """P2-6：相位按累计睡眠时长推进，10 分钟 tick 也能到达深睡且收尾不降级。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.engine = LifeStateEngine(self.store, self.config, DummyLLM(), DummyLogger())
        self.start = datetime(2026, 9, 21, 0, 35, tzinfo=TZ)
        self.end = datetime(2026, 9, 21, 6, 5, tzinfo=TZ)
        self.segment = {"kind": "sleep", "summary": "夜间睡眠", "location": "卧室"}

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _seed(self):
        state = await self.store.get_state()
        state["energy"] = 60; state["last_updated_at"] = self.start.timestamp()
        await self.store.save_state(state)
        runtime = await self.store.get_sleep_runtime()
        runtime.update({"phase": "awake", "started_at": self.start.timestamp(), "last_event": ""})
        await self.store.save_sleep_runtime(runtime)

    async def test_ticks_reach_deep_sleep_and_closing_advance_keeps_it(self):
        await self._seed()
        rank = {"falling_asleep": 0, "light_sleep": 1, "deep_sleep": 2}
        phases: list[str] = []
        moment = self.start
        while moment < self.end:
            moment += timedelta(minutes=30)
            result = await self.engine.advance(moment, self.segment, None)
            phases.append(str(result["state"]["sleep_phase"]))
        self.assertIn("deep_sleep", phases)
        # 相位只向前推进，不会被打回浅睡。
        self.assertEqual(phases, sorted(phases, key=lambda phase: rank[phase]))
        # 收尾 advance（elapsed=0）不覆写相位。
        closing = await self.engine.advance(self.end, self.segment, None)
        self.assertEqual(closing["state"]["sleep_phase"], "deep_sleep")
        self.assertEqual((await self.store.get_sleep_runtime())["phase"], "deep_sleep")

    async def test_offline_timeline_keeps_deep_sleep_and_writes_hourly_snapshots(self):
        """advance_timeline 路径：收尾 advance 不降级，且离线中间小时留下快照（P3-4）。"""
        await self._seed()
        spans = []
        cursor = self.start
        while cursor < self.end:
            nxt = cursor + timedelta(minutes=30)
            spans.append({"start": cursor, "end": nxt, "segment": self.segment})
            cursor = nxt
        result = await self.engine.advance_timeline(self.end, spans, self.segment, None)
        self.assertEqual(result["state"]["sleep_phase"], "deep_sleep")
        rows = self.store.conn.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]
        self.assertGreaterEqual(rows, 5)


class OfflineDiaryBackfillTests(unittest.IsolatedAsyncioTestCase):
    """P2-7：离线多日补日记窗口扩展，同时保留新装首跑的 F1 守卫。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.service = MemoryService(self.store, self.config, DummyLLM(), DummyLogger())
        self.schedule = ScheduleService(self.store, self.config, DummyLLM(),
                                        str(Path(__file__).parents[1]), DummyLogger())
        self.now = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    def _framework(self, day: str):
        return self.schedule._fallback(day, datetime.fromisoformat(day).weekday() >= 5)

    async def test_two_offline_days_get_diaries_and_frameworks(self):
        """有框架日 + 离线 2 天：补 2 篇日记，离线日补落库框架并照旧产生 owner_only 契机。"""
        await self.store.sync_users([UserProfile(user_id="10001", role="owner", proactive_enabled=True)])
        await self.store.replace_framework("2026-09-18", self._framework("2026-09-18"))
        await self.service.ensure_daily(self.now)
        for day in ("2026-09-19", "2026-09-20"):
            self.assertTrue(await self.store.get_diary(day), f"{day} 缺日记")
            self.assertTrue(await self.store.get_framework(day), f"{day} 缺框架")
        opportunities = await self.store.active_opportunities(self.now.timestamp())
        self.assertEqual(len([item for item in opportunities if item["privacy"] == "owner_only"]), 2)

    async def test_fresh_store_still_skips_backfill(self):
        """全新库（向前 14 天无任何框架）：一天都不补，F1 守卫保留。"""
        await self.store.sync_users([UserProfile(user_id="10001", role="owner", proactive_enabled=True)])
        await self.service.ensure_daily(self.now)
        self.assertEqual(await self.store.get_diary("2026-09-20"), {})
        self.assertEqual(await self.store.list_diaries(30), [])

    async def test_backfill_window_caps_at_seven_days(self):
        """离线 8 天：窗口上限 7 天，只补最近的 7 天，更早的离线日仍无框架。"""
        await self.store.replace_framework("2026-09-12", self._framework("2026-09-12"))
        await self.service.ensure_daily(self.now)
        diaries = {item["day"] for item in await self.store.list_diaries(30)}
        self.assertEqual(len(diaries), 7)
        self.assertIn("2026-09-14", diaries)
        self.assertNotIn("2026-09-13", diaries)
        self.assertEqual(await self.store.get_framework("2026-09-13"), [])
        self.assertEqual(await self.store.get_diary("2026-09-13"), {})


class PastDateGuardTests(unittest.IsolatedAsyncioTestCase):
    """P3-2：模型高置信但日期已过时降级为候选，不直接落库重要日期。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.config.memory.date_model_analysis_enabled = True
        self.now = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    def _llm(self, suggested: str):
        class DateLLM(DummyLLM):
            def task_available(self, kind): return kind == "date_analysis"
            async def generate_json(self, prompt, system, fallback, max_tokens=0, **kwargs):
                return {"has_date": True, "event_name": "考试", "date_text": "之前约好的那个",
                        "suggested_date": suggested, "confidence": 0.95, "recurrence": "none"}
        return DateLLM()

    async def test_past_high_confidence_date_becomes_candidate(self):
        service = MemoryService(self.store, self.config, self._llm("2026-09-10"), DummyLogger())
        await service.observe_message("10001", "帮我记住之前约好的那场考试，别忘了", self.now)
        self.assertEqual(await self.store.list_important_dates("10001"), [])
        candidates = await self.store.list_date_candidates("10001")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["suggested_date"], "2026-09-10")

    async def test_future_high_confidence_date_still_saves(self):
        service = MemoryService(self.store, self.config, self._llm("2026-09-28"), DummyLogger())
        await service.observe_message("10001", "帮我记住之前约好的那场考试，别忘了", self.now)
        dates = await self.store.list_important_dates("10001")
        self.assertEqual(len(dates), 1)
        self.assertEqual(dates[0]["event_date"], "2026-09-28")
        self.assertEqual(await self.store.list_date_candidates("10001"), [])


class DreamExpiryTests(unittest.IsolatedAsyncioTestCase):
    """P3-8：梦境契机的过期时间基于入睡时刻，离线补算后不会出生即过期。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.engine = LifeStateEngine(self.store, self.config, DummyLLM(), DummyLogger())
        self.now = datetime(2026, 9, 21, 6, 30, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_dream_opportunity_expires_twelve_hours_after_sleep_started(self):
        sleep_started = self.now.timestamp() - 8 * 3600
        await self.engine.generate_dream(await self.store.get_state(), sleep_started, 8.0, self.now)
        opportunities = await self.store.active_opportunities(self.now.timestamp())
        dream = next(item for item in opportunities if str(item["id"]).startswith("dream-"))
        self.assertEqual(float(dream["expires_at"]), sleep_started + 12 * 3600)
        self.assertGreater(float(dream["expires_at"]), self.now.timestamp())


class ScheduleIdeaPoolTests(unittest.IsolatedAsyncioTestCase):
    """P3-22：探索笔记按关联分数过滤后再进入日程灵感池。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.now = datetime(2026, 9, 21, 3, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_low_relevance_notes_stay_out_of_the_pool(self):
        class CapturingLLM:
            def __init__(self): self.prompt = ""
            def task_available(self, kind): return kind == "schedule"
            def task_for(self, kind): return "planner"
            async def generate_json(self, prompt, system, fallback, **kwargs):
                self.prompt = prompt; return fallback

        for note_id, topic, score in (("n-low", "冷门笔记", 0.2), ("n-high", "深海火山", 0.5)):
            await self.store.save_exploration_note({
                "id": note_id, "topic": topic, "query": "q", "summary": "s", "source_urls": [],
                "created_at": self.now.timestamp(), "relevance_score": score,
                "relevance_reason": "", "opportunity_id": "", "expires_at": self.now.timestamp() + 86400})
        llm = CapturingLLM()
        service = ScheduleService(self.store, self.config, llm, str(Path(__file__).parents[1]), DummyLogger())
        await service.ensure_day(self.now, "人格", "晴", force=True)
        self.assertIn("活动灵感", llm.prompt)
        self.assertIn("深海火山", llm.prompt)
        self.assertNotIn("冷门笔记", llm.prompt)


class SkipLogThrottleTests(unittest.IsolatedAsyncioTestCase):
    """P3-6：跳过补日记的 info 按原因签名限流，同因 24 小时内只记一条。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.logger = RecordingLogger()
        self.service = MemoryService(self.store, MaiLifeSettings(), DummyLLM(), self.logger)
        self.now = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_same_reason_logs_once_per_day(self):
        await self.service.ensure_daily(self.now)
        await self.service.ensure_daily(self.now)
        skipped = [text for text in self.logger.messages("info") if "跳过补日记" in text]
        self.assertEqual(len(skipped), 1)
        # 24 小时后同原因再次跳过才补记一条。
        await self.service.ensure_daily(self.now + timedelta(hours=25))
        skipped = [text for text in self.logger.messages("info") if "跳过补日记" in text]
        self.assertEqual(len(skipped), 2)


if __name__ == "__main__":
    unittest.main()
