"""RSS/Atom 与可读 HTML 正文解析。"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any


def _local(tag:str)->str:return tag.rsplit("}",1)[-1].lower()


def _clean_space(value:str)->str:return " ".join(html.unescape(str(value or "")).split())


class _TextExtractor(HTMLParser):
    def __init__(self)->None:
        super().__init__(convert_charrefs=True); self.parts=[]; self._skip=0
    def handle_starttag(self,tag:str,attrs:list[tuple[str,str|None]])->None:
        if tag in {"script","style","noscript","svg","nav","footer","header"}:self._skip+=1
        elif not self._skip and tag in {"p","br","li","h1","h2","h3","article","section"}:self.parts.append("\n")
    def handle_endtag(self,tag:str)->None:
        if tag in {"script","style","noscript","svg","nav","footer","header"} and self._skip:self._skip-=1
        elif not self._skip and tag in {"p","li","article","section"}:self.parts.append("\n")
    def handle_data(self,data:str)->None:
        if not self._skip and data.strip():self.parts.append(data)


def readable_text(raw_html:str,max_chars:int)->str:
    parser=_TextExtractor()
    try:parser.feed(raw_html)
    except Exception:return ""
    lines=[]
    for line in "".join(parser.parts).splitlines():
        clean=_clean_space(line)
        if len(clean)>=12 and clean not in lines:lines.append(clean)
    return "\n".join(lines)[:max(0,int(max_chars))]


def html_to_text(raw_html:str,max_chars:int=3000)->str:
    return readable_text(raw_html,max_chars)


def _published(value:str)->float:
    text=str(value or "").strip()
    if not text:return 0
    try:
        parsed=parsedate_to_datetime(text)
        return parsed.timestamp()
    except (TypeError,ValueError,OverflowError):pass
    try:return datetime.fromisoformat(text.replace("Z","+00:00")).timestamp()
    except ValueError:return 0


def _child_text(node:ET.Element,names:set[str])->str:
    for child in list(node):
        if _local(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def _atom_link(node:ET.Element)->str:
    fallback=""
    for child in list(node):
        if _local(child.tag)!="link":continue
        href=str(child.attrib.get("href") or child.text or "").strip()
        if not href:continue
        rel=str(child.attrib.get("rel") or "alternate")
        if rel=="alternate":return href
        fallback=fallback or href
    return fallback


@dataclass(frozen=True)
class FeedEntry:
    entry_id:str
    title:str
    url:str
    summary:str
    content:str
    published_at:float


def parse_feed(data:bytes)->list[FeedEntry]:
    try:root=ET.fromstring(data)
    except ET.ParseError:return []
    entries=[]; root_kind=_local(root.tag)
    nodes=[node for node in root.iter() if _local(node.tag)==("entry" if root_kind=="feed" else "item")]
    for node in nodes:
        title=_clean_space(_child_text(node,{"title"}))
        url=_atom_link(node) if root_kind=="feed" else _clean_space(_child_text(node,{"link"}))
        entry_id=_clean_space(_child_text(node,{"id","guid"})) or url or title
        summary_html=_child_text(node,{"summary","description"})
        content_html=_child_text(node,{"content","encoded"})
        summary=html_to_text(summary_html,3000) or _clean_space(re.sub(r"<[^>]+>"," ",summary_html))
        content=html_to_text(content_html,8000)
        published=_published(_child_text(node,{"published","updated","pubdate","date"}))
        if title or url or summary:
            entries.append(FeedEntry(entry_id,title or "未命名条目",url,summary,content,published))
    return entries
