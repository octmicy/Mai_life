# 发现记录

- 项目：https://github.com/octmicy/Mai_life，基线版本 v1.9.2。
- 插件功能：生活状态、日程、消息管线、联网见闻、书柜创作、社交转述、管理指令与公共 API。
- 当前联网搜索：博查/Tavily/You.com/OpenAI Responses/OpenAI Chat API 链，带 Key 轮换、冷却、隐私清洗与 SQLite 统计。
- 基线测试命令需将插件父目录放入 PYTHONPATH，并使用 OneKey 的 python-env；2026-08-14 结果 175/175 通过。
- 本机系统 Python 已有 Playwright 1.62.0；运行环境 python-env 需要后续安装。
- 插件规范要求独立仓库，不应修改 MaiBot 主程序。
