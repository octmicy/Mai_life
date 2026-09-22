"""tests/ 共享的 mock 与构造助手。

集中放置历史修复回归文件里反复定义的替身类与插件组装逻辑；
本文件不是 test_ 开头，unittest discover 不会收集它。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from Mai_life.config import MaiLifeSettings
from Mai_life.core.llm_service import LLMService

# 三处历史版本（v1.14.4 state / v1.14.4 gate / v1.14.5 gate）共用的东八区时区。
TZ = timezone(timedelta(hours=8))


class DummyLogger:
    """全 no-op 日志替身，仅把 error 调用收集到 errors 供断言。"""

    def __init__(self):
        self.errors: list[tuple] = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def error(self, *args, **kwargs):
        self.errors.append(args)


class RecordingLogger:
    """记录各级别日志调用，供断言日志级别与限流次数。

    同时兼容历史两套读取 API：
    - records/texts(level)：v1.14.3、v1.14.5 的写法；
    - calls/messages(level)：v1.14.3 memory_weather、v1.14.4 state 的写法。
    """

    def __init__(self):
        self.records: list[tuple[str, tuple]] = []
        self.calls: list[tuple[str, str]] = []

    def _log(self, level, *args):
        self.records.append((level, args))
        self.calls.append((level, str(args[0]) if args else ""))

    def info(self, *args): self._log("info", *args)
    def warning(self, *args): self._log("warning", *args)
    def error(self, *args): self._log("error", *args)
    def debug(self, *args): self._log("debug", *args)

    def texts(self, level: str) -> list[str]:
        return [str(args[0]) if args else "" for lv, args in self.records if lv == level]

    def messages(self, level: str) -> list[str]:
        return [text for lv, text in self.calls if lv == level]

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class DummyLLM:
    """无模型环境：任何任务都不可用，generate 返回空串。

    所有服务的 generate_json 调用点都在 task_available 为真之后，
    因此该替身只用于把代码路径钉在本地兜底分支上。
    """

    def task_available(self, kind): return False

    def task_for(self, kind): return "planner"

    async def generate(self, *args, **kwargs): return ""

    async def generate_json(self, prompt, system, fallback=None, max_tokens=0, **kwargs):
        return fallback if fallback is not None else {}


class DummyStateEngine:
    """生活状态引擎替身：mark_woken 空实现。"""

    async def mark_woken(self, *args, **kwargs): pass


class DummyCtx:
    """空 ctx 替身：只带 logger 属性，满足 _set_context 后继服务的日志读取。"""

    def __init__(self, logger=None):
        self.logger = logger if logger is not None else DummyLogger()


async def build_plugin(store, config, *, ctx=None, users=(), llm=None, now=None,
                       plugin_dir: str = "."):
    """按 v1.14.3 StatusNoteTests 的全服务模式组装 MaiLifePlugin 并挂上下文。

    - ctx：缺省构造 DummyCtx；调用方需要断言发送内容时传入自带 send/chat 的替身。
    - users：非空时先 sync_users 并逐个数 user stream（沿用 v1.14.4 命令层写法）。
    - llm：缺省构造可用任务为空的 LLMService；传入 DummyLLM() 可完全隔离模型层。
    - now：非空时冻结 _env.now()，供夜间闸门类测试使用。
    """
    from Mai_life.core.environment import EnvironmentService
    from Mai_life.creation.bookshelf_service import BookshelfService
    from Mai_life.creation.creation_service import CreationService
    from Mai_life.information.information_service import InformationService
    from Mai_life.life.bedtime import BedtimeManager
    from Mai_life.life.continuity import ContinuityService
    from Mai_life.life.life_state import LifeStateEngine
    from Mai_life.life.memory_service import MemoryService
    from Mai_life.life.proactive import ProactiveEngine
    from Mai_life.life.rest_gate import RestGate
    from Mai_life.life.schedule_service import ScheduleService
    from Mai_life.management.admin_service import AdminService
    from Mai_life.messaging.message_pipeline import MessageDebouncer
    from Mai_life.messaging.recall_service import RecallService
    from Mai_life.plugin import MaiLifePlugin
    from Mai_life.social.group_observer import GroupObserver
    from Mai_life.social.relay_service import RelayService

    if ctx is None:
        ctx = DummyCtx()
    logger = getattr(ctx, "logger", None) or DummyLogger()
    if users:
        await store.sync_users(list(users))
        for user in users:
            await store.set_user_stream(str(user.user_id), f"stream-{user.user_id}")
    if llm is None:
        llm = LLMService(ctx, config, store)
        llm.available_tasks = set()  # 没有任何可用任务 → 叙事全缺
        llm.health_error = ""
    plugin = MaiLifePlugin()
    plugin._set_context(ctx)
    plugin.set_plugin_config(config.model_dump(mode="python"))
    plugin._store = store
    plugin._env = EnvironmentService(store, config, logger)
    plugin._llm = llm
    plugin._state = LifeStateEngine(store, config, llm, logger)
    plugin._schedule = ScheduleService(store, config, llm, plugin_dir, logger)
    plugin._rest = RestGate(store, config, llm, plugin._state, logger)
    plugin._proactive = ProactiveEngine(ctx, store, config, plugin._env, logger)
    plugin._debouncer = MessageDebouncer(config, logger)
    plugin._recall = RecallService(ctx, store, config, logger)
    plugin._continuity = ContinuityService(store, config, llm, logger)
    plugin._memory = MemoryService(store, config, llm, logger)
    plugin._information = InformationService(ctx, store, config, llm, logger)
    plugin._group_observer = GroupObserver(store, config, llm, logger)
    plugin._relay = RelayService(ctx, store, config, logger)
    plugin._bookshelf = BookshelfService(store, config)
    plugin._creation = CreationService(ctx, store, config, llm, logger)
    plugin._admin = AdminService(store, config)
    plugin._bedtime = BedtimeManager(ctx, store, config, plugin._env, plugin._state, logger)
    if now is not None:
        plugin._env.now = lambda: now
    return plugin


def group_message(mid: str, text: str, group_id: str = "100", user_id: str = "30001"):
    """构造一条 napcat 群聊消息（v1.14.4 gate / v1.14.5 gate 两处同源）。"""
    return {"message_id": mid, "session_id": f"group-stream-{group_id}", "platform": "qq",
            "processed_plain_text": text, "message_info": {
                "user_info": {"user_id": user_id, "user_nickname": "可变昵称"},
                "group_info": {"group_id": group_id, "group_name": "Host 自动群名"},
                "additional_config": {"napcat_message_type": "group"}},
            "raw_message": [{"type": "text", "data": text}], "is_command": False, "is_notify": False}


def make_group_config(*, group_gate: bool, group_ids: tuple[str, ...] = ("100",),
                      group_mode: str = "all") -> MaiLifeSettings:
    """构造群闸门配置：总开关 + 分群模式 + 白名单（v1.14.5 的 make_config）。"""
    from Mai_life.config import SocialGroupProfile

    config = MaiLifeSettings()
    config.rest_gate.group_enabled = group_gate
    config.rest_gate.group_mode = group_mode
    config.social.enabled = True
    config.social.groups = [
        SocialGroupProfile(group_id=gid, enabled=True, observe_enabled=True,
                           relay_target_enabled=False, rest_gate_enabled=True)
        for gid in group_ids
    ]
    return config
