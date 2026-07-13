from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from Mai_life.config import (
    MaiLifeSettings,SocialGroupProfile,SocialRelationProfile,UserProfile,
)
from Mai_life.core.storage import LifeStore
from Mai_life.messaging.adapter_compat import SUPPORTED_ADAPTERS,adapter_name
from Mai_life.social.group_observer import GroupObserver
from Mai_life.social.relay_service import RelayService


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class OfflineLLM:
    def task_available(self,kind):return False


class SpyLLM:
    def __init__(self):self.calls=0
    def task_available(self,kind):return True
    async def generate_json(self,*args,**kwargs):self.calls+=1; return {"public":True,"score":1,"topic":"测试"}
    async def generate(self,*args,**kwargs):self.calls+=1; return "摘要"


class DummyChat:
    async def get_stream_by_group_id(self,group_id,platform="qq"):
        return {"stream_id":f"group-stream-{group_id}","group_id":group_id,"platform":platform}
    async def get_group_streams(self,platform="qq"):return []
    async def open_session(self,**kwargs):return {}


class DummyProactive:
    def __init__(self):self.calls=[]
    async def trigger(self,**kwargs):self.calls.append(kwargs); return True


class DummyMaisaka:
    def __init__(self):self.proactive=DummyProactive()


class DummyContext:
    def __init__(self):self.chat=DummyChat(); self.maisaka=DummyMaisaka()


def group_message(mid:str,text:str,adapter:str,user_id:str="other",group_id:str="100"):
    marker={f"{adapter}_message_type":"group"}
    return {"message_id":mid,"session_id":f"group-stream-{group_id}","platform":"qq",
            "processed_plain_text":text,"message_info":{
                "user_info":{"user_id":user_id,"user_nickname":"群友"},
                "group_info":{"group_id":group_id,"group_name":"测试群"},"additional_config":marker},
            "raw_message":[{"type":"text","data":text}],"is_command":False,"is_notify":False}


class SocialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.config.social.enabled=True
        # 生产配置下限为 0.5 秒；测试直接缩短内部等待以验证并发收口。
        object.__setattr__(self.config.social,"observation_wait_seconds",0.02)
        self.config.social.groups=[SocialGroupProfile(group_id="100",alias="朋友群",display_name="测试群",
                                                       observe_enabled=True,relay_target_enabled=True)]
        self.config.social.relations=[SocialRelationProfile(group_alias="朋友群",alias="小明",user_id="200",display_name="小明")]
        self.ctx=DummyContext()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_group_buffer_persists_abstract_summary_not_raw_messages(self):
        observer=GroupObserver(self.store,self.config,OfflineLLM(),DummyLogger())
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        first=asyncio.create_task(observer.observe(group_message("m1","小王说今晚游戏更新了", "napcat"),now))
        await asyncio.sleep(0.005)
        second=asyncio.create_task(observer.observe(group_message("m2","有人要一起看看新活动吗？", "napcat"),now))
        old,new=await asyncio.gather(first,second)
        self.assertEqual(old["status"],"superseded"); self.assertEqual(new["status"],"saved")
        rows=await self.store.recent_group_observations(now.timestamp(),5)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]["source_adapter"],"napcat")
        self.assertNotIn("小王说",rows[0]["summary"]); self.assertNotIn("一起看看",rows[0]["summary"])
        self.assertIn("游戏",rows[0]["summary"])

    async def test_sensitive_group_fragment_is_rejected_before_model_call(self):
        llm=SpyLLM(); observer=GroupObserver(self.store,self.config,llm,DummyLogger())
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        result=await observer.observe(group_message("m1","手机号是 13800138000", "snowluma"),now)
        self.assertEqual(result["status"],"private_or_empty"); self.assertEqual(llm.calls,0)
        self.assertEqual(await self.store.recent_group_observations(now.timestamp(),5),[])

    async def test_group_to_private_requires_known_six_hour_absence_and_queues_one_target(self):
        owner=UserProfile(user_id="1",role="owner",enabled=True,proactive_enabled=True)
        self.config.users.profiles=[owner]; self.config.social.interesting_threshold=0.5
        await self.store.sync_users([owner]); await self.store.set_user_stream("1","private-1")
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        await self.store.record_group_activity("100","1","主人",now.timestamp()-7*3600)
        observer=GroupObserver(self.store,self.config,OfflineLLM(),DummyLogger())
        first=asyncio.create_task(observer.observe(group_message("m1","游戏有新的大型更新", "snowluma"),now))
        await asyncio.sleep(0.005)
        second=asyncio.create_task(observer.observe(group_message("m2","群里在讨论周末游戏活动", "snowluma"),now))
        await asyncio.gather(first,second)
        opportunities=await self.store.active_opportunities(now.timestamp())
        targets=[item for item in opportunities if item.get("privacy")=="group_public"]
        self.assertEqual(len(targets),1); self.assertEqual(targets[0]["target_user_id"],"1")
        stats=await self.store.social_share_stats("1",now.replace(hour=0).timestamp())
        self.assertEqual(stats["count"],1)
        await self.store.add_proactive_pending("event-1","1",targets[0]["id"],"private-1",
                                               now.timestamp(),now.timestamp()+120)
        self.assertTrue(await self.store.mark_pending_sent("private-1",now.timestamp()+1))
        relay=self.store.conn.execute(
            "SELECT status FROM relay_candidates WHERE opportunity_id=?",(targets[0]["id"],)
        ).fetchone()
        self.assertEqual(relay[0],"sent")

    async def test_friend_group_share_requires_profile_opt_in(self):
        friend=UserProfile(user_id="2",role="friend",enabled=True,proactive_enabled=True,
                           group_to_private_enabled=True)
        self.config.users.profiles=[friend]; self.config.social.interesting_threshold=0.5
        await self.store.sync_users([friend]); await self.store.set_user_stream("2","private-2")
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        await self.store.record_group_activity("100","2","朋友",now.timestamp()-7*3600)
        observer=GroupObserver(self.store,self.config,OfflineLLM(),DummyLogger())
        first=asyncio.create_task(observer.observe(group_message("m1","游戏更新和周末活动", "napcat"),now))
        await asyncio.sleep(0.005)
        second=asyncio.create_task(observer.observe(group_message("m2","大家在讨论游戏活动", "napcat"),now))
        await asyncio.gather(first,second)
        targets=[item for item in await self.store.active_opportunities(now.timestamp())
                 if item.get("privacy")=="group_public"]
        self.assertEqual([item["target_user_id"] for item in targets],["2"])

    async def test_napcat_and_snowluma_share_one_standard_mention_contract(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        encoded=[]
        for adapter in SUPPORTED_ADAPTERS:
            result=await relay.trigger_explicit("朋友群",f"请转述 {adapter}","小明")
            self.assertTrue(result["success"])
            message=group_message(f"out-{adapter}","准备发送",adapter)
            message["raw_message"]=[{"type":"text","data":"测试转述"}]
            mutated,reserved=await relay.mutate_before_send(message)
            self.assertTrue(reserved); self.assertEqual(adapter_name(mutated),adapter)
            at=mutated["raw_message"][0]
            self.assertEqual(at["type"],"at"); self.assertEqual(at["data"]["target_user_id"],"200")
            encoded.append(at)
            # 原子 sending 状态确保同一回复的后续 Host 分段不会重复 @。
            later=dict(message); later["raw_message"]=[{"type":"text","data":"第二段"}]
            _later,reserved_again=await relay.mutate_before_send(later)
            self.assertFalse(reserved_again)
            self.assertTrue(await relay.confirm_after_send(mutated,True))
        self.assertEqual(encoded[0],encoded[1])
        self.assertEqual(len(self.ctx.maisaka.proactive.calls),2)

    async def test_ambiguous_relation_is_rejected_before_planner_trigger(self):
        self.config.social.relations.append(
            SocialRelationProfile(group_alias="朋友群",alias="小明",user_id="201",display_name="另一个小明")
        )
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        result=await relay.trigger_explicit("朋友群","测试","小明")
        self.assertFalse(result["success"]); self.assertIn("多个匹配",result["error"])
        self.assertEqual(self.ctx.maisaka.proactive.calls,[])

    async def test_planner_silence_keeps_pending_without_sent_event(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        result=await relay.trigger_explicit("朋友群","这是一条待判断的转述")
        context=await relay.prompt_context(result["stream_id"])
        self.assertIn("不可信背景数据",context); self.assertIn("可以完全沉默",context)
        row=self.store.conn.execute("SELECT status,sent_at FROM relay_candidates WHERE id=?",(result["relay_id"],)).fetchone()
        self.assertEqual(tuple(row),("pending",0.0))

    async def test_new_explicit_relay_supersedes_unsent_candidate(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        first=await relay.trigger_explicit("朋友群","旧转述")
        second=await relay.trigger_explicit("朋友群","新转述")
        rows=self.store.conn.execute(
            "SELECT id,status FROM relay_candidates WHERE id IN (?,?) ORDER BY created_at",
            (first["relay_id"],second["relay_id"]),
        ).fetchall()
        self.assertEqual({row[0]:row[1] for row in rows},
                         {first["relay_id"]:"superseded",second["relay_id"]:"pending"})
        context=await relay.prompt_context(second["stream_id"])
        self.assertIn("新转述",context); self.assertNotIn("旧转述",context)


if __name__=="__main__":unittest.main()
