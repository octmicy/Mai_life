# Mai_life Playwright 联网搜索重构设计

## 背景

Mai_life v1.9.2 的新闻阅读、主动探索和 `mai_life_web_search` Tool 共用 `information/search_service.py` 中的 API 搜索链。当前实现把结果模型、协议解析、Key 轮换、冷却、降级和数据库统计耦合在一个服务类中，支持博查、Tavily、You.com 和 OpenAI 兼容服务。全部 175 个现有测试在基线提交 `838e92b` 中通过。

## 目标

1. 将联网搜索的实现重构为清晰的模型、解析、浏览器搜索和服务编排边界。
2. 新增 Playwright 浏览器搜索提供方，默认使用 Bing，不要求 API Key。
3. 保留既有 API 提供方作为可选备用，避免搜索页结构变化或浏览器环境缺失时让已有用户立即失去可用配置。
4. 复用现有隐私清洗、每日额度、外部请求上限、搜索事件统计和健康状态。
5. 不修改无关生活、消息、创作、社交模块，不改变 SQLite Schema v9。
6. 全部测试通过，并用真实 Chromium 搜索验证 Playwright 链路。

## 方案对比

- **完全移除 API，仅保留 Playwright**：实现最直观，但搜索页反爬、浏览器缺失和网络区域问题会直接造成功能不可用，也无法保留已付费服务的用户配置。
- **Playwright 优先，API 保留为备用（采用）**：默认新增免 Key 的 Bing 浏览器搜索，用户仍可按列表顺序加入 API 失败备援。迁移风险低，符合渐进式重构。
- **仅用 Playwright 抓正文，搜索仍走 API**：改动小，但没有满足“联网搜索用 Playwright”的核心目标。

## 目标结构

```text
information/
  search_models.py        # SearchResult、SearchResponse、搜索后端异常
  search_parsing.py       # API JSON 解析、结果清洗、Key 回显清除
  playwright_search.py    # Playwright 浏览器生命周期、Bing/DDG HTML 解析
  search_service.py       # 提供方顺序、Key/浏览器健康、冷却、事件统计
```

`SearchService.search(query, operation, freshness, event_at)` 的公开签名保持不变，`InformationService`、`NewsService` 和 `plugin.py` 的调用方不改接口。

## 配置设计

- `SearchProviderProfile.provider_type` 新增 `playwright`。
- 新增 `browser_engine: Literal["bing", "duckduckgo"]`，默认 `bing`。
- 新增 `headless: bool`，默认 `true`，用于排查验证码或页面结构问题。
- `SearchAPISettings.providers` 默认包含一个启用的 `playwright + bing` 项；`information.enabled` 仍默认关闭，因此不会默认联网。
- `api_keys` 对 Playwright 提供方必须为空，非空配置直接校验失败，避免误导用户。
- 搜索引擎固定为枚举，不允许自定义 URL，避免把 Playwright 变成任意内网地址访问器。
- 配置版本、插件版本和说明升级到 `1.10.0`。

## Playwright 行为

- 只在执行 Playwright 搜索时延迟导入 `playwright`，缺失依赖时返回明确的 `playwright_unavailable` 错误并记录服务健康状态，不隐藏异常。
- 使用 Chromium、独立 browser context、简体中文 locale、固定视口和可配置 headless 模式。
- 浏览器生命周期由 `PlaywrightSearchClient` 管理，多次搜索复用 context，服务关闭或配置更新时释放。
- Bing 使用 `https://www.bing.com/search?q=...`，解析 `li.b_algo` 的标题、真实 URL 和摘要；Bing 重定向链接按 `u=a1<Base64URL>` 解码。
- DuckDuckGo 使用 HTML 结果页，解析结果标题、链接和摘要，作为 Bing 结构变化时的可选浏览器备援。
- `freshness` 映射为搜索引擎日期参数；若搜索引擎无可靠参数，则在明确测试和文档中暴露限制，不伪造结果时效。
- 空结果、验证码/阻断、导航超时和浏览器缺失分类记录；浏览器异常会让同一逻辑搜索继续尝试下一个已配置提供方，但不假装成功。

## 健康与统计

- Playwright 无 API Key，在既有 `search_key_runtime` 中使用稳定指纹 `browser`，不新增数据库字段。
- `search_api_events.provider_type` 记录 `playwright`，`key_fingerprint` 记录 `browser`。
- 健康快照显示 engine、headless、启用状态和最近错误，不暴露查询内容。
- 既有 API 提供方的鉴权、限流、额度、网络错误和退避逻辑保持不变。

## 测试策略

- 为 Bing/DuckDuckGo HTML 解析准备离线 fixture，覆盖标题、摘要、重定向解码、广告/无效项过滤和数量截断。
- 用假的 Playwright 页面对象测试浏览器搜索 URL、freshness 参数、超时、错误分类和资源释放。
- 用假搜索客户端测试 `SearchService` 的无 Key 播放、事件统计、健康快照和 API 备援。
- 保留并调整现有 175 个测试，确保 API 行为不回退。
- 最后运行完整测试、编译检查，并执行一次真实 Chromium 搜索作为集成验证。

## 文档与交付

- 更新 README 的联网搜索、依赖安装、浏览器安装和故障排查说明。
- `requirements.txt` 加入 `playwright>=1.49,<2`，浏览器二进制仍由部署者执行 `python -m playwright install chromium`。
- `CHANGELOG.md` 增加 1.10.0 主要功能与细节。
- 插件独立仓库内提交基线、设计、实现和验证结果，不改动主程序根目录 `.gitignore`。
