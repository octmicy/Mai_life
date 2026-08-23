from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Mai_life.config import MaiLifeSettings, UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.life.life_state import LifeStateEngine
from Mai_life.life.rest_gate import RestGate
from Mai_life.life.schedule_service import ScheduleService
from Mai_life.messaging.message_pipeline import MessageDebouncer, classify_intent


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

    async def test_role_defaults_resolve_to_per_user_quota(self):
        await self.store.sync_users([
            UserProfile(user_id="1",role="owner"),UserProfile(user_id="2",role="friend"),
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
        self.assertEqual((await self.store.get_user("1"))["temperature"],29.25)
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
        self.assertEqual(upgraded.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],"7")
        await upgraded.close(); other.cleanup()

    async def test_corrupt_database_is_closed_preserved_and_rebuilt(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        path.write_bytes(b"not-a-sqlite-database")
        upgraded=LifeStore(other.name); await upgraded.initialize()
        self.assertEqual(len(list(Path(other.name).glob("mai_life.corrupt.*.db"))),1)
        self.assertEqual(upgraded.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],"7")
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
    async def test_scene_delta_applied_once(self):
        day="2026-07-11"; nodes=self.service._fallback(day,False); await self.store.replace_framework(day,nodes)
        first=nodes[0]; await self.store.save_scene(first["id"],"睡觉",{"energy":5},[])
        self.assertEqual(len(await self.store.completed_unapplied_scenes(day,first["end_minute"])),1)
        await self.store.mark_scene_applied(first["id"])
        self.assertEqual(await self.store.completed_unapplied_scenes(day,first["end_minute"]),[])

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

    def test_local_intent_classifier(self):
        self.assertEqual(classify_intent("这张图里是什么",["image"]),"询问当前图片")
        self.assertEqual(classify_intent("醒醒，有急事",["text"]),"安全或紧急需要")


if __name__=="__main__": unittest.main()

