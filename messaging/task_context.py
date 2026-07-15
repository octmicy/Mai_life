"""Track one Mai_life proactive turn across Planner-selected reply anchors."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping


PLUGIN_ID = "maibot-community.mai-life"
HOST_TASK_PREFIX = f"proactive:{PLUGIN_ID}:"

_TASK_BLOCK_RE = re.compile(
    # reason 里可能包含看似结束标签的普通文本；正文必须延伸到 Host 写入的最终结束标签。
    r"<plugin_proactive_task\b(?P<attrs>[^>]*)>(?P<body>.*)</plugin_proactive_task>",
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE_RE = re.compile(r"(?P<name>[a-zA-Z_][\w.-]*)\s*=\s*\"(?P<value>[^\"]*)\"")
_METADATA_LINE_RE = re.compile(r"^附加信息：[ \t]*(?P<value>[^\r\n]+)", re.MULTILINE)


def _content_text(value: Any, *, depth: int = 0) -> str:
    """Read textual prompt blocks without traversing image/base64 payloads."""
    if depth > 5:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_content_text(item, depth=depth + 1) for item in value)))
    if not isinstance(value, Mapping):
        return ""
    kind = str(value.get("type") or "").lower()
    if kind in {"image", "image_url", "audio", "video", "file"}:
        return ""
    for key in ("text", "content"):
        if key in value:
            return _content_text(value.get(key), depth=depth + 1)
    return ""


def _metadata_from_body(body: str) -> dict[str, Any]:
    # Host 在 reason 之后写元数据；取最后一条独立行，避免 reason 内的同名文本抢先匹配。
    matches = list(_METADATA_LINE_RE.finditer(body))
    if not matches:
        return {}
    raw = matches[-1].group("value").lstrip()
    try:
        value, _end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class PluginTaskMarker:
    task_id: str
    plugin_id: str
    metadata: dict[str, Any]


def latest_plugin_task_marker(messages: Any) -> PluginTaskMarker | None:
    """读取最后一条由 Host 直接放入历史的完整主动任务标记。"""
    if not isinstance(messages, list):
        return None
    latest: PluginTaskMarker | None = None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        # Host 虚拟任务正文以标签开头；不扫描普通聊天正文，避免用户文本伪造任务标签。
        text = _content_text(message.get("content")).lstrip()
        match = _TASK_BLOCK_RE.match(text)
        if not match:
            continue
        attrs = {item.group("name"): item.group("value") for item in _ATTRIBUTE_RE.finditer(match.group("attrs"))}
        task_id = str(attrs.get("id") or "").strip()
        plugin_id = str(attrs.get("plugin_id") or "").strip()
        if task_id and plugin_id:
            latest = PluginTaskMarker(task_id=task_id, plugin_id=plugin_id,
                                      metadata=_metadata_from_body(match.group("body")))
    return latest


@dataclass(slots=True)
class ActivePluginTask:
    session_id: str
    task_id: str
    kind: str
    record_id: str
    opportunity_id: str
    created_at: float
    retain_until: float
    status: str = "pending"
    reply_anchor: str = ""
    reply_reserved: bool = False
    direct_anchor: str = ""
    direct_send_reserved: bool = False
    sent: bool = False


class ActiveTaskRegistry:
    """Serialize task attribution within a session while preserving real quote IDs."""

    def __init__(self, retention_seconds: int = 180) -> None:
        self._retention_seconds = max(20, int(retention_seconds))
        self._active: dict[str, ActivePluginTask] = {}
        self._last_inbound_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def update_retention(self, seconds: int) -> None:
        self._retention_seconds = max(20, int(seconds))

    async def reset(self) -> None:
        async with self._lock:
            self._active.clear()
            self._last_inbound_at.clear()

    async def clear_active(self, session_id: str) -> ActivePluginTask | None:
        """仅清理当前任务，不把同会话后续新任务视为已被用户消息打断。"""
        if not session_id:return None
        async with self._lock:
            item=self._active.pop(session_id,None)
            return replace(item) if item else None

    async def note_inbound(self, session_id: str, now: float) -> ActivePluginTask | None:
        """真实入站消息开始新轮次前，结束该会话旧主动任务的运行时归因。"""
        if not session_id:
            return None
        async with self._lock:
            previous = self._active.pop(session_id, None)
            self._last_inbound_at[session_id] = max(float(now), self._last_inbound_at.get(session_id, 0.0))
            return replace(previous) if previous else None

    def _prune_locked(self, now: float) -> None:
        expired = [session for session, item in self._active.items() if item.retain_until <= now]
        for session in expired:
            self._active.pop(session, None)

    async def activate(
        self,
        session_id: str,
        marker: PluginTaskMarker,
        *,
        kind: str,
        record: Mapping[str, Any],
        now: float,
    ) -> ActivePluginTask | None:
        """只激活能与持久层候选精确对应的 Host 任务，并恢复短期已发送墓碑。"""
        if not session_id or marker.plugin_id != PLUGIN_ID or not marker.task_id.startswith(HOST_TASK_PREFIX):
            return None
        created_at = float(record.get("created_at") or 0)
        status = str(record.get("status") or "pending")
        sent_at = float(record.get("sent_at") or 0)
        record_id = str(record.get("id") or "")
        if not record_id:
            return None
        if status == "sent":
            # Runner 热重载后仍短暂恢复已发送任务的墓碑，拦住 Host 对 Bot 输出的二次回复。
            if sent_at <= 0 or sent_at + self._retention_seconds <= now:
                return None
            retain_until = sent_at + self._retention_seconds
        elif status in {"pending", "sending"} and float(record.get("expires_at") or 0) > now:
            retain_until = max(float(record.get("expires_at") or 0), now + self._retention_seconds)
        else:
            return None
        async with self._lock:
            self._prune_locked(now)
            existing = self._active.get(session_id)
            if existing and existing.task_id == marker.task_id:
                existing.kind = kind
                existing.record_id = record_id
                existing.opportunity_id = str(record.get("opportunity_id") or existing.opportunity_id)
                existing.status = status
                existing.sent = existing.sent or status == "sent"
                existing.retain_until = max(existing.retain_until, retain_until)
                return replace(existing)
            # 新入站消息会使 Planner 历史里更早的 pending 任务失效，禁止旧任务重新获得发送权。
            if created_at <= self._last_inbound_at.get(session_id, 0.0):
                return None
            item = ActivePluginTask(
                session_id=session_id,
                task_id=marker.task_id,
                kind=kind,
                record_id=record_id,
                opportunity_id=str(record.get("opportunity_id") or ""),
                created_at=created_at,
                retain_until=retain_until,
                status=status,
                sent=status == "sent",
            )
            self._active[session_id] = item
            return replace(item)

    async def current(self, session_id: str, now: float) -> ActivePluginTask | None:
        if not session_id:
            return None
        async with self._lock:
            self._prune_locked(now)
            item = self._active.get(session_id)
            return replace(item) if item else None

    async def reserve_reply(self, session_id: str, task_id: str, anchor: str, now: float) -> bool:
        async with self._lock:
            self._prune_locked(now)
            item = self._active.get(session_id)
            if not item or item.task_id != task_id or item.sent or item.reply_reserved:
                return False
            item.reply_anchor = anchor
            item.reply_reserved = True
            return True

    async def release_reply(self, session_id: str, task_id: str, anchor: str) -> None:
        async with self._lock:
            item = self._active.get(session_id)
            if not item or item.task_id != task_id or item.sent or item.reply_anchor != anchor:
                return
            item.reply_anchor = ""
            item.reply_reserved = False

    async def reserve_send(self, session_id: str, task_id: str, anchor: str, now: float) -> bool:
        """允许同一 Replyer 结果的正常分段，但无 Replyer 锚点的直接发送只能占用一次。"""
        async with self._lock:
            self._prune_locked(now)
            item = self._active.get(session_id)
            if not item or item.task_id != task_id:
                return False
            if item.sent:
                return bool(item.reply_reserved and item.reply_anchor == anchor)
            if item.reply_reserved:
                return item.reply_anchor == anchor
            if item.direct_send_reserved:
                return False
            item.direct_anchor = anchor
            item.direct_send_reserved = True
            return True

    async def finish_send(
        self,
        session_id: str,
        task_id: str,
        anchor: str,
        sent: bool,
        now: float,
    ) -> ActivePluginTask | None:
        """提交平台发送结果；失败释放本次预留，成功则保留墓碑阻止后续重复回复。"""
        async with self._lock:
            self._prune_locked(now)
            item = self._active.get(session_id)
            if not item or item.task_id != task_id:
                return None
            if sent:
                item.sent = True
                item.status = "sent"
                item.retain_until = max(item.retain_until, now + self._retention_seconds)
            elif not item.sent:
                if item.reply_reserved and item.reply_anchor == anchor:
                    item.reply_reserved = False
                    item.reply_anchor = ""
                if item.direct_send_reserved and item.direct_anchor == anchor:
                    item.direct_send_reserved = False
                    item.direct_anchor = ""
            return replace(item)

    @staticmethod
    def planner_instruction(task: ActivePluginTask) -> str:
        return (
            "\n【Mai_life 主动任务约束】\n"
            f"当前主动任务编号是 {task.task_id}。若决定调用 reply，请优先把 msg_id 设为该任务编号，"
            "不要改为较早的用户消息或 Bot 自己刚发出的消息。一次任务最多产生一轮可见发送；"
            "发送后等待用户回应，不要自行解释或连续追发。插件仍会在本地按会话任务防重。\n"
        )
