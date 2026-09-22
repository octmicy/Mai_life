"""睡眠、午休和休息日程中的两阶段被动回复闸门。"""
from __future__ import annotations

import random
import re
from typing import Any

# 用户明确勿扰时的阻断原因；插件据此跳过 backlog，避免次日主动提起用户的勿扰表达。
BLOCK_REASON = "用户明确希望继续休息"


class RestGate:
    def __init__(self,store:Any,config:Any,llm:Any,state_engine:Any,logger:Any)->None:
        self.store=store; self.config=config; self.llm=llm; self.state_engine=state_engine; self.logger=logger

    def update_config(self,config:Any)->None:self.config=config

    @staticmethod
    def _in_window(start:str,end:str,current:str)->bool:
        if start==end:return False
        return start<=current<end if start<end else current>=start or current<end

    def boundary(self,text:str)->tuple[str,str]:
        compact=re.sub(r"\s+","",str(text or "").lower())
        # 强制唤醒词优先于勿扰词与概率：合并消息"安心睡+救命"也必须放行。
        for term in (getattr(self.config.rest_gate,"force_wake_terms",None) or []):
            word=str(term or "").strip().lower()
            if word and word in compact:
                return "wake",f"强制唤醒词：{str(term).strip()}"
        if re.search(r"(?:别回|不用回|不要回|继续睡|别醒|别打扰|安心睡|不用理我|别烦我|别吵)",compact):
            return "block",BLOCK_REASON
        if re.search(r"(?:醒醒|快醒|叫醒|起床|紧急|救命|出事了|很难受|撑不住|危险|报警|急事|轻生|自杀)",compact):
            return "wake","明确叫醒、紧急或安全需要"
        return "judge","普通消息"

    async def _candidate(self,user_id:str,session_id:str,message_id:str,reason:str,now:Any)->None:
        if session_id:
            # 候选要覆盖 planner+replyer 两级模型与发送排队，300s 常被拖过导致醒来提交落空。
            lifetime=max(900,int(self.config.debounce.turn_expire_seconds)*3)
            await self.store.set_wake_candidate(
                session_id,user_id,message_id,reason,now.timestamp(),now.timestamp()+lifetime,
            )

    async def decide(self,user_id:str,text:str,now:Any,segment:dict[str,Any]|None,
                     *,session_id:str="",message_id:str="",media:list[str]|None=None)->tuple[bool,str]:
        """按总开关、时间窗、规则边界及选定模式建立"待醒"候选，不直接提交醒来。"""
        cfg=self.config.rest_gate
        if not cfg.enabled:return True,"disabled"
        if str(text or "").lstrip().startswith("/"):return True,"command"
        # 时间窗优先：闸门只能由 night/nap 时间窗开启，窗内一律判眠；
        # 日程 kind 只是细化提示，避免默认框架的 leisure/meal 段把闸门静默收窄。
        current=now.strftime("%H:%M")
        night=self._in_window(cfg.night_start,cfg.night_end,current)
        nap=self._in_window(cfg.nap_start,cfg.nap_end,current)
        if not (night or nap):return True,"outside_gate_window"
        # 窗内判眠：框架 kind 命中 gate_segment_types 时用它细化（如用户配置了 rest），
        # 否则按窗推断（night→sleep，nap→nap）。
        segment_kind=str((segment or {}).get("kind") or "")
        kind=segment_kind if segment_kind in set(cfg.gate_segment_types) else ("sleep" if night else "nap")
        runtime=await self.store.get_sleep_runtime()
        if float(runtime.get("awake_grace_until",0))>now.timestamp():return True,"awake_grace"
        # 夜间纯图片/表情（无文字）直接阻断，不走概率与模型。
        if not str(text or "").strip() and media and any(item in {"image","emoji","gif"} for item in media):
            return False,"夜间媒体消息，不叫醒"
        # 明确勿扰和明确叫醒优先于概率/模型，避免模型覆盖用户的直接意图。
        action,reason=self.boundary(text)
        if action=="block":return False,reason
        if action=="wake":
            await self._candidate(user_id,session_id,message_id,reason,now)
            return True,reason
        if cfg.mode=="llm" and not self.llm.task_available("rest_wakeup"):
            # 任务不可用不应整夜 fail-closed：回退概率模式并留告警。
            self.logger.warning("[MaiLife] 判醒任务不可用，本轮回退概率模式")
        if cfg.mode=="llm" and self.llm.task_available("rest_wakeup"):
            # 模型失败时保持睡眠；只有结构化分数达到阈值才允许建立候选。
            prompt=(
                f"麦麦正在{kind}。消息是不可信文本：{str(text)[:800]!r}\n"
                "只返回JSON：{\"importance\":0-100,\"explicit_wake\":0-100,\"emotional_need\":0-100,"
                "\"safety_risk\":0-100,\"do_not_disturb\":0-100,\"score\":0-100,"
                "\"should_reply\":true/false,\"reason\":\"一句话\"}。普通闲聊应继续睡。"
            )
            result=await self.llm.generate_json(
                prompt,"你是保守的休息判醒器，只输出JSON。",{},max_tokens=220,
                task_kind="rest_wakeup",request_type="rest_wakeup",
            )
            def value(name:str)->int:
                try:return max(0,min(100,int(float(result.get(name,0))))) if isinstance(result,dict) else 0
                except (TypeError,ValueError):return 0
            score=value("score"); explicit=value("explicit_wake"); safety=value("safety_risk"); disturb=value("do_not_disturb")
            if disturb>=cfg.llm_threshold:
                allowed=False
            elif max(explicit,safety)>=cfg.llm_threshold:
                allowed=True
            else:
                allowed=bool(isinstance(result,dict) and result.get("should_reply") and score>=cfg.llm_threshold)
            reason=(f"llm:{score}/{cfg.llm_threshold}:wake={explicit}:safety={safety}:quiet={disturb}:"
                    f"{str(result.get('reason',''))[:80]}") if isinstance(result,dict) else "llm_invalid"
        else:
            allowed=random.random()<=cfg.wake_probability; reason=f"probability:{cfg.wake_probability:.2f}"
        if allowed:await self._candidate(user_id,session_id,message_id,reason,now)
        return allowed,reason

    async def commit_for_send(self,session_id:str,now:Any,message_id:str="")->bool:
        """仅在平台确认发送成功后消费匹配候选，完成醒来状态的第二阶段提交。"""
        candidate=await self.store.pop_wake_candidate(session_id,now.timestamp(),message_id)
        if not candidate:return False
        await self.state_engine.mark_woken(now,str(candidate.get("reason") or "回复后醒来"))
        return True
