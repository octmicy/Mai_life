from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Mai_life.config import PLUGIN_VERSION,MaiLifeSettings,SocialGroupProfile,UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.messaging.prompt_builder import PromptBuilder, relationship_stage
from Mai_life.plugin import MaiLifePlugin
from Mai_life.tests.support import TZ, build_plugin

# webui_* 只读 @API 契约（后端/前端/测试三方共同遵守），名称逐字一致。
# v1.14.12 全量重设计后共 11 个：4 个既有页面数据源扩展 + 7 个新增页面数据源。
WEBUI_API_NAMES=("webui_status_overview","webui_status_history",
                 "webui_information_digest","webui_bookshelf_overview",
                 "webui_schedule","webui_users","webui_memories","webui_dates",
                 "webui_usage","webui_config_summary","webui_admin_summary")


class ContractTests(unittest.TestCase):
    def test_playwright_release_contract_is_synchronized(self):
        root=Path(__file__).parents[1]
        manifest=json.loads((root/"_manifest.json").read_text(encoding="utf-8-sig"))
        requirements=(root/"requirements.txt").read_text(encoding="utf-8")
        readme=(root/"README.md").read_text(encoding="utf-8")
        self.assertEqual(manifest["version"],"1.23.5")
        self.assertIn("playwright>=1.49,<2",requirements)
        self.assertIn("python -m playwright install chromium",readme)
        self.assertIn("Playwright/Bing",readme)

    def test_version_constant_is_unified_around_1_14_5(self):
        from Mai_life.config import PLUGIN_VERSION
        from Mai_life.information.http_client import _USER_AGENT
        root=Path(__file__).parents[1]
        self.assertEqual(PLUGIN_VERSION,"1.23.5")
        self.assertIn(f"Mai_life/{PLUGIN_VERSION}",_USER_AGENT)
        self.assertNotIn("1.9.2",_USER_AGENT)
        # 管理概览与入口的显示版本必须统一为 1.23.5，不允许残留旧版本号。
        for relative in ("plugin.py","management/admin_service.py",
                         "information/http_client.py","core/environment.py","config.py",
                         "README.md","_manifest.json"):
            text=(root/relative).read_text(encoding="utf-8-sig",errors="replace")
            self.assertNotIn("1.9.2",text,msg=f"{relative} 仍残留旧版本号 1.9.2")
        # 管理概览通过 PLUGIN_VERSION 常量渲染版本，避免出现多处硬编码。
        admin_text=(root/"management/admin_service.py").read_text(encoding="utf-8-sig",errors="replace")
        self.assertIn("PLUGIN_VERSION",admin_text)

    def test_default_toml_validates(self):
        root=Path(__file__).parents[1]
        # 仓库只提交模板 config.toml.example；实际 config.toml 由 Runner 生成且不入库。
        config=MaiLifeSettings.model_validate(tomllib.loads((root/"config.toml.example").read_text(encoding="utf-8-sig")))
        self.assertEqual(config.plugin.config_version,"1.11.0")
        self.assertEqual(config.environment.timezone,"Asia/Shanghai")
        self.assertEqual(config.users.profiles[0].daily_proactive_max,1)
        self.assertFalse(config.rest_gate.enabled)
        self.assertTrue(config.debounce.enabled)
        self.assertEqual(config.debounce.merge_separator,"\n")
        self.assertTrue(config.debounce.ignore_empty_message)
        self.assertTrue(config.debounce.log_detail)
        self.assertTrue(config.recall.enabled)
        self.assertFalse(config.recall.cache_summary_enabled)
        self.assertTrue(config.search_api.tool_enabled)
        self.assertEqual(config.context.prompt_max_chars,4000)
        self.assertEqual(config.models.scene_detail_task,"")
        self.assertTrue(config.memory.enabled)
        self.assertFalse(config.memory.date_model_analysis_enabled)
        self.assertFalse(config.information.enabled); self.assertFalse(config.news.enabled); self.assertFalse(config.search.enabled)
        self.assertEqual(len(config.search_api.providers),1); self.assertEqual(config.search_api.providers[0].provider_type,"playwright")
        self.assertFalse(config.debounce.group_enabled)
        self.assertFalse(config.social.enabled)
        self.assertFalse(config.users.profiles[0].group_to_private_enabled)
        self.assertFalse(config.creation.enabled); self.assertFalse(config.creation.plaintext_storage_acknowledged)
        # WebUI 的 TOML 写回不支持 None，默认配置必须全部可序列化。
        def assert_no_none(value):
            if isinstance(value, dict):
                for child in value.values():
                    assert_no_none(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_none(child)
            else:
                self.assertIsNotNone(value)
        assert_no_none(config.model_dump(mode="python"))

    def test_weather_configuration_uses_city_only(self):
        schema=MaiLifePlugin.build_config_schema()
        fields=schema["sections"]["environment"]["fields"]
        self.assertIn("city",fields)
        self.assertNotIn("latitude",fields)
        self.assertNotIn("longitude",fields)

    def test_sdk_components_registered(self):
        plugin=MaiLifePlugin(); components=plugin.get_components()
        names={str(item.get("name") or "") for item in components}
        for expected in {"/麦麦","/麦麦帮助","/麦麦状态","/麦麦配置","/麦麦休息测试","/麦麦添加日期","/麦麦删除日期","/麦麦确认日期","/麦麦立即创作","/麦麦转述","/麦麦重生日程","mai_life_web_search","get_life_state","get_current_scene","admin_snapshot","mai_life_management",*WEBUI_API_NAMES}:
            self.assertIn(expected,names)
        self.assertFalse(any(name.startswith("/mai") for name in names))
        self.assertNotIn("/麦麦技能",names)
        webui_apis={str(item.get("name") or ""):item for item in components if str(item.get("name") or "") in WEBUI_API_NAMES}
        self.assertEqual(set(webui_apis),set(WEBUI_API_NAMES))
        for name,component in webui_apis.items():
            self.assertEqual(component.get("type"),"API",name)
            self.assertFalse((component.get("metadata") or {}).get("public"),name)
        hooks={str((item.get("metadata") or {}).get("hook") or "") for item in components if item.get("type")=="HOOK_HANDLER"}
        self.assertIn("chat.receive.before_process",hooks)
        self.assertIn("maisaka.replyer.after_response",hooks)
        self.assertIn("send_service.after_send",hooks)
        self.assertIn("send_service.before_send",hooks)
        search_tool=next(item for item in components if item.get("name")=="mai_life_web_search")
        self.assertEqual(search_tool.get("type"),"TOOL")
        tool_parameters=(search_tool.get("metadata") or {}).get("parameters") or []
        self.assertEqual({item.get("name") for item in tool_parameters},{"query","result_limit","freshness"})
        self.assertGreaterEqual(len(components),20)
        private_api=next(item for item in components if item.get("name")=="admin_snapshot")
        self.assertFalse((private_api.get("metadata") or {}).get("public"))
        home_card=next(item for item in components if item.get("name")=="mai_life_management")
        self.assertEqual(home_card.get("type"),"HOME_CARD")
        self.assertTrue(str((home_card.get("metadata") or {}).get("link_url")).startswith("/plugin-config"))

    def test_all_webui_fields_have_translated_labels(self):
        schema=MaiLifePlugin.build_config_schema(plugin_id="maibot-community.mai-life",plugin_name="麦生活")
        self.assertTrue(schema.get("sections"))
        for section_name,section in schema["sections"].items():
            self.assertNotEqual(section.get("title"),section_name)
            for field_name,field in section.get("fields",{}).items():
                self.assertNotEqual(field.get("label"),field_name)
                self.assertTrue(field.get("hint") or field.get("description"))
                for item_name,item in (field.get("item_fields") or {}).items():
                    self.assertNotEqual(item.get("label"),item_name)
                    self.assertTrue((item.get("i18n") or {}).get("zh_CN"))

    def test_prompt_is_partitioned(self):
        text=PromptBuilder().planner(
            {"energy":50,"hunger":40,"mood_valence":0.1,"mood_arousal":0.5,"health_note":"状态正常","sleep_phase":"awake","current_location":"家里","current_activity":"切番茄","body_cycle":"未启用"},
            {"description":"小雨","temperature":22},
            {"current":{"summary":"做晚饭","location":"厨房"},"next":{"summary":"休息","location":"客厅"}},
            {"temperature":45}, {}, [],
        )
        self.assertIn("【麦麦内在生活状态】",text)
        self.assertIn("【独立环境背景】",text)
        self.assertIn("只有“当前真实场景”",text)
        self.assertEqual(relationship_stage(45),"熟悉")

    def test_planner_context_is_injected_into_messages_contract(self):
        messages=[{"role":"system","content":"原系统提示"},{"role":"user","content":"当前消息"}]
        result=MaiLifePlugin._planner_messages_with_context(messages,"\n【麦生活】背景")
        self.assertIn("【麦生活】",result[0]["content"])
        self.assertEqual(result[1],messages[1]); self.assertNotIn("【麦生活】",messages[0]["content"])

    def test_only_one_owner_is_allowed(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            MaiLifeSettings.model_validate({"users":{"profiles":[
                {"user_id":"1","role":"owner"},{"user_id":"2","role":"owner"},
            ]}})
        with self.assertRaises(ValidationError):
            MaiLifeSettings.model_validate({"users":{"profiles":[
                {"user_id":"1","role":"owner"},{"user_id":"1","role":"friend"},
            ]}})

    def test_old_config_version_is_normalized_without_nulls(self):
        config=MaiLifeSettings.model_validate({"plugin":{"config_version":"1.0.2"}})
        self.assertEqual(config.plugin.config_version,"1.11.0")
        self.assertTrue(config.debounce.enabled)

    def test_legacy_negative_user_quota_becomes_explicit_role_default(self):
        config=MaiLifeSettings.model_validate({"users":{"profiles":[
            {"user_id":"10001","role":"owner","daily_proactive_max":-1},
            {"user_id":"10002","role":"friend","daily_proactive_max":-1},
        ]}})
        self.assertEqual([item.daily_proactive_max for item in config.users.profiles],[2,1])

    def test_invalid_rest_windows_restore_each_field_default(self):
        config=MaiLifeSettings.model_validate({"rest_gate":{
            "night_start":"bad","night_end":"8:00","nap_start":"25:00","nap_end":"",
        }})
        self.assertEqual((config.rest_gate.night_start,config.rest_gate.night_end),("22:30","08:00"))
        self.assertEqual((config.rest_gate.nap_start,config.rest_gate.nap_end),("12:00","14:30"))

    def test_friend_memory_prompt_excludes_private_diary(self):
        text=PromptBuilder().replyer(
            {"energy":60,"mood_valence":0,"current_location":"家里","current_activity":"看书"},{"description":"晴"},
            {"current":{"summary":"休息","location":"家里"}},{"temperature":50,"role":"friend"},[],
            memory={"diary":{},"upcoming_dates":[{"name":"考试","date":"2026-07-20","days":7}]},
        )
        self.assertIn("当前关系无权读取私人日记",text)
        self.assertIn("考试",text); self.assertNotIn("技能",text)

    def test_identity_fields_only_accept_qq_numbers(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            MaiLifeSettings.model_validate({"users":{"profiles":[{"user_id":"可修改昵称"}]}})
        with self.assertRaises(ValidationError):
            MaiLifeSettings.model_validate({"social":{"groups":[{"group_id":"群别名"}]}})
        self.assertNotIn("display_name",UserProfile.model_fields)
        self.assertNotIn("alias",SocialGroupProfile.model_fields)

    def test_friend_prompt_contains_explicit_boundary(self):
        text=PromptBuilder().replyer(
            {"energy":60,"mood_valence":0,"current_location":"家里","current_activity":"看书"},{"description":"晴"},
            {"current":{"summary":"休息","location":"家里"}},{"temperature":50,"role":"friend"},[],
            {"time_period":"晚上","day_type":"工作日","media":["text"]},{"unresolved_topics":["插件测试"]},"提出问题",
        )
        self.assertIn("普通朋友",text)
        self.assertIn("不得对这位用户使用主人/恋人称呼",text)


class WebuiApiContractTests(unittest.IsolatedAsyncioTestCase):
    """webui_* 只读 @API 契约：响应键逐字一致、列表限量、不泄露 URL/用户身份/Key。"""

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.now=datetime(2026,10,7,14,30,tzinfo=timezone(timedelta(hours=8)))
        self.plugin=await build_plugin(self.store,MaiLifeSettings(),now=self.now)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_webui_apis_expose_private_methods_with_matching_names(self):
        components={str(item.get("name") or ""):item for item in self.plugin.get_components()}
        for name in WEBUI_API_NAMES:
            self.assertEqual(components[name].get("type"),"API",name)
            self.assertFalse((components[name].get("metadata") or {}).get("public"),name)
            self.assertTrue(callable(getattr(self.plugin,name,None)),name)

    async def test_webui_apis_return_failure_shape_when_not_ready(self):
        bare=MaiLifePlugin()
        for name in WEBUI_API_NAMES:
            self.assertEqual(await getattr(bare,name)(),{"success":False,"error":"not_ready"},name)

    async def test_webui_declaration_binds_only_registered_apis(self):
        """webui.json 的每个查询都必须绑定本插件已注册的 @API，且页面/组件不越协议上限。"""
        root=Path(__file__).parents[1]
        schema_path=Path(os.environ.get("MAIBOT_ROOT") or "")/"src/plugin_runtime/webui_schema.py"
        if not schema_path.exists():self.skipTest("未提供 MAIBOT_ROOT，跳过宿主 schema 校验")
        spec=importlib.util.spec_from_file_location("webui_schema",schema_path)
        schema=importlib.util.module_from_spec(spec); spec.loader.exec_module(schema)
        extension=schema.WebUIExtension.model_validate(json.loads((root/"webui.json").read_text(encoding="utf-8-sig")))
        registered={str(item.get("name") or "") for item in self.plugin.get_components()
                    if item.get("type")=="API"}
        self.assertLessEqual(len(extension.pages),20)
        for page in extension.pages:
            self.assertLessEqual(len(page.queries),10)
            self.assertEqual(page.actions,{},"WebUI 只做只读查询，不声明 actions")
            for alias,binding in page.queries.items():
                self.assertIn(binding.api,registered,f"{page.id}.{alias} 绑定未注册 API {binding.api}")
                self.assertEqual(binding.version,"1")
            stack=[(node,1) for node in page.content]; count=0
            while stack:
                node,depth=stack.pop(); count+=1
                self.assertLessEqual(depth,8); self.assertLessEqual(count,200)
                if node.type=="table":self.assertLessEqual(len(node.columns),50)
                if node.type=="chart":self.assertTrue(node.value.field)
                stack.extend((child,depth+1) for child in node.children)

    async def test_webui_status_overview_keys_are_literal(self):
        data=await self.plugin.webui_status_overview()
        self.assertEqual(set(data),{"generated_at","bot_name","version","state","schedule","weather",
                                    "day_type","upcoming_festival","sleep","linkage","services","recall"})
        self.assertEqual(set(data["state"]),{"energy","hunger","mood_valence","mood_arousal","mood_label",
                                            "sleep_phase_zh","current_activity","current_location","health_note"})
        self.assertEqual(set(data["schedule"]),{"current","next","next_summary","scene"})
        self.assertEqual(set(data["sleep"]),{"phase_zh","kind_zh","grace_until","woken_count",
                                             "last_event","defer_until","started_at"})
        self.assertEqual(set(data["linkage"]),{"drawpic_enabled","drawpic_detected"})
        self.assertEqual(set(data["services"]),{"llm_health","available_tasks","background_tasks","active_bursts",
                                                "active_groups","users_summary","diary_last_day","narrative_note",
                                                "news_count","exploration_count","search_history_count","last_failure",
                                                "group_observations","archived_today","pending_inspirations"})
        self.assertEqual(set(data["recall"]),{"enabled","cache_enabled","latest"})
        self.assertEqual(data["bot_name"],"麦麦")
        self.assertIsInstance(data["weather"],str)
        # 无日程/无撤回摘要时返回键齐全的空字典（渲染器途经 null 会抛错整页）。
        self.assertEqual(set(data["schedule"]["current"]),
                         {"kind", "kind_zh", "summary", "location", "start", "end", "window"})
        self.assertEqual(data["schedule"]["current"]["kind_zh"], "未知")
        self.assertEqual(set(data["recall"]["latest"]), {"user_id", "summary", "media_types", "age"})
        day=self.now.date().isoformat()
        await self.store.replace_framework(day,[{"id":"n1","day":day,"start_minute":0,"end_minute":1439,
            "kind":"leisure","summary":"看书","location":"书房","energy_load":0.2,"shareability":0.5}])
        data=await self.plugin.webui_status_overview()
        self.assertEqual(set(data["schedule"]["current"]),{"kind","kind_zh","summary","location","start","end","window"})
        self.assertEqual(data["schedule"]["current"]["summary"],"看书")
        self.assertEqual(data["schedule"]["current"]["kind_zh"],"闲暇")
        self.assertEqual(data["schedule"]["current"]["window"],"00:00-23:59")
        self.assertEqual(data["schedule"]["next_summary"],"")
        self.assertEqual(set(data["schedule"]["next"]), {"kind_zh", "summary", "start"})  # 无下一节点为空字典

    async def test_webui_status_overview_rest_and_recall_runtime(self):
        now_ts=self.now.timestamp()
        await self.store.save_sleep_runtime({"phase":"deep_sleep","started_at":now_ts-3600,
            "awake_grace_until":now_ts+600,"woken_count":2,"last_event":"叫醒后重新入睡","sleep_defer_until":0})
        await self.store.record_recall_event(session_id="s1",recalled_message_id="m1",user_id="10001",
            operator_id="10001",group_id="",notice_type="private_recall",source_adapter="napcat",
            summary="刚撤回的那句话",media=["image"],now=now_ts-120,
            expires_at=now_ts+3600,summary_expires_at=now_ts+600)
        data=await self.plugin.webui_status_overview()
        self.assertEqual(data["sleep"]["phase_zh"],"深睡")
        self.assertEqual(data["sleep"]["woken_count"],2)
        self.assertEqual(data["sleep"]["kind_zh"],"休息")  # 无框架且窗内无段时按 rest 兜底
        self.assertEqual(data["recall"]["enabled"],"开启")  # 开关中文化，网页不显示裸 true/false
        self.assertEqual(data["recall"]["latest"]["user_id"],"10001")
        self.assertEqual(data["recall"]["latest"]["media_types"],["image"])
        self.assertEqual(data["recall"]["latest"]["age"],"2 分钟前")

    async def test_webui_status_history_is_bounded_ascending_and_literal(self):
        self.assertEqual(await self.plugin.webui_status_history(),{"points":[]})  # 无数据时空数组
        base=self.now.timestamp()
        for offset in (100*3600,2*3600,3600):  # 乱序写入；>72 小时的快照必须被排除
            await self.store.save_state_snapshot(base-offset,{"energy":50,"hunger":10,
                "mood_valence":0.2,"mood_arousal":0.5,"sleep_phase":"awake","current_activity":"看书"})
        points=(await self.plugin.webui_status_history())["points"]
        self.assertEqual(len(points),2)
        self.assertEqual([p["ts"] for p in points],sorted(p["ts"] for p in points))  # 按时间升序
        # 类别轴等宽分格：网格点间隔必须恒定（2 小时），稀疏快照不得让时间轴失真。
        self.assertEqual(round(points[1]["ts"]-points[0]["ts"]),7200)
        for point in points:
            self.assertEqual(set(point),{"ts","time","energy","hunger","mood_valence",
                                         "mood_arousal","sleep_phase"})
            for key in ("energy","hunger","mood_valence","mood_arousal"):
                self.assertEqual(round(float(point[key]),1),float(point[key]))  # 一位小数
            # time 是图表 x 轴直接渲染的字段：必须是 MM-DD HH:MM 字符串，不能是裸 epoch。
            self.assertRegex(point["time"], r"^\d{2}-\d{2} \d{2}:\d{2}$")
            expected=datetime.fromtimestamp(point["ts"],tz=self.now.tzinfo).strftime("%m-%d %H:%M")
            self.assertEqual(point["time"], expected)

    async def test_webui_information_digest_full_text_for_owner(self):
        now=self.now.timestamp()
        for index in range(6):  # 6 条新闻全量返回（上限 10 条）
            await self.store.upsert_news_item({"id":f"n{index}","source_id":"s1","title":f"新闻{index}",
                "url":f"https://example.com/{index}","summary":f"摘要{index}","content":"正文",
                "published_at":now-index*60,"fetched_at":now-index*60,"content_hash":f"h{index}",
                "expires_at":now+86400})
        await self.store.save_exploration_note({"id":"e1","topic":"量子计算","query":"量子计算入门",
            "summary":"量子计算利用量子比特。","source_urls":["https://example.com/explore"],
            "created_at":now-300,"relevance_score":0.5,"relevance_reason":"","opportunity_id":"",
            "expires_at":now+86400})
        for index in range(9):  # 9 条搜索历史全量返回（上限 20 条）
            await self.store.record_search_history(created_at=now-index*60,operation="proactive",
                query=f"问题{index}",provider_type="playwright",success=index%2==0,result_count=3,
                error_class="",duration_ms=120.0,results=[],expires_at=now+86400)
        data=await self.plugin.webui_information_digest()
        self.assertEqual(set(data),{"status","news","news_full_text",
                                    "explorations","explorations_full_text","search_history"})
        self.assertEqual(set(data["status"]),{"information_enabled","news_enabled","search_enabled","sources",
                                              "recent_news","recent_explorations","recent_search_history"})
        self.assertEqual(len(data["news"]),6)
        self.assertEqual(len(data["explorations"]),1)
        self.assertEqual(len(data["search_history"]),9)
        self.assertTrue(all(set(item)=={"title","summary","url","content","age","score"} for item in data["news"]))
        self.assertTrue(all(set(item)=={"topic","query","summary","sources_text","age","score"} for item in data["explorations"]))
        self.assertTrue(all(set(item)=={"time","operation","query","provider_type","success",
                                        "result_count","error_class","results_digest","duration_ms"} for item in data["search_history"]))
        blob=json.dumps(data,ensure_ascii=False)
        self.assertIn("example.com",blob)      # 主人后台全量展示：URL 与正文直接透出
        self.assertIn("https://",blob)
        self.assertNotIn("user_id",blob)       # 不含用户身份

    async def test_webui_bookshelf_overview_includes_private_for_owner(self):
        now=self.now.timestamp()
        await self.store.create_bookshelf_document({"id":"w1","doc_type":"work","title":"公开短诗",
            "privacy":"public","status":"archived","summary":"一首短诗。","created_at":now})
        await self.store.create_bookshelf_document({"id":"w2","doc_type":"work","title":"私人草稿",
            "privacy":"private","status":"archived","summary":"不对外。","created_at":now})
        await self.store.add_bookshelf_revision("w1","body","诗歌正文","fallback",now,status="archived")
        await self.store.save_diary(self.now.date().isoformat(),"七月的日记","日记正文","平静","digest",now)
        data=await self.plugin.webui_bookshelf_overview()
        self.assertEqual(set(data),{"counts","creation","books","books_full_text","runs","latest_work"})
        self.assertEqual(set(data["counts"]),{"documents","private_documents","creation_runs"})
        self.assertEqual(set(data["creation"]),{"enabled","plaintext_acknowledged","archived_today",
                                                "pending_inspirations","daily_max"})
        self.assertEqual(set(data["books"][0]),{"doc_type","title","privacy","status","updated_age","summary","content"})
        if data["runs"]:
            self.assertEqual(set(data["runs"][0]),{"document_id","title","status","started_at"})
        # 主人专属网页：私人文档一并展示，latest_work 仍取有正文的那篇。
        titles=[book["title"] for book in data["books"]]
        self.assertIn("私人草稿",titles)
        self.assertEqual(data["latest_work"]["title"],"公开短诗")
        self.assertEqual(data["counts"]["private_documents"],2)  # 私人草稿 + 私人日记
        self.assertEqual(data["creation"]["daily_max"],1)

    async def test_webui_schedule_nodes_and_tomorrow(self):
        day=self.now.date().isoformat()
        tomorrow=(self.now+timedelta(days=1)).date().isoformat()
        await self.store.replace_framework(day,[
            {"id":"n1","day":day,"start_minute":0,"end_minute":600,"kind":"sleep","summary":"睡觉",
             "location":"卧室","energy_load":1,"shareability":0.05},
            {"id":"n2","day":day,"start_minute":600,"end_minute":1200,"kind":"meal","summary":"吃早饭",
             "location":"厨房","energy_load":-1,"shareability":0.25}])
        await self.store.replace_framework(tomorrow,[
            {"id":"t1","day":tomorrow,"start_minute":600,"end_minute":660,"kind":"study","summary":"写作业",
             "location":"书桌前","energy_load":-3,"shareability":0.2}])
        await self.store.save_scene("n2","边吃边看窗外",{"energy":1},[])
        data=await self.plugin.webui_schedule()
        self.assertEqual(set(data),{"day","generated_at","current","next","scene","nodes","tomorrow"})
        self.assertEqual(data["day"],day); self.assertEqual(data["tomorrow"]["day"],tomorrow)
        self.assertEqual(set(data["nodes"][1]),{"start","end","kind_zh","summary","location","scene",
                                                "energy_load","shareability"})
        # 场景按 framework_id 关联，不按 start_minute 合并。
        self.assertEqual(data["nodes"][1]["scene"],"边吃边看窗外")
        self.assertEqual(data["nodes"][0]["scene"],"")
        self.assertEqual(data["nodes"][1]["kind_zh"],"用餐")
        self.assertEqual(set(data["tomorrow"]["nodes"][0]),{"start","end","kind_zh","summary","location"})
        self.assertIsNotNone(data["current"])  # 14:30 落在 n2 段内

    async def test_webui_users_counts_and_recent(self):
        from Mai_life.config import UserProfile
        await self.store.sync_users([UserProfile(user_id="10001",role="owner",enabled=True),
                                     UserProfile(user_id="10002",role="friend",enabled=False)])
        await self.store.update_user_display_name("10001","主人")
        await self.store.record_interaction("10001","今晚吃了火锅",time.time(),20)
        data=await self.plugin.webui_users()
        self.assertEqual(set(data),{"counts","users"})
        self.assertEqual(set(data["counts"]),{"total","enabled","disabled","owners","proactive_enabled"})
        self.assertEqual(set(data["users"][0]),{"user_id","display_name","role","enabled","temperature","stage",
                                                "proactive_enabled","proactive_count","daily_proactive_max",
                                                "last_message_age","last_proactive_age","last_relation_day","recent"})
        self.assertEqual((data["counts"]["total"],data["counts"]["enabled"],
                          data["counts"]["disabled"],data["counts"]["owners"]),(2,1,1,1))
        owner=next(item for item in data["users"] if item["user_id"]=="10001")
        self.assertEqual(owner["stage"],"认识")
        self.assertEqual(owner["recent"],"今晚吃了火锅")

    async def test_webui_memories_lists_diaries_and_dreams(self):
        now_ts=self.now.timestamp()
        await self.store.save_diary("2026-10-06","昨天","日记正文"*100,"平静","digest",now_ts)
        await self.store.save_diary("2026-10-05","前天","更早的日记","开心","digest",now_ts-86400)
        await self.store.add_dream("梦见在海边",0.1,2.0,now_ts-7200,["片段一","片段二"],created_at=now_ts-3600)
        await self.store.add_dream("梦见下雨",-0.1,-1.0,now_ts-7200,[],created_at=now_ts-86400*3)
        data=await self.plugin.webui_memories()
        self.assertEqual(set(data),{"generated_at","last_diary_day","diary_count","dream_count",
                                    "latest_diary","diaries","diaries_full_text","latest_dream","dreams"})
        self.assertEqual(data["last_diary_day"],"2026-10-05")  # 最后落库的那一篇
        self.assertEqual(data["diary_count"],2); self.assertEqual(data["dream_count"],2)  # 两条都在近 30 天窗口内
        self.assertEqual(set(data["latest_diary"]),{"day","title","content","mood_summary"})
        self.assertEqual(data["latest_diary"]["content"],"日记正文"*100)  # 正文全量透出
        self.assertTrue(all(set(item)=={"day","title","mood_summary","content"} for item in data["diaries"]))
        self.assertEqual(set(data["latest_dream"]),{"created_at","content","mood_delta","energy_delta","fragments"})
        self.assertEqual(data["latest_dream"]["fragments"],["片段一","片段二"])
        self.assertTrue(all(set(item)=={"created_at","content","mood_delta","energy_delta"} for item in data["dreams"]))

    async def test_webui_dates_occurrence_and_countdown(self):
        await self.store.add_important_date("10001","生日","2000-10-10","annual","local_rule",self.now.timestamp())
        await self.store.add_important_date("10001","考试","2026-10-09","none","local_rule",self.now.timestamp())
        await self.store.add_date_candidate("10001","见面","下周","",0.45,"提到见面",self.now.timestamp())
        data=await self.plugin.webui_dates()
        self.assertEqual(set(data),{"counts","dates","candidates"})
        self.assertEqual(set(data["counts"]),{"dates","candidates","within_30d"})
        self.assertEqual(set(data["dates"][0]),{"id","user_id","user_name","event_name","event_date",
                                                "recurrence","occurrence","countdown","days_left"})
        self.assertEqual(set(data["candidates"][0]),{"id","user_id","user_name","event_name","date_text",
                                                     "suggested_date","confidence","created_at"})
        # 2026-10-09 在 2026-10-07 之后两天，排在最前；生日年度推算到 2026-10-10。
        self.assertEqual([item["event_name"] for item in data["dates"]],["考试","生日"])
        self.assertEqual(data["dates"][0]["days_left"],2)
        self.assertEqual(data["dates"][0]["countdown"],"2 天后")
        self.assertEqual(data["dates"][1]["occurrence"],"2026-10-10")
        self.assertEqual(data["counts"]["within_30d"],2)

    async def test_webui_usage_daily_series_is_differenced(self):
        day_start=self.now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        await self.store.record_llm_usage(created_at=day_start+3600,source="plugin",task_name="diary",
            model_name="planner",request_type="diary",prompt_tokens=100,completion_tokens=50,
            total_tokens=150,latency_ms=900.0,success=True)
        await self.store.record_llm_usage(created_at=day_start-86400+60,source="plugin",task_name="news",
            model_name="utils",request_type="news",prompt_tokens=10,completion_tokens=5,
            total_tokens=15,latency_ms=300.0,success=False)
        await self.store.record_search_api_event(created_at=day_start+7200,operation="search",provider_id="p1",
            provider_type="playwright",key_fingerprint="fp1",status_code=200,success=True,result_count=3,
            latency_ms=120.0,error_class="")
        data=await self.plugin.webui_usage()
        self.assertEqual(set(data),{"day","generated_at","totals","tokens","searches","daily"})
        self.assertEqual(set(data["totals"]),{"tokens_today","calls_today","search_calls_today",
                                              "tokens_7d","calls_7d"})
        self.assertEqual(set(data["tokens"][0]),{"label","source","task_name","calls","successes",
                                                 "prompt_tokens","completion_tokens","total_tokens","avg_latency_ms"})
        self.assertEqual(data["tokens"][0]["label"],"plugin/diary")
        self.assertEqual(set(data["searches"][0]),{"provider_type","calls","successes","results",
                                                   "average_latency_ms","last_at"})
        self.assertEqual(len(data["daily"]),7)
        self.assertEqual([item["day"] for item in data["daily"]],sorted(item["day"] for item in data["daily"]))
        # 近 7 日序列按日差分：今日 150 Token，昨日 15 Token，其余为 0。
        self.assertEqual([item["tokens"] for item in data["daily"]],[0,0,0,0,0,15,150])
        self.assertEqual(data["totals"]["tokens_today"],150)
        self.assertEqual(data["totals"]["tokens_7d"],165)
        self.assertEqual(data["totals"]["search_calls_today"],1)

    async def test_webui_config_summary_has_every_section_and_no_keys(self):
        data=await self.plugin.webui_config_summary()
        self.assertEqual(set(data),{"plugin","users","models","environment","linkage","debounce","recall",
                                    "context","rest_gate","memory","information","news","search","search_api",
                                    "social","creation","schedule","proactive","usage"})
        self.assertEqual(set(data["rest_gate"]),{"enabled","mode","gate_mode","wake_probability","llm_threshold",
                                                 "awake_grace_minutes","silence_minutes","night_window",
                                                 "nap_window","group_enabled","group_mode",
                                                 "group_night_window","group_nap_window",
                                                 "group_segment_types"})
        self.assertEqual(set(data["search_api"]),{"tool_enabled","history_enabled","history_retention_days",
                                                  "timeout_seconds","max_attempts","max_results","provider_count"})
        # 布尔一律翻成「开启/关闭」，不出现裸布尔；数值型参数与 version 不参与该检查。
        switch_fields={"linkage":{"drawpic_enabled","drawpic_detected"},
                       "debounce":{"private","group"},
                       "recall":{"enabled","cache_summary"},
                       "context":{"enabled","continuity_enabled"},
                       "memory":{"enabled","dream_fragments_enabled","diary_enabled",
                                 "important_dates_enabled","date_model_analysis_enabled"},
                       "information":{"enabled","association_enabled","proactive_share_enabled"},
                       "news":{"enabled"},"search":{"enabled","include_chat_topics"},
                       "search_api":{"tool_enabled","history_enabled"},
                       "social":{"enabled","owner_group_to_private_enabled"},
                       "creation":{"enabled","plaintext_storage_acknowledged","public_works_enabled",
                                   "external_reading_enabled"},
                       "proactive":{"enabled"},"usage":{"enabled"}}
        for section,keys in switch_fields.items():
            for key in keys:
                self.assertIn(str(data[section][key]),("开启","关闭"),f"{section}.{key} 未翻成中文开关")
        self.assertEqual(data["plugin"]["enabled"],"开启")
        self.assertEqual(data["users"],{"configured":0,"enabled":0})
        self.assertEqual(data["rest_gate"]["night_window"],"22:30-08:00")
        self.assertEqual(data["rest_gate"]["silence_minutes"],10)
        self.assertEqual(data["rest_gate"]["group_night_window"],"22:30-08:00")
        self.assertEqual(data["plugin"]["version"],PLUGIN_VERSION)
        # 配置摘要不出任何 Key：整个响应里不得出现 api_keys/fingerprint 字样。
        blob=json.dumps(data,ensure_ascii=False)
        for forbidden in ("api_keys","api_key","fingerprint","sk-"):
            self.assertNotIn(forbidden,blob)

    async def test_webui_admin_summary_scopes_and_key_fingerprints_only(self):
        from Mai_life.config import SocialGroupProfile,UserProfile
        config=MaiLifeSettings()
        config.social.enabled=True
        config.social.groups=[SocialGroupProfile(group_id="888",enabled=True,observe_enabled=True)]
        plugin=await build_plugin(self.store,config,now=self.now,
                                  users=[UserProfile(user_id="10001",role="owner")])
        await self.store.upsert_group_directory("888","测试群","stream-888",self.now.timestamp())
        await self.store.add_proactive_pending("ev1","10001","op1","stream-1",self.now.timestamp(),
                                               self.now.timestamp()+120)
        await self.store.record_proactive_skip("10001","low_energy",self.now.timestamp(),
                                               self.now.date().isoformat())
        # Key 只以指纹形态落库：用 save_search_key_runtime 写入一条健康态，验证只出指纹。
        await self.store.save_search_key_runtime("p1-playwright","abcdef0123456789",status="healthy",
            cooldown_until=0,failure_count=0,error_class="",used_at=self.now.timestamp())
        data=await plugin.webui_admin_summary()
        self.assertEqual(set(data),{"overview","groups","proactive","sources"})
        self.assertEqual(set(data["overview"]),{"version","users","owners","pending_dates","pending_proactive",
                                                "bookshelf_documents","private_documents","token_calls_today",
                                                "token_total_today","search_calls_today","social_enabled",
                                                "creation_enabled","network_enabled"})
        self.assertEqual(set(data["groups"][0]),{"group_id","group_name","enabled","observe_enabled",
                                                 "relay_target_enabled","stream_known"})
        self.assertEqual(set(data["proactive"]),{"candidates","skips"})
        self.assertEqual(set(data["proactive"]["candidates"][0]),{"user_id","topic","status","created_at","expires_at"})
        self.assertEqual(set(data["proactive"]["skips"][0]),{"reason","total","users"})
        self.assertEqual(set(data["sources"]),{"information_enabled","news_enabled","search_enabled",
                                               "external_reading","providers","keys"})
        self.assertEqual(set(data["sources"]["external_reading"]),{"enabled","api_name"})
        self.assertEqual(set(data["sources"]["providers"][0]),{"provider_id","provider_type","enabled",
                                                               "key_count","endpoint","model"})
        self.assertEqual(set(data["sources"]["keys"][0]),{"provider_id","fingerprint","status",
                                                          "cooldown_until","failure_count","last_error_class"})
        # 布尔转「开/关」；Key 只出指纹，原始凭据绝不出现。
        self.assertEqual(data["overview"]["social_enabled"],"开")
        self.assertEqual(data["sources"]["providers"][0]["enabled"],"开")
        blob=json.dumps(data,ensure_ascii=False)
        self.assertNotIn("secret-key-value",blob)
        self.assertNotIn("api_keys",blob)
        self.assertEqual(data["proactive"]["skips"][0]["reason"],"low_energy")


if __name__=="__main__":unittest.main()
