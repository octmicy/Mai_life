"""使用 Pillow 本地渲染冰蓝玻璃风格的命令菜单。"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any,Sequence

import io
import os
import subprocess
import sys

from ..config import PLUGIN_VERSION
from .command_catalog import CommandSection

try:
    from PIL import Image,ImageDraw,ImageFilter,ImageFont
except Exception:  # Pillow 是可选依赖，缺失时命令自动降级为文本。
    Image=ImageDraw=ImageFilter=ImageFont=None


class MaiLifeMenuRenderer:
    """渲染静态命令目录；相同内容会复用 PNG，避免每次命令重复绘图。"""

    # 1320px 在 QQ 压缩后仍能保持清晰，同时不会明显增加单张 PNG 的内存和发送体积。
    WIDTH=1320
    CACHE_LIMIT=8
    _PALETTE={
        "background_left":"#DDF1F8","background_right":"#CFD1F1",
        "panel":"#F7FAFDE8","panel_border":"#FFFFFF",
        "card":"#FFFFFFD8","card_border":"#FFFFFF",
        "title":"#33415F","body":"#59647F","muted":"#79839E",
        "accent":"#6678C8","line":"#D9DFF2",
    }

    def __init__(self,font_path:str="")->None:
        self.regular_font_path,self.bold_font_path=self._find_font_paths(font_path)
        self._cache:OrderedDict[tuple[Any,...],bytes]=OrderedDict()
        self.last_error=""
        if Image is not None and not self.regular_font_path:
            self.last_error=("未找到中文字体，菜单中文将显示为方块。"
                              "Debian/Ubuntu 建议安装 fonts-noto-cjk 或 fonts-wqy-microhei（如 apt install fonts-noto-cjk）。")

    @property
    def available(self)->bool:return Image is not None and ImageDraw is not None and ImageFont is not None

    # 期望字体关键词优先级：圆体 → 圆润黑体 → 现代黑体 → 通用无衬线兜底。
    # 文件名统一小写匹配，顺序靠前的与冰蓝玻璃圆润风格更接近。
    _FONT_KEYWORD_PRIORITY:tuple[str,...]=(
        # 圆体（与幼圆/M PLUS Rounded 圆润风格一致）
        "simyou","youyuan","rounded","mplus",
        # 文泉驿微米黑（圆润、类微软雅黑）
        "microhei",
        # 现代黑体
        "msyh","yahei","notosanssc","notosanscjk","sourcehansans","sourcehanscjk",
        "pingfang","harmonyos","misans","opposans",
        # 文泉驿正黑
        "zenhei",
        # 通用黑体/无衬线兜底
        "simhei","deng","gothic","sans","hei",
    )
    _FONT_SUFFIXES:tuple[str,...]=(".ttf",".ttc",".otf",".otc")

    @staticmethod
    def _system_font_dirs()->list[Path]:
        """返回当前系统常见的字体目录，用于目录扫描兜底。"""
        dirs:list[Path]=[]
        if sys.platform=="win32" or os.name=="nt":
            dirs.append(Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts")
            local=os.environ.get("LOCALAPPDATA")
            if local:dirs.append(Path(local)/"Microsoft"/"Windows"/"Fonts")
        elif sys.platform=="darwin":
            dirs.extend((Path("/System/Library/Fonts"),Path("/Library/Fonts"),Path.home()/"Library"/"Fonts"))
        else:
            dirs.extend((
                Path("/usr/share/fonts"),Path("/usr/local/share/fonts"),
                Path.home()/".fonts",Path.home()/".local"/"share"/"fonts",
            ))
        return dirs

    @classmethod
    def _scan_font_files(cls)->list[Path]:
        """动态列出系统可用字体：优先 fc-list（fontconfig，可限定中文），失败回退目录扫描。"""
        files:list[Path]=[]
        # 1) fontconfig 能精确返回文件路径，且可限定支持中文的字体（Linux/macOS 通常可用）。
        try:
            out=subprocess.run(
                ["fc-list","--format","%{file}\n",":lang=zh"],
                capture_output=True,text=True,timeout=3,
            )
            if out.returncode==0:
                files=[Path(line.strip()) for line in out.stdout.splitlines() if line.strip()]
        except Exception:
            files=[]
        if files:
            return files
        # 2) 目录扫描兜底：覆盖无 fontconfig 的最小环境与 Windows。
        for directory in cls._system_font_dirs():
            try:
                for suffix in cls._FONT_SUFFIXES:
                    files.extend(directory.glob(f"*{suffix}"))
            except Exception:
                continue
        return files

    @classmethod
    def _rank_fonts(cls,files:list[Path])->list[Path]:
        """按关键词优先级排序字体文件；未知字体排在最后，去重保持顺序。"""
        def score(path:Path)->int:
            name=path.name.casefold()
            for index,keyword in enumerate(cls._FONT_KEYWORD_PRIORITY):
                if keyword in name:
                    return index
            return len(cls._FONT_KEYWORD_PRIORITY)+1
        seen:set[str]=set(); ranked:list[Path]=[]
        for path in sorted(set(files),key=score):
            key=str(path).casefold()
            if key in seen:continue
            seen.add(key); ranked.append(path)
        return ranked

    @classmethod
    def _find_font_paths(cls,explicit:str="")->tuple[str,str]:
        """查找可用中文字体：explicit → 插件内置 → 动态扫描 → 已知路径兜底。

        动态扫描优先用 fontconfig 并限定中文，再按“圆体→黑体→无衬线”关键词排序，
        在保持冰蓝玻璃圆润风格的同时适配 Windows / macOS / 各 Linux 发行版与自定义字体目录。
        """
        root=Path(__file__).resolve().parents[1]
        explicit_path=Path(explicit) if explicit and explicit.strip() else Path()
        builtin=[explicit_path,root/"assets"/"font.ttf"]
        scanned=cls._rank_fonts(cls._scan_font_files())
        fallback=[
            Path("C:/Windows/Fonts/SIMYOU.TTF"),Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
            Path("/usr/share/fonts/opentype/noto-cjk/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto-cjk/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy-microhei/wqy-microhei.ttc"),
            Path("/usr/share/fonts/truetype/wqy-zenhei/wqy-zenhei.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        ]
        regular=""
        for path in builtin+scanned+fallback:
            if str(path) not in {"","."} and path.is_file():
                regular=str(path); break
        # bold：优先使用插件内置的独立粗体；否则复用 regular（_draw_text 用描边合成粗体，
        # 保证标题与正文同字体、风格一致，也避免误用不含中文的粗体字体）。
        bundled_bold=root/"assets"/"font-bold.ttf"
        bold=str(bundled_bold) if bundled_bold.is_file() else ""
        return regular,bold or regular

    def _font(self,size:int,*,bold:bool=False)->Any:
        path=self.bold_font_path if bold else self.regular_font_path
        if path:return ImageFont.truetype(path,size=size)
        try:return ImageFont.load_default(size=size)
        except TypeError:return ImageFont.load_default()

    @staticmethod
    def _text_width(font:Any,text:str)->int:
        box=font.getbbox(text or " ")
        return max(0,int(box[2]-box[0]))

    @staticmethod
    def _draw_text(draw:Any,position:tuple[int,int],text:str,*,font:Any,fill:Any,stroke_width:int=1)->None:
        """使用同色描边合成粗体；幼圆没有独立 Bold 字体时也能保持足够字重。"""
        draw.text(position,text,font=font,fill=fill,stroke_width=max(1,int(stroke_width)),stroke_fill=fill)

    @classmethod
    def _wrap(cls,text:str,font:Any,max_width:int)->list[str]:
        value=" ".join(str(text or "").replace("\x00","").split())
        if not value:return [""]
        lines=[]; current=""
        for char in value:
            candidate=current+char
            if current and cls._text_width(font,candidate)>max_width:
                lines.append(current.rstrip()); current=char.lstrip()
            else:current=candidate
        if current or not lines:lines.append(current.rstrip())
        return lines

    @staticmethod
    def _line_height(font:Any,padding:int=0)->int:
        box=font.getbbox("国Ag")
        return max(1,int(box[3]-box[1]))+padding

    @staticmethod
    def _rgba(value:str)->tuple[int,int,int,int]:
        raw=value.lstrip("#")
        if len(raw)==6:raw+="FF"
        return tuple(int(raw[index:index+2],16) for index in range(0,8,2))  # type: ignore[return-value]

    def _background(self,width:int,height:int)->Any:
        image=Image.new("RGBA",(width,height),self._PALETTE["background_left"])
        draw=ImageDraw.Draw(image)
        left=self._rgba(self._PALETTE["background_left"]); right=self._rgba(self._PALETTE["background_right"])
        for x in range(width):
            ratio=x/max(1,width-1)
            color=tuple(round(left[index]*(1-ratio)+right[index]*ratio) for index in range(4))
            draw.line((x,0,x,height),fill=color)
        # 参考图顶部更亮，使用整幅柔和遮罩而不是离散装饰色块。
        veil=Image.new("RGBA",(width,height),(255,255,255,0)); veil_draw=ImageDraw.Draw(veil)
        for y in range(min(height,420)):
            alpha=max(0,55-round(y/420*55)); veil_draw.line((0,y,width,y),fill=(255,255,255,alpha))
        return Image.alpha_composite(image,veil)

    @staticmethod
    def _shadowed_round_rect(image:Any,box:tuple[int,int,int,int],radius:int,fill:Any,outline:Any,
                             *,shadow_alpha:int=42,blur:int=18)->None:
        shadow=Image.new("RGBA",image.size,(0,0,0,0)); draw=ImageDraw.Draw(shadow)
        x1,y1,x2,y2=box
        draw.rounded_rectangle((x1+4,y1+12,x2+4,y2+12),radius=radius,fill=(67,77,126,shadow_alpha))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))
        draw=ImageDraw.Draw(image)
        draw.rounded_rectangle(box,radius=radius,fill=fill,outline=outline,width=2)

    @staticmethod
    def _sparkle(draw:Any,center:tuple[int,int],radius:int,color:Any)->None:
        x,y=center
        draw.polygon(((x,y-radius),(x+radius//4,y-radius//4),(x+radius,y),
                      (x+radius//4,y+radius//4),(x,y+radius),(x-radius//4,y+radius//4),
                      (x-radius,y),(x-radius//4,y-radius//4)),fill=color)

    def _measure_section(self,section:CommandSection,width:int,fonts:dict[str,Any])->tuple[int,list[Any]]:
        inner=width-48; header_height=self._line_height(fonts["section"],5)
        layout=[]; total=24+header_height+14
        for item in section.items:
            command_lines=self._wrap(item.command,fonts["command"],inner)
            description_lines=self._wrap(item.description,fonts["description"],inner)
            item_height=(len(command_lines)*self._line_height(fonts["command"],3)+4+
                         len(description_lines)*self._line_height(fonts["description"],4)+14)
            layout.append((command_lines,description_lines,item_height)); total+=item_height
        return total+10,layout

    def _draw_section(self,image:Any,box:tuple[int,int,int,int],section:CommandSection,
                      layout:list[Any],fonts:dict[str,Any],accent:str)->None:
        """按预计算行高绘制一个命令分组，保证文字不会在绘制阶段改变卡片尺寸。"""
        self._shadowed_round_rect(
            image,box,18,self._rgba(self._PALETTE["card"]),self._rgba(self._PALETTE["card_border"]),
            shadow_alpha=22,blur=10,
        )
        draw=ImageDraw.Draw(image); x1,y1,x2,_y2=box
        draw.rounded_rectangle((x1+18,y1+22,x1+23,y1+54),radius=3,fill=accent)
        self._draw_text(draw,(x1+34,y1+20),section.title,font=fonts["section"],fill=self._PALETTE["title"])
        y=y1+66
        command_h=self._line_height(fonts["command"],3); description_h=self._line_height(fonts["description"],4)
        for index,(item,values) in enumerate(zip(section.items,layout)):
            command_lines,description_lines,item_height=values
            for line in command_lines:
                self._draw_text(draw,(x1+24,y),line,font=fonts["command"],fill=accent); y+=command_h
            y+=2
            for line in description_lines:
                self._draw_text(draw,(x1+24,y),line,font=fonts["description"],fill=self._PALETTE["body"]); y+=description_h
            y+=(max(0,item_height-(len(command_lines)*command_h+2+len(description_lines)*description_h)))
            if index<len(section.items)-1:
                draw.line((x1+24,y-6,x2-24,y-6),fill=self._PALETTE["line"],width=1)

    def render(self,title:str,sections:Sequence[CommandSection],*,version:str=PLUGIN_VERSION,notice:str="")->bytes:
        """测量双栏布局并生成 PNG；任何 Pillow/字体异常都返回空字节触发文本降级。"""
        if not self.available:return b""
        clean_notice=" ".join(str(notice or "").replace("\x00","").split())[:120]
        cache_key=(str(title),str(version),clean_notice,tuple(sections))
        if cache_key in self._cache:
            # 未知子命令会产生不同提示，使用小型 LRU 避免长期运行时无界缓存 PNG。
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        try:
            fonts={
                "eyebrow":self._font(20,bold=True),"title":self._font(47,bold=True),
                "subtitle":self._font(23,bold=True),"section":self._font(27,bold=True),
                "command":self._font(23,bold=True),"description":self._font(20,bold=True),
                "footer":self._font(19,bold=True),
            }
            # 先完整测量两列高度，再创建画布，避免长命令把页脚挤出图片。
            margin=86; gap=22; column_width=(self.WIDTH-2*margin-gap)//2
            section_values=[self._measure_section(section,column_width,fonts) for section in sections]
            columns=[list(range(0,len(sections),2)),list(range(1,len(sections),2))]
            column_heights=[sum(section_values[index][0] for index in column)+gap*max(0,len(column)-1) for column in columns]
            content_top=318 if clean_notice else 282
            height=max(930,content_top+max(column_heights,default=0)+92)
            # 外层窗口、卡片与文字分层绘制，阴影只作用于各自透明图层。
            image=self._background(self.WIDTH,height)
            self._shadowed_round_rect(
                image,(42,38,self.WIDTH-42,height-38),28,self._rgba(self._PALETTE["panel"]),
                self._rgba(self._PALETTE["panel_border"]),shadow_alpha=55,blur=22,
            )
            draw=ImageDraw.Draw(image)
            draw.line((62,108,self.WIDTH-62,108),fill="#D7DFF0",width=2)
            for x,color in ((78,"#F35F60"),(108,"#F4BF4F"),(138,"#4FC26B")):
                draw.ellipse((x-10,72,x+10,92),fill=color)
            self._draw_text(draw,(margin,132),"MAI LIFE  /  LOCAL COMMAND MENU",font=fonts["eyebrow"],fill=self._PALETTE["accent"])
            self._draw_text(draw,(margin,166),str(title or "麦麦生活 · 指令中心"),font=fonts["title"],fill=self._PALETTE["title"],stroke_width=2)
            self._draw_text(draw,(margin,226),"同一条生活时间线，安静记录每一天",font=fonts["subtitle"],fill=self._PALETTE["muted"])
            badge_width=120
            draw.rounded_rectangle((self.WIDTH-margin-badge_width,144,self.WIDTH-margin,188),radius=12,
                                   fill="#E9EDFB",outline="#FFFFFF",width=2)
            self._draw_text(draw,(self.WIDTH-margin-badge_width+22,154),f"v{version}",font=fonts["eyebrow"],fill=self._PALETTE["accent"])
            self._sparkle(draw,(self.WIDTH-105,225),18,"#FFFFFFB8")
            if clean_notice:
                draw.rounded_rectangle((margin,265,self.WIDTH-margin,302),radius=10,fill="#EEF1FC",outline="#FFFFFF",width=1)
                self._draw_text(draw,(margin+16,273),clean_notice,font=fonts["footer"],fill="#6A7195")
            accents=("#6379CE","#796FC1","#548CB2","#8975B5")
            for column_index,column in enumerate(columns):
                x=margin+column_index*(column_width+gap); y=content_top
                for section_index in column:
                    section_height,layout=section_values[section_index]
                    box=(x,y,x+column_width,y+section_height)
                    self._draw_section(image,box,sections[section_index],layout,fonts,accents[section_index%len(accents)])
                    y+=section_height+gap
            footer_y=height-74
            draw.line((margin,footer_y-12,self.WIDTH-margin,footer_y-12),fill="#D6DDEF",width=1)
            self._draw_text(draw,(margin,footer_y),"MAI LIFE  /  COMMAND INDEX",font=fonts["footer"],fill=self._PALETTE["body"])
            footer="私聊用户与管理员可用"
            footer_width=self._text_width(fonts["footer"],footer)
            self._draw_text(draw,(self.WIDTH-margin-footer_width,footer_y),footer,font=fonts["footer"],fill=self._PALETTE["muted"])
            buffer=io.BytesIO(); image.convert("RGB").save(buffer,format="PNG",optimize=True)
            result=buffer.getvalue(); self._cache[cache_key]=result
            while len(self._cache)>self.CACHE_LIMIT:self._cache.popitem(last=False)
            self.last_error=""
            return result
        except Exception as exc:
            self.last_error=type(exc).__name__
            return b""


__all__=["MaiLifeMenuRenderer"]
