# Mai_life 重构任务计划

## 目标

理解并重构 Mai_life，重点把联网搜索改为 Playwright 优先，保留 API 备援，并完成验证。

## 当前阶段

- [x] 获取并理解 GitHub 项目 v1.9.2。
- [x] 建立独立 Git 仓库并提交基线。
- [x] 运行基线测试：175 个全部通过。
- [x] 完成设计文档与实施计划。
- [x] Task 1：搜索模型与 Bing/DuckDuckGo 解析器。
- [x] Task 2：Playwright 搜索客户端。
- [x] Task 3：配置与 SearchService 集成。
- [x] Task 4：依赖、文档、版本与真实验证。

## 验收标准

1. 默认搜索链包含免 Key Playwright/Bing 提供方。
2. 现有 API 提供方仍可作为可选备援。
3. 全部单元测试与编译检查通过。
4. 真实 Chromium 搜索返回自然结果。
5. README、CHANGELOG、manifest、requirements 和配置模板同步。

## 完成记录

- Playwright Chromium 已完成真实 Bing 搜索验证并返回自然结果。
- 191 个单元测试全部通过，`compileall` 和 `git diff --check` 通过。
