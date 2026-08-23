from __future__ import annotations

import base64
import io
import json
import unittest
from pathlib import Path
from typing import Any

from Mai_life.messaging.command_catalog import COMMAND_SECTIONS,build_command_usage_text
from Mai_life.messaging.command_reply import CommandReplyService
from Mai_life.messaging.menu_renderer import MaiLifeMenuRenderer
from Mai_life.plugin import MaiLifePlugin


class DummyLogger:
    def __getattr__(self,name:str):
        del name
        return lambda *args,**kwargs:None


class DummyChat:
    def __init__(self)->None:self.exact_stream="live-private"

    async def get_stream_by_user_id(self,user_id:str,platform:str="qq")->dict[str,Any]:
        return {"success":True,"stream":{"stream_id":self.exact_stream,"user_id":user_id,"platform":platform}}

    async def get_stream_by_group_id(self,group_id:str,platform:str="qq")->dict[str,Any]:
        return {"success":True,"stream":{"stream_id":f"live-group-{group_id}","group_id":group_id,"platform":platform}}

    async def get_private_streams(self,platform:str="qq")->dict[str,Any]:
        return {"success":True,"streams":[{"stream_id":self.exact_stream,"user_id":"10001","platform":platform}]}

    async def get_group_streams(self,platform:str="qq")->dict[str,Any]:
        return {"success":True,"streams":[]}

    async def get_all_streams(self,platform:str="qq")->dict[str,Any]:
        return await self.get_private_streams(platform)

    async def open_session(self,**kwargs:Any)->dict[str,Any]:
        del kwargs
        return {"success":True,"session_id":self.exact_stream}


class DummySend:
    def __init__(self,image_result:bool=True)->None:
        self.image_result=image_result; self.images:list[tuple[str,str]]=[]; self.texts:list[dict[str,str]]=[]

    async def image(self,image_data:str,stream_id:str)->bool:
        self.images.append((image_data,stream_id)); return self.image_result

    async def text(self,**kwargs:str)->bool:
        self.texts.append(dict(kwargs)); return True


class DummyContext:
    def __init__(self,image_result:bool=True)->None:
        self.logger=DummyLogger(); self.chat=DummyChat(); self.send=DummySend(image_result)


class DummyStore:
    async def get_user(self,user_id:str)->dict[str,Any]:
        return {"user_id":user_id,"enabled":True,"role":"friend"} if user_id=="10001" else {}


class EmptyRenderer:
    def render(self,*args:Any,**kwargs:Any)->bytes:
        del args,kwargs
        return b""


class StaticRenderer:
    def render(self,*args:Any,**kwargs:Any)->bytes:
        del args,kwargs
        return b"menu-png"


class CommandCatalogTests(unittest.TestCase):
    def test_catalog_matches_registered_commands_and_text_is_plain(self):
        catalog={item.command.split()[0] for section in COMMAND_SECTIONS for item in section.items}
        components=MaiLifePlugin().get_components()
        registered={str(item.get("name") or "") for item in components if item.get("type")=="COMMAND"}
        self.assertEqual(catalog|{"/mai","/mai_help"},registered)
        text=build_command_usage_text()
        for command in catalog:self.assertIn(command,text)
        self.assertNotIn("```",text); self.assertNotIn("**",text); self.assertNotIn("| ---",text)

    def test_manifest_declares_local_image_and_stream_capabilities(self):
        manifest=json.loads((Path(__file__).parents[1]/"_manifest.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["version"],"1.7.2")
        self.assertIn("send.image",manifest["capabilities"])
        self.assertIn("chat.get_all_streams",manifest["capabilities"])

    def test_renderer_makes_nonblank_cached_png(self):
        renderer=MaiLifeMenuRenderer()
        if not renderer.available or not renderer.regular_font_path:
            self.skipTest("当前环境没有 Pillow 或可用中文字体")
        first=renderer.render("麦麦生活 · 指令中心",COMMAND_SECTIONS,version="1.7.2")
        second=renderer.render("麦麦生活 · 指令中心",COMMAND_SECTIONS,version="1.7.2")
        self.assertIs(first,second); self.assertGreater(len(first),10_000)
        from PIL import Image
        with Image.open(io.BytesIO(first)) as image:
            self.assertEqual(image.format,"PNG"); self.assertEqual(image.width,renderer.WIDTH)
            self.assertGreaterEqual(image.height,900)
            extrema=image.convert("RGB").resize((64,64)).getextrema()
            self.assertTrue(all(high-low>20 for low,high in extrema))


class CommandReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_resolves_live_stream_and_sends_base64_png(self):
        ctx=DummyContext(); service=CommandReplyService(ctx,ctx.logger)
        self.assertEqual(await service.resolve_live_stream_id("stale","10001"),"live-private")
        self.assertTrue(await service.send_image_bytes_with_fallback(b"png-data","stale","10001"))
        payload,stream_id=ctx.send.images[-1]
        self.assertEqual(base64.b64decode(payload),b"png-data"); self.assertEqual(stream_id,"live-private")

    async def test_direct_string_stream_result_is_supported(self):
        ctx=DummyContext()

        async def direct_stream(**kwargs:Any)->str:
            del kwargs
            return "direct-live-stream"

        ctx.chat.get_stream_by_user_id=direct_stream  # type: ignore[method-assign]
        service=CommandReplyService(ctx,ctx.logger)
        self.assertEqual(await service.resolve_live_stream_id("stale","10001"),"direct-live-stream")

    async def test_menu_commands_return_sdk_triples_and_text_fallback(self):
        ctx=DummyContext(image_result=False); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore(); plugin._menu_renderer=EmptyRenderer()
        common={"user_id":"10001","group_id":"","stream_id":"stale","platform":"qq"}
        menu=await plugin.cmd_menu(**common,matched_groups={})
        help_result=await plugin.cmd_help(**common)
        unknown=await plugin.cmd_menu(**common,matched_groups={"content":"不存在"})
        for result in (menu,help_result,unknown):
            self.assertEqual(len(result),3); self.assertEqual(result[2],2)
        self.assertTrue(menu[0]); self.assertTrue(help_result[0]); self.assertFalse(unknown[0])
        self.assertGreaterEqual(len(ctx.send.texts),3)
        self.assertTrue(all(item["stream_id"]=="live-private" for item in ctx.send.texts))
        self.assertIn("未识别子命令",ctx.send.texts[-1]["text"])

    async def test_menu_command_prefers_image_when_available(self):
        ctx=DummyContext(); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore(); plugin._menu_renderer=StaticRenderer()
        result=await plugin.cmd_menu(
            user_id="10001",group_id="",stream_id="stale",platform="qq",matched_groups={},
        )
        self.assertEqual(result,(True,"命令菜单图片已发送",2))
        self.assertEqual(len(ctx.send.images),1); self.assertEqual(ctx.send.texts,[])
        self.assertEqual(base64.b64decode(ctx.send.images[0][0]),b"menu-png")


if __name__=="__main__":unittest.main()
