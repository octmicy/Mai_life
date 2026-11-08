"""Reference-image submission contract, without a host or paid image request."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import ast
import base64
import os
import tempfile
import unittest
import uuid


def load_submit():
    source = Path(__file__).resolve().parents[1] / "plugin.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MaiLifePlugin")
    method = next(node for node in plugin.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_submit_persona_ref_draw")
    namespace = {
        "Any": object, "os": os, "base64": base64, "uuid": uuid,
        "_mood_label": lambda _: "平静",
        "EnvironmentService": SimpleNamespace(weather_text=lambda _: "晴"),
        "_query_persona_ref_request": lambda self, request_id: True,  # API raise → 查询失败按存在处理
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_submit_persona_ref_draw"]


class PersonaReferenceContractTests(unittest.IsolatedAsyncioTestCase):
    _query_result = True  # 默认：查询失败按存在处理

    async def submit(self, receipt, *, persona_ref_request=None, query_result=True):
        api = AsyncMock(return_value=receipt)
        async def _query_stub(request_id: str) -> bool:
            return query_result
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "ref.png").write_bytes(b"fixture-image")
            plugin = SimpleNamespace(
                _load_persona_refs=lambda: ([{"file": "ref.png"}], root),
                PERSONA_REF_MAX=3, PERSONA_REF_MAX_BYTES=1024,
                _bot_name="Fixture", _persona_ref_fail={},
                _persona_ref_request=persona_ref_request or {},
                _query_persona_ref_request=_query_stub,
                ctx=SimpleNamespace(api=SimpleNamespace(call=api), logger=Mock()),
            )
            payload = {"state": {"current_activity": "看书", "current_location": "窗边"}}
            accepted = await load_submit()(plugin, "fixture-session", payload, "2026-11-08")
        return accepted, api, plugin

    async def test_v2_receipt_is_accepted_and_reference_task_is_explicit(self):
        accepted, api, plugin = await self.submit({"accepted": True, "task_id": "fixture-task", "status": "pending"})
        self.assertTrue(accepted)
        self.assertNotIn("fixture-session", plugin._persona_ref_fail)
        args = api.await_args.kwargs
        self.assertEqual(args["task_type"], "edit_image")
        self.assertEqual(args["version"], "2")
        self.assertEqual(base64.b64decode(args["source_images"][0]), b"fixture-image")
        self.assertIn("看书", args["prompt"])
        self.assertIn("窗边", args["prompt"])

    async def test_rejection_is_not_success(self):
        """明确被拒 → ok=False 且清除未确认登记（测试桩 fixture 无 fail 标记写入路径）。"""
        accepted, _, plugin = await self.submit({"accepted": False, "task_id": None, "error": {"code": "INVALID_REQUEST"}})
        self.assertFalse(accepted)
        self.assertNotIn("fixture-session", plugin._persona_ref_request)

    async def test_accepted_without_task_id_is_not_a_valid_receipt(self):
        for receipt in [{"accepted": True}, {"accepted": True, "task_id": " "}, {"accepted": True, "task_id": 1}]:
            with self.subTest(receipt=receipt):
                accepted, _, _ = await self.submit(receipt)
                self.assertFalse(accepted)

    async def test_transport_success_does_not_mean_v2_acceptance(self):
        accepted, _, _ = await self.submit({"success": True, "result": {"success": True}})
        self.assertFalse(accepted)

    async def test_unconfirmed_request_query_found_suppresses_resubmit(self):
        """Issue #26：未确认请求查询确认已受理 → 本轮不重画、返回 False（不创建第二个任务）。"""
        pre_request = {"fixture-session": {
            "day": "2026-11-08", "request_id": "refdraw-abc123456789", "state": "unconfirmed"}}
        accepted, _, plugin = await self.submit(
            {"accepted": False, "task_id": None},
            persona_ref_request=pre_request, query_result=True)
        self.assertFalse(accepted)   # 查询确认已受理 → 本轮不重画
        self.assertNotIn("fixture-session", plugin._persona_ref_fail)  # 不设 fail（任务可能在生成中）
        self.assertIn("fixture-session", plugin._persona_ref_request)  # 登记保留


if __name__ == "__main__":
    unittest.main()
