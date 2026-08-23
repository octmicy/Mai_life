from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import PropertyMock,patch

from Mai_life.config import MaiLifeSettings,SocialGroupProfile,UserProfile
from Mai_life.core.storage import LifeStore,SCHEMA_VERSION
from Mai_life.messaging.adapter_compat import adapter_name,recall_notice
from Mai_life.messaging.message_pipeline import MessageDebouncer,direct_text
from Mai_life.messaging.recall_service import RecallService
from Mai_life.plugin import MaiLifePlugin
from Mai_life.social.group_observer import GroupObserver


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class DummyMessageCapability:
    def __init__(self):self.messages={}
    async def get_by_id(self,message_id,**kwargs):
        del kwargs
        return {"success":True,"message":self.messages.get(message_id)}


class DummySend:
    def __init__(self):self.messages=[]
    async def text(self,**kwargs):self.messages.append(kwargs); return True


class DummyContext:
    def __init__(self):
        self.logger=DummyLogger(); self.message=DummyMessageCapability(); self.send=DummySend()


class OfflineLLM:
    def task_available(self,kind):del kind; return False


def private_message(mid:str,text:str="测试消息",user_id:str="1",session_id:str="private-1",
                    adapter:str="napcat",with_binary:bool=False)->dict:
    image={"type":"image","hash":"image-hash"}
    if with_binary:image["binary_data_base64"]="c2Vuc2l0aXZlLWJpbmFyeQ=="
    raw=[{"type":"text","data":text}]
    if with_binary:raw.append(image)
    return {
        "message_id":mid,"session_id":session_id,"platform":"qq","processed_plain_text":text,
        "message_info":{"user_info":{"user_id":user_id},"additional_config":{f"{adapter}_message_type":"private"}},
        "raw_message":raw,"is_notify":False,"is_command":False,
    }


def group_message(mid:str,text:str="公开话题",group_id:str="100",user_id:str="1")->dict:
    return {
        "message_id":mid,"session_id":f"group-{group_id}","platform":"qq","processed_plain_text":text,
        "message_info":{"user_info":{"user_id":user_id},"group_info":{"group_id":group_id,"group_name":"测试群"},
                        "additional_config":{"napcat_message_type":"group"}},
        "raw_message":[{"type":"text","data":text}],"is_notify":False,"is_command":False,
    }


def recall_message(adapter:str,message_id:str,*,user_id:str="1",group_id:str="")->dict:
    notice_type="group_recall" if group_id else "friend_recall"
    payload={"message_id":message_id,"user_id":user_id,"operator_id":user_id}
    if group_id:payload["group_id"]=group_id
    additional={f"{adapter}_notice_type":notice_type,f"{adapter}_notice_payload":payload}
    if adapter=="snowluma":
        # SnowLuma 同时提供 NapCat 兼容字段，来源识别必须仍以原生字段为准。
        additional.update({"napcat_notice_type":notice_type,"napcat_notice_payload":dict(payload)})
    return {
        "message_id":f"notice-{adapter}-{message_id}",
        "session_id":f"group-{group_id}" if group_id else "private-1","platform":"qq","is_notify":True,
        "message_info":{"user_info":{"user_id":user_id},
                        "group_info":{"group_id":group_id} if group_id else None,
                        "additional_config":additional},
        "raw_message":[],
    }


class RecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.ctx=DummyContext()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    def test_both_adapters_share_recall_contract_and_snowluma_wins(self):
        for adapter in ("snowluma","napcat"):
            private=recall_notice(recall_message(adapter,"m1"))
            group=recall_notice(recall_message(adapter,"g1",group_id="100"))
            self.assertEqual(private["notice_type"],"friend_recall")
            self.assertEqual(group["notice_type"],"group_recall")
            self.assertEqual(private["recalled_message_id"],"m1")
            self.assertEqual(private["adapter"],adapter)
        self.assertEqual(adapter_name(recall_message("snowluma","m2")),"snowluma")

    async def test_schema_v8_upgrade_is_idempotent_and_merged_source_cancels_turn(self):
        now=time.time()
        await self.store.register_message_turn("private-1","m2",["m1","m2"],"1",now,now+600)
        await self.store.record_recall_event(
            session_id="private-1",recalled_message_id="m1",user_id="1",operator_id="1",group_id="",
            notice_type="friend_recall",source_adapter="napcat",summary="",media=[],now=now,expires_at=now+600,
        )
        self.assertTrue(await self.store.is_recalled_turn("private-1","m2",now+1))
        await self.store.initialize()
        self.assertEqual(SCHEMA_VERSION,9)
        columns={row[1] for row in self.store.conn.execute("PRAGMA table_info(recall_events)")}
        self.assertIn("summary_expires_at",columns)

    async def test_recall_removes_only_item_from_waiting_burst(self):
        object.__setattr__(self.config.debounce,"text_wait_seconds",0.05)
        debouncer=MessageDebouncer(self.config,DummyLogger())
        pending=asyncio.create_task(debouncer.collect(private_message("m1")))
        await asyncio.sleep(0.01)
        removed=await debouncer.recall("private-1","m1")
        allowed,_message,reason=await pending
        self.assertEqual(removed["message_id"],"m1")
        self.assertFalse(allowed); self.assertEqual(reason,"superseded")

    async def test_recall_one_merged_item_keeps_remaining_message(self):
        object.__setattr__(self.config.debounce,"text_wait_seconds",0.05)
        debouncer=MessageDebouncer(self.config,DummyLogger())
        first=asyncio.create_task(debouncer.collect(private_message("m1","第一句")))
        await asyncio.sleep(0.005)
        second=asyncio.create_task(debouncer.collect(private_message("m2","第二句")))
        await asyncio.sleep(0.005)
        await debouncer.recall("private-1","m1")
        old_result,new_result=await asyncio.gather(first,second)
        self.assertFalse(old_result[0]); self.assertTrue(new_result[0])
        self.assertEqual(direct_text(new_result[1]),"第二句")
        additional=new_result[1]["message_info"]["additional_config"]
        self.assertEqual(additional["mai_life_merged_message_ids"],["m2"])

    async def test_summary_default_off_and_enabled_cache_is_private_to_sender(self):
        service=RecallService(self.ctx,self.store,self.config,DummyLogger())
        service.note_inbound(private_message("m1","不会保存",with_binary=True))
        self.assertEqual(service._inbound,{})

        self.config.recall.cache_summary_enabled=True
        self.config.users.profiles=[UserProfile(user_id="1"),UserProfile(user_id="2")]
        await self.store.sync_users([UserProfile(user_id="1"),UserProfile(user_id="2")])
        message=private_message("m2","这是本人撤回的内容",with_binary=True)
        service.note_inbound(message); await service.register_turn(message)
        await service.record_notice("private-1",recall_notice(recall_message("napcat","m2")),time.time())
        own=await service.query_context("private-1","1")
        other=await service.query_context("private-1","2")
        self.assertIn("本人撤回",own["item"]["summary"]); self.assertEqual(other["item"],{})
        row=dict(self.store.conn.execute("SELECT * FROM recall_events WHERE recalled_message_id='m2'").fetchone())
        self.assertNotIn("c2Vuc2l0aXZlLWJpbmFyeQ",json.dumps(row,ensure_ascii=False))
        expired=await service.query_context("private-1","1",time.time()+self.config.recall.summary_ttl_minutes*60+1)
        self.assertEqual(expired["item"],{})

    async def test_group_recall_never_caches_summary(self):
        self.config.recall.cache_summary_enabled=True
        await self.store.sync_users([UserProfile(user_id="1")])
        service=RecallService(self.ctx,self.store,self.config,DummyLogger())
        await service.record_notice("group-100",recall_notice(recall_message("snowluma","g1",group_id="100")))
        row=dict(self.store.conn.execute("SELECT * FROM recall_events WHERE recalled_message_id='g1'").fetchone())
        self.assertEqual(row["summary"],""); self.assertEqual(row["media_types"],"[]")

    async def test_host_summary_recovery_happens_after_tombstone(self):
        self.config.recall.cache_summary_enabled=True
        self.config.users.profiles=[UserProfile(user_id="1")]
        await self.store.sync_users(self.config.users.profiles)
        self.ctx.message.messages["m3"]=private_message("m3","热重载后恢复的摘要")
        service=RecallService(self.ctx,self.store,self.config,DummyLogger())
        notice=recall_notice(recall_message("napcat","m3")); now=time.time()
        result=await service.record_notice("private-1",notice,now)
        self.assertTrue(result["needs_summary_recovery"])
        self.assertTrue(await service.is_turn_recalled("private-1","m3",now+0.01))
        self.assertEqual((await service.query_context("private-1","1",now+0.01))["item"],{})
        self.assertTrue(await service.recover_notice_summary("private-1",notice,now))
        item=(await service.query_context("private-1","1",now+0.02))["item"]
        self.assertIn("热重载后恢复",item["summary"])

    async def test_replyer_and_each_send_segment_check_persistent_tombstone(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","private-1")
        service=RecallService(self.ctx,self.store,self.config,DummyLogger())
        message=private_message("m1"); await service.register_turn(message)
        await service.record_notice("private-1",recall_notice(recall_message("napcat","m1")))
        plugin=MaiLifePlugin(); plugin._set_context(self.ctx)
        plugin.set_plugin_config(self.config.model_dump(mode="python")); plugin._store=self.store; plugin._recall=service
        plugin._session_runtime={"private-1":{"user_id":"1","message_id":"m1","source_message_ids":["m1"]}}
        reply=await plugin.on_replyer_after(session_id="private-1",response="不应发出",reply_message_id="m1")
        self.assertEqual(reply["modified_kwargs"]["response"],"")
        outbound=await plugin.on_send_before(message={"session_id":"private-1"},reply_message_id="m1")
        self.assertEqual(outbound["action"],"abort")

    async def test_recall_after_first_segment_only_blocks_remaining_segments(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","private-1")
        service=RecallService(self.ctx,self.store,self.config,DummyLogger())
        message=private_message("m1"); await service.register_turn(message)
        plugin=MaiLifePlugin(); plugin._set_context(self.ctx)
        plugin.set_plugin_config(self.config.model_dump(mode="python")); plugin._store=self.store; plugin._recall=service
        plugin._session_runtime={"private-1":{"user_id":"1","message_id":"m1","source_message_ids":["m1"]}}
        generated=await plugin.on_replyer_after(session_id="private-1",response="第一段\n第二段",reply_message_id="m1")
        self.assertEqual(generated["modified_kwargs"]["response"],"第一段\n第二段")
        self.assertEqual((await plugin.on_send_before(message={"session_id":"private-1"},reply_message_id="m1"))["action"],"continue")
        await plugin.on_send_after(message={"session_id":"private-1"},sent=True,reply_message_id="m1")
        await service.record_notice("private-1",recall_notice(recall_message("snowluma","m1")))
        remaining=await plugin.on_send_before(message={"session_id":"private-1"},reply_message_id="m1")
        self.assertEqual(remaining["action"],"abort")

    async def test_reply_alias_tombstone_survives_runtime_reconstruction(self):
        service=RecallService(self.ctx,self.store,self.config,DummyLogger()); now=time.time()
        await self.store.register_message_turn("private-1","m2",["m1","m2"],"1",now,now+600)
        await service.register_reply_anchor("private-1","older-quote",["m1","m2"],"1",now)
        await service.record_notice("private-1",recall_notice(recall_message("napcat","m1")),now+1)
        restored=MaiLifePlugin(); restored._set_context(self.ctx)
        restored.set_plugin_config(self.config.model_dump(mode="python")); restored._store=self.store
        restored._recall=RecallService(self.ctx,self.store,restored.config,DummyLogger())
        result=await restored.on_send_before(message={"session_id":"private-1"},reply_message_id="older-quote")
        self.assertEqual(result["action"],"abort")

    async def test_recall_query_command_is_private_and_cache_off_never_guesses(self):
        self.config.users.profiles=[UserProfile(user_id="1")]
        await self.store.sync_users(self.config.users.profiles); await self.store.set_user_stream("1","private-1")
        service=RecallService(self.ctx,self.store,self.config,DummyLogger())
        prompt=await service.query_prompt_context("private-1","1")
        self.assertIn("没有保存撤回内容",prompt); self.assertIn("不得",prompt)
        plugin=MaiLifePlugin(); plugin._set_context(self.ctx)
        plugin.set_plugin_config(self.config.model_dump(mode="python")); plugin._store=self.store; plugin._recall=service
        result=await plugin.cmd_recalled(user_id="1",stream_id="private-1",group_id="")
        self.assertEqual(len(result),3); self.assertEqual(result[2],2)
        self.assertIn("不会保存你撤回的内容",self.ctx.send.messages[-1]["text"])
        await plugin.cmd_recalled(user_id="1",stream_id="group-100",group_id="100")
        self.assertIn("私聊用户或私聊管理员",self.ctx.send.messages[-1]["text"])

    async def test_one_switch_handles_private_and_group_notices(self):
        await self.store.sync_users([UserProfile(user_id="1")])
        now=time.time()
        await self.store.record_interaction("1","即将撤回",now,12,source_message_id="m1")
        await self.store.set_wake_candidate("private-1","1","m1","明确叫醒",now,now+300)
        plugin=MaiLifePlugin(); plugin._set_context(self.ctx)
        plugin.set_plugin_config(self.config.model_dump(mode="python")); plugin._store=self.store
        plugin._recall=RecallService(self.ctx,self.store,plugin.config,DummyLogger())
        plugin._debouncer=MessageDebouncer(plugin.config,DummyLogger())
        with patch.object(MaiLifePlugin,"_ready",new_callable=PropertyMock,return_value=True):
            private=await plugin.on_receive(message=recall_message("napcat","m1"))
            group=await plugin.on_receive(message=recall_message("snowluma","g1",group_id="100"))
            self.assertEqual((private["action"],group["action"]),("abort","abort"))
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0],2)
            self.assertEqual((await self.store.get_user("1"))["last_user_message_at"],0)
            self.assertIsNone(self.store.conn.execute(
                "SELECT 1 FROM wake_candidates WHERE session_id='private-1' AND message_id='m1'"
            ).fetchone())
            plugin.config.recall.enabled=False
            disabled=await plugin.on_receive(message=recall_message("napcat","m2"))
            self.assertEqual(disabled["action"],"abort")
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0],2)

    async def test_group_observation_and_pending_buffer_are_removed(self):
        config=MaiLifeSettings(); config.social.enabled=True
        object.__setattr__(config.social,"observation_wait_seconds",0.05)
        config.social.groups=[SocialGroupProfile(group_id="100",observe_enabled=True)]
        observer=GroupObserver(self.store,config,OfflineLLM(),DummyLogger())
        now=datetime.now(timezone.utc)
        pending=asyncio.create_task(observer.observe(group_message("g1"),now))
        await asyncio.sleep(0.01)
        removed=await observer.recall("100","g1",now)
        result=await pending
        self.assertTrue(removed["pending"]); self.assertEqual(result["status"],"superseded")
        activity=await self.store.get_group_activity("100","1")
        self.assertEqual(activity["last_active_at"],0); self.assertEqual(activity["source_message_id"],"")
        await self.store.save_group_observation({
            "id":"obs","group_id":"100","group_alias":"测试群","topic":"公开话题","summary":"抽象摘要",
            "interest_score":0.8,"source_adapter":"napcat","created_at":now.timestamp(),
            "expires_at":now.timestamp()+600,"source_message_ids":["g2"],
        })
        self.assertEqual(await self.store.retract_group_observation_source("100","g2",now.timestamp()),1)
        self.assertEqual(await self.store.recent_group_observations(now.timestamp(),5),[])

    async def test_recalled_date_cancels_unfinished_proactive_chain(self):
        await self.store.sync_users([UserProfile(user_id="1")]); now=time.time()
        date_id=await self.store.add_important_date(
            "1","考试","2026-08-01","none","local_rule",now,source_message_id="m1",
        )
        opportunity_id="date-op"
        await self.store.add_opportunity({
            "id":opportunity_id,"framework_id":f"important-date:{date_id}","topic":"准备考试",
            "motive":"提醒","weight":0.8,"privacy":"normal","expires_at":now+600,"target_user_id":"1",
        })
        await self.store.consume_opportunity(opportunity_id,"1",now)
        await self.store.add_proactive_pending("date-event","1",opportunity_id,"private-1",now,now+300)
        await self.store.redact_recalled_private_artifacts("1","m1")
        self.assertEqual(await self.store.list_important_dates("1"),[])
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM proactive_opportunities WHERE id=?",(opportunity_id,)
        ).fetchone())
        status=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='date-event'").fetchone()[0]
        self.assertEqual(status,"cancelled")


if __name__=="__main__":unittest.main()
