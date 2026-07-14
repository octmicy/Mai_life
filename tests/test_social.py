from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from Mai_life.config import MaiLifeSettings,SocialGroupProfile,UserProfile
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
        return {"stream_id":f"group-stream-{group_id}","group_id":group_id,
                "group_name":"Host 自动群名","platform":platform}
    async def get_group_streams(self,platform="qq"):return []
    async def open_session(self,**kwargs):return {}


class UnavailableChat:
    async def get_stream_by_group_id(self,**kwargs):raise RuntimeError("offline")
    async def get_group_streams(self,**kwargs):return []
    async def open_session(self,**kwargs):return {}


class DummyProactive:
    def __init__(self):self.calls=[]
    async def trigger(self,**kwargs):
        self.calls.append(kwargs); return {"success":True,"task_id":f"proactive:test:{len(self.calls)}"}


class DummyMaisaka:
    def __init__(self):self.proactive=DummyProactive()


class DummyContext:
    def __init__(self):self.chat=DummyChat(); self.maisaka=DummyMaisaka()


class FailedProactive(DummyProactive):
    async def trigger(self,**kwargs):self.calls.append(kwargs); return {"success":False,"error":"stream unavailable"}


class MissingTaskProactive(DummyProactive):
    async def trigger(self,**kwargs):self.calls.append(kwargs); return {"success":True}


def group_message(mid:str,text:str,adapter:str,user_id:str="30001",group_id:str="100"):
    marker={f"{adapter}_message_type":"group"}
    return {"message_id":mid,"session_id":f"group-stream-{group_id}","platform":"qq",
            "processed_plain_text":text,"message_info":{
                "user_info":{"user_id":user_id,"user_nickname":"可变昵称"},
                "group_info":{"group_id":group_id,"group_name":"Host 自动群名"},"additional_config":marker},
            "raw_message":[{"type":"text","data":text}],"is_command":False,"is_notify":False}


class SocialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.config.social.enabled=True
        object.__setattr__(self.config.social,"observation_wait_seconds",0.02)
        self.config.social.groups=[SocialGroupProfile(group_id="100",observe_enabled=True,relay_target_enabled=True)]
        self.ctx=DummyContext()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_group_buffer_persists_abstract_summary_and_host_name(self):
        observer=GroupObserver(self.store,self.config,OfflineLLM(),DummyLogger())
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        first=asyncio.create_task(observer.observe(group_message("m1","小王说今晚游戏更新了","napcat"),now))
        await asyncio.sleep(0.005)
        second=asyncio.create_task(observer.observe(group_message("m2","有人要一起看看新活动吗？","napcat"),now))
        old,new=await asyncio.gather(first,second)
        self.assertEqual(old["status"],"superseded"); self.assertEqual(new["status"],"saved")
        rows=await self.store.recent_group_observations(now.timestamp(),5)
        self.assertEqual(rows[0]["group_alias"],"Host 自动群名")
        self.assertNotIn("小王说",rows[0]["summary"]); self.assertIn("游戏",rows[0]["summary"])
        directory=await self.store.get_group_directory("100")
        self.assertEqual(directory["group_name"],"Host 自动群名")

    async def test_sensitive_group_fragment_is_rejected_before_model_call(self):
        llm=SpyLLM(); observer=GroupObserver(self.store,self.config,llm,DummyLogger())
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        result=await observer.observe(group_message("m1","手机号是 13800138000","snowluma"),now)
        self.assertEqual(result["status"],"private_or_empty"); self.assertEqual(llm.calls,0)

    async def test_group_to_private_compares_exact_qq_number(self):
        owner=UserProfile(user_id="10001",role="owner",enabled=True,proactive_enabled=True,daily_proactive_max=2)
        self.config.users.profiles=[owner]; self.config.social.interesting_threshold=0.5
        await self.store.sync_users([owner]); await self.store.set_user_stream("10001","private-1")
        now=datetime(2026,7,13,18,0,tzinfo=timezone(timedelta(hours=8)))
        await self.store.record_group_activity("100","10001","旧昵称",now.timestamp()-7*3600)
        await self.store.record_group_activity("100","99999","和主人同名",now.timestamp()-7*3600)
        observer=GroupObserver(self.store,self.config,OfflineLLM(),DummyLogger())
        first=asyncio.create_task(observer.observe(group_message("m1","游戏有新的大型更新","snowluma"),now))
        await asyncio.sleep(0.005)
        second=asyncio.create_task(observer.observe(group_message("m2","群里在讨论周末游戏活动","snowluma"),now))
        await asyncio.gather(first,second)
        targets=[item for item in await self.store.active_opportunities(now.timestamp()) if item.get("privacy")=="group_public"]
        self.assertEqual([item["target_user_id"] for item in targets],["10001"])

    async def test_relay_uses_exact_group_id_and_never_injects_at_for_both_adapters(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        rejected=await relay.trigger_explicit("Host 自动群名","不能按名称匹配")
        self.assertFalse(rejected["success"]); self.assertEqual(self.ctx.maisaka.proactive.calls,[])
        for adapter in SUPPORTED_ADAPTERS:
            result=await relay.trigger_explicit("100",f"请转述 {adapter}")
            self.assertTrue(result["success"])
            task_id=f"proactive:test:{len(self.ctx.maisaka.proactive.calls)}"
            message=group_message(f"out-{adapter}","准备发送",adapter)
            message["raw_message"]=[{"type":"text","data":"测试转述"}]
            mutated,reserved=await relay.mutate_before_send(message,task_id)
            self.assertTrue(reserved); self.assertEqual(adapter_name(mutated),adapter)
            self.assertEqual(mutated["raw_message"],[{"type":"text","data":"测试转述"}])
            self.assertFalse(any(item.get("type")=="at" for item in mutated["raw_message"]))
            self.assertTrue(await relay.confirm_after_send(mutated,True))
        directory=await self.store.get_group_directory("100")
        self.assertEqual(directory["group_name"],"Host 自动群名")

    async def test_planner_silence_and_new_relay_supersession(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        first=await relay.trigger_explicit("100","旧转述")
        second=await relay.trigger_explicit("100","新转述")
        context=await relay.prompt_context(second["stream_id"])
        self.assertIn("新转述",context); self.assertNotIn("旧转述",context); self.assertIn("不要构造 @",context)
        statuses={row[0]:row[1] for row in self.store.conn.execute(
            "SELECT id,status FROM relay_candidates WHERE id IN (?,?)",(first["relay_id"],second["relay_id"]))}
        self.assertEqual(statuses[first["relay_id"]],"superseded"); self.assertEqual(statuses[second["relay_id"]],"pending")

    async def test_host_task_id_prevents_superseded_relay_mismatch(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        first=await relay.trigger_explicit("100","旧转述"); second=await relay.trigger_explicit("100","新转述")
        self.assertTrue(await relay.should_abort_send(group_message("old","旧回复","napcat"),"proactive:test:1"))
        message=group_message("new","新回复","snowluma")
        self.assertFalse(await relay.should_abort_send(message,"proactive:test:2"))
        mutated,reserved=await relay.mutate_before_send(message,"proactive:test:2")
        self.assertTrue(reserved); self.assertTrue(await relay.confirm_after_send(mutated,True))
        self.assertEqual((await self.store.relay_candidate(first["relay_id"]))["status"],"superseded")
        self.assertEqual((await self.store.relay_candidate(second["relay_id"]))["status"],"sent")

    async def test_failed_or_unmapped_host_trigger_is_terminal(self):
        for proactive in (FailedProactive(),MissingTaskProactive()):
            self.ctx.maisaka.proactive=proactive; relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
            result=await relay.trigger_explicit("100","无法建立任务")
            self.assertFalse(result["success"])
            status=self.store.conn.execute("SELECT status FROM relay_candidates ORDER BY created_at DESC LIMIT 1").fetchone()[0]
            self.assertEqual(status,"failed")

    async def test_expired_relay_task_is_aborted(self):
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        result=await relay.trigger_explicit("100","过期转述")
        self.store.conn.execute("UPDATE relay_candidates SET expires_at=0 WHERE id=?",(result["relay_id"],)); self.store.conn.commit()
        self.assertTrue(await relay.should_abort_send(group_message("late","迟到回复","napcat"),"proactive:test:1"))

    async def test_shared_focus_stream_is_not_used_as_ambiguous_relay_fallback(self):
        self.ctx.chat=UnavailableChat(); now=datetime.now(tz=timezone.utc).timestamp()
        await self.store.upsert_group_directory("100","群一","focus-shared",now)
        await self.store.upsert_group_directory("200","群二","focus-shared",now)
        relay=RelayService(self.ctx,self.store,self.config,DummyLogger())
        result=await relay.trigger_explicit("100","不能误发到另一个群")
        self.assertFalse(result["success"]); self.assertEqual(self.ctx.maisaka.proactive.calls,[])


if __name__=="__main__":unittest.main()
