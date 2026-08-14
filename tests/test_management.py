from __future__ import annotations

import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from Mai_life.config import MaiLifeSettings,SearchProviderProfile,SocialGroupProfile,UserProfile
from Mai_life.core.storage import LifeStore
from Mai_life.management.admin_service import AdminService


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.now=datetime(2026,7,13,20,0,tzinfo=timezone(timedelta(hours=8)))
        users=[UserProfile(user_id="10001",role="owner",daily_proactive_max=2),
               UserProfile(user_id="10002",role="friend")]
        self.config.users.profiles=users; await self.store.sync_users(users)
        await self.store.update_user_display_name("10001","自动主人昵称")
        self.config.social.groups=[SocialGroupProfile(group_id="20001",observe_enabled=True,relay_target_enabled=True)]
        await self.store.upsert_group_directory("20001","自动读取群名","group-stream",self.now.timestamp())
        await self.store.add_date_candidate("10002","考试","下周","",0.5,"候选摘要",self.now.timestamp())
        await self.store.create_bookshelf_document({"id":"private-work","doc_type":"work","work_type":"essay",
            "title":"私人作品","privacy":"private","status":"archived","source_kind":"dream","source_ref":"1",
            "summary":"私人摘要","created_at":self.now.timestamp()})
        await self.store.add_bookshelf_revision("private-work","final","私人正文","creation_review",
                                                self.now.timestamp(),status="archived")
        await self.store.record_llm_usage(created_at=self.now.timestamp(),source="plugin",task_name="utils",model_name="model",
            request_type="test",prompt_tokens=10,completion_tokens=5,total_tokens=15,latency_ms=1,success=True)
        await self.store.add_proactive_pending("event-1","10001","op-1","stream-1",self.now.timestamp(),self.now.timestamp()+120)
        self.service=AdminService(self.store,self.config)

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_overview_and_scopes_return_expected_aggregates(self):
        overview=await self.service.snapshot("overview",self.now)
        self.assertEqual(overview["users"],2); self.assertEqual(overview["owners"],1)
        self.assertEqual(overview["pending_dates"],1); self.assertEqual(overview["private_documents"],1)
        self.assertEqual(overview["token_total_today"],15); self.assertEqual(overview["pending_proactive"],1)
        groups=await self.service.snapshot("groups",self.now)
        self.assertEqual(groups["items"][0]["group_id"],"20001")
        self.assertEqual(groups["items"][0]["group_name"],"自动读取群名")

    async def test_management_bookshelf_never_returns_body_or_revisions(self):
        data=await self.service.snapshot("bookshelf",self.now); item=data["items"][0]
        self.assertEqual(item["title"],"私人作品")
        self.assertNotIn("content",item); self.assertNotIn("revisions",item); self.assertNotIn("私人正文",str(data))
        await self.store.start_creation_run("run-1","inspiration-1","private-work",self.now.timestamp())
        await self.store.finish_creation_run("run-1","failed",self.now.timestamp()+1,
                                             "https://user:pass@example.com/?key=secret")
        data=await self.service.snapshot("bookshelf",self.now)
        self.assertNotIn("error_summary",str(data)); self.assertNotIn("user:pass",str(data))

    async def test_user_summary_excludes_stream_and_message_content(self):
        await self.store.set_user_stream("10001","sensitive-stream")
        await self.store.update_user_display_name("10001","改名后的昵称")
        await self.store.record_interaction("10001","不应出现在管理摘要中的聊天原文",self.now.timestamp(),20)
        data=await self.service.snapshot("users",self.now); serialized=str(data)
        owner=next(item for item in data["items"] if item["user_id"]=="10001")
        self.assertEqual(owner["display_name"],"改名后的昵称"); self.assertEqual(owner["role"],"owner")
        self.assertNotIn("stream_id",serialized); self.assertNotIn("sensitive-stream",serialized)
        self.assertNotIn("聊天原文",serialized)

    async def test_source_snapshot_only_exposes_key_fingerprint(self):
        self.config.information.enabled=True; self.config.news.enabled=True; self.config.search.enabled=True
        self.config.search_api.providers=[SearchProviderProfile(
            enabled=True,provider_type="openai_chat",api_keys=["top-secret","backup-secret"],
            endpoint="https://user:pass@example.com/top-secret/v1?token=secret",model="backup-secret-model",
        )]
        self.service.update_config(self.config)
        data=await self.service.snapshot("sources",self.now); serialized=str(data)
        self.assertIn("openai_chat",serialized); self.assertIn("key_count': 2",serialized)
        self.assertNotIn("user:pass",serialized); self.assertNotIn("token=",serialized)
        self.assertNotIn("top-secret",serialized); self.assertNotIn("backup-secret",serialized)

    async def test_token_scope_separates_api_requests_from_tokens(self):
        await self.store.record_search_api_event(created_at=self.now.timestamp(),operation="news",provider_id="p1",
            provider_type="bocha",key_fingerprint="abc",success=True,status_code=200,latency_ms=10,
            result_count=5,error_class="")
        data=await self.service.snapshot("tokens",self.now)
        self.assertEqual(data["model_usage"][0]["total_tokens"],15)
        self.assertEqual(data["search_api_usage"][0]["calls"],1)
        text=await self.service.format_text("tokens",self.now)
        self.assertIn("不计作 Token",text)

    async def test_unknown_scope_falls_back_to_overview(self):
        data=await self.service.snapshot("not-a-scope",self.now)
        self.assertEqual(data["scope"],"overview")
        users=await self.service.snapshot("users",self.now,limit="invalid")
        self.assertEqual(len(users["items"]),2)


if __name__=="__main__":unittest.main()
