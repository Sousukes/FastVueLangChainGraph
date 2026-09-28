# FastAPI + Vue3 全栈 LLM 实战（18 阶段）

面向 **0 基础** 的「全栈 AI 实战」教程：用 **FastAPI + Vue3** 从零构建一个全栈 **AI 研究助手**，
覆盖大模型 API 编程、Prompt、流式、结构化输出、函数调用、MCP、RAG、GraphRAG、Agent、
Deep Research、多模态到 Computer Use，共 18 个阶段 + 进阶篇。

## 仓库结构（monorepo）

```
.
├── backend/          # FastAPI 后端（自封装 LLM client + 各阶段能力）
├── frontend/         # Vue3 + Vite + Pinia + Element Plus 前端
├── docs/             # VitePress 图文教程（本文件所在）
│   ├── .vitepress/
│   ├── DESIGN.md     # 课程设计树（权威设计文档）
│   ├── index.md
│   └── stages/       # 各阶段教程
└── examples/         # 阶段 01–05 独立练习（能力积木）
```

## 阶段分支策略

每个阶段是 git 上的一个独立分支 `stage-00 … stage-18`，保存该阶段的完整可运行快照：

```bash
git checkout stage-05   # 查看/运行第 05 阶段状态
git diff stage-05 stage-06   # 看清「这阶段加了什么」
```

## 本地运行

```bash
# 后端
cd backend && uv sync && uv run fastapi dev main.py

# 前端
cd frontend && pnpm install && pnpm dev

# 文档站
cd docs && pnpm install && pnpm docs:dev
```

> 详见 [阶段 00 · 环境准备](./docs/stages/00-环境准备)。
