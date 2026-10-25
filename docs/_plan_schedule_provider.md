# 日程提供方三接口调研与执行报告（v1.23.5 拟定，未动代码）

> 为 MaiTrace 接入拟定的三个公开接口：`get_schedule_provider_info` / `get_schedule_snapshot` / `get_schedule_timeline`。
> 本报告基于 2026-10-20 对 Mai_life 当前代码（v1.23.4，9c85d87→2bc9afe→54e18d0）的只读调研。

## 0. 结论

- **三个接口全部可落地，无阻塞项。** 数据面完全够用：时区配置现成、活动 ID 天然跨天唯一且内容稳定、72 小时保留期成立、场景支持批量读取。
- **两个设计决策需要 MaiTrace 侧确认**（不阻塞开发，先按本报告取值实现）：① 活动的 `description` 字段 Mai_life 没有独立数据源，拟返回空串；② 失败信封的 `error` 拟为字符串、`data` 为 null。
- 预估改动：plugin.py 约 +90 行（3 个 @API + 3 个辅助函数）、测试新增 1 类约 8 条、guide 行号重算（插入点约 2020 行，之后引用整体位移）、CHANGELOG 1.23.5、AGENTS.md 计数、版本号 6 处。

## 1. 现状盘点（只读调研结果）

### 1.1 数据面

- **日程表** `daily_framework`（core/storage.py:186-191）：`id/day/start_minute/end_minute/kind/summary/location/energy_load/shareability`，按 `day` 存取、按 `start_minute` 排序返回（`get_framework` storage.py:777-780）。
- **活动 ID 已是内容哈希**：`sha1(f"{day}:{start}:{end}:{kind}:{summary}")[:16]`（life/schedule_service.py:151、181）——跨天唯一、同一内容重生成幂等不变。拟定方案的「稳定的活动ID」**无需再加前缀**，直接透出节点 id。
- **校验保证**：段间无重叠（start 钳到上一段 end，schedule_service.py `_validate`）、start<end、kind 限定 8 种枚举（sleep/nap/rest/study/work/leisure/meal/travel）——与拟定方案示例的 `kind:"study"` 同词汇表。
- **场景表** `detailed_scenes`（storage.py:192-197）：`framework_id/scene/state_deltas/created_at/applied`；**只有被细化的节点有场景**（`expand_due` 只细化当前与下一节点，schedule_service.py:287-292），过去节点大概率 `scene=""`——拟定方案已预留「scene 可以为空」✓。批量读取 `get_scenes_by_framework_ids`（storage.py:806）可一次取整段时间线的场景。
- **保留期**：日程只在同日重写时被替换（`replace_framework` storage.py:762-776，同时清旧场景与机会），**没有任何按龄清理**（storage.py:17 注释明确清理策略分工）；`ensure_day` 只对“今天”生成。→ 「保存并允许读取过去至少 72 小时」**成立**；timeline 也天然满足「查询不生成新日程」（纯 `get_framework` 读）。
- **跨午夜**：`end_minute` 可为 1440（23:30-24:00 段），换算 `day 00:00 + timedelta(minutes=end_minute)` 自然落到次日 00:00。

### 1.2 时区（拟定方案最难的外部依赖，结果是完全现成）

- `config.environment.timezone`（config.py:458-465）：**IANA 名称，默认 `Asia/Shanghai`**，与拟定方案示例一致。
- `EnvironmentService.now()` 返回**带时区**的 datetime（ZoneInfo(config.environment.timezone)，缺 tzdata 时回退 UTC+8 并告警一次，core/environment.py:85-95）。
- provider_info 的 `timezone` 直接读配置字符串；snapshot 的 `as_of` 用 `now().isoformat()`（自动带 `+08:00` 偏移）；timeline 的 `start_at/end_at` 解析规则：ISO 解析（`fromisoformat`），naive 按配置时区解释，aware 用 `astimezone` 归一到配置时区。

### 1.3 接口面

- 现有公开 API 共 6 个，全部 `@API(name=..., version="1", public=True)`，**裸返回**业务数据（如 `get_schedule` 直接返回分钟数组，plugin.py:2016-2020）。新三接口按 MaiTrace 契约返回 `{"success","error","data"}` 信封——**这是新约定，只用于这三个接口**，不回改旧接口（主人已说明「当前已有接口可以继续保留」）。
- 宿主对 `api.call` 的包装：调用方拿到 `{"success": bool, "result": <方法返回值>}`（与 drawpic 集成文档同款跨插件约定）——信封位于 `result` 内层。业务错误**不抛异常**（抛了会变成 RPC 级失败），一律走信封返回，与 WebUI API 的既有惯例一致。
- 名称无冲突：现有 `get_schedule` 与三个新名互不相同；注册版本统一 `version="1"`，`protocol_version=1` 与之对应，不兼容变更时两者同步升版。

### 1.4 调用形态（MaiTrace 侧）

`ctx.api.call("maibot-community.mai-life.get_schedule_snapshot", version="1", future_limit=3)` → `result` 即信封。参数经 kwargs 透传（`future_limit`/`start_at`/`end_at`）。

## 2. 逐接口映射设计

### 2.1 get_schedule_provider_info()

| 字段 | 值 | 来源 |
| --- | --- | --- |
| provider_id | `"maibot-community.mai-life"` | manifest id |
| protocol | `"maibot.schedule"` | 双方约定 |
| protocol_version | `1` | 双方约定 |
| api_version | `"1"` | 宿主 @API 注册版本（新增字段，落实「接口注册版本明确约定」） |
| scope | `"global"` | 麦麦单条全局时间线 |
| timezone | `config.environment.timezone` | IANA 字符串原样 |
| capabilities | `["snapshot","timeline"]` | — |
| max_query_hours | `72` | 与 timeline 的跨度上限一致 |

无参数；不依赖 `_ready`（信息声明类，服务未就绪也应可答）。

### 2.2 get_schedule_snapshot(future_limit=3)

- `future_limit`：默认 3，接受 0–20，越界钳制。
- 读取 `_schedule.context(now)`——**只读**（`context()` 只查库；生成只发生在维护 tick 与热更新路径），满足无副作用。
- `schedule_available`：当日框架非空。为 false 时 `current=null`、`next_activities=[]`、`revision=""`（bot 刚启动、维护 tick 未跑时会出现，属诚实状态）。
- `current`：`start_minute <= now < end_minute` 的节点（与 `current_and_next` 同判定，schedule_service.py:278-284）；无（空档期）→ null。
- `next_activities`：有 current 时取 `start_minute >= current.end_minute` 的节点；无 current 时取 `start_minute > now` 的节点；按 start 排序后截 `future_limit`。
- 活动字段映射：`id`=节点 id（内容哈希）；`kind`=原始枚举；`name`=summary；`description`=`""`（Mai_life 无独立字段，**待 MaiTrace 确认**）；`start_at/end_at`=当日 00:00 + 分钟数（配置时区 aware ISO）；`location`；`scene`=场景文本或 `""`。
- `revision`：当日节点 id 序列的 sha1[:16]——内容不变则不变（重生成幂等），内容变化即变。
- 未就绪（`_ready` 为假）返回 `{"success":False,"error":"日程服务尚未初始化","data":None}`。

### 2.3 get_schedule_timeline(start_at, end_at)

- 参数解析：`fromisoformat`（3.13 支持 `Z` 与偏移）；解析失败/`start>=end`/跨度 >72h → 信封报错（`max_query_hours` 与 provider_info 声明一致）。
- 遍历 `start.date()..end.date()`（按配置时区的日历日）逐日 `get_framework`；**空日进 `missing_days`**（有框架但无交集活动不算缺失）。
- 交集判定 `a_start < range_end and a_end > range_start`；结果按 `start_at` 排序；字段与快照活动一致。
- 场景批量 `get_scenes_by_framework_ids`（一次查询，避免 N 次）。
- 纯读，不触发生成、不用模板补齐 ✓。

## 3. 实施位置与改动清单（下一轮执行）

1. **plugin.py**（约 +90 行）：`api_get_schedule`（现 2016-2020 行）之后新增三个 `@API(..., version="1", public=True)` 方法，加辅助 `_schedule_tz()` / `_schedule_activity(node, tz, scene)` / `_framework_revision(nodes)`；所有业务分支走信封、不抛异常。
2. **测试**：新类 `V1235ScheduleProviderTests`（test_fixes.py），真实 `LifeStore` + `ScheduleService`，用 `replace_framework`+`save_scene` 预置数据（不触 LLM）。用例：provider_info 字段全量断言；快照映射（分钟→ISO、当前段、scene、next 排序与截断）、空日程 `schedule_available=false`；时间线交集/排序/missing_days/72h 拒绝/不生成（查完框架仍缺）；未就绪信封。预计 8 条。
3. **文档**：guide §1.8 标题 18→21 个 @API（6→9 公开）、正文与 §12 清单补三接口、plugin.py 行号重算（插入点 ~2020，之后引用整体 +约 90）；CHANGELOG 1.23.5；AGENTS.md 计数；版本号 6 处。
4. **README**：「公开 API 保持」清单（6 条）需要补 3 条——README 由主人维护，本轮不动，提交时提醒。

## 4. 风险与待确认

| 项 | 状态 |
| --- | --- |
| `description` 恒为空串（无独立数据源） | **待 MaiTrace 确认**；可改为复用 summary 但与 name 重复 |
| 失败信封 `error` 为字符串、`data=null` | **待 MaiTrace 确认**（拟定方案只定义了成功形态） |
| `scene` 大概率为空（只细化当前/下一节点，过去节点尤甚） | 拟定方案已预留 ✓，MaiTrace 需容忍 |
| 72h 保留依赖「无按龄清理」现状 | 当前成立；未来若加日程清理策略，`max_query_hours` 必须同步下调 |
| 快照无副作用（当日未生成时 `schedule_available=false`，不即时兜底生成） | 与「查询不生成」一致；如 MaiTrace 希望快照触发补生成需另行约定 |
| 同日重写（热更新 force）会使场景清空、id 不变但内容可能变 | revision 哈希会随之变化 ✓；MaiTrace 侧以 revision 判断刷新 |
| kind 为英文枚举 8 种 | 与拟定示例一致 ✓；未知值透传 |

## 5. 执行顺序（下一轮，待主人放行）

1. plugin.py 三接口 + 辅助函数（信封化错误处理）→ py_compile
2. 测试类 8 条 → 全量回归
3. guide 内容 + 行号重算 → CHANGELOG → AGENTS.md → 版本号 6 处
4. commit（不推送，发布等指示）
