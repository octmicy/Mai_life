# Mai_life Playwright 搜索重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Mai_life 联网搜索重构为清晰模块，并新增默认 Playwright/Bing 免 Key 搜索，同时保留 API 备援。

**Architecture:** `search_models.py` 定义共享模型和异常，`search_parsing.py` 承接既有 API 协议解析，`playwright_search.py` 管理浏览器生命周期和结果页解析，`search_service.py` 统一处理顺序、健康、冷却和统计。公开 `SearchService.search()` 签名不变，上层业务无需重写。

**Tech Stack:** Python 3.10+、pydantic PluginConfigBase、Playwright Async Chromium、unittest/isolated asyncio、SQLite Schema v9。

## Global Constraints

- 首选语言和所有新增 UI/注释/文档文案为简体中文。
- 插件 SDK 最低 2.7.0，MaiBot 最低 1.0.12。
- 不修改 MaiBot 主程序、不修改根目录 `.gitignore`、不新增 ConfigUpgradeHook。
- SQLite Schema 保持 v9。
- 不使用 `getattr`/`setattr` 掩盖类型错误；错误必须显式分类并记录。
- 每个生产行为先写失败测试，再实现。
- 最终验证命令：`python -m unittest discover -s tests -v` 与 `python -m compileall .`。

---

### Task 1: 搜索模型与 HTML 解析器

**Files:**
- Create: `information/search_models.py`
- Create: `information/search_parsing.py`
- Create: `information/playwright_search.py`
- Modify: `information/search_service.py`
- Test: `tests/test_playwright_search.py`

**Interfaces:**
- Produces: `SearchResult(title: str, url: str, snippet: str, provider_generated: bool=False)`。
- Produces: `SearchResponse(results: list[SearchResult], provider_id: str="", provider_type: str="", generated_text: str="", cited: bool=False, model: str="", prompt_tokens: int=0, completion_tokens: int=0, total_tokens: int=0)`。
- Produces: `SearchBackendError(message: str, *, error_class: str)`。
- Produces: `BingSearchParser.parse(html: str, limit: int) -> list[SearchResult]`。
- Produces: `DuckDuckGoSearchParser.parse(html: str, limit: int) -> list[SearchResult]`。

- [ ] **Step 1: 写 Bing 解析失败测试**

```python
def test_bing_parser_extracts_organic_redirect_and_snippet(self):
    parser = BingSearchParser()
    html = Path("tests/fixtures/bing_search.html").read_text(encoding="utf-8")
    results = parser.parse(html, 5)
    self.assertEqual(results[0].title, "人工智能 - Microsoft")
    self.assertEqual(results[0].url, "https://www.microsoft.com/ai")
    self.assertIn("人工智能", results[0].snippet)
    self.assertTrue(all("bing.com/ck/a" not in item.url for item in results))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_playwright_search -v`
Expected: `ImportError: cannot import name 'BingSearchParser'`

- [ ] **Step 3: 实现模型与解析器**

用标准库 `html.parser.HTMLParser` 分别提取 Bing `li.b_algo/h2/a/p` 与 DuckDuckGo `.result__a/.result__snippet`；Bing URL 先解码 HTML entity，再解析 `u=a1<Base64URL>`。过滤空标题、空链接和广告节点。

- [ ] **Step 4: 写 DuckDuckGo、截断和无效结果测试并确认先失败后通过**

覆盖 DDG 标题/摘要/链接、`limit=1` 截断、缺失摘要仍保留结果、非法 URL 被过滤。

- [ ] **Step 5: 运行信息搜索相关测试**

Run: `python -m unittest tests.test_playwright_search tests.test_information -v`
Expected: 新解析测试通过，既有测试暂不回归。

- [ ] **Step 6: Commit**

```bash
git add information/search_models.py information/search_parsing.py information/playwright_search.py tests/test_playwright_search.py tests/fixtures
git commit -m "refactor: extract search models and browser result parsers"
```

---

### Task 2: Playwright 搜索客户端

**Files:**
- Modify: `information/playwright_search.py`
- Test: `tests/test_playwright_search.py`

**Interfaces:**
- Produces: `PlaywrightSearchClient(logger: Any, *, headless: bool=True)`。
- Produces: `await client.search(query: str, *, engine: str, freshness: str, timeout_seconds: float, max_results: int) -> SearchResponse`。
- Produces: `await client.close() -> None`。
- Produces: `PlaywrightSearchClient.search_url(engine: str, query: str, freshness: str) -> str`。

- [ ] **Step 1: 写 URL 与 freshness 失败测试**

```python
def test_search_url_encodes_query_and_day_filter(self):
    url = PlaywrightSearchClient.search_url("bing", "人工智能 新闻", "day")
    self.assertIn("q=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+%E6%96%B0%E9%97%BB", url)
    self.assertIn("filters=", url)
```

- [ ] **Step 2: 运行确认失败后实现纯 URL 逻辑**

Bing 映射：`day -> ex1:"ez5_19700_19701"`、`week -> ex1:"ez5_19700_19702"`、`month -> ex1:"ez5_19700_19704"`、`year -> ex1:"ez5_19700_19705"`；DuckDuckGo 使用 `df=d/w/m/y`。实现后用浏览器实际页面确认参数不会导致解析误判。

- [ ] **Step 3: 写假 Playwright 页面行为测试**

Fake page 记录 `goto` URL、返回 fixture HTML，并在 `close=True` 时抛出超时。断言成功结果、空结果分类、导航超时分类和 `close()` 释放 browser/context。

- [ ] **Step 4: 实现延迟导入、锁、浏览器/context 生命周期和错误分类**

缺失 Playwright 抛 `SearchBackendError(error_class="playwright_unavailable")`；导航超时为 `network`；无结果为 `empty_result`；页面被阻断为 `blocked`。

- [ ] **Step 5: 运行测试并提交**

Run: `python -m unittest tests.test_playwright_search -v`
Expected: 全部通过。

```bash
git add information/playwright_search.py tests/test_playwright_search.py
git commit -m "feat: add Playwright web search client"
```

---

### Task 3: 配置与 SearchService 集成

**Files:**
- Modify: `config.py`
- Modify: `config.toml`
- Modify: `information/search_service.py`
- Test: `tests/test_playwright_search.py`
- Test: `tests/test_information.py`
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `await playwright_client.search(query, engine=engine, freshness=freshness, timeout_seconds=timeout, max_results=limit)`。
- Produces: `SearchProviderProfile.provider_type` 支持 `"playwright"`。
- Produces: `SearchProviderProfile.browser_engine` 支持 `"bing"` / `"duckduckgo"`。
- Produces: `SearchProviderProfile.headless: bool=True`。
- Produces: Playwright 提供方稳定 `key_fingerprint="browser"`。

- [ ] **Step 1: 写配置失败测试**

```python
def test_default_search_chain_prefers_playwright_without_key(self):
    config = MaiLifeSettings()
    profile = config.search_api.providers[0]
    self.assertTrue(profile.enabled)
    self.assertEqual(profile.provider_type, "playwright")
    self.assertEqual(profile.browser_engine, "bing")
    self.assertEqual(profile.api_keys, [])
```

再增加 Playwright 提供方配置 `api_keys=["x"]` 时抛出 `ValidationError` 的测试。

- [ ] **Step 2: 确认失败后实现配置**

版本升到 `1.10.0`；默认 provider 为启用 Playwright/Bing/headless；TOML 模板同步；保持 `information.enabled=false`。

- [ ] **Step 3: 写服务集成失败测试**

用 Fake Playwright client 注入 `SearchService`，断言无 Key 也会搜索、provider_type 为 `playwright`、事件指纹为 `browser`、健康快照包含 engine/headless；Fake 抛 `SearchBackendError("network")` 后继续调用下一个博查 provider。

- [ ] **Step 4: 重构 SearchService**

抽出 API 解析函数到 `search_parsing.py`；保留公开方法与 API 冷却行为。Playwright 分支不遍历 API Key，使用伪指纹 `browser`，一次浏览器失败只消耗一次尝试并进入下一个 provider。配置更新时关闭旧浏览器客户端。

- [ ] **Step 5: 运行完整单元测试**

Run: `python -m unittest discover -s tests -v`
Expected: 全部通过。

- [ ] **Step 6: Commit**

```bash
git add config.py config.toml information/search_service.py information/search_parsing.py tests
git commit -m "feat: integrate Playwright into search provider chain"
```

---

### Task 4: 依赖、文档、版本与真实验证

**Files:**
- Modify: `requirements.txt`
- Modify: `_manifest.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `mai_template.json`（仅当其显示插件版本）

**Interfaces:**
- Produces: 插件版本 `1.10.0`。
- Produces: `playwright>=1.49,<2` 依赖声明。
- Produces: 部署说明中的 `python -m playwright install chromium`。

- [ ] **Step 1: 写版本与依赖契约失败测试**

在 `tests/test_contract.py` 增加断言：manifest/config/模板版本为 `1.10.0`，requirements 包含 Playwright，README 包含浏览器安装命令和 API 备援说明。

- [ ] **Step 2: 更新依赖和文档**

README 明确默认免费搜索是 Playwright/Bing，API 是可选备援；说明验证码、搜索页变化和浏览器缺失会在 `/麦麦管理 来源` 中显示分类错误。CHANGELOG 按“主要功能/细节”记录。

- [ ] **Step 3: 完整验证**

```powershell
python -m playwright install chromium
python -m unittest discover -s tests -v
python -m compileall .
python - <<'PY'
import asyncio
from Mai_life.information.playwright_search import PlaywrightSearchClient
async def main():
    client = PlaywrightSearchClient(None)
    try:
        response = await client.search("MaiBot 插件", engine="bing", freshness="any", timeout_seconds=20, max_results=3)
        print(response.provider_type, len(response.results), [item.title for item in response.results])
    finally:
        await client.close()
asyncio.run(main())
PY
```

Expected: 175+ 全部测试通过；compileall 无错误；真实搜索返回至少 1 条 Bing 自然结果。

- [ ] **Step 4: 最终提交**

```bash
git add requirements.txt _manifest.json README.md CHANGELOG.md mai_template.json tests
git commit -m "release: add Playwright-first web search in 1.10.0"
```
