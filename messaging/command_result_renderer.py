"""把指令文本渲染成与菜单一致的冰蓝玻璃风格结果卡片。"""
from __future__ import annotations

import io
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .menu_renderer import MaiLifeMenuRenderer

try:
    from PIL import Image,ImageDraw
except Exception:  # Pillow 是可选依赖，缺失时集中发送层会降级为纯文本。
    Image=ImageDraw=None


@dataclass(frozen=True,slots=True)
class RenderedCommandPage:
    """保存单页 PNG 与对应文本，便于图片发送失败后只降级未发送部分。"""

    image_bytes:bytes
    plain_text:str


class MaiLifeCommandResultRenderer(MaiLifeMenuRenderer):
    """动态渲染指令结果；长文本自动分页，缓存只保留最近少量结果。"""

    CACHE_LIMIT=16
    MAX_TEXT_CHARS=20_000
    LINES_PER_PAGE=42

    def __init__(self,font_path:str="")->None:
        super().__init__(font_path)
        self._result_cache:OrderedDict[tuple[str,str],tuple[RenderedCommandPage,...]]=OrderedDict()

    @staticmethod
    def _clean_text(text:str)->str:
        value=str(text or "").replace("\x00","").replace("\r\n","\n").replace("\r","\n").strip()
        if not value:value="指令已执行，没有可显示内容。"
        if len(value)>MaiLifeCommandResultRenderer.MAX_TEXT_CHARS:
            value=value[:MaiLifeCommandResultRenderer.MAX_TEXT_CHARS].rstrip()+"\n\n（内容过长，图片结果已在本地保护上限处截断。）"
        return value

    def _wrapped_lines(self,text:str,font:Any,max_width:int)->list[str]:
        """保留段落空行，并按实际字体宽度拆分中英文混排内容。"""
        lines:list[str]=[]
        for raw_line in text.split("\n"):
            if not raw_line.strip():
                lines.append("")
                continue
            lines.extend(self._wrap(raw_line,font,max_width))
        return lines or [""]

    @staticmethod
    def _accent_for(text:str)->str:
        """错误和权限提示使用柔和暖色，其余结果保持主题蓝紫色。"""
        warning_words=("失败","无权","仅对","只有","尚未初始化","未能","没有找到","格式无效","请先")
        return "#B56F83" if any(word in text[:160] for word in warning_words) else "#6678C8"

    def _render_page(self,title:str,lines:list[str],page_index:int,page_count:int,accent:str,
                     fonts:dict[str,Any])->bytes:
        line_height=self._line_height(fonts["body"],9)
        body_height=max(110,len(lines)*line_height+46)
        card_top=286; footer_height=90
        height=max(650,card_top+body_height+footer_height)
        image=self._background(self.WIDTH,height)
        self._shadowed_round_rect(
            image,(42,38,self.WIDTH-42,height-38),28,self._rgba(self._PALETTE["panel"]),
            self._rgba(self._PALETTE["panel_border"]),shadow_alpha=55,blur=22,
        )
        draw=ImageDraw.Draw(image); margin=86
        draw.line((62,108,self.WIDTH-62,108),fill="#D7DFF0",width=2)
        for x,color in ((78,"#F35F60"),(108,"#F4BF4F"),(138,"#4FC26B")):
            draw.ellipse((x-10,72,x+10,92),fill=color)
        self._draw_text(draw,(margin,132),"MAI LIFE  /  COMMAND RESULT",font=fonts["eyebrow"],fill=accent)
        self._draw_text(draw,(margin,171),title,font=fonts["title"],fill=self._PALETTE["title"],stroke_width=2)
        self._draw_text(draw,(margin,229),"本地渲染 · 不调用生图模型",font=fonts["subtitle"],fill=self._PALETTE["muted"])

        badge=f"{page_index + 1} / {page_count}"
        badge_width=max(118,self._text_width(fonts["eyebrow"],badge)+42)
        draw.rounded_rectangle((self.WIDTH-margin-badge_width,148,self.WIDTH-margin,192),radius=12,
                               fill="#E9EDFB",outline="#FFFFFF",width=2)
        self._draw_text(draw,(self.WIDTH-margin-badge_width+20,157),badge,font=fonts["eyebrow"],fill=accent)

        card=(margin,card_top,self.WIDTH-margin,card_top+body_height)
        self._shadowed_round_rect(
            image,card,18,self._rgba(self._PALETTE["card"]),self._rgba(self._PALETTE["card_border"]),
            shadow_alpha=24,blur=11,
        )
        draw=ImageDraw.Draw(image); y=card_top+24
        for line in lines:
            if line:self._draw_text(draw,(margin+28,y),line,font=fonts["body"],fill=self._PALETTE["body"])
            y+=line_height

        footer_y=height-74
        draw.line((margin,footer_y-12,self.WIDTH-margin,footer_y-12),fill="#D6DDEF",width=1)
        self._draw_text(draw,(margin,footer_y),"MAI LIFE  /  LOCAL RESULT CARD",font=fonts["footer"],fill=self._PALETTE["body"])
        footer="图片失败时自动降级文本"
        footer_width=self._text_width(fonts["footer"],footer)
        self._draw_text(draw,(self.WIDTH-margin-footer_width,footer_y),footer,font=fonts["footer"],fill=self._PALETTE["muted"])

        buffer=io.BytesIO(); image.convert("RGB").save(buffer,format="PNG",optimize=True)
        return buffer.getvalue()

    def render(self,text:str,*,title:str="麦麦生活 · 指令结果")->tuple[RenderedCommandPage,...]:
        """生成一页或多页 PNG；字体或 Pillow 异常时返回空元组触发文本降级。"""
        if not self.available or Image is None or ImageDraw is None:return ()
        clean_text=self._clean_text(text); clean_title=" ".join(str(title or "麦麦生活 · 指令结果").split())[:36]
        cache_key=(clean_title,clean_text)
        if cache_key in self._result_cache:
            self._result_cache.move_to_end(cache_key)
            return self._result_cache[cache_key]
        try:
            fonts={
                "eyebrow":self._font(20,bold=True),"title":self._font(44,bold=True),
                "subtitle":self._font(22,bold=True),"body":self._font(25,bold=True),
                "footer":self._font(19,bold=True),
            }
            lines=self._wrapped_lines(clean_text,fonts["body"],self.WIDTH-2*86-56)
            chunks=[lines[index:index+self.LINES_PER_PAGE] for index in range(0,len(lines),self.LINES_PER_PAGE)]
            accent=self._accent_for(clean_text); page_count=len(chunks)
            pages=tuple(
                RenderedCommandPage(
                    image_bytes=self._render_page(clean_title,chunk,index,page_count,accent,fonts),
                    plain_text="\n".join(chunk).strip(),
                )
                for index,chunk in enumerate(chunks)
            )
            self._result_cache[cache_key]=pages
            while len(self._result_cache)>self.CACHE_LIMIT:self._result_cache.popitem(last=False)
            self.last_error=""
            return pages
        except Exception as exc:
            self.last_error=type(exc).__name__
            return ()


__all__=["MaiLifeCommandResultRenderer","RenderedCommandPage"]
