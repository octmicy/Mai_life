"""Daily framework generation and near-term scene expansion."""
from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ALLOWED={"meal","work","study","travel","leisure","sleep","nap","rest"}


def to_minute(text: str, *, allow_day_end: bool=False) -> int | None:
    try:
        if allow_day_end and text=="24:00":return 1440
        h,m=map(int,text.split(":"));
        if 0<=h<=23 and 0<=m<=59: return h*60+m
    except (ValueError,AttributeError):
        pass
    return None


def hhmm(value: int) -> str:
    value=max(0,min(1440,value)); return "24:00" if value==1440 else f"{value//60:02d}:{value%60:02d}"


class ScheduleService:
    def __init__(self, store: Any, config: Any, llm: Any, plugin_dir: str, logger: Any) -> None:
        self.store=store; self.config=config; self.llm=llm; self.plugin_dir=Path(plugin_dir); self.logger=logger

    def update_config(self, config: Any) -> None:
        self.config=config

    def _template(self) -> dict[str, Any]:
        root=self.plugin_dir.resolve()
        path=(root/str(self.config.schedule.template_file or "mai_template.json")).resolve()
        try:path.relative_to(root)
        except ValueError:
            self.logger.warning("[MaiLife] 日程模板必须位于插件目录内，已使用内置 fallback")
            return {}
        try:
            data=json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data,dict) else {}
        except Exception as exc:
            self.logger.warning(f"[MaiLife] 日程模板读取失败: {exc}"); return {}

    def _fallback(self, day: str, weekend: bool) -> list[dict[str, Any]]:
        builtin=[
            {"start":"00:00","end":"08:00","kind":"sleep","summary":"安稳睡觉","location":"卧室","energy_load":8,"shareability":0.05},
            {"start":"08:00","end":"09:00","kind":"meal","summary":"起床洗漱并吃早餐","location":"家里","energy_load":-1,"shareability":0.25},
            {"start":"09:00","end":"12:00","kind":"work","summary":"处理自己的事情","location":"书桌前","energy_load":-5,"shareability":0.25},
            {"start":"12:00","end":"13:00","kind":"meal","summary":"准备午饭","location":"厨房","energy_load":-1,"shareability":0.4},
            {"start":"13:00","end":"13:40","kind":"nap","summary":"短暂午休","location":"卧室","energy_load":3,"shareability":0.05},
            {"start":"13:40","end":"18:00","kind":"study","summary":"继续学习和整理东西","location":"书桌前","energy_load":-5,"shareability":0.25},
            {"start":"18:00","end":"19:00","kind":"meal","summary":"做晚饭","location":"厨房","energy_load":-1,"shareability":0.55},
            {"start":"19:00","end":"23:30","kind":"leisure","summary":"放松、看东西和随便逛逛","location":"家里","energy_load":-3,"shareability":0.55},
            {"start":"23:30","end":"24:00","kind":"sleep","summary":"准备睡觉","location":"卧室","energy_load":1,"shareability":0.05},
        ]
        template=self._template(); key="weekend" if weekend else "workday"; raw=template.get(key)
        # 模板即使是数组也可能缺睡眠或进餐；校验失败必须回到稳定内置骨架。
        validated=self._validate(day,raw) if isinstance(raw,list) else []
        return validated or self._validate(day,builtin)

    def _state_summary(self, state: dict[str, Any] | None) -> str:
        """把当前精力/饥饿/心情浓缩成一句中文提示，供日程生成参考。"""
        if not isinstance(state,dict): return ""
        try:
            energy=float(state.get("energy",70)); hunger=float(state.get("hunger",20)); mood=float(state.get("mood_valence",0))
        except (TypeError,ValueError): return ""
        hunger_note="（很高，最近经常饿）" if hunger>75 else ""
        energy_note="（很低，需要多休息）" if energy<30 else ""
        mood_note="（偏低）" if mood<-0.3 else ""
        return (f"麦麦当前状态：精力 {energy:.0f}/100{energy_note}，饥饿 {hunger:.0f}/100{hunger_note}，"
                f"心情 {mood:+.2f}{mood_note}。生成日程时请保证正常三餐、不要安排过高强度。")

    @staticmethod
    def _apply_memory_hints(day:str,nodes:list[dict[str,Any]],memory_context:dict[str,Any]|None)->list[dict[str,Any]]:
        context=memory_context or {}; dates=context.get("private_date_hints") or []
        if not dates:return nodes
        result=[dict(item) for item in nodes]
        target=next((item for item in result if item.get("kind")=="leisure"),None)
        if target is None:return result
        target["summary"]=(str(target.get("summary") or "自由活动")+"，也为临近的重要安排做些准备")[:160]
        seed=f"{day}:{target['start_minute']}:{target['end_minute']}:{target['kind']}:{target['summary']}"
        target["id"]=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        return result

    # 所有 LLM 日程必须经过时间、类型、重叠和必要节点校验。
    def _validate(self, day: str, raw: Any, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """规范化节点、修复可安全修复的重叠，并拒绝缺少夜间睡眠或进餐的框架。"""
        if not isinstance(raw,list): return []
        cleaned=[]
        for index,item in enumerate(raw):
            if not isinstance(item,dict): continue
            start=to_minute(str(item.get("start") or item.get("time") or ""))
            end=to_minute(str(item.get("end") or ""),allow_day_end=True)
            if start is None: continue
            if end is None: end=min(1440,start+60)
            if end<=start: continue
            kind=str(item.get("kind") or "leisure").lower(); kind=kind if kind in _ALLOWED else "leisure"
            summary=str(item.get("summary") or item.get("activity") or "自由活动")[:160]
            location=str(item.get("location") or "家里")[:80]
            try: energy=max(-12.0,min(12.0,float(item.get("energy_load",0))))
            except (TypeError,ValueError): energy=0
            try: share=max(0,min(1,float(item.get("shareability",0.3))))
            except (TypeError,ValueError): share=0.3
            cleaned.append({"start_minute":start,"end_minute":end,"kind":kind,"summary":summary,"location":location,"energy_load":energy,"shareability":share})
        # 排序后只向后推迟重叠节点，绝不把后段改回已经过去的时间。
        cleaned.sort(key=lambda x:x["start_minute"])
        result=[]; last_end=-1
        for item in cleaned:
            if item["start_minute"]<last_end: item["start_minute"]=last_end
            if item["end_minute"]<=item["start_minute"]: continue
            seed=f"{day}:{item['start_minute']}:{item['end_minute']}:{item['kind']}:{item['summary']}"
            item["id"]=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]; item["day"]=day
            result.append(item); last_end=item["end_minute"]
        night_sleep=sum(
            max(0,min(item["end_minute"],10*60)-item["start_minute"])
            +max(0,item["end_minute"]-max(item["start_minute"],21*60))
            for item in result if item["kind"]=="sleep"
        )
        meals=sum(x["kind"]=="meal" for x in result)
        # 饥饿偏高时要求三餐齐全，避免少餐框架把饥饿锁在告警区。
        hungry=float((state or {}).get("hunger",0) or 0)>75
        if hungry and meals<3: return []
        return result if night_sleep>=5*60 and meals>=2 else []

    @staticmethod
    def _state_deltas(node:dict[str,Any])->dict[str,float]:
        """节点完成数值由本地规则给出，不接受模型任意改写核心状态。"""
        kind=str(node.get("kind") or "leisure")
        try:energy=max(-12.0,min(12.0,float(node.get("energy_load") or 0)))
        except (TypeError,ValueError):energy=0.0
        return {"energy":energy,"hunger":-35.0 if kind=="meal" else 0.0,
                "mood_valence":0.03 if kind=="leisure" else 0.0,"mood_arousal":0.0}

    # LLM 失败或结果不合格时使用模板骨架，保证日程服务始终可用。
    # 生成前收集三个变化源：最近几天日程、历法背景、兴趣素材；避免每天照抄模板。
    async def _variety_context(self, now: datetime) -> str:
        blocks: list[str] = []
        recent: list[str] = []
        for offset in range(1, 6):
            past = (now - timedelta(days=offset)).date().isoformat()
            nodes = await self.store.get_framework(past)
            if not nodes: continue
            text = "；".join(str(node.get("summary") or "")[:40] for node in nodes
                            if node.get("kind") not in {"sleep"})[:400]
            if text: recent.append(f"{past}：{text}")
        if recent:
            blocks.append("最近几天的安排（时间结构可以相似，具体内容必须明显错开）：\n"+"\n".join(recent))
        return "\n".join(blocks)

    async def ensure_day(self, now: datetime, personality: str, weather_text: str, force: bool=False,
                         memory_context: dict[str,Any]|None=None,
                         environment: dict[str,Any]|None=None) -> list[dict[str, Any]]:
        day=now.strftime("%Y-%m-%d"); existing=await self.store.get_framework(day)
        if existing and not force: return existing
        state=await self.store.get_state()
        weekend=now.weekday()>=5; fallback=self._apply_memory_hints(day,self._fallback(day,weekend),memory_context)
        variety=await self._variety_context(now)
        calendar_bits=[str(environment.get(key)) for key in ("day_type","holiday","lunar","solar_term")
                       if environment and environment.get(key) and environment.get(key)!="无"]
        if calendar_bits:
            variety=(variety+"\n今日历法："+ "，".join(calendar_bits[:4])).strip()
        ideas: list[str] = []
        try:
            for note in await self.store.recent_exploration_notes(now.timestamp(),5):
                topic=str(note.get("topic") or "").strip()
                if topic: ideas.append(topic[:40])
        except Exception: pass
        try:
            for doc in await self.store.list_bookshelf_documents(allow_private=True,limit=5):
                title=str(doc.get("title") or "").strip()
                if title and title!="未命名": ideas.append(f"自己的作品《{title[:30]}》")
        except Exception: pass
        picked=random.sample(ideas,min(3,len(ideas))) if ideas else []
        if picked:
            variety=(variety+"\n今天可以考虑的活动灵感（自由选用，不要硬塞）："+ "、".join(picked)).strip()
        prompt=(f"为虚拟网友麦麦生成{day}的生活框架。{'周末' if weekend else '工作日'}，天气背景：{weather_text}。\n"
                f"人格：{personality or '自然、独立、有自己的生活'}\n"
                f"{self._state_summary(state)}\n"
                f"参考节奏（只参考时间结构和比例，禁止照抄里面的描述）："
                f"{json.dumps(self._template().get('weekend' if weekend else 'workday',[]),ensure_ascii=False)}\n"
                f"匿名生活记忆：{json.dumps(memory_context or {},ensure_ascii=False)}。日期提示不含用户身份，不得猜测是谁。"
                f"{variety}\n"
                "要求：\n"
                "1) 每个节点的 summary 必须具体到'正在做什么'（例如'给连载小说写第三章''玩两把新出的游戏''整理相册并修图'），"
                "禁止'处理自己的事情''放松、看东西和随便逛逛'这类空泛描述；\n"
                "2) 工作/学习段写清楚主题方向，休闲段每天至少有一件事与最近几天不同；\n"
                "3) 节假日、节气、农历和天气要自然反映在活动里；\n"
                "4) 日程应符合普通人的时间、精力和生活常识，不安排突兀的高强度事项，时间不重叠，包含夜间睡眠和至少两顿饭。\n"
                "返回JSON数组。字段必须是start,end,kind,summary,location,energy_load,shareability。"
                "kind只能是meal/work/study/travel/leisure/sleep/nap/rest。")
        raw=fallback
        if self.llm.task_available("schedule"):
            raw=await self.llm.generate_json(prompt,"你是生活日程规划器，只输出合法JSON数组。",fallback,max_tokens=2200,task_kind="schedule",request_type="daily_schedule")
        nodes=self._validate(day,raw,state) or fallback
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
        """细化当前与下一节点；状态增量始终由本地规则生成，模型只写叙事和机会。"""
        minute=now.hour*60+now.minute; lead=self.config.schedule.detail_lead_minutes
        due=[n for n in nodes if n["end_minute"]>minute and n["start_minute"]<=minute+lead][:2]
        for node in due:
            if await self.store.get_scene(node["id"]): continue
            expires=datetime.combine(now.date(),datetime.min.time(),tzinfo=now.tzinfo)+timedelta(minutes=node["end_minute"])
            fallback={"scene":node["summary"],"state_deltas":self._state_deltas(node),
                      "opportunities":([{"topic":node["summary"],"motive":"想自然分享一点正在经历的生活","weight":node["shareability"],"privacy":"normal"}] if node["shareability"]>=0.35 else [])}
            prompt=(f"把生活框架细化为具体但克制的场景。框架：{json.dumps(node,ensure_ascii=False)}\n"
                    f"当前精力{state.get('energy')}、饥饿{state.get('hunger')}、天气{weather_text}。\n"
                    "返回JSON对象：scene字符串和opportunities数组。机会字段topic,motive,weight。"
                    "不要凭空制造重大事件，不要强行想用户。")
            raw=fallback
            if self.llm.task_available("scene_detail"):
                raw=await self.llm.generate_json(prompt,"你是日常场景细化器，只返回JSON对象。",fallback,max_tokens=900,task_kind="scene_detail",request_type="scene_detail")
            if not isinstance(raw,dict): raw=fallback
            scene=str(raw.get("scene") or fallback["scene"])[:500]
            deltas=self._state_deltas(node)
            opportunities=[]
            for idx,item in enumerate(raw.get("opportunities") or []):
                if not isinstance(item,dict): continue
                try: weight=max(0,min(1,float(item.get("weight",0.3))))
                except (TypeError,ValueError): weight=0.3
                op_id=hashlib.sha1(f"{node['id']}:{idx}:{item.get('topic','')}".encode()).hexdigest()[:20]
                opportunities.append({"id":op_id,"topic":str(item.get("topic") or node["summary"])[:160],
                    "motive":str(item.get("motive") or "想分享生活")[:240],"weight":weight,
                    "privacy":"normal","expires_at":expires.timestamp()})
            await self.store.save_scene(node["id"],scene,deltas,opportunities)

    async def state_timeline(self,start:datetime,end:datetime)->list[dict[str,Any]]:
        """按日程边界切分离线时间；缺失的过去日程只使用本地模板，场景按 ID 批量读取。"""
        if end<=start:return []
        windows=[]; day=start.date()
        while day<=end.date():
            day_start=datetime.combine(day,datetime.min.time(),tzinfo=end.tzinfo)
            next_day=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=end.tzinfo)
            window_start=max(start,day_start); window_end=min(end,next_day)
            if window_end<=window_start:day+=timedelta(days=1); continue
            stored=await self.store.get_framework(day.isoformat())
            nodes=stored or self._fallback(day.isoformat(),day.weekday()>=5)
            windows.append((window_start,window_end,day_start,stored,nodes))
            day+=timedelta(days=1)
        # 先收集窗口内所有已存储框架的节点 ID，再一次性查询场景，消除每节点一次 get_scene。
        scene_map=await self.store.get_scenes_by_framework_ids(
            node["id"] for _window_start,_window_end,_day_start,stored,nodes in windows if stored
            for node in nodes
        )
        spans=[]
        for window_start,window_end,day_start,stored,nodes in windows:
            cursor=window_start
            for node in nodes:
                node_start=day_start+timedelta(minutes=int(node["start_minute"]))
                node_end=day_start+timedelta(minutes=int(node["end_minute"]))
                if node_end<=window_start or node_start>=window_end:continue
                span_start=max(window_start,node_start); span_end=min(window_end,node_end)
                if cursor<span_start:
                    spans.append({"start":cursor,"end":span_start,"segment":{"kind":"leisure","summary":"自由活动","location":"家里"}})
                scene=scene_map.get(str(node["id"]),{}) if stored else {}
                completion={}
                if node_end<=end and node_end>start:
                    if scene and not int(scene.get("applied") or 0):completion={"framework_id":str(node["id"]),"deltas":self._state_deltas(node)}
                    elif not scene:completion={"framework_id":"","deltas":self._state_deltas(node)}
                spans.append({"start":span_start,"end":span_end,"segment":node,"completion":completion})
                cursor=max(cursor,span_end)
            if cursor<window_end:
                spans.append({"start":cursor,"end":window_end,"segment":{"kind":"leisure","summary":"自由活动","location":"家里"}})
        return spans

    async def context(self, now: datetime) -> dict[str, Any]:
        """返回今日框架、当前/下一节点和当前节点唯一的细化场景。"""
        nodes=await self.store.get_framework(now.strftime("%Y-%m-%d")); minute=now.hour*60+now.minute
        current,nxt=self.current_and_next(nodes,minute); scene=await self.store.get_scene(current["id"]) if current else {}
        return {"nodes":nodes,"current":current,"next":nxt,"scene":scene}
