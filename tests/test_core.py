from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Mai_life.config import MaiLifeSettings, UserProfile
from Mai_life.life_state import LifeStateEngine
from Mai_life.rest_gate import RestGate
from Mai_life.schedule_service import ScheduleService
from Mai_life.storage import LifeStore


class DummyLogger:
    def __getattr__(self, name): return lambda *args, **kwargs: None


class DummyLLM:
    async def generate(self, *args, **kwargs): return ""
    async def generate_json(self, prompt, system, fallback, max_tokens=0): return fallback


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
    async def test_relationship_daily_delta_is_bounded(self):
        await self.store.sync_users([UserProfile(user_id="1",initial_temperature=30)])
        start=time.time()-86400; end=start+86400
        for index in range(6):await self.store.record_interaction("1",f"msg-{index}",start+index+10,12)
        await self.store.update_relationships("2026-07-10",start,end,time.time())
        self.assertEqual((await self.store.get_user("1"))["temperature"],31.0)

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
    async def test_scene_delta_applied_once(self):
        day="2026-07-11"; nodes=self.service._fallback(day,False); await self.store.replace_framework(day,nodes)
        first=nodes[0]; await self.store.save_scene(first["id"],"睡觉",{"energy":5},[])
        self.assertEqual(len(await self.store.completed_unapplied_scenes(day,first["end_minute"])),1)
        await self.store.mark_scene_applied(first["id"])
        self.assertEqual(await self.store.completed_unapplied_scenes(day,first["end_minute"]),[])


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
        allowed,_=await self.gate.decide("1","醒醒，有急事",self.now,segment); self.assertTrue(allowed)
        self.assertGreater((await self.store.get_sleep_runtime())["awake_grace_until"],self.now.timestamp())
    async def test_awake_grace_skips_rejudge(self):
        await self.state_engine.mark_woken(self.now,"test"); self.config.rest_gate.wake_probability=0
        allowed,reason=await self.gate.decide("1","普通消息",self.now,{"kind":"sleep"})
        self.assertTrue(allowed); self.assertEqual(reason,"awake_grace")
    async def test_offline_state_progress(self):
        state=await self.store.get_state(); state["last_updated_at"]=self.now.timestamp()-7200; await self.store.save_state(state)
        result=await self.state_engine.advance(self.now,{"kind":"work","summary":"工作","location":"书桌"},None)
        self.assertLess(result["state"]["energy"],70); self.assertGreater(result["state"]["hunger"],20)


if __name__=="__main__": unittest.main()

