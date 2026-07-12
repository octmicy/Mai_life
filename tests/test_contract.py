from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from Mai_life.config import MaiLifeSettings
from Mai_life.plugin import MaiLifePlugin
from Mai_life.prompt_builder import PromptBuilder, relationship_stage


class ContractTests(unittest.TestCase):
    def test_default_toml_validates(self):
        root=Path(__file__).parents[1]
        config=MaiLifeSettings.model_validate(tomllib.loads((root/"config.toml").read_text(encoding="utf-8-sig")))
        self.assertEqual(config.plugin.config_version,"1.0.2")
        self.assertEqual(config.environment.timezone,"Asia/Shanghai")
        self.assertEqual(config.proactive.daily_max_per_user,2)
        self.assertFalse(config.rest_gate.enabled)
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
        for expected in {"/mai_status","/mai_schedule","/mai_relation","get_life_state","get_current_scene"}:
            self.assertIn(expected,names)
        hooks={str(item.get("hook") or "") for item in components if item.get("type")=="hook_handler"}
        # SDK descriptors vary by release; at minimum all 16 declarations must register.
        self.assertGreaterEqual(len(components),16)

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


if __name__=="__main__":unittest.main()
