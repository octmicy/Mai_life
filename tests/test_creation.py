from __future__ import annotations

import asyncio
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
    def task_available(self,kind):return kind=="creation_review"
    async def generate_json(self,*args,**kwargs):
        return {"accepted":False,"review_notes":"不适合归档","revised_content":"","summary":""}


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


class CreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.ctx=DummyContext(); self.llm=OfflineLLM()
        self.now=datetime(2026,7,13,16,0,tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def add_inspiration(self,kind="life",privacy="public",ref="test"):
        return await self.store.add_creation_inspiration({
            "id":f"inspiration-{kind}-{ref}","source_kind":kind,"source_ref":ref,
            "prompt_digest":"最近生活里留下的一点安静灵感","privacy_ceiling":privacy,
            "score":0.8,"created_at":self.now.timestamp(),"expires_at":self.now.timestamp()+86400,
        })

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

    async def test_rejected_review_without_revision_is_not_archived(self):
        self.config.creation.enabled=True; self.config.creation.plaintext_storage_acknowledged=True
        await self.add_inspiration()
        service=CreationService(self.ctx,self.store,self.config,RejectingReviewLLM(),DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},force=True)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(await self.store.archived_work_count(self.now.replace(hour=0).timestamp(),
                                                              (self.now.replace(hour=0)+timedelta(days=1)).timestamp()),0)

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

    def test_schema_v7(self):self.assertEqual(SCHEMA_VERSION,7)


if __name__=="__main__":unittest.main()
