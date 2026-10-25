# 日程提供方接口（maibot.schedule 协议 v1）

适合开发同一 MaiBot 实例内其他插件/工具的接入方（如 MaiTrace）。本页说明如何调用麦生活的日程查询接口、请求参数与返回格式。协议设计讨论见[调研报告](_plan_schedule_provider.md)。

目录：[提供方信息](#提供方信息) · [调用方式](#调用方式) · [返回信封](#返回信封) · [快照接口](#get_schedule_snapshot-当前快照) · [时间线接口](#get_schedule_timeline-时间线查询) · [活动结构](#活动结构) · [错误一览](#错误一览) · [边界与保证](#边界与保证) · [版本约定](#版本约定)

## 提供方信息

| 项 | 值 |
| --- | --- |
| 插件 ID | `maibot-community.mai-life` |
| 协议名 | `maibot.schedule` |
| 协议版本 | `1` |
| 接口注册版本 | `"1"`（`@API` version，调用时传 `version="1"`） |
| 作用域 | `global`——麦麦只有一条全局生活时间线，不区分用户 |
| 时区 | 插件配置的 IANA 时区（默认 `Asia/Shanghai`），所有接口的返回与解释都基于它 |
| 能力 | `["snapshot", "timeline"]` |
| 最大查询跨度 | 72 小时 |

以上信息可通过 [`get_schedule_provider_info`](#提供方信息接口) 在运行时读取。

## 调用方式

调用方 manifest 需声明：

```json
{"capabilities": ["api.call"]}
```

三个接口都是公开 API（`public=True`），经 SDK 的 `ctx.api.call` 按名调用，参数作为关键字参数透传：

```python
async def read_schedule(ctx):
    response = await ctx.api.call(
        "maibot-community.mai-life.get_schedule_snapshot",
        version="1",
        future_limit=3,
    )
    if response["success"] is not True:
        raise RuntimeError(response["error"])   # RPC 层失败（接口不存在/版本不符等）
    envelope = response["result"]               # 业务信封，见下一节
    if envelope["success"]:
        current = envelope["data"]["current"]   # 当前活动（可能为 null）
```

`response["success"]` 是 Host 的 RPC 层结果；业务结果在 `response["result"]` 里，是下述信封。业务失败不会抛异常，一律走信封返回——不要用 try/except 判断业务错误。

## 返回信封

三个接口统一返回：

```json
{"success": true, "error": null, "data": { ... }}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `success` | bool | 业务是否成功 |
| `error` | string \| null | 失败原因（人读中文短句）；成功时为 `null` |
| `data` | object \| null | 业务数据；失败时为 `null` |

## get_schedule_provider_info 提供方信息

**用途**：识别提供方、协商协议版本。无参数；不依赖日程服务就绪（插件刚启动也能答）。

```python
info = await ctx.api.call("maibot-community.mai-life.get_schedule_provider_info", version="1")
```

`data` 结构：

```json
{
  "provider_id": "maibot-community.mai-life",
  "protocol": "maibot.schedule",
  "protocol_version": 1,
  "api_version": "1",
  "scope": "global",
  "timezone": "Asia/Shanghai",
  "capabilities": ["snapshot", "timeline"],
  "max_query_hours": 72
}
```

接入方应核对 `protocol` 与 `protocol_version`；不认识就按未知提供方处理，不要猜字段。

## get_schedule_snapshot 当前快照

**用途**：一次获取麦麦当前在做什么、当前场景和接下来的安排。只读，不生成日程。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `future_limit` | int | `3` | `next_activities` 最多返回几条；接受 0~20，越界钳到边界，非法值回退默认 3 |

`data` 结构（示例：10-25 10:00，正在图书馆自习）：

```json
{
  "timezone": "Asia/Shanghai",
  "as_of": "2026-10-25T10:00:00+08:00",
  "revision": "9f3ab02c1d8e4f5a",
  "schedule_available": true,
  "current": {
    "id": "a1", "kind": "study", "name": "图书馆自习", "description": "",
    "start_at": "2026-10-25T09:00:00+08:00", "end_at": "2026-10-25T11:00:00+08:00",
    "location": "图书馆", "scene": "坐在窗边整理今天的笔记"
  },
  "next_activities": [
    {
      "id": "a2", "kind": "meal", "name": "午饭", "description": "",
      "start_at": "2026-10-25T11:00:00+08:00", "end_at": "2026-10-25T12:00:00+08:00",
      "location": "食堂", "scene": ""
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `as_of` | 快照时刻（配置时区，ISO 8601 带偏移） |
| `revision` | 当日框架内容指纹；内容不变则不变，可用作刷新判断 |
| `schedule_available` | 当日框架是否非空。`false` 时 `current=null`、`next_activities=[]`、`revision=""`——通常出现在刚启动、日程尚未生成时，是诚实状态而非错误 |
| `current` | 当前时刻所在活动；两段之间的空档期为 `null` |
| `next_activities` | 当前段结束之后的安排，按 `start_at` 升序；无 `current` 时从当前时刻之后的段起算 |

## get_schedule_timeline 时间线查询

**用途**：查询指定时间范围内**已保存**的活动——日程查询与离线回放。只返回实际存在的日程：不生成新日程、不用模板补齐缺失的历史。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `start_at` | string | 是 | ISO 8601；不带时区按提供方时区解释，带时区则归一到提供方时区 |
| `end_at` | string | 是 | 同上；开区间（`end_at` 当刻不算入）。`start_at` 必须早于 `end_at`，跨度 ≤72 小时 |

`data` 结构：

```json
{
  "timezone": "Asia/Shanghai",
  "activities": [ /* 活动结构，按 start_at 升序 */ ],
  "missing_days": ["2026-10-21"]
}
```

行为细节：

- **交集判定**：活动与 `[start_at, end_at)` 有重叠即返回，**保留原始起止时间**——跨午夜段（如 22:00-24:00）的 `end_at` 自然落在次日 00:00，方便识别与去重。
- **缺失日**：范围内「当天框架为空」的日期记入 `missing_days`（按提供方时区的日历日）。有框架但无交集活动的日期**不算**缺失。
- **过去可查**：日程至少保留过去 72 小时（含已结束活动）；更早的日期同样可能查到，但不在承诺内。
- **无副作用**：查询不会触发日程生成；缺失的日子查完仍然缺失。

## 活动结构

快照的 `current`/`next_activities` 与时间线的 `activities` 共用同一结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 稳定活动 ID：内容哈希（日期+起止+类型+摘要），同一内容重复生成不变，跨天唯一，可作去重键 |
| `kind` | string | 段型枚举：`sleep`/`nap`/`rest`/`study`/`work`/`leisure`/`meal`/`travel` |
| `name` | string | 活动名（日程摘要原文，中文） |
| `description` | string | 恒为空串——当前无独立数据源，占位保持结构稳定 |
| `start_at` | string | 开始时刻（该活动所属日历日 + 段内分钟，配置时区，ISO 8601 带偏移） |
| `end_at` | string | 结束时刻；跨午夜段的结束落在次日 00:00 |
| `location` | string | 地点（可空串） |
| `scene` | string | 细化场景文本。**只有被细化过的节点有场景**（通常是临近/进行中的段），过去的段大概率是空串——调用方必须容忍空场景 |

## 错误一览

业务错误全部走信封（`success=false` + `error` 短句 + `data=null`），不抛异常：

| error 文案 | 出现条件 |
| --- | --- |
| `日程服务尚未初始化` | 插件服务未就绪（快照/时间线） |
| `start_at/end_at 必须是 ISO 8601 时间` | 参数缺失或解析失败 |
| `start_at 必须早于 end_at` | 起止相同或倒序 |
| `查询跨度超过 max_query_hours=72 小时` | 范围超限（上限以 provider_info 为准） |

`future_limit` 非法不会报错，按默认 3 处理。

## 边界与保证

- **只读保证**：三个接口都不写库、不调模型、不生成日程；可放心高频调用。
- **时区单一**：所有时刻字段都是配置时区的 aware ISO 8601（`+08:00` 形式）。时区取自插件配置 `environment.timezone`，修改配置后即刻生效。
- **当日重生成**：同一天框架被重写（如热更新强制重生成）时活动 id 可能变化、场景清空——以 `revision` 变化为准做增量刷新，不要假设 id 永生不变。
- **schedule_available=false 不重试生成**：快照只报告现状；想要日程生成是麦生活自己的后台循环的事，与查询无关。
- **帧与并发**：快照单次读取内 `current`/`next_activities` 来自同一份框架，帧内一致；连续两次调用之间可能跨过重生成。

## 版本约定

- 接口注册版本（`@API` 的 `version="1"`）与协议版本（`protocol_version=1`）成对出现，当前都是 1。
- 不兼容变更（字段删除/语义变化）会发布新版本号；旧版本在过渡期内保留。
- 新增**可选**字段不算不兼容变更——调用方应忽略未认识的字段。

相关文档：[调研与设计报告](_plan_schedule_provider.md) · [插件完全文档](plugin-guide.md)。
