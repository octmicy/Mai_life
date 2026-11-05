from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import sqlite3
from pathlib import Path
from datetime import datetime,timedelta,timezone

from Mai_life.config import MaiLifeSettings,UserProfile
from Mai_life.core.storage import LifeStore,SCHEMA_VERSION
from Mai_life.creation.bookshelf_service import BookshelfService
from Mai_life.creation.creation_service import CreationService
from Mai_life.creation.inspiration_service import InspirationService


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class OfflineLLM:
    def task_available(self,kind):return False
    def task_for(self,kind):return kind


class RejectingReviewLLM(OfflineLLM):
    """始终拒绝且不给修订稿：v1.25.0 起会触发一次带意见重写，二次仍拒才 fail-closed。"""
    def task_available(self,kind):return kind in {"creation_review","creation_body"}
    def task_for(self,kind):return kind
    async def generate_json(self,*args,**kwargs):
        return {"accepted":False,"review_notes":"不适合归档","revised_content":"","summary":""}
    async def generate(self,prompt,system,**kwargs):return "重写后的正文内容"


class RejectOnceReviewLLM(OfflineLLM):
    """首次审校拒绝且不给修订稿，重写后复核通过：验证 Issue #16 的带意见重写恢复路径。"""
    def __init__(self):
        self.reviews=0; self.body_prompts=[]
    def task_available(self,kind):return kind in {"creation_outline","creation_review","creation_body"}
    def task_for(self,kind):return kind
    async def generate_json(self,prompt,system,fallback,**kwargs):
        if str(kwargs.get("task_kind") or "")=="creation_review":
            self.reviews+=1
            if self.reviews==1:
                return {"accepted":False,"review_notes":"节奏拖沓，压缩前两段","revised_content":"","summary":""}
            return {"accepted":True,"review_notes":"复核通过","revised_content":"","summary":"本章摘要"}
        return {"title":"拟定标题","premise":"拟定前提","sections":["起","承","转"],"privacy":"public"}
    async def generate(self,prompt,system,**kwargs):
        self.body_prompts.append(prompt)
        return "重写后的本章正文"


class ScriptedLLM:
    """确定性模型替身：三个创作任务可用，记录 prompt 并返回可断言的续写内容。"""

    def __init__(self):self.calls=[]
    def task_available(self,kind):return kind in {"creation_outline","creation_body","creation_review"}
    def task_for(self,kind):return kind
    async def generate_json(self,prompt,system,fallback,**kwargs):
        self.calls.append({"kind":str(kwargs.get("task_kind") or ""),"prompt":prompt})
        if str(kwargs.get("task_kind") or "")=="creation_outline":
            # 故意返回一个不同的书名，验证续写路径会把标题钉回原书名。
            return {"title":"模型拟定的书名","premise":"模型拟定的前提",
                    "sections":["起","承","转"],"privacy":"public"}
        return {"accepted":True,"review_notes":"连续性检查通过。","revised_content":"","summary":"本章摘要"}
    async def generate(self,prompt,system,**kwargs):
        self.calls.append({"kind":str(kwargs.get("task_kind") or ""),"prompt":prompt})
        return "模型续写的本章正文，开头与上一章结尾自然衔接，篇幅足够断言。"


class DummyAPI:
    def __init__(self):self.calls=[]
    async def call(self,name,**kwargs):
        self.calls.append((name,kwargs))
        return {"items":[
            {"id":"page-1","title":"一页素材","summary":"一段可供阅读的文字摘要。",
             "binary_data_base64":"QUJD","image":b"ignored"},
            {"id":"page-2","title":"二进制素材","summary":b"ignored-binary"},
            {"id":"page-3","title":"Data URL 素材","content":"data:image/png;base64,QUJD"},
        ]}


class DummyContext:
    def __init__(self):self.api=DummyAPI()


class Base64RejectingAPI:
    """外部阅读返回 88 字符 base64 载荷与一段正常文本，验证短 base64 同样被拒。"""
    def __init__(self):self.calls=[]
    async def call(self,name,**kwargs):
        self.calls.append((name,kwargs))
        return {"items":[
            {"id":"page-b64","title":"Base64 素材","summary":"QUJD"*22},
            {"id":"page-ok","title":"正常素材","summary":"一段可供阅读的正常文字摘要。"},
        ]}


class CreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.ctx=DummyContext(); self.llm=OfflineLLM()
        self.now=datetime(2026,7,13,16,0,tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def add_inspiration(self,kind="life",privacy="public",ref="test",expires_at=None):
        return await self.store.add_creation_inspiration({
            "id":f"inspiration-{kind}-{ref}","source_kind":kind,"source_ref":ref,
            "prompt_digest":"最近生活里留下的一点安静灵感","privacy_ceiling":privacy,
            "score":0.8,"created_at":self.now.timestamp(),
            "expires_at":float(expires_at if expires_at is not None else self.now.timestamp()+86400),
        })

    def ready_continuation(self,probability=1.0):
        """打开创作与续写，并把概率钉在端点，避免依赖随机流。"""
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        self.config.creation.continuation_enabled=True
        self.config.creation.continuation_probability=probability
        return CreationService(self.ctx,self.store,self.config,self.llm,DummyLogger())

    async def seed_book(self,*,document_id="work-old",title="旧书",premise="旧书的前提",chapters=1,
                        privacy="private",work_type="novel_fragment",last_chapter_at=None):
        """直接写一本已归档旧书（document + 每章 outline/draft/review/final 四条修订），不跑流水线。"""
        base=float(last_chapter_at if last_chapter_at is not None else self.now.timestamp()-5*86400)
        self.assertTrue(await self.store.create_bookshelf_document({
            "id":document_id,"doc_type":"work","work_type":work_type,"title":title,"privacy":privacy,
            "status":"archived","source_kind":"life","source_ref":"seed","summary":premise,
            "created_at":base}))
        for index in range(chapters):
            day=base-(chapters-1-index)*86400
            outline={"title":title,"premise":premise,"sections":["起点","变化","余韵"],"privacy":privacy}
            await self.store.add_bookshelf_revision(document_id,"outline",json.dumps(outline,ensure_ascii=False),
                                                    "creation_outline",day,set_current=False)
            await self.store.add_bookshelf_revision(document_id,"draft",f"第{index+1}章草稿。",
                                                    "creation_body",day,set_current=False)
            await self.store.add_bookshelf_revision(document_id,"review",
                json.dumps({"accepted":True,"summary":f"第{index+1}章摘要"},ensure_ascii=False),
                "creation_review",day,set_current=False)
            await self.store.add_bookshelf_revision(document_id,"final",
                f"第{index+1}章终稿正文，结尾留在尾部标记TAIL{index+1}。","creation_review",day,status="archived")
        return document_id

    async def test_plaintext_acknowledgement_blocks_pipeline(self):
        self.config.creation.enabled=True
        service=CreationService(self.ctx,self.store,self.config,self.llm,DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"plaintext_not_acknowledged")
        self.assertEqual(await self.store.list_bookshelf_documents(allow_private=True),[])

    async def test_fallback_pipeline_archives_all_stages_and_respects_daily_limit(self):
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        await self.add_inspiration()
        service=CreationService(self.ctx,self.store,self.config,self.llm,DummyLogger())
        result=await service.tick(self.now,"喜欢安静创作",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result["privacy"],"public")
        document=await self.store.get_bookshelf_document(result["document_id"],allow_private=False)
        self.assertEqual(document["status"],"archived")
        self.assertEqual([item["stage"] for item in document["revisions"]],["outline","draft","review","final"])
        self.assertTrue(document["content"])
        again=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(again["status"],"daily_limit")

    async def test_concurrent_creation_cannot_exceed_daily_limit(self):
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        await self.add_inspiration(); service=CreationService(self.ctx,self.store,self.config,self.llm,DummyLogger())
        state=await self.store.get_state(); schedule={"current":{"kind":"leisure"}}
        results=await asyncio.gather(
            service.tick(self.now,"人格",state,schedule,force=True),
            service.tick(self.now,"人格",state,schedule,force=True),
        )
        self.assertEqual(sum(item["status"]=="archived" for item in results),1)
        self.assertEqual(await self.store.archived_work_count(self.now.replace(hour=0).timestamp(),
                                                              (self.now.replace(hour=0)+timedelta(days=1)).timestamp()),1)

    async def test_interrupted_creation_claim_is_recovered(self):
        await self.add_inspiration(); self.assertTrue(await self.store.claim_creation_inspiration("inspiration-life-test"))
        await self.store.create_bookshelf_document({"id":"unfinished","doc_type":"work","work_type":"essay",
            "title":"未完成","privacy":"private","status":"draft","source_kind":"life","source_ref":"test",
            "summary":"","created_at":self.now.timestamp()})
        await self.store.start_creation_run("run-unfinished","inspiration-life-test","unfinished",self.now.timestamp())
        await self.store.recover_creation_claims(self.now.timestamp()+1)
        inspiration=self.store.conn.execute("SELECT status FROM creation_inspirations WHERE id='inspiration-life-test'").fetchone()[0]
        run=self.store.conn.execute("SELECT status FROM creation_runs WHERE id='run-unfinished'").fetchone()[0]
        document=self.store.conn.execute("SELECT status FROM bookshelf_documents WHERE id='unfinished'").fetchone()[0]
        self.assertEqual((inspiration,run,document),("pending","interrupted","failed"))

    async def test_private_work_is_visible_to_owner_not_friend(self):
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        owner=UserProfile(user_id="1",role="owner"); friend=UserProfile(user_id="2",role="friend")
        self.config.users.profiles=[owner,friend]; await self.store.sync_users([owner,friend])
        await self.add_inspiration("dream","private","dream-1")
        service=CreationService(self.ctx,self.store,self.config,self.llm,DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"rest"}},force=True)
        self.assertEqual(result["privacy"],"private")
        shelf=BookshelfService(self.store,self.config)
        self.assertTrue(await shelf.read_for_user(result["document_id"],await self.store.get_user("1")))
        self.assertEqual(await shelf.read_for_user(result["document_id"],await self.store.get_user("2")),{})
        opportunities=await self.store.active_opportunities(self.now.timestamp())
        work=[item for item in opportunities if item.get("privacy")=="owner_only"]
        self.assertEqual(len(work),1); self.assertEqual(work[0]["target_user_id"],"1")

    async def test_read_reference_accepts_list_index_and_full_id(self):
        """书柜阅读支持同源列表的序号；完整 ID 与权限过滤不受影响。"""
        shelf=BookshelfService(self.store,self.config)
        owner=UserProfile(user_id="1",role="owner"); friend=UserProfile(user_id="2",role="friend")
        self.config.users.profiles=[owner]; await self.store.sync_users([owner])
        await self.store.create_bookshelf_document({"id":"work-public","doc_type":"work","work_type":"essay",
            "title":"公开作品","privacy":"public","status":"archived","created_at":1.0})
        await self.store.save_diary("2026-07-12","普通的一天","内容","平稳","digest",3.0)
        user=await self.store.get_user("1")
        # 列表按最近更新排序，与 WebUI 书柜页展示一致：日记(3.0) 在前。
        rows=await shelf.list_for_user(user,20)
        self.assertEqual([row["id"] for row in rows],["diary:2026-07-12","work-public"])
        self.assertEqual(await shelf.resolve_reference("1",user),"diary:2026-07-12")
        self.assertEqual(await shelf.resolve_reference("2",user),"work-public")
        self.assertEqual(await shelf.resolve_reference("9",user),"")
        self.assertEqual(await shelf.resolve_reference(" work-public ",user),"work-public")
        # 朋友视角列表不含私人日记，同一序号映射到可见列表的第一本。
        self.config.users.profiles=[owner,friend]; await self.store.sync_users([owner,friend])
        friend_user=await self.store.get_user("2")
        self.assertEqual(await shelf.resolve_reference("1",friend_user),"work-public")

    async def test_rejected_review_triggers_one_rewrite_then_fail_closed(self):
        """Issue #16：审校拒绝且无修订稿 → 带意见重写一次并复核；仍拒才 fail-closed。"""
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        await self.add_inspiration()
        llm=RejectingReviewLLM()
        service=CreationService(self.ctx,self.store,self.config,llm,DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"failed")
        self.assertIn("已按意见重写一次",str(result.get("error") or ""))
        doc_id=self.store.conn.execute(
            "SELECT document_id FROM creation_runs WHERE status='failed' ORDER BY started_at DESC LIMIT 1").fetchone()[0]
        rows=self.store.conn.execute(
            "SELECT stage FROM bookshelf_revisions WHERE document_id=? ORDER BY created_at,rowid",
            (doc_id,)).fetchall()
        kinds=[r[0] for r in rows]
        self.assertEqual(kinds,["outline","draft","review","draft","review"])  # 重写对 draft/review 各补一条
        self.assertEqual(await self.store.archived_work_count(self.now.replace(hour=0).timestamp(),
                                                              (self.now.replace(hour=0)+timedelta(days=1)).timestamp()),0)

    async def test_rejected_review_with_rewrite_recovers_to_archived(self):
        """Issue #16：重写稿复核通过 → 正常归档，当日额度照计。"""
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        await self.add_inspiration()
        llm=RejectOnceReviewLLM()
        service=CreationService(self.ctx,self.store,self.config,llm,DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived",result)
        self.assertEqual(llm.reviews,2)
        self.assertEqual(len(llm.body_prompts),2)
        self.assertIn("审校意见重写",llm.body_prompts[1])   # 重写 prompt 携带审校意见
        self.assertIn("节奏拖沓",llm.body_prompts[1])
        self.assertEqual(await self.store.archived_work_count(self.now.replace(hour=0).timestamp(),
                                                              (self.now.replace(hour=0)+timedelta(days=1)).timestamp()),1)

    async def test_external_reading_api_ignores_binary_and_stays_private(self):
        self.config.creation.plaintext_storage_acknowledged=True
        self.config.creation.external_reading_enabled=True
        self.config.creation.external_reading_api_name="reader.list_items"
        self.config.creation.reading_annotation_enabled=False
        service=InspirationService(self.ctx,self.store,self.config,self.llm,DummyLogger())
        count=await service.collect(self.now)
        self.assertEqual(count,1); self.assertEqual(self.ctx.api.calls[0][0],"reader.list_items")
        note=self.store.conn.execute("SELECT * FROM reading_notes").fetchone()
        self.assertIsNotNone(note); self.assertNotIn("binary",note.keys())
        documents=await self.store.list_bookshelf_documents(allow_private=True,doc_type="reading_note")
        self.assertEqual(len(documents),1); self.assertEqual(documents[0]["privacy"],"private")
        self.assertNotIn("QUJD",documents[0]["content"])

    async def test_external_reading_rejects_short_base64_and_keeps_plain_text(self):
        """超过 64 字符的纯 base64 特征串一律拒绝，正常文本仍可作为外部阅读素材。"""
        self.config.creation.plaintext_storage_acknowledged=True
        self.config.creation.external_reading_enabled=True
        self.config.creation.external_reading_api_name="reader.list_items"
        self.config.creation.reading_annotation_enabled=False
        ctx=DummyContext(); ctx.api=Base64RejectingAPI()
        service=InspirationService(ctx,self.store,self.config,self.llm,DummyLogger())
        count=await service.collect(self.now)
        self.assertEqual(count,1); self.assertEqual(ctx.api.calls[0][0],"reader.list_items")
        documents=await self.store.list_bookshelf_documents(allow_private=True,doc_type="reading_note")
        self.assertEqual(len(documents),1)
        self.assertNotIn("QUJD",documents[0]["content"])
        self.assertIn("正常文字摘要",documents[0]["content"])
        self.assertEqual(documents[0]["privacy"],"private")

    async def test_diary_is_archived_into_private_bookshelf(self):
        await self.store.save_diary("2026-07-12","普通的一天","今天安静地过完了。","平稳","digest",time.time())
        document=await self.store.get_bookshelf_document("diary:2026-07-12",allow_private=True)
        self.assertEqual(document["doc_type"],"diary"); self.assertEqual(document["privacy"],"private")
        self.assertEqual(await self.store.get_bookshelf_document("diary:2026-07-12",allow_private=False),{})

    async def test_existing_diary_is_backfilled_during_schema_upgrade(self):
        other=tempfile.TemporaryDirectory(); path=Path(other.name)/"mai_life.db"
        conn=sqlite3.connect(path); conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','5');
        CREATE TABLE diary_entries(day TEXT PRIMARY KEY,created_at REAL NOT NULL,title TEXT NOT NULL,
          content TEXT NOT NULL,mood_summary TEXT NOT NULL,privacy TEXT NOT NULL DEFAULT 'private',source_digest TEXT NOT NULL DEFAULT '');
        INSERT INTO diary_entries VALUES('2026-07-10',1,'旧日记','旧日记正文','平稳','private','digest');
        """); conn.commit(); conn.close()
        upgraded=LifeStore(other.name)
        try:
            await upgraded.initialize()
            item=await upgraded.get_bookshelf_document("diary:2026-07-10",allow_private=True)
            self.assertEqual(item["content"],"旧日记正文"); self.assertEqual(item["privacy"],"private")
        finally:
            await upgraded.close(); other.cleanup()

    # ==========书柜续写==========

    async def test_continuation_appends_chapter_to_existing_book(self):
        """续写不建新文档：同一本书追加四条修订，当前修订指向新终稿，且计入当日额度。"""
        service=self.ready_continuation(1.0)
        await self.seed_book()
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result["document_id"],"work-old")
        self.assertTrue(result.get("continued")); self.assertEqual(result.get("chapter"),2)
        self.assertEqual(result["title"],"旧书"); self.assertEqual(result["privacy"],"private")
        document=await self.store.get_bookshelf_document("work-old",allow_private=True)
        self.assertEqual(document["status"],"archived"); self.assertEqual(document["title"],"旧书")
        self.assertEqual([item["stage"] for item in document["revisions"]],
                         ["outline","draft","review","final"]*2)
        self.assertEqual(int(document["current_revision"]),8)
        self.assertEqual(document["revisions"][-1]["stage"],"final")
        self.assertIn("第2章",document["revisions"][-1]["content"])
        self.assertEqual(await self.store.archived_work_count(self.now.replace(hour=0).timestamp(),
            (self.now.replace(hour=0)+timedelta(days=1)).timestamp()),1)
        again=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(again["status"],"daily_limit")

    async def test_continuation_prompt_carries_book_context(self):
        """续写 prompt 带上原书名、原前提、上一章正文尾与章节号，且标题被钉回原书名。"""
        service=self.ready_continuation(1.0)
        await self.seed_book()
        await self.add_inspiration()
        llm=ScriptedLLM()
        service=CreationService(self.ctx,self.store,self.config,llm,DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result["title"],"旧书")
        kinds=[item["kind"] for item in llm.calls]
        self.assertEqual(kinds,["creation_outline","creation_body","creation_review"])
        outline_prompt=llm.calls[0]["prompt"]
        self.assertIn("旧书",outline_prompt); self.assertIn("旧书的前提",outline_prompt)
        self.assertIn("第2章",outline_prompt); self.assertIn("TAIL1",outline_prompt)
        self.assertIn("不是独立新作",outline_prompt)
        body_prompt=llm.calls[1]["prompt"]
        self.assertIn("旧书",body_prompt); self.assertIn("TAIL1",body_prompt)
        self.assertIn("不得概括或复述已写章节",body_prompt)
        review_prompt=llm.calls[2]["prompt"]
        self.assertIn("TAIL1",review_prompt); self.assertIn("与已写章节是否矛盾",review_prompt)
        document=await self.store.get_bookshelf_document("work-old",allow_private=True)
        self.assertEqual(json.loads(document["revisions"][4]["content"])["title"],"旧书")

    async def test_probability_zero_always_creates_new_book(self):
        """probability=0.0 时即使有资格旧书也开新书，旧书修订一条不动（回退路径）。"""
        service=self.ready_continuation(0.0)
        await self.seed_book()
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived")
        self.assertNotEqual(result["document_id"],"work-old")
        self.assertTrue(str(result["document_id"]).startswith("work-"))
        self.assertNotIn("continued",result)
        self.assertEqual(len((await self.store.get_bookshelf_document("work-old",allow_private=True))["revisions"]),4)

    async def test_stale_book_is_not_continued(self):
        """超过停滞天数的书视为完结，probability=1.0 也开新书。"""
        service=self.ready_continuation(1.0)
        self.config.creation.continuation_stale_days=30
        await self.seed_book(last_chapter_at=self.now.timestamp()-31*86400)
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertNotEqual(result["document_id"],"work-old")
        self.assertEqual(len((await self.store.get_bookshelf_document("work-old",allow_private=True))["revisions"]),4)

    async def test_degenerate_gap_window_falls_back_to_new_book(self):
        """停滞天数<=最小间隔时候选窗口为空：续写静默失效，必须回落开新书而非报错。

        两个滑块各自合法（stale ge=1 le=365、gap ge=0 le=30），组合退化时只表现成
        “永远开新书”，用户无从判断是功能坏了；这里钉住回落行为与旧书不被触碰。
        """
        service=self.ready_continuation(1.0)
        self.config.creation.continuation_stale_days=2
        self.config.creation.continuation_min_gap_days=5
        await self.seed_book(last_chapter_at=self.now.timestamp()-3*86400)
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived")
        self.assertNotEqual(result["document_id"],"work-old")
        self.assertFalse(result.get("continued"))
        self.assertEqual(len((await self.store.get_bookshelf_document("work-old",allow_private=True))["revisions"]),4)

    async def test_max_chapters_book_is_not_continued(self):
        """写满章节上限的书不再续写，把机会让给新书。"""
        service=self.ready_continuation(1.0)
        await self.seed_book(chapters=8)
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertNotEqual(result["document_id"],"work-old")
        self.assertEqual(len((await self.store.get_bookshelf_document("work-old",allow_private=True))["revisions"]),32)

    async def test_diary_and_reading_note_documents_are_never_continued(self):
        """日记与阅读笔记文档即使已归档也不进续写候选集，只建 doc_type='work' 新书。"""
        service=self.ready_continuation(1.0)
        # 阳性对照：一本完全合格的 public 旧书，现实参数下必须是唯一候选，
        # 防止断言退化成"过滤把一切挡在外面"的空转（stale_before=0.0/min_gap_before=0.0
        # 会连合格旧书一起排除，那样断言永远为真、验证不了任何过滤）。
        await self.seed_book(document_id="work-old",privacy="public")
        await self.store.save_diary("2026-07-12","普通的一天","今天安静地过完了。","平稳","digest",
                                    self.now.timestamp()-86400)
        await self.store.save_reading_note({"id":"note-1","source_api":"reader.list_items","external_id":"ext-1",
            "title":"阅读笔记","summary":"摘要","annotation":"批注","source_digest":"d",
            "created_at":self.now.timestamp()-86400})
        # 给日记/阅读笔记也补上"昨天的终稿"：让它们在时间闸门下完全合格，
        # 只剩 doc_type='work'/work_type 两层存储过滤——仍不得出现在候选集里。
        for document_id in ("diary:2026-07-12","reading:note-1"):
            await self.store.add_bookshelf_revision(document_id,"final","昨天的终稿正文。",
                "creation_review",self.now.timestamp()-86400,status="archived",set_current=False)
        # 参数与 _select_continuation 一致（停滞 30 天、最小间隔 1 天）。
        now_ts=self.now.timestamp()
        candidates=await self.store.continuable_works(["novel_fragment"],
            stale_before=now_ts-30*86400,max_chapters=8,
            min_gap_before=now_ts-1*86400,limit=50)
        self.assertEqual([item["id"] for item in candidates],["work-old"])
        # private 灵感 + public 旧书：隐私守卫挡住续写，只能开 doc_type='work' 新书。
        await self.add_inspiration("life","private","diary-note")
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived")
        self.assertTrue(str(result["document_id"]).startswith("work-"))
        self.assertNotIn("continued",result)

    async def test_private_book_survives_interrupted_continuation(self):
        """续写运行中被硬打断：有 final 修订的旧书恢复 archived 且不刷新 archived_at，仍留在书柜。"""
        service=self.ready_continuation(1.0)
        await self.seed_book()
        await self.add_inspiration()
        self.assertTrue(await self.store.claim_creation_inspiration("inspiration-life-test"))
        await self.store.start_creation_run("run-cont","inspiration-life-test","work-old",self.now.timestamp())
        await self.store.recover_creation_claims(self.now.timestamp()+1)
        document=await self.store.get_bookshelf_document("work-old",allow_private=True)
        self.assertEqual(document["status"],"archived"); self.assertEqual(len(document["revisions"]),4)
        self.assertEqual(document["archived_at"],self.now.timestamp()-5*86400)
        self.assertEqual([row["id"] for row in await self.store.list_bookshelf_documents(allow_private=True)],
                         ["work-old"])

    async def test_public_book_rejects_private_inspiration(self):
        """public 旧书拒绝 private 灵感；private 旧书接受 public 灵感且隐私继承文档。"""
        service=self.ready_continuation(1.0)
        await self.seed_book(document_id="work-public",privacy="public",
                             last_chapter_at=self.now.timestamp()-4*86400)
        await self.add_inspiration("dream","private","dream-1")
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertNotEqual(result["document_id"],"work-public")
        self.assertNotIn("continued",result); self.assertEqual(result["privacy"],"private")
        self.assertEqual(len((await self.store.get_bookshelf_document("work-public",allow_private=True))["revisions"]),4)
        await self.seed_book(document_id="work-private",privacy="private",
                             last_chapter_at=self.now.timestamp()-5*86400)
        await self.add_inspiration("life","public","second",expires_at=self.now.timestamp()+2*86400)
        tomorrow=self.now+timedelta(days=1)
        result2=await service.tick(tomorrow,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result2["status"],"archived"); self.assertEqual(result2["document_id"],"work-private")
        self.assertEqual(result2.get("chapter"),2); self.assertEqual(result2["privacy"],"private")
        document=await self.store.get_bookshelf_document("work-private",allow_private=True)
        self.assertEqual(document["privacy"],"private"); self.assertEqual(len(document["revisions"]),8)

    async def test_continuation_fallback_archives_four_stages(self):
        """无模型兜底下续写也走完四条修订并归档，兜底标题保持原书名。"""
        service=self.ready_continuation(1.0)
        await self.seed_book()
        await self.add_inspiration()
        service=CreationService(self.ctx,self.store,self.config,OfflineLLM(),DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result["title"],"旧书")
        document=await self.store.get_bookshelf_document("work-old",allow_private=True)
        self.assertEqual([item["stage"] for item in document["revisions"]],
                         ["outline","draft","review","final"]*2)
        self.assertIn("第2章",document["revisions"][5]["content"])
        self.assertEqual(json.loads(document["revisions"][4]["content"])["title"],"旧书")
        self.assertIn("旧书的前提",document["revisions"][4]["content"])

    async def test_continuation_respects_min_gap(self):
        """同一本书一天最多一章：last_chapter_at 是今天且 min_gap_days=1 时改开新书。"""
        service=self.ready_continuation(1.0)
        # 种子的最后一章就归档于今天，本身已占一个当日额度，放大 daily_max 才能走到续写选择。
        self.config.creation.daily_max=2
        await self.seed_book(last_chapter_at=self.now.timestamp())
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertNotEqual(result["document_id"],"work-old")
        self.assertEqual(len((await self.store.get_bookshelf_document("work-old",allow_private=True))["revisions"]),4)

    async def test_min_gap_zero_allows_same_day_continuation(self):
        """min_gap_days=0 是合法取值：放开后同一天也能给旧书续写，且每章仍花一个每日额度。"""
        service=self.ready_continuation(1.0)
        self.config.creation.continuation_min_gap_days=0
        self.config.creation.daily_max=2
        await self.seed_book(last_chapter_at=self.now.timestamp())
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result["document_id"],"work-old")
        self.assertEqual(result.get("chapter"),2)
        # 同日续写也逐章花额度：种子的第 1 章 + 刚续写的第 2 章已占满 daily_max=2，
        # 同一个黄昏再 tick 必须 daily_limit，不能同一天免费续写第 3 章。
        await self.add_inspiration("life","public","second",expires_at=self.now.timestamp()+2*86400)
        again=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(again["status"],"daily_limit")

    async def test_oldest_waiting_book_is_continued_first(self):
        """候选多本时按等最久优先，天然轮转。"""
        service=self.ready_continuation(1.0)
        await self.seed_book(document_id="work-recent",last_chapter_at=self.now.timestamp()-2*86400)
        await self.seed_book(document_id="work-stale",last_chapter_at=self.now.timestamp()-9*86400)
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result["document_id"],"work-stale")

    async def test_empty_work_type_intersection_disables_continuation(self):
        """可续写体裁与作品类型交集为空时等价禁用续写。"""
        service=self.ready_continuation(1.0)
        self.config.creation.work_types=["poem"]
        await self.seed_book()
        await self.add_inspiration()
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertNotEqual(result["document_id"],"work-old")
        self.assertEqual(len((await self.store.get_bookshelf_document("work-old",allow_private=True))["revisions"]),4)

    async def test_public_book_not_continued_when_public_works_disabled(self):
        """关闭"允许公开作品"后 public 旧书不再增厚，新书也只能是 private。"""
        service=self.ready_continuation(1.0)
        self.config.creation.public_works_enabled=False
        await self.seed_book(document_id="work-public",privacy="public")
        await self.add_inspiration("life","public","pub")
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertNotEqual(result["document_id"],"work-public")
        self.assertEqual(result["privacy"],"private")
        self.assertEqual(len((await self.store.get_bookshelf_document("work-public",allow_private=True))["revisions"]),4)

    async def test_continuation_share_opportunity_is_chapter_scoped(self):
        """续写不建新文档，分享契机 id 必须带章节号，否则第 2 章起会被静默丢弃。"""
        service=self.ready_continuation(1.0)
        owner=UserProfile(user_id="1",role="owner")
        self.config.users.profiles=[owner]; await self.store.sync_users([owner])
        await self.seed_book(privacy="private")
        await self.add_inspiration("dream","private","dream-1")
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"archived"); self.assertEqual(result.get("chapter"),2)
        rows=self.store.conn.execute(
            "SELECT framework_id,topic FROM proactive_opportunities WHERE framework_id LIKE 'creation:work-old%'"
        ).fetchall()
        self.assertEqual(len(rows),1); self.assertIn("往下写了一章",rows[0][1])
        await self.add_inspiration("dream","private","dream-2",expires_at=self.now.timestamp()+2*86400)
        tomorrow=self.now+timedelta(days=1)
        result2=await service.tick(tomorrow,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result2.get("chapter"),3)
        rows=self.store.conn.execute(
            "SELECT framework_id FROM proactive_opportunities WHERE framework_id LIKE 'creation:work-old%' ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(rows),2); self.assertEqual(len({row[0] for row in rows}),2)
        self.assertTrue(all("work-old:ch" in row[0] for row in rows))

    async def test_failed_continuation_keeps_book_and_daily_quota(self):
        """审校拒绝导致续写失败：旧书与已写章节完好、不刷新 archived_at、不吃当日额度。"""
        service=self.ready_continuation(1.0)
        await self.seed_book()
        await self.add_inspiration()
        service=CreationService(self.ctx,self.store,self.config,RejectingReviewLLM(),DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"failed")
        document=await self.store.get_bookshelf_document("work-old",allow_private=True)
        # 中间修订是不可变尝试记录，照常落库；但 current_revision 仍指向上章终稿，文档未被碰过。
        # v1.25.0 起：审校拒绝还会带意见重写一次（多一对 draft/review），仍拒才失败。
        self.assertEqual([item["stage"] for item in document["revisions"]],
                         ["outline","draft","review","final","outline","draft","review","draft","review"])
        self.assertEqual(int(document["current_revision"]),4)
        self.assertEqual(document["status"],"archived")
        self.assertEqual(document["archived_at"],self.now.timestamp()-5*86400)
        self.assertEqual(await self.store.archived_work_count(self.now.replace(hour=0).timestamp(),
            (self.now.replace(hour=0)+timedelta(days=1)).timestamp()),0)
        # 当天换一份灵感重试：额度没被失败运行吃掉，续写可以重新开始。
        await self.add_inspiration("life","public","retry")
        retry=CreationService(self.ctx,self.store,self.config,OfflineLLM(),DummyLogger())
        again=await retry.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(again["status"],"archived"); self.assertEqual(again["document_id"],"work-old")
        self.assertEqual(again.get("chapter"),2)

    def test_schema_v9(self):self.assertEqual(SCHEMA_VERSION,14)


if __name__=="__main__":unittest.main()
