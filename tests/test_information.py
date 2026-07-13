from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from Mai_life.config import MaiLifeSettings,NewsSourceProfile
from Mai_life.core.storage import LifeStore,SCHEMA_VERSION
from Mai_life.information.feed_parser import parse_feed,readable_text
from Mai_life.information.http_client import HttpClient
from Mai_life.information.information_service import InformationService
from Mai_life.information.news_service import NewsService
from Mai_life.information.search_service import SearchService


class DummyLogger:
    def __getattr__(self,name):return lambda *args,**kwargs:None


class OfflineLLM:
    def task_available(self,kind):return False


class DummyAPI:
    def __init__(self):self.calls=[]
    async def call(self,name,**kwargs):
        self.calls.append((name,kwargs)); return {"items":[{"id":"b1","title":"新视频","url":"https://example.com/b1","summary":"一个创作记录"}]}


class DummyContext:
    def __init__(self):self.api=DummyAPI()


class LocalHandler(BaseHTTPRequestHandler):
    counts={}
    def log_message(self,format,*args):pass
    def do_GET(self):
        path=self.path.split("?",1)[0]; self.counts[path]=self.counts.get(path,0)+1
        if path=="/feed.xml":
            port=self.server.server_port
            body=f'''<?xml version="1.0"?><rss version="2.0"><channel><title>测试源</title><item>
            <guid>item-1</guid><title>本地科技新闻</title><link>http://127.0.0.1:{port}/article</link>
            <description><![CDATA[<p>这是订阅摘要，包含可以验证的内容。</p>]]></description><pubDate>Mon, 13 Jul 2026 01:00:00 GMT</pubDate>
            </item></channel></rss>'''.encode()
            self.send_response(200); self.send_header("Content-Type","application/rss+xml; charset=utf-8"); self.end_headers(); self.wfile.write(body); return
        if path=="/article":
            body="<html><article><h1>本地科技新闻</h1><p>这是一段可以从正文中读取的完整测试内容，用来验证正文优先策略。</p></article></html>".encode()
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body); return
        if path=="/search":
            body=json.dumps({"results":[{"title":"科技创作的新方法","url":"https://example.com/a","content":"介绍创作工具与日常实践。"}]}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(body); return
        self.send_response(503); self.end_headers(); self.wfile.write(b"unavailable")


class InformationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        LocalHandler.counts={}; self.server=ThreadingHTTPServer(("127.0.0.1",0),LocalHandler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_port}"; self.now=datetime(2026,7,13,10,0,tzinfo=timezone(timedelta(hours=8)))

    async def asyncTearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        await self.store.close(); self.tmp.cleanup()

    def test_feed_and_html_parsers_are_dependency_free(self):
        rss=b"<rss><channel><item><title>A</title><link>https://example.com/a</link><description>&lt;p&gt;hello world content&lt;/p&gt;</description></item></channel></rss>"
        entries=parse_feed(rss); self.assertEqual(entries[0].title,"A"); self.assertIn("hello world",entries[0].summary)
        self.assertEqual(readable_text("<script>bad()</script><p>useful article paragraph</p>",100),"useful article paragraph")

    async def test_news_refresh_prefers_readable_article_and_uses_cache_interval(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.news.enabled=True
        config.news.sources=[NewsSourceProfile(source_id="local",url=self.base+"/feed.xml")]
        service=NewsService(DummyContext(),self.store,config,HttpClient(DummyLogger()),DummyLogger())
        changed=await service.refresh_due(self.now); again=await service.refresh_due(self.now+timedelta(minutes=1))
        items=await self.store.recent_news_items(self.now.timestamp(),5)
        self.assertEqual(changed,1); self.assertEqual(again,0); self.assertEqual(len(items),1)
        self.assertIn("完整测试内容",items[0]["content"]); self.assertEqual(LocalHandler.counts["/feed.xml"],1)

    async def test_failed_source_enters_persistent_backoff(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.news.enabled=True
        config.news.sources=[NewsSourceProfile(source_id="bad",url=self.base+"/unavailable")]
        service=NewsService(DummyContext(),self.store,config,HttpClient(DummyLogger()),DummyLogger())
        await service.refresh_due(self.now); await service.refresh_due(self.now+timedelta(minutes=1))
        runtime=await self.store.get_information_source_runtime("news:bad")
        self.assertEqual(runtime["failure_count"],1); self.assertGreater(runtime["next_retry_at"],self.now.timestamp())
        self.assertEqual(LocalHandler.counts["/unavailable"],1)

    async def test_bilibili_plugin_api_source_is_normalized(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.news.enabled=True
        config.news.sources=[NewsSourceProfile(source_id="bili",source_type="bilibili_api",api_name="other.bili_updates")]
        ctx=DummyContext(); service=NewsService(ctx,self.store,config,HttpClient(DummyLogger()),DummyLogger())
        self.assertEqual(await service.refresh_due(self.now),1)
        self.assertEqual(ctx.api.calls[0][0],"other.bili_updates")
        self.assertEqual((await self.store.recent_news_items(self.now.timestamp(),5))[0]["title"],"新视频")

    async def test_json_search_and_exploration_note(self):
        config=MaiLifeSettings(); config.information.enabled=True; config.search.enabled=True
        config.search.endpoint=self.base+"/search"; config.information.association_threshold=0.2
        service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        schedule={"current":{"kind":"leisure","summary":"看看新东西"},"next":None}
        result=await service.explore_due(self.now,"喜欢科技和创作",await self.store.get_state(),schedule,[])
        notes=await self.store.recent_exploration_notes(self.now.timestamp(),5)
        self.assertTrue(result); self.assertEqual(len(notes),1); self.assertTrue(notes[0]["source_urls"])
        self.assertEqual(await self.store.exploration_count(self.now.replace(hour=0).timestamp(),(self.now.replace(hour=0)+timedelta(days=1)).timestamp()),1)
        opportunities=await self.store.active_opportunities(self.now.timestamp())
        self.assertTrue(opportunities); self.assertEqual(opportunities[0]["privacy"],"external")

    async def test_default_disabled_tick_performs_no_network(self):
        config=MaiLifeSettings(); service=InformationService(DummyContext(),self.store,config,OfflineLLM(),DummyLogger())
        result=await service.tick(self.now,"人格",await self.store.get_state(),{"current":{"kind":"leisure"}},[])
        self.assertEqual(result,{"news":0,"associated":0,"search":0}); self.assertEqual(LocalHandler.counts,{})

    async def test_search_query_privacy_filter(self):
        value=InformationService._safe_query("搜索 @张三 123456789 https://private.example.com 和 test@example.com 的消息")
        self.assertNotIn("张三",value); self.assertNotIn("123456789",value); self.assertNotIn("private.example",value); self.assertNotIn("test@example",value)

    def test_schema_v4(self):self.assertEqual(SCHEMA_VERSION,4)


if __name__=="__main__":unittest.main()
