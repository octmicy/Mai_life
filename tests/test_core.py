from __future__ import annotations

import asyncio
import copy
import math
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Mai_life.config import MaiLifeSettings, UserProfile
from Mai_life.core.llm_service import LLMService
from Mai_life.core.storage import LifeStore
from Mai_life.life.life_state import LifeStateEngine
from Mai_life.life.rest_gate import RestGate
from Mai_life.life.schedule_service import ScheduleService
from Mai_life.messaging.message_pipeline import MessageDebouncer, classify_intent, media_types


class DummyLogger:
    def __getattr__(self, name): return lambda *args, **kwargs: None


class DummyLLM:
    def task_available(self,kind): return False
    async def generate(self, *args, **kwargs): return ""
    async def generate_json(self, prompt, system, fallback, max_tokens=0, **kwargs): return fallback


class GateLLM(DummyLLM):
    def __init__(self,result):self.result=result
    def task_available(self,kind):return kind=="rest_wakeup"
    async def generate_json(self,*args,**kwargs):return dict(self.result)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()
    async def test_schema_is_idempotent_and_users_are_isolated(self):
        await self.store.initialize()
        await self.store.sync_users([UserProfile(user_id="1",initial_temperature=30),UserProfile(user_id="2",initial_temperature=70)])
        self.assertEqual((await self.store.get_user("1"))["temperature"],30)
        self.assertEqual((await self.store.get_user("2"))["temperature"],70)
        now=time.time(); await self.store.record_interaction("1","hello",now,12)
        self.assertGreater((await self.store.get_user("1"))["last_user_message_at"],0)
        self.assertEqual((await self.store.get_user("2"))["last_user_message_at"],0)

    async def test_batch_active_hours_and_rest_backlogs_return_per_user_dicts(self):
        await self.store.sync_users([UserProfile(user_id="1"),UserProfile(user_id="2")])
        now=time.time(); await self.store.record_interaction("1","第一条",now,9)
        await self.store.record_interaction("1","第二条",now+1,9)
        await self.store.record_interaction("2","第三条",now+2,21)
        since=now-60
        active=await self.store.active_hours_batch(["1","2","3"],since)
        self.assertEqual(active["1"],{9:2})
        self.assertEqual(active["2"],{21:1})
        self.assertEqual(active["3"],{})
        self.assertEqual(active,{key:await self.store.active_hours(key,since) for key in ("1","2","3")})
        await self.store.add_rest_backlog("1","积压一",now)
        await self.store.add_rest_backlog("1","积压二",now+1)
        await self.store.add_rest_backlog("2","积压三",now+2)
        await self.store.add_rest_backlog("2","积压四",now+3)
        await self.store.add_rest_backlog("2","积压五",now+4)
        await self.store.add_rest_backlog("2","积压六",now+5)
        backlogs=await self.store.peek_rest_backlogs_batch(["1","2","3"])
        self.assertEqual(backlogs["1"],["积压一","积压二"])
        # 单次注入上限为 5 条（深夜连续被拦的消息尽量一次带过）。
        self.assertEqual(backlogs["2"],["积压三","积压四","积压五","积压六"])
        self.assertEqual(len(backlogs["2"]),4)
        self.assertEqual(backlogs["3"],[])
        self.assertEqual(backlogs["2"],await self.store.peek_rest_backlogs("2"))

    async def test_batch_scenes_by_framework_ids_keep_missing_entries(self):
        now=time.time()
        self.store.conn.executemany(
            "INSERT INTO daily_framework(id,day,start_minute,end_minute,kind,summary,location,energy_load,shareability) VALUES(?,?,?,?,?,?,?,?,?)",
            [("f1","2026-07-13",600,900,"daily","上午框架","家里",0.5,0.6),
             ("f2","2026-07-13",900,1200,"daily","下午框架","公司",0.4,0.5)],
        )
        self.store.conn.executemany(
            "INSERT INTO detailed_scenes(framework_id,scene,state_deltas,created_at) VALUES(?,?,?,?)",
            [("f1","切番茄","{\"energy\":-5}",now),
             ("f2","写代码","{\"energy\":-6,\"mood_valence\":0.2}",now)],
        )
        self.store.conn.commit()
        scenes=await self.store.get_scenes_by_framework_ids(["f1","f2","missing"])
        self.assertEqual(set(scenes),{"f1","f2","missing"})
        self.assertEqual(scenes["f1"]["scene"],"切番茄")
        self.assertEqual(scenes["f1"]["state_deltas"],{"energy":-5})
        self.assertEqual(scenes["f2"]["state_deltas"],{"energy":-6,"mood_valence":0.2})
        self.assertEqual(scenes["missing"],{})
        self.assertEqual(scenes["f1"]["scene"],(await self.store.get_scene("f1"))["scene"])

    async def test_role_defaults_resolve_to_per_user_quota(self):
        await self.store.sync_users([
            UserProfile(user_id="1",role="owner",daily_proactive_max=2),
            UserProfile(user_id="2",role="friend",daily_proactive_max=1),
        ])
        self.assertEqual((await self.store.get_user("1"))["daily_proactive_max"],2)
        self.assertEqual((await self.store.get_user("2"))["daily_proactive_max"],1)
    async def test_relationship_daily_delta_is_bounded(self):
        await self.store.sync_users([UserProfile(user_id="1",initial_temperature=30)])
        start=time.time()-86400; end=start+86400
        for index in range(6):await self.store.record_interaction("1",f"msg-{index}",start+index+10,12)
        await self.store.update_relationships("2026-07-10",start,end,time.time())
        self.assertEqual((await self.store.get_user("1"))["temperature"],31.0)

    async def test_relationship_decay_is_settled_by_each_missed_day(self):
        tz=timezone(timedelta(hours=8)); interacted=datetime(2026,7,1,12,0,tzinfo=tz)
        await self.store.sync_users([UserProfile(user_id="1",initial_temperature=30),
                                     UserProfile(user_id="2",initial_temperature=10)])
        await self.store.record_interaction("1","最后一次互动",interacted.timestamp(),12)
        await self.store.record_interaction("2","低温度用户互动",interacted.timestamp(),12)
        for offset in range(1,10):
            day=interacted.date()+timedelta(days=offset)
            start=datetime.combine(day,datetime.min.time(),tzinfo=tz); end=start+timedelta(days=1)
            await self.store.update_relationships(day.isoformat(),start.timestamp(),end.timestamp(),end.timestamp())
        # 沉默衰减 -0.4/天 × 9 天，30 分用户停在 10 分地板（-0.25→-0.4 的强化）。
        self.assertAlmostEqual((await self.store.get_user("1"))["temperature"],28.8)
        self.assertEqual((await self.store.get_user("2"))["temperature"],10.0)

    async def test_passive_reply_confirmation_does_not_open_write_transaction(self):
        journal=self.store.path.with_name(self.store.path.name+"-journal")
        self.assertFalse(await self.store.mark_pending_sent("no-pending-stream",time.time()))
        self.assertFalse(journal.exists())

    async def test_pending_only_counts_after_replyer_confirmation(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time(); await self.store.add_proactive_pending("e1","1","o1","s1",now,now+120)
        self.assertEqual((await self.store.get_user("1"))["proactive_count"],0)
        self.assertTrue(await self.store.mark_pending_sent("s1",now+1))
        self.assertEqual((await self.store.get_user("1"))["proactive_count"],1)

    async def test_proactive_confirmation_matches_exact_host_task(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time()
        await self.store.add_proactive_pending("e1","1","o1","s1",now,now+120)
        await self.store.add_proactive_pending("e2","1","o2","s1",now+0.1,now+120)
        await self.store.set_proactive_task_id("e1","proactive:test:1")
        await self.store.set_proactive_task_id("e2","proactive:test:2")
        event=await self.store.pending_proactive_for_task("s1","proactive:test:1",now+1)
        self.assertEqual(event["id"],"e1")
        self.assertTrue(await self.store.mark_pending_sent("s1",now+1,event_id="e1"))
        self.assertEqual((await self.store.get_user("1"))["proactive_count"],1)
        remaining=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='e2'").fetchone()
        self.assertEqual(remaining[0],"pending")

    async def test_exact_after_send_can_settle_when_platform_io_crosses_expiry(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time(); await self.store.add_proactive_pending("e1","1","o1","s1",now,now+0.01)
        await self.store.set_proactive_task_id("e1","proactive:test:1")
        self.assertTrue(await self.store.mark_pending_sent("s1",now+1,event_id="e1"))
        self.assertEqual((await self.store.get_user("1"))["proactive_count"],1)

    async def test_expired_event_still_settles_by_host_task_id(self):
        """发送确认跨越过期边界（事件已被 expire_pending 标记）后仍能按 host_task_id 结算。"""
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time()
        await self.store.add_opportunity({"id":"o1","framework_id":"f","topic":"t","motive":"m","weight":0.5,"expires_at":now+3600})
        await self.store.consume_opportunity("o1","1",now)
        await self.store.add_proactive_pending("e1","1","o1","s1",now,now+0.01)
        await self.store.set_proactive_task_id("e1","proactive:test:1")
        await self.store.expire_pending(now+1,max_retries=2)
        self.assertEqual(self.store.conn.execute("SELECT status FROM proactive_events WHERE id='e1'").fetchone()[0],"expired")
        self.assertEqual(self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='o1'").fetchone()[0],0)
        self.assertTrue(await self.store.mark_pending_sent("s1",now+2,host_task_id="proactive:test:1"))
        self.assertEqual((await self.store.get_user("1"))["proactive_count"],1)
        # 幂等：sent_at>0 后不再重复结算。
        self.assertFalse(await self.store.mark_pending_sent("s1",now+3,host_task_id="proactive:test:1"))
        self.assertEqual((await self.store.get_user("1"))["proactive_count"],1)

    async def test_expire_pending_stops_releasing_after_retry_limit(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time()
        await self.store.add_opportunity({"id":"o1","framework_id":"f","topic":"t","motive":"m","weight":0.5,"expires_at":now+3600})
        await self.store.consume_opportunity("o1","1",now)
        await self.store.add_proactive_pending("e1","1","o1","s1",now,now+0.01)
        await self.store.expire_pending(now+1,max_retries=1)
        self.assertEqual(self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='o1'").fetchone()[0],0)
        # 第二次触发并再次过期：历史 expired 数已达上限，机会不再释放。
        await self.store.consume_opportunity("o1","1",now+10)
        await self.store.add_proactive_pending("e2","1","o1","s1",now+10,now+10.01)
        await self.store.expire_pending(now+11,max_retries=1)
        self.assertNotEqual(self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='o1'").fetchone()[0],0)

    async def test_proactive_skip_stats_aggregate_by_reason(self):
        await self.store.record_proactive_skip("1","quiet",time.time())
        await self.store.record_proactive_skip("1","quiet",time.time())
        await self.store.record_proactive_skip("1","low_score",time.time())
        summary=await self.store.proactive_skip_summary(time.strftime("%Y-%m-%d"))
        by_reason={item["reason"]:item["total"] for item in summary}
        self.assertEqual(by_reason["quiet"],2); self.assertEqual(by_reason["low_score"],1)

    async def test_mood_events_cap_daily_and_apply_atomically(self):
        """心情事件按日限额原子加分；只动 mood_valence，不影响状态推进游标。"""
        now=datetime(2026,9,13,12,0,tzinfo=timezone.utc)
        before=(await self.store.get_state())
        self.assertAlmostEqual(float(before["mood_valence"]),0.0)
        for _ in range(3):
            self.assertAlmostEqual(await self.store.record_mood_event("passive_reply",now),0.02)
        self.assertEqual(await self.store.record_mood_event("passive_reply",now),0.0)
        self.assertAlmostEqual(await self.store.record_mood_event("proactive_reply",now),0.03)
        state=await self.store.get_state()
        self.assertAlmostEqual(float(state["mood_valence"]),0.09)
        self.assertEqual(float(state["last_updated_at"]),float(before["last_updated_at"]))
        # 次日限额重新计数。
        self.assertAlmostEqual(await self.store.record_mood_event("passive_reply",now+timedelta(days=1)),0.02)
        # 未知事件类型不加分。
        self.assertEqual(await self.store.record_mood_event("unknown_kind",now),0.0)

    async def test_state_snapshot_is_hourly_idempotent(self):
        state={"energy":70.0,"hunger":20.0,"mood_valence":0.1,"mood_arousal":0.6,
               "sleep_phase":"awake","current_activity":"自由活动"}
        base=datetime(2026,9,13,9,5,tzinfo=timezone.utc).timestamp()
        await self.store.save_state_snapshot(base,state)
        await self.store.save_state_snapshot(base+600,state)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0],1)
        await self.store.save_state_snapshot(base+3600,state)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0],2)
        row=self.store.conn.execute("SELECT * FROM state_snapshots ORDER BY ts").fetchone()
        self.assertAlmostEqual(float(row["energy"]),70.0); self.assertEqual(row["sleep_phase"],"awake")

    async def test_v12_database_upgrades_to_v13_without_data_loss(self):
        """模拟生产库 v12 → v13 原地升级：书柜、日记等旧数据完整保留。"""
        await self.store.sync_users([UserProfile(user_id="1")])
        await self.store.record_interaction("1","历史消息",time.time(),9)
        await self.store.save_diary("2026-09-01","旧日记","内容","平稳","digest",time.time())
        self.store.conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
        self.store.conn.commit()
        data_dir=str(self.store.path.parent); await self.store.close()
        reopened=LifeStore(data_dir); await reopened.initialize()
        try:
            self.assertEqual(reopened.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],"13")
            # 新表可用且为空。
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM mood_events").fetchone()[0],0)
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0],0)
            # 旧数据完整：互动、日记、书柜（含日记书柜化）全部保留。
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM interaction_events").fetchone()[0],1)
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM diary_entries").fetchone()[0],1)
            self.assertEqual(reopened.conn.execute(
                "SELECT COUNT(*) FROM bookshelf_documents WHERE doc_type='diary'").fetchone()[0],1)
            self.assertEqual(reopened.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],1)
        finally:
            await reopened.close()

    async def test_proactive_skip_stats_respect_explicit_day(self):
        """调用方按配置时区传入自然日；服务器时区不同也不会落错日期桶。"""
        now=time.time()
        await self.store.record_proactive_skip("1","quiet",now,day="2026-08-01")
        await self.store.record_proactive_skip("1","quiet",now,day="2026-08-01")
        await self.store.record_proactive_skip("1","quiet",now,day="2026-08-02")
        # proactive_skip_summary 是“day>=since”的累计口径：08-02 只含当天，08-01 含两天。
        self.assertEqual({item["reason"]:item["total"] for item in await self.store.proactive_skip_summary("2026-08-02")},{"quiet":1})
        self.assertEqual({item["reason"]:item["total"] for item in await self.store.proactive_skip_summary("2026-08-01")},{"quiet":3})

    async def test_expire_pending_records_skip_under_explicit_day(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time()
        await self.store.add_opportunity({"id":"o1","framework_id":"f","topic":"t","motive":"m","weight":0.5,"expires_at":now+3600})
        await self.store.consume_opportunity("o1","1",now)
        await self.store.add_proactive_pending("e1","1","o1","s1",now,now+0.01)
        await self.store.expire_pending(now+1,max_retries=2,day="2026-08-01")
        summary=await self.store.proactive_skip_summary("2026-08-01")
        self.assertEqual([item["reason"] for item in summary],["planner_no_reply"])

    async def test_cleanup_purges_stale_operational_records(self):
        """长期运行记录按保留期清理终态行；pending 与近期行不受影响。"""
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time(); old=now-200*86400
        await self.store.add_opportunity({"id":"o-old","framework_id":"f","topic":"t","motive":"m","weight":0.5,"expires_at":old})
        await self.store.add_opportunity({"id":"o-new","framework_id":"f","topic":"t","motive":"m","weight":0.5,"expires_at":now+3600})
        await self.store.add_proactive_pending("e-old","1","o-old","s1",old,old+120)
        await self.store.add_proactive_pending("e-pending","1","o-new","s1",now,now+120)
        self.assertTrue(await self.store.mark_pending_sent("s1",old,event_id="e-old"))
        await self.store.record_interaction("1","旧消息",old,9)
        await self.store.record_interaction("1","新消息",now-3600,9)
        await self.store.create_relay_candidate({"id":"r-old","kind":"group_to_private","target_user_id":"1",
            "target_stream_id":"s1","summary":"s","status":"pending","created_at":old,"expires_at":old+7200})
        await self.store.set_relay_status("r-old","expired",old+3600,"test")
        await self.store.create_relay_candidate({"id":"r-new","kind":"group_to_private","target_user_id":"1",
            "target_stream_id":"s1","summary":"s","status":"pending","created_at":now,"expires_at":now+7200})
        await self.store.record_proactive_skip("1","quiet",old,day=time.strftime("%Y-%m-%d",time.localtime(old)))
        await self.store.record_proactive_skip("1","quiet",now,day=time.strftime("%Y-%m-%d"))
        await self.store.cleanup_runtime_records(now,now,records_before=now-90*86400)
        conn=self.store.conn
        self.assertIsNone(conn.execute("SELECT 1 FROM proactive_events WHERE id='e-old'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM proactive_events WHERE id='e-pending'").fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM proactive_opportunities WHERE id='o-old'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM proactive_opportunities WHERE id='o-new'").fetchone())
        self.assertEqual([str(row[0]) for row in conn.execute("SELECT content_summary FROM interaction_events")],["新消息"])
        self.assertIsNone(conn.execute("SELECT 1 FROM relay_candidates WHERE id='r-old'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM relay_candidates WHERE id='r-new'").fetchone())
        self.assertIsNone(conn.execute("SELECT 1 FROM relay_events WHERE relay_id='r-old'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM relay_events WHERE relay_id='r-new'").fetchone())
        self.assertEqual([str(row[0]) for row in conn.execute("SELECT day FROM proactive_skip_stats")],[time.strftime("%Y-%m-%d")])

    async def test_wake_candidate_requires_matching_message(self):
        now=time.time(); await self.store.set_wake_candidate("s1","1","m1","wake",now,now+120)
        self.assertEqual(await self.store.pop_wake_candidate("s1",now+1,"m2"),{})
        self.assertEqual((await self.store.pop_wake_candidate("s1",now+1,"m1"))["message_id"],"m1")

    async def test_reply_turn_is_reserved_once_and_can_be_released(self):
        now=time.time()
        self.assertTrue(await self.store.reserve_reply_turn("s1","m1",now,now+60))
        self.assertFalse(await self.store.reserve_reply_turn("s1","m1",now+1,now+60))
        await self.store.release_reply_turn("s1","m1")
        self.assertTrue(await self.store.reserve_reply_turn("s1","m1",now+2,now+60))

    async def test_usage_statistics_separate_sources(self):
        now=time.time()
        await self.store.record_llm_usage(created_at=now,source="plugin",task_name="utils",model_name="m",
            request_type="continuity",prompt_tokens=10,completion_tokens=5,total_tokens=15,latency_ms=20,success=True)
        await self.store.record_llm_usage(created_at=now,source="host_replyer",task_name="replyer",model_name="m",
            request_type="reply",prompt_tokens=20,completion_tokens=10,total_tokens=30,latency_ms=0,success=True)
        rows=await self.store.usage_summary(now-1)
        self.assertEqual({row["source"] for row in rows},{"plugin","host_replyer"})

    async def test_v1_user_table_is_upgraded_without_losing_user(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        conn=sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','1');
        CREATE TABLE users(user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,proactive_enabled INTEGER NOT NULL,
          display_name TEXT NOT NULL,temperature REAL NOT NULL,quiet_start TEXT NOT NULL,quiet_end TEXT NOT NULL,
          stream_id TEXT NOT NULL DEFAULT '',last_user_message_at REAL NOT NULL DEFAULT 0,last_proactive_at REAL NOT NULL DEFAULT 0,
          proactive_day TEXT NOT NULL DEFAULT '',proactive_count INTEGER NOT NULL DEFAULT 0,last_relation_day TEXT NOT NULL DEFAULT '');
        INSERT INTO users VALUES('old',1,1,'旧用户',42,'00:00','08:00','',0,0,'',0,'');
        """); conn.commit(); conn.close()
        upgraded=LifeStore(other.name); await upgraded.initialize()
        user=await upgraded.get_user("old")
        self.assertEqual(user["temperature"],42); self.assertEqual(user["role"],"friend")
        await upgraded.close(); other.cleanup()

    async def test_v2_opportunity_table_gains_target_user_without_data_loss(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        conn=sqlite3.connect(path); conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','2');
        CREATE TABLE proactive_opportunities(
          id TEXT PRIMARY KEY,framework_id TEXT NOT NULL,topic TEXT NOT NULL,motive TEXT NOT NULL,
          weight REAL NOT NULL,privacy TEXT NOT NULL,expires_at REAL NOT NULL,
          consumed_by TEXT NOT NULL DEFAULT '',consumed_at REAL NOT NULL DEFAULT 0);
        INSERT INTO proactive_opportunities VALUES('old','f1','旧契机','旧数据',0.5,'normal',9999999999,'',0);
        """); conn.commit(); conn.close()
        upgraded=LifeStore(other.name); await upgraded.initialize()
        columns={row[1] for row in upgraded.conn.execute("PRAGMA table_info(proactive_opportunities)")}
        row=upgraded.conn.execute("SELECT topic,target_user_id FROM proactive_opportunities WHERE id='old'").fetchone()
        self.assertIn("target_user_id",columns); self.assertEqual(tuple(row),("旧契机",""))
        await upgraded.close(); other.cleanup()

    async def test_invalid_schema_version_is_preserved_and_rebuilt(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        conn=sqlite3.connect(path); conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','invalid');
        """); conn.commit(); conn.close()
        upgraded=LifeStore(other.name); await upgraded.initialize()
        backups=list(Path(other.name).glob("mai_life.incompatible.*.db"))
        self.assertEqual(len(backups),1)
        self.assertEqual(upgraded.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],"13")
        await upgraded.close(); other.cleanup()

    async def test_corrupt_database_is_closed_preserved_and_rebuilt(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        path.write_bytes(b"not-a-sqlite-database")
        upgraded=LifeStore(other.name); await upgraded.initialize()
        self.assertEqual(len(list(Path(other.name).glob("mai_life.corrupt.*.db"))),1)
        self.assertEqual(upgraded.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],"13")
        await upgraded.close(); other.cleanup()

    async def test_v8_to_v9_drops_skills_aliases_and_converts_legacy_quota(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        conn=sqlite3.connect(path); conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','8');
        CREATE TABLE users(user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,proactive_enabled INTEGER NOT NULL,
          display_name TEXT NOT NULL,temperature REAL NOT NULL,role TEXT NOT NULL DEFAULT 'friend',
          daily_proactive_max INTEGER NOT NULL DEFAULT -1,quiet_start TEXT NOT NULL,quiet_end TEXT NOT NULL,
          stream_id TEXT NOT NULL DEFAULT '',last_user_message_at REAL NOT NULL DEFAULT 0,last_proactive_at REAL NOT NULL DEFAULT 0,
          proactive_day TEXT NOT NULL DEFAULT '',proactive_count INTEGER NOT NULL DEFAULT 0,last_relation_day TEXT NOT NULL DEFAULT '');
        INSERT INTO users VALUES('10001',1,1,'手填昵称',35,'owner',-1,'00:00','08:00','private',0,0,'',0,'');
        CREATE TABLE memory_runtime(id INTEGER PRIMARY KEY,last_diary_day TEXT NOT NULL DEFAULT '',
          last_skill_day TEXT NOT NULL DEFAULT '',last_cleanup_at REAL NOT NULL DEFAULT 0);
        INSERT INTO memory_runtime VALUES(1,'2026-07-12','2026-07-12',12);
        CREATE TABLE skills(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE skill_events(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE relationship_entries(id INTEGER PRIMARY KEY,alias TEXT);
        """); conn.commit(); conn.close()
        upgraded=LifeStore(other.name); await upgraded.initialize(); await upgraded.initialize()
        tables={row[0] for row in upgraded.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns={row[1] for row in upgraded.conn.execute("PRAGMA table_info(memory_runtime)")}
        user=await upgraded.get_user("10001")
        self.assertFalse({"skills","skill_events","relationship_entries"}&tables)
        self.assertNotIn("last_skill_day",columns); self.assertEqual(user["daily_proactive_max"],2)
        self.assertEqual(user["display_name"],""); self.assertEqual(user["temperature"],35)
        await upgraded.close(); other.cleanup()

    async def test_v9_and_v10_upgrade_keeps_user_data_without_rebuild(self):
        """v9/v10 -> v11 升级曾因 DML 隐式事务嵌套炸库，导致整库被误替换为空库。"""
        for legacy_version,drop_search_history in ((9,True),(10,False)):
            with self.subTest(version=legacy_version):
                other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
                seeded=LifeStore(other.name); await seeded.initialize(); await seeded.close()
                conn=sqlite3.connect(path)
                if drop_search_history:
                    conn.execute("DROP TABLE IF EXISTS search_history")
                conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",(str(legacy_version),))
                conn.execute(
                    "INSERT OR IGNORE INTO users(user_id,enabled,proactive_enabled,display_name,temperature,"
                    "role,daily_proactive_max,quiet_start,quiet_end) "
                    "VALUES('10086',1,1,'升级前昵称',66.5,'owner',2,'00:00','08:00')")
                conn.execute(
                    "INSERT OR IGNORE INTO diary_entries(day,title,content,mood_summary,created_at) "
                    "VALUES('2026-08-01','升级前日记','升级前的日记正文','平静',1754000000)")
                conn.commit(); conn.close()
                upgraded=LifeStore(other.name); await upgraded.initialize()
                self.assertEqual(
                    upgraded.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],"13")
                self.assertEqual([],list(Path(other.name).glob("mai_life.incompatible.*.db")))
                user=await upgraded.get_user("10086")
                self.assertEqual(user["display_name"],"升级前昵称"); self.assertEqual(user["temperature"],66.5)
                diary=upgraded.conn.execute("SELECT title FROM diary_entries WHERE day='2026-08-01'").fetchone()
                self.assertEqual(diary[0],"升级前日记")
                await upgraded.close(); other.cleanup()


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.service=ScheduleService(self.store,self.config,DummyLLM(),str(Path(__file__).parents[1]),DummyLogger())
    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()
    def test_validation_repairs_overlap(self):
        raw=[{"start":"00:00","end":"08:00","kind":"sleep","summary":"睡觉","location":"卧室"},{"start":"07:30","end":"09:00","kind":"meal","summary":"早餐","location":"家"},{"start":"12:00","end":"13:00","kind":"meal","summary":"午饭","location":"家"}]
        nodes=self.service._validate("2026-07-11",raw)
        self.assertEqual(nodes[1]["start_minute"],nodes[0]["end_minute"])
        self.assertTrue(all(a["end_minute"]<=b["start_minute"] for a,b in zip(nodes,nodes[1:])))
    def test_template_path_cannot_escape_plugin_directory(self):
        self.config.schedule.template_file="../outside.json"
        self.assertEqual(self.service._template(),{})
        self.assertTrue(self.service._fallback("2026-07-13",False))
    def test_invalid_template_array_uses_builtin_framework(self):
        path=Path(self.tmp.name)/"invalid-template.json"
        path.write_text('[{"start":"09:00","end":"10:00","kind":"leisure"}]',encoding="utf-8")
        self.config.schedule.template_file=path.name
        service=ScheduleService(self.store,self.config,DummyLLM(),self.tmp.name,DummyLogger())
        nodes=service._fallback("2026-07-13",False)
        self.assertTrue(nodes)
        self.assertGreaterEqual(sum(item["kind"]=="meal" for item in nodes),2)
        self.assertTrue(any(item["kind"]=="sleep" for item in nodes))
    def test_validation_requires_real_night_sleep_and_supports_day_end(self):
        short=[{"start":"23:00","end":"24:00","kind":"sleep","summary":"睡觉","location":"卧室"},
               {"start":"08:00","end":"09:00","kind":"meal","summary":"早餐","location":"家"},
               {"start":"12:00","end":"13:00","kind":"meal","summary":"午饭","location":"家"}]
        self.assertEqual(self.service._validate("2026-07-11",short),[])
        fallback=self.service._fallback("2026-07-11",False)
        self.assertEqual(fallback[-1]["end_minute"],1440)
    async def test_scene_applied_flag_marks_delta_as_consumed(self):
        """场景 applied 标记保证节点结束增量只执行一次（经 get_scene 观察）。"""
        day="2026-07-11"; nodes=self.service._fallback(day,False); await self.store.replace_framework(day,nodes)
        first=nodes[0]; await self.store.save_scene(first["id"],"睡觉",{"energy":5},[])
        scene=await self.store.get_scene(first["id"])
        self.assertEqual(scene["state_deltas"],{"energy":5}); self.assertEqual(int(scene["applied"]),0)
        await self.store.mark_scene_applied(first["id"])
        scene=await self.store.get_scene(first["id"])
        self.assertEqual(int(scene["applied"]),1)
        # 批量读取口径一致：applied 后的场景不会再被 timeline 重复应用。
        scenes=await self.store.get_scenes_by_framework_ids([first["id"],"missing"])
        self.assertEqual(int(scenes[first["id"]]["applied"]),1); self.assertEqual(scenes["missing"],{})

    def test_validation_requires_three_meals_when_hungry(self):
        """饥饿偏高时两餐框架不合格、三餐框架通过；不饿时仍维持两餐下限。"""
        two_meals=[{"start":"00:00","end":"08:00","kind":"sleep","summary":"睡觉","location":"卧室"},
                   {"start":"08:00","end":"09:00","kind":"meal","summary":"早餐","location":"家"},
                   {"start":"12:00","end":"13:00","kind":"meal","summary":"午饭","location":"家"},
                   {"start":"19:00","end":"23:00","kind":"leisure","summary":"放松","location":"家"},
                   {"start":"23:00","end":"24:00","kind":"sleep","summary":"入睡","location":"卧室"}]
        three_meals=two_meals+[{"start":"18:00","end":"19:00","kind":"meal","summary":"晚饭","location":"家"}]
        self.assertEqual(self.service._validate("2026-07-11",two_meals,{"hunger":80}),[])
        self.assertTrue(self.service._validate("2026-07-11",three_meals,{"hunger":80}))
        self.assertTrue(self.service._validate("2026-07-11",two_meals,{"hunger":40}))
        self.assertTrue(self.service._validate("2026-07-11",two_meals))

    async def test_ensure_day_prompt_carries_current_state_summary(self):
        """日程生成提示词必须携带当前状态摘要：饥饿偏高时明确提示保证三餐。"""
        class CapturingLLM:
            def __init__(self):self.prompt=""
            def task_available(self,kind):return kind=="schedule"
            def task_for(self,kind):return "planner"
            async def generate_json(self,prompt,system,fallback,**kwargs):
                self.prompt=prompt; return fallback
        now=datetime(2026,9,19,3,0,tzinfo=timezone(timedelta(hours=8)))
        state=await self.store.get_state()
        state["energy"]=62; state["hunger"]=90; state["mood_valence"]=-0.35
        await self.store.save_state(state)
        llm=CapturingLLM()
        service=ScheduleService(self.store,self.config,llm,str(Path(__file__).parents[1]),DummyLogger())
        nodes=await service.ensure_day(now,"人格","晴",force=True)
        self.assertTrue(nodes)
        self.assertIn("麦麦当前状态",llm.prompt)
        self.assertIn("饥饿 90/100（很高，最近经常饿）",llm.prompt)
        self.assertIn("保证正常三餐",llm.prompt)
        # 饥饿偏高时两餐 LLM 结果被拒，回退到内置三餐骨架。
        self.assertGreaterEqual(sum(node["kind"]=="meal" for node in nodes),3)

    async def test_offline_timeline_crosses_sleep_and_meal_boundaries(self):
        start=datetime(2026,7,9,22,0,tzinfo=timezone(timedelta(hours=8)))
        now=datetime(2026,7,10,10,0,tzinfo=start.tzinfo)
        state=await self.store.get_state(); state["last_updated_at"]=start.timestamp(); await self.store.save_state(state)
        timeline=await self.service.state_timeline(start,now)
        engine=LifeStateEngine(self.store,self.config,DummyLLM(),DummyLogger())
        nodes=self.service._fallback(now.date().isoformat(),False)
        current,_=self.service.current_and_next(nodes,now.hour*60+now.minute-1)
        result=await engine.advance_timeline(now,timeline,current,None)
        self.assertEqual(result["state"]["sleep_phase"],"awake")
        self.assertLess(result["state"]["hunger"],20)
        self.assertTrue(await self.store.latest_dream())


    async def test_ensure_day_prompt_carries_variety_context(self):
        """日程生成提示词必须携带变化源：最近几天、历法、兴趣素材与具体化约束。"""
        class CapturingLLM:
            def __init__(self):self.prompt=""
            def task_available(self,kind):return kind=="schedule"
            def task_for(self,kind):return "planner"
            async def generate_json(self,prompt,system,fallback,**kwargs):
                self.prompt=prompt; return fallback
        now=datetime(2026,9,19,3,0,tzinfo=timezone(timedelta(hours=8)))
        yesterday=(now-timedelta(days=1)).date().isoformat()
        await self.store.replace_framework(yesterday,self.service._fallback(yesterday,False))
        await self.store.save_exploration_note({"id":"n1","topic":"深海火山","query":"q","summary":"s",
            "source_urls":[],"created_at":now.timestamp(),"relevance_score":0.5,
            "relevance_reason":"","opportunity_id":"","expires_at":now.timestamp()+86400})
        await self.store.create_bookshelf_document({"id":"w1","doc_type":"work","work_type":"essay",
            "title":"旧作","privacy":"public","status":"archived","created_at":1.0})
        llm=CapturingLLM()
        service=ScheduleService(self.store,self.config,llm,str(Path(__file__).parents[1]),DummyLogger())
        nodes=await service.ensure_day(now,"人格","晴",force=True,
            environment={"day_type":"周六","holiday":"","lunar":"七廿八","solar_term":"白露"})
        self.assertTrue(nodes)
        prompt=llm.prompt
        self.assertIn("最近几天的安排",prompt)
        self.assertIn("短暂午休",prompt)
        self.assertIn("今日历法",prompt); self.assertIn("白露",prompt)
        self.assertIn("活动灵感",prompt); self.assertIn("深海火山",prompt); self.assertIn("《旧作》",prompt)
        self.assertIn("禁止'处理自己的事情'",prompt)
        self.assertEqual(await self.store.get_framework(now.date().isoformat()),nodes)


class RestAndStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.config.rest_gate.enabled=True
        self.state_engine=LifeStateEngine(self.store,self.config,DummyLLM(),DummyLogger())
        self.gate=RestGate(self.store,self.config,DummyLLM(),self.state_engine,DummyLogger())
        self.now=datetime(2026,7,11,2,0,tzinfo=timezone(timedelta(hours=8)))
    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()
    async def test_explicit_quiet_blocks_and_wakeup_allows(self):
        segment={"kind":"sleep"}
        allowed,_=await self.gate.decide("1","不用回我，继续睡",self.now,segment); self.assertFalse(allowed)
        allowed,_=await self.gate.decide("1","醒醒，有急事",self.now,segment,session_id="s1",message_id="m1"); self.assertTrue(allowed)
        self.assertEqual((await self.store.get_sleep_runtime())["awake_grace_until"],0)
        await self.gate.commit_for_send("s1",self.now)
        self.assertGreater((await self.store.get_sleep_runtime())["awake_grace_until"],self.now.timestamp())
    async def test_awake_grace_skips_rejudge(self):
        await self.state_engine.mark_woken(self.now,"test"); self.config.rest_gate.wake_probability=0
        allowed,reason=await self.gate.decide("1","普通消息",self.now,{"kind":"sleep"})
        self.assertTrue(allowed); self.assertEqual(reason,"awake_grace")
    async def test_offline_state_progress(self):
        state=await self.store.get_state(); state["last_updated_at"]=self.now.timestamp()-7200; await self.store.save_state(state)
        result=await self.state_engine.advance(self.now,{"kind":"work","summary":"工作","location":"书桌"},None)
        self.assertLess(result["state"]["energy"],70); self.assertGreater(result["state"]["hunger"],20)

    async def test_mood_regression_doubles_under_sustained_hunger(self):
        """饥饿持续偏高时基线回归翻倍：终值高于不加权口径，且不钉死在 -1 钳位边界。"""
        state=await self.store.get_state()
        state["last_updated_at"]=self.now.timestamp()-24*3600
        state["hunger"]=90; state["energy"]=100; state["mood_valence"]=0.0
        await self.store.save_state(state)
        result=await self.state_engine.advance(self.now,{"kind":"rest","summary":"休息","location":"家"},None)
        weighted=result["state"]["mood_valence"]
        # 同样驱动下不加权的终值（按原公式复算：精力>70 奖励 + 饥饿惩罚 + 原速率回归）。
        mood=0.0+0.01*24-0.04*24
        mood+=(0.15-mood)*(1.0-math.exp(-0.0175*24))
        self.assertGreater(weighted,mood)
        self.assertGreater(weighted,-0.5)

    async def test_dream_skips_nap_but_survives_nap_before_night_sleep(self):
        """午休不做梦；nap 紧接夜间睡眠时，整晚睡眠的梦境不再被误判为午休抑制。"""
        base=self.now.replace(hour=0,minute=0,second=0,microsecond=0)
        async def seed(last_hour):
            state=await self.store.get_state()
            state["last_updated_at"]=(base+timedelta(hours=last_hour)).timestamp(); state["energy"]=40
            await self.store.save_state(state)
            runtime=await self.store.get_sleep_runtime()
            runtime.update({"phase":"awake","started_at":base.timestamp(),"last_event":""})
            await self.store.save_sleep_runtime(runtime)
        async def dream_count():
            return self.store.conn.execute("SELECT COUNT(*) FROM dreams").fetchone()[0]
        engine=LifeStateEngine(self.store,self.config,DummyLLM(),DummyLogger())
        # 纯午休 4 小时：时长足够但类型是 nap，不做梦。
        await seed(13)
        await engine.advance(base+timedelta(hours=13),{"kind":"nap","summary":"午休","location":"卧室"},None)
        result=await engine.advance(base+timedelta(hours=17),{"kind":"leisure","summary":"放松","location":"家"},None)
        self.assertTrue(result["woke"]); self.assertGreaterEqual(result["sleep_duration"],3)
        self.assertEqual(await dream_count(),0)
        # 22:00 nap 紧接 22:30-06:30 夜间睡眠：醒来后生成 1 条梦境。
        await seed(22)
        await engine.advance(base+timedelta(hours=22),{"kind":"nap","summary":"打盹","location":"卧室"},None)
        await engine.advance(base+timedelta(hours=22,minutes=30),{"kind":"sleep","summary":"夜间睡眠","location":"卧室"},None)
        result=await engine.advance(base+timedelta(days=1,hours=6,minutes=30),{"kind":"meal","summary":"早饭","location":"家"},None)
        self.assertTrue(result["woke"]); self.assertGreaterEqual(result["sleep_duration"],8.0)
        self.assertEqual(await dream_count(),1)

    async def test_body_cycle_applies_mild_adjustments(self):
        """经期内只做轻度修正：精力消耗提高约 10%，心情增量低于非经期。"""
        base=self.now.replace(hour=9,minute=0,second=0,microsecond=0); start=base-timedelta(hours=8)
        async def run(in_period: bool) -> dict[str, Any]:
            self.config.state.body_cycle_enabled=in_period
            self.config.state.body_cycle_start_date=base.date().isoformat() if in_period else ""
            state=await self.store.get_state()
            state.update({"energy":100.0,"hunger":20.0,"mood_valence":0.0,"mood_arousal":0.6,
                          "last_updated_at":start.timestamp()})
            await self.store.save_state(state)
            result=await self.state_engine.advance(base,{"kind":"work","summary":"工作","location":"书桌"},None)
            return result["state"]
        on=await run(True); off=await run(False)
        self.assertEqual(on["body_cycle"],"周期第1天"); self.assertEqual(off["body_cycle"],"未启用")
        # 精力消耗：经期恰为非经期的 1.1 倍。
        self.assertAlmostEqual(100-off["energy"],(100-on["energy"])/1.1,places=6)
        # 心情增量：经期被轻抑，低于非经期。
        self.assertLess(on["mood_valence"],off["mood_valence"])

    async def test_gate_requires_time_window_and_rest_segment(self):
        daytime=self.now.replace(hour=10)
        self.config.rest_gate.wake_probability=0
        allowed,reason=await self.gate.decide("1","普通消息",daytime,{"kind":"sleep"})
        self.assertTrue(allowed); self.assertEqual(reason,"outside_gate_window")
        self.assertFalse(self.gate._in_window("22:30","08:00","08:00"))
        self.assertFalse(self.gate._in_window("00:00","00:00","00:00"))

    async def test_model_gate_honors_quiet_and_safety_dimensions(self):
        self.config.rest_gate.mode="llm"
        quiet=RestGate(self.store,self.config,GateLLM({"score":100,"should_reply":True,"do_not_disturb":90}),
                       self.state_engine,DummyLogger())
        allowed,_=await quiet.decide("1","我先自己静一静",self.now,{"kind":"sleep"})
        self.assertFalse(allowed)
        safety=RestGate(self.store,self.config,GateLLM({"score":10,"should_reply":False,"safety_risk":95}),
                        self.state_engine,DummyLogger())
        allowed,_=await safety.decide("1","我现在有些不对劲",self.now,{"kind":"sleep"})
        self.assertTrue(allowed)


class DebounceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(mid,text):
        return {"message_id":mid,"session_id":"s1","platform":"qq","processed_plain_text":text,
            "message_info":{"user_info":{"user_id":"1","user_nickname":"u"},"group_info":None,"additional_config":{}},
            "raw_message":[{"type":"text","data":text}],"is_command":False,"is_notify":False}

    @staticmethod
    def group_message(mid,text,group_id="100",user_id="1",adapter="napcat"):
        return {"message_id":mid,"session_id":"focus-shared","platform":"qq","processed_plain_text":text,
            "message_info":{"user_info":{"user_id":user_id,"user_nickname":"可变昵称"},
                "group_info":{"group_id":group_id,"group_name":"群名"},
                "additional_config":{f"{adapter}_message_type":"group"}},
            "raw_message":[{"type":"text","data":text}],"is_command":False,"is_notify":False}

    async def test_concurrent_followups_only_keep_latest_hook(self):
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=0.04; config.debounce.max_wait_seconds=0.5
        service=MessageDebouncer(config,DummyLogger())
        first=asyncio.create_task(service.collect(self.message("m1","我想说")))
        await asyncio.sleep(0.01)
        second=asyncio.create_task(service.collect(self.message("m2","还有一件事")))
        old,new=await asyncio.gather(first,second)
        self.assertFalse(old[0]); self.assertTrue(new[0])
        self.assertEqual(new[1]["message_id"],"m2")
        self.assertIn("我想说",new[1]["processed_plain_text"])
        self.assertIn("还有一件事",new[1]["processed_plain_text"])

    async def test_close_releases_waiting_burst(self):
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=2; config.debounce.max_wait_seconds=3
        service=MessageDebouncer(config,DummyLogger())
        task=asyncio.create_task(service.collect(self.message("m1","准备卸载")))
        await asyncio.sleep(0.01); await service.close()
        result=await asyncio.wait_for(task,0.5)
        self.assertTrue(result[0])

    async def test_group_focus_session_isolated_by_group_and_sender_qq(self):
        config=MaiLifeSettings(); config.debounce.group_enabled=True
        config.debounce.group_text_wait_seconds=0.03; config.debounce.group_max_wait_seconds=0.5
        service=MessageDebouncer(config,DummyLogger())
        messages=[self.group_message("m1","群一用户一","100","1"),
                  self.group_message("m2","群一用户二","100","2"),
                  self.group_message("m3","群二用户一","200","1","snowluma")]
        results=await asyncio.gather(*(service.collect(item) for item in messages))
        self.assertTrue(all(item[0] for item in results))
        self.assertEqual([item[1]["processed_plain_text"] for item in results],
                         ["群一用户一","群一用户二","群二用户一"])

    async def test_group_same_sender_merges_and_recall_removes_source(self):
        config=MaiLifeSettings(); config.debounce.group_enabled=True
        config.debounce.group_text_wait_seconds=0.04; config.debounce.group_max_wait_seconds=0.5
        service=MessageDebouncer(config,DummyLogger())
        first=asyncio.create_task(service.collect(self.group_message("m1","第一句")))
        await asyncio.sleep(0.01)
        second=asyncio.create_task(service.collect(self.group_message("m2","补充一句")))
        await asyncio.sleep(0.01); removed=await service.recall("focus-shared","m1")
        old,new=await asyncio.gather(first,second)
        self.assertEqual(removed["message_id"],"m1"); self.assertFalse(old[0]); self.assertTrue(new[0])
        self.assertEqual(new[1]["processed_plain_text"],"补充一句")

    def test_group_and_private_use_independent_waits(self):
        config=MaiLifeSettings(); service=MessageDebouncer(config,DummyLogger())
        group=self.group_message("g","",adapter="snowluma"); group["raw_message"]=[{"type":"image","binary_data_base64":"AA=="}]
        private=self.message("p",""); private["raw_message"]=[{"type":"image","binary_data_base64":"AA=="}]
        self.assertEqual(service._quiet_wait([group],False),config.debounce.group_image_wait_seconds)
        self.assertEqual(service._quiet_wait([private],True),config.debounce.image_wait_seconds)
        group["raw_message"]=[{"type":"forward","data":[{"content":[{"type":"text","data":"转发"}]}]}]
        self.assertEqual(service._quiet_wait([group],False),config.debounce.group_forward_wait_seconds)

    def test_quiet_wait_reuses_entry_media_hint(self):
        config=MaiLifeSettings(); service=MessageDebouncer(config,DummyLogger())
        private=self.message("p",""); private["raw_message"]=[{"type":"image","binary_data_base64":"AA=="}]
        expected=media_types(private)
        self.assertEqual(expected,["image"])
        self.assertEqual(service._quiet_wait([private],True),config.debounce.image_wait_seconds)
        self.assertEqual(service._quiet_wait([private],True,media_hints=[["image"]]),config.debounce.image_wait_seconds)
        self.assertEqual(service._quiet_wait([private],True,media_hints=[list(expected)]),
                         service._quiet_wait([private],True))

    async def test_merge_uses_configured_separator(self):
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=0.04
        config.debounce.merge_separator=" || "
        service=MessageDebouncer(config,DummyLogger())
        first=asyncio.create_task(service.collect(self.message("m1","第一段")))
        await asyncio.sleep(0.01)
        second=asyncio.create_task(service.collect(self.message("m2","第二段")))
        old,new=await asyncio.gather(first,second)
        self.assertFalse(old[0]); self.assertTrue(new[0])
        self.assertEqual(new[1]["processed_plain_text"],"第一段 || 第二段")
        text_parts=[part["data"] for part in new[1]["raw_message"]
                    if isinstance(part,dict) and part.get("type")=="text"]
        self.assertIn(" || ",text_parts)

    async def test_ignore_empty_message_passes_through_without_burst(self):
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=3
        service=MessageDebouncer(config,DummyLogger())
        empty=self.message("m0",""); empty["raw_message"]=[{"type":"unsupported","data":"x"}]
        allowed,merged,reason=await service.collect(empty)
        self.assertTrue(allowed); self.assertIs(merged,empty); self.assertEqual(reason,"empty_ignored")
        self.assertEqual(service.active_bursts,0)

    async def test_ignore_empty_message_disabled_participates_in_burst(self):
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=0.04
        config.debounce.ignore_empty_message=False
        service=MessageDebouncer(config,DummyLogger())
        empty=self.message("m0",""); empty["raw_message"]=[{"type":"unsupported","data":"x"}]
        first=asyncio.create_task(service.collect(self.message("m1","正文")))
        await asyncio.sleep(0.01)
        second=asyncio.create_task(service.collect(empty))
        old,new=await asyncio.gather(first,second)
        self.assertFalse(old[0]); self.assertTrue(new[0])
        self.assertEqual(new[1]["processed_plain_text"],"正文")

    async def test_merged_message_carries_emoji_and_picture_flags(self):
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=0.04
        service=MessageDebouncer(config,DummyLogger())
        image=self.message("m1",""); image["raw_message"]=[{"type":"image","binary_data_base64":"AA=="}]
        emoji=self.message("m2",""); emoji["raw_message"]=[{"type":"emoji","data":"😀"}]
        first=asyncio.create_task(service.collect(image))
        await asyncio.sleep(0.01)
        second=asyncio.create_task(service.collect(emoji))
        old,new=await asyncio.gather(first,second)
        self.assertFalse(old[0]); self.assertTrue(new[0])
        self.assertTrue(new[1]["is_picture"]); self.assertFalse(new[1]["is_emoji"])
        # 纯表情的两条消息合并后 is_emoji 才是 True
        service=MessageDebouncer(config,DummyLogger())
        emoji2=self.message("m3",""); emoji2["raw_message"]=[{"type":"emoji","data":"🔥"}]
        third=asyncio.create_task(service.collect(copy.deepcopy(emoji)))
        await asyncio.sleep(0.01)
        fourth=asyncio.create_task(service.collect(emoji2))
        old2,new2=await asyncio.gather(third,fourth)
        self.assertFalse(old2[0]); self.assertTrue(new2[0])
        self.assertTrue(new2[1]["is_emoji"]); self.assertFalse(new2[1]["is_picture"])

    async def test_log_detail_records_debounce_events(self):
        class CountingLogger:
            def __init__(self):self.lines=[]
            def info(self,*args,**kwargs):
                if args and len(args)>1:self.lines.append(args[0]%args[1:])
                elif args:self.lines.append(args[0])
        config=MaiLifeSettings(); config.debounce.text_wait_seconds=0.04
        config.debounce.log_detail=True
        service=MessageDebouncer(config,CountingLogger())
        first=asyncio.create_task(service.collect(self.message("m1","你好")))
        await asyncio.sleep(0.01)
        second=asyncio.create_task(service.collect(self.message("m2","在吗")))
        await asyncio.gather(first,second)
        text="\n".join(service.logger.lines)
        self.assertIn("消息防抖开始",text)
        self.assertIn("消息防抖追加",text)
        self.assertIn("消息防抖结算",text)

    def test_local_intent_classifier(self):
        self.assertEqual(classify_intent("这张图里是什么",["image"]),"询问当前图片")
        self.assertEqual(classify_intent("醒醒，有急事",["text"]),"安全或紧急需要")


class LLMRouteTests(unittest.TestCase):
    """SDK 2.8.1 任务路由契约：签名含 task_name 时显式传任务名；旧 SDK 用 model 传任务名。"""

    @staticmethod
    def _make_service(llm):
        class Ctx:
            pass
        ctx = Ctx(); ctx.llm = llm; ctx.logger = DummyLogger()
        return LLMService(ctx, MaiLifeSettings(), store=None)

    def test_new_sdk_routes_by_task_name(self):
        captured = {}
        class NewSDKLLM:
            async def generate(self, prompt, model="", temperature=None, max_tokens=None,
                               *, task_name="utils", model_name="", **kwargs):
                captured.update({"model": model, "task_name": task_name, "prompt": prompt})
                return {"success": True, "response": "ok"}
            async def get_available_models(self): return ["planner"]
        service = self._make_service(NewSDKLLM())
        self.assertEqual(asyncio.run(service.generate("你好", task_kind="reasoning")), "ok")
        self.assertEqual(captured["task_name"], "planner")
        self.assertEqual(captured["model"], "")

    def test_old_sdk_routes_by_model_field(self):
        captured = {}
        class OldSDKLLM:
            async def generate(self, prompt, model="", temperature=None, max_tokens=None, **kwargs):
                captured.update({"model": model, "prompt": prompt, "extra": set(kwargs)})
                return {"success": True, "response": "ok"}
            async def get_available_models(self): return ["planner"]
        service = self._make_service(OldSDKLLM())
        self.assertEqual(asyncio.run(service.generate("你好", task_kind="reasoning")), "ok")
        self.assertEqual(captured["model"], "planner")
        self.assertNotIn("task_name", captured["extra"])


if __name__=="__main__": unittest.main()
