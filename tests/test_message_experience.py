from __future__ import annotations

import base64
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,patch

from Mai_life.config import MaiLifeSettings,UserProfile
from Mai_life.core.environment import EnvironmentService
from Mai_life.core.storage import LifeStore
from Mai_life.life.life_state import LifeStateEngine
from Mai_life.life.rest_gate import RestGate
from Mai_life.messaging.adapter_compat import adapter_name,reply_target_ids
from Mai_life.messaging.message_pipeline import MessageDebouncer,direct_text,is_command,media_types,plain_text
from Mai_life.messaging.vision_service import VisionService
from Mai_life.plugin import MaiLifePlugin


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class DummyMessageCapability:
    async def get_by_id(self,*args,**kwargs):return {"success":True,"message":None}


class DummyContext:
    def __init__(self):self.message=DummyMessageCapability()


class FakeVisionLLM:
    def __init__(self):self.last_prompt=[]
    def task_available(self,kind):return kind=="vision_summary"
    async def generate(self,*args,**kwargs):
        self.last_prompt=args[0] if args else kwargs.get("prompt",[])
        return '{"summary":"一张桌面截图，显示正在编辑插件代码。","intent":"分享开发进度","ownership_hint":"由当前私聊用户直接发送"}'


class OfflineLLM:
    def task_available(self,kind):return False


class MessageExperienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _active_event(self,event_id:str="event1",opportunity_id:str="op1",task_id:str="proactive:test:1"):
        now=time.time()
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        await self.store.add_opportunity({"id":opportunity_id,"framework_id":"f","topic":"topic","motive":"motive",
            "weight":0.5,"privacy":"normal","expires_at":now+300})
        await self.store.consume_opportunity(opportunity_id,"1",now)
        await self.store.add_proactive_pending(event_id,"1",opportunity_id,"s1",now,now+180)
        await self.store.set_proactive_task_id(event_id,task_id)
        return now

    async def test_difficult_image_summary_stores_no_binary(self):
        config=MaiLifeSettings(); service=VisionService(DummyContext(),self.store,config,FakeVisionLLM(),DummyLogger())
        raw=b"\xff\xd8\xff"+b"test-image"
        message={"message_id":"m1","session_id":"s1","processed_plain_text":"[image]","message_info":{
            "additional_config":{"napcat_message_type":"private"}},"raw_message":[{
            "type":"image","hash":"h1","binary_data_base64":base64.b64encode(raw).decode(),
        }]}
        summary=await service.summarize_if_needed(message)
        self.assertIn("插件代码",summary)
        rows=await self.store.current_image_summaries("s1",time.time())
        self.assertEqual(len(rows),1)
        self.assertNotIn("binary",rows[0])

    async def test_emoji_gif_is_summarized_like_difficult_image(self):
        # 表情包（emoji）GIF 同样携带 binary_data_base64，应被视觉服务识别为难图并生成摘要。
        config=MaiLifeSettings(); service=VisionService(DummyContext(),self.store,config,FakeVisionLLM(),DummyLogger())
        raw=b"GIF89a"+b"emoji-gif-payload"
        message={"message_id":"m4","session_id":"s1","processed_plain_text":"[表情包]","message_info":{
            "additional_config":{"napcat_message_type":"private"}},"raw_message":[{
            "type":"emoji","hash":"eh1","binary_data_base64":base64.b64encode(raw).decode(),
        }]}
        self.assertIn("gif",media_types(message))
        summary=await service.summarize_if_needed(message)
        self.assertIn("插件代码",summary)

    async def test_static_emoji_alone_is_summarized(self):
        # 单条静态表情（无真实文字，仅 [表情包] 占位）也应被识别为难图并生成摘要。
        config=MaiLifeSettings(); service=VisionService(DummyContext(),self.store,config,FakeVisionLLM(),DummyLogger())
        raw=b"\x89PNG"+b"static-emoji-payload"
        message={"message_id":"m5","session_id":"s1","processed_plain_text":"[表情包]","message_info":{
            "additional_config":{"napcat_message_type":"private"}},"raw_message":[{
            "type":"emoji","hash":"eh2","binary_data_base64":base64.b64encode(raw).decode(),
        }]}
        self.assertEqual(plain_text(message),"")
        summary=await service.summarize_if_needed(message)
        self.assertIn("插件代码",summary)

    def test_adapter_image_placeholders_use_single_image_wait(self):
        config=MaiLifeSettings(); debouncer=MessageDebouncer(config,DummyLogger())
        for marker in ({"napcat_message_type":"private"},{"snowluma_message_type":"private"}):
            message={"processed_plain_text":"[image]","message_info":{"additional_config":marker},
                     "raw_message":[{"type":"image","binary_data_base64":"AA=="}]}
            self.assertEqual(plain_text(message),"")
            self.assertEqual(debouncer._quiet_wait([message]),config.debounce.image_wait_seconds)

    async def test_image_then_caption_still_gets_merged_visual_summary(self):
        config=MaiLifeSettings(); service=VisionService(DummyContext(),self.store,config,FakeVisionLLM(),DummyLogger())
        raw=b"\xff\xd8\xff"+b"merged-image"
        message={"message_id":"m2","session_id":"s1","processed_plain_text":"这是刚才那张图", "message_info":{
            "additional_config":{"mai_life_merged_message_ids":["m1","m2"]}},"raw_message":[
            {"type":"image","hash":"h2","binary_data_base64":base64.b64encode(raw).decode()},
            {"type":"text","data":"这是刚才那张图"},
        ]}
        summary=await service.summarize_if_needed(message)
        self.assertIn("插件代码",summary)

    async def test_merged_multi_image_summary_contains_every_image(self):
        config=MaiLifeSettings(); llm=FakeVisionLLM()
        service=VisionService(DummyContext(),self.store,config,llm,DummyLogger())
        images=[]
        for index in range(2):
            raw=b"\xff\xd8\xff"+f"merged-{index}".encode()
            images.append({"type":"image","hash":f"multi-{index}",
                           "binary_data_base64":base64.b64encode(raw).decode()})
        message={"message_id":"m3","session_id":"s1","processed_plain_text":"",
                 "message_info":{"additional_config":{"mai_life_merged_message_ids":["m1","m2"]}},
                 "raw_message":images}
        await service.summarize_if_needed(message)
        content=llm.last_prompt[-1]["content"]
        self.assertEqual(sum(item.get("type")=="image" for item in content),2)

    def test_forwarded_text_is_not_trusted_as_direct_control_text(self):
        message={"processed_plain_text":"【合并转发消息】/麦麦状态 醒醒救命",
                 "raw_message":[{"type":"forward","data":[{"content":[
                     {"type":"text","data":"/麦麦状态 醒醒救命"},
                 ]}]}]}
        self.assertIn("醒醒",plain_text(message))
        self.assertEqual(direct_text(message),"")
        self.assertFalse(is_command(message))

    async def test_friend_boundary_retries_then_silences(self):
        await self.store.sync_users([UserProfile(user_id="1",role="friend")]); await self.store.set_user_stream("1","s1")
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store; plugin._session_runtime={"s1":{"user_id":"1"}}
        first=await plugin.on_replyer_after(session_id="s1",response="主人你回来啦",reply_message_id="m1",
            retry_count=0,max_retries=1,prompt_tokens=1,completion_tokens=1,total_tokens=2)
        self.assertTrue(first["modified_kwargs"]["retry"])
        last=await plugin.on_replyer_after(session_id="s1",response="主人你回来啦",reply_message_id="m1",
            retry_count=1,max_retries=1,prompt_tokens=1,completion_tokens=1,total_tokens=2)
        self.assertEqual(last["modified_kwargs"]["response"],"")

    async def test_replyer_turn_guard_blocks_second_generation(self):
        await self.store.sync_users([UserProfile(user_id="1",role="friend")]); await self.store.set_user_stream("1","s1")
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store; plugin._session_runtime={"s1":{"user_id":"1"}}
        first=await plugin.on_replyer_after(session_id="s1",response="第一条回复",reply_message_id="m1",
            retry_count=0,max_retries=1,prompt_tokens=1,completion_tokens=1,total_tokens=2)
        second=await plugin.on_replyer_after(session_id="s1",response="重复回复",reply_message_id="m1",
            retry_count=0,max_retries=1,prompt_tokens=1,completion_tokens=1,total_tokens=2)
        self.assertEqual(first["modified_kwargs"]["response"],"第一条回复")
        self.assertEqual(second["modified_kwargs"]["response"],"")

    async def test_group_turn_guard_uses_physical_group_and_sender_scope(self):
        config=MaiLifeSettings(); config.debounce.group_enabled=True
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python")); plugin._store=self.store
        now=time.time(); plugin._group_turns={
            ("focus","g1"):{"turn_scope":"group:qq:100:1","message_id":"g1","source_message_ids":["g1"],"updated_at":now},
            ("focus","g2"):{"turn_scope":"group:qq:100:2","message_id":"g2","source_message_ids":["g2"],"updated_at":now},
        }
        first=await plugin.on_replyer_after(session_id="focus",response="用户一回复",reply_message_id="g1")
        duplicate=await plugin.on_replyer_after(session_id="focus",response="用户一重复",reply_message_id="g1")
        other=await plugin.on_replyer_after(session_id="focus",response="用户二回复",reply_message_id="g2")
        self.assertEqual(first["modified_kwargs"]["response"],"用户一回复")
        self.assertEqual(duplicate["modified_kwargs"]["response"],"")
        self.assertEqual(other["modified_kwargs"]["response"],"用户二回复")
        await plugin._cancel_group_confirmations("group:qq:100:2")
        first_confirmation=plugin._reply_confirmations[("focus","g1")]
        self.assertFalse(bool(first_confirmation.get("cancelled")))

    async def test_focus_shared_group_turn_never_injects_private_context(self):
        config=MaiLifeSettings(); config.debounce.group_enabled=True
        await self.store.sync_users([UserProfile(user_id="10001",role="owner")])
        await self.store.set_user_stream("10001","focus")
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python")); plugin._store=self.store
        plugin._session_runtime={"focus":{"user_id":"10001","message_id":"private-old"}}
        plugin._group_turns={("focus","group-new"):{"turn_scope":"group:qq:100:20001",
            "message_id":"group-new","source_message_ids":["group-new"],"updated_at":time.time()}}
        planner=await plugin.on_planner(session_id="focus",messages=[{"role":"user","content":"群消息"}])
        replyer=await plugin.on_replyer(session_id="focus",reply_message_id="group-new")
        self.assertEqual(planner,{"action":"continue"}); self.assertEqual(replyer,{"action":"continue"})

    async def test_new_group_message_only_cancels_older_same_sender_turn(self):
        config=MaiLifeSettings(); config.debounce.group_enabled=True
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python")); plugin._store=self.store
        now=time.time(); plugin._group_turns={
            ("focus","old"):{"turn_scope":"group:qq:100:1","message_id":"old","generation":1,"updated_at":now},
            ("focus","new"):{"turn_scope":"group:qq:100:1","message_id":"new","generation":2,"updated_at":now},
            ("focus","other"):{"turn_scope":"group:qq:100:2","message_id":"other","generation":3,"updated_at":now},
        }
        stale=await plugin.on_replyer_after(session_id="focus",response="旧回复",reply_message_id="old")
        latest=await plugin.on_replyer_after(session_id="focus",response="新回复",reply_message_id="new")
        other=await plugin.on_replyer_after(session_id="focus",response="另一人回复",reply_message_id="other")
        self.assertEqual(stale["modified_kwargs"]["response"],"")
        self.assertEqual(latest["modified_kwargs"]["response"],"新回复")
        self.assertEqual(other["modified_kwargs"]["response"],"另一人回复")

    async def test_reply_guard_does_not_touch_unconfigured_session(self):
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        first=await plugin.on_replyer_after(session_id="unconfigured",response="第一条",reply_message_id="m1")
        second=await plugin.on_replyer_after(session_id="unconfigured",response="第二条",reply_message_id="m1")
        self.assertNotIn("modified_kwargs",first); self.assertNotIn("modified_kwargs",second)

    async def test_expired_plugin_proactive_reply_is_silenced(self):
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=time.time(); await self.store.add_proactive_pending("event-expired","1","op","s1",now-10,now-1)
        await self.store.set_proactive_task_id("event-expired","proactive:test:expired")
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        plugin._session_runtime={"s1":{"user_id":"1"}}
        result=await plugin.on_replyer_after(session_id="s1",response="迟到的主动回复",
                                             reply_message_id="proactive:test:expired")
        self.assertEqual(result["modified_kwargs"]["response"],"")

    async def test_orphaned_mai_life_host_task_is_silenced(self):
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        task_id="proactive:maibot-community.mai-life:orphan"
        reply=await plugin.on_replyer_after(session_id="group-or-private",response="不应发送",reply_message_id=task_id)
        self.assertEqual(reply["modified_kwargs"]["response"],"")
        outbound=await plugin.on_send_before(message={"session_id":"group-or-private"},set_reply=True,
                                             reply_message_id=task_id)
        self.assertEqual(outbound["action"],"abort")

    async def test_failed_send_releases_exact_proactive_opportunity(self):
        now=time.time(); await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        await self.store.add_opportunity({"id":"op1","framework_id":"f","topic":"topic","motive":"motive",
            "weight":0.5,"privacy":"normal","expires_at":now+120})
        await self.store.consume_opportunity("op1","1",now)
        await self.store.add_proactive_pending("event1","1","op1","s1",now,now+120)
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        plugin._reply_confirmations[("s1","task1")]={"anchor":"task1","created_at":now,
            "proactive_event_id":"event1","proactive_opportunity_id":"op1"}
        await plugin.on_send_after(message={"session_id":"s1"},sent=False,reply_message_id="task1")
        event=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='event1'").fetchone()[0]
        opportunity=self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='op1'").fetchone()[0]
        self.assertEqual(event,"failed"); self.assertEqual(opportunity,0.0)

    async def test_direct_active_send_without_replyer_commits_quota_once(self):
        await self._active_event()
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        plugin._env=EnvironmentService(self.store,plugin.config,DummyLogger())
        await plugin.on_send_after(message={"session_id":"s1"},sent=True,reply_message_id="proactive:test:1")
        await plugin.on_send_after(message={"session_id":"s1"},sent=True,reply_message_id="proactive:test:1")
        event=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='event1'").fetchone()[0]
        user=await self.store.get_user("1")
        self.assertEqual(event,"sent"); self.assertEqual(user["proactive_count"],1)

    async def test_synthetic_active_anchor_is_never_encoded_as_qq_reply(self):
        await self._active_event()
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        for marker in ({"napcat_message_type":"private"},{"snowluma_message_type":"private"}):
            message={"session_id":"s1","message_info":{"additional_config":marker},
                     "raw_message":[{"type":"text","data":"主动消息"}]}
            result=await plugin.on_send_before(message=message,set_reply=True,reply_message_id="proactive:test:1")
            self.assertEqual(result["action"],"continue")
            self.assertFalse(result["modified_kwargs"]["set_reply"])
            self.assertEqual(result["modified_kwargs"]["reply_message_id"],"proactive:test:1")

    async def test_direct_failed_active_send_releases_opportunity(self):
        await self._active_event()
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        await plugin.on_send_after(message={"session_id":"s1"},sent=False,reply_message_id="proactive:test:1")
        event=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='event1'").fetchone()[0]
        consumed=self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='op1'").fetchone()[0]
        self.assertEqual(event,"failed"); self.assertEqual(consumed,0.0)

    async def test_wake_candidate_commits_only_for_exact_direct_send(self):
        config=MaiLifeSettings(); env=EnvironmentService(self.store,config,DummyLogger())
        state=LifeStateEngine(self.store,config,OfflineLLM(),DummyLogger())
        rest=RestGate(self.store,config,OfflineLLM(),state,DummyLogger())
        now=env.now(); await self.store.set_wake_candidate("s1","1","m1","明确叫醒",now.timestamp(),now.timestamp()+300)
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store=self.store; plugin._env=env; plugin._rest=rest
        await plugin.on_send_after(message={"session_id":"s1"},sent=True,reply_message_id="")
        self.assertIsNotNone(self.store.conn.execute("SELECT 1 FROM wake_candidates WHERE session_id='s1'").fetchone())
        await plugin.on_send_after(message={"session_id":"s1"},sent=True,reply_message_id="m1")
        runtime=await self.store.get_sleep_runtime()
        self.assertEqual(runtime["phase"],"woken")
        self.assertIsNone(self.store.conn.execute("SELECT 1 FROM wake_candidates WHERE session_id='s1'").fetchone())

    async def test_replyer_wake_confirmation_uses_final_inbound_not_old_quote(self):
        config=MaiLifeSettings(); env=EnvironmentService(self.store,config,DummyLogger())
        state=LifeStateEngine(self.store,config,OfflineLLM(),DummyLogger())
        rest=RestGate(self.store,config,OfflineLLM(),state,DummyLogger())
        await self.store.sync_users([UserProfile(user_id="1")]); await self.store.set_user_stream("1","s1")
        now=env.now(); await self.store.set_wake_candidate(
            "s1","1","new-inbound","明确叫醒",now.timestamp(),now.timestamp()+300,
        )
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store=self.store; plugin._env=env; plugin._rest=rest
        plugin._session_runtime={"s1":{"user_id":"1","message_id":"new-inbound"}}
        await plugin.on_replyer_after(session_id="s1",response="我醒了",reply_message_id="older-quote")
        await plugin.on_send_after(message={"session_id":"s1"},sent=True,reply_message_id="older-quote")
        self.assertEqual((await self.store.get_sleep_runtime())["phase"],"woken")

    async def test_new_inbound_cancels_pending_passive_send(self):
        config=MaiLifeSettings(); await self.store.sync_users([UserProfile(user_id="1")])
        await self.store.set_user_stream("1","s1")
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python")); plugin._store=self.store
        plugin._session_runtime={"s1":{"user_id":"1","message_id":"m1"}}
        await plugin.on_replyer_after(session_id="s1",response="旧回复",reply_message_id="m1")
        await plugin._cancel_reply_confirmations("s1")
        outbound=await plugin.on_send_before(message={"session_id":"s1"},reply_message_id="m1")
        self.assertEqual(outbound["action"],"abort")
        turn=self.store.conn.execute(
            "SELECT 1 FROM reply_turns WHERE session_id='s1' AND anchor_message_id='m1'"
        ).fetchone()
        self.assertIsNone(turn)

    async def test_active_multisegment_failure_after_first_success_does_not_reopen_event(self):
        await self._active_event(); config=MaiLifeSettings()
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python")); plugin._store=self.store
        plugin._env=EnvironmentService(self.store,config,DummyLogger()); plugin._session_runtime={"s1":{"user_id":"1"}}
        result=await plugin.on_replyer_after(session_id="s1",response="第一段\n第二段",reply_message_id="proactive:test:1")
        self.assertEqual(result["modified_kwargs"]["response"],"第一段\n第二段")
        await plugin.on_send_after(message={"session_id":"s1"},sent=True,reply_message_id="proactive:test:1")
        await plugin.on_send_after(message={"session_id":"s1"},sent=False,reply_message_id="proactive:test:1")
        event=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='event1'").fetchone()[0]
        consumed=self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='op1'").fetchone()[0]
        turn=self.store.conn.execute("SELECT 1 FROM reply_turns WHERE session_id='s1' AND anchor_message_id='proactive:test:1'").fetchone()
        self.assertEqual(event,"sent"); self.assertGreater(consumed,0); self.assertIsNotNone(turn)

    async def test_expired_active_tasks_abort_before_both_adapters(self):
        now=await self._active_event()
        self.store.conn.execute("UPDATE proactive_events SET expires_at=? WHERE id='event1'",(now-1,)); self.store.conn.commit()
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        for marker in ({"napcat_message_type":"private"},{"snowluma_message_type":"private"}):
            message={"session_id":"s1","message_info":{"additional_config":marker},"raw_message":[{"type":"text","data":"late"}]}
            result=await plugin.on_send_before(message=message,reply_message_id="proactive:test:1")
            self.assertEqual(result["action"],"abort")
        status=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='event1'").fetchone()[0]
        self.assertEqual(status,"expired")

    async def test_hot_disabled_active_task_is_cancelled_and_released(self):
        await self._active_event(); config=MaiLifeSettings(); config.proactive.enabled=False
        plugin=MaiLifePlugin(); plugin.set_plugin_config(config.model_dump(mode="python")); plugin._store=self.store
        message={"session_id":"s1","message_info":{"additional_config":{"snowluma_message_type":"private"}},
                 "raw_message":[{"type":"image","binary_data_base64":"AA=="}]}
        result=await plugin.on_send_before(message=message,reply_message_id="proactive:test:1")
        status=self.store.conn.execute("SELECT status FROM proactive_events WHERE id='event1'").fetchone()[0]
        consumed=self.store.conn.execute("SELECT consumed_at FROM proactive_opportunities WHERE id='op1'").fetchone()[0]
        self.assertEqual(result["action"],"abort"); self.assertEqual(status,"cancelled"); self.assertEqual(consumed,0.0)

    async def test_send_hooks_leave_unconfigured_group_untouched(self):
        plugin=MaiLifePlugin(); plugin.set_plugin_config(MaiLifeSettings().model_dump(mode="python")); plugin._store=self.store
        message={"session_id":"group-1","message_info":{"group_info":{"group_id":"1"},
            "additional_config":{"napcat_message_type":"group"}},"raw_message":[{"type":"text","data":"普通群回复"}]}
        result=await plugin.on_send_before(message=message,reply_message_id="group-message")
        self.assertEqual(result,{"action":"continue"})
        await plugin.on_send_after(message=message,sent=True,reply_message_id="group-message")

    async def test_public_relationship_api_excludes_stream_and_activity(self):
        await self.store.sync_users([UserProfile(user_id="1",role="owner")]); await self.store.set_user_stream("1","private-secret")
        plugin=MaiLifePlugin(); plugin._store=self.store
        result=await plugin.api_get_relationship("1")
        self.assertEqual(result["role"],"owner"); self.assertNotIn("stream_id",result)
        self.assertNotIn("last_user_message_at",result); self.assertNotIn("quiet_start",result)

    async def test_environment_snapshot_has_offline_fallback(self):
        service=EnvironmentService(self.store,MaiLifeSettings(),DummyLogger())
        snapshot=service.snapshot(platform="qq",adapter="napcat",chat_type="private",media=["image"])
        self.assertEqual(snapshot["platform"],"qq")
        self.assertEqual(snapshot["adapter"],"napcat")
        self.assertEqual(snapshot["media"],["image"])
        self.assertIn(snapshot["day_type"],{"工作日","休息日","元旦","春节","清明节","劳动节","端午节","中秋节","国庆节"})

    async def test_city_change_does_not_reuse_old_weather_cache(self):
        config=MaiLifeSettings(); service=EnvironmentService(self.store,config,DummyLogger())
        await self.store.save_weather({"fetched_at":time.time(),"location_name":"Shanghai","latitude":1,
            "longitude":2,"temperature":30,"weather_code":0,"description":"晴朗","raw_json":{}})
        changed=MaiLifeSettings(); changed.environment.city="Beijing"; service.update_config(changed)
        service._resolve_city=AsyncMock(side_effect=RuntimeError("offline"))
        weather=await service.refresh_weather(force=True)
        self.assertEqual(weather["description"],"天气未知")
        self.assertEqual(weather["location_name"],"Beijing")
        self.assertEqual(await self.store.get_weather(),{})

    async def test_restart_rejects_cache_tagged_for_another_city(self):
        await self.store.save_weather({"fetched_at":time.time(),"location_name":"上海","latitude":1,
            "longitude":2,"temperature":30,"weather_code":0,"description":"晴朗",
            "raw_json":{"_mai_life_query_city":"Shanghai"}})
        config=MaiLifeSettings(); config.environment.city="Beijing"
        service=EnvironmentService(self.store,config,DummyLogger())
        service._resolve_city=AsyncMock(side_effect=RuntimeError("offline"))
        weather=await service.refresh_weather(force=True)
        self.assertEqual(weather["description"],"天气未知")
        self.assertEqual(await self.store.get_weather(),{})

    async def test_restart_discards_legacy_weather_without_city_tag(self):
        await self.store.save_weather({"fetched_at":time.time(),"location_name":"未知旧城市","latitude":1,
            "longitude":2,"temperature":30,"weather_code":0,"description":"晴朗","raw_json":{}})
        config=MaiLifeSettings(); service=EnvironmentService(self.store,config,DummyLogger())
        service._resolve_city=AsyncMock(side_effect=RuntimeError("offline"))
        weather=await service.refresh_weather(force=True)
        self.assertEqual(weather["description"],"天气未知")
        self.assertEqual(await self.store.get_weather(),{})

    async def test_weather_result_is_discarded_when_city_changes_inflight(self):
        config=MaiLifeSettings(); config.environment.city="Shanghai"
        service=EnvironmentService(self.store,config,DummyLogger())
        async def resolve_and_switch(_city):
            changed=MaiLifeSettings(); changed.environment.city="Beijing"; service.update_config(changed)
            return "上海",1.0,2.0
        service._resolve_city=resolve_and_switch
        with patch("Mai_life.core.environment._fetch_json",return_value={
            "current":{"temperature_2m":30,"weather_code":0},
        }):
            weather=await service.refresh_weather(force=True)
        self.assertEqual(weather["location_name"],"Beijing")
        self.assertEqual(weather["description"],"天气未知")
        self.assertEqual(await self.store.get_weather(),{})

    def test_napcat_and_snowluma_messages_share_normalized_contract(self):
        fixtures=[
            {"name":"napcat","marker":{"napcat_message_type":"private"},"reply":{},"dict_type":"video"},
            {"name":"snowluma","marker":{"snowluma_message_type":"private"},"reply_to":"42","dict_type":"file"},
        ]
        for fixture in fixtures:
            with self.subTest(adapter=fixture["name"]):
                message={"reply_to":fixture.get("reply_to",""),"processed_plain_text":"看看这个", "message_info":{
                    "user_info":{"user_id":"1"},"additional_config":fixture["marker"]},"raw_message":[
                    {"type":"reply","data":{"target_message_id":"42"}},
                    {"type":"dict","data":{"type":fixture["dict_type"],"data":{}}},
                    {"type":"forward","data":[{"content":[{"type":"image","binary_data_base64":"AA=="}]}]},
                ]}
                self.assertEqual(adapter_name(message),fixture["name"])
                self.assertEqual(plain_text(message),"看看这个")
                self.assertIn(fixture["dict_type"],media_types(message))
                self.assertIn("forward",media_types(message)); self.assertIn("image",media_types(message))
                self.assertEqual(reply_target_ids(message),["42"])

    def test_adapter_text_fallbacks_preserve_media_awareness(self):
        fixtures=[("napcat","[视频] 文件: clip.mp4","video"),("napcat","[文件] 文件: notes.txt","file"),
                  ("snowluma","[voice]","voice")]
        for adapter,display,expected in fixtures:
            message={"processed_plain_text":display,"message_info":{"additional_config":{
                f"{adapter}_message_type":"private"}},"raw_message":[{"type":"text","data":display}]}
            self.assertIn(expected,media_types(message))


if __name__=="__main__":unittest.main()
