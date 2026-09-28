from __future__ import annotations

import base64
import io
import json
import re
import unittest
from pathlib import Path
from typing import Any

from Mai_life.config import MaiLifeSettings
from Mai_life.messaging.command_catalog import COMMAND_SECTIONS,build_command_usage_text
from Mai_life.messaging.command_reply import CommandReplyService
from Mai_life.messaging.command_result_renderer import MaiLifeCommandResultRenderer,RenderedCommandPage
from Mai_life.messaging.menu_renderer import MaiLifeMenuRenderer
from Mai_life.plugin import ADMIN_SCOPE_ALIASES,MaiLifePlugin


COMMAND_CASES:dict[str,tuple[str,str]]={
    "/麦麦":("/麦麦","/mai"),
    "/麦麦状态":("/麦麦状态","/mai_status"),
    "/麦麦日程":("/麦麦日程","/mai_schedule"),
    "/麦麦关系":("/麦麦关系","/mai_relation"),
    "/麦麦撤回":("/麦麦撤回","/mai_recalled"),
    "/麦麦日记":("/麦麦日记","/mai_diary"),
    "/麦麦日期":("/麦麦日期","/mai_dates"),
    "/麦麦添加日期":("/麦麦添加日期 2026-08-01 生日","/mai_date_add 2026-08-01 生日"),
    "/麦麦删除日期":("/麦麦删除日期 1","/mai_date_remove 1"),
    "/麦麦确认日期":("/麦麦确认日期 1 2026-08-01","/mai_date_confirm 1 2026-08-01"),
    "/麦麦新闻":("/麦麦新闻","/mai_news"),
    "/麦麦探索":("/麦麦探索","/mai_explore"),
    "/麦麦书柜":("/麦麦书柜","/mai_bookshelf"),
    "/麦麦阅读":("/麦麦阅读 doc-1","/mai_read doc-1"),
    "/麦麦立即创作":("/麦麦立即创作","/mai_create_now"),
    "/麦麦转述":("/麦麦转述 123456 测试内容","/mai_relay 123456 测试内容"),
    "/麦麦统计":("/麦麦统计","/mai_tokens"),
    "/麦麦管理":("/麦麦管理 来源","/mai_admin sources"),
    "/麦麦配置":("/麦麦配置","/mai_config"),
    "/麦麦帮助":("/麦麦帮助","/mai_help"),
    "/麦麦重生日程":("/麦麦重生日程","/mai_regenerate_schedule"),
    "/麦麦休息测试":("/麦麦休息测试","/mai_rest_test"),
}


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
    def __init__(self,image_result:bool=True,image_results:list[bool]|None=None)->None:
        self.image_result=image_result; self.image_results=list(image_results or [])
        self.images:list[tuple[str,str]]=[]; self.texts:list[dict[str,str]]=[]

    async def image(self,image_data:str,stream_id:str)->bool:
        self.images.append((image_data,stream_id))
        return self.image_results.pop(0) if self.image_results else self.image_result

    async def text(self,**kwargs:str)->bool:
        self.texts.append(dict(kwargs)); return True


class DummyContext:
    def __init__(self,image_result:bool=True,image_results:list[bool]|None=None)->None:
        self.logger=DummyLogger(); self.chat=DummyChat(); self.send=DummySend(image_result,image_results)


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


class EmptyResultRenderer:
    def render(self,*args:Any,**kwargs:Any)->tuple[RenderedCommandPage,...]:
        del args,kwargs
        return ()


class StaticResultRenderer:
    def __init__(self,page_count:int=1)->None:self.page_count=page_count

    def render(self,text:str,**kwargs:Any)->tuple[RenderedCommandPage,...]:
        del kwargs
        return tuple(RenderedCommandPage(f"result-{index}".encode(),f"{text}｜第 {index + 1} 页")
                     for index in range(self.page_count))


class CommandCatalogTests(unittest.TestCase):
    def test_catalog_matches_registered_commands_and_text_is_plain(self):
        catalog={item.command.split()[0] for section in COMMAND_SECTIONS for item in section.items}
        components=MaiLifePlugin().get_components()
        registered={str(item.get("name") or "") for item in components if item.get("type")=="COMMAND"}
        self.assertEqual(catalog|{"/麦麦","/麦麦帮助"},registered)
        text=build_command_usage_text()
        for command in catalog:self.assertIn(command,text)
        self.assertNotIn("/mai",text)
        self.assertNotIn("```",text); self.assertNotIn("**",text); self.assertNotIn("| ---",text)

    def test_chinese_commands_are_public_and_english_forms_remain_compatible(self):
        components={str(item.get("name") or ""):item for item in MaiLifePlugin().get_components()
                    if item.get("type")=="COMMAND"}
        self.assertEqual(set(components),set(COMMAND_CASES))
        for name,(chinese_sample,legacy_sample) in COMMAND_CASES.items():
            pattern=str((components[name].get("metadata") or {}).get("command_pattern") or "")
            self.assertIsNotNone(re.fullmatch(pattern,chinese_sample),name)
            self.assertIsNotNone(re.fullmatch(pattern,legacy_sample),name)
        self.assertEqual(ADMIN_SCOPE_ALIASES["群聊"],"groups")
        self.assertEqual(ADMIN_SCOPE_ALIASES["统计"],"tokens")

    def test_manifest_declares_local_image_and_stream_capabilities(self):
        manifest=json.loads((Path(__file__).parents[1]/"_manifest.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["version"],"1.14.11")
        self.assertIn("send.image",manifest["capabilities"])
        self.assertIn("chat.get_all_streams",manifest["capabilities"])

    def test_renderer_makes_nonblank_cached_png(self):
        renderer=MaiLifeMenuRenderer()
        if not renderer.available or not renderer.regular_font_path:
            self.skipTest("当前环境没有 Pillow 或可用中文字体")
        bundled_font=Path(__file__).parents[1]/"assets"/"font.ttf"
        system_youyuan=Path("C:/Windows/Fonts/SIMYOU.TTF")
        if system_youyuan.is_file() and not bundled_font.is_file():
            self.assertEqual(Path(renderer.regular_font_path).name.casefold(),"simyou.ttf")
            self.assertEqual(Path(renderer.bold_font_path).name.casefold(),"simyou.ttf")
        first=renderer.render("麦生活 · 指令中心",COMMAND_SECTIONS,version="1.11.0")
        second=renderer.render("麦生活 · 指令中心",COMMAND_SECTIONS,version="1.11.0")
        self.assertIs(first,second); self.assertGreater(len(first),10_000)
        from PIL import Image
        with Image.open(io.BytesIO(first)) as image:
            self.assertEqual(image.format,"PNG"); self.assertEqual(image.width,renderer.WIDTH)
            self.assertGreaterEqual(image.height,900)
            extrema=image.convert("RGB").resize((64,64)).getextrema()
            self.assertTrue(all(high-low>20 for low,high in extrema))

    def test_result_renderer_paginates_long_text_and_caches_png_pages(self):
        renderer=MaiLifeCommandResultRenderer()
        if not renderer.available or not renderer.regular_font_path:
            self.skipTest("当前环境没有 Pillow 或可用中文字体")
        text="\n".join(f"第 {index + 1} 行｜"+("较长的状态说明"*10) for index in range(50))
        first=renderer.render(text); second=renderer.render(text)
        self.assertIs(first,second); self.assertGreater(len(first),1)
        for page in first:
            self.assertGreater(len(page.image_bytes),10_000); self.assertTrue(page.plain_text)
            from PIL import Image
            with Image.open(io.BytesIO(page.image_bytes)) as image:
                self.assertEqual(image.format,"PNG"); self.assertEqual(image.width,renderer.WIDTH)
                self.assertGreaterEqual(image.height,650)


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
        plugin._store=DummyStore(); plugin._menu_renderer=EmptyRenderer(); plugin._result_renderer=EmptyResultRenderer()
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

    async def test_admin_without_user_profile_can_use_private_management_commands(self):
        config=MaiLifeSettings.model_validate({"plugin":{"admin_user_ids":["90001"]}})
        ctx=DummyContext(image_result=False); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin.set_plugin_config(config.model_dump(mode="python"))
        plugin._store=DummyStore(); plugin._menu_renderer=EmptyRenderer(); plugin._result_renderer=EmptyResultRenderer()
        common={"user_id":"90001","group_id":"","stream_id":"admin-private","platform":"qq"}

        menu=await plugin.cmd_menu(**common,matched_groups={})
        admin=await plugin.cmd_admin(**common,matched_groups={"scope":"概览"})
        relation=await plugin.cmd_relation(**common)

        self.assertTrue(menu[0]); self.assertTrue(admin[0]); self.assertTrue(relation[0])
        self.assertIn("麦生活 · 指令中心",ctx.send.texts[0]["text"])
        self.assertEqual(ctx.send.texts[1]["text"],"管理服务尚未初始化。")
        self.assertIn("管理员身份已生效",ctx.send.texts[2]["text"])
        self.assertNotIn("私聊用户或私聊管理员",ctx.send.texts[2]["text"])

        await plugin.cmd_menu(
            user_id="90001",group_id="12345",stream_id="group-stream",platform="qq",matched_groups={},
        )
        await plugin.cmd_menu(
            user_id="80001",group_id="",stream_id="other-private",platform="qq",matched_groups={},
        )
        self.assertIn("私聊用户或私聊管理员",ctx.send.texts[-2]["text"])
        self.assertIn("私聊用户或私聊管理员",ctx.send.texts[-1]["text"])

    async def test_regular_command_output_prefers_rendered_images(self):
        ctx=DummyContext(); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore(); plugin._result_renderer=StaticResultRenderer(page_count=2)
        result=await plugin.cmd_status(
            user_id="10001",group_id="",stream_id="stale",platform="qq",
        )
        self.assertEqual(result,(True,"指令结果图片已发送（2 页）",2))
        self.assertEqual([base64.b64decode(item[0]) for item in ctx.send.images],[b"result-0",b"result-1"])
        self.assertEqual(ctx.send.texts,[])

    async def test_result_image_failure_falls_back_to_original_text(self):
        ctx=DummyContext(image_result=False); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore(); plugin._result_renderer=StaticResultRenderer()
        result=await plugin.cmd_status(
            user_id="10001",group_id="",stream_id="stale",platform="qq",
        )
        self.assertEqual(result,(True,"指令结果已发送（文本降级）",2))
        self.assertIn("麦生活尚未初始化",ctx.send.texts[-1]["text"])

    async def test_partial_page_failure_only_falls_back_to_remaining_text(self):
        # 第一页成功；第二页的实时 stream 与原始 stream 两次图片发送均失败。
        ctx=DummyContext(image_result=False,image_results=[True,False,False])
        plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore(); plugin._result_renderer=StaticResultRenderer(page_count=2)
        result=await plugin.cmd_status(
            user_id="10001",group_id="",stream_id="stale",platform="qq",
        )
        self.assertEqual(result,(True,"指令结果已发送（后续页面降级为文本）",2))
        self.assertIn("第 2 页",ctx.send.texts[-1]["text"])
        self.assertNotIn("第 1 页",ctx.send.texts[-1]["text"])


# ==========v1.14.10==========
# 无中文字体时降级纯文本（不再发豆腐块图）+ 字体对象缓存。


class NoFontRenderer(MaiLifeMenuRenderer):
    """模拟系统没有任何中文字体：字体解析返回空路径（Pillow 本身可用）。"""

    def _find_font_paths(self,font_path:str="")->tuple[str,str]:
        return "",""


class NoFontResultRenderer(MaiLifeCommandResultRenderer):
    def _find_font_paths(self,font_path:str="")->tuple[str,str]:
        return "",""


class RendererFontGateTests(unittest.TestCase):
    """v1.14.10 修复 1：无字体时渲染入口直接返回空，交给既有文本降级链。"""

    def test_no_font_menu_returns_empty_bytes(self):
        renderer=NoFontRenderer()
        self.assertEqual(renderer.regular_font_path,"")
        self.assertTrue(renderer.available)  # Pillow 可导入，缺的只是字体
        self.assertIn("未找到中文字体",renderer.last_error)
        self.assertEqual(renderer.render("麦生活 · 指令中心",COMMAND_SECTIONS,version="1.14.10"),b"")

    def test_no_font_result_renderer_returns_empty_pages(self):
        renderer=NoFontResultRenderer()
        self.assertEqual(renderer.render("任意结果",title="麦生活 · 指令结果"),())


class NoFontPluginFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_command_falls_back_to_text_without_font(self):
        ctx=DummyContext(); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore()
        plugin._menu_renderer=NoFontRenderer(); plugin._result_renderer=NoFontResultRenderer()
        result=await plugin.cmd_menu(user_id="10001",group_id="",stream_id="stale",platform="qq",matched_groups={})
        self.assertTrue(result[0]); self.assertEqual(result[2],2)
        self.assertEqual(ctx.send.images,[])
        self.assertTrue(any("麦生活 · 指令中心" in item["text"] for item in ctx.send.texts))

    async def test_regular_command_falls_back_to_text_without_font(self):
        ctx=DummyContext(); plugin=MaiLifePlugin(); plugin._set_context(ctx)
        plugin._store=DummyStore()
        plugin._menu_renderer=NoFontRenderer(); plugin._result_renderer=NoFontResultRenderer()
        result=await plugin.cmd_status(user_id="10001",group_id="",stream_id="stale",platform="qq")
        self.assertEqual(result[2],2)
        self.assertEqual(ctx.send.images,[])
        self.assertTrue(ctx.send.texts)


class RendererFontCacheTests(unittest.TestCase):
    """v1.14.10 修复 2：字体对象按 (字号,粗体) 缓存，冷渲染不再重复解析字体文件。"""

    def setUp(self):
        self.renderer=MaiLifeMenuRenderer()
        if not self.renderer.available or not self.renderer.regular_font_path:
            self.skipTest("当前环境没有 Pillow 或可用中文字体")

    def test_font_objects_are_cached_per_size(self):
        first=self.renderer._font(20,bold=True)
        self.assertIs(first,self.renderer._font(20,bold=True))
        self.assertIsNot(first,self.renderer._font(44,bold=True))

    def test_repeated_cold_renders_do_not_reload_fonts(self):
        import Mai_life.messaging.menu_renderer as menu_module
        real_truetype=menu_module.ImageFont.truetype
        calls={"n":0}
        def counting(path,*,size):
            calls["n"]+=1
            return real_truetype(path,size=size)
        menu_module.ImageFont.truetype=counting
        try:
            self.renderer._font_cache.clear(); self.renderer._cache.clear()
            self.renderer.render("麦生活 · 指令中心",COMMAND_SECTIONS,version="cold-render")
            self.assertGreater(calls["n"],0)
            after_first=calls["n"]
            self.renderer._cache.clear()  # 清掉 PNG 缓存，强制完整重渲（字体缓存保留）
            self.renderer.render("麦生活 · 指令中心",COMMAND_SECTIONS,version="cold-render")
            self.assertEqual(calls["n"],after_first)  # 第二次冷渲染零字体加载
        finally:
            menu_module.ImageFont.truetype=real_truetype


if __name__=="__main__":unittest.main()
