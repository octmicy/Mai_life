"""Mai_life WebUI 配置模型。

所有分组和字段都提供中文标签、中文说明及英文 i18n，避免 WebUI 直接显示
``user_id``、``wake_probability`` 等内部字段名。
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

CONFIG_SCHEMA_VERSION = "1.0.2"
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _ui(
    label: str,
    hint: str,
    order: int,
    *,
    label_en: str = "",
    hint_en: str = "",
    placeholder: str | None = None,
    hidden: bool = False,
    disabled: bool = False,
    enum_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造 MaiBot WebUI 使用的字段元数据。"""
    zh: dict[str, str] = {"label": label, "hint": hint}
    en: dict[str, str] = {"label": label_en or label, "hint": hint_en or hint}
    result: dict[str, Any] = {
        "label": label,
        "hint": hint,
        "order": order,
        "i18n": {"zh_CN": zh, "en_US": en},
    }
    if placeholder is not None:
        result["placeholder"] = placeholder
        zh["placeholder"] = placeholder
        en["placeholder"] = placeholder
    if hidden:
        result["hidden"] = True
    if disabled:
        result["disabled"] = True
    if enum_labels:
        # 同时提供两种常见枚举翻译扩展，兼容不同 WebUI 渲染版本。
        result["enum_labels"] = enum_labels
        result["x-enumNames"] = [enum_labels[key] for key in enum_labels]
    return result


def _time(value: Any, default: str) -> str:
    """把非法时间恢复为安全默认值。"""
    text = str(value or "").strip()
    return text if _TIME_RE.fullmatch(text) else default


class PluginSettings(PluginConfigBase):
    """插件开关、模型与管理员配置。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Plugin Settings", "description": "Basic switch, model and administrator settings."}
    }

    # SDK 2.7+ 强制要求 plugin.config_version，用于配置补齐与迁移判定。
    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 Schema 版本，请勿手动修改。",
        json_schema_extra=_ui(
            "配置版本", "由插件维护，请勿手动修改。", 99,
            label_en="Config Version", hint_en="Managed by the plugin. Do not edit manually.",
            hidden=True, disabled=True,
        ),
    )
    enabled: bool = Field(
        default=True,
        description="麦麦生活总开关。关闭后停止状态维护、日程生成和主动私聊。",
        json_schema_extra=_ui(
            "启用麦麦生活", "关闭后保留数据库，但停止所有后台模拟与主动行为。", 0,
            label_en="Enable Mai Life", hint_en="Keep data but stop simulation and proactive behavior when disabled.",
        ),
    )
    llm_model: Literal["reply", "planner", "utils"] = Field(
        default="planner",
        description="生成日程、场景和梦境时使用的 MaiBot 模型类型。",
        json_schema_extra=_ui(
            "生活生成模型", "planner 最均衡；reply 语言更自然；utils 成本通常更低。", 1,
            label_en="Life Generation Model", hint_en="Model used for schedules, scenes and dreams.",
            enum_labels={"reply": "回复模型（reply）", "planner": "规划模型（planner）", "utils": "工具模型（utils）"},
        ),
    )
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="允许执行日程重生成和休息诊断命令的 QQ 号列表。",
        json_schema_extra=_ui(
            "管理员 QQ 列表", "留空时，私聊用户列表中第一个有效 QQ 自动成为管理员。", 2,
            label_en="Administrator QQ IDs", hint_en="If empty, the first valid private user becomes administrator.",
            placeholder="123456789",
        ),
    )


class UserProfile(PluginConfigBase):
    """单个私聊网友的关系和免打扰设置。"""

    __ui_label__: ClassVar[str] = "私聊用户资料"
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Private User Profile", "description": "Per-user relationship and quiet-hour settings."}
    }

    user_id: str = Field(
        default="",
        description="QQ 用户 ID。",
        json_schema_extra=_ui(
            "QQ 号", "填写允许进入麦麦生活系统的 QQ 号。", 0,
            label_en="QQ User ID", hint_en="QQ account allowed to use Mai Life.", placeholder="123456789",
        ),
    )
    enabled: bool = Field(
        default=True,
        description="是否记录该用户的互动和关系状态。",
        json_schema_extra=_ui(
            "启用用户档案", "关闭后不记录该用户互动，也不会注入生活上下文。", 1,
            label_en="Enable User Profile", hint_en="Disable to stop relationship tracking and context injection for this user.",
        ),
    )
    proactive_enabled: bool = Field(
        default=True,
        description="是否允许麦麦向该用户主动发起私聊。",
        json_schema_extra=_ui(
            "允许主动私聊", "关闭后仍可正常被动聊天，但麦麦不会主动找这个用户。", 2,
            label_en="Allow Proactive Chat", hint_en="Passive chat remains available, but Mai will not proactively message this user.",
        ),
    )
    display_name: str = Field(
        default="",
        description="麦麦用于识别该用户的称呼。",
        json_schema_extra=_ui(
            "用户称呼", "可留空；填写后用于状态面板和后续个性化上下文。", 3,
            label_en="Display Name", hint_en="Optional name used in status and personalized context.", placeholder="例如：小明",
        ),
    )
    initial_temperature: int = Field(
        default=30, ge=0, le=100,
        description="首次创建用户档案时使用的关系温度。",
        json_schema_extra=_ui(
            "初始关系温度", "仅首次创建档案时使用。30 表示“认识”，不会自动进入恋爱语气。", 4,
            label_en="Initial Relationship Temperature", hint_en="Used only when the profile is first created. Default 30 means acquaintance.",
        ),
    )
    quiet_start: str = Field(
        default="00:00",
        description="该用户免打扰时段开始时间。",
        json_schema_extra=_ui(
            "免打扰开始", "此时间后不向该用户主动发消息，支持跨午夜时段。格式 HH:MM。", 5,
            label_en="Quiet Hours Start", hint_en="No proactive messages after this time. HH:MM format.", placeholder="00:00",
        ),
    )
    quiet_end: str = Field(
        default="08:00",
        description="该用户免打扰时段结束时间。",
        json_schema_extra=_ui(
            "免打扰结束", "到达此时间后恢复主动消息。格式 HH:MM。", 6,
            label_en="Quiet Hours End", hint_en="Proactive messaging resumes after this time. HH:MM format.", placeholder="08:00",
        ),
    )

    @field_validator("user_id", mode="before")
    @classmethod
    def normalize_user_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("quiet_start", mode="before")
    @classmethod
    def validate_quiet_start(cls, value: Any) -> str:
        return _time(value, "00:00")

    @field_validator("quiet_end", mode="before")
    @classmethod
    def validate_quiet_end(cls, value: Any) -> str:
        return _time(value, "08:00")


class UsersSettings(PluginConfigBase):
    """允许进入陪伴系统的私聊用户列表。"""

    __ui_label__: ClassVar[str] = "私聊用户"
    __ui_order__: ClassVar[int] = 1
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Private Users", "description": "Users allowed to share Mai's global life timeline."}
    }

    profiles: list[UserProfile] = Field(
        default_factory=list,
        description="允许进入麦麦生活系统的私聊用户。",
        json_schema_extra=_ui(
            "私聊用户列表", "麦麦生活状态全局共享，但每位用户的关系、额度和免打扰相互独立。", 0,
            label_en="Private User Profiles", hint_en="Life state is global while relationship, quota and quiet hours are per user.",
        ),
    )


class EnvironmentSettings(PluginConfigBase):
    """时间与现实天气背景配置。"""

    __ui_label__: ClassVar[str] = "环境与天气"
    __ui_order__: ClassVar[int] = 2
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Environment and Weather", "description": "Timezone and Open-Meteo weather location."}
    }

    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA 时区名称。",
        json_schema_extra=_ui(
            "时区", "影响日程日期、免打扰和每日结算。中国大陆通常使用 Asia/Shanghai。", 0,
            label_en="Timezone", hint_en="IANA timezone used for schedules, quiet hours and daily settlement.", placeholder="Asia/Shanghai",
        ),
    )
    city: str = Field(
        default="Shanghai",
        description="Open-Meteo 查询天气时使用的城市。",
        json_schema_extra=_ui(
            "天气城市", "填写麦麦生活所在地的城市名，插件会自动查询天气坐标。", 1,
            label_en="Weather City", hint_en="City name used to automatically resolve weather coordinates.", placeholder="Shanghai",
        ),
    )
    weather_refresh_minutes: int = Field(
        default=30, ge=10, le=360,
        description="天气缓存刷新间隔。",
        json_schema_extra=_ui(
            "天气刷新间隔（分钟）", "后台刷新，不会在被动回复时等待网络请求。建议 30～60 分钟。", 4,
            label_en="Weather Refresh Interval (min)", hint_en="Background refresh interval. Passive replies never wait for weather requests.",
        ),
    )


class StateSettings(PluginConfigBase):
    """生活数值推进与身体周期配置。"""

    __ui_label__: ClassVar[str] = "生活状态"
    __ui_order__: ClassVar[int] = 3
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Life State", "description": "Deterministic energy, hunger, health and body-cycle simulation."}
    }

    tick_interval_minutes: int = Field(
        default=10, ge=1, le=60,
        description="生活状态推进间隔。",
        json_schema_extra=_ui(
            "状态推进间隔（分钟）", "越短状态更新越及时，但数据库写入更频繁。建议保持 10 分钟。", 0,
            label_en="State Tick Interval (min)", hint_en="Shorter intervals update faster but write to the database more often.",
        ),
    )
    body_cycle_enabled: bool = Field(
        default=False,
        description="是否启用身体周期模拟。",
        json_schema_extra=_ui(
            "启用身体周期", "默认关闭。启用前请确认符合麦麦的人格和身体设定。", 1,
            label_en="Enable Body Cycle", hint_en="Disabled by default. Enable only when it fits the character setting.",
        ),
    )
    body_cycle_start_date: str = Field(
        default="",
        description="身体周期第 1 天对应的日期。",
        json_schema_extra=_ui(
            "周期起始日期", "格式 YYYY-MM-DD；仅在启用身体周期后使用。", 2,
            label_en="Cycle Start Date", hint_en="Day one of the cycle in YYYY-MM-DD format.", placeholder="2026-07-01",
        ),
    )
    body_cycle_length_days: int = Field(
        default=28, ge=20, le=45,
        description="完整身体周期长度。",
        json_schema_extra=_ui(
            "周期长度（天）", "允许范围 20～45 天，默认 28 天。", 3,
            label_en="Cycle Length (days)", hint_en="Full cycle length, from 20 to 45 days.",
        ),
    )
    body_cycle_period_days: int = Field(
        default=5, ge=1, le=10,
        description="周期中经期持续天数。",
        json_schema_extra=_ui(
            "经期持续天数", "允许范围 1～10 天，只用于轻度状态修正。", 4,
            label_en="Period Duration (days)", hint_en="From 1 to 10 days; only used for mild state adjustments.",
        ),
    )


class RestGateSettings(PluginConfigBase):
    """睡眠和午休期间的被动回复判醒设置。"""

    __ui_label__: ClassVar[str] = "休息回复闸门"
    __ui_order__: ClassVar[int] = 4
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Rest Reply Gate", "description": "Decides whether Mai wakes up for passive messages during rest."}
    }

    enabled: bool = Field(
        default=False,
        description="是否启用睡眠和午休期间的判醒闸门。",
        json_schema_extra=_ui(
            "启用休息回复闸门", "默认关闭。开启后，麦麦睡眠或午休时可能暂不回复普通消息。", 0,
            label_en="Enable Rest Reply Gate", hint_en="When enabled, Mai may remain silent for ordinary messages during sleep or naps.",
        ),
    )
    mode: Literal["probability", "llm"] = Field(
        default="probability",
        description="普通消息使用概率还是 LLM 进行判醒。",
        json_schema_extra=_ui(
            "判醒模式", "规则会先处理明确勿扰和紧急叫醒；这里只控制剩余普通消息。", 1,
            label_en="Wake Decision Mode", hint_en="Explicit quiet and urgent wake signals are handled before this mode.",
            enum_labels={"probability": "规则边界 + 概率", "llm": "LLM 模型判断"},
        ),
    )
    wake_probability: float = Field(
        default=0.18, ge=0, le=1,
        description="概率模式下普通消息叫醒麦麦的概率。",
        json_schema_extra=_ui(
            "普通消息醒来概率", "0.18 表示 18%。只在 probability 模式下生效。", 2,
            label_en="Wake Probability", hint_en="0.18 means 18%. Only used in probability mode.",
        ),
    )
    llm_threshold: int = Field(
        default=70, ge=0, le=100,
        description="LLM 判醒分数阈值。",
        json_schema_extra=_ui(
            "LLM 判醒阈值", "分数达到该值且模型认为应回复时才会醒来。只在 LLM 模式生效。", 3,
            label_en="LLM Wake Threshold", hint_en="Wake only when the model score reaches this threshold.",
        ),
    )
    awake_grace_minutes: int = Field(
        default=30, ge=0, le=240,
        description="被叫醒后免于重复判醒的时间。",
        json_schema_extra=_ui(
            "醒来缓冲（分钟）", "缓冲期间普通消息直接放行，避免睡眠段内每条消息都重新判醒。", 4,
            label_en="Awake Grace Period (min)", hint_en="Messages pass directly during this period without another wake decision.",
        ),
    )
    gate_segment_types: list[Literal["sleep", "nap", "rest"]] = Field(
        default_factory=lambda: ["sleep", "nap"],
        description="哪些日程类型启用休息回复闸门。",
        json_schema_extra=_ui(
            "闸门生效的日程类型", "建议保持“夜间睡眠 + 午休”。加入 rest 后，普通休息段也可能不回复。", 5,
            label_en="Gated Schedule Types", hint_en="Recommended: sleep and nap. Adding rest also gates ordinary rest periods.",
            enum_labels={"sleep": "夜间睡眠（sleep）", "nap": "午休小睡（nap）", "rest": "普通休息（rest）"},
        ),
    )


class ScheduleSettings(PluginConfigBase):
    """每日生活框架与临近场景细化设置。"""

    __ui_label__: ClassVar[str] = "日程与场景"
    __ui_order__: ClassVar[int] = 5
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Schedule and Scenes", "description": "Daily framework generation and near-term scene expansion."}
    }

    generate_hour: int = Field(
        default=3, ge=0, le=23,
        description="每天强制重新生成生活框架的小时。",
        json_schema_extra=_ui(
            "日程生成时间（小时）", "0～23 的整数。默认凌晨 3 点；缺少当日日程时仍会立即生成。", 0,
            label_en="Schedule Generation Hour", hint_en="Hour from 0 to 23. Missing schedules are still generated immediately.",
        ),
    )
    detail_lead_minutes: int = Field(
        default=60, ge=10, le=240,
        description="提前多少分钟细化即将开始的生活场景。",
        json_schema_extra=_ui(
            "场景提前细化时间（分钟）", "默认提前 60 分钟细化当前段和下一段，减少模型调用并保持环境新鲜。", 1,
            label_en="Scene Detail Lead Time (min)", hint_en="Expand current and next scenes this many minutes before they start.",
        ),
    )
    template_file: str = Field(
        default="mai_template.json",
        description="日程骨架模板文件名。",
        json_schema_extra=_ui(
            "日程模板文件", "相对于插件目录。LLM 失败或返回非法日程时使用该模板降级。", 2,
            label_en="Schedule Template File", hint_en="Path relative to the plugin directory, used as generation fallback.", placeholder="mai_template.json",
        ),
    )


class ProactiveSettings(PluginConfigBase):
    """多用户主动私聊频率和评分阈值。"""

    __ui_label__: ClassVar[str] = "主动私聊"
    __ui_order__: ClassVar[int] = 6
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en_US": {"title": "Proactive Private Chat", "description": "Quota, cooldown and candidate scoring settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用主动私聊巡检。",
        json_schema_extra=_ui(
            "启用主动私聊", "关闭后麦麦仍维护生活状态，但不会主动找任何用户。", 0,
            label_en="Enable Proactive Chat", hint_en="Life simulation continues, but Mai will not proactively message users.",
        ),
    )
    patrol_interval_minutes: int = Field(
        default=10, ge=1, le=60,
        description="检查主动聊天契机的间隔。",
        json_schema_extra=_ui(
            "主动巡检间隔（分钟）", "建议 10 分钟。更短只会增加检查频率，不代表一定发送更多消息。", 1,
            label_en="Proactive Patrol Interval (min)", hint_en="How often proactive opportunities are evaluated.",
        ),
    )
    daily_max_per_user: int = Field(
        default=2, ge=0, le=20,
        description="每位用户每天实际主动发送上限。",
        json_schema_extra=_ui(
            "每用户每日主动上限", "默认 2 次。Planner 选择沉默时不会消耗实际发送额度。", 2,
            label_en="Daily Limit per User", hint_en="Maximum actual proactive messages per user each day.",
        ),
    )
    min_interval_minutes: int = Field(
        default=180, ge=1, le=1440,
        description="同一用户两次主动消息之间的最小间隔。",
        json_schema_extra=_ui(
            "主动消息最小间隔（分钟）", "默认 180 分钟，防止短时间连续打扰同一用户。", 3,
            label_en="Minimum Proactive Interval (min)", hint_en="Minimum time between proactive messages to the same user.",
        ),
    )
    recent_user_silence_minutes: int = Field(
        default=30, ge=0, le=240,
        description="用户刚发言后暂停主动消息的时间。",
        json_schema_extra=_ui(
            "用户发言后冷却（分钟）", "用户刚刚聊过时无需再主动开场。默认等待 30 分钟。", 4,
            label_en="Cooldown after User Message (min)", hint_en="Do not start another proactive topic immediately after the user speaks.",
        ),
    )
    pending_expire_seconds: int = Field(
        default=120, ge=30, le=600,
        description="主动触发与 Replyer 回复关联的有效时间。",
        json_schema_extra=_ui(
            "主动回复确认窗口（秒）", "用于判断 Planner 是否真的生成了主动回复。通常无需修改。", 5,
            label_en="Proactive Confirmation Window (sec)", hint_en="Window used to correlate a Planner trigger with an actual Replyer response.",
        ),
    )
    minimum_energy: int = Field(
        default=25, ge=0, le=100,
        description="允许普通主动聊天所需的最低精力。",
        json_schema_extra=_ui(
            "主动聊天最低精力", "低于该值时麦麦优先休息，不产生普通主动开场。", 6,
            label_en="Minimum Energy for Proactive Chat", hint_en="Mai prioritizes rest below this energy level.",
        ),
    )
    score_threshold: float = Field(
        default=0.45, ge=0, le=2,
        description="主动候选进入 Planner 终审所需的最低综合分。",
        json_schema_extra=_ui(
            "主动候选分数阈值", "越高越克制。综合考虑契机、精力、心情、关系和用户活跃时间。", 7,
            label_en="Proactive Candidate Score Threshold", hint_en="Higher values make proactive behavior more conservative.",
        ),
    )


class MaiLifeSettings(PluginConfigBase):
    """麦麦生活完整配置。"""

    plugin: PluginSettings = Field(
        default_factory=PluginSettings,
        json_schema_extra=_ui("插件设置", "总开关、模型与管理员。", 0, label_en="Plugin Settings", hint_en="Switch, model and administrators."),
    )
    users: UsersSettings = Field(
        default_factory=UsersSettings,
        json_schema_extra=_ui("私聊用户", "配置允许互动和主动私聊的用户。", 1, label_en="Private Users", hint_en="Users allowed to interact with Mai Life."),
    )
    environment: EnvironmentSettings = Field(
        default_factory=EnvironmentSettings,
        json_schema_extra=_ui("环境与天气", "时区、城市和天气刷新。", 2, label_en="Environment and Weather", hint_en="Timezone, location and weather refresh."),
    )
    state: StateSettings = Field(
        default_factory=StateSettings,
        json_schema_extra=_ui("生活状态", "精力、饥饿、健康和周期。", 3, label_en="Life State", hint_en="Energy, hunger, health and body cycle."),
    )
    rest_gate: RestGateSettings = Field(
        default_factory=RestGateSettings,
        json_schema_extra=_ui("休息回复闸门", "睡眠和午休期间的判醒逻辑。", 4, label_en="Rest Reply Gate", hint_en="Wake decisions during sleep and naps."),
    )
    schedule: ScheduleSettings = Field(
        default_factory=ScheduleSettings,
        json_schema_extra=_ui("日程与场景", "每日框架和临近场景细化。", 5, label_en="Schedule and Scenes", hint_en="Daily framework and scene expansion."),
    )
    proactive: ProactiveSettings = Field(
        default_factory=ProactiveSettings,
        json_schema_extra=_ui("主动私聊", "主动消息额度、冷却和评分。", 6, label_en="Proactive Private Chat", hint_en="Quota, cooldown and candidate scoring."),
    )
