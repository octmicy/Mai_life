from __future__ import annotations

import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from Mai_life.config import MaiLifeSettings,NewsSourceProfile,SocialRelationProfile,UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.management.admin_service import AdminService


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.now=datetime(2026,7,13,20,0,tzinfo=timezone(timedelta(hours=8)))
        users=[UserProfile(user_id="1",role="owner",display_name="主人"),
               UserProfile(user_id="2",role="friend",display_name="朋友")]
        self.config.users.profiles=users; await self.store.sync_users(users)
        await self.store.sync_relationship_entries([
            SocialRelationProfile(group_alias="朋友群",alias="小明",user_id="3",display_name="小明")
        ])
        await self.store.add_date_candidate("2","考试","下周","",0.5,"候选摘要",self.now.timestamp())
        await self.store.create_bookshelf_document({"id":"private-work","doc_type":"work","work_type":"essay",
            "title":"私人作品","privacy":"private","status":"archived","source_kind":"dream","source_ref":"1",
            "summary":"私人摘要","created_at":self.now.timestamp()})
        await self.store.add_bookshelf_revision("private-work","final","私人正文","creation_review",
                                                self.now.timestamp(),status="archived")
        await self.store.record_llm_usage(created_at=self.now.timestamp(),source="plugin",task_name="utils",model_name="model",
            request_type="test",prompt_tokens=10,completion_tokens=5,total_tokens=15,latency_ms=1,success=True)
        await self.store.add_proactive_pending("event-1","1","op-1","stream-1",self.now.timestamp(),self.now.timestamp()+120)
        self.service=AdminService(self.store,self.config)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_overview_and_scopes_return_expected_aggregates(self):
        overview=await self.service.snapshot("overview",self.now)
        self.assertEqual(overview["users"],2); self.assertEqual(overview["owners"],1)
        self.assertEqual(overview["pending_dates"],1); self.assertEqual(overview["private_documents"],1)
        self.assertEqual(overview["token_total_today"],15); self.assertEqual(overview["pending_proactive"],1)
        relations=await self.service.snapshot("relations",self.now)
        self.assertEqual(relations["items"][0]["alias"],"小明")

    async def test_management_bookshelf_never_returns_body_or_revisions(self):
        data=await self.service.snapshot("bookshelf",self.now)
        item=data["items"][0]
        self.assertEqual(item["title"],"私人作品")
        self.assertNotIn("content",item); self.assertNotIn("revisions",item)
        self.assertNotIn("私人正文",str(data))
        await self.store.start_creation_run("run-1","inspiration-1","private-work",self.now.timestamp())
        await self.store.finish_creation_run("run-1","failed",self.now.timestamp()+1,
                                             "https://user:pass@example.com/?key=secret")
        data=await self.service.snapshot("bookshelf",self.now)
        self.assertNotIn("error_summary",str(data)); self.assertNotIn("user:pass",str(data))

    async def test_user_summary_excludes_stream_and_message_content(self):
        await self.store.set_user_stream("1","sensitive-stream")
        await self.store.record_interaction("1","不应出现在管理摘要中的聊天原文",self.now.timestamp(),20)
        data=await self.service.snapshot("users",self.now)
        serialized=str(data)
        self.assertNotIn("stream_id",serialized); self.assertNotIn("sensitive-stream",serialized)
        self.assertNotIn("聊天原文",serialized)

    async def test_source_snapshot_strips_credentials_query_and_api_key(self):
        self.config.information.enabled=True; self.config.news.enabled=True; self.config.search.enabled=True
        self.config.news.sources=[NewsSourceProfile(source_id="secure",name="源",url="https://user:pass@example.com/feed?token=secret")]
        self.config.search.endpoint="https://search.example.com/api?key=secret"; self.config.search.api_key="top-secret"
        self.service.update_config(self.config)
        data=await self.service.snapshot("sources",self.now)
        serialized=str(data)
        self.assertIn("https://example.com/feed",serialized); self.assertNotIn("user:pass",serialized)
        self.assertNotIn("token=",serialized); self.assertNotIn("top-secret",serialized)

    async def test_unknown_scope_falls_back_to_overview(self):
        data=await self.service.snapshot("not-a-scope",self.now)
        self.assertEqual(data["scope"],"overview")
        users=await self.service.snapshot("users",self.now,limit="invalid")
        self.assertEqual(len(users["items"]),2)


if __name__=="__main__":unittest.main()
