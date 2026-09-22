from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from Mai_life.config import MaiLifeSettings, SocialGroupProfile, UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.life.rest_gate import BLOCK_REASON, RestGate
from Mai_life.social.group_observer import GroupObserver

TZ = timezone(timedelta(hours=8))
NIGHT = datetime(2026, 9, 22, 23, 0, tzinfo=TZ)
NOON = datetime(2026, 9, 22, 12, 30, tzinfo=TZ)
DAYTIME = datetime(2026, 9, 22, 15, 0, tzinfo=TZ)


class DummyLogger:
    def __init__(self): self.records: list[tuple[str, tuple]] = []

    def _log(self, level, *args): self.records.append((level, args))

    def info(self, *args): self._log("info", *args)
    def warning(self, *args): self._log("warning", *args)
    def error(self, *args): self._log("error", *args)
    def debug(self, *args): self._log("debug", *args)

    def texts(self, level: str) -> list[str]:
        return [str(args[0]) if args else "" for lv, args in self.records if lv == level]


class DummyLLM:
    def task_available(self, kind): return False
    async def generate(self, *args, **kwargs): return ""
    async def generate_json(self, prompt, system, fallback, max_tokens=0, **kwargs): return fallback


class DummyStateEngine:
    async def mark_woken(self, *args, **kwargs): pass


def group_message(mid: str, text: str, group_id: str = "100", user_id: str = "30001"):
    return {"message_id": mid, "session_id": f"group-stream-{group_id}", "platform": "qq",
            "processed_plain_text": text, "message_info": {
                "user_info": {"user_id": user_id, "user_nickname": "可变昵称"},
                "group_info": {"group_id": group_id, "group_name": "Host 自动群名"},
                "additional_config": {"napcat_message_type": "group"}},
            "raw_message": [{"type": "text", "data": text}], "is_command": False, "is_notify": False}


def make_config(*, group_gate: bool, group_ids: tuple[str, ...] = ("100",),
                 group_mode: str = "all") -> MaiLifeSettings:
    config = MaiLifeSettings()
    config.rest_gate.group_enabled = group_gate
    config.rest_gate.group_mode = group_mode
    config.social.enabled = True
    config.social.groups = [
        SocialGroupProfile(group_id=gid, enabled=True, observe_enabled=True,
                           relay_target_enabled=False, rest_gate_enabled=True)
        for gid in group_ids
    ]
    return config


class GroupGateDecisionTests(unittest.IsolatedAsyncioTestCase):
    """群聊轻量版闸门：总闸/分群开关 → 时间窗 → 强制词/勿扰词/静默。"""

    def setUp(self):
        self.store = None

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    def _gate(self, config: MaiLifeSettings) -> RestGate:
        return RestGate(self.store, config, DummyLLM(), DummyStateEngine(), DummyLogger())

    async def test_night_window_silences_ordinary_group_message(self):
        gate = self._gate(make_config(group_gate=True))
        allowed, reason = gate.decide_group("晚上好呀大家", NIGHT, "100")
        self.assertFalse(allowed)
        self.assertEqual(reason, "群休息时段静默")

    async def test_force_wake_term_passes_in_group_window(self):
        gate = self._gate(make_config(group_gate=True))
        allowed, reason = gate.decide_group("出事了救命啊", NIGHT, "100")
        self.assertTrue(allowed)
        self.assertIn("强制唤醒词", reason)

    async def test_quiet_term_is_blocked_in_group_window(self):
        gate = self._gate(make_config(group_gate=True))
        allowed, reason = gate.decide_group("别回我，你们继续", NIGHT, "100")
        self.assertFalse(allowed)
        self.assertEqual(reason, BLOCK_REASON)

    async def test_outside_window_passes_everything(self):
        gate = self._gate(make_config(group_gate=True))
        allowed, reason = gate.decide_group("中午好", DAYTIME, "100")
        self.assertTrue(allowed)
        self.assertEqual(reason, "outside_group_window")

    async def test_nap_window_also_gates(self):
        gate = self._gate(make_config(group_gate=True))
        allowed, reason = gate.decide_group("午安", NOON, "100")
        self.assertFalse(allowed)

    async def test_master_switch_off_passes(self):
        gate = self._gate(make_config(group_gate=False))
        allowed, reason = gate.decide_group("凌晨闲聊", NIGHT, "100")
        self.assertTrue(allowed)
        self.assertEqual(reason, "group_gate_disabled")

    async def test_group_without_switch_passes_in_selected_mode(self):
        gate = self._gate(make_config(group_gate=True, group_ids=("999",), group_mode="selected"))
        allowed, reason = gate.decide_group("凌晨闲聊", NIGHT, "100")
        self.assertTrue(allowed)
        self.assertEqual(reason, "group_not_enabled")

    async def test_all_mode_gates_unlisted_group(self):
        # group_mode=all（默认）：开箱即用，未在白名单登记的群也受闸门管辖。
        gate = self._gate(make_config(group_gate=True, group_ids=()))
        allowed, reason = gate.decide_group("凌晨闲聊", NIGHT, "100")
        self.assertFalse(allowed)
        self.assertEqual(reason, "群休息时段静默")

    async def test_selected_mode_skips_group_with_switch_off(self):
        config = make_config(group_gate=True, group_ids=("100",), group_mode="selected")
        config.social.groups[0].rest_gate_enabled = False
        gate = self._gate(config)
        allowed, reason = gate.decide_group("凌晨闲聊", NIGHT, "100")
        self.assertTrue(allowed)
        self.assertEqual(reason, "group_not_enabled")

    async def test_custom_group_force_wake_terms(self):
        config = make_config(group_gate=True)
        config.rest_gate.group_force_wake_terms = ["服务器炸了"]
        gate = self._gate(config)
        allowed, reason = gate.decide_group("不好了服务器炸了", NIGHT, "100")
        self.assertTrue(allowed)
        self.assertIn("服务器炸了", reason)


class GroupPipelineGateTests(unittest.IsolatedAsyncioTestCase):
    """群闸门在 _process_group_message 的行为：阻断 abort 且不落任何状态。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = make_config(group_gate=True)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    def _plugin(self, config: MaiLifeSettings, *, group_enabled_debounce: bool = False):
        from Mai_life.core.environment import EnvironmentService
        from Mai_life.messaging.message_pipeline import MessageDebouncer
        from Mai_life.plugin import MaiLifePlugin
        from Mai_life.social.group_observer import GroupObserver
        from Mai_life.messaging.recall_service import RecallService
        from Mai_life.social.relay_service import RelayService
        logger = DummyLogger()

        class Ctx:
            pass
        ctx = Ctx(); ctx.logger = logger
        plugin = MaiLifePlugin()
        plugin._set_context(ctx)
        plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store = self.store
        plugin._env = EnvironmentService(self.store, config, logger)
        plugin._rest = RestGate(self.store, config, DummyLLM(), DummyStateEngine(), logger)
        plugin._debouncer = MessageDebouncer(config, logger)
        plugin._group_observer = GroupObserver(self.store, config, DummyLLM(), logger)
        plugin._relay = RelayService(ctx, self.store, config, logger)
        plugin._recall = RecallService(ctx, self.store, config, logger)
        # 冻结假时钟：闸门按群夜窗判定（否则用真实时间会落在窗外而放行）。
        plugin._env.now = lambda: NIGHT
        return plugin, logger

    async def test_blocked_group_message_aborts_without_backlog_or_candidate(self):
        plugin, logger = self._plugin(self.config)
        result = await plugin._process_group_message(
            {"message": group_message("g1", "深夜水群")}, group_message("g1", "深夜水群"),
            "30001", "group-stream-100", "g1", ["g1"])
        self.assertEqual(result.get("action"), "abort")
        # 不写积压、不建待醒候选、不登记群轮次。
        self.assertEqual(await self.store.peek_rest_backlogs("30001"), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM wake_candidates").fetchone()[0], 0)
        self.assertEqual(plugin._group_turns, {})
        self.assertTrue(any("群休息闸门阻断" in t for t in logger.texts("info")))

    async def test_force_wake_group_message_passes_through_pipeline(self):
        plugin, _logger = self._plugin(self.config)
        result = await plugin._process_group_message(
            {"message": group_message("g2", "群友们出事了我很难受")}, group_message("g2", "群友们出事了我很难受"),
            "30001", "group-stream-100", "g2", ["g2"])
        self.assertEqual(result.get("action"), "continue")
        # 群防抖默认关：不登记 group_turns，但消息被放行到主程序。
        self.assertIn("message", result.get("modified_kwargs", {}))

    async def test_group_command_passes_even_in_window(self):
        plugin, _logger = self._plugin(self.config)
        message = group_message("g3", "/麦麦状态")
        result = await plugin._process_group_message(
            {"message": message}, message, "30001", "group-stream-100", "g3", ["g3"])
        # is_command 在管线前部已直通，闸门不会收到命令消息。
        self.assertEqual(result.get("action"), "continue")

    async def test_blocked_group_message_spawns_no_observer(self):
        plugin, _logger = self._plugin(self.config)
        observed: list[str] = []
        original = plugin._group_observer.observe

        async def spy(message, now):
            observed.append(str(message.get("message_id")))
            return await original(message, now)

        plugin._group_observer.observe = spy
        await plugin._process_group_message(
            {"message": group_message("g4", "日常灌水")}, group_message("g4", "日常灌水"),
            "30001", "group-stream-100", "g4", ["g4"])
        await asyncio_sleep_zero()
        self.assertEqual(observed, [])


async def asyncio_sleep_zero() -> None:
    import asyncio
    await asyncio.sleep(0)


class GroupObserverSleepTests(unittest.IsolatedAsyncioTestCase):
    """群观察在群静音窗内跳过（不调 group_judgment/relay_summary）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = make_config(group_gate=True)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_observe_is_rest_gated_in_group_window(self):
        observer = GroupObserver(self.store, self.config, DummyLLM(), DummyLogger())
        result = await observer.observe(group_message("o1", "夜间群聊"), NIGHT)
        self.assertEqual(result.get("status"), "rest_gated")
        # 未写任何观察摘要。
        self.assertEqual(await self.store.recent_group_observations(NIGHT.timestamp(), 10), [])

    async def test_observe_runs_outside_window(self):
        observer = GroupObserver(self.store, self.config, DummyLLM(), DummyLogger())
        result = await observer.observe(group_message("o2", "白天群聊"), DAYTIME)
        self.assertNotEqual(result.get("status"), "rest_gated")

    async def test_observe_runs_when_group_gate_disabled(self):
        config = make_config(group_gate=False)
        observer = GroupObserver(self.store, config, DummyLLM(), DummyLogger())
        result = await observer.observe(group_message("o3", "夜间群聊"), NIGHT)
        self.assertNotEqual(result.get("status"), "rest_gated")


class RelaySleepCheckTests(unittest.IsolatedAsyncioTestCase):
    """/麦麦转述 在睡眠相位被拒。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = make_config(group_gate=True)
        self.config.social.groups[0].relay_target_enabled = True

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    def _relay(self):
        from Mai_life.social.relay_service import RelayService

        class Ctx:
            pass
        return RelayService(Ctx(), self.store, self.config, DummyLogger())

    async def test_relay_rejected_during_sleep(self):
        relay = self._relay()
        runtime = await self.store.get_sleep_runtime()
        runtime.update({"phase": "deep_sleep"})
        await self.store.save_sleep_runtime(runtime)
        result = await relay.trigger_explicit("100", "向大家问好")
        self.assertFalse(result.get("success"))
        self.assertIn("休息", str(result.get("error")))

    async def test_relay_allowed_while_awake(self):
        relay = self._relay()
        runtime = await self.store.get_sleep_runtime()
        runtime.update({"phase": "awake"})
        await self.store.save_sleep_runtime(runtime)
        #  awake 时进入正常流程：群流解析会失败（无 Host），但不会被睡眠门禁拦下。
        result = await relay.trigger_explicit("100", "向大家问好")
        self.assertFalse(result.get("success"))
        self.assertNotIn("休息", str(result.get("error")))


class GroupGateConfigTests(unittest.TestCase):
    """群闸门配置的默认值与校验。"""

    def test_defaults_are_off(self):
        config = MaiLifeSettings()
        self.assertFalse(config.rest_gate.group_enabled)
        self.assertEqual(config.rest_gate.group_mode, "all")
        self.assertEqual(config.rest_gate.group_night_start, "22:30")
        self.assertEqual(config.rest_gate.group_night_end, "08:00")
        self.assertEqual(config.rest_gate.group_nap_start, "12:00")
        self.assertEqual(config.rest_gate.group_nap_end, "14:30")
        self.assertIn("救命", config.rest_gate.group_force_wake_terms)
        profile = SocialGroupProfile(group_id="1")
        self.assertFalse(profile.rest_gate_enabled)

    def test_invalid_group_times_restore_defaults(self):
        config = MaiLifeSettings.model_validate({"rest_gate": {
            "group_night_start": "99:99", "group_nap_end": "xx"}})
        self.assertEqual(config.rest_gate.group_night_start, "22:30")
        self.assertEqual(config.rest_gate.group_nap_end, "14:30")

    def test_private_gate_unchanged(self):
        """群/私设置互不影响：改群窗口不动私聊窗口。"""
        config = MaiLifeSettings()
        config.rest_gate.group_night_start = "20:00"
        self.assertEqual(config.rest_gate.night_start, "22:30")
        self.assertTrue(config.rest_gate.force_wake_terms)


if __name__ == "__main__":
    unittest.main()
