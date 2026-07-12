"""Passive reply rest gate."""
from __future__ import annotations

import random
import re
from typing import Any


class RestGate:
    def __init__(self, store: Any, config: Any, llm: Any, state_engine: Any, logger: Any) -> None:
        self.store=store; self.config=config; self.llm=llm; self.state_engine=state_engine; self.logger=logger

    def update_config(self, config: Any) -> None:
        self.config=config

    @staticmethod
    # 明确勿扰和紧急叫醒优先于随机或模型判断。
    def boundary(text: str) -> tuple[str,str]:
        compact=re.sub(r"\s+","",text.lower())
        if re.search(r"(?:别回|不用回|不要回|继续睡|别醒|别打扰|安心睡|不用理我)",compact):
            return "block","用户明确希望继续休息"
        if re.search(r"(?:醒醒|快醒|叫醒|起床|紧急|救命|出事了|很难受|撑不住|危险|报警|急事)",compact):
            return "wake","明确叫醒或紧急需求"
        return "judge","普通消息"

    # 返回 True 表示继续 MaiBot 被动回复链，False 表示本轮保持睡眠。
    async def decide(self, user_id: str, text: str, now: Any, segment: dict[str,Any]|None) -> tuple[bool,str]:
        cfg=self.config.rest_gate
        if not cfg.enabled: return True,"disabled"
        if text.lstrip().startswith("/"): return True,"command"
        kind=str((segment or {}).get("kind") or "")
        if kind not in set(cfg.gate_segment_types): return True,"not_rest_segment"
        runtime=await self.store.get_sleep_runtime()
        if float(runtime.get("awake_grace_until",0))>now.timestamp(): return True,"awake_grace"
        action,reason=self.boundary(text)
        if action=="block": return False,reason
        if action=="wake":
            await self.state_engine.mark_woken(now,reason); return True,reason
        if cfg.mode=="llm":
            prompt=(f"麦麦正处于{kind}。判断是否应该被这条消息叫醒。消息：{text[:800]}\n"
                    "只返回JSON：{\"score\":0-100,\"should_reply\":true/false,\"reason\":\"一句话\"}。"
                    "普通闲聊应继续睡；明确叫醒、紧急支持才建议醒来。")
            result=await self.llm.generate_json(prompt,"你是休息醒来判定器，只输出JSON。",{},max_tokens=180)
            try: score=int(float(result.get("score",0))) if isinstance(result,dict) else 0
            except (TypeError,ValueError): score=0
            allowed=score>=cfg.llm_threshold and bool(result.get("should_reply",False))
            reason=f"llm:{score}/{cfg.llm_threshold}:{str(result.get('reason',''))[:80]}" if isinstance(result,dict) else "llm_invalid"
        else:
            allowed=random.random()<=cfg.wake_probability; reason=f"probability:{cfg.wake_probability:.2f}"
        if allowed: await self.state_engine.mark_woken(now,reason)
        return allowed,reason
