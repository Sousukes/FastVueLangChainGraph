# examples/ — 阶段 01–05 独立练习（能力积木）

> **本目录是空壳，实际代码在主目录。**
>
> 设计文档原本规划阶段 01–05 的自包含练习放在这里，但实施时确认了更优方案：
> 五个阶段的练习**共用同一套后端与前端骨架**（`backend/llm.py`、`backend/schemas.py`、
> `backend/main.py`、`frontend/src/App.vue` 逐阶段累加），拆成自包含目录会大量重复代码，
> 且会让已发布的教程路径与 `stage-01…05` 分支快照全部失配。
>
> 完整理由见 `docs/DESIGN.md` 一·交付与仓库。

各阶段代码的实际位置：

| 阶段 | 主题 | 代码位置 |
|---|---|---|
| 01 | 大模型 API 编程基础 | `backend/llm.py`、`frontend/src/` |
| 02 | Prompt 工程 | `frontend/src/components/`（模板实验台） |
| 03 | 多轮对话与流式 | `backend/main.py`（`/api/chat`、`/api/chat/stream`）、`frontend/src/composables/useChatStream.ts` |
| 04 | 结构化输出与信息抽取 | `backend/extract.py`、`frontend/src/composables/useExtractor.ts` |
| 05 | 函数调用与工具集成 | `backend/tools.py`、`backend/agent.py`、`frontend/src/composables/useToolRunner.ts` |
| 06 | MCP 协议开发（主线起点） | `backend/mcpkit/`、`frontend/src/components/McpConsole.vue` |

每个阶段对应一个 git 分支快照（`stage-01 … stage-06`），
`git diff stage-N stage-N+1` 就是「这一阶段加了什么」。
