from __future__ import annotations

import base64
import tempfile
import time
import unittest

from Mai_life.config import MaiLifeSettings,UserProfile
from Mai_life.core.environment import EnvironmentService
from Mai_life.core.storage import LifeStore
from Mai_life.messaging.adapter_compat import adapter_name,reply_target_ids
from Mai_life.messaging.message_pipeline import media_types,plain_text
from Mai_life.messaging.vision_service import VisionService
from Mai_life.plugin import MaiLifePlugin


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class DummyMessageCapability:
    async def get_by_id(self,*args,**kwargs):return {"success":True,"message":None}


class DummyContext:
    def __init__(self):self.message=DummyMessageCapability()


class FakeVisionLLM:
    def task_available(self,kind):return kind=="vision_summary"
    async def generate(self,*args,**kwargs):
        return '{"summary":"一张桌面截图，显示正在编辑插件代码。","intent":"分享开发进度","ownership_hint":"由当前私聊用户直接发送"}'


class MessageExperienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_difficult_image_summary_stores_no_binary(self):
        config=MaiLifeSettings(); service=VisionService(DummyContext(),self.store,config,FakeVisionLLM(),DummyLogger())
        raw=b"\xff\xd8\xff"+b"test-image"
        message={"message_id":"m1","session_id":"s1","processed_plain_text":"","raw_message":[{
            "type":"image","hash":"h1","binary_data_base64":base64.b64encode(raw).decode(),
        }]}
        summary=await service.summarize_if_needed(message)
        self.assertIn("插件代码",summary)
        rows=await self.store.current_image_summaries("s1",time.time())
        self.assertEqual(len(rows),1)
        self.assertNotIn("binary",rows[0])

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

    async def test_environment_snapshot_has_offline_fallback(self):
        service=EnvironmentService(self.store,MaiLifeSettings(),DummyLogger())
        snapshot=service.snapshot(platform="qq",adapter="napcat",chat_type="private",media=["image"])
        self.assertEqual(snapshot["platform"],"qq")
        self.assertEqual(snapshot["adapter"],"napcat")
        self.assertEqual(snapshot["media"],["image"])
        self.assertIn(snapshot["day_type"],{"工作日","休息日","元旦","春节","清明节","劳动节","端午节","中秋节","国庆节"})

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


if __name__=="__main__":unittest.main()
