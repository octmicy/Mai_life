"""Mai_life plugin entrypoint."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any, ClassVar, Iterable, Optional

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

from .config import MaiLifeSettings
from .environment import EnvironmentService
from .life_state import LifeStateEngine
from .llm_service import LLMService
from .proactive import ProactiveEngine
from .prompt_builder import PromptBuilder, relationship_stage
from .rest_gate import RestGate
from .schedule_service import ScheduleService, hhmm
from .storage import LifeStore


class MaiLifePlugin(MaiBotPlugin):
    config_model = MaiLifeSettings
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[LifeStore]=None
        self._env: Optional[EnvironmentService]=None
        self._llm: Optional[LLMService]=None
        self._state: Optional[LifeStateEngine]=None
        self._schedule: Optional[ScheduleService]=None
        self._rest: Optional[RestGate]=None
        self._proactive: Optional[ProactiveEngine]=None
        self._prompts=PromptBuilder()
        self._tasks: list[asyncio.Task[Any]]=[]
        self._personality=""
        self._maintenance_lock=asyncio.Lock()

    @property
    def _ready(self) -> bool:
        return all((self._store,self._env,self._llm,self._state,self._schedule,self._rest,self._proactive))

    # 生命周期：初始化服务与存储；即使插件关闭，也保留热启用所需的底座。
    async def on_load(self) -> None:
        data_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)),"data")
        self._store=LifeStore(data_dir); await self._store.initialize()
        self._llm=LLMService(self.ctx,self.config)
        self._env=EnvironmentService(self._store,self.config,self.ctx.logger)
        self._state=LifeStateEngine(self._store,self.config,self._llm,self.ctx.logger)
        self._schedule=ScheduleService(self._store,self.config,self._llm,os.path.dirname(os.path.abspath(__file__)),self.ctx.logger)
        self._rest=RestGate(self._store,self.config,self._llm,self._state,self.ctx.logger)
        self._proactive=ProactiveEngine(self.ctx,self._store,self.config,self._env,self.ctx.logger)
        await self._store.sync_users(self.config.users.profiles)
        await self._refresh_personality(); await self._resolve_all_streams()
        if self.config.plugin.enabled:
            await self._maintenance_tick(force_weather=True)
            self._start_tasks()
        self.ctx.logger.info("[MaiLife] 麦麦生活加载完成")

    async def on_unload(self) -> None:
        await self._stop_tasks()
        if self._store: await self._store.close()
        self.ctx.logger.info("[MaiLife] 麦麦生活已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str,Any], version: str) -> None:
        del config_data,version
        await self._stop_tasks()
        for service in (self._llm,self._env,self._state,self._schedule,self._rest,self._proactive):
            if service: service.update_config(self.config)
        if self._store: await self._store.sync_users(self.config.users.profiles)
        if scope=="bot": await self._refresh_personality()
        await self._resolve_all_streams()
        if self.config.plugin.enabled:
            await self._maintenance_tick(force_weather=True); self._start_tasks()
        self.ctx.logger.info(f"[MaiLife] 配置热更新完成 scope={scope}")

    # 后台任务统一保存引用，热更新和卸载时必须 cancel + await。
    def _start_tasks(self) -> None:
        if self._tasks:return
        self._tasks=[asyncio.create_task(self._maintenance_loop(),name="mai-life-maintenance"),
                     asyncio.create_task(self._proactive_loop(),name="mai-life-proactive"),
                     asyncio.create_task(self._daily_generation_loop(),name="mai-life-daily-generation")]

    async def _stop_tasks(self) -> None:
        tasks=self._tasks; self._tasks=[]
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)

    async def _daily_generation_loop(self) -> None:
        while True:
            try:
                assert self._env and self._schedule
                now=self._env.now()
                next_run=now.replace(hour=self.config.schedule.generate_hour,minute=0,second=0,microsecond=0)
                if next_run<=now:next_run+=timedelta(days=1)
                await asyncio.sleep(max(1,(next_run-now).total_seconds()))
                weather=await self._env.refresh_weather()
                await self._schedule.ensure_day(self._env.now(),self._personality,self._env.weather_text(weather),force=True)
            except asyncio.CancelledError:raise
            except Exception as exc:self.ctx.logger.error(f"[MaiLife] 每日日程生成异常: {exc}")

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(60,self.config.state.tick_interval_minutes*60))
                await self._maintenance_tick()
            except asyncio.CancelledError: raise
            except Exception as exc: self.ctx.logger.error(f"[MaiLife] 状态维护异常: {exc}")

    async def _proactive_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(60,self.config.proactive.patrol_interval_minutes*60))
                await self._resolve_all_streams()
                if self._proactive and self._store and self._env:
                    now=self._env.now(); state=await self._store.get_state(); await self._proactive.patrol(now,state)
            except asyncio.CancelledError: raise
            except Exception as exc: self.ctx.logger.error(f"[MaiLife] 主动巡检异常: {exc}")

    # 单次生活维护：天气 → 日程 → 场景细化 → 状态推进 → 关系结算。
    async def _maintenance_tick(self, force_weather: bool=False) -> None:
        if not self._ready:return
        async with self._maintenance_lock:
            assert self._env and self._schedule and self._store and self._state
            now=self._env.now(); weather=await self._env.refresh_weather(force=force_weather)
            nodes=await self._schedule.ensure_day(now,self._personality,self._env.weather_text(weather))
            context=await self._schedule.context(now); state=await self._store.get_state()
            await self._schedule.expand_due(now,nodes,state,self._env.weather_text(weather))
            context=await self._schedule.context(now)
            await self._state.advance(now,context.get("current"),context.get("scene"))
            await self._schedule.apply_completed(now,self._state)
            relation_day=now.date()-timedelta(days=1)
            relation_start=now.replace(year=relation_day.year,month=relation_day.month,day=relation_day.day,hour=0,minute=0,second=0,microsecond=0)
            await self._store.update_relationships(relation_day.isoformat(),relation_start.timestamp(),(relation_start+timedelta(days=1)).timestamp(),now.timestamp())

    async def _refresh_personality(self) -> None:
        try:self._personality=str(await self.ctx.config.get("personality.personality","") or "")
        except Exception as exc:self.ctx.logger.warning(f"[MaiLife] 读取人格失败: {exc}")

    async def _resolve_stream(self,user_id: str) -> str:
        try:
            info=await self.ctx.chat.get_stream_by_user_id(user_id=user_id)
            if isinstance(info,dict) and info.get("stream_id"):return str(info["stream_id"])
        except Exception:pass
        try:
            streams=await self.ctx.chat.get_private_streams()
            values=streams.values() if isinstance(streams,dict) else streams if isinstance(streams,list) else []
            for item in values:
                if isinstance(item,dict) and str(item.get("user_id", ""))==user_id:return str(item.get("stream_id") or "")
        except Exception as exc:self.ctx.logger.debug(f"[MaiLife] stream 解析失败 user={user_id}: {exc}")
        return ""

    async def _resolve_all_streams(self) -> None:
        if not self._store:return
        for user in await self._store.list_users():
            if user.get("stream_id"):continue
            stream=await self._resolve_stream(user["user_id"])
            if stream:await self._store.set_user_stream(user["user_id"],stream)

    @staticmethod
    def _extract_message(kwargs: dict[str,Any]) -> tuple[str,str,str,bool]:
        msg=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        info=msg.get("message_info") if isinstance(msg.get("message_info"),dict) else {}
        uinfo=info.get("user_info") if isinstance(info.get("user_info"),dict) else {}
        user_id=str(uinfo.get("user_id") or msg.get("user_id") or kwargs.get("user_id") or "")
        stream_id=str(msg.get("stream_id") or info.get("stream_id") or kwargs.get("stream_id") or kwargs.get("session_id") or "")
        text=str(msg.get("processed_plain_text") or msg.get("raw_message") or kwargs.get("text") or "")
        group=info.get("group_info") if isinstance(info.get("group_info"),dict) else {}
        is_private=not bool(group.get("group_id") or msg.get("group_id") or kwargs.get("group_id"))
        return user_id,stream_id,text.strip(),is_private

    async def _user_by_session(self,session_id: str) -> dict[str,Any]:
        if not self._store or not session_id:return {}
        for user in await self._store.list_users():
            if str(user.get("stream_id") or "")==session_id:return user
        return {}

    # 入站闸门只处理已配置用户的私聊；群聊与未知用户保持原 MaiBot 行为。
    @HookHandler("chat.receive.before_process",mode=HookMode.BLOCKING,order=HookOrder.EARLY)
    async def on_receive(self,**kwargs:Any)->dict[str,Any]:
        if not self.config.plugin.enabled or not self._ready:return {"action":"continue"}
        assert self._store and self._env and self._schedule and self._rest
        user_id,stream_id,text,is_private=self._extract_message(kwargs)
        if not is_private or not user_id:return {"action":"continue"}
        user=await self._store.get_user(user_id)
        if not user or not user.get("enabled"):return {"action":"continue"}
        if stream_id and stream_id!=user.get("stream_id"):await self._store.set_user_stream(user_id,stream_id)
        if text.lstrip().startswith("/mai_"):return {"action":"continue"}
        now=self._env.now(); await self._store.record_interaction(user_id,text,now.timestamp(),now.hour)
        context=await self._schedule.context(now)
        allowed,reason=await self._rest.decide(user_id,text,now,context.get("current"))
        if not allowed:
            await self._store.add_rest_backlog(user_id,text or "发来一条消息",now.timestamp())
            self.ctx.logger.info(f"[MaiLife] 休息闸门阻断 user={user_id} reason={reason}")
            return {"action":"abort"}
        return {"action":"continue"}

    async def _prompt_payload(self,session_id:str,consume_backlog:bool=False)->tuple[dict[str,Any],dict[str,Any],dict[str,Any],dict[str,Any],dict[str,Any],list[str]]|None:
        if not self._ready:return None
        assert self._store and self._env and self._schedule
        user=await self._user_by_session(session_id)
        if not user:return None
        now=self._env.now(); state=await self._store.get_state(); weather=await self._store.get_weather() or {"description":"天气未知"}; context=await self._schedule.context(now); dream=await self._store.latest_dream()
        backlogs=await (self._store.consume_rest_backlogs(user["user_id"]) if consume_backlog else self._store.peek_rest_backlogs(user["user_id"]))
        return state,weather,context,user,dream,backlogs

    # Planner 获取完整生活上下文，用于决策；不会向其他会话泄露状态。
    @HookHandler("maisaka.planner.before_request",mode=HookMode.BLOCKING)
    async def on_planner(self,**kwargs:Any)->dict[str,Any]:
        session=str(kwargs.get("session_id") or ""); payload=await self._prompt_payload(session)
        if not payload:return {"action":"continue"}
        suffix=self._prompts.planner(*payload); kwargs["extra_prompt"]=(kwargs.get("extra_prompt") or "")+suffix
        return {"action":"continue","modified_kwargs":kwargs}

    # Replyer 只获取压缩摘要，避免每次回复机械汇报天气和日程。
    @HookHandler("maisaka.replyer.before_request",mode=HookMode.BLOCKING)
    async def on_replyer(self,**kwargs:Any)->dict[str,Any]:
        session=str(kwargs.get("session_id") or ""); payload=await self._prompt_payload(session,consume_backlog=True)
        if not payload:return {"action":"continue"}
        state,weather,context,user,_dream,backlogs=payload
        kwargs["extra_prompt"]=(kwargs.get("extra_prompt") or "")+self._prompts.replyer(state,weather,context,user,backlogs)
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("maisaka.replyer.after_response",mode=HookMode.OBSERVE)
    async def on_replyer_after(self,**kwargs:Any)->None:
        if self._proactive:await self._proactive.mark_replyer_sent(str(kwargs.get("session_id") or ""))

    def _is_admin(self,user_id:str)->bool:
        admins=[str(x) for x in self.config.plugin.admin_user_ids if str(x)]
        if not admins:admins=[str(p.user_id) for p in self.config.users.profiles if str(p.user_id).strip()][:1]
        return user_id in admins

    # Command 必须自行发送文本，并按 SDK 规范返回三元组。
    async def _send_command(self,kwargs:dict[str,Any],text:str)->tuple[bool,str,int]:
        user_id=str(kwargs.get("user_id") or "")
        user=await self._store.get_user(user_id) if self._store and user_id else {}
        if kwargs.get("group_id") or not user or not user.get("enabled"):text="该命令仅对已配置的私聊用户开放。"
        stream=str(kwargs.get("stream_id") or "")
        try:
            ok=await self.ctx.send.text(text=text,stream_id=stream); return bool(ok),"命令结果已发送" if ok else "发送返回False",2
        except Exception as exc:return False,f"发送失败: {exc}",2

    @Command(name="/mai_status",pattern=r"^/mai_status\b",description="查看麦麦全局生活状态")
    async def cmd_status(self,**kwargs:Any)->tuple[bool,str,int]:
        return await self._send_command(kwargs,await self._status_report())

    @Command(name="/mai_schedule",pattern=r"^/mai_schedule\b",description="查看麦麦今日日程和当前场景")
    async def cmd_schedule(self,**kwargs:Any)->tuple[bool,str,int]:
        return await self._send_command(kwargs,await self._schedule_report())

    @Command(name="/mai_relation",pattern=r"^/mai_relation\b",description="查看当前用户与麦麦的关系温度")
    async def cmd_relation(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._store.get_user(uid) if self._store else {}
        text="你尚未加入麦麦生活的私聊用户列表。" if not user else f"关系温度：{float(user['temperature']):.1f}/100\n关系阶段：{relationship_stage(float(user['temperature']))}"
        return await self._send_command(kwargs,text)

    @Command(name="/mai_config",pattern=r"^/mai_config\b",description="查看麦麦生活配置摘要")
    async def cmd_config(self,**kwargs:Any)->tuple[bool,str,int]:
        c=self.config; text=(f"麦麦生活：{'开启' if c.plugin.enabled else '关闭'}\n配置用户：{len(c.users.profiles)}\n"
            f"状态推进：{c.state.tick_interval_minutes}分钟\n天气城市：{c.environment.city}\n休息闸门：{'开启' if c.rest_gate.enabled else '关闭'} ({c.rest_gate.mode})\n"
            f"主动私聊：每天{c.proactive.daily_max_per_user}次，最小间隔{c.proactive.min_interval_minutes}分钟")
        return await self._send_command(kwargs,text)

    @Command(name="/mai_help",pattern=r"^/mai_help\b",description="查看麦麦生活命令")
    async def cmd_help(self,**kwargs:Any)->tuple[bool,str,int]:
        return await self._send_command(kwargs,"/mai_status 状态\n/mai_schedule 日程\n/mai_relation 关系\n/mai_config 配置\n/mai_regenerate_schedule 重生成日程\n/mai_rest_test 休息闸门诊断")

    @Command(name="/mai_regenerate_schedule",pattern=r"^/mai_regenerate_schedule\b",description="管理员重新生成今日日程")
    async def cmd_regenerate(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not self._is_admin(uid):return await self._send_command(kwargs,"只有管理员可以重新生成日程。")
        if not self._ready:return await self._send_command(kwargs,"服务尚未初始化。")
        assert self._env and self._schedule
        now=self._env.now(); weather=await self._env.refresh_weather(); nodes=await self._schedule.ensure_day(now,self._personality,self._env.weather_text(weather),force=True)
        return await self._send_command(kwargs,f"已重新生成今日日程，共 {len(nodes)} 个节点。")

    @Command(name="/mai_rest_test",pattern=r"^/mai_rest_test\b",description="管理员查看休息闸门状态")
    async def cmd_rest_test(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not self._is_admin(uid):return await self._send_command(kwargs,"只有管理员可以查看休息闸门诊断。")
        if not self._ready:return await self._send_command(kwargs,"服务尚未初始化。")
        assert self._env and self._schedule and self._store
        now=self._env.now(); context=await self._schedule.context(now); runtime=await self._store.get_sleep_runtime()
        text=f"当前日程类型：{(context.get('current') or {}).get('kind','无')}\n睡眠阶段：{runtime.get('phase')}\n醒来缓冲至：{datetime.fromtimestamp(float(runtime.get('awake_grace_until',0))).isoformat() if runtime.get('awake_grace_until') else '无'}"
        return await self._send_command(kwargs,text)

    async def _status_report(self)->str:
        if not self._ready:return "麦麦生活尚未初始化。"
        assert self._store and self._env and self._schedule
        state=await self._store.get_state(); weather=await self._env.refresh_weather(); context=await self._schedule.context(self._env.now())
        return (f"麦麦生活状态\n精力：{state.get('energy',0):.0f}/100\n饥饿：{state.get('hunger',0):.0f}/100\n"
            f"心情：{state.get('mood_valence',0):.2f}\n健康：{state.get('health_note')}\n睡眠：{state.get('sleep_phase')}\n"
            f"位置感：{state.get('current_location')}\n当前场景：{state.get('current_activity')}\n天气：{self._env.weather_text(weather)}\n"
            f"当前日程：{(context.get('current') or {}).get('summary','无')}")

    async def _schedule_report(self)->str:
        if not self._ready:return "日程服务尚未初始化。"
        assert self._env and self._schedule
        now=self._env.now(); context=await self._schedule.context(now); lines=[f"麦麦 {now:%Y-%m-%d} 日程"]
        for node in context["nodes"]:lines.append(f"{hhmm(node['start_minute'])}-{hhmm(node['end_minute'])} {node['summary']} @ {node['location']}")
        if context.get("scene"):lines.append(f"\n当前细化场景：{context['scene'].get('scene')}")
        return "\n".join(lines)

    # 公共 API 只返回结构化数据，不直接触发消息发送。
    @API(name="get_life_state",description="获取麦麦全局生活状态",version="1",public=True)
    async def api_life_state(self,**kwargs:Any)->dict[str,Any]:
        return await self._store.get_state() if self._store else {}

    @API(name="get_current_scene",description="获取麦麦当前细化场景",version="1",public=True)
    async def api_current_scene(self,**kwargs:Any)->dict[str,Any]:
        if not self._ready:return {}
        assert self._env and self._schedule
        return await self._schedule.context(self._env.now())

    @API(name="get_schedule",description="获取麦麦今日日程",version="1",public=True)
    async def api_schedule(self,**kwargs:Any)->list[dict[str,Any]]:
        if not self._ready:return []
        assert self._env and self._store
        return await self._store.get_framework(self._env.now().strftime("%Y-%m-%d"))

    @API(name="get_user_relationship",description="获取指定用户关系状态",version="1",public=True)
    async def api_relationship(self,user_id:str="",**kwargs:Any)->dict[str,Any]:
        if not self._store:return {}
        user=await self._store.get_user(str(user_id));
        if user:user["stage"]=relationship_stage(float(user["temperature"]))
        return user

    @API(name="create_proactive_opportunity",description="创建外部主动分享契机",version="1",public=True)
    async def api_create_opportunity(self,topic:str="",motive:str="",weight:float=0.5,expires_minutes:int=120,**kwargs:Any)->dict[str,Any]:
        if not self._ready or not topic:return {"success":False,"error":"not_ready_or_empty_topic"}
        assert self._store and self._env and self._schedule
        now=self._env.now(); context=await self._schedule.context(now); current=context.get("current")
        if not current:return {"success":False,"error":"no_current_framework"}
        import hashlib
        oid=hashlib.sha1(f"api:{now.timestamp()}:{topic}".encode()).hexdigest()[:20]
        await self._store.add_opportunity({"id":oid,"framework_id":current["id"],"topic":topic[:160],"motive":motive[:240] or "外部生活事件值得分享","weight":max(0,min(1,float(weight))),"privacy":"normal","expires_at":now.timestamp()+max(1,expires_minutes)*60})
        return {"success":True,"opportunity_id":oid}


def create_plugin()->MaiBotPlugin:
    return MaiLifePlugin()


