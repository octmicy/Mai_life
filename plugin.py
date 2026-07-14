"""Mai_life v1.6.0 插件入口。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import date,datetime,timedelta
from typing import Any,ClassVar,Iterable,Optional

from maibot_sdk import API,Command,HomeCard,HookHandler,MaiBotPlugin
from maibot_sdk.types import HookMode,HookOrder

from .config import MaiLifeSettings
from .creation import BookshelfService,CreationService
from .core.environment import EnvironmentService
from .core.llm_service import LLMService
from .core.storage import LifeStore
from .information.information_service import InformationService
from .life.continuity import ContinuityService
from .life.life_state import LifeStateEngine
from .life.memory_service import MemoryService,skill_stage
from .life.proactive import ProactiveEngine
from .life.rest_gate import RestGate
from .life.schedule_service import ScheduleService,hhmm
from .messaging.adapter_compat import adapter_name,group_identity,recall_notice
from .messaging.message_pipeline import MessageDebouncer,classify_intent,direct_text,is_command,media_types,message_identity
from .messaging.prompt_builder import PromptBuilder,relationship_stage
from .messaging.recall_service import RecallService,is_recall_query
from .messaging.task_context import ActivePluginTask,ActiveTaskRegistry,HOST_TASK_PREFIX,PLUGIN_ID,latest_plugin_task_marker
from .messaging.vision_service import VisionService
from .management import AdminService
from .social import GroupObserver,RelayService


class MaiLifePlugin(MaiBotPlugin):
    config_model=MaiLifeSettings
    config_reload_subscriptions:ClassVar[Iterable[str]]=("bot","model")

    def __init__(self)->None:
        super().__init__()
        self._store:Optional[LifeStore]=None; self._env:Optional[EnvironmentService]=None
        self._llm:Optional[LLMService]=None; self._state:Optional[LifeStateEngine]=None
        self._schedule:Optional[ScheduleService]=None; self._rest:Optional[RestGate]=None
        self._proactive:Optional[ProactiveEngine]=None; self._debouncer:Optional[MessageDebouncer]=None
        self._recall:Optional[RecallService]=None; self._vision:Optional[VisionService]=None
        self._continuity:Optional[ContinuityService]=None
        self._memory:Optional[MemoryService]=None
        self._information:Optional[InformationService]=None
        self._group_observer:Optional[GroupObserver]=None; self._relay:Optional[RelayService]=None
        self._bookshelf:Optional[BookshelfService]=None; self._creation:Optional[CreationService]=None
        self._admin:Optional[AdminService]=None
        self._prompts=PromptBuilder(); self._tasks:list[asyncio.Task[Any]]=[]; self._transient:set[asyncio.Task[Any]]=set()
        self._message_tasks:dict[tuple[str,str],set[asyncio.Task[Any]]]={}
        self._personality=""; self._maintenance_lock=asyncio.Lock()
        self._session_runtime:dict[str,dict[str,Any]]={}
        self._reply_confirmations:dict[tuple[str,str],dict[str,Any]]={}
        self._active_tasks=ActiveTaskRegistry()
        self._stopping=False; self._reloading=False

    @property
    def _ready(self)->bool:
        return all((self._store,self._env,self._llm,self._state,self._schedule,self._rest,self._proactive,
                    self._debouncer,self._vision,self._continuity,self._memory,self._information,
                    self._group_observer,self._relay,self._bookshelf,self._creation,self._admin,self._recall))

    async def on_load(self)->None:
        self._stopping=False
        self._active_tasks.update_retention(int(self.config.debounce.turn_expire_seconds))
        await self._active_tasks.reset()
        root=os.path.dirname(os.path.abspath(__file__)); self._store=LifeStore(os.path.join(root,"data"))
        await self._store.initialize()
        if not self.config.recall.enabled or not self.config.recall.cache_summary_enabled:
            await self._store.clear_recall_summaries()
        await self._store.recover_creation_claims(time.time())
        self._llm=LLMService(self.ctx,self.config,self._store)
        self._env=EnvironmentService(self._store,self.config,self.ctx.logger)
        self._state=LifeStateEngine(self._store,self.config,self._llm,self.ctx.logger)
        self._schedule=ScheduleService(self._store,self.config,self._llm,root,self.ctx.logger)
        self._rest=RestGate(self._store,self.config,self._llm,self._state,self.ctx.logger)
        self._proactive=ProactiveEngine(self.ctx,self._store,self.config,self._env,self.ctx.logger)
        self._debouncer=MessageDebouncer(self.config,self.ctx.logger)
        self._recall=RecallService(self.ctx,self._store,self.config,self.ctx.logger)
        self._vision=VisionService(self.ctx,self._store,self.config,self._llm,self.ctx.logger)
        self._continuity=ContinuityService(self._store,self.config,self._llm,self.ctx.logger)
        self._memory=MemoryService(self._store,self.config,self._llm,self.ctx.logger)
        self._information=InformationService(self.ctx,self._store,self.config,self._llm,self.ctx.logger)
        self._group_observer=GroupObserver(self._store,self.config,self._llm,self.ctx.logger)
        self._relay=RelayService(self.ctx,self._store,self.config,self.ctx.logger)
        self._bookshelf=BookshelfService(self._store,self.config)
        self._creation=CreationService(self.ctx,self._store,self.config,self._llm,self.ctx.logger)
        self._admin=AdminService(self._store,self.config)
        await self._store.sync_users(self.config.users.profiles)
        await self._store.sync_relationship_entries(self.config.social.relations)
        await self._refresh_personality(); await self._resolve_all_streams(); await self._llm.refresh_health()
        if self.config.plugin.enabled:
            await self._maintenance_tick(allow_weather_network=False); self._start_tasks()
            self._spawn_transient(self._env.refresh_weather(force=True),"mai-life-weather-initial")
        self.ctx.logger.info("[MaiLife] 麦麦生活 v1.6.0 加载完成")

    async def on_unload(self)->None:
        self._stopping=True
        if self._debouncer:await self._debouncer.close()
        if self._group_observer:await self._group_observer.close()
        await self._stop_tasks()
        await self._active_tasks.reset()
        if self._recall:self._recall.clear()
        self._session_runtime.clear(); self._reply_confirmations.clear(); self._message_tasks.clear()
        if self._store:await self._store.close()
        self.ctx.logger.info("[MaiLife] 麦麦生活已卸载")

    async def on_config_update(self,scope:str,config_data:dict[str,Any],version:str)->None:
        self._reloading=True
        try:await self._apply_config_update(scope,config_data,version)
        finally:self._reloading=False

    async def _apply_config_update(self,scope:str,config_data:dict[str,Any],version:str)->None:
        del config_data,version
        await self._stop_tasks()
        await self._active_tasks.reset()
        self._active_tasks.update_retention(int(self.config.debounce.turn_expire_seconds))
        self._reply_confirmations.clear()
        if self._group_observer:await self._group_observer.reset()
        for service in (self._llm,self._env,self._state,self._schedule,self._rest,self._proactive,
                        self._debouncer,self._vision,self._continuity,self._memory,self._information,
                        self._group_observer,self._relay,self._bookshelf,self._creation,self._admin,self._recall):
            if service:service.update_config(self.config)
        if self._store and (not self.config.recall.enabled or not self.config.recall.cache_summary_enabled):
            await self._store.clear_recall_summaries()
            if self._recall:self._recall.clear()
        if self._store:await self._store.sync_users(self.config.users.profiles)
        if self._store:await self._store.sync_relationship_entries(self.config.social.relations)
        enabled_ids={str(profile.user_id) for profile in self.config.users.profiles if profile.enabled}
        self._session_runtime={session:item for session,item in self._session_runtime.items()
                               if str(item.get("user_id") or "") in enabled_ids}
        if scope=="bot":await self._refresh_personality()
        if self._llm:await self._llm.refresh_health()
        await self._resolve_all_streams()
        if self.config.plugin.enabled:
            await self._maintenance_tick(allow_weather_network=False); self._start_tasks()
            if self._env and scope not in {"bot","model"}:
                self._spawn_transient(self._env.refresh_weather(force=True),"mai-life-weather-config")
        self.ctx.logger.info(f"[MaiLife] 配置热更新完成 scope={scope}")

    def _spawn_transient(self,coro:Any,name:str,
                         message_keys:Iterable[tuple[str,str]]=())->None:
        task=asyncio.create_task(coro,name=name); self._transient.add(task)
        keys=tuple((str(session),str(message_id)) for session,message_id in message_keys if session and message_id)
        for key in keys:self._message_tasks.setdefault(key,set()).add(task)
        def finished(done:asyncio.Task[Any])->None:
            self._transient.discard(done)
            for key in keys:
                tracked=self._message_tasks.get(key)
                if tracked:
                    tracked.discard(done)
                    if not tracked:self._message_tasks.pop(key,None)
            if not done.cancelled() and (error:=done.exception()) is not None:
                self._get_logger().warning(f"[MaiLife] 临时任务异常 name={done.get_name()}: {error}")
        task.add_done_callback(finished)

    async def _cancel_message_tasks(self,session_id:str,message_id:str)->None:
        tasks=list(self._message_tasks.pop((session_id,message_id),set()))
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

    def _start_tasks(self)->None:
        if self._tasks:return
        self._tasks=[asyncio.create_task(self._maintenance_loop(),name="mai-life-maintenance"),
                     asyncio.create_task(self._proactive_loop(),name="mai-life-proactive"),
                     asyncio.create_task(self._daily_generation_loop(),name="mai-life-daily-generation"),
                     asyncio.create_task(self._information_loop(),name="mai-life-information"),
                     asyncio.create_task(self._creation_loop(),name="mai-life-creation")]

    async def _stop_tasks(self)->None:
        tasks=[*self._tasks,*self._transient]; self._tasks=[]; self._transient.clear()
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self._message_tasks.clear()

    async def _daily_generation_loop(self)->None:
        while True:
            try:
                assert self._env and self._schedule and self._memory
                now=self._env.now(); next_run=now.replace(hour=self.config.schedule.generate_hour,minute=0,second=0,microsecond=0)
                if next_run<=now:next_run+=timedelta(days=1)
                await asyncio.sleep(max(1,(next_run-now).total_seconds()))
                weather=await self._env.refresh_weather()
                current=self._env.now(); memory_context=await self._memory.schedule_context(current)
                await self._schedule.ensure_day(current,self._personality,self._env.weather_text(weather),force=True,
                                                memory_context=memory_context)
            except asyncio.CancelledError:raise
            except Exception as exc:self.ctx.logger.error(f"[MaiLife] 每日日程生成异常: {exc}")

    async def _maintenance_loop(self)->None:
        while True:
            try:
                await asyncio.sleep(max(60,self.config.state.tick_interval_minutes*60)); await self._maintenance_tick()
            except asyncio.CancelledError:raise
            except Exception as exc:self.ctx.logger.error(f"[MaiLife] 状态维护异常: {exc}")

    async def _proactive_loop(self)->None:
        while True:
            try:
                await asyncio.sleep(max(60,self.config.proactive.patrol_interval_minutes*60)); await self._resolve_all_streams()
                if self._proactive and self._store and self._env:
                    now=self._env.now(); await self._proactive.patrol(now,await self._store.get_state())
            except asyncio.CancelledError:raise
            except Exception as exc:self.ctx.logger.error(f"[MaiLife] 主动巡检异常: {exc}")

    async def _information_loop(self)->None:
        while True:
            try:
                await asyncio.sleep(60 if self.config.information.enabled else 300)
                if not self.config.information.enabled:continue
                assert self._information and self._env and self._store and self._schedule
                now=self._env.now(); state=await self._store.get_state(); schedule=await self._schedule.context(now)
                topics=[]
                if self.config.search.include_chat_topics:
                    for user in await self._store.list_users():
                        if str(user.get("role") or "friend")!="owner":continue
                        continuity=await self._store.get_continuity(str(user["user_id"]))
                        topics.extend(str(item)[:120] for item in continuity.get("unresolved_topics") or [])
                await self._information.tick(now,self._personality,state,schedule,topics)
            except asyncio.CancelledError:raise
            except Exception as exc:self.ctx.logger.error(f"[MaiLife] 联网见闻巡检异常: {exc}")

    async def _creation_loop(self)->None:
        while True:
            try:
                interval=int(self.config.creation.patrol_interval_minutes)*60 if self.config.creation.enabled else 300
                await asyncio.sleep(max(60,interval))
                if not self.config.creation.enabled:continue
                assert self._creation and self._env and self._store and self._schedule
                now=self._env.now(); state=await self._store.get_state(); schedule=await self._schedule.context(now)
                await self._creation.tick(now,self._personality,state,schedule)
            except asyncio.CancelledError:raise
            except Exception as exc:self.ctx.logger.error(f"[MaiLife] 书柜创作巡检异常: {exc}")

    async def _maintenance_tick(self,force_weather:bool=False,*,allow_weather_network:bool=True)->None:
        if not self._ready:return
        async with self._maintenance_lock:
            assert self._env and self._schedule and self._store and self._state and self._memory
            now=self._env.now(); weather=await self._env.refresh_weather(force=force_weather,allow_network=allow_weather_network)
            memory_context=await self._memory.schedule_context(now)
            nodes=await self._schedule.ensure_day(now,self._personality,self._env.weather_text(weather),memory_context=memory_context)
            context=await self._schedule.context(now); state=await self._store.get_state()
            await self._schedule.expand_due(now,nodes,state,self._env.weather_text(weather)); context=await self._schedule.context(now)
            last_updated=float(state.get("last_updated_at") or now.timestamp())
            simulation_start=datetime.fromtimestamp(max(last_updated,(now-timedelta(hours=72)).timestamp()),tz=now.tzinfo)
            timeline=await self._schedule.state_timeline(simulation_start,now)
            await self._state.advance_timeline(now,timeline,context.get("current"),context.get("scene"))
            await self._memory.ensure_daily(now)
            await self._settle_relationships(now)
            await self._store.cleanup_runtime_records(now.timestamp(),now.timestamp()-int(self.config.usage.retention_days)*86400)

    async def _settle_relationships(self,now:datetime)->None:
        """按自然日补算离线期间关系变化，最多回看一年即可覆盖温度下限。"""
        if not self._store:return
        users=await self._store.list_users()
        if not users:return
        target=now.date()-timedelta(days=1); starts=[]
        for user in users:
            try:starts.append(date.fromisoformat(str(user.get("last_relation_day") or ""))+timedelta(days=1))
            except ValueError:starts.append(target)
        current=max(target-timedelta(days=365),min(starts))
        while current<=target:
            start=datetime.combine(current,datetime.min.time(),tzinfo=now.tzinfo)
            end=datetime.combine(current+timedelta(days=1),datetime.min.time(),tzinfo=now.tzinfo)
            await self._store.update_relationships(current.isoformat(),start.timestamp(),end.timestamp(),end.timestamp())
            current+=timedelta(days=1)

    async def _refresh_personality(self)->None:
        try:self._personality=str(await self.ctx.config.get("personality.personality","") or "")
        except Exception as exc:self.ctx.logger.warning(f"[MaiLife] 读取人格失败: {exc}")

    async def _resolve_stream(self,user_id:str)->str:
        try:
            info=await self.ctx.chat.get_stream_by_user_id(user_id=user_id)
            if isinstance(info,dict):
                stream=info.get("stream") if isinstance(info.get("stream"),dict) else info
                if stream.get("stream_id"):return str(stream["stream_id"])
        except Exception:pass
        try:
            streams=await self.ctx.chat.get_private_streams()
            if isinstance(streams,dict):streams=streams.get("streams",streams)
            values=streams.values() if isinstance(streams,dict) else streams if isinstance(streams,list) else []
            for item in values:
                if isinstance(item,dict) and str(item.get("user_id") or "")==user_id:return str(item.get("stream_id") or "")
        except Exception as exc:self.ctx.logger.debug(f"[MaiLife] stream 解析失败 user={user_id}: {exc}")
        return ""

    async def _resolve_all_streams(self)->None:
        if not self._store:return
        for user in await self._store.list_users():
            if user.get("stream_id"):continue
            stream=await self._resolve_stream(user["user_id"])
            if stream:await self._store.set_user_stream(user["user_id"],stream)

    async def _user_by_session(self,session_id:str)->dict[str,Any]:
        if not self._store or not session_id:return {}
        runtime=self._session_runtime.get(session_id) or {}; uid=str(runtime.get("user_id") or "")
        if uid:
            user=await self._store.get_user(uid)
            return user if user.get("enabled") else {}
        for user in await self._store.list_users():
            if str(user.get("stream_id") or "")==session_id:return user
        return {}

    async def _relay_for_session(self,session_id:str,task_id:str="")->dict[str,Any]:
        if not self._store or not session_id:return {}
        if task_id:return await self._store.relay_for_task(session_id,task_id)
        return await self._store.pending_relay_context(session_id,time.time())

    async def _activate_planner_task(self,session_id:str,messages:Any)->ActivePluginTask|None:
        """把 Planner 历史中的虚拟任务映射到数据库记录，兼容 task_id 回写竞态。"""
        now=time.time(); marker=latest_plugin_task_marker(messages)
        if not marker:
            return await self._active_tasks.current(session_id,now)
        if marker.plugin_id!=PLUGIN_ID or not marker.task_id.startswith(HOST_TASK_PREFIX):
            previous=await self._active_tasks.clear_active(session_id)
            await self._supersede_active_task(previous)
            return None
        if not self._store:return None
        event_id=str(marker.metadata.get("mai_life_event_id") or "")
        relay_id=str(marker.metadata.get("mai_life_relay_id") or "")
        proactive={}; relay={}
        if event_id:
            # Host 元数据来自本次触发，优先级高于毫秒 task_id，避免极端并发下任务号碰撞。
            candidate=await self._store.proactive_event(event_id) if event_id else {}
            if candidate and str(candidate.get("stream_id") or "")==session_id:
                bound=str(candidate.get("host_task_id") or "")
                if not bound:await self._store.set_proactive_task_id(event_id,marker.task_id)
                if not bound or bound==marker.task_id:proactive=await self._store.proactive_event(event_id)
        elif not relay_id:
            proactive=await self._store.proactive_for_task(session_id,marker.task_id)
        if relay_id:
            candidate=await self._store.relay_candidate(relay_id) if relay_id else {}
            if candidate and str(candidate.get("target_stream_id") or "")==session_id:
                bound=str(candidate.get("host_task_id") or "")
                if not bound:await self._store.set_relay_task_id(relay_id,marker.task_id)
                if not bound or bound==marker.task_id:relay=await self._store.relay_candidate(relay_id)
        elif not event_id:
            relay=await self._store.relay_for_task(session_id,marker.task_id)
        record=relay or proactive
        if not record:
            current=await self._active_tasks.current(session_id,now)
            if current and current.task_id!=marker.task_id:
                previous=await self._active_tasks.clear_active(session_id)
                await self._supersede_active_task(previous)
                return None
            return current
        status=str(record.get("status") or "")
        if status in {"pending","sending"} and float(record.get("expires_at") or 0)<=now:
            if relay:await self._store.set_relay_status(str(record["id"]),"expired",now,"planner_after_expiry")
            else:await self._store.set_proactive_event_status(str(record["id"]),"expired")
            return await self._active_tasks.current(session_id,now)
        return await self._active_tasks.activate(
            session_id,marker,kind="relay" if relay else "proactive",record=record,now=now,
        )

    async def _supersede_active_task(self,task:ActivePluginTask|None)->None:
        """真实入站优先于尚未发送的主动轮，防止旧回复跨入新一轮对话。"""
        if not task or task.sent or not self._store:return
        if task.kind=="proactive":
            record=await self._store.proactive_event(task.record_id)
            if str(record.get("status") or "")=="pending":
                await self._store.set_proactive_event_status(task.record_id,"cancelled")
                if task.opportunity_id:await self._store.release_opportunity(task.opportunity_id)
        elif task.kind=="relay":
            record=await self._store.relay_candidate(task.record_id)
            if str(record.get("status") or "") in {"pending","sending"}:
                await self._store.set_relay_status(task.record_id,"cancelled",time.time(),"new_inbound")
        await self._store.release_reply_turn(task.session_id,task.task_id)
        for key,value in self._reply_confirmations.items():
            if key[0]==task.session_id and value.get("task_id")==task.task_id:value["cancelled"]=True

    async def _cancel_reply_confirmations(self,session_id:str)->None:
        """先同步标记再释放持久化锁，发送 Hook 即使并发进入也能看到取消状态。"""
        if not session_id:return
        matches=[value for (session,_anchor),value in self._reply_confirmations.items() if session==session_id]
        for value in matches:value["cancelled"]=True
        for value in matches:
            if self._store:
                await self._store.release_reply_turn(session_id,str(value.get("turn_anchor") or value.get("anchor") or ""))
                wake_id=str(value.get("wake_message_id") or "")
                if wake_id:await self._store.clear_wake_candidate(session_id,wake_id)
            task_id=str(value.get("task_id") or "")
            if task_id:await self._active_tasks.release_reply(session_id,task_id,str(value.get("anchor") or ""))

    async def _cancel_recalled_confirmations(self,session_id:str,anchors:set[str],message_id:str)->None:
        """撤回只取消命中的回复轮次，不干扰同会话之后真正的新消息。"""
        matches=[]
        for (session,_anchor),value in self._reply_confirmations.items():
            if session!=session_id:continue
            candidates={str(value.get("anchor") or ""),str(value.get("turn_anchor") or ""),
                        str(value.get("wake_message_id") or "")}
            sources={str(item) for item in value.get("source_message_ids") or []}
            if message_id in sources or candidates&anchors:matches.append(value)
        for value in matches:value["cancelled"]=True
        for value in matches:
            turn_anchor=str(value.get("turn_anchor") or value.get("anchor") or "")
            if self._store:
                await self._store.release_reply_turn(session_id,turn_anchor)
                wake_id=str(value.get("wake_message_id") or "")
                if wake_id:await self._store.clear_wake_candidate(session_id,wake_id)
            task_id=str(value.get("task_id") or "")
            if task_id:await self._active_tasks.release_reply(session_id,task_id,str(value.get("anchor") or ""))

    async def _is_recalled(self,session_id:str,*anchors:str)->bool:
        """持久墓碑是 Replyer 与逐段发送之间的最终防线，热重载后同样有效。"""
        if not self._recall or not self.config.recall.enabled or not session_id:return False
        checked:set[str]=set()
        for raw_anchor in anchors:
            anchor=str(raw_anchor or "").strip()
            if not anchor or anchor in checked:continue
            checked.add(anchor)
            try:
                if await self._recall.is_turn_recalled(session_id,anchor):return True
            except Exception as exc:
                self._get_logger().warning(f"[MaiLife] 撤回墓碑查询失败，发送链失败开放: {exc}")
                return False
        return False

    async def _discard_recalled_private_turn(self,session_id:str,user_id:str,turn_anchor:str,
                                              source_message_ids:Iterable[str])->bool:
        sources={str(value) for value in source_message_ids if str(value).strip()}
        if not await self._is_recalled(session_id,turn_anchor,*sources):return False
        for source in sources:await self._cancel_message_tasks(session_id,source)
        if self._store:
            for source_or_anchor in {turn_anchor,*sources}:
                await self._store.redact_recalled_private_artifacts(user_id,source_or_anchor)
                await self._store.clear_wake_candidate(session_id,source_or_anchor)
            await self._store.save_continuity(user_id,"",[],time.time())
        return True

    async def _handle_recall(self,message:dict[str,Any],notice:dict[str,str])->None:
        """先建立持久撤回墓碑，再清理仍在运行的本地派生任务。"""
        if not self._recall or not self._store:return
        _uid,session,_notice_id,_private=message_identity(message)
        if not session:return
        current=time.time(); result=await self._recall.record_notice(session,notice,current)
        message_id=str(result.get("message_id") or "")
        anchors={str(value) for value in result.get("anchors") or [] if str(value).strip()}
        await self._cancel_message_tasks(session,message_id)
        if self._debouncer:await self._debouncer.recall(session,message_id)
        await self._cancel_recalled_confirmations(session,anchors,message_id)
        for source_or_anchor in {message_id,*anchors}:
            await self._store.clear_wake_candidate(session,source_or_anchor)
        user_id=str(result.get("user_id") or ""); group_id=str(result.get("group_id") or "")
        if user_id and not group_id:
            # 合并补话中的任意来源撤回后，整轮派生状态都应失效，不能只删原始 ID。
            for source_or_anchor in {message_id,*anchors}:
                await self._store.redact_recalled_private_artifacts(user_id,source_or_anchor)
            await self._store.save_continuity(user_id,"",[],current)
        if group_id and self._group_observer and self._env:
            await self._group_observer.recall(group_id,message_id,self._env.now())
        if result.get("needs_summary_recovery"):
            self._spawn_transient(
                self._recall.recover_notice_summary(session,notice,current),
                f"mai-life-recall-summary-{message_id}",
            )
        runtime=self._session_runtime.get(session)
        if runtime and message_id in {str(value) for value in runtime.get("source_message_ids") or []}:
            runtime["recalled"]=True
        self.ctx.logger.info(
            f"[MaiLife] 已处理撤回通知 session={session} message={message_id} "
            f"type={notice.get('notice_type')} adapter={notice.get('adapter')}"
        )

    @staticmethod
    def _message_additional(message:dict[str,Any])->dict[str,Any]:
        info=message.get("message_info")
        if not isinstance(info,dict):info={}; message["message_info"]=info
        additional=info.get("additional_config")
        if not isinstance(additional,dict):additional={}; info["additional_config"]=additional
        return additional

    @HookHandler("chat.receive.before_process",mode=HookMode.BLOCKING,order=HookOrder.EARLY,timeout_ms=30000)
    async def on_receive(self,**kwargs:Any)->dict[str,Any]:
        message=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        if not message:return {"action":"continue"}
        notice=recall_notice(message)
        if notice and self.config.plugin.enabled:
            # 通知不进入 Maisaka；总开关只决定是否建立墓碑和取消派生任务。
            if self.config.recall.enabled and self._ready and not self._stopping:
                try:await self._handle_recall(message,notice)
                except Exception as exc:self.ctx.logger.error(f"[MaiLife] 撤回通知处理失败: {exc}")
            return {"action":"abort"}
        if self._stopping or self._reloading or not self.config.plugin.enabled or not self._ready:return {"action":"continue"}
        if message.get("is_notify"):return {"action":"continue"}
        uid,session,mid,private=message_identity(message)
        assert self._store and self._env and self._schedule and self._rest and self._debouncer and self._vision and self._continuity and self._memory and self._recall
        self._recall.note_inbound(message)
        try:await self._recall.register_turn(message)
        except Exception as exc:self.ctx.logger.warning(f"[MaiLife] 撤回轮次注册失败，消息继续处理: {exc}")
        initial_sources=self._recall.source_message_ids(message)
        if session:
            initial_text=direct_text(message); initial_media=media_types(message)
            self._session_runtime[session]={
                "user_id":uid,"message_id":mid,"source_message_ids":initial_sources,
                "intent":classify_intent(initial_text,initial_media),"recall_query":is_recall_query(initial_text),
                "media":initial_media,"platform":str(message.get("platform") or "qq"),
                "adapter":adapter_name(message),"chat_type":"private" if private else "group",
                "updated_at":time.time(),
            }
        if session:
            # 新的真实平台消息开启新轮次，旧主动任务不得再借会话兜底关联到本轮回复。
            await self._cancel_reply_confirmations(session)
            previous=await self._active_tasks.note_inbound(session,time.time())
            await self._supersede_active_task(previous)
        if not private:
            if uid and not is_command(message) and self._group_observer and self.config.social.enabled:
                # 群聊观察使用独立后台缓冲；无论整理成功与否都不阻塞 Host 原有群聊链。
                self._spawn_transient(
                    self._group_observer.observe(message,self._env.now()),f"mai-life-group-{mid}",
                    message_keys=[(session,source) for source in initial_sources],
                )
            if await self._is_recalled(session,mid,*initial_sources):
                # 撤回处理可能早于群观察任务注册；这里补做一次按来源取消，封住并发窗口。
                for source in initial_sources:await self._cancel_message_tasks(session,source)
                group_id,_group_name=group_identity(message)
                if group_id and self._group_observer:
                    for source in initial_sources:await self._group_observer.recall(group_id,source,self._env.now())
                return {"action":"abort"}
            return {"action":"continue"}
        if not uid or is_command(message):
            return {"action":"abort"} if await self._is_recalled(session,mid,*initial_sources) else {"action":"continue"}
        user=await self._store.get_user(uid)
        if not user or not user.get("enabled"):
            return {"action":"abort"} if await self._is_recalled(session,mid,*initial_sources) else {"action":"continue"}
        if session and session!=user.get("stream_id"):await self._store.set_user_stream(uid,session)

        # 视觉任务与收口等待并行；旧一代消息被合并后会取消自己的无效视觉任务。
        vision_task=asyncio.create_task(self._vision.summarize_if_needed(message),name=f"mai-life-vision-{mid}")
        started=time.monotonic()
        try:allowed,merged,reason=await self._debouncer.collect(message)
        except Exception as exc:
            vision_task.cancel(); await asyncio.gather(vision_task,return_exceptions=True)
            self.ctx.logger.warning(f"[MaiLife] 消息收口失败，失败开放: {exc}")
            return {"action":"continue"}
        if not allowed:
            vision_task.cancel(); await asyncio.gather(vision_task,return_exceptions=True)
            return {"action":"abort"}
        if self._stopping or self._reloading or not self.config.plugin.enabled:
            # 热禁用可能发生在防抖等待期间；此时保留合并结果，但不再记录或阻断消息。
            vision_task.cancel(); await asyncio.gather(vision_task,return_exceptions=True)
            kwargs["message"]=merged
            return {"action":"continue","modified_kwargs":kwargs}
        # 视觉摘要是内部背景，互动/意图/休息判断只能使用用户原始文本。
        user_text=direct_text(merged); user_media=media_types(merged)
        remaining=max(0.01,float(self.config.vision.timeout_seconds)-(time.monotonic()-started)); summary=""
        try:summary=await asyncio.wait_for(vision_task,timeout=remaining)
        except (asyncio.TimeoutError,asyncio.CancelledError):vision_task.cancel()
        except Exception as exc:self.ctx.logger.debug(f"[MaiLife] 视觉摘要降级: {exc}")
        # “先发图后补文字”时旧任务会被取消；多图补话则必须用组合哈希重新理解完整一轮。
        merged_info=merged.get("message_info") if isinstance(merged.get("message_info"),dict) else {}
        merged_additional=merged_info.get("additional_config") if isinstance(merged_info.get("additional_config"),dict) else {}
        merged_ids=merged_additional.get("mai_life_merged_message_ids")
        needs_merged_vision=bool(
            isinstance(merged_ids,list) and len(merged_ids)>1
            and any(kind in user_media for kind in ("image","gif","reply","forward"))
        )
        remaining=max(0.0,float(self.config.vision.timeout_seconds)-(time.monotonic()-started))
        if (not summary or needs_merged_vision) and remaining>0.05:
            try:
                merged_summary=await asyncio.wait_for(self._vision.summarize_if_needed(merged),timeout=remaining)
                if merged_summary:summary=merged_summary
            except (asyncio.TimeoutError,asyncio.CancelledError):pass
            except Exception as exc:self.ctx.logger.debug(f"[MaiLife] 合并图片摘要降级: {exc}")
        uid,session,mid,_=message_identity(merged); text=user_text; media=user_media
        source_ids=self._recall.source_message_ids(merged)
        try:await self._recall.register_turn(merged)
        except Exception as exc:self.ctx.logger.warning(f"[MaiLife] 合并撤回轮次注册失败，消息继续处理: {exc}")
        if await self._discard_recalled_private_turn(session,uid,mid,source_ids):return {"action":"abort"}
        intent=classify_intent(text,media)
        self._session_runtime[session]={"user_id":uid,"message_id":mid,"source_message_ids":source_ids,
                                        "intent":intent,"recall_query":is_recall_query(text),"media":media,
                                        "platform":str(merged.get("platform") or "qq"),"adapter":adapter_name(merged),
                                        "chat_type":"private","updated_at":time.time()}
        await self._store.record_interaction(
            uid,text or f"发送了{','.join(media) or '一条消息'}",
            self._env.now().timestamp(),self._env.now().hour,source_message_id=mid,
        )
        task_keys=[(session,source) for source in source_ids]
        self._spawn_transient(self._continuity.refresh(uid,intent),f"mai-life-continuity-{uid}",message_keys=task_keys)
        self._spawn_transient(
            self._memory.observe_message(uid,text,self._env.now(),source_message_id=mid),
            f"mai-life-date-{uid}",message_keys=task_keys,
        )
        context=await self._schedule.context(self._env.now())
        gate_allowed,gate_reason=await self._rest.decide(uid,text,self._env.now(),context.get("current"),session_id=session,message_id=mid)
        if not gate_allowed:
            if await self._discard_recalled_private_turn(session,uid,mid,source_ids):return {"action":"abort"}
            await self._store.add_rest_backlog(
                uid,text or f"发送了{','.join(media) or '一条消息'}",
                self._env.now().timestamp(),source_message_id=mid,
            )
            await self._discard_recalled_private_turn(session,uid,mid,source_ids)
            self.ctx.logger.info(f"[MaiLife] 休息闸门阻断 user={uid} reason={gate_reason}")
            return {"action":"abort"}
        if await self._discard_recalled_private_turn(session,uid,mid,source_ids):return {"action":"abort"}
        kwargs["message"]=merged
        self.ctx.logger.debug(f"[MaiLife] 消息收口完成 session={session} {reason} media={media}")
        return {"action":"continue","modified_kwargs":kwargs}

    async def _prompt_payload(self,session_id:str,consume_backlog:bool=False)->dict[str,Any]|None:
        if self._stopping or self._reloading or not self.config.plugin.enabled or not self._ready:return None
        assert self._store and self._env and self._schedule and self._memory and self._information and self._bookshelf
        user=await self._user_by_session(session_id)
        if not user:return None
        now=self._env.now(); runtime=self._session_runtime.get(session_id) or {}
        backlogs=await (self._store.consume_rest_backlogs(user["user_id"]) if consume_backlog else self._store.peek_rest_backlogs(user["user_id"]))
        return {"state":await self._store.get_state(),"weather":await self._store.get_weather() or {"description":"天气未知"},
                "context":await self._schedule.context(now),"user":user,"dream":await self._store.latest_dream(),"backlogs":backlogs,
                "environment":self._env.snapshot(now,platform=str(runtime.get("platform") or "qq"),adapter=str(runtime.get("adapter") or "unknown"),
                                                 chat_type="private",media=runtime.get("media") or ["text"]),
                "continuity":await self._store.get_continuity(user["user_id"]),"intent":str(runtime.get("intent") or ""),
                "images":await self._store.current_image_summaries(session_id,now.timestamp()),
                "memory":await self._memory.context_for_user(user,now),
                "information":await self._information.context(now),
                "bookshelf":await self._bookshelf.context_for_user(user)}

    @staticmethod
    def _planner_messages_with_context(messages:Any,suffix:str)->list[dict[str,Any]]:
        """Planner Hook 只接受 messages；把背景并入 system 消息而不覆盖其他插件内容。"""
        result=[dict(item) for item in messages if isinstance(item,dict)] if isinstance(messages,list) else []
        for item in result:
            if str(item.get("role") or "").lower()=="system" and isinstance(item.get("content"),str):
                item["content"]=str(item["content"])+suffix
                return result
        result.insert(0,{"role":"system","content":suffix.strip()})
        return result

    @HookHandler("maisaka.planner.before_request",mode=HookMode.BLOCKING)
    async def on_planner(self,**kwargs:Any)->dict[str,Any]:
        if self._stopping or self._reloading or not self.config.plugin.enabled:return {"action":"continue"}
        session=str(kwargs.get("session_id") or ""); suffix=""
        active=await self._activate_planner_task(session,kwargs.get("messages"))
        if self.config.context.enabled:
            payload=await self._prompt_payload(session)
            if payload:
                suffix=self._prompts.planner(payload["state"],payload["weather"],payload["context"],payload["user"],payload["dream"],
                                             payload["backlogs"],payload["environment"],payload["continuity"],payload["intent"],
                                             int(self.config.context.prompt_max_chars),memory=payload["memory"],information=payload["information"],
                                             bookshelf=payload["bookshelf"],image_summaries=payload["images"])
        if self._recall and self.config.recall.enabled:
            suffix+=await self._recall.planner_context(session)
            runtime=self._session_runtime.get(session) or {}
            if runtime.get("recall_query"):
                user=await self._user_by_session(session)
                if user:suffix+=await self._recall.query_prompt_context(session,str(user.get("user_id") or ""))
        if active:suffix+=self._active_tasks.planner_instruction(active)
        if active and active.kind=="relay" and self._relay and self.config.social.enabled:
            suffix+=await self._relay.prompt_context(session,active.task_id)
        if not suffix:return {"action":"continue"}
        kwargs["messages"]=self._planner_messages_with_context(kwargs.get("messages"),suffix)
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("maisaka.planner.after_response",mode=HookMode.OBSERVE)
    async def on_planner_after(self,**kwargs:Any)->None:
        session=str(kwargs.get("session_id") or "")
        active=await self._active_tasks.current(session,time.time())
        scoped=bool(await self._user_by_session(session) or active or await self._relay_for_session(session))
        if self.config.plugin.enabled and scoped and self._llm:
            await self._llm.record_observed(source="host_planner",task_name="planner",request_type="planner",
                model_name="",prompt_tokens=int(kwargs.get("prompt_tokens") or 0),completion_tokens=int(kwargs.get("completion_tokens") or 0),
                total_tokens=int(kwargs.get("total_tokens") or 0))

    @HookHandler("maisaka.replyer.before_request",mode=HookMode.BLOCKING)
    async def on_replyer(self,**kwargs:Any)->dict[str,Any]:
        if self._stopping or self._reloading or not self.config.plugin.enabled:return {"action":"continue"}
        session=str(kwargs.get("session_id") or ""); suffix=""
        active=await self._active_tasks.current(session,time.time())
        if self.config.context.enabled:
            payload=await self._prompt_payload(session,consume_backlog=True)
            if payload:
                suffix=self._prompts.replyer(payload["state"],payload["weather"],payload["context"],payload["user"],payload["backlogs"],
                                             payload["environment"],payload["continuity"],payload["intent"],payload["images"],
                                             min(2400,int(self.config.context.prompt_max_chars)),memory=payload["memory"],information=payload["information"],
                                             bookshelf=payload["bookshelf"])
        if self._recall and self.config.recall.enabled:
            suffix+=await self._recall.planner_context(session)
            runtime=self._session_runtime.get(session) or {}
            if runtime.get("recall_query"):
                user=await self._user_by_session(session)
                if user:suffix+=await self._recall.query_prompt_context(session,str(user.get("user_id") or ""))
        if active and active.kind=="relay" and self._relay and self.config.social.enabled:
            suffix+=await self._relay.prompt_context(session,active.task_id)
        if not suffix:return {"action":"continue"}
        kwargs["extra_prompt"]=(kwargs.get("extra_prompt") or "")+suffix
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("maisaka.replyer.after_response",mode=HookMode.BLOCKING)
    async def on_replyer_after(self,**kwargs:Any)->dict[str,Any]:
        session=str(kwargs.get("session_id") or ""); response=str(kwargs.get("response") or "").strip()
        anchor=str(kwargs.get("reply_message_id") or ""); now=time.time()
        active=await self._active_tasks.current(session,now)
        task_id=active.task_id if active else anchor
        runtime=self._session_runtime.get(session) or {}
        recall_sources=[str(value) for value in runtime.get("source_message_ids") or [] if str(value).strip()] if not active else []
        recall_turn_anchor=str(runtime.get("message_id") or "") if not active else ""
        if response and await self._is_recalled(session,anchor,recall_turn_anchor,*recall_sources):
            kwargs["response"]=""
            self._get_logger().info(f"[MaiLife] 已撤回轮次的 Replyer 输出已取消 session={session} anchor={anchor}")
            return {"action":"continue","modified_kwargs":kwargs}
        if response and not active and recall_sources and anchor and self._recall:
            try:
                await self._recall.register_reply_anchor(
                    session,anchor,recall_sources,str(runtime.get("user_id") or ""),now,
                )
            except Exception as exc:self._get_logger().warning(f"[MaiLife] Replyer 撤回锚点注册失败: {exc}")
        user=await self._user_by_session(session)
        relay_task=await self._relay_for_session(session,task_id) if task_id else {}
        proactive=await self._store.proactive_for_task(session,task_id) if self._store and task_id else {}
        own_task=bool(active or anchor.startswith(HOST_TASK_PREFIX))
        if not user and not relay_task and not proactive and not own_task:return {"action":"continue"}
        if self.config.plugin.enabled and self._llm:
            await self._llm.record_observed(source="host_replyer",task_name=str(kwargs.get("task_name") or "replyer"),
                request_type=str(kwargs.get("request_type") or "replyer"),model_name=str(kwargs.get("requested_model_name") or ""),
                prompt_tokens=int(kwargs.get("prompt_tokens") or 0),completion_tokens=int(kwargs.get("completion_tokens") or 0),
                total_tokens=int(kwargs.get("total_tokens") or 0),success=bool(response))
        if not response:return {"action":"continue"}
        if active and active.sent:
            kwargs["response"]=""
            self._get_logger().info(f"[MaiLife] 主动任务后续 Replyer 已收口 session={session} task={active.task_id}")
            return {"action":"continue","modified_kwargs":kwargs}
        if own_task and not relay_task and not proactive:
            # RPC 在热重载时可能已排队但来不及写回 task_id；无关联任务一律静默，避免失控发送。
            kwargs["response"]=""; return {"action":"continue","modified_kwargs":kwargs}
        proactive_pending=bool(proactive and str(proactive.get("status") or "")=="pending"
                               and float(proactive.get("expires_at") or 0)>now)
        if proactive and not proactive_pending:
            if str(proactive.get("status") or "")=="pending" and self._store:
                await self._store.set_proactive_event_status(str(proactive["id"]),"expired")
            kwargs["response"]=""; return {"action":"continue","modified_kwargs":kwargs}
        relay_status=str(relay_task.get("status") or "")
        relay_expired=bool(relay_task and relay_status in {"pending","sending"}
                           and float(relay_task.get("expires_at") or 0)<=now)
        if relay_expired and self._store:
            await self._store.set_relay_status(str(relay_task["id"]),"expired",now,"reply_after_expiry")
        if relay_task and (relay_expired or relay_status in {"superseded","failed","expired","cancelled"}):
            kwargs["response"]=""; return {"action":"continue","modified_kwargs":kwargs}
        cancel_proactive=bool(proactive and (self._stopping or self._reloading or not self.config.plugin.enabled or not self.config.proactive.enabled))
        cancel_relay=bool(relay_task and (self._stopping or self._reloading or not self.config.plugin.enabled or not self.config.social.enabled))
        if cancel_proactive:
            await self._store.set_proactive_event_status(str(proactive["id"]),"cancelled")
            await self._store.release_opportunity(str(proactive["opportunity_id"]))
        if cancel_relay:
            await self._store.set_relay_status(str(relay_task["id"]),"cancelled",now,"plugin_disabled")
        if cancel_proactive or cancel_relay:
            kwargs["response"]=""; return {"action":"continue","modified_kwargs":kwargs}
        if not self.config.plugin.enabled:return {"action":"continue"}
        if user and str(user.get("role") or "friend")=="friend":
            matched=next((term for term in self.config.context.owner_only_terms if term and term in response),"")
            if matched:
                retry_count=int(kwargs.get("retry_count") or 0); max_retries=int(kwargs.get("max_retries") or 0)
                if retry_count<max_retries:
                    kwargs.update({"retry":True,"retry_reason":"当前对象是普通朋友，禁止主人/恋人专属称呼和私密上下文。",
                                   "matched_regex":"mai_life_friend_boundary","matched_regex_pattern":matched,
                                   "matched_regex_description":"朋友关系边界"})
                else:kwargs.update({"response":"","retry":False})
                return {"action":"continue","modified_kwargs":kwargs}
        turn_anchor=task_id if active else anchor
        turn_reserved=False
        if self.config.debounce.outbound_turn_guard and turn_anchor and self._store:
            turn_reserved=await self._store.reserve_reply_turn(
                session,turn_anchor,time.time(),time.time()+int(self.config.debounce.turn_expire_seconds),
            )
            if not turn_reserved:
                kwargs["response"]=""
                self._get_logger().info(f"[MaiLife] 同轮重复 Replyer 已收口 session={session} anchor={turn_anchor}")
                return {"action":"continue","modified_kwargs":kwargs}
        if active and not await self._active_tasks.reserve_reply(session,active.task_id,anchor,now):
            if turn_reserved and self._store:await self._store.release_reply_turn(session,turn_anchor)
            kwargs["response"]=""
            return {"action":"continue","modified_kwargs":kwargs}
        expiry=max(20,int(self.config.debounce.turn_expire_seconds))
        self._reply_confirmations={key:value for key,value in self._reply_confirmations.items()
                                   if now-float(value.get("created_at") or 0)<expiry}
        self._reply_confirmations[(session,anchor)]={
            "anchor":anchor,"turn_anchor":turn_anchor,"task_id":task_id if active else "","created_at":now,
            "wake_message_id":str(runtime.get("message_id") or "") if user and not active else "",
            "recall_turn_anchor":recall_turn_anchor,"source_message_ids":recall_sources,
            "proactive_event_id":str(proactive.get("id") or "") if proactive_pending else "",
            "proactive_opportunity_id":str(proactive.get("opportunity_id") or "") if proactive_pending else "",
        }
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("send_service.before_send",mode=HookMode.BLOCKING)
    async def on_send_before(self,**kwargs:Any)->dict[str,Any]:
        message=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        if not message:return {"action":"continue"}
        anchor=str(kwargs.get("reply_message_id") or ""); session=str(message.get("session_id") or "")
        pending_confirmation=self._reply_confirmations.get((session,anchor))
        def recall_anchors()->set[str]:
            values={anchor}
            if pending_confirmation:
                values.update(str(pending_confirmation.get(key) or "") for key in (
                    "turn_anchor","recall_turn_anchor","wake_message_id",
                ))
                values.update(str(value) for value in pending_confirmation.get("source_message_ids") or [])
            return {value for value in values if value}
        async def recalled_now()->bool:
            values=recall_anchors()
            if not await self._is_recalled(session,*values):return False
            if self._store:
                for value in values:await self._store.clear_wake_candidate(session,value)
            if pending_confirmation:
                pending_confirmation["cancelled"]=True
                await self._cancel_recalled_confirmations(session,values,anchor)
                self._reply_confirmations.pop((session,anchor),None)
            return True
        def confirmation_cancelled()->bool:
            return bool(pending_confirmation and pending_confirmation.get("cancelled")
                        and self._reply_confirmations.get((session,anchor)) is pending_confirmation)
        if await recalled_now():return {"action":"abort"}
        if confirmation_cancelled():
            self._reply_confirmations.pop((session,anchor),None)
            return {"action":"abort"}
        active=await self._active_tasks.current(session,time.time())
        task_id=active.task_id if active else anchor
        proactive_task=await self._store.proactive_for_task(session,task_id) if self._store and task_id else {}
        modified=False
        if proactive_task:
            if bool(kwargs.get("set_reply")) and (anchor==task_id or anchor.startswith(HOST_TASK_PREFIX)):
                # proactive:* 是 Host 内部锚点，不是 QQ 消息号；禁止 NapCat 将其编码成无效 reply 段。
                kwargs["set_reply"]=False; modified=True
            status=str(proactive_task.get("status") or "")
            expired=status=="pending" and float(proactive_task.get("expires_at") or 0)<=time.time()
            disabled=self._stopping or not self.config.plugin.enabled or not self.config.proactive.enabled
            if expired:await self._store.set_proactive_event_status(str(proactive_task["id"]),"expired")
            elif disabled and status=="pending":
                await self._store.set_proactive_event_status(str(proactive_task["id"]),"cancelled")
                await self._store.release_opportunity(str(proactive_task["opportunity_id"]))
            if expired or disabled or status in {"failed","expired","cancelled"}:return {"action":"abort"}
        relay_task=await self._relay_for_session(session,task_id) if task_id else {}
        if anchor.startswith(HOST_TASK_PREFIX) and not proactive_task and not relay_task:
            return {"action":"abort"}
        if relay_task and bool(kwargs.get("set_reply")) and (anchor==task_id or anchor.startswith(HOST_TASK_PREFIX)):
            kwargs["set_reply"]=False; modified=True
        if relay_task and (self._stopping or not self.config.plugin.enabled or not self.config.social.enabled):
            if str(relay_task.get("status") or "") in {"pending","sending"} and self._store:
                await self._store.set_relay_status(str(relay_task["id"]),"cancelled",time.time(),"plugin_disabled")
            return {"action":"abort"}
        if active:
            # 关联 ID 与真实引用 ID 分离：任务状态按 task_id 结算，QQ 引用仍保留 Planner 选择的数字消息号。
            if not await self._active_tasks.reserve_send(session,active.task_id,anchor,time.time()):
                return {"action":"abort"}
            additional=self._message_additional(message)
            additional["mai_life_active_task_id"]=active.task_id
            additional["mai_life_reply_anchor"]=anchor
            kwargs["message"]=message; modified=True
        # 上面的数据库查询存在 await；真实入站可能在此期间取消旧轮次，因此在交给适配器前复核一次。
        if await recalled_now() or confirmation_cancelled():
            self._reply_confirmations.pop((session,anchor),None)
            return {"action":"abort"}
        if not self._relay or not self.config.plugin.enabled or not self.config.social.enabled:
            return {"action":"continue","modified_kwargs":kwargs} if modified else {"action":"continue"}
        if await self._relay.should_abort_send(message,task_id):return {"action":"abort"}
        mutated,reserved=await self._relay.mutate_before_send(message,task_id)
        if await recalled_now() or confirmation_cancelled():
            self._reply_confirmations.pop((session,anchor),None)
            return {"action":"abort"}
        if not reserved:
            return {"action":"continue","modified_kwargs":kwargs} if modified else {"action":"continue"}
        kwargs["message"]=mutated
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("send_service.after_send",mode=HookMode.OBSERVE)
    async def on_send_after(self,**kwargs:Any)->None:
        message=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        sent=bool(kwargs.get("sent")); now_ts=time.time()
        # 即使发送期间刚好热禁用了社交模块，也必须收口已经进入 sending 的候选。
        if self._relay:await self._relay.confirm_after_send(message,sent)
        session=str(message.get("session_id") or ""); anchor=str(kwargs.get("reply_message_id") or "")
        info=message.get("message_info") if isinstance(message.get("message_info"),dict) else {}
        additional=info.get("additional_config") if isinstance(info.get("additional_config"),dict) else {}
        tagged_task_id=str(additional.get("mai_life_active_task_id") or "")
        if tagged_task_id:
            await self._active_tasks.finish_send(session,tagged_task_id,anchor,sent,now_ts)
        confirmation=self._reply_confirmations.pop((session,anchor),None)
        if not confirmation and not anchor:
            # 兼容少数不回传 reply_message_id 的 Host，只在会话中恰有一个候选时降级关联。
            matches=[key for key in self._reply_confirmations if key[0]==session]
            if len(matches)==1:confirmation=self._reply_confirmations.pop(matches[0],None)
        if confirmation:
            anchor=str(confirmation.get("anchor") or anchor)
        task_id=tagged_task_id or str((confirmation or {}).get("task_id") or "") or anchor
        if task_id and not tagged_task_id and confirmation and confirmation.get("task_id"):
            await self._active_tasks.finish_send(session,task_id,anchor,sent,now_ts)
        proactive={}
        if not confirmation and self._store and task_id:
            proactive=await self._store.proactive_for_task(session,task_id)
        # before_send 已完成过期检查；平台 I/O 跨过截止秒时仍应按实际发送结果结算。
        proactive_pending=bool(proactive and str(proactive.get("status") or "")=="pending")
        if not confirmation and not proactive_pending:
            # 无锚点发送无法安全归因；不能因此误叫醒正在休息的麦麦。
            if not tagged_task_id and anchor and sent and self._env and self._rest:
                await self._rest.commit_for_send(session,self._env.now(),anchor)
            elif not tagged_task_id and anchor and not sent and self._store:
                await self._store.clear_wake_candidate(session,anchor)
            return
        confirmation=confirmation or {"anchor":anchor,"turn_anchor":task_id,"task_id":task_id,
                                      "proactive_event_id":str(proactive["id"]),
                                      "proactive_opportunity_id":str(proactive.get("opportunity_id") or "")}
        anchor=str(confirmation.get("anchor") or "")
        turn_anchor=str(confirmation.get("turn_anchor") or anchor)
        active_attribution=bool(tagged_task_id or confirmation.get("task_id") or proactive)
        if not sent:
            if self._store:await self._store.release_reply_turn(session,turn_anchor)
            wake_id=str(confirmation.get("wake_message_id") or anchor)
            if self._store and not active_attribution:await self._store.clear_wake_candidate(session,wake_id)
            active_task_id=str(confirmation.get("task_id") or tagged_task_id)
            if active_task_id:await self._active_tasks.release_reply(session,active_task_id,anchor)
            event_id=str(confirmation.get("proactive_event_id") or "")
            if self._store and event_id:
                await self._store.set_proactive_event_status(event_id,"failed")
                opportunity_id=str(confirmation.get("proactive_opportunity_id") or "")
                if opportunity_id:await self._store.release_opportunity(opportunity_id)
            return
        if self._env and self._rest and not active_attribution:
            await self._rest.commit_for_send(
                session,self._env.now(),str(confirmation.get("wake_message_id") or anchor),
            )
        if self._store and self._env:
            event_id=str(confirmation.get("proactive_event_id") or "")
            if event_id:
                now=self._env.now()
                await self._store.mark_pending_sent(session,now.timestamp(),now.strftime("%Y-%m-%d"),event_id=event_id)

    def _is_admin(self,user_id:str)->bool:
        admins=[str(item).strip() for item in self.config.plugin.admin_user_ids if str(item).strip()]
        if not admins:
            admins=[str(profile.user_id).strip() for profile in self.config.users.profiles
                    if profile.enabled and str(profile.user_id).strip()][:1]
        return user_id in admins

    async def _is_owner_or_admin(self,user_id:str)->bool:
        if self._is_admin(user_id):return True
        user=await self._store.get_user(user_id) if self._store and user_id else {}
        return bool(user and str(user.get("role") or "friend")=="owner")

    async def _configured_command_user(self,user_id:str)->dict[str,Any]:
        user=await self._store.get_user(user_id) if self._store and user_id else {}
        return user if user and user.get("enabled") else {}

    async def _command_user(self,kwargs:dict[str,Any])->dict[str,Any]:
        """命令业务执行前统一确认来源是配置用户的私聊，避免群聊产生隐藏副作用。"""
        if str(kwargs.get("group_id") or "").strip():return {}
        return await self._configured_command_user(str(kwargs.get("user_id") or ""))

    async def _send_command(self,kwargs:dict[str,Any],text:str)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._store.get_user(uid) if self._store and uid else {}
        if kwargs.get("group_id") or not user or not user.get("enabled"):text="该命令仅对已配置的私聊用户开放。"
        try:
            ok=await self.ctx.send.text(text=text,stream_id=str(kwargs.get("stream_id") or ""))
            return bool(ok),"命令结果已发送" if ok else "发送返回 False",2
        except Exception as exc:return False,f"发送失败: {exc}",2

    @Command(name="/mai_status",pattern=r"^/mai_status\b",description="查看麦麦生活与消息管线状态")
    async def cmd_status(self,**kwargs:Any)->tuple[bool,str,int]:
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        return await self._send_command(kwargs,await self._status_report())

    @Command(name="/mai_schedule",pattern=r"^/mai_schedule\b",description="查看今日框架与当前场景")
    async def cmd_schedule(self,**kwargs:Any)->tuple[bool,str,int]:
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        return await self._send_command(kwargs,await self._schedule_report())

    @Command(name="/mai_relation",pattern=r"^/mai_relation\b",description="查看当前用户关系")
    async def cmd_relation(self,**kwargs:Any)->tuple[bool,str,int]:
        user=await self._command_user(kwargs)
        if not user:return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        text=(f"关系角色：{user.get('role','friend')}\n关系温度：{float(user['temperature']):.1f}/100\n"
              f"关系阶段：{relationship_stage(float(user['temperature']))}\n每日主动上限：{user.get('daily_proactive_max',1)}")
        return await self._send_command(kwargs,text)

    @Command(name="/mai_recalled",pattern=r"^/mai_recalled\b",description="查询本人私聊的最近撤回摘要")
    async def cmd_recalled(self,**kwargs:Any)->tuple[bool,str,int]:
        user=await self._command_user(kwargs)
        if not user or not self._recall:
            return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        session=str(kwargs.get("stream_id") or user.get("stream_id") or "")
        context=await self._recall.query_context(session,str(user.get("user_id") or ""))
        return await self._send_command(kwargs,self._recall.format_query_result(context))

    @Command(name="/mai_diary",pattern=r"^/mai_diary\b",description="主人或管理员查看最近生活日记")
    async def cmd_diary(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看私人日记。")
        entries=await self._store.list_diaries(3) if self._store else []
        if not entries:return await self._send_command(kwargs,"还没有生成生活日记。")
        lines=["麦麦最近的生活日记"]
        for item in entries:lines.append(f"\n{item['day']}｜{item['title']}\n{item['content']}\n心情：{item['mood_summary']}")
        return await self._send_command(kwargs,"\n".join(lines))

    @Command(name="/mai_dates",pattern=r"^/mai_dates\b",description="查看当前用户的重要日期和待确认项")
    async def cmd_dates(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        dates=await self._store.list_important_dates(uid) if self._store else []
        candidates=await self._store.list_date_candidates(uid) if self._store else []
        lines=["你的重要日期"]
        lines.extend(f"#{item['id']} {item['event_date']} {item['event_name']}"+("（每年）" if item['recurrence']=="annual" else "") for item in dates)
        if not dates:lines.append("暂无正式日期。")
        if candidates:
            lines.append("\n待确认")
            lines.extend(f"#{item['id']} {item['event_name']}｜{item['date_text']}"+(f"｜建议 {item['suggested_date']}" if item['suggested_date'] else "") for item in candidates)
        return await self._send_command(kwargs,"\n".join(lines))

    @Command(name="/mai_date_add",pattern=r"^/mai_date_add\s+(?P<event_date>\d{4}-\d{2}-\d{2})\s+(?P<event_name>.+)$",description="添加当前用户的重要日期")
    async def cmd_date_add(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        raw_date=str(groups.get("event_date") or ""); name=str(groups.get("event_name") or "").strip()[:120]
        try:parsed=date.fromisoformat(raw_date)
        except ValueError:return await self._send_command(kwargs,"日期格式无效，请使用 YYYY-MM-DD。")
        recurrence="annual" if any(word in name for word in ("生日","纪念日")) else "none"
        saved=await self._store.add_important_date(uid,name,parsed.isoformat(),recurrence,"manual",self._env.now().timestamp()) if self._store and self._env and name else 0
        return await self._send_command(kwargs,f"已记录：{parsed.isoformat()} {name}" if saved else "未能添加日期，请检查输入。")

    @Command(name="/mai_date_remove",pattern=r"^/mai_date_remove\s+(?P<date_id>\d+)$",description="删除当前用户的重要日期")
    async def cmd_date_remove(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        removed=await self._store.remove_important_date(int(groups.get("date_id") or 0),uid) if self._store else False
        return await self._send_command(kwargs,"已删除该日期。" if removed else "没有找到属于你的该日期。")

    @Command(name="/mai_date_confirm",pattern=r"^/mai_date_confirm\s+(?P<candidate_id>\d+)(?:\s+(?P<event_date>\d{4}-\d{2}-\d{2}))?$",description="确认当前用户的日期候选")
    async def cmd_date_confirm(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        candidate_id=int(groups.get("candidate_id") or 0); candidates=await self._store.list_date_candidates(uid) if self._store else []
        candidate=next((item for item in candidates if int(item["id"])==candidate_id),None)
        raw_date=str(groups.get("event_date") or (candidate or {}).get("suggested_date") or "")
        try:parsed=date.fromisoformat(raw_date)
        except ValueError:return await self._send_command(kwargs,"候选没有明确日期，请在命令末尾补充 YYYY-MM-DD。")
        saved=await self._store.confirm_date_candidate(candidate_id,uid,parsed.isoformat(),self._env.now().timestamp()) if self._store and self._env else 0
        return await self._send_command(kwargs,f"已确认日期 #{saved}：{parsed.isoformat()}。" if saved else "没有找到属于你的待确认项。")

    @Command(name="/mai_skills",pattern=r"^/mai_skills\b",description="主人或管理员查看麦麦技能成长")
    async def cmd_skills(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看完整技能记录。")
        skills=await self._store.list_skills(20) if self._store else []
        text="尚无技能实践记录。" if not skills else "麦麦的技能熟悉度\n"+"\n".join(
            f"{item['skill_name']}：{skill_stage(float(item['level']))}（{float(item['level']):.1f}/100，证据 {item['evidence_count']}）" for item in skills)
        return await self._send_command(kwargs,text)

    @Command(name="/mai_news",pattern=r"^/mai_news\b",description="主人或管理员查看近期新闻见闻")
    async def cmd_news(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看完整新闻见闻。")
        items=await self._store.recent_news_items(self._env.now().timestamp(),5) if self._store and self._env else []
        if not items:return await self._send_command(kwargs,"近期没有读取到新闻见闻，或新闻功能尚未启用。")
        text="麦麦近期读到的内容\n"+"\n".join(f"{item['title']}\n{item['summary'] or '只有标题，正文暂不可读'}" for item in items)
        return await self._send_command(kwargs,text)

    @Command(name="/mai_explore",pattern=r"^/mai_explore\b",description="主人或管理员查看主动搜索笔记")
    async def cmd_explore(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看完整探索笔记。")
        notes=await self._store.recent_exploration_notes(self._env.now().timestamp(),5) if self._store and self._env else []
        if not notes:return await self._send_command(kwargs,"近期没有主动搜索笔记，或搜索功能尚未启用。")
        text="麦麦近期探索笔记\n"+"\n".join(f"{item['topic']}\n{item['summary']}" for item in notes)
        return await self._send_command(kwargs,text)

    @Command(name="/mai_bookshelf",pattern=r"^/mai_bookshelf\b",description="查看当前关系有权访问的书柜")
    async def cmd_bookshelf(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._command_user(kwargs)
        if not user or not self._bookshelf:return await self._send_command(kwargs,"书柜尚未初始化。")
        rows=await self._bookshelf.list_for_user(user,20,is_admin=self._is_admin(uid))
        if not rows:return await self._send_command(kwargs,"当前关系可见的书柜还是空的。")
        lines=["麦麦书柜"]
        for item in rows:
            lines.append(f"{item['id']}｜{self._bookshelf.type_label(str(item.get('work_type') or item.get('doc_type')))}｜"
                         f"{item['title']}｜{item['privacy']}")
        return await self._send_command(kwargs,"\n".join(lines))

    @Command(name="/mai_read",pattern=r"^/mai_read\s+(?P<document_id>\S+)$",description="阅读有权限访问的书柜文本")
    async def cmd_read(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._command_user(kwargs)
        groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        document_id=str(groups.get("document_id") or "")
        item=await self._bookshelf.read_for_user(document_id,user,is_admin=self._is_admin(uid)) if self._bookshelf and user else {}
        if not item:return await self._send_command(kwargs,"没有找到该文本，或当前关系无权读取。")
        text=f"{item['title']}｜{item['privacy']}\n\n{str(item.get('content') or '')[:12000]}"
        return await self._send_command(kwargs,text)

    @Command(name="/mai_create_now",pattern=r"^/mai_create_now\b",description="管理员立即执行一次创作判断")
    async def cmd_create_now(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs) or not self._is_admin(uid):
            return await self._send_command(kwargs,"只有已配置的私聊管理员可以手动执行创作判断。")
        if not self._creation or not self._env or not self._store or not self._schedule:
            return await self._send_command(kwargs,"创作服务尚未初始化。")
        now=self._env.now(); result=await self._creation.tick(
            now,self._personality,await self._store.get_state(),await self._schedule.context(now),force=True,
        )
        return await self._send_command(kwargs,"创作结果："+json.dumps(result,ensure_ascii=False))

    @Command(name="/mai_relay",pattern=r"^/mai_relay\s+(?P<group_alias>\S+)\s+(?P<relay_content>.+)$",description="主人或管理员向白名单群发起转述")
    async def cmd_relay(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs) or not await self._is_owner_or_admin(uid):
            return await self._send_command(kwargs,"该命令只允许已配置的主人或管理员在私聊中使用。")
        groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        group_alias=str(groups.get("group_alias") or "").strip(); content=str(groups.get("relay_content") or "").strip()
        relation=""; parts=content.split(maxsplit=1)
        if parts and parts[0].startswith("@"):
            if len(parts)<2:return await self._send_command(kwargs,"@ 群友后还需要填写要转述的内容。")
            relation=parts[0][1:]; content=parts[1]
        result=await self._relay.trigger_explicit(group_alias,content,relation) if self._relay else {
            "success":False,"error":"社交转述服务尚未初始化。"
        }
        return await self._send_command(kwargs,str(result.get("message") if result.get("success") else result.get("error")))

    @Command(name="/mai_tokens",pattern=r"^/mai_tokens\b",description="管理员查看今日插件 Token 统计")
    async def cmd_tokens(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs) or not self._is_admin(uid):
            return await self._send_command(kwargs,"只有已配置的私聊管理员可以查看 Token 统计。")
        return await self._send_command(kwargs,await self._token_report())

    @Command(name="/mai_admin",pattern=r"^/mai_admin(?:\s+(?P<scope>overview|users|relations|dates|sources|bookshelf|tokens|proactive))?\s*$",description="管理员查看聚合管理摘要")
    async def cmd_admin(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs) or not self._is_admin(uid):
            return await self._send_command(kwargs,"只有已配置的私聊管理员可以查看管理摘要。")
        groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        scope=str(groups.get("scope") or "overview")
        text=await self._admin.format_text(scope,self._env.now()) if self._admin and self._env else "管理服务尚未初始化。"
        return await self._send_command(kwargs,text)

    @Command(name="/mai_config",pattern=r"^/mai_config\b",description="查看麦麦生活配置摘要")
    async def cmd_config(self,**kwargs:Any)->tuple[bool,str,int]:
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        text=(f"麦麦生活：{'开启' if self.config.plugin.enabled else '关闭'}\n配置用户：{len(self.config.users.profiles)}\n"
              f"消息收口：{'开启' if self.config.debounce.enabled else '关闭'}\n休息闸门：{'开启' if self.config.rest_gate.enabled else '关闭'}\n"
              f"撤回增强：{'开启' if self.config.recall.enabled else '关闭'}（本人摘要缓存 {'开' if self.config.recall.cache_summary_enabled else '关'}）\n"
              f"生活记忆：{'开启' if self.config.memory.enabled else '关闭'}\n"
              f"联网见闻：{'开启' if self.config.information.enabled else '关闭'}（新闻 {'开' if self.config.news.enabled else '关'} / 搜索 {'开' if self.config.search.enabled else '关'}）\n"
              f"社交转述：{'开启' if self.config.social.enabled else '关闭'}（白名单群 {len(self.config.social.groups)}）\n"
              f"书柜创作：{'开启' if self.config.creation.enabled else '关闭'}（明文确认 {'是' if self.config.creation.plaintext_storage_acknowledged else '否'}）\n"
              f"模型任务：{self.config.models.fast_task}/{self.config.models.reasoning_task}/{self.config.models.vision_task}")
        return await self._send_command(kwargs,text)

    @Command(name="/mai_help",pattern=r"^/mai_help\b",description="查看麦麦生活命令")
    async def cmd_help(self,**kwargs:Any)->tuple[bool,str,int]:
        if not await self._command_user(kwargs):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        return await self._send_command(kwargs,"/mai_status 状态\n/mai_schedule 日程\n/mai_relation 关系\n/mai_recalled 本人最近撤回摘要\n/mai_diary 私人日记\n/mai_dates 重要日期\n/mai_date_add YYYY-MM-DD 名称\n/mai_date_remove ID\n/mai_date_confirm ID [YYYY-MM-DD]\n/mai_skills 技能成长\n/mai_news 新闻见闻\n/mai_explore 搜索笔记\n/mai_bookshelf 可见书柜\n/mai_read 文本ID\n/mai_create_now 立即创作判断（管理员）\n/mai_relay 群别名 [@群友别名] 内容\n/mai_admin [范围] 管理摘要（管理员）\n/mai_tokens Token统计（管理员）\n/mai_config 配置\n/mai_regenerate_schedule 重生成日程\n/mai_rest_test 闸门诊断")

    @Command(name="/mai_regenerate_schedule",pattern=r"^/mai_regenerate_schedule\b",description="管理员重新生成今日日程")
    async def cmd_regenerate(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs) or not self._is_admin(uid):
            return await self._send_command(kwargs,"只有已配置的私聊管理员可以重新生成日程。")
        if not self._ready:return await self._send_command(kwargs,"服务尚未初始化。")
        assert self._env and self._schedule and self._memory and self._store
        now=self._env.now(); weather=await self._store.get_weather() or {"description":"天气未知"}
        memory_context=await self._memory.schedule_context(now)
        nodes=await self._schedule.ensure_day(now,self._personality,self._env.weather_text(weather),force=True,memory_context=memory_context)
        return await self._send_command(kwargs,f"已重新生成今日日程，共 {len(nodes)} 个节点。")

    @Command(name="/mai_rest_test",pattern=r"^/mai_rest_test\b",description="管理员查看休息闸门状态")
    async def cmd_rest_test(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._command_user(kwargs) or not self._is_admin(uid):
            return await self._send_command(kwargs,"只有已配置的私聊管理员可以查看休息闸门诊断。")
        if not self._ready:return await self._send_command(kwargs,"服务尚未初始化。")
        assert self._env and self._schedule and self._store
        now=self._env.now(); context=await self._schedule.context(now); runtime=await self._store.get_sleep_runtime()
        text=(f"当前时间：{now.strftime('%H:%M')}\n日程类型：{(context.get('current') or {}).get('kind','无')}\n"
              f"睡眠阶段：{runtime.get('phase')}\n醒来缓冲至：{datetime.fromtimestamp(float(runtime.get('awake_grace_until',0)),tz=now.tzinfo).isoformat() if runtime.get('awake_grace_until') else '无'}")
        return await self._send_command(kwargs,text)

    async def _status_report(self)->str:
        if not self._ready:return "麦麦生活尚未初始化。"
        assert self._store and self._env and self._schedule and self._llm and self._debouncer and self._information and self._creation
        state=await self._store.get_state(); weather=await self._store.get_weather() or {"description":"天气未知"}
        context=await self._schedule.context(self._env.now())
        tasks=",".join(sorted(self._llm.available_tasks)) or "未知"
        diaries=await self._store.list_diaries(1); skills=await self._store.list_skills(100); info=await self._information.status(self._env.now())
        observations=await self._store.recent_group_observations(self._env.now().timestamp(),100)
        creation=await self._creation.status(self._env.now())
        return (f"麦麦生活 v1.6.0\n精力：{state.get('energy',0):.0f}/100  饥饿：{state.get('hunger',0):.0f}/100\n"
                f"心情：{state.get('mood_valence',0):.2f}  睡眠：{state.get('sleep_phase')}\n"
                f"场景：{state.get('current_activity')}\n日程：{(context.get('current') or {}).get('summary','无')}\n"
                f"天气：{self._env.weather_text(weather)}\n消息收口：{'开启' if self.config.debounce.enabled else '关闭'}（活跃 {self._debouncer.active_bursts}）\n"
                f"撤回增强：{'开启' if self.config.recall.enabled else '关闭'}，本人摘要缓存 {'开启' if self.config.recall.cache_summary_enabled else '关闭'}\n"
                f"视觉摘要：{'可用' if self._llm.task_available('vision_summary') else '降级'}\n可用模型任务：{tasks}\n"
                f"生活记忆：日记 {len(diaries)}（最近一篇），技能 {len(skills)} 项\n"
                f"联网见闻：{'开启' if info['enabled'] else '关闭'}，来源 {info['sources']}，新闻 {info['recent_news']}，探索 {info['recent_explorations']}\n"
                f"社交转述：{'开启' if self.config.social.enabled else '关闭'}，短期群摘要 {len(observations)}，"
                f"群缓冲 {self._group_observer.active_groups if self._group_observer else 0}\n"
                f"书柜创作：{'开启' if creation['enabled'] else '关闭'}，今日归档 {creation['archived_today']}，"
                f"待处理灵感 {creation['pending_inspirations']}，明文确认 {'是' if creation['plaintext_acknowledged'] else '否'}\n"
                f"模型健康：{self._llm.health_error or '正常'}\n后台任务：{len(self._tasks)}")

    async def _schedule_report(self)->str:
        if not self._ready:return "麦麦生活尚未初始化。"
        assert self._store and self._env and self._schedule
        now=self._env.now(); nodes=await self._store.get_framework(now.date().isoformat()); context=await self._schedule.context(now)
        lines=[f"今日生活框架（{now.date().isoformat()}）"]
        for node in nodes:lines.append(f"{hhmm(node['start_minute'])}-{hhmm(node['end_minute'])} {node['summary']} @ {node['location']}")
        scene=context.get("scene") or {}; lines.append(f"\n当前细化场景：{scene.get('scene') or '尚未细化'}")
        return "\n".join(lines)

    async def _token_report(self)->str:
        if not self._store or not self._env:return "Token 统计尚未初始化。"
        now=self._env.now(); start=now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp(); rows=await self._store.usage_summary(start)
        if not rows:return "今日尚无 Mai_life 模型调用记录。"
        lines=["Mai_life 今日 Token 统计"]
        for row in rows:
            lines.append(f"{row['source']}/{row['task_name']}：{row['calls']} 次，{int(row['total_tokens'] or 0)} Token，成功 {int(row['successes'] or 0)} 次")
        return "\n".join(lines)

    @API(name="get_life_state",description="获取麦麦全局生活状态",version="1",public=True)
    async def api_get_state(self,**kwargs:Any)->dict[str,Any]:return await self._store.get_state() if self._store else {}

    @API(name="get_current_scene",description="获取麦麦当前细化场景",version="1",public=True)
    async def api_get_scene(self,**kwargs:Any)->dict[str,Any]:
        if not self._ready:return {}
        assert self._schedule and self._env
        return await self._schedule.context(self._env.now())

    @API(name="get_schedule",description="获取麦麦今日日程",version="1",public=True)
    async def api_get_schedule(self,day:str="",**kwargs:Any)->list[dict[str,Any]]:
        if not self._ready:return []
        assert self._store and self._env
        return await self._store.get_framework(day or self._env.now().date().isoformat())

    @API(name="get_user_relationship",description="获取指定用户关系状态",version="1",public=True)
    async def api_get_relationship(self,user_id:str="",**kwargs:Any)->dict[str,Any]:
        if not self._store:return {}
        user=await self._store.get_user(str(user_id));
        if not user:return {}
        # 公共 API 只提供关系契约，不暴露 stream、活跃时间和免打扰时段。
        return {"user_id":str(user["user_id"]),"enabled":bool(user.get("enabled")),
                "display_name":str(user.get("display_name") or ""),"role":str(user.get("role") or "friend"),
                "temperature":float(user.get("temperature") or 0),
                "stage":relationship_stage(float(user.get("temperature") or 0)),
                "proactive_enabled":bool(user.get("proactive_enabled")),
                "daily_proactive_max":int(user.get("daily_proactive_max") or 0)}

    @API(name="get_environment_snapshot",description="获取当前时间、历法和媒介环境快照",version="1",public=True)
    async def api_get_environment(self,platform:str="qq",adapter:str="unknown",chat_type:str="private",media:list[str]|None=None,**kwargs:Any)->dict[str,Any]:
        clean_media=[str(item)[:40] for item in media if str(item).strip()] if isinstance(media,list) else ["text"]
        return self._env.snapshot(platform=platform,adapter=adapter,chat_type=chat_type,media=clean_media or ["text"]) if self._env else {}

    @API(name="create_proactive_opportunity",description="创建外部主动分享契机",version="1",public=True)
    async def api_create_opportunity(self,topic:str="",motive:str="",weight:float=0.5,expires_minutes:int=120,**kwargs:Any)->dict[str,Any]:
        clean_topic=" ".join(str(topic or "").replace("\x00","").split())[:160]
        clean_motive=" ".join(str(motive or "").replace("\x00","").split())[:240]
        if not self._ready or not clean_topic:return {"success":False,"error":"not_ready_or_empty_topic"}
        assert self._store and self._env and self._schedule
        now=self._env.now(); current=(await self._schedule.context(now)).get("current")
        if not current:return {"success":False,"error":"no_current_framework"}
        try:clean_weight=max(0.0,min(1.0,float(weight)))
        except (TypeError,ValueError):clean_weight=0.5
        try:ttl=max(1,min(1440,int(expires_minutes)))
        except (TypeError,ValueError):ttl=120
        oid=hashlib.sha1(f"api:{now.timestamp()}:{clean_topic}".encode()).hexdigest()[:20]
        await self._store.add_opportunity({"id":oid,"framework_id":current["id"],"topic":clean_topic,
            "motive":clean_motive or "外部生活事件值得分享","weight":clean_weight,"privacy":"external",
            "expires_at":now.timestamp()+ttl*60})
        return {"success":True,"opportunity_id":oid}

    @API(name="admin_snapshot",description="获取 Mai_life 私有管理摘要",version="1",public=False)
    async def api_admin_snapshot(self,scope:str="overview",limit:int=20,**kwargs:Any)->dict[str,Any]:
        """保留给 Host 鉴权桥或插件自身；不作为其他插件可调用的公共 API。"""
        del kwargs
        if not self._admin or not self._env:return {"success":False,"error":"not_ready"}
        return {"success":True,"data":await self._admin.snapshot(scope,self._env.now(),limit)}

    @HomeCard(
        name="mai_life_management",title="麦麦生活管理",
        description="配置生活、社交、联网与书柜模块；敏感明细请使用管理员命令。",
        content=[
            {"type":"key_value","entries":{"版本":"1.6.0","管理命令":"/mai_admin","私密 API":"不公开"}},
            {"type":"list","items":["用户角色与主动额度","日期候选与关系词条","来源、书柜与 Token 聚合"]},
        ],
        link_url="/plugin-config?plugin=maibot-community.mai-life",link_label="打开麦麦生活配置",
        icon="heart-pulse",width="medium",order=420,
    )
    async def home_card_management(self,**kwargs:Any)->dict[str,Any]:
        del kwargs
        return {"success":True}


def create_plugin()->MaiBotPlugin:return MaiLifePlugin()
