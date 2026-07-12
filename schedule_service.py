"""Daily framework generation and near-term scene expansion."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ALLOWED={"meal","work","study","travel","leisure","sleep","nap","rest"}


def to_minute(text: str) -> int | None:
    try:
        h,m=map(int,text.split(":"));
        if 0<=h<=23 and 0<=m<=59: return h*60+m
    except (ValueError,AttributeError):
        pass
    return None


def hhmm(value: int) -> str:
    value=max(0,min(1439,value)); return f"{value//60:02d}:{value%60:02d}"


class ScheduleService:
    def __init__(self, store: Any, config: Any, llm: Any, plugin_dir: str, logger: Any) -> None:
        self.store=store; self.config=config; self.llm=llm; self.plugin_dir=Path(plugin_dir); self.logger=logger

    def update_config(self, config: Any) -> None:
        self.config=config

    def _template(self) -> dict[str, Any]:
        path=self.plugin_dir/self.config.schedule.template_file
        try:
            data=json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data,dict) else {}
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 日程模板读取失败: {exc}"); return {}

    def _fallback(self, day: str, weekend: bool) -> list[dict[str, Any]]:
        template=self._template(); key="weekend" if weekend else "workday"; raw=template.get(key)
        if not isinstance(raw,list):
            raw=[
                {"start":"00:00","end":"08:00","kind":"sleep","summary":"安稳睡觉","location":"卧室","energy_load":8,"shareability":0.05},
                {"start":"08:00","end":"09:00","kind":"meal","summary":"起床洗漱并吃早餐","location":"家里","energy_load":-1,"shareability":0.25},
                {"start":"09:00","end":"12:00","kind":"work","summary":"处理自己的事情","location":"书桌前","energy_load":-5,"shareability":0.25},
                {"start":"12:00","end":"13:00","kind":"meal","summary":"准备午饭","location":"厨房","energy_load":-1,"shareability":0.4},
                {"start":"13:00","end":"13:40","kind":"nap","summary":"短暂午休","location":"卧室","energy_load":3,"shareability":0.05},
                {"start":"13:40","end":"18:00","kind":"study","summary":"继续学习和整理东西","location":"书桌前","energy_load":-5,"shareability":0.25},
                {"start":"18:00","end":"19:00","kind":"meal","summary":"做晚饭","location":"厨房","energy_load":-1,"shareability":0.55},
                {"start":"19:00","end":"23:30","kind":"leisure","summary":"放松、看东西和随便逛逛","location":"家里","energy_load":-3,"shareability":0.55},
                {"start":"23:30","end":"23:59","kind":"sleep","summary":"准备睡觉","location":"卧室","energy_load":1,"shareability":0.05},
            ]
        return self._validate(day,raw)

    # 所有 LLM 日程必须经过时间、类型、重叠和必要节点校验。
    def _validate(self, day: str, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw,list): return []
        cleaned=[]
        for index,item in enumerate(raw):
            if not isinstance(item,dict): continue
            start=to_minute(str(item.get("start") or item.get("time") or "")); end=to_minute(str(item.get("end") or ""))
            if start is None: continue
            if end is None: end=min(1439,start+60)
            if end<=start: continue
            kind=str(item.get("kind") or "leisure").lower(); kind=kind if kind in _ALLOWED else "leisure"
            summary=str(item.get("summary") or item.get("activity") or "自由活动")[:160]
            location=str(item.get("location") or "家里")[:80]
            try: energy=float(item.get("energy_load",0))
            except (TypeError,ValueError): energy=0
            try: share=max(0,min(1,float(item.get("shareability",0.3))))
            except (TypeError,ValueError): share=0.3
            cleaned.append({"start_minute":start,"end_minute":end,"kind":kind,"summary":summary,"location":location,"energy_load":energy,"shareability":share})
        cleaned.sort(key=lambda x:x["start_minute"])
        result=[]; last_end=-1
        for item in cleaned:
            if item["start_minute"]<last_end: item["start_minute"]=last_end
            if item["end_minute"]<=item["start_minute"]: continue
            seed=f"{day}:{item['start_minute']}:{item['end_minute']}:{item['kind']}:{item['summary']}"
            item["id"]=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]; item["day"]=day
            result.append(item); last_end=item["end_minute"]
        has_sleep=any(x["kind"]=="sleep" for x in result); meals=sum(x["kind"]=="meal" for x in result)
        return result if has_sleep and meals>=2 else []

    # LLM 失败或结果不合格时使用模板骨架，保证日程服务始终可用。
    async def ensure_day(self, now: datetime, personality: str, weather_text: str, force: bool=False) -> list[dict[str, Any]]:
        day=now.strftime("%Y-%m-%d"); existing=await self.store.get_framework(day)
        if existing and not force: return existing
        weekend=now.weekday()>=5; fallback=self._fallback(day,weekend)
        prompt=(f"为虚拟网友麦麦生成{day}的生活框架。{'周末' if weekend else '工作日'}，天气背景：{weather_text}。\n"
                f"人格：{personality or '自然、独立、有自己的生活'}\n模板骨架：{json.dumps(self._template().get('weekend' if weekend else 'workday',[]),ensure_ascii=False)}\n"
                "返回JSON数组。字段必须是start,end,kind,summary,location,energy_load,shareability。"
                "kind只能是meal/work/study/travel/leisure/sleep/nap/rest。时间不重叠，包含夜间睡眠和至少两顿饭。")
        raw=await self.llm.generate_json(prompt,"你是生活日程规划器，只输出合法JSON数组。",fallback,max_tokens=2200)
        nodes=self._validate(day,raw) or fallback
        await self.store.replace_framework(day,nodes); return await self.store.get_framework(day)

    @staticmethod
    def current_and_next(nodes: list[dict[str, Any]], minute: int) -> tuple[dict[str, Any]|None,dict[str, Any]|None]:
        current=None; nxt=None
        for node in nodes:
            if node["start_minute"]<=minute<node["end_minute"]: current=node
            elif node["start_minute"]>minute:
                nxt=node; break
        return current,nxt

    # 只细化临近节点，减少模型调用并保持场景与最新环境一致。
    async def expand_due(self, now: datetime, nodes: list[dict[str, Any]], state: dict[str, Any], weather_text: str) -> None:
        minute=now.hour*60+now.minute; lead=self.config.schedule.detail_lead_minutes
        due=[n for n in nodes if n["end_minute"]>minute and n["start_minute"]<=minute+lead][:2]
        for node in due:
            if await self.store.get_scene(node["id"]): continue
            expires=datetime.combine(now.date(),datetime.min.time(),tzinfo=now.tzinfo)+timedelta(minutes=node["end_minute"])
            fallback={"scene":node["summary"],"state_deltas":{"energy":node["energy_load"],"hunger":-35 if node["kind"]=="meal" else 0,"mood_valence":0.03 if node["kind"]=="leisure" else 0},
                      "opportunities":([{"topic":node["summary"],"motive":"想自然分享一点正在经历的生活","weight":node["shareability"],"privacy":"normal"}] if node["shareability"]>=0.35 else [])}
            prompt=(f"把生活框架细化为具体但克制的场景。框架：{json.dumps(node,ensure_ascii=False)}\n"
                    f"当前精力{state.get('energy')}、饥饿{state.get('hunger')}、天气{weather_text}。\n"
                    "返回JSON对象：scene字符串，state_deltas对象，opportunities数组。机会字段topic,motive,weight,privacy。"
                    "不要凭空制造重大事件，不要强行想用户。")
            raw=await self.llm.generate_json(prompt,"你是日常场景细化器，只返回JSON对象。",fallback,max_tokens=900)
            if not isinstance(raw,dict): raw=fallback
            scene=str(raw.get("scene") or fallback["scene"])[:500]
            deltas=raw.get("state_deltas") if isinstance(raw.get("state_deltas"),dict) else fallback["state_deltas"]
            opportunities=[]
            for idx,item in enumerate(raw.get("opportunities") or []):
                if not isinstance(item,dict): continue
                try: weight=max(0,min(1,float(item.get("weight",0.3))))
                except (TypeError,ValueError): weight=0.3
                op_id=hashlib.sha1(f"{node['id']}:{idx}:{item.get('topic','')}".encode()).hexdigest()[:20]
                opportunities.append({"id":op_id,"topic":str(item.get("topic") or node["summary"])[:160],
                    "motive":str(item.get("motive") or "想分享生活")[:240],"weight":weight,
                    "privacy":str(item.get("privacy") or "normal")[:30],"expires_at":expires.timestamp()})
            await self.store.save_scene(node["id"],scene,deltas,opportunities)

    # applied 标记确保节点结束增量只执行一次。
    async def apply_completed(self, now: datetime, state_engine: Any) -> None:
        day=now.strftime("%Y-%m-%d"); minute=now.hour*60+now.minute
        for scene in await self.store.completed_unapplied_scenes(day,minute):
            await state_engine.apply_deltas(scene["state_deltas"]); await self.store.mark_scene_applied(scene["framework_id"])

    async def context(self, now: datetime) -> dict[str, Any]:
        nodes=await self.store.get_framework(now.strftime("%Y-%m-%d")); minute=now.hour*60+now.minute
        current,nxt=self.current_and_next(nodes,minute); scene=await self.store.get_scene(current["id"]) if current else {}
        return {"nodes":nodes,"current":current,"next":nxt,"scene":scene}
