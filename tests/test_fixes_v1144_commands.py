"""v1.14.4 命令层反馈与文案修复的回归测试（P2-11~P2-14、P3-13/16/24/27/29/30/31）。"""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from typing import Any

from Mai_life.config import MaiLifeSettings,SearchProviderProfile,UserProfile
from Mai_life.core.environment import EnvironmentService
from Mai_life.core.llm_service import LLMService
from Mai_life.core.storage import LifeStore
from Mai_life.creation.bookshelf_service import BookshelfService
from Mai_life.creation.creation_service import CreationService
from Mai_life.information.information_service import InformationService
from Mai_life.life.continuity import ContinuityService
from Mai_life.life.life_state import LifeStateEngine
from Mai_life.life.memory_service import MemoryService
from Mai_life.life.proactive import ProactiveEngine
from Mai_life.life.rest_gate import RestGate
from Mai_life.life.schedule_service import ScheduleService
from Mai_life.management.admin_service import AdminService
from Mai_life.messaging.adapter_compat import recall_notice
from Mai_life.messaging.command_catalog import COMMAND_SECTIONS
from Mai_life.messaging.menu_renderer import MaiLifeMenuRenderer
from Mai_life.messaging.message_pipeline import MessageDebouncer
from Mai_life.messaging.recall_service import RecallService
from Mai_life.messaging.task_context import HOST_TASK_PREFIX,PLUGIN_ID,PluginTaskMarker
from Mai_life.plugin import MaiLifePlugin
from Mai_life.social.group_observer import GroupObserver
from Mai_life.social.relay_service import RelayService


class DummyLogger:
    def __getattr__(self,name:str):
        del name
        return lambda *args,**kwargs:None


class DummyChat:
    def __init__(self)->None:self.exact_stream="live-private"

    async def get_stream_by_user_id(self,user_id:str,platform:str="qq")->dict[str,Any]:
        return {"success":True,"stream":{"stream_id":self.exact_stream,"user_id":user_id,"platform":platform}}

    async def get_stream_by_group_id(self,group_id:str,platform:str="qq")->dict[str,Any]:
        return {"success":True,"stream":{"stream_id":f"live-group-{group_id}","group_id":group_id,"platform":platform}}

    async def get_private_streams(self,platform:str="qq")->dict[str,Any]:
        return {"success":True,"streams":[{"stream_id":self.exact_stream,"user_id":"10001","platform":platform}]}

    async def get_group_streams(self,platform:str="qq")->dict[str,Any]:
        return {"success":True,"streams":[]}

    async def get_all_streams(self,platform:str="qq")->dict[str,Any]:
        return await self.get_private_streams(platform)

    async def open_session(self,**kwargs:Any)->dict[str,Any]:
        del kwargs
        return {"success":True,"session_id":self.exact_stream}


class DummySend:
    def __init__(self,image_result:bool=False)->None:
        self.image_result=image_result; self.images:list[tuple[str,str]]=[]; self.texts:list[dict[str,str]]=[]

    async def image(self,image_data:str,stream_id:str)->bool:
        self.images.append((image_data,stream_id)); return self.image_result

    async def text(self,**kwargs:str)->bool:
        self.texts.append(dict(kwargs)); return True


class DummyContext:
    def __init__(self,image_result:bool=False)->None:
        self.logger=DummyLogger(); self.chat=DummyChat(); self.send=DummySend(image_result)


def command_message(text:str,*,user_id:str="10001",session_id:str="stream-10001",
                    message_id:str="m1")->dict[str,Any]:
    """构造一条命令形态的私聊消息；raw_message 保留原始文字（含尾空格）。"""
    return {
        "message_id":message_id,"session_id":session_id,"platform":"qq","processed_plain_text":text,
        "message_info":{"user_info":{"user_id":user_id},"additional_config":{}},
        "raw_message":[{"type":"text","data":text}],"is_notify":False,"is_command":False,
    }


class CommandFixTests(unittest.IsolatedAsyncioTestCase):
    """按 tests/test_fixes_v1143.py 的全服务构造法组装插件，再逐项验证修复。"""

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=LifeStore(self.tmp.name); await self.store.initialize()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _build_plugin(self,config:MaiLifeSettings,users:list[UserProfile])->tuple[MaiLifePlugin,DummyContext]:
        await self.store.sync_users(users)
        for user in users:await self.store.set_user_stream(str(user.user_id),f"stream-{user.user_id}")
        ctx=DummyContext(image_result=False); logger=ctx.logger
        llm=LLMService(ctx,config,self.store); llm.available_tasks=set(); llm.health_error=""
        plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store=self.store
        plugin._env=EnvironmentService(self.store,config,logger)
        plugin._llm=llm
        plugin._state=LifeStateEngine(self.store,config,llm,logger)
        plugin._schedule=ScheduleService(self.store,config,llm,".",logger)
        plugin._rest=RestGate(self.store,config,llm,plugin._state,logger)
        plugin._proactive=ProactiveEngine(ctx,self.store,config,plugin._env,logger)
        plugin._debouncer=MessageDebouncer(config,logger)
        plugin._recall=RecallService(ctx,self.store,config,logger)
        plugin._continuity=ContinuityService(self.store,config,llm,logger)
        plugin._memory=MemoryService(self.store,config,llm,logger)
        plugin._information=InformationService(ctx,self.store,config,llm,logger)
        plugin._group_observer=GroupObserver(self.store,config,llm,logger)
        plugin._relay=RelayService(ctx,self.store,config,logger)
        plugin._bookshelf=BookshelfService(self.store,config)
        plugin._creation=CreationService(ctx,self.store,config,llm,logger)
        plugin._admin=AdminService(self.store,config)
        return plugin,ctx

    @staticmethod
    def _sent_texts(ctx:DummyContext)->list[str]:
        return [str(item.get("text") or "") for item in ctx.send.texts]

    # P2-11：admin_user_ids 配为他人时，主人（档案 role=owner）不应被五条管理命令锁出。
    async def test_owner_passes_management_commands_when_admin_ids_points_elsewhere(self):
        config=MaiLifeSettings.model_validate({"plugin":{"admin_user_ids":["99999"]}})
        plugin,ctx=await self._build_plugin(config,[UserProfile(user_id="10001",role="owner")])
        common={"user_id":"10001","group_id":"","stream_id":"stream-10001","platform":"qq"}
        results=[
            await plugin.cmd_admin(**common,matched_groups={"scope":"概览"}),
            await plugin.cmd_create_now(**common),
            await plugin.cmd_tokens(**common),
            await plugin.cmd_regenerate(**common),
            await plugin.cmd_rest_test(**common),
        ]
        for result in results:
            self.assertEqual(len(result),3); self.assertEqual(result[2],2); self.assertTrue(result[0])
        texts=self._sent_texts(ctx)
        self.assertTrue(any("管理概览" in text for text in texts))
        self.assertTrue(any("书柜创作未启用" in text for text in texts))
        self.assertFalse(any("只有私聊管理员" in text for text in texts))
        # 无档案的管理员 90001 仍可过（既有契约：_command_access 不变）。
        admin_results=[
            await plugin.cmd_admin(user_id="90001",group_id="",stream_id="admin-private",platform="qq",
                                   matched_groups={"scope":"概览"}),
            await plugin.cmd_create_now(user_id="90001",group_id="",stream_id="admin-private",platform="qq"),
        ]
        for result in admin_results:self.assertTrue(result[0])
        self.assertFalse(any("只有私聊管理员" in text for text in self._sent_texts(ctx)))

    # P2-12：/麦麦立即创作 返回中文映射而非裸 JSON。
    async def test_create_now_returns_chinese_instead_of_json(self):
        plugin,ctx=await self._build_plugin(MaiLifeSettings(),[UserProfile(user_id="10001",role="owner")])
        common={"user_id":"10001","group_id":"","stream_id":"stream-10001","platform":"qq"}
        result=await plugin.cmd_create_now(**common)
        self.assertTrue(result[0])
        text=self._sent_texts(ctx)[-1]
        self.assertIn("书柜创作未启用或未确认明文存储",text)
        self.assertNotIn('"status"',text); self.assertNotIn("创作结果：{",text)

        config=MaiLifeSettings()
        config.creation.enabled=True; config.creation.plaintext_storage_acknowledged=True
        plugin,ctx=await self._build_plugin(config,[UserProfile(user_id="10001",role="owner")])
        now=time.time()
        await self.store.add_creation_inspiration({
            "id":"inspiration-force-1","source_kind":"life","source_ref":"force",
            "prompt_digest":"最近生活里留下的一点安静灵感","privacy_ceiling":"public",
            "score":0.8,"created_at":now,"expires_at":now+86400,
        })
        result=await plugin.cmd_create_now(**common)
        self.assertTrue(result[0])
        text=self._sent_texts(ctx)[-1]
        self.assertIn("已创作并归档《",text); self.assertIn("（公开）",text)
        self.assertNotIn("archived",text); self.assertNotIn('"status"',text)

    # P2-13：/麦麦休息测试 输出中文相位、日程类型与 HH:MM 缓冲时间。
    async def test_rest_test_translates_phase_kind_and_grace_time(self):
        config=MaiLifeSettings.model_validate({"plugin":{"admin_user_ids":["90001"]}})
        plugin,ctx=await self._build_plugin(config,[])
        today=plugin._env.now().date().isoformat()
        await self.store.replace_framework(today,[{
            "id":"f1","day":today,"start_minute":0,"end_minute":1440,"kind":"meal",
            "summary":"安安静静吃顿饭","location":"厨房","energy_load":-1,"shareability":0.4,
        }])
        grace=plugin._env.now().replace(minute=35,second=0,microsecond=0)
        await self.store.save_sleep_runtime({
            "phase":"light_sleep","started_at":time.time()-600,"awake_grace_until":grace.timestamp(),
            "woken_count":0,"last_event":"",
        })
        common={"user_id":"90001","group_id":"","stream_id":"admin-private","platform":"qq"}
        result=await plugin.cmd_rest_test(**common)
        self.assertTrue(result[0])
        text=self._sent_texts(ctx)[-1]
        self.assertNotIn("light_sleep",text); self.assertNotIn("awake",text)
        self.assertNotIn("meal",text)
        self.assertIn("浅睡",text); self.assertIn("用餐",text)
        self.assertRegex(text,r"醒来缓冲至：\d{2}:\d{2}")
        self.assertIn(grace.strftime("%H:%M"),text)

    # P2-14：命令形态但未命中自家命令的手滑输入走菜单兜底；正常命令不误入。
    async def test_unrecognized_mai_commands_fall_back_to_menu(self):
        plugin,ctx=await self._build_plugin(MaiLifeSettings(),[UserProfile(user_id="10001",role="owner")])
        for index,text in enumerate(("/麦麦 ","/mai ","/麦麦阅读")):
            with self.subTest(text=text):
                before=len(ctx.send.texts)
                result=await plugin.on_receive(message=command_message(text,message_id=f"typo-{index}"))
                self.assertEqual(result,{"action":"abort"})
                self.assertGreater(len(ctx.send.texts),before)
                self.assertIn("未识别的指令，已为你显示指令菜单。",self._sent_texts(ctx)[-1])
        # 正常命令不进入兜底：继续放行交给 Host 派发，且不触发菜单。
        before=len(ctx.send.texts)
        result=await plugin.on_receive(message=command_message("/麦麦状态",message_id="normal-1"))
        self.assertEqual(result,{"action":"continue"})
        self.assertEqual(len(ctx.send.texts),before)
        # 未识别子命令仍由 cmd_menu 自身处理（命中 pattern），不走兜底。
        self.assertFalse(plugin._is_unmatched_mai_command(command_message("/mai xyz")))
        self.assertFalse(plugin._is_unmatched_mai_command(command_message("今天天气不错")))

    # P3-27：重复添加同一日期给出“已存在”反馈。
    async def test_duplicate_date_add_reports_existing(self):
        plugin,ctx=await self._build_plugin(MaiLifeSettings(),[UserProfile(user_id="10001",role="owner")])
        common={"user_id":"10001","group_id":"","stream_id":"stream-10001","platform":"qq"}
        first=await plugin.cmd_date_add(**common,matched_groups={
            "event_date":"2026-08-01","event_name":"妈妈生日"})
        self.assertTrue(first[0]); self.assertIn("已记录",self._sent_texts(ctx)[-1])
        second=await plugin.cmd_date_add(**common,matched_groups={
            "event_date":"2026-08-01","event_name":"妈妈生日"})
        self.assertTrue(second[0])
        text=self._sent_texts(ctx)[-1]
        self.assertIn("该日期已存在：2026-08-01 妈妈生日",text)
        dates=await self.store.list_important_dates("10001")
        self.assertEqual(len(dates),1)

    # P3-29：/麦麦配置 用户数口径区分已配置与启用。
    async def test_config_reports_enabled_user_count(self):
        config=MaiLifeSettings()
        config.users.profiles=[UserProfile(user_id="10001",role="owner"),
                               UserProfile(user_id="10002",role="friend",enabled=False)]
        plugin,ctx=await self._build_plugin(config,list(config.users.profiles))
        common={"user_id":"10001","group_id":"","stream_id":"stream-10001","platform":"qq"}
        result=await plugin.cmd_config(**common)
        self.assertTrue(result[0])
        self.assertIn("配置用户：已配置 2 个（启用 1 个）",self._sent_texts(ctx)[-1])

    # P3-16：WebUI 配置保存不得重置主动/转述任务注册表。
    async def test_config_update_keeps_active_task_registry(self):
        config=MaiLifeSettings(); config.plugin.enabled=False  # 避免热更新末尾重启后台循环
        plugin,_ctx=await self._build_plugin(config,[UserProfile(user_id="10001",role="owner")])
        reset_calls:list[str]=[]
        original_reset=plugin._active_tasks.reset
        async def spy_reset()->None:
            reset_calls.append("reset"); await original_reset()
        plugin._active_tasks.reset=spy_reset  # type: ignore[method-assign]
        now=time.time()
        marker=PluginTaskMarker(task_id=HOST_TASK_PREFIX+"900",plugin_id=PLUGIN_ID,metadata={})
        activated=await plugin._active_tasks.activate(
            "stream-10001",marker,kind="proactive",
            record={"id":"event-1","status":"pending","expires_at":now+600,"created_at":now,
                    "opportunity_id":"op-1"},
            now=now,
        )
        self.assertIsNotNone(activated)
        await plugin._apply_config_update("self",{},"1.14.4")
        self.assertEqual(reset_calls,[])
        current=await plugin._active_tasks.current("stream-10001",time.time())
        self.assertIsNotNone(current); self.assertEqual(current.task_id,HOST_TASK_PREFIX+"900")

    # P3-30：菜单 PNG 量化后体积低于 150KB。
    def test_menu_png_is_quantized_below_150kb(self):
        renderer=MaiLifeMenuRenderer()
        if not renderer.available or not renderer.regular_font_path:
            self.skipTest("当前环境没有 Pillow 或可用中文字体")
        png=renderer.render("麦麦生活 · 指令中心",COMMAND_SECTIONS,version="1.14.4")
        self.assertGreater(len(png),10_000); self.assertLess(len(png),150_000)
        Path(self.tmp.name,"menu_v1144.png").write_bytes(png)
        from PIL import Image
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format,"PNG"); self.assertEqual(image.width,renderer.WIDTH)
            self.assertGreaterEqual(image.height,900)


class AdminTextFixTests(unittest.IsolatedAsyncioTestCase):
    """P3-24/P3-31：管理摘要空数据与 Playwright 来源展示。"""

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.now=datetime(2026,7,13,20,0,tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_tokens_and_search_scopes_show_placeholder_without_data(self):
        service=AdminService(self.store,MaiLifeSettings())
        text=await service.format_text("tokens",self.now)
        self.assertIn("今日暂无模型调用。",text)
        self.assertNotIn("今日模型 Token 聚合",text); self.assertNotIn("不计作 Token",text)
        search_text=await service.format_text("search",self.now)
        self.assertIn("暂无搜索历史。",search_text)
        self.assertNotIn("最近本地搜索历史",search_text)

    async def test_sources_scope_labels_playwright_without_key(self):
        config=MaiLifeSettings()
        config.search_api.providers=[SearchProviderProfile(enabled=True,provider_type="playwright")]
        service=AdminService(self.store,config)
        text=await service.format_text("sources",self.now)
        self.assertIn("浏览器（无需 Key）",text); self.assertNotIn("Key 0",text)


class RecallNoticeFixTests(unittest.TestCase):
    """P3-13：payload 为 JSON 字符串的撤回通知不再被静默忽略。"""

    def test_json_string_payload_is_parsed(self):
        payload=json.dumps({"message_id":"m-json","user_id":"1","operator_id":"1"})
        message={
            "message_id":"notice-json","session_id":"private-1","platform":"qq","is_notify":True,
            "message_info":{"user_info":{"user_id":"1"},
                            "additional_config":{"napcat_notice_type":"friend_recall",
                                                 "napcat_notice_payload":payload}},
            "raw_message":[],
        }
        notice=recall_notice(message)
        self.assertEqual(notice["notice_type"],"friend_recall")
        self.assertEqual(notice["recalled_message_id"],"m-json")
        self.assertEqual(notice["adapter"],"napcat")

    def test_invalid_json_string_payload_returns_empty(self):
        message={
            "message_id":"notice-bad","session_id":"private-1","platform":"qq","is_notify":True,
            "message_info":{"user_info":{"user_id":"1"},
                            "additional_config":{"napcat_notice_type":"friend_recall",
                                                 "napcat_notice_payload":"not-a-json"}},
            "raw_message":[],
        }
        self.assertEqual(recall_notice(message),{})


if __name__=="__main__":unittest.main()
