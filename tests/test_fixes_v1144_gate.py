from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from Mai_life.config import MaiLifeSettings, SocialGroupProfile, UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.life.rest_gate import BLOCK_REASON, RestGate
from Mai_life.messaging.message_pipeline import MessageDebouncer
from Mai_life.messaging.task_context import (
    ActiveTaskRegistry,
    HOST_TASK_PREFIX,
    PLUGIN_ID,
    PluginTaskMarker,
)
from Mai_life.social.group_observer import GroupObserver

TZ = timezone(timedelta(hours=8))


class DummyLogger:
    def __getattr__(self, name): return lambda *args, **kwargs: None


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


class ActiveTaskRetentionTests(unittest.IsolatedAsyncioTestCase):
    """P2-2：Replyer 迟到（>120s）时 pending 任务的归因必须存活。"""

    async def test_late_replyer_keeps_pending_attribution_alive(self):
        registry = ActiveTaskRegistry()
        # plugin.py 现只用 update_retention(120)；pending 过期窗保持 120，保留窗合成为 240s。
        registry.update_retention(120)
        now = time.time()
        marker = PluginTaskMarker(task_id=f"{HOST_TASK_PREFIX}9001", plugin_id=PLUGIN_ID, metadata={})
        record = {"id": "event-1", "status": "pending", "expires_at": now + 120,
                  "created_at": now, "opportunity_id": "op-1", "sent_at": 0}
        item = await registry.activate("s1", marker, kind="proactive", record=record, now=now)
        self.assertIsNotNone(item)
        self.assertGreaterEqual(item.retain_until, now + 240)
        # 迟到 130s：事件本身已过期，active 仍应存活以走完抑制与结算链路。
        late = await registry.current("s1", now + 130)
        self.assertIsNotNone(late)
        self.assertEqual(late.record_id, "event-1")

    async def test_update_retention_never_drops_pending_expire_below_120(self):
        registry = ActiveTaskRegistry()
        registry.update_retention(60)
        self.assertGreaterEqual(registry._pending_expire_seconds, 120)
        now = time.time()
        marker = PluginTaskMarker(task_id=f"{HOST_TASK_PREFIX}9002", plugin_id=PLUGIN_ID, metadata={})
        record = {"id": "event-2", "status": "sending", "expires_at": now + 60,
                  "created_at": now, "opportunity_id": "op-2", "sent_at": 0}
        item = await registry.activate("s1", marker, kind="proactive", record=record, now=now)
        self.assertIsNotNone(item)
        self.assertGreaterEqual(item.retain_until, now + 240)

    async def test_update_pending_expire_extends_retention(self):
        registry = ActiveTaskRegistry()
        registry.update_pending_expire(300)
        now = time.time()
        marker = PluginTaskMarker(task_id=f"{HOST_TASK_PREFIX}9003", plugin_id=PLUGIN_ID, metadata={})
        record = {"id": "event-3", "status": "pending", "expires_at": now + 120,
                  "created_at": now, "opportunity_id": "op-3", "sent_at": 0}
        item = await registry.activate("s1", marker, kind="proactive", record=record, now=now)
        self.assertIsNotNone(item)
        self.assertGreaterEqual(item.retain_until, now + 420)


class CleanupExpirePendingRaceTests(unittest.IsolatedAsyncioTestCase):
    """P2-3：维护清理与 expire_pending 共用同一套过期逻辑，不再静默失效。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        await self.store.sync_users([UserProfile(user_id="1")])
        await self.store.set_user_stream("1", "s1")

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _cycle(self, event_id: str, now: float) -> None:
        await self.store.add_opportunity({"id": "o1", "framework_id": "f", "topic": "t",
            "motive": "m", "weight": 0.5, "expires_at": now + 3600})
        await self.store.consume_opportunity("o1", "1", now)
        await self.store.add_proactive_pending(event_id, "1", "o1", "s1", now, now + 0.01)
        await self.store.cleanup_runtime_records(now + 1, now + 1)

    async def test_cleanup_releases_opportunity_and_records_planner_no_reply(self):
        now = time.time()
        await self._cycle("e1", now)
        conn = self.store.conn
        self.assertEqual(conn.execute(
            "SELECT status FROM proactive_events WHERE id='e1'").fetchone()[0], "expired")
        # 机会被释放，与 expire_pending 行为一致。
        self.assertEqual(conn.execute(
            "SELECT consumed_at FROM proactive_opportunities WHERE id='o1'").fetchone()[0], 0)
        # 跳过统计记 planner_no_reply。
        rows = conn.execute("SELECT user_id,reason,count FROM proactive_skip_stats").fetchall()
        self.assertEqual([(str(row[0]), str(row[1]), int(row[2])) for row in rows],
                         [("1", "planner_no_reply", 1)])
        # 未达 max_retries 上限时再次过期仍释放。
        await self._cycle("e2", now + 10)
        self.assertEqual(conn.execute(
            "SELECT consumed_at FROM proactive_opportunities WHERE id='o1'").fetchone()[0], 0)
        # 达到 max_retries=2 上限后不再释放，事件仍标记 expired。
        await self._cycle("e3", now + 20)
        self.assertNotEqual(conn.execute(
            "SELECT consumed_at FROM proactive_opportunities WHERE id='o1'").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT status FROM proactive_events WHERE id='e3'").fetchone()[0], "expired")
        rows = conn.execute("SELECT SUM(count) FROM proactive_skip_stats").fetchone()
        self.assertEqual(int(rows[0]), 3)


class GateTimeWindowTests(unittest.IsolatedAsyncioTestCase):
    """P2-4/P3-9/P3-10：时间窗优先、新增阻断词、夜间纯媒体阻断。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.config.rest_gate.enabled = True
        self.config.rest_gate.wake_probability = 0.0
        self.gate = RestGate(self.store, self.config, DummyLLM(), DummyStateEngine(), DummyLogger())

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_window_gates_non_rest_segment_kinds(self):
        # 默认框架的 leisure 段：夜间窗内一律判眠，闸门不再静默失效。
        allowed, reason = await self.gate.decide(
            "1", "普通消息", datetime(2026, 9, 21, 22, 45, tzinfo=TZ), {"kind": "leisure"})
        self.assertFalse(allowed); self.assertEqual(reason, "probability:0.00")
        # 午休窗内的 meal 段同样生效。
        allowed, reason = await self.gate.decide(
            "1", "普通消息", datetime(2026, 9, 21, 12, 30, tzinfo=TZ), {"kind": "meal"})
        self.assertFalse(allowed); self.assertEqual(reason, "probability:0.00")
        # 两个时间窗之外：即使框架写着 sleep 也放行。
        allowed, reason = await self.gate.decide(
            "1", "普通消息", datetime(2026, 9, 21, 10, 0, tzinfo=TZ), {"kind": "sleep"})
        self.assertTrue(allowed); self.assertEqual(reason, "outside_gate_window")

    async def test_night_media_only_message_is_blocked(self):
        night = datetime(2026, 9, 21, 23, 0, tzinfo=TZ)
        # 夜间纯图片（无文字）直接阻断，不走概率。
        allowed, reason = await self.gate.decide("1", "", night, {"kind": "sleep"}, media=["image"])
        self.assertFalse(allowed); self.assertEqual(reason, "夜间媒体消息，不叫醒")
        # 表情与 gif 同样阻断。
        allowed, reason = await self.gate.decide("1", "", night, {"kind": "sleep"}, media=["gif"])
        self.assertFalse(allowed); self.assertEqual(reason, "夜间媒体消息，不叫醒")
        # 有文字时仍走概率链路；media 缺省（None）不影响旧调用方。
        allowed, reason = await self.gate.decide("1", "在吗", night, {"kind": "sleep"}, media=["image"])
        self.assertFalse(allowed); self.assertEqual(reason, "probability:0.00")

    async def test_added_block_terms_cover_night(self):
        night = datetime(2026, 9, 21, 23, 0, tzinfo=TZ)
        for text in ("别烦我", "别吵"):
            allowed, reason = await self.gate.decide("1", text, night, {"kind": "sleep"})
            self.assertFalse(allowed); self.assertEqual(reason, BLOCK_REASON)


class ForceWakeGateTests(unittest.IsolatedAsyncioTestCase):
    """P2-9：强制唤醒词优先于勿扰词与概率，并建立待醒候选。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.config.rest_gate.enabled = True
        self.config.rest_gate.wake_probability = 0.0
        self.gate = RestGate(self.store, self.config, DummyLLM(), DummyStateEngine(), DummyLogger())
        self.night = datetime(2026, 9, 21, 23, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_default_force_term_wins_over_quiet_term(self):
        # 合并消息“安心睡+救命”：强制唤醒词优先，判 wake 并建立候选。
        allowed, reason = await self.gate.decide(
            "1", "安心睡吧，救命", self.night, {"kind": "sleep"}, session_id="s1", message_id="m1")
        self.assertTrue(allowed)
        self.assertEqual(reason, "强制唤醒词：救命")
        row = self.store.conn.execute(
            "SELECT reason FROM wake_candidates WHERE session_id='s1'").fetchone()
        self.assertIsNotNone(row); self.assertIn("救命", str(row[0]))

    async def test_custom_force_term_takes_effect(self):
        self.config.rest_gate.force_wake_terms = ["出大事了"]
        allowed, reason = await self.gate.decide(
            "1", "出大事了", self.night, {"kind": "sleep"}, session_id="s1", message_id="m1")
        self.assertTrue(allowed); self.assertEqual(reason, "强制唤醒词：出大事了")
        # 自定义词表后默认词不再强制；不含强制词仍走原边界逻辑。
        allowed, reason = await self.gate.decide("1", "在吗", self.night, {"kind": "sleep"})
        self.assertFalse(allowed); self.assertEqual(reason, "probability:0.00")
        allowed, reason = await self.gate.decide(
            "1", "快起床", self.night, {"kind": "sleep"}, session_id="s2", message_id="m2")
        self.assertTrue(allowed); self.assertEqual(reason, "明确叫醒、紧急或安全需要")


class ForceWakeDebounceTests(unittest.IsolatedAsyncioTestCase):
    """P2-9：强制唤醒词在防抖入口单独立即结算，不与后续消息合并。"""

    @staticmethod
    def _message(mid: str, text: str):
        return {"message_id": mid, "session_id": "s1", "platform": "qq",
                "processed_plain_text": text,
                "message_info": {"user_info": {"user_id": "1", "user_nickname": "u"},
                                 "group_info": None, "additional_config": {}},
                "raw_message": [{"type": "text", "data": text}],
                "is_command": False, "is_notify": False}

    async def test_force_term_settles_immediately(self):
        config = MaiLifeSettings()
        config.debounce.text_wait_seconds = 3.0
        config.debounce.max_wait_seconds = 5.0
        service = MessageDebouncer(config, DummyLogger())
        allowed, merged, reason = await asyncio.wait_for(
            service.collect(self._message("m1", "救命，出事了")), 0.5)
        self.assertTrue(allowed)
        self.assertEqual(reason, "merged:1")
        self.assertIn("救命", merged["processed_plain_text"])

    async def test_plain_message_waits_for_debounce_window(self):
        config = MaiLifeSettings()
        config.debounce.text_wait_seconds = 3.0
        config.debounce.max_wait_seconds = 5.0
        service = MessageDebouncer(config, DummyLogger())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(service.collect(self._message("m2", "在吗")), 0.2)
        await service.close()


class GroupPrivateSwitchTests(unittest.IsolatedAsyncioTestCase):
    """P2-5：群转私对主人是全局开关 AND 档案开关；朋友只看档案开关。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.config.social.enabled = True
        object.__setattr__(self.config.social, "observation_wait_seconds", 0.02)
        self.config.social.groups = [SocialGroupProfile(group_id="100", observe_enabled=True)]
        self.now = datetime(2026, 9, 21, 18, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _run(self, profile: UserProfile, owner_switch: bool, tag: str):
        await self.store.sync_users([profile])
        await self.store.set_user_stream(str(profile.user_id), "private-1")
        self.config.users.profiles = [profile]
        self.config.social.owner_group_to_private_enabled = owner_switch
        self.config.social.interesting_threshold = 0.5
        await self.store.record_group_activity("100", str(profile.user_id), "旧昵称",
                                               self.now.timestamp() - 7 * 3600)
        observer = GroupObserver(self.store, self.config, DummyLLM(), DummyLogger())
        first = asyncio.create_task(observer.observe(
            group_message("m1", f"{tag}游戏有新的大型更新"), self.now))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(observer.observe(
            group_message("m2", f"{tag}群里在讨论周末游戏活动"), self.now))
        await asyncio.gather(first, second)
        return [item for item in await self.store.active_opportunities(self.now.timestamp())
                if item.get("privacy") == "group_public"]

    async def test_owner_needs_both_switches(self):
        owner = UserProfile(user_id="10001", role="owner", enabled=True, proactive_enabled=True,
                            daily_proactive_max=2, group_to_private_enabled=True)
        # 全局开关关 + 档案开 → 不再创建群转私候选。
        self.assertEqual(await self._run(owner, False, "第一轮"), [])
        # 两者都开 → 创建；P3-17：契机权重下限 0.7，排在日常场景契机之前。
        targets = await self._run(owner, True, "第二轮")
        self.assertEqual([item["target_user_id"] for item in targets], ["10001"])
        self.assertTrue(all(float(item["weight"]) >= 0.7 for item in targets))

    async def test_friend_path_ignores_owner_switch(self):
        friend = UserProfile(user_id="10002", role="friend", enabled=True, proactive_enabled=True,
                             group_to_private_enabled=True)
        targets = await self._run(friend, False, "朋友")
        self.assertEqual([item["target_user_id"] for item in targets], ["10002"])


class GroupMergedSourceTests(unittest.IsolatedAsyncioTestCase):
    """P2-10：合并轮全部来源 ID 进入观察摘要，较早分段撤回后摘要被删。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.config.social.enabled = True
        object.__setattr__(self.config.social, "observation_wait_seconds", 0.02)
        self.config.social.groups = [SocialGroupProfile(group_id="100", observe_enabled=True)]
        self.now = datetime(2026, 9, 21, 18, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_recall_of_merged_segment_deletes_observation(self):
        observer = GroupObserver(self.store, self.config, DummyLLM(), DummyLogger())
        merged = group_message("m3", "游戏有新的大型更新")
        merged["message_info"]["additional_config"]["mai_life_merged_message_ids"] = ["m1", "m2", "m3"]
        result = await observer.observe(merged, self.now)
        self.assertEqual(result["status"], "saved")
        rows = await self.store.recent_group_observations(self.now.timestamp(), 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(json.loads(rows[0]["source_message_ids"])), ["m1", "m2", "m3"])
        # 撤回合并轮中最早的一条 → 观察摘要被删。
        removed = await observer.recall("100", "m1", self.now)
        self.assertGreaterEqual(removed["saved"], 1)
        self.assertEqual(await self.store.recent_group_observations(self.now.timestamp(), 5), [])


class MergeCommandAndCorruptRawTests(unittest.IsolatedAsyncioTestCase):
    """P3-11/P3-15：合并轮命令判定与坏 raw_message 防御。"""

    @staticmethod
    def _message(mid: str, text: str, command: bool = False):
        return {"message_id": mid, "session_id": "s1", "platform": "qq",
                "processed_plain_text": text,
                "message_info": {"user_info": {"user_id": "1", "user_nickname": "u"},
                                 "group_info": None, "additional_config": {}},
                "raw_message": [{"type": "text", "data": text}],
                "is_command": command, "is_notify": False}

    async def test_merged_round_is_command_when_any_segment_is_command(self):
        config = MaiLifeSettings(); config.debounce.text_wait_seconds = 0.04
        service = MessageDebouncer(config, DummyLogger())
        # 连发拆分命令：第二段是命令，合并轮按命令处理。
        first = asyncio.create_task(service.collect(self._message("m1", "先看看这个")))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(service.collect(self._message("m2", "/麦麦状态")))
        old, new = await asyncio.gather(first, second)
        self.assertFalse(old[0]); self.assertTrue(new[0])
        self.assertTrue(new[1]["is_command"])
        # 两段都是普通文本 → 不是命令。
        service = MessageDebouncer(config, DummyLogger())
        first = asyncio.create_task(service.collect(self._message("m3", "第一段")))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(service.collect(self._message("m4", "第二段")))
        old, new = await asyncio.gather(first, second)
        self.assertTrue(new[0]); self.assertFalse(new[1]["is_command"])

    def test_command_flag_and_forwarded_text_are_handled(self):
        config = MaiLifeSettings(); service = MessageDebouncer(config, DummyLogger())
        flagged = service._merge([self._message("m5", "普通"), self._message("m6", "继续", command=True)])
        self.assertTrue(flagged["is_command"])
        # 转发里的命令原文不算命令：direct_text 只读顶层。
        forward = {"message_id": "m7", "session_id": "s1", "platform": "qq",
                   "processed_plain_text": "【合并转发消息】/麦麦状态",
                   "message_info": {"user_info": {"user_id": "1"}, "group_info": None,
                                    "additional_config": {}},
                   "raw_message": [{"type": "forward", "data": [{"content": [
                       {"type": "text", "data": "/麦麦状态"}]}]}],
                   "is_command": False}
        merged = service._merge([self._message("m8", "看看这个"), forward])
        self.assertFalse(merged["is_command"])

    async def test_non_list_raw_message_is_skipped_without_crashing(self):
        config = MaiLifeSettings(); config.debounce.text_wait_seconds = 0.04
        service = MessageDebouncer(config, DummyLogger())
        corrupt = {"message_id": "m9", "session_id": "s1", "platform": "qq",
                   "processed_plain_text": "坏组件消息",
                   "message_info": {"user_info": {"user_id": "1"}, "group_info": None,
                                    "additional_config": {}},
                   "raw_message": {"type": "text", "data": "x"}, "is_command": False}
        first = asyncio.create_task(service.collect(self._message("m10", "正常消息")))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(service.collect(corrupt))
        old, new = await asyncio.gather(first, second)
        self.assertTrue(new[0])
        # 非列表组件被跳过：只剩正常消息的文本组件与分隔符。
        self.assertEqual([part.get("data") for part in new[1]["raw_message"]], ["正常消息", "\n"])
        self.assertEqual(new[1]["processed_plain_text"], "正常消息\n坏组件消息")
        merged = service._merge([self._message("m11", "单独"), corrupt])
        self.assertEqual([part.get("data") for part in merged["raw_message"]], ["单独", "\n"])


class LatestDreamOrderTests(unittest.IsolatedAsyncioTestCase):
    """P3-5：同秒梦境按 id 决胜，latest_dream 稳定返回最新一条。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_same_second_dreams_return_latest_id(self):
        stamp = time.time()
        first = await self.store.add_dream("先做的梦", 0.0, 0.0, stamp - 3600, ["片段一"], created_at=stamp)
        second = await self.store.add_dream("后做的梦", 0.0, 0.0, stamp - 3600, ["片段二"], created_at=stamp)
        self.assertGreater(second, first)
        dream = await self.store.latest_dream()
        self.assertEqual(dream["id"], second)
        self.assertEqual(dream["content"], "后做的梦")


if __name__ == "__main__":
    unittest.main()
