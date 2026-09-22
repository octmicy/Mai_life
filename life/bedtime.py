"""睡前流程：入睡推迟点维护与被叫醒后的重新入睡。

本模块只维护“何时真正入睡”这一件事，不主动发言——睡前晚安由
Replyer 在自然回复里说出（见 prompt_builder 的 approaching 注入）。
两条规则：
1. 夜窗前有对话时，入睡点推迟到“最后一条私聊 + BEDTIME_SILENCE_MINUTES”，
   每条新消息都重新推（v1.14.6 需求④）；
2. 被叫醒后宽限（awake_grace_minutes）到期且仍在休息窗内，相位重新入睡。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# 夜窗前多少分钟开始进入“睡前氛围”（Replyer 被注入收尾/晚安提示）。
GOODNIGHT_LEAD_MINUTES = 10
# 入睡需要“最后一条私聊后静默”的分钟数。
BEDTIME_SILENCE_MINUTES = 10
# 睡前流程检查循环粒度（秒）。
BEDTIME_LOOP_SECONDS = 60

_SLEEP_PHASES = {"falling_asleep", "light_sleep", "deep_sleep"}


def _in_window(start: str, end: str, current: str) -> bool:
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


class BedtimeManager:
    def __init__(self, ctx: Any, store: Any, config: Any, environment: Any,
                 state_engine: Any, logger: Any) -> None:
        self.ctx = ctx; self.store = store; self.config = config
        self.environment = environment; self.state_engine = state_engine; self.logger = logger

    def _night_start_ts(self, now: datetime, start: str, end: str) -> float:
        """当前/即将到来的这一晚夜窗开始时刻。

        跨午夜窗（如 22:30-08:00）的凌晨按前一晚计算；夜窗开始前（晚安 lead 期）
        取今晚即将开始的时刻，不能误减一天。
        """
        hour, minute = (int(part) for part in str(start or "00:00").split(":")[:2])
        today = datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo).timestamp()
        if today <= now.timestamp():
            return today
        if _in_window(start, end, now.strftime("%H:%M")):
            return today - 86400
        return today

    def _night_span(self, now: datetime, night_start: str, night_end: str) -> tuple[float, float]:
        """夜窗前 lead 分钟到夜窗结束的时刻区间；lead 期本身在窗外，不能按 HH:MM 窗口判。"""
        start = self._night_start_ts(now, night_start, night_end) - GOODNIGHT_LEAD_MINUTES * 60
        hour, minute = (int(part) for part in str(night_end or "08:00").split(":")[:2])
        end = datetime(now.year, now.month, now.day, hour, minute, tzinfo=now.tzinfo).timestamp()
        if end <= start:
            end += 86400
        return start, end

    async def tick(self, now: datetime) -> None:
        runtime = await self.store.get_sleep_runtime()
        phase = str(runtime.get("phase") or "")
        await self._update_sleep_defer(now, runtime, phase)
        await self._check_resleep(now, runtime, phase)

    async def _update_sleep_defer(self, now: datetime, runtime: dict[str, Any], phase: str) -> None:
        cfg = self.config.rest_gate
        current = now.strftime("%H:%M")
        defer = 0.0
        # 只在夜窗内、她还没睡着也没被叫醒时协商入睡点；睡眠期/宽限期内一律为 0，
        # 防止被闸门拦下的消息（同样更新 last_user_message_at）把入睡点重新推开。
        if cfg.enabled and phase not in _SLEEP_PHASES and phase != "woken" \
                and _in_window(cfg.night_start, cfg.night_end, current):
            users = await self.store.list_users()
            # 麦麦是单一生命：任一用户还在聊，她就还没睡。
            latest = max((float(user.get("last_user_message_at") or 0) for user in users), default=0.0)
            floor = self._night_start_ts(now, cfg.night_start, cfg.night_end)
            defer = max(floor, latest + BEDTIME_SILENCE_MINUTES * 60)
        if float(runtime.get("sleep_defer_until", 0) or 0) != defer:
            runtime["sleep_defer_until"] = defer
            await self.store.save_sleep_runtime(runtime)

    async def _check_resleep(self, now: datetime, runtime: dict[str, Any], phase: str) -> None:
        """宽限到期且仍在休息窗内：从 woken 重新入睡（与宽限共用 awake_grace_minutes）。"""
        if phase != "woken":
            return
        if float(runtime.get("awake_grace_until", 0) or 0) > now.timestamp():
            return
        cfg = self.config.rest_gate
        current = now.strftime("%H:%M")
        if not (_in_window(cfg.night_start, cfg.night_end, current)
                or _in_window(cfg.nap_start, cfg.nap_end, current)):
            return  # 窗外交给日程推进自然转 awake，不强制
        await self.state_engine.mark_resleep(now)

    def approaching(self, now: datetime) -> bool:
        """是否处于夜窗前的睡前氛围期（Replyer 注入收尾/晚安提示）。"""
        cfg = self.config.rest_gate
        if not cfg.enabled:
            return False
        start, end = self._night_span(now, cfg.night_start, cfg.night_end)
        return start <= now.timestamp() < end
