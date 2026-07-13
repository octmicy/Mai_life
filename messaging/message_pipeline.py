"""配置私聊的入站消息收口与媒介识别。"""
from __future__ import annotations

import asyncio
import base64
import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .adapter_compat import component_kind, component_text, walk_components


_URGENT_RE=re.compile(r"急事|紧急|救命|出事了|危险|报警|叫醒|醒醒|快醒|撑不住|自杀|轻生",re.I)
_QUIET_RE=re.compile(r"别回|不要回|不用回|继续睡|别醒|别打扰|不用理我",re.I)


_walk_components=walk_components


def plain_text(message: dict[str,Any]) -> str:
    processed=message.get("processed_plain_text")
    if isinstance(processed,str) and processed.strip():return processed.strip()
    values=[]
    for item in _walk_components(message.get("raw_message") or []):
        if component_kind(item)=="text":values.append(component_text(item))
    return " ".join(value.strip() for value in values if value.strip()).strip()


def media_types(message: dict[str,Any]) -> list[str]:
    found=[]
    for item in _walk_components(message.get("raw_message") or []):
        kind=component_kind(item)
        if kind in {"image","voice","video","reply","forward","file","emoji"} and kind not in found:found.append(kind)
        if kind=="image":
            fmt=str(item.get("format") or item.get("image_format") or "").lower()
            data=str(item.get("binary_data_base64") or item.get("base64") or item.get("base64_data") or "")
            if fmt=="gif" or data.startswith("R0lGOD"):
                if "gif" not in found:found.append("gif")
    if plain_text(message) and "text" not in found:found.insert(0,"text")
    return found


def classify_intent(text: str, media: list[str]) -> str:
    compact=" ".join(str(text or "").split())
    if _URGENT_RE.search(compact):return "安全或紧急需要"
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
    return bool(message.get("is_command")) or plain_text(message).lstrip().startswith("/")


def media_bytes(message: dict[str,Any]) -> int:
    total=0
    for item in _walk_components(message.get("raw_message") or []):
        raw=item.get("binary_data_base64") or item.get("base64") or item.get("base64_data") or item.get("image_base64")
        if isinstance(raw,str):
            if "," in raw and raw.startswith("data:"):raw=raw.split(",",1)[1]
            total+=len(raw)*3//4
    return total


@dataclass
class _Burst:
    started: float
    generation: int=0
    messages: list[dict[str,Any]]=field(default_factory=list)
    event: asyncio.Event=field(default_factory=asyncio.Event)


class MessageDebouncer:
    """同一会话允许并发 Hook 进入，但最终只有最新调用继续主链。"""

    def __init__(self, config: Any, logger: Any) -> None:
        self.config=config; self.logger=logger
        self._lock=asyncio.Lock(); self._bursts:dict[str,_Burst]={}; self._closed=False

    def update_config(self, config: Any) -> None:self.config=config

    async def close(self) -> None:
        async with self._lock:
            self._closed=True
            for burst in self._bursts.values():burst.event.set()

    def _quiet_wait(self, messages:list[dict[str,Any]]) -> float:
        types={kind for msg in messages for kind in media_types(msg)}
        cfg=self.config.debounce
        if "forward" in types:return float(cfg.forward_wait_seconds)
        image_count=sum(1 for msg in messages for item in _walk_components(msg.get("raw_message") or []) if component_kind(item)=="image")
        if image_count==1 and not any(plain_text(msg) for msg in messages):return float(cfg.image_wait_seconds)
        return float(cfg.text_wait_seconds)

    @staticmethod
    def _merge(messages:list[dict[str,Any]]) -> dict[str,Any]:
        latest=copy.deepcopy(messages[-1]); combined=[]; texts=[]; ids=[]
        for index,message in enumerate(messages):
            components=copy.deepcopy(message.get("raw_message") or [])
            if index and combined:combined.append({"type":"text","data":"\n"})
            combined.extend(components)
            text=plain_text(message)
            if text:texts.append(text)
            mid=str(message.get("message_id") or "")
            if mid:ids.append(mid)
        latest["raw_message"]=combined
        latest["processed_plain_text"]="\n".join(texts)
        info=latest.setdefault("message_info",{})
        additional=info.setdefault("additional_config",{}) if isinstance(info,dict) else {}
        if isinstance(additional,dict):additional["mai_life_merged_message_ids"]=ids
        return latest

    async def collect(self, message:dict[str,Any]) -> tuple[bool,dict[str,Any],str]:
        """返回 ``(是否继续, 最终消息, 原因)``；旧一代调用会被终止。"""
        cfg=self.config.debounce
        if not cfg.enabled:return True,message,"disabled"
        _uid,session,_mid,_private=message_identity(message)
        if not session:return True,message,"missing_session"
        now=time.monotonic()
        async with self._lock:
            if self._closed:return True,message,"closing"
            burst=self._bursts.get(session)
            if burst is None:
                burst=_Burst(started=now); self._bursts[session]=burst
            else:
                burst.event.set(); burst.event=asyncio.Event()
            burst.messages.append(copy.deepcopy(message)); burst.generation+=1
            generation=burst.generation; event=burst.event
            over_limit=len(burst.messages)>=int(cfg.max_messages) or sum(media_bytes(item) for item in burst.messages)>int(cfg.max_media_bytes)
            text=plain_text(message); immediate=bool(_URGENT_RE.search(text) or _QUIET_RE.search(text) or over_limit)
        while not immediate:
            async with self._lock:
                current=self._bursts.get(session)
                if current is not burst or burst.generation!=generation:return False,message,"superseded"
                if self._closed:break
                quiet=self._quiet_wait(burst.messages)
                remaining=max(0.0,min(quiet,float(cfg.max_wait_seconds)-(time.monotonic()-burst.started)))
                event=burst.event
            if remaining<=0:break
            try:await asyncio.wait_for(event.wait(),timeout=remaining)
            except asyncio.TimeoutError:break
            async with self._lock:
                if burst.generation!=generation:return False,message,"superseded"
        async with self._lock:
            current=self._bursts.get(session)
            if current is not burst or burst.generation!=generation:return False,message,"superseded"
            self._bursts.pop(session,None)
            merged=self._merge(burst.messages)
        return True,merged,f"merged:{len(burst.messages)}"

    @property
    def active_bursts(self) -> int:return len(self._bursts)
