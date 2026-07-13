"""Deterministic life-state simulation with narrative dreams."""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any


class LifeStateEngine:
    def __init__(self, store: Any, config: Any, llm: Any, logger: Any) -> None:
        self.store=store; self.config=config; self.llm=llm; self.logger=logger

    def update_config(self, config: Any) -> None:
        self.config=config

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low,min(high,value))

    def _body_cycle(self, now: datetime) -> str:
        cfg=self.config.state
        if not cfg.body_cycle_enabled: return "未启用"
        try:
            start=date.fromisoformat(cfg.body_cycle_start_date)
        except ValueError:
            return "已启用但未配置起始日期"
        offset=(now.date()-start).days % cfg.body_cycle_length_days
        if offset<cfg.body_cycle_period_days: return f"周期第{offset+1}天"
        return f"周期第{offset+1}天，非经期"

    # 状态数值由确定性规则推进，LLM 不得直接改写核心数值。
    async def advance(self, now: datetime, segment: dict[str, Any] | None, scene: dict[str, Any] | None) -> dict[str, Any]:
        state=await self.store.get_state(); runtime=await self.store.get_sleep_runtime()
        now_ts=now.timestamp(); elapsed=max(0.0,min(72.0,(now_ts-float(state.get("last_updated_at",now_ts)))/3600))
        kind=str((segment or {}).get("kind") or "leisure")
        scheduled_sleep=kind in {"sleep","nap"}
        grace=float(runtime.get("awake_grace_until",0))>now_ts
        old_phase=str(runtime.get("phase") or "awake")
        old_sleep_started=float(runtime.get("started_at",now_ts))
        old_sleep_event=str(runtime.get("last_event") or "")
        effective_sleep=scheduled_sleep and not grace
        woke=False; sleep_duration=0.0
        # 睡眠恢复与清醒消耗分支互斥，避免同一时间段重复计算。
        if effective_sleep:
            new_phase="deep_sleep" if kind=="sleep" and elapsed>=0.5 else "light_sleep"
            if old_phase not in {"falling_asleep","light_sleep","deep_sleep","sleeping_again"}:
                runtime.update({"phase":"falling_asleep","started_at":min(now_ts,float(state.get("last_updated_at",now_ts))),"last_event":f"进入{kind}"})
                new_phase="falling_asleep"
            runtime["phase"]=new_phase
            recover=(8.0 if kind=="sleep" else 4.0)*elapsed
            state["energy"]=self._clamp(float(state.get("energy",70))+recover,0,100)
            state["hunger"]=self._clamp(float(state.get("hunger",20))+1.0*elapsed,0,100)
            state["mood_arousal"]=self._clamp(float(state.get("mood_arousal",0.6))-0.2*elapsed,0,1)
        else:
            if old_phase in {"falling_asleep","light_sleep","deep_sleep","sleeping_again"}:
                sleep_duration=max(0,(now_ts-float(runtime.get("started_at",now_ts)))/3600)
                woke=True; runtime.update({"phase":"awake","started_at":now_ts,"last_event":"自然醒来"})
            elif grace:
                runtime["phase"]="woken"
            else:
                runtime["phase"]="awake"
            load={"work":2.1,"study":1.8,"travel":1.4,"leisure":1.0,"meal":0.5,"rest":0.3}.get(kind,1.1)
            state["energy"]=self._clamp(float(state.get("energy",70))-load*elapsed,0,100)
            state["hunger"]=self._clamp(float(state.get("hunger",20))+5.0*elapsed,0,100)
            state["mood_arousal"]=self._clamp(float(state.get("mood_arousal",0.6))+0.05*elapsed,0,1)
        energy=float(state["energy"]); hunger=float(state["hunger"])
        mood=float(state.get("mood_valence",0))
        mood += (-0.03*elapsed if energy<30 else 0.01*elapsed if energy>70 else 0)
        mood += -0.04*elapsed if hunger>75 else 0
        state["mood_valence"]=self._clamp(mood,-1,1)
        if energy<20:
            state["health_status"]="tired"; state["health_note"]="精力很低，需要休息"
        elif hunger>88:
            state["health_status"]="mild_discomfort"; state["health_note"]="有些饿，胃里空空的"
        else:
            state["health_status"]="normal"; state["health_note"]="状态正常"
        state["sleep_phase"]=runtime["phase"]
        state["current_location"]=str((segment or {}).get("location") or state.get("current_location") or "家里")
        state["current_activity"]=str((scene or {}).get("scene") or (segment or {}).get("summary") or "自由活动")
        state["body_cycle"]=self._body_cycle(now)
        state["last_updated_at"]=now_ts
        runtime["last_event"]=runtime.get("last_event","")
        await self.store.save_state(state); await self.store.save_sleep_runtime(runtime)
        if self.config.memory.enabled and woke and sleep_duration>=3 and "nap" not in old_sleep_event:
            await self.generate_dream(state, old_sleep_started, sleep_duration,now)
        return {"state":state,"woke":woke,"sleep_duration":sleep_duration}

    async def advance_timeline(self,now:datetime,timeline:list[dict[str,Any]],
                               final_segment:dict[str,Any]|None,final_scene:dict[str,Any]|None)->dict[str,Any]:
        """按顺序推进离线日程，在节点边界完成睡眠转换和一次性状态增量。"""
        if timeline:
            first_start=timeline[0]["start"]
            state=await self.store.get_state()
            if float(state.get("last_updated_at") or 0)<first_start.timestamp():
                # 超过离线补算上限的旧时间直接截断，防止一次启动循环数百天。
                state["last_updated_at"]=first_start.timestamp(); await self.store.save_state(state)
                runtime=await self.store.get_sleep_runtime()
                if float(runtime.get("started_at") or 0)<first_start.timestamp():
                    runtime["started_at"]=first_start.timestamp(); await self.store.save_sleep_runtime(runtime)
        for span in timeline:
            segment=span.get("segment") if isinstance(span.get("segment"),dict) else None
            await self.advance(span["start"],segment,None)
            await self.advance(span["end"],segment,None)
            completion=span.get("completion") if isinstance(span.get("completion"),dict) else {}
            deltas=completion.get("deltas") if isinstance(completion.get("deltas"),dict) else {}
            if deltas:
                await self.apply_deltas(deltas,updated_at=span["end"].timestamp())
                framework_id=str(completion.get("framework_id") or "")
                if framework_id:await self.store.mark_scene_applied(framework_id)
        return await self.advance(now,final_segment,final_scene)

    # 场景结束时一次性应用增量，并统一限制在合法范围内。
    async def apply_deltas(self, deltas: dict[str, Any], updated_at: float=0) -> dict[str, Any]:
        state=await self.store.get_state()
        for key,low,high in (("energy",0,100),("hunger",0,100),("mood_valence",-1,1),("mood_arousal",0,1)):
            try: delta=float(deltas.get(key,0))
            except (TypeError,ValueError): delta=0
            state[key]=self._clamp(float(state.get(key,0))+delta,low,high)
        state["last_updated_at"]=updated_at or time.time(); await self.store.save_state(state); return state

    # 梦境只负责叙事和轻微余韵，不制造预言或重大健康事件。
    async def generate_dream(self, state: dict[str, Any], sleep_started_at: float, hours: float,
                             woke_at: datetime|None=None) -> None:
        count=int(self.config.memory.dream_fragment_count) if self.config.memory.dream_fragments_enabled else 0
        fallback={"summary":"只记得梦里走过一条被晨光照亮的小路，醒来时细节已经慢慢散掉了。",
                  "fragments":["路边有很轻的风","远处的窗户亮着暖光","醒来前像是听见了水声"][:count],"mood":"calm"}
        result=fallback
        if self.llm.task_available("dream"):
            prompt=(f"麦麦刚结束约{hours:.1f}小时睡眠。当前心情值{state.get('mood_valence',0):.2f}，"
                    f"最近生活场景是{state.get('current_activity','普通日常')}。生成克制自然的醒后梦境，"
                    f"返回JSON：summary为40到120字摘要，fragments为最多{count}个短片段，mood为calm/warm/uneasy之一。"
                    "不要解释，不要写成预言，不要强行出现用户，也不要制造重大健康事件。")
            raw=await self.llm.generate_json(
                prompt,"你只输出合法JSON格式的梦境记录。",fallback,max_tokens=360,
                task_kind="dream",request_type="dream",
            )
            if isinstance(raw,dict):result=raw
        text=str(result.get("summary") or fallback["summary"])[:500]
        raw_fragments=result.get("fragments") if isinstance(result.get("fragments"),list) else []
        fragments=[str(item).strip()[:300] for item in raw_fragments if str(item).strip()][:count]
        mood=str(result.get("mood") or "calm"); mood_delta=0.03 if mood=="warm" else -0.02 if mood=="uneasy" else 0.0
        now=woke_at.timestamp() if woke_at else time.time()
        dream_id=await self.store.add_dream(text,mood_delta,0.5,sleep_started_at,fragments,created_at=now)
        await self.store.add_opportunity({
            "id":f"dream-{dream_id}","framework_id":f"dream:{dream_id}","topic":"昨晚醒来后还记得一点梦",
            "motive":"梦境留下了短暂余韵，可能想向熟悉的网友自然提起",
            "weight":0.46,"privacy":"normal","expires_at":now+12*3600,
        })
        await self.apply_deltas({"mood_valence":mood_delta,"energy":0.5},updated_at=now)

    # 被用户叫醒后设置清醒宽限，避免每条消息反复判醒。
    async def mark_woken(self, now: datetime, reason: str) -> None:
        runtime=await self.store.get_sleep_runtime(); state=await self.store.get_state()
        runtime["phase"]="woken"; runtime["awake_grace_until"]=now.timestamp()+self.config.rest_gate.awake_grace_minutes*60
        runtime["woken_count"]=int(runtime.get("woken_count",0))+1; runtime["last_event"]=reason
        state["sleep_phase"]="woken"; state["energy"]=self._clamp(float(state.get("energy",70))-3,0,100); state["last_updated_at"]=now.timestamp()
        await self.store.save_sleep_runtime(runtime); await self.store.save_state(state)



