"""v1.27.0 书籍导入（#35）与自定义日程模板（#33）测试。

防卡死硬约束（见 docs/_plan_v1.27.0_import_book_schedule_ui.md §6，上一轮测试曾致系统崩溃重启）：
- 解析逻辑用同步 TestCase，不起事件循环、不装配插件；fixture 全部内存生成且 ≤64KB。
- 存储/API 用 IsolatedAsyncioTestCase + tempdir LifeStore + 轻量插件骨架（**不用 build_plugin**，
  避免装配维护循环与服务树）；DummyLLM 钉死本地兜底分支，零网络/零真实 RPC。
- 零 sleep、零后台任务、零子进程/线程；分块测试 ≤3 块 × ≤200 字符；过期清理直接调函数一次。
- HTML/epub 契约用静态文本断言，不开浏览器。单文件目标 <10s。
"""
from __future__ import annotations

import io
import re
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from Mai_life.config import MaiLifeSettings
from Mai_life.core.environment import EnvironmentService
from Mai_life.core.storage import LifeStore
from Mai_life.creation import book_import
from Mai_life.creation.book_import import BookImportError
from Mai_life.creation.bookshelf_service import BookshelfService
from Mai_life.creation.creation_service import CreationService
from Mai_life.life.schedule_service import ScheduleService
from Mai_life.messaging.prompt_builder import PromptBuilder
from Mai_life.plugin import MaiLifePlugin

if __package__:
    from .support import DummyCtx, DummyLLM, DummyLogger, TZ
else:
    from support import DummyCtx, DummyLLM, DummyLogger, TZ

PLUGIN_DIR = str(Path(__file__).parents[1])


# ==================== 1. 纯解析（同步，无事件循环） ====================


class BookImportParserTests(unittest.TestCase):
    def test_normalize_and_sha_stable(self):
        self.assertEqual(book_import.normalize_text("  a\n b\t c  "), "a b c")
        # 指纹对空白归一：不同排版同内容 → 同 sha。
        self.assertEqual(book_import.content_sha256("a b\nc"), book_import.content_sha256("a  b c"))

    def test_parse_txt_utf8_with_chapters(self):
        raw = "前言一段。\n第一章 起点\n正文甲。\n第二章 转折\n正文乙。".encode("utf-8")
        parsed = book_import.parse_txt(raw, title="测试书")
        self.assertEqual(parsed.title, "测试书")
        self.assertEqual(parsed.total_chars, len(raw.decode("utf-8")))
        self.assertEqual(len(parsed.sha256), 64)
        titles = [c["title"] for c in parsed.chapters]
        self.assertIn("前言", titles)
        self.assertTrue(any("第一章" in t for t in titles))

    def test_parse_txt_gb18030_fallback(self):
        text = "中文内容测试"
        parsed = book_import.parse_txt(text.encode("gb18030"))
        self.assertIn("中文内容测试", parsed.chapters[0]["text"])
        self.assertTrue(any("GB18030" in w for w in parsed.warnings))

    def test_parse_txt_bom_removed(self):
        parsed = book_import.parse_txt(b"\xef\xbb\xbf" + "你好".encode("utf-8"))
        self.assertEqual(parsed.chapters[0]["text"], "你好")
        self.assertTrue(any("BOM" in w for w in parsed.warnings))

    def test_parse_txt_empty_raises(self):
        with self.assertRaises(BookImportError) as ctx:
            book_import.parse_txt(b"   \n  ")
        self.assertEqual(ctx.exception.code, "empty")

    def test_chunk_text_boundaries(self):
        self.assertEqual(book_import.chunk_text("abcdef", 2), ["ab", "cd", "ef"])
        self.assertEqual(book_import.chunk_text("abcde", 2), ["ab", "cd", "e"])
        self.assertEqual(book_import.chunk_text("", 2), [])

    def test_validate_submission_limits(self):
        book_import.validate_submission(title="书", total_chars=100, max_chars=200)
        for kwargs, code in (({"title": "", "total_chars": 100, "max_chars": 200}, "title"),
                             ({"title": "书", "total_chars": 0, "max_chars": 200}, "empty"),
                             ({"title": "书", "total_chars": 300, "max_chars": 200}, "too_large")):
            with self.assertRaises(BookImportError) as ctx:
                book_import.validate_submission(**kwargs)
            self.assertEqual(ctx.exception.code, code)


# ==================== 2. 存储与导入 API（async，轻量骨架） ====================


class BookImportStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        # 轻量插件骨架：只挂 API 所需依赖，不 build_plugin（不启维护循环/服务树）。
        self.plugin = MaiLifePlugin()
        self.plugin.set_plugin_config(self.config.model_dump(mode="python"))
        self.plugin._store = self.store
        self.plugin._env = EnvironmentService(self.store, self.config, DummyLogger())
        self.plugin._schedule = ScheduleService(self.store, self.config, DummyLLM(), PLUGIN_DIR, DummyLogger())
        self.plugin._creation = CreationService(DummyCtx(), self.store, self.config, DummyLLM(), DummyLogger())
        self.plugin._personality = "测试人格"

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _begin(self, title="测试书", total=300, privacy="private"):
        res = await self.plugin.webui_book_import_begin(title=title, author="作者", sha256="ab" * 32,
                                                        total_chars=total, privacy=privacy)
        self.assertTrue(res.get("success"), res)
        return res["import_id"]

    async def _fill(self, import_id, text):
        size = 100
        for seq, i in enumerate(range(0, len(text), size)):
            res = await self.plugin.webui_book_import_chunk(import_id=import_id, seq=seq, text=text[i:i + size])
            self.assertTrue(res.get("success"), res)

    async def test_full_flow_commit_lands_document(self):
        text = "甲" * 250  # 3 块（100/100/50）
        import_id = await self._begin(total=len(text))
        await self._fill(import_id, text)
        res = await self.plugin.webui_book_import_commit(import_id=import_id)
        self.assertTrue(res.get("success"), res)
        doc_id = res["document_id"]
        doc = await self.store.get_bookshelf_document(doc_id, allow_private=True)
        self.assertEqual(doc["doc_type"], "imported_book")
        self.assertEqual(doc["status"], "archived")
        self.assertEqual(doc["privacy"], "private")
        self.assertEqual(doc["source_kind"], "user_import")
        page = await self.store.imported_book_read(doc_id, offset=0, limit=20000)
        self.assertEqual(page["content"], text)
        self.assertEqual(page["total_chars"], 250)
        self.assertFalse(page["truncated"])
        # staging 与暂存分块已清空。
        self.assertEqual(await self.store.book_import_get(import_id), {})

    async def test_chunk_idempotent_overwrite(self):
        import_id = await self._begin(total=100)
        await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0, text="甲" * 100)
        res = await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0, text="乙" * 100)
        self.assertEqual(res["received_chars"], 100)  # 覆盖而非累加
        self.assertEqual(await self.store.book_import_first_chunk(import_id), "乙" * 100)

    async def test_commit_missing_chunk_rejected(self):
        import_id = await self._begin(total=200)
        await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0, text="甲" * 100)
        # 缺 seq=1 → 总长不符
        res = await self.plugin.webui_book_import_commit(import_id=import_id)
        self.assertFalse(res.get("success"))
        self.assertEqual(res.get("code"), "commit_invalid")

    async def test_commit_noncontiguous_seq_rejected(self):
        import_id = await self._begin(total=200)
        await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0, text="甲" * 100)
        await self.plugin.webui_book_import_chunk(import_id=import_id, seq=2, text="乙" * 100)
        res = await self.plugin.webui_book_import_commit(import_id=import_id)
        self.assertFalse(res.get("success"))

    async def test_begin_replaces_previous_staging(self):
        first = await self._begin(title="第一本")
        second = await self._begin(title="第二本")
        self.assertNotEqual(first, second)
        self.assertEqual(await self.store.book_import_get(first), {})
        self.assertTrue(await self.store.book_import_get(second))

    async def test_staging_expiry_cleanup(self):
        import_id = await self._begin()
        await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0, text="甲" * 100)
        # 直接把过期时间拨到过去，再调一次清理函数（不起 tick）。
        async with self.store._lock:
            self.store.conn.execute("UPDATE book_import_staging SET expires_at=0 WHERE id=?", (import_id,))
            self.store.conn.commit()
        removed = await self.store.book_import_cleanup_expired(now=1e12)
        self.assertEqual(removed, 1)
        self.assertEqual(await self.store.book_import_get(import_id), {})

    async def test_duplicate_check_by_sha_and_title(self):
        text = "独特内容" * 30
        sha = book_import.content_sha256(text)
        import_id = await self._begin(title="唯一书", total=len(text))
        await self._fill(import_id, text)
        # 用真实 sha 重新 begin 以便 source_ref 命中
        await self.plugin.webui_book_import_cancel(import_id=import_id)
        res = await self.plugin.webui_book_import_begin(title="唯一书", author="", sha256=sha,
                                                        total_chars=len(text), privacy="private")
        import_id = res["import_id"]
        await self._fill(import_id, text)
        await self.plugin.webui_book_import_commit(import_id=import_id)
        by_sha = await self.plugin.webui_book_import_check(title="别的名字", sha256=sha, total_chars=len(text))
        self.assertTrue(by_sha["duplicate"].get("id"))
        by_title = await self.plugin.webui_book_import_check(title="唯一书", sha256="", total_chars=len(text))
        self.assertTrue(by_title["duplicate"].get("id"))

    async def test_overview_truncates_and_excludes_imported(self):
        text = "长" * 3000
        import_id = await self._begin(title="超长书", total=len(text))
        await self._fill(import_id, text)
        await self.plugin.webui_book_import_commit(import_id=import_id)
        overview = await self.plugin.webui_bookshelf_overview()
        self.assertTrue(overview.get("success", True) is not False, overview)
        book = next(b for b in overview["books"] if b["doc_type"] == "imported_book")
        self.assertLessEqual(len(book["content"]), 2000)
        self.assertTrue(book["truncated"])
        self.assertEqual(book["total_chars"], 3000)
        self.assertNotIn("超长书", overview["books_full_text"])

    async def test_book_read_pagination(self):
        text = "0123456789" * 30  # 300 字
        import_id = await self._begin(total=len(text))
        await self._fill(import_id, text)
        doc_id = (await self.plugin.webui_book_import_commit(import_id=import_id))["document_id"]
        res = await self.plugin.webui_book_read(document_id=doc_id, offset=100, limit=50)
        self.assertEqual(res["content"], text[100:150])
        self.assertTrue(res["truncated"])
        self.assertEqual(res["total_chars"], 300)

    async def test_friend_cannot_see_private_imported(self):
        import_id = await self._begin(total=100, privacy="private")
        await self._fill(import_id, "甲" * 100)
        doc_id = (await self.plugin.webui_book_import_commit(import_id=import_id))["document_id"]
        self.assertEqual(await self.store.get_bookshelf_document(doc_id, allow_private=False), {})
        self.assertTrue(await self.store.get_bookshelf_document(doc_id, allow_private=True))

    async def test_cancel_clears_staging(self):
        import_id = await self._begin()
        await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0, text="甲" * 100)
        res = await self.plugin.webui_book_import_cancel(import_id=import_id)
        self.assertTrue(res["success"])
        self.assertEqual(await self.store.book_import_get(import_id), {})

    async def test_import_disabled_rejects(self):
        self.plugin.config.creation.book_import_enabled = False
        res = await self.plugin.webui_book_import_begin(title="书", total_chars=100)
        self.assertFalse(res.get("success"))

    async def test_chunk_too_large_rejected(self):
        import_id = await self._begin(total=book_import.CHUNK_CHARS + 10)
        res = await self.plugin.webui_book_import_chunk(import_id=import_id, seq=0,
                                                        text="甲" * (book_import.CHUNK_CHARS + 1))
        self.assertFalse(res.get("success"))
        self.assertEqual(res.get("code"), "chunk_too_large")


# ==================== 3. 日程模板（async + 同步校验） ====================


def _node(start, end, kind="leisure", summary="活动", **extra):
    base = {"start": start, "end": end, "kind": kind, "summary": summary,
            "location": "家", "energy_load": 0, "shareability": 0.3}
    base.update(extra)
    return base


def _template(workday=None, weekend=None):
    default = [_node("00:00", "08:00", "sleep", "睡觉"), _node("08:00", "09:00", "meal", "早餐"),
               _node("12:00", "13:00", "meal", "午餐"), _node("19:00", "22:00", "leisure", "休闲"),
               _node("23:00", "24:00", "sleep", "入睡")]
    return {"workday": workday if workday is not None else list(default),
            "weekend": weekend if weekend is not None else list(default)}


class ScheduleTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.plugin = MaiLifePlugin()
        self.plugin.set_plugin_config(self.config.model_dump(mode="python"))
        self.plugin._store = self.store
        self.plugin._env = EnvironmentService(self.store, self.config, DummyLogger())
        # 模板目录指向空 tempdir，隔离仓库自带 mai_template.json。
        self.plugin._schedule = ScheduleService(self.store, self.config, DummyLLM(), self.tmp.name, DummyLogger())
        self.plugin._personality = "测试人格"
        self.now = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_db_template_precedence_over_file(self):
        Path(self.tmp.name, "mai_template.json").write_text(
            __import__("json").dumps(_template(), ensure_ascii=False), encoding="utf-8")
        svc = self.plugin._schedule
        await svc.refresh_template_cache()
        self.assertEqual(svc.template_status(), "ok(file)")
        db_tpl = _template(workday=[_node("00:00", "08:00", "sleep", "睡觉"),
                                    _node("08:00", "09:00", "meal", "早餐"),
                                    _node("12:00", "13:00", "meal", "午餐"),
                                    _node("09:00", "12:00", "study", "DB专属学习", time_fixed=True),
                                    _node("23:00", "24:00", "sleep", "入睡")])
        await self.store.schedule_template_save("workday", db_tpl["workday"], 1.0)
        await self.store.schedule_template_save("weekend", db_tpl["weekend"], 1.0)
        await svc.refresh_template_cache()
        self.assertEqual(svc.template_status(), "ok(db)")
        self.assertIn("DB专属学习", [n["summary"] for n in svc._template()["workday"]])

    async def test_db_invalid_falls_back_to_file(self):
        Path(self.tmp.name, "mai_template.json").write_text(
            __import__("json").dumps(_template(), ensure_ascii=False), encoding="utf-8")
        await self.store.schedule_template_save("workday", [{"bad": "node"}], 1.0)
        await self.store.schedule_template_save("weekend", [], 1.0)
        svc = self.plugin._schedule
        await svc.refresh_template_cache()
        # DB 校验失败被整份弃用，回退到合法文件 → 最终来源是 file（DB 问题已 warning 记录）。
        self.assertEqual(svc.template_status(), "ok(file)")
        summaries = [n["summary"] for n in svc._template()["workday"]]
        self.assertNotIn("bad", summaries)
        self.assertTrue(summaries)

    async def test_both_missing_uses_builtin_skeleton(self):
        svc = self.plugin._schedule
        await svc.refresh_template_cache()
        self.assertEqual(svc.template_status(), "missing")
        editable = await svc.editable_template(self.now)
        self.assertTrue(editable["workday"])
        self.assertTrue(any(n["kind"] == "sleep" for n in editable["workday"]))

    async def test_save_validation_rejects_overlapping_fixed(self):
        tpl = _template(workday=[_node("00:00", "08:00", "sleep", "睡觉"),
                                 _node("08:00", "09:00", "meal", "早餐"),
                                 _node("12:00", "13:00", "meal", "午餐"),
                                 _node("09:00", "12:00", "study", "固定A", time_fixed=True),
                                 _node("10:00", "11:00", "work", "固定B", time_fixed=True),
                                 _node("23:00", "24:00", "sleep", "入睡")])
        res = await self.plugin.webui_schedule_template_save(template=tpl)
        self.assertFalse(res.get("success"))
        self.assertTrue(any("重叠" in p for p in res.get("problems", [])))

    async def test_save_valid_template_persists(self):
        res = await self.plugin.webui_schedule_template_save(template=_template())
        self.assertTrue(res.get("success"), res)
        self.assertEqual(res.get("status"), "ok(db)")
        stored = await self.store.schedule_template_get()
        self.assertIn("workday", stored)

    async def test_fixed_summary_exempt_from_overlap_gate(self):
        svc = self.plugin._schedule
        tpl = _template(workday=[_node("00:00", "08:00", "sleep", "睡觉"),
                                 _node("08:00", "09:00", "meal", "早餐"),
                                 _node("12:00", "13:00", "meal", "午餐"),
                                 _node("20:00", "21:00", "rest", "写日记", content_fixed=True),
                                 _node("23:00", "24:00", "sleep", "入睡")])
        await self.store.schedule_template_save("workday", tpl["workday"], 1.0)
        await self.store.schedule_template_save("weekend", tpl["weekend"], 1.0)
        await svc.refresh_template_cache()
        # 连续两天都含固定事项「写日记」+ 一个重复自由节点：固定事项豁免，自由节点不足阈值不拒。
        raw = [_node("20:00", "21:00", "rest", "写日记"), _node("14:00", "16:00", "leisure", "拼拼图")]
        recent = {"写日记", "拼拼图"}
        self.assertFalse(svc._recent_day_overlap(self.now, raw, recent))
        # 缺失固定事项 → _fixed_content_missing 命中。
        missing = svc._fixed_content_missing(False, [_node("14:00", "16:00", "leisure", "拼拼图")])
        self.assertIn("写日记", missing)

    async def test_time_fixed_injection_trims_overlap(self):
        svc = self.plugin._schedule
        tpl = _template(workday=[_node("00:00", "08:00", "sleep", "睡觉"),
                                 _node("08:00", "09:00", "meal", "早餐"),
                                 _node("12:00", "13:00", "meal", "午餐"),
                                 _node("09:00", "12:00", "study", "锁定学习", time_fixed=True),
                                 _node("23:00", "24:00", "sleep", "入睡")])
        await self.store.schedule_template_save("workday", tpl["workday"], 1.0)
        await self.store.schedule_template_save("weekend", tpl["weekend"], 1.0)
        await svc.refresh_template_cache()
        # 模型生成一个横跨 09:00-12:00 的自由节点，应被固定区间裁切。
        day = self.now.date().isoformat()
        generated = svc._validate(day, [_node("00:00", "08:00", "sleep", "睡觉"),
                                        _node("08:00", "09:00", "meal", "早餐"),
                                        _node("10:00", "14:00", "leisure", "外出"),
                                        _node("12:00", "13:00", "meal", "午餐"),
                                        _node("23:00", "24:00", "sleep", "入睡")])
        result = svc._apply_time_fixed(day, False, generated)
        fixed = [n for n in result if n["summary"] == "锁定学习"]
        self.assertEqual(len(fixed), 1)
        self.assertEqual((fixed[0]["start_minute"], fixed[0]["end_minute"]), (9 * 60, 12 * 60))
        # 无重叠：所有节点按 start 排序且后节点 start >= 前节点 end。
        self.assertTrue(all(a["end_minute"] <= b["start_minute"] for a, b in zip(result, result[1:])))

    async def test_regenerate_today_uses_force(self):
        res = await self.plugin.webui_schedule_regenerate_today()
        self.assertTrue(res.get("success"), res)
        self.assertGreater(res.get("count", 0), 0)
        self.assertIn(res.get("report", {}).get("source"), {"llm", "fallback"})
        # 再次调用（force）仍成功，且当日框架已落库。
        again = await self.plugin.webui_schedule_regenerate_today()
        self.assertTrue(again.get("success"))

    async def test_template_get_and_preview_shape(self):
        got = await self.plugin.webui_schedule_template_get()
        self.assertTrue(got.get("success"))
        self.assertIn("workday", got["template"])
        preview = await self.plugin.webui_schedule_template_preview()
        self.assertTrue(preview.get("success"))
        self.assertIn("nodes", preview)
        self.assertIn("quiet_segments", preview)
        self.assertIn("gate_mode", preview)


# ==================== 4. 前端契约（静态文本，不开浏览器） ====================


class ImportFrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (Path(PLUGIN_DIR) / "webui" / "life.html").read_text(encoding="utf-8")

    def test_epub_parser_present_with_decompression_guard(self):
        self.assertIn("function parseEpub", self.html)
        self.assertIn("DecompressionStream", self.html)
        self.assertIn("deflate-raw", self.html)
        self.assertIn("META-INF/container.xml", self.html)
        # 老浏览器降级提示存在。
        self.assertIn("不支持 epub 解压", self.html)

    def test_import_flow_uses_declared_actions(self):
        for action in ("book_import_check", "book_import_begin", "book_import_chunk",
                       "book_import_commit", "book_import_cancel", "book_read"):
            self.assertIn(f"'{action}'", self.html, f"life.html 缺少 {action} 调用")

    def test_template_editor_uses_declared_bindings(self):
        for name in ("template_get", "template_preview", "schedule_template_save", "schedule_regenerate_today"):
            self.assertIn(f"'{name}'", self.html, f"life.html 缺少 {name} 调用")
        self.assertIn("data-f=\"time_fixed\"", self.html)
        self.assertIn("data-f=\"content_fixed\"", self.html)

    def test_no_network_or_external_resources(self):
        self.assertIsNone(re.search(r"\bfetch\s*\(|\bXMLHttpRequest\b", self.html))
        self.assertIsNone(re.search(r"""(?:src|href)\s*=\s*["']https?://""", self.html, re.I))


# ==================== 5. 导入书作为聊天候选（v1.27.0，#35） ====================


class ImportedBookChatCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LifeStore(self.tmp.name); await self.store.initialize()
        self.config = MaiLifeSettings()
        self.bookshelf = BookshelfService(self.store, self.config)
        self.owner = {"user_id": "1", "role": "owner", "temperature": 50}

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _import_book(self, title="三体", text="宇宙很大，生活更大。" * 20):
        now = 1_000_000.0
        await self.store.book_import_begin(import_id="imp1", owner="webui-admin", title=title,
                                           author="刘慈欣", sha256="cd" * 32, total_chars=len(text),
                                           privacy="private", now=now)
        await self.store.book_import_add_chunk("imp1", 0, text, now)
        await self.store.book_import_commit("imp1", document_id=f"imported:{title}", summary=f"《{title}》 {text[:40]}", now=now)

    def test_reading_note_frames_imported_book(self):
        shelf = {"items": [{"title": "三体", "summary": "宇宙很大", "imported": True}]}
        note = PromptBuilder._reading_note(shelf, "麦麦")
        self.assertIn("【在读分享】", note)
        self.assertIn("三体", note)
        self.assertIn("自然聊起", note)
        self.assertIn("不要透露这条提示", note)

    def test_reading_note_empty_without_imported(self):
        self.assertEqual(PromptBuilder._reading_note({"items": [{"title": "自创", "imported": False}]}, "麦麦"), "")
        self.assertEqual(PromptBuilder._reading_note({"items": []}, "麦麦"), "")
        self.assertEqual(PromptBuilder._reading_note(None, "麦麦"), "")

    async def test_context_for_user_marks_imported(self):
        await self._import_book()
        await self.store.create_bookshelf_document({"id": "work-1", "doc_type": "work", "work_type": "essay",
                                                    "title": "自创随笔", "privacy": "private", "status": "archived",
                                                    "created_at": 2_000_000.0})
        ctx = await self.bookshelf.context_for_user(self.owner, limit=10)
        by_title = {it["title"]: it for it in ctx["items"]}
        self.assertTrue(by_title["三体"]["imported"])
        self.assertFalse(by_title["自创随笔"]["imported"])

    async def test_replyer_and_planner_carry_reading_note(self):
        await self._import_book()
        ctx = await self.bookshelf.context_for_user(self.owner, limit=10)
        state = {"energy": 60, "mood_valence": 0.0, "current_activity": "看书"}
        replyer = PromptBuilder().replyer(state, {"description": "晴"}, {"current": None, "next": None},
                                          self.owner, [], bookshelf=ctx)
        self.assertIn("【在读分享】", replyer)
        stable, _ = PromptBuilder().planner_split(state, {"description": "晴"}, {"current": None, "next": None},
                                                  self.owner, {}, [], bookshelf=ctx)
        self.assertIn("【在读分享】", stable)

    async def test_schedule_ideas_label_imported_as_reading(self):
        await self._import_book(title="三体")

        class CapturingLLM:
            def __init__(self): self.prompt = ""
            def task_available(self, kind): return kind == "schedule"
            def task_for(self, kind): return "planner"
            async def generate_json(self, prompt, system, fallback, **kwargs):
                self.prompt = prompt; return fallback

        llm = CapturingLLM()
        svc = ScheduleService(self.store, self.config, llm, self.tmp.name, DummyLogger())
        now = datetime(2026, 11, 12, 3, 0, tzinfo=TZ)
        await svc.ensure_day(now, "人格", "晴", force=True)
        self.assertIn("在读《三体》", llm.prompt)
        self.assertNotIn("自己的作品《三体》", llm.prompt)


if __name__ == "__main__":
    unittest.main()