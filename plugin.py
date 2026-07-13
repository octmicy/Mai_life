"""Mai_life v1.5.0 插件入口。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import date,datetime,timedelta
from typing import Any,ClassVar,Iterable,Optional

from maibot_sdk import API,Command,HookHandler,MaiBotPlugin
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
from .messaging.adapter_compat import adapter_name
from .messaging.message_pipeline import MessageDebouncer,classify_intent,is_command,media_types,message_identity,plain_text
from .messaging.prompt_builder import PromptBuilder,relationship_stage
from .messaging.vision_service import VisionService
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
        self._vision:Optional[VisionService]=None; self._continuity:Optional[ContinuityService]=None
        self._memory:Optional[MemoryService]=None
        self._information:Optional[InformationService]=None
        self._group_observer:Optional[GroupObserver]=None; self._relay:Optional[RelayService]=None
        self._bookshelf:Optional[BookshelfService]=None; self._creation:Optional[CreationService]=None
        self._prompts=PromptBuilder(); self._tasks:list[asyncio.Task[Any]]=[]; self._transient:set[asyncio.Task[Any]]=set()
        self._personality=""; self._maintenance_lock=asyncio.Lock()
        self._session_runtime:dict[str,dict[str,Any]]={}; self._reply_confirmations:dict[str,dict[str,Any]]={}

    @property
    def _ready(self)->bool:
        return all((self._store,self._env,self._llm,self._state,self._schedule,self._rest,self._proactive,
                    self._debouncer,self._vision,self._continuity,self._memory,self._information,
                    self._group_observer,self._relay,self._bookshelf,self._creation))

    async def on_load(self)->None:
        root=os.path.dirname(os.path.abspath(__file__)); self._store=LifeStore(os.path.join(root,"data"))
        await self._store.initialize()
        self._llm=LLMService(self.ctx,self.config,self._store)
        self._env=EnvironmentService(self._store,self.config,self.ctx.logger)
        self._state=LifeStateEngine(self._store,self.config,self._llm,self.ctx.logger)
        self._schedule=ScheduleService(self._store,self.config,self._llm,root,self.ctx.logger)
        self._rest=RestGate(self._store,self.config,self._llm,self._state,self.ctx.logger)
        self._proactive=ProactiveEngine(self.ctx,self._store,self.config,self._env,self.ctx.logger)
        self._debouncer=MessageDebouncer(self.config,self.ctx.logger)
        self._vision=VisionService(self.ctx,self._store,self.config,self._llm,self.ctx.logger)
        self._continuity=ContinuityService(self._store,self.config,self._llm,self.ctx.logger)
        self._memory=MemoryService(self._store,self.config,self._llm,self.ctx.logger)
        self._information=InformationService(self.ctx,self._store,self.config,self._llm,self.ctx.logger)
        self._group_observer=GroupObserver(self._store,self.config,self._llm,self.ctx.logger)
        self._relay=RelayService(self.ctx,self._store,self.config,self.ctx.logger)
        self._bookshelf=BookshelfService(self._store,self.config)
        self._creation=CreationService(self.ctx,self._store,self.config,self._llm,self.ctx.logger)
        await self._store.sync_users(self.config.users.profiles)
        await self._store.sync_relationship_entries(self.config.social.relations)
        await self._refresh_personality(); await self._resolve_all_streams(); await self._llm.refresh_health()
        if self.config.plugin.enabled:
            await self._maintenance_tick(force_weather=True); self._start_tasks()
        self.ctx.logger.info("[MaiLife] 麦麦生活 v1.5.0 加载完成")

    async def on_unload(self)->None:
        if self._debouncer:await self._debouncer.close()
        if self._group_observer:await self._group_observer.close()
        await self._stop_tasks()
        if self._store:await self._store.close()
        self.ctx.logger.info("[MaiLife] 麦麦生活已卸载")

    async def on_config_update(self,scope:str,config_data:dict[str,Any],version:str)->None:
        del config_data,version
        await self._stop_tasks()
        if self._group_observer:await self._group_observer.reset()
        for service in (self._llm,self._env,self._state,self._schedule,self._rest,self._proactive,
                        self._debouncer,self._vision,self._continuity,self._memory,self._information,
                        self._group_observer,self._relay,self._bookshelf,self._creation):
            if service:service.update_config(self.config)
        if self._store:await self._store.sync_users(self.config.users.profiles)
        if self._store:await self._store.sync_relationship_entries(self.config.social.relations)
        if scope=="bot":await self._refresh_personality()
        if self._llm:await self._llm.refresh_health()
        await self._resolve_all_streams()
        if self.config.plugin.enabled:
            await self._maintenance_tick(force_weather=True); self._start_tasks()
        self.ctx.logger.info(f"[MaiLife] 配置热更新完成 scope={scope}")

    def _spawn_transient(self,coro:Any,name:str)->None:
        task=asyncio.create_task(coro,name=name); self._transient.add(task)
        def finished(done:asyncio.Task[Any])->None:
            self._transient.discard(done)
            if not done.cancelled() and (error:=done.exception()) is not None:
                self._get_logger().warning(f"[MaiLife] 临时任务异常 name={done.get_name()}: {error}")
        task.add_done_callback(finished)

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

    async def _maintenance_tick(self,force_weather:bool=False)->None:
        if not self._ready:return
        async with self._maintenance_lock:
            assert self._env and self._schedule and self._store and self._state and self._memory
            now=self._env.now(); weather=await self._env.refresh_weather(force=force_weather)
            await self._memory.ensure_daily(now); memory_context=await self._memory.schedule_context(now)
            nodes=await self._schedule.ensure_day(now,self._personality,self._env.weather_text(weather),memory_context=memory_context)
            context=await self._schedule.context(now); state=await self._store.get_state()
            await self._schedule.expand_due(now,nodes,state,self._env.weather_text(weather)); context=await self._schedule.context(now)
            await self._state.advance(now,context.get("current"),context.get("scene")); await self._schedule.apply_completed(now,self._state)
            relation_day=now.date()-timedelta(days=1)
            start=now.replace(year=relation_day.year,month=relation_day.month,day=relation_day.day,hour=0,minute=0,second=0,microsecond=0)
            await self._store.update_relationships(relation_day.isoformat(),start.timestamp(),(start+timedelta(days=1)).timestamp(),now.timestamp())
            await self._store.cleanup_runtime_records(now.timestamp(),now.timestamp()-int(self.config.usage.retention_days)*86400)

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
        if uid:return await self._store.get_user(uid)
        for user in await self._store.list_users():
            if str(user.get("stream_id") or "")==session_id:return user
        return {}

    @HookHandler("chat.receive.before_process",mode=HookMode.BLOCKING,order=HookOrder.EARLY,timeout_ms=30000)
    async def on_receive(self,**kwargs:Any)->dict[str,Any]:
        if not self.config.plugin.enabled or not self._ready:return {"action":"continue"}
        message=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        if not message or message.get("is_notify"):return {"action":"continue"}
        uid,session,mid,private=message_identity(message)
        if not private:
            if uid and not is_command(message) and self._group_observer and self.config.social.enabled:
                # 群聊观察使用独立后台缓冲；无论整理成功与否都不阻塞 Host 原有群聊链。
                self._spawn_transient(self._group_observer.observe(message,self._env.now()),f"mai-life-group-{mid}")
            return {"action":"continue"}
        if not uid or is_command(message):return {"action":"continue"}
        assert self._store and self._env and self._schedule and self._rest and self._debouncer and self._vision and self._continuity and self._memory
        user=await self._store.get_user(uid)
        if not user or not user.get("enabled"):return {"action":"continue"}
        if session and session!=user.get("stream_id"):await self._store.set_user_stream(uid,session)

        # 视觉任务与收口等待并行；旧一代消息被合并后会取消自己的无效视觉任务。
        vision_task=asyncio.create_task(self._vision.summarize_if_needed(message),name=f"mai-life-vision-{mid}")
        started=time.monotonic()
        try:allowed,merged,reason=await self._debouncer.collect(message)
        except Exception as exc:
            vision_task.cancel(); self.ctx.logger.warning(f"[MaiLife] 消息收口失败，失败开放: {exc}")
            return {"action":"continue"}
        if not allowed:
            vision_task.cancel(); await asyncio.gather(vision_task,return_exceptions=True)
            return {"action":"abort"}
        # 视觉摘要是内部背景，互动/意图/休息判断只能使用用户原始文本。
        user_text=plain_text(merged); user_media=media_types(merged)
        remaining=max(0.01,float(self.config.vision.timeout_seconds)-(time.monotonic()-started)); summary=""
        try:summary=await asyncio.wait_for(vision_task,timeout=remaining)
        except (asyncio.TimeoutError,asyncio.CancelledError):vision_task.cancel()
        except Exception as exc:self.ctx.logger.debug(f"[MaiLife] 视觉摘要降级: {exc}")
        if summary:merged=self._vision.inject_summary(merged,summary)
        uid,session,mid,_=message_identity(merged); text=user_text; media=user_media
        intent=classify_intent(text,media)
        self._session_runtime[session]={"user_id":uid,"message_id":mid,"intent":intent,"media":media,
                                        "platform":str(merged.get("platform") or "qq"),"adapter":adapter_name(merged),
                                        "chat_type":"private","updated_at":time.time()}
        await self._store.record_interaction(uid,text or f"发送了{','.join(media) or '一条消息'}",self._env.now().timestamp(),self._env.now().hour)
        self._spawn_transient(self._continuity.refresh(uid,intent),f"mai-life-continuity-{uid}")
        self._spawn_transient(self._memory.observe_message(uid,text,self._env.now()),f"mai-life-date-{uid}")
        context=await self._schedule.context(self._env.now())
        gate_allowed,gate_reason=await self._rest.decide(uid,text,self._env.now(),context.get("current"),session_id=session,message_id=mid)
        if not gate_allowed:
            await self._store.add_rest_backlog(uid,text or f"发送了{','.join(media) or '一条消息'}",self._env.now().timestamp())
            self.ctx.logger.info(f"[MaiLife] 休息闸门阻断 user={uid} reason={gate_reason}")
            return {"action":"abort"}
        kwargs["message"]=merged
        self.ctx.logger.debug(f"[MaiLife] 消息收口完成 session={session} {reason} media={media}")
        return {"action":"continue","modified_kwargs":kwargs}

    async def _prompt_payload(self,session_id:str,consume_backlog:bool=False)->dict[str,Any]|None:
        if not self._ready:return None
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

    @HookHandler("maisaka.planner.before_request",mode=HookMode.BLOCKING)
    async def on_planner(self,**kwargs:Any)->dict[str,Any]:
        session=str(kwargs.get("session_id") or ""); suffix=""
        if self.config.context.enabled:
            payload=await self._prompt_payload(session)
            if payload:
                suffix=self._prompts.planner(payload["state"],payload["weather"],payload["context"],payload["user"],payload["dream"],
                                             payload["backlogs"],payload["environment"],payload["continuity"],payload["intent"],
                                             int(self.config.context.prompt_max_chars),memory=payload["memory"],information=payload["information"],
                                             bookshelf=payload["bookshelf"])
        if self._relay and self.config.social.enabled:suffix+=await self._relay.prompt_context(session)
        if not suffix:return {"action":"continue"}
        kwargs["extra_prompt"]=(kwargs.get("extra_prompt") or "")+suffix
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("maisaka.planner.after_response",mode=HookMode.OBSERVE)
    async def on_planner_after(self,**kwargs:Any)->None:
        if self._llm:
            await self._llm.record_observed(source="host_planner",task_name="planner",request_type="planner",
                model_name="",prompt_tokens=int(kwargs.get("prompt_tokens") or 0),completion_tokens=int(kwargs.get("completion_tokens") or 0),
                total_tokens=int(kwargs.get("total_tokens") or 0))

    @HookHandler("maisaka.replyer.before_request",mode=HookMode.BLOCKING)
    async def on_replyer(self,**kwargs:Any)->dict[str,Any]:
        session=str(kwargs.get("session_id") or ""); suffix=""
        if self.config.context.enabled:
            payload=await self._prompt_payload(session,consume_backlog=True)
            if payload:
                suffix=self._prompts.replyer(payload["state"],payload["weather"],payload["context"],payload["user"],payload["backlogs"],
                                             payload["environment"],payload["continuity"],payload["intent"],payload["images"],
                                             int(self.config.context.prompt_max_chars),memory=payload["memory"],information=payload["information"],
                                             bookshelf=payload["bookshelf"])
        if self._relay and self.config.social.enabled:suffix+=await self._relay.prompt_context(session)
        if not suffix:return {"action":"continue"}
        kwargs["extra_prompt"]=(kwargs.get("extra_prompt") or "")+suffix
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("maisaka.replyer.after_response",mode=HookMode.BLOCKING)
    async def on_replyer_after(self,**kwargs:Any)->dict[str,Any]:
        session=str(kwargs.get("session_id") or ""); response=str(kwargs.get("response") or "").strip()
        if self._llm:
            await self._llm.record_observed(source="host_replyer",task_name=str(kwargs.get("task_name") or "replyer"),
                request_type=str(kwargs.get("request_type") or "replyer"),model_name=str(kwargs.get("requested_model_name") or ""),
                prompt_tokens=int(kwargs.get("prompt_tokens") or 0),completion_tokens=int(kwargs.get("completion_tokens") or 0),
                total_tokens=int(kwargs.get("total_tokens") or 0),success=bool(response))
        if not response:return {"action":"continue"}
        user=await self._user_by_session(session)
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
        anchor=str(kwargs.get("reply_message_id") or "")
        if self.config.debounce.outbound_turn_guard and anchor and self._store:
            reserved=await self._store.reserve_reply_turn(session,anchor,time.time(),time.time()+int(self.config.debounce.turn_expire_seconds))
            if not reserved:
                kwargs["response"]=""
                self._get_logger().info(f"[MaiLife] 同轮重复 Replyer 已收口 session={session} anchor={anchor}")
                return {"action":"continue","modified_kwargs":kwargs}
        self._reply_confirmations[session]={"anchor":anchor,"created_at":time.time()}
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("send_service.before_send",mode=HookMode.BLOCKING)
    async def on_send_before(self,**kwargs:Any)->dict[str,Any]:
        if not self._relay or not self.config.plugin.enabled or not self.config.social.enabled:return {"action":"continue"}
        message=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        if not message:return {"action":"continue"}
        mutated,reserved=await self._relay.mutate_before_send(message)
        if not reserved:return {"action":"continue"}
        kwargs["message"]=mutated
        return {"action":"continue","modified_kwargs":kwargs}

    @HookHandler("send_service.after_send",mode=HookMode.OBSERVE)
    async def on_send_after(self,**kwargs:Any)->None:
        message=kwargs.get("message") if isinstance(kwargs.get("message"),dict) else {}
        sent=bool(kwargs.get("sent"))
        if self._relay and self.config.social.enabled:await self._relay.confirm_after_send(message,sent)
        session=str(message.get("session_id") or ""); confirmation=self._reply_confirmations.pop(session,None)
        if not confirmation:return
        anchor=str(confirmation.get("anchor") or "")
        if not sent:
            if self._store:await self._store.release_reply_turn(session,anchor)
            if self._store:await self._store.clear_wake_candidate(session)
            return
        if self._env and self._rest:await self._rest.commit_for_send(session,self._env.now())
        if self._proactive and self._store and self._env:
            now=self._env.now(); await self._store.mark_pending_sent(session,now.timestamp(),now.strftime("%Y-%m-%d"))

    def _is_admin(self,user_id:str)->bool:
        admins=[str(item) for item in self.config.plugin.admin_user_ids if str(item)]
        if not admins:admins=[str(profile.user_id) for profile in self.config.users.profiles if str(profile.user_id).strip()][:1]
        return user_id in admins

    async def _is_owner_or_admin(self,user_id:str)->bool:
        if self._is_admin(user_id):return True
        user=await self._store.get_user(user_id) if self._store and user_id else {}
        return bool(user and str(user.get("role") or "friend")=="owner")

    async def _configured_command_user(self,user_id:str)->dict[str,Any]:
        user=await self._store.get_user(user_id) if self._store and user_id else {}
        return user if user and user.get("enabled") else {}

    async def _send_command(self,kwargs:dict[str,Any],text:str)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._store.get_user(uid) if self._store and uid else {}
        if kwargs.get("group_id") or not user or not user.get("enabled"):text="该命令仅对已配置的私聊用户开放。"
        try:
            ok=await self.ctx.send.text(text=text,stream_id=str(kwargs.get("stream_id") or ""))
            return bool(ok),"命令结果已发送" if ok else "发送返回 False",2
        except Exception as exc:return False,f"发送失败: {exc}",2

    @Command(name="/mai_status",pattern=r"^/mai_status\b",description="查看麦麦生活与消息管线状态")
    async def cmd_status(self,**kwargs:Any)->tuple[bool,str,int]:return await self._send_command(kwargs,await self._status_report())

    @Command(name="/mai_schedule",pattern=r"^/mai_schedule\b",description="查看今日框架与当前场景")
    async def cmd_schedule(self,**kwargs:Any)->tuple[bool,str,int]:return await self._send_command(kwargs,await self._schedule_report())

    @Command(name="/mai_relation",pattern=r"^/mai_relation\b",description="查看当前用户关系")
    async def cmd_relation(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._store.get_user(uid) if self._store else {}
        text="尚未配置该用户。" if not user else (f"关系角色：{user.get('role','friend')}\n关系温度：{float(user['temperature']):.1f}/100\n"
            f"关系阶段：{relationship_stage(float(user['temperature']))}\n每日主动上限：{user.get('daily_proactive_max',1)}")
        return await self._send_command(kwargs,text)

    @Command(name="/mai_diary",pattern=r"^/mai_diary\b",description="主人或管理员查看最近生活日记")
    async def cmd_diary(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看私人日记。")
        entries=await self._store.list_diaries(3) if self._store else []
        if not entries:return await self._send_command(kwargs,"还没有生成生活日记。")
        lines=["麦麦最近的生活日记"]
        for item in entries:lines.append(f"\n{item['day']}｜{item['title']}\n{item['content']}\n心情：{item['mood_summary']}")
        return await self._send_command(kwargs,"\n".join(lines))

    @Command(name="/mai_dates",pattern=r"^/mai_dates\b",description="查看当前用户的重要日期和待确认项")
    async def cmd_dates(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); dates=await self._store.list_important_dates(uid) if self._store else []
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
        if not await self._configured_command_user(uid):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        raw_date=str(groups.get("event_date") or ""); name=str(groups.get("event_name") or "").strip()[:120]
        try:parsed=date.fromisoformat(raw_date)
        except ValueError:return await self._send_command(kwargs,"日期格式无效，请使用 YYYY-MM-DD。")
        recurrence="annual" if any(word in name for word in ("生日","纪念日")) else "none"
        saved=await self._store.add_important_date(uid,name,parsed.isoformat(),recurrence,"manual",self._env.now().timestamp()) if self._store and self._env and name else 0
        return await self._send_command(kwargs,f"已记录：{parsed.isoformat()} {name}" if saved else "未能添加日期，请检查输入。")

    @Command(name="/mai_date_remove",pattern=r"^/mai_date_remove\s+(?P<date_id>\d+)$",description="删除当前用户的重要日期")
    async def cmd_date_remove(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        if not await self._configured_command_user(uid):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
        removed=await self._store.remove_important_date(int(groups.get("date_id") or 0),uid) if self._store else False
        return await self._send_command(kwargs,"已删除该日期。" if removed else "没有找到属于你的该日期。")

    @Command(name="/mai_date_confirm",pattern=r"^/mai_date_confirm\s+(?P<candidate_id>\d+)(?:\s+(?P<event_date>\d{4}-\d{2}-\d{2}))?$",description="确认当前用户的日期候选")
    async def cmd_date_confirm(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        if not await self._configured_command_user(uid):return await self._send_command(kwargs,"该命令仅对已配置的私聊用户开放。")
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
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看完整技能记录。")
        skills=await self._store.list_skills(20) if self._store else []
        text="尚无技能实践记录。" if not skills else "麦麦的技能熟悉度\n"+"\n".join(
            f"{item['skill_name']}：{skill_stage(float(item['level']))}（{float(item['level']):.1f}/100，证据 {item['evidence_count']}）" for item in skills)
        return await self._send_command(kwargs,text)

    @Command(name="/mai_news",pattern=r"^/mai_news\b",description="主人或管理员查看近期新闻见闻")
    async def cmd_news(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看完整新闻见闻。")
        items=await self._store.recent_news_items(self._env.now().timestamp(),5) if self._store and self._env else []
        if not items:return await self._send_command(kwargs,"近期没有读取到新闻见闻，或新闻功能尚未启用。")
        text="麦麦近期读到的内容\n"+"\n".join(f"{item['title']}\n{item['summary'] or '只有标题，正文暂不可读'}" for item in items)
        return await self._send_command(kwargs,text)

    @Command(name="/mai_explore",pattern=r"^/mai_explore\b",description="主人或管理员查看主动搜索笔记")
    async def cmd_explore(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._is_owner_or_admin(uid):return await self._send_command(kwargs,"只有主人或管理员可以查看完整探索笔记。")
        notes=await self._store.recent_exploration_notes(self._env.now().timestamp(),5) if self._store and self._env else []
        if not notes:return await self._send_command(kwargs,"近期没有主动搜索笔记，或搜索功能尚未启用。")
        text="麦麦近期探索笔记\n"+"\n".join(f"{item['topic']}\n{item['summary']}" for item in notes)
        return await self._send_command(kwargs,text)

    @Command(name="/mai_bookshelf",pattern=r"^/mai_bookshelf\b",description="查看当前关系有权访问的书柜")
    async def cmd_bookshelf(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or ""); user=await self._configured_command_user(uid)
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
        uid=str(kwargs.get("user_id") or ""); user=await self._configured_command_user(uid)
        groups=kwargs.get("matched_groups") if isinstance(kwargs.get("matched_groups"),dict) else {}
        document_id=str(groups.get("document_id") or "")
        item=await self._bookshelf.read_for_user(document_id,user,is_admin=self._is_admin(uid)) if self._bookshelf and user else {}
        if not item:return await self._send_command(kwargs,"没有找到该文本，或当前关系无权读取。")
        text=f"{item['title']}｜{item['privacy']}\n\n{str(item.get('content') or '')[:12000]}"
        return await self._send_command(kwargs,text)

    @Command(name="/mai_create_now",pattern=r"^/mai_create_now\b",description="管理员立即执行一次创作判断")
    async def cmd_create_now(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not await self._configured_command_user(uid) or not self._is_admin(uid):
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
        if kwargs.get("group_id") or not await self._configured_command_user(uid) or not await self._is_owner_or_admin(uid):
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
        if not self._is_admin(uid):return await self._send_command(kwargs,"只有管理员可以查看 Token 统计。")
        return await self._send_command(kwargs,await self._token_report())

    @Command(name="/mai_config",pattern=r"^/mai_config\b",description="查看麦麦生活配置摘要")
    async def cmd_config(self,**kwargs:Any)->tuple[bool,str,int]:
        text=(f"麦麦生活：{'开启' if self.config.plugin.enabled else '关闭'}\n配置用户：{len(self.config.users.profiles)}\n"
              f"消息收口：{'开启' if self.config.debounce.enabled else '关闭'}\n休息闸门：{'开启' if self.config.rest_gate.enabled else '关闭'}\n"
              f"生活记忆：{'开启' if self.config.memory.enabled else '关闭'}\n"
              f"联网见闻：{'开启' if self.config.information.enabled else '关闭'}（新闻 {'开' if self.config.news.enabled else '关'} / 搜索 {'开' if self.config.search.enabled else '关'}）\n"
              f"社交转述：{'开启' if self.config.social.enabled else '关闭'}（白名单群 {len(self.config.social.groups)}）\n"
              f"书柜创作：{'开启' if self.config.creation.enabled else '关闭'}（明文确认 {'是' if self.config.creation.plaintext_storage_acknowledged else '否'}）\n"
              f"模型任务：{self.config.models.fast_task}/{self.config.models.reasoning_task}/{self.config.models.vision_task}")
        return await self._send_command(kwargs,text)

    @Command(name="/mai_help",pattern=r"^/mai_help\b",description="查看麦麦生活命令")
    async def cmd_help(self,**kwargs:Any)->tuple[bool,str,int]:
        return await self._send_command(kwargs,"/mai_status 状态\n/mai_schedule 日程\n/mai_relation 关系\n/mai_diary 私人日记\n/mai_dates 重要日期\n/mai_date_add YYYY-MM-DD 名称\n/mai_date_remove ID\n/mai_date_confirm ID [YYYY-MM-DD]\n/mai_skills 技能成长\n/mai_news 新闻见闻\n/mai_explore 搜索笔记\n/mai_bookshelf 可见书柜\n/mai_read 文本ID\n/mai_create_now 立即创作判断（管理员）\n/mai_relay 群别名 [@群友别名] 内容\n/mai_tokens Token统计（管理员）\n/mai_config 配置\n/mai_regenerate_schedule 重生成日程\n/mai_rest_test 闸门诊断")

    @Command(name="/mai_regenerate_schedule",pattern=r"^/mai_regenerate_schedule\b",description="管理员重新生成今日日程")
    async def cmd_regenerate(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not self._is_admin(uid):return await self._send_command(kwargs,"只有管理员可以重新生成日程。")
        if not self._ready:return await self._send_command(kwargs,"服务尚未初始化。")
        assert self._env and self._schedule and self._memory
        now=self._env.now(); weather=await self._env.refresh_weather(); memory_context=await self._memory.schedule_context(now)
        nodes=await self._schedule.ensure_day(now,self._personality,self._env.weather_text(weather),force=True,memory_context=memory_context)
        return await self._send_command(kwargs,f"已重新生成今日日程，共 {len(nodes)} 个节点。")

    @Command(name="/mai_rest_test",pattern=r"^/mai_rest_test\b",description="管理员查看休息闸门状态")
    async def cmd_rest_test(self,**kwargs:Any)->tuple[bool,str,int]:
        uid=str(kwargs.get("user_id") or "")
        if not self._is_admin(uid):return await self._send_command(kwargs,"只有管理员可以查看休息闸门诊断。")
        if not self._ready:return await self._send_command(kwargs,"服务尚未初始化。")
        assert self._env and self._schedule and self._store
        now=self._env.now(); context=await self._schedule.context(now); runtime=await self._store.get_sleep_runtime()
        text=(f"当前时间：{now.strftime('%H:%M')}\n日程类型：{(context.get('current') or {}).get('kind','无')}\n"
              f"睡眠阶段：{runtime.get('phase')}\n醒来缓冲至：{datetime.fromtimestamp(float(runtime.get('awake_grace_until',0))).isoformat() if runtime.get('awake_grace_until') else '无'}")
        return await self._send_command(kwargs,text)

    async def _status_report(self)->str:
        if not self._ready:return "麦麦生活尚未初始化。"
        assert self._store and self._env and self._schedule and self._llm and self._debouncer and self._information and self._creation
        state=await self._store.get_state(); weather=await self._env.refresh_weather(); context=await self._schedule.context(self._env.now())
        tasks=",".join(sorted(self._llm.available_tasks)) or "未知"
        diaries=await self._store.list_diaries(1); skills=await self._store.list_skills(100); info=await self._information.status(self._env.now())
        observations=await self._store.recent_group_observations(self._env.now().timestamp(),100)
        creation=await self._creation.status(self._env.now())
        return (f"麦麦生活 v1.5.0\n精力：{state.get('energy',0):.0f}/100  饥饿：{state.get('hunger',0):.0f}/100\n"
                f"心情：{state.get('mood_valence',0):.2f}  睡眠：{state.get('sleep_phase')}\n"
                f"场景：{state.get('current_activity')}\n日程：{(context.get('current') or {}).get('summary','无')}\n"
                f"天气：{self._env.weather_text(weather)}\n消息收口：{'开启' if self.config.debounce.enabled else '关闭'}（活跃 {self._debouncer.active_bursts}）\n"
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
        if user:user["stage"]=relationship_stage(float(user["temperature"]))
        return user

    @API(name="get_environment_snapshot",description="获取当前时间、历法和媒介环境快照",version="1",public=True)
    async def api_get_environment(self,platform:str="qq",adapter:str="unknown",chat_type:str="private",media:list[str]|None=None,**kwargs:Any)->dict[str,Any]:
        return self._env.snapshot(platform=platform,adapter=adapter,chat_type=chat_type,media=media or ["text"]) if self._env else {}

    @API(name="create_proactive_opportunity",description="创建外部主动分享契机",version="1",public=True)
    async def api_create_opportunity(self,topic:str="",motive:str="",weight:float=0.5,expires_minutes:int=120,**kwargs:Any)->dict[str,Any]:
        if not self._ready or not topic:return {"success":False,"error":"not_ready_or_empty_topic"}
        assert self._store and self._env and self._schedule
        now=self._env.now(); current=(await self._schedule.context(now)).get("current")
        if not current:return {"success":False,"error":"no_current_framework"}
        oid=hashlib.sha1(f"api:{now.timestamp()}:{topic}".encode()).hexdigest()[:20]
        await self._store.add_opportunity({"id":oid,"framework_id":current["id"],"topic":topic[:160],"motive":motive[:240] or "外部生活事件值得分享",
            "weight":max(0,min(1,float(weight))),"privacy":"normal","expires_at":now.timestamp()+max(1,expires_minutes)*60})
        return {"success":True,"opportunity_id":oid}


def create_plugin()->MaiBotPlugin:return MaiLifePlugin()
