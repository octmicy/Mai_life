"""Structured prompt partitions for planner and replyer."""
from __future__ import annotations

from typing import Any


def relationship_stage(value: float) -> str:
    if value<20:return "陌生"
    if value<40:return "认识"
    if value<60:return "熟悉"
    if value<80:return "亲近"
    return "知己"


class PromptBuilder:
    @staticmethod
    def _segment_text(node: dict[str,Any]|None) -> str:
        if not node:return "无明确安排"
        return f"{node.get('summary','')}（{node.get('location','')}）"

    # Planner 使用完整结构化背景，负责决定是否行动以及行动方向。
    def planner(self, state: dict[str,Any], weather: dict[str,Any], context: dict[str,Any], user: dict[str,Any], dream: dict[str,Any], backlogs: list[str]) -> str:
        temperature=float(user.get("temperature",30)); dream_text=str(dream.get("content") or "无明显梦境余韵")
        backlog_text="；".join(backlogs) if backlogs else "无"
        return (
            "\n【麦麦内在生活状态】\n"
            f"精力 {state.get('energy',70):.0f}/100；饥饿 {state.get('hunger',20):.0f}/100；"
            f"心情 {state.get('mood_valence',0):.2f}；精神活跃度 {state.get('mood_arousal',0.6):.2f}；"
            f"健康 {state.get('health_note','状态正常')}；睡眠阶段 {state.get('sleep_phase','awake')}；"
            f"当前位置感 {state.get('current_location','家里')}；当前真实场景 {state.get('current_activity','自由活动')}；"
            f"周期 {state.get('body_cycle','未启用')}；梦境余韵 {dream_text[:160]}。\n"
            "【独立环境背景】\n"
            f"天气 {weather.get('description','天气未知')}，温度 {weather.get('temperature','未知')}；"
            f"当前日程 {self._segment_text(context.get('current'))}；下一日程 {self._segment_text(context.get('next'))}。\n"
            "【当前网友关系】\n"
            f"关系温度 {temperature:.1f}/100，阶段 {relationship_stage(temperature)}；休息期间未回应摘要：{backlog_text}。\n"
            "这些内容是判断背景，不要求主动提及。不要把天气或日程说成用户刚刚告诉你的事实；"
            "只有“当前真实场景”可以当作麦麦此刻正在做的事。关系阶段只控制自然的熟悉程度，不要情感绑架。\n"
        )

    # Replyer 只接收必要摘要，避免生成机械的状态播报。
    def replyer(self, state: dict[str,Any], weather: dict[str,Any], context: dict[str,Any], user: dict[str,Any], backlogs: list[str]) -> str:
        temperature=float(user.get("temperature",30)); backlog_text="；".join(backlogs) if backlogs else "无"
        return (
            "\n【回复所需生活摘要】\n"
            f"麦麦当前场景：{state.get('current_activity','自由活动')}；位置感：{state.get('current_location','家里')}；"
            f"精力：{state.get('energy',70):.0f}/100；心情：{state.get('mood_valence',0):.2f}；"
            f"关系阶段：{relationship_stage(temperature)}。\n"
            "【独立环境摘要】\n"
            f"天气：{weather.get('description','天气未知')}；当前日程：{self._segment_text(context.get('current'))}。\n"
            f"可能相关的未回应消息：{backlog_text}。背景不相关时不要强行提及，也不要逐项汇报状态。\n"
        )
