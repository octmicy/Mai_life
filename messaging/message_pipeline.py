"""入站消息的意图识别与媒介识别工具。消息收口防抖已移交宿主（v1.24.0）。"""
from __future__ import annotations

import base64
import re
from typing import Any

from .adapter_compat import component_kind, component_text, walk_components


_URGENT_RE=re.compile(r"急事|紧急|救命|出事了|危险|报警|叫醒|醒醒|快醒|撑不住|自杀|轻生",re.I)
_QUIET_RE=re.compile(r"别回|不要回|不用回|继续睡|别醒|别打扰|不用理我",re.I)
_MEDIA_PLACEHOLDER_RE=re.compile(
    r"\[(?:image|图片|emoji|表情包|表情|voice|语音|video|视频|file|文件|reply|forward|unsupported)\]",
    re.I,
)
_PLACEHOLDER_MEDIA={
    "image":re.compile(r"\[(?:image|图片)\]",re.I),
    "emoji":re.compile(r"\[emoji\]",re.I),
    "voice":re.compile(r"\[(?:voice|语音)\]",re.I),
    "video":re.compile(r"\[(?:video|视频)\]",re.I),
    "file":re.compile(r"\[(?:file|文件)\]",re.I),
    "reply":re.compile(r"\[reply\]",re.I),
    "forward":re.compile(r"\[forward\]",re.I),
}


_walk_components=walk_components


def plain_text(message: dict[str,Any]) -> str:
    """提取用户真正输入的文字，忽略两套 QQ 适配器生成的媒介占位符。"""
    processed=message.get("processed_plain_text")
    if isinstance(processed,str) and processed.strip():
        cleaned=" ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",processed).split())
        if cleaned:return cleaned
    values=[]
    for item in _walk_components(message.get("raw_message") or []):
        if component_kind(item)=="text":values.append(component_text(item))
    return " ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",value).strip() for value in values
                    if _MEDIA_PLACEHOLDER_RE.sub(" ",value).strip()).strip()


def direct_text(message:dict[str,Any])->str:
    """只读取顶层用户文字，控制逻辑不得把合并转发里的原话当成用户指令。"""
    values=[]
    raw=message.get("raw_message")
    if isinstance(raw,list):
        for item in raw:
            if isinstance(item,dict) and component_kind(item)=="text":values.append(component_text(item))
    text=" ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",value).strip() for value in values
                  if _MEDIA_PLACEHOLDER_RE.sub(" ",value).strip()).strip()
    if text or isinstance(raw,list):return text
    processed=message.get("processed_plain_text")
    return " ".join(_MEDIA_PLACEHOLDER_RE.sub(" ",str(processed or "")).split())


def media_types(message: dict[str,Any]) -> list[str]:
    found=[]
    display_parts=[str(message.get("processed_plain_text") or "")]
    for item in _walk_components(message.get("raw_message") or []):
        kind=component_kind(item)
        if kind in {"image","voice","video","reply","forward","file","emoji"} and kind not in found:found.append(kind)
        if kind=="text":display_parts.append(component_text(item))
        if kind in ("image","emoji"):
            fmt=str(item.get("format") or item.get("image_format") or "").lower()
            data=str(item.get("binary_data_base64") or item.get("base64") or item.get("base64_data") or "")
            if fmt=="gif" or data.startswith("R0lGOD"):
                if "gif" not in found:found.append("gif")
    display=" ".join(display_parts)
    for kind,pattern in _PLACEHOLDER_MEDIA.items():
        if kind not in found and pattern.search(display):found.append(kind)
    if plain_text(message) and "text" not in found:found.insert(0,"text")
    return found


def classify_intent(text: str, media: list[str]) -> str:
    compact=" ".join(str(text or "").split())
    if _URGENT_RE.search(compact):return "安全或紧急需要"
    recall_compact="".join(compact.lower().split())
    if "撤回" in recall_compact and any(term in recall_compact for term in ("什么","啥","内容","看见","看到","记得","刚才","刚刚")):
        return "询问本人撤回内容"
    if "image" in media or "gif" in media:
        if re.search(r"这(?:张|个)|图里|图片|看得出|是什么|什么意思",compact):return "询问当前图片"
        return "分享图片"
    if re.search(r"怎么办|难受|烦|害怕|焦虑|伤心|撑不住",compact):return "情绪表达或寻求支持"
    if re.search(r"请|帮我|能不能|可以.*吗|麻烦",compact):return "提出请求"
    if "?" in compact or "？" in compact or re.search(r"^(为什么|怎么|什么|谁|哪里|多少|是不是)",compact):return "提出问题"
    return "分享近况或继续话题"


def message_identity(message: dict[str,Any]) -> tuple[str,str,str,bool]:
    info=message.get("message_info") if isinstance(message.get("message_info"),dict) else {}
    user_info=info.get("user_info") if isinstance(info.get("user_info"),dict) else {}
    group_info=info.get("group_info") if isinstance(info.get("group_info"),dict) else {}
    return (
        str(user_info.get("user_id") or ""),str(message.get("session_id") or ""),
        str(message.get("message_id") or ""),not bool(group_info.get("group_id")),
    )


def is_command(message: dict[str,Any]) -> bool:
    return bool(message.get("is_command")) or direct_text(message).lstrip().startswith("/")


def media_bytes(message: dict[str,Any]) -> int:
    total=0
    for item in _walk_components(message.get("raw_message") or []):
        raw=item.get("binary_data_base64") or item.get("base64") or item.get("base64_data") or item.get("image_base64")
        if isinstance(raw,str):
            if "," in raw and raw.startswith("data:"):raw=raw.split(",",1)[1]
            total+=len(raw)*3//4
    return total

