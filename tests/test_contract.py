from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from Mai_life.config import MaiLifeSettings,SocialGroupProfile,UserProfile
from Mai_life.messaging.prompt_builder import PromptBuilder, relationship_stage
from Mai_life.plugin import MaiLifePlugin


class ContractTests(unittest.TestCase):
    def test_default_toml_validates(self):
        root=Path(__file__).parents[1]
        config=MaiLifeSettings.model_validate(tomllib.loads((root/"config.toml").read_text(encoding="utf-8-sig")))
        self.assertEqual(config.plugin.config_version,"1.7.0")
        self.assertEqual(config.environment.timezone,"Asia/Shanghai")
        self.assertEqual(config.users.profiles[0].daily_proactive_max,1)
        self.assertFalse(config.rest_gate.enabled)
        self.assertTrue(config.debounce.enabled)
        self.assertTrue(config.recall.enabled)
        self.assertFalse(config.recall.cache_summary_enabled)
        self.assertEqual(config.context.prompt_max_chars,4000)
        self.assertEqual(config.models.scene_detail_task,"")
        self.assertEqual(config.models.vision_task,"vlm")
        self.assertTrue(config.memory.enabled)
        self.assertFalse(config.memory.date_model_analysis_enabled)
        self.assertFalse(config.information.enabled); self.assertFalse(config.news.enabled); self.assertFalse(config.search.enabled)
        self.assertEqual(config.search_api.providers,[])
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
        for expected in {"/mai_status","/mai_schedule","/mai_relation","/mai_recalled","/mai_diary","/mai_dates","/mai_news","/mai_explore","/mai_relay","/mai_bookshelf","/mai_read","/mai_create_now","/mai_admin","get_life_state","get_current_scene","admin_snapshot","mai_life_management"}:
            self.assertIn(expected,names)
        self.assertNotIn("/mai_skills",names)
        hooks={str((item.get("metadata") or {}).get("hook") or "") for item in components if item.get("type")=="HOOK_HANDLER"}
        self.assertIn("chat.receive.before_process",hooks)
        self.assertIn("maisaka.replyer.after_response",hooks)
        self.assertIn("send_service.after_send",hooks)
        self.assertIn("send_service.before_send",hooks)
        self.assertGreaterEqual(len(components),20)
        private_api=next(item for item in components if item.get("name")=="admin_snapshot")
        self.assertFalse((private_api.get("metadata") or {}).get("public"))
        home_card=next(item for item in components if item.get("name")=="mai_life_management")
        self.assertEqual(home_card.get("type"),"HOME_CARD")
        self.assertTrue(str((home_card.get("metadata") or {}).get("link_url")).startswith("/plugin-config"))

    def test_all_webui_fields_have_translated_labels(self):
        schema=MaiLifePlugin.build_config_schema(plugin_id="maibot-community.mai-life",plugin_name="麦麦生活")
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
        result=MaiLifePlugin._planner_messages_with_context(messages,"\n【麦麦生活】背景")
        self.assertIn("【麦麦生活】",result[0]["content"])
        self.assertEqual(result[1],messages[1]); self.assertNotIn("【麦麦生活】",messages[0]["content"])

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
        self.assertEqual(config.plugin.config_version,"1.7.0")
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
            {"time_period":"晚上","day_type":"工作日","media":["text"]},{"unresolved_topics":["插件测试"]},"提出问题",[],
        )
        self.assertIn("普通朋友",text)
        self.assertIn("不得对这位用户使用主人/恋人称呼",text)


if __name__=="__main__":unittest.main()
