from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime,timedelta,timezone

from Mai_life.config import MaiLifeSettings,UserProfile
from Mai_life.core.storage import LifeStore,SCHEMA_VERSION
from Mai_life.life.life_state import LifeStateEngine
from Mai_life.life.memory_service import MemoryService,skill_stage
from Mai_life.life.proactive import ProactiveEngine


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class MemoryLLM:
    def __init__(self,available:bool=True):self.available=available; self.prompts=[]
    def task_available(self,kind):return self.available
    async def generate_json(self,prompt,system,fallback,max_tokens=0,**kwargs):
        self.prompts.append((kwargs.get("request_type"),prompt))
        if kwargs.get("request_type")=="daily_diary":
            return {"title":"收好今天","content":"今天做了自己的事，也和网友有过一些交流。","mood_summary":"安静而充实"}
        if kwargs.get("request_type")=="dream":
            return {"summary":"梦里走过一间亮着灯的旧书店。","fragments":["木门轻响","书页翻动","窗外下雨"],"mood":"warm"}
        return fallback


class TriggerRecorder:
    def __init__(self):self.calls=[]; self.proactive=self
    async def trigger(self,**kwargs):self.calls.append(kwargs)


class ProactiveContext:
    def __init__(self):self.maisaka=TriggerRecorder()


class LifeMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.llm=MemoryLLM(); self.service=MemoryService(self.store,self.config,self.llm,DummyLogger())
        self.now=datetime(2026,7,13,3,0,tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_current_schema_and_dream_fragments_are_idempotent(self):
        self.assertGreaterEqual(SCHEMA_VERSION,3); await self.store.initialize()
        dream_id=await self.store.add_dream("梦境摘要",0.02,0.5,self.now.timestamp()-8*3600,["片段一","片段二"])
        dream=await self.store.latest_dream()
        self.assertEqual(dream["id"],dream_id); self.assertEqual(dream["fragments"],["片段一","片段二"])

    async def test_diary_never_copies_interaction_content(self):
        await self.store.sync_users([UserProfile(user_id="owner",role="owner",proactive_enabled=True)])
        target=self.now.date()-timedelta(days=1); day=target.isoformat(); start,_=self.service._day_bounds(self.now,target)
        await self.store.replace_framework(day,[{"id":"n1","day":day,"start_minute":480,"end_minute":540,
            "kind":"meal","summary":"做早餐","location":"厨房","energy_load":-1,"shareability":0.3}])
        await self.store.record_interaction("owner","绝不能进入日记的聊天原句",start+10,9)
        await self.service._generate_diary(self.now,target)
        diary=await self.store.get_diary(day)
        self.assertEqual(diary["title"],"收好今天")
        diary_prompt=next(prompt for kind,prompt in self.llm.prompts if kind=="daily_diary")
        self.assertNotIn("绝不能进入日记的聊天原句",diary_prompt)
        opportunities=await self.store.active_opportunities(self.now.timestamp())
        self.assertEqual(next(item for item in opportunities if item["privacy"]=="owner_only")["target_user_id"],"owner")

    async def test_explicit_and_fuzzy_dates_are_isolated(self):
        await self.service.observe_message("u1","我的生日是8月15日",self.now)
        await self.service.observe_message("u2","下周三有考试",self.now)
        dates=await self.store.list_important_dates("u1"); candidates=await self.store.list_date_candidates("u2")
        self.assertEqual(len(dates),1); self.assertEqual(dates[0]["recurrence"],"annual")
        self.assertEqual(len(candidates),1); self.assertEqual(candidates[0]["event_name"],"考试")
        self.assertEqual(await self.store.list_important_dates("u2"),[])
        saved=await self.store.confirm_date_candidate(candidates[0]["id"],"u2","2026-07-22",self.now.timestamp())
        self.assertGreater(saved,0); self.assertEqual((await self.store.list_important_dates("u2"))[0]["event_date"],"2026-07-22")

    async def test_skill_growth_uses_evidence_dedup_and_daily_cap(self):
        day="2026-07-12"; now=self.now.timestamp()
        first=await self.store.add_skill_evidence(day,"编程","技术","schedule","调试插件","same",0.7,1.0,now)
        duplicate=await self.store.add_skill_evidence(day,"编程","技术","schedule","调试插件","same",0.7,1.0,now)
        capped=await self.store.add_skill_evidence(day,"编程","技术","schedule","继续调试","other",0.7,1.0,now)
        skill=(await self.store.list_skills())[0]
        self.assertTrue(first); self.assertFalse(duplicate); self.assertTrue(capped)
        self.assertEqual(skill["level"],1.0); self.assertEqual(skill["evidence_count"],2)
        self.assertEqual(skill_stage(skill["level"]),"不太熟")

    async def test_generated_dream_creates_fragments_and_opportunity(self):
        engine=LifeStateEngine(self.store,self.config,self.llm,DummyLogger())
        await engine.generate_dream(await self.store.get_state(),self.now.timestamp()-8*3600,8)
        dream=await self.store.latest_dream(); self.assertEqual(len(dream["fragments"]),3)
        self.assertGreater(dream["mood_delta"],0)
        opportunities=await self.store.active_opportunities(time.time())
        self.assertTrue(any(str(item["id"]).startswith("dream-") for item in opportunities))

    async def test_targeted_date_opportunity_only_triggers_target_user(self):
        await self.store.sync_users([
            UserProfile(user_id="u1",proactive_enabled=True,quiet_start="00:00",quiet_end="08:00"),
            UserProfile(user_id="u2",proactive_enabled=True,quiet_start="00:00",quiet_end="08:00"),
        ])
        await self.store.set_user_stream("u1","stream-1"); await self.store.set_user_stream("u2","stream-2")
        now=self.now.replace(hour=10); await self.store.add_important_date("u2","考试",now.date().isoformat(),"none","test",now.timestamp())
        await self.service._create_date_opportunities(now)
        ctx=ProactiveContext(); engine=ProactiveEngine(ctx,self.store,self.config,None,DummyLogger())
        triggered=await engine.patrol(now,await self.store.get_state())
        self.assertTrue(triggered); self.assertEqual(ctx.maisaka.calls[0]["stream_id"],"stream-2")


if __name__=="__main__":unittest.main()
