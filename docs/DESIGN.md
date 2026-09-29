# 课程设计树（终稿 · 已确认）

> 本文件是《FastAPI + Vue3 全栈 LLM 实战（18 阶段）》的权威设计文档。
> 由 `grill-me` → `grilling` 拷问式访谈生成，所有分支均已与用户确认。

## 〇、总体定位
- **课程名**：FastAPI + Vue3 全栈 LLM 实战（18 阶段）
- **形态**：面向 0 基础的「全栈 AI 实战」课程
- **受众**：有通用编程基础，但零 AI/LLM、零全栈经验
- **主线产品**：全栈 AI 研究助手（mini Perplexity / Manus，阶段 06 起逐步加能力）

## 一、交付与仓库
- **交付形态**：图文教程 + 完整可运行代码库 + VitePress 在线文档站；**不含视频**
- **文档站**：VitePress，仓库内 `docs/`
- **结构**：混合 —— 阶段 01–05 独立练习（能力积木），阶段 06–18 并入主线大项目
- **仓库**：单一 monorepo（即本仓库根），根目录 `backend/` + `frontend/` + `docs/`
- **版本快照**：git 分支 `stage-00 … stage-18` 各存一阶段；`git diff stage-N stage-N+1` 即「这阶段加了什么」
- **阶段 01–05** 独立练习置于 `examples/` 子目录
  - **⚠️ 已修正的决策（阶段 07 动笔时确认）**：实施阶段 01–05 时，独立练习最终**全部写在主目录**（`backend/` + `frontend/`），未落地到 `examples/`。
    理由：① 五个阶段的练习**共用同一套后端与前端骨架**（`llm.py` / `schemas.py` / `main.py` / `App.vue` 逐阶段累加），拆成自包含目录会大量重复代码，反而看不清"这阶段加了什么"；
    ② 教程正文引用的路径统一为 `backend/xxx.py`，迁移会让已发布的 01–05 教程与 `stage-01…05` 分支快照全部失配；
    ③ `git diff stage-N stage-N+1` 的"阶段增量"叙事在单一目录下最清晰。
    因此 `examples/` 保留为**空壳 + README 说明**，指向主目录中的对应实现。此后新增阶段一律直接进主目录。

## 二、技术栈基线
| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+，`uv` 管依赖，FastAPI（最新稳定） |
| LLM 客户端 | 自封装轻量 client（基于 OpenAI SDK，切 `base_url`/`key`） |
| 默认模型 | **DeepSeek-Flash**（阶段 00–17 统一使用） |
| 阶段 18 | Computer Use —— 实做改走 **方案 C · DeepSeek 视觉仿制**（变更理由见下） |
| 前端 | Vite + Vue3 + Pinia + Element Plus |
| 流式 | 前端原生 `fetch` + `ReadableStream` 解析 SSE |
| RAG | ChromaDB（本地向量库）+ 本地嵌入（bge / Ollama embeddings） |
| 持久化 | SQLite，按会话 id 存对话历史；**无登录鉴权** |
| 前后端协作 | 阶段 01–02 前端直连 LLM 讲原理；阶段 03 起切 FastAPI 代理 + SSE |

> 具体补丁版本（FastAPI / Vue3 / Element Plus / VitePress / ChromaDB / DeepSeek 模型 id）以**动笔时最新稳定版**为准，不硬编码。

> **⚠️ 变更记录（阶段 18 动笔时改定；同日追加 18B，把方案 A 补回为可选）**：
> 本节原定「Computer Use 需 Claude 能力，保留 Claude 为唯一例外（方案 A）」。
> 实做前重新核查后改为 **方案 C · DeepSeek 视觉仿制**，理由：
> 1. `.env` 里 `CLAUDE_API_KEY` **为空**，且 `CLAUDE_BASE_URL` 指向 Anthropic **原生**端点
>    （`/v1/messages` + `anthropic-beta` 头 + `x-api-key`），而本项目的 `llm.py` 只会说 OpenAI 协议 ——
>    走 Claude 等于新增一个 SDK、一个付费 Key、一套 Docker 沙箱；
> 2. Computer Use 的闭环里**只有「看屏幕并给出动作」这一步**绑定模型厂商，
>    截图 / 执行 / 回灌三步本课已在阶段 05、10、16 建好；
> 3. 第 2 步用阶段 16 已验证过的 DeepSeek 视觉即可接上 —— 零新依赖、零新密钥。
>    **代价是坐标精度**，而精度差恰好是本阶段最好的教材（详见 [`stages/18-ComputerUse.md`](./stages/18-ComputerUse.md) §5）；
> 4. 「Claude 是唯一例外」在 2026 年已不准确（OpenAI 的 Responses API `computer` 工具、Gemini 的 `computer_use` 都可用），
>    但对本项目结论不变：它们各自都不在 OpenAI Chat Completions 的形状里。

> **✅ 追加（阶段 18B）**：上面第 1 条把「`CLAUDE_API_KEY` 为空」当成了**放弃方案 A 的理由**，
> 但那是**本机当下的状态**，不是学习者必然的状态 —— 按原计划「拿到 Claude 就演示」更合本意。
> 于是 18B 把方案 A **按原计划补回，但作为可选 provider**：
> - 默认仍是 **方案 C**（零密钥、零依赖、开箱即跑），`provider=auto` 时若有 Claude Key 则自动选用 A；
> - 实现**没有引入 `anthropic` SDK**：直接用已有的 `httpx` 打 `POST /v1/messages`，
>   带 `anthropic-beta: computer-use-2025-01-24` 与内置工具 `computer_20250124`；
> - 因此「走 Claude 要新增一个 SDK」这条理由**被证伪**了，剩下两条（付费 Key、Docker 沙箱）依然成立；
> - 沙箱这条本阶段**天然满足**：被操作的「电脑」是我们自己渲染的一屏虚拟桌面，
>   所有动作只改内存状态，碰不到真实文件系统 / 鼠标 / 键盘 / 网络。
>
> 关键结构：闭环里唯一绑定厂商的第②步被抽成**可插拔大脑**（`backend/brains.py`），
> 主循环、动作白名单、坐标钳制、危险动作闸门、评分逻辑**一行未改** —— 这正是本条注记
> 「只有一步是厂商卖的」从论断变成可执行代码的证据。详见
> [`stages/18B-ComputerUseClaude.md`](./stages/18B-ComputerUseClaude.md)。
>
> ⚠️ 诚实声明：`docs.anthropic.com` 在当前网络**被地区屏蔽**（跳转 `app-unavailable-in-region`），
> 线格式是依据二手资料核实的；且**无 Key 无法真机验证 Claude 路径**。18B 用**本地 mock 服务**
> 断言我们发出的字节（请求头 / 工具定义 / 消息形状 / 图放在 `tool_result` 里），
> 但**未验证真实 Anthropic 服务的响应**——这一点在文档 §5 ④ 明确标注为「未实测」。

> **安全红线不变**：真实 Computer Use 必须跑在沙箱里（官方要求 Docker/VM）。本阶段把「被操作的电脑」
> 做成一屏自渲染的虚拟桌面，所有动作只改内存状态，碰不到真实文件系统/鼠标/网络，危险动作默认被拒。

## 三、阶段映射总表
| 阶段 | 内容 | 形态 |
|---|---|---|
| 00 | 环境与工具准备（装 uv/Python/Node/pnpm、申请 DeepSeek Key、跑通最小调用、VitePress 预览） | 前置章 |
| 01 | 大模型 API 编程基础 | 独立练习 |
| 02 | Prompt 工程（模板实验台） | 独立练习 |
| 03 | 多轮对话与流式 | 独立练习 |
| 04 | 结构化输出与信息抽取 | 独立练习 |
| 05 | 函数调用与工具集成 | 独立练习 |
| 06 | MCP 协议开发 | 主线 |
| 07 | RAG 基础（向量检索） | 主线 |
| 08 | RAG 进阶（混合检索/重排） | 主线 |
| 09 | GraphRAG 知识图谱 | 主线 |
| 10 | 单智能体（ReAct） | 主线 |
| 11 | 多智能体 | 主线 |
| 12 | Agent Harness 框架 | 主线 |
| 13 | Agentic RAG | 主线 |
| 14 | AI 搜索应用 | 主线 |
| 15 | Deep Research | 主线 |
| 16 | 多模态·图像 | 主线（能力插件） |
| 17 | 多模态·语音 | 主线（能力插件） |
| 18 | Computer Use（默认：DeepSeek 视觉 + 自渲染虚拟屏幕；可选 Claude 原生 Computer Use） | 主线 |

## 四、单阶段模板（7 段，统一）
1. 阶段目标
2. 前置知识/环境
3. 核心概念
4. 动手实现（后端+前端分步）
5. 运行验证
6. 小结
7. 练习与验收（每阶段 1 道改造/扩展题 + 验收标准；15/18 加调试题）

## 五、进阶篇（不占 01–18 编号）
1. 部署（Docker + 基础云）
2. 登录鉴权 JWT（呼应「无登录」主线决策）
3. 多模型接入 litellm（呼应「自封装 client」主线决策）
4. 测试与 CI

## 六、篇幅基线
每阶段 3000–5000 字 + 完整代码 + 1–2 练习；约 1–2 小时/阶段；全课约 30–60 小时。

## 七、前端设计规范（frontend-design + vue-best-practices）
- **页面设计**遵循 `frontend-design`：先做设计计划（调色板/字体/布局/签名元素），杜绝模板化 AI 审美，把视觉大胆集中在唯一「签名元素」。
- **前端开发**遵循 `vue-best-practices`：Vue 3 + Composition API + `<script setup lang="ts">`；组件小而聚焦；状态/副作用抽 composables；props down / events up。
- 完整设计语言（调色板、字体、Stage Rail 布局、流式光标签名、开发约束、反模式）见 **[设计系统文档](./design-system.md)**。
- 注：`vue-best-practices` 技能未在本机注册表，已直接读取其 `SKILL.md` 并纳入；构建前端前须读其 `references/`（reactivity / sfc / component-data-flow / composables）。
