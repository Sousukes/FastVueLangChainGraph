# 前端设计系统（课程 UI 规范）

> 本文件是《FastAPI + Vue3 全栈 LLM 实战》全部前端（主线 AI 研究助手 + 各阶段练习 UI）的**统一设计语言**。
> 设计美学遵循 `frontend-design`；开发实现遵循 `vue-best-practices`。

## 1. 设计原则（frontend-design）
- **不套模板**：规避三种 AI 默认风——米色衬线+陶土色 / 纯黑+荧光绿 / 报纸细线排版。
- **每个选择都有理由**，且服务于「全栈 AI 研究助手」这一主题。
- **把大胆用在一个地方**（signature），其余克制、安静。

## 2. 主题锚点（grounding）
- **主体**：面向 0 基础中文学习者的「全栈 AI 研究助手」教程与产品。
- **一句话定位**：像在**实验室工作台**做研究——精确、有结构、每一步可追溯。
- **视觉隐喻**：蓝图 / 实验控制台（schematic + console），而非消费级聊天 App。

## 3. 调色板（4–6 命名 hex）
| 名称 | Hex | 用途 |
|---|---|---|
| Ink | `#0E1116` | 背景，带蓝调的墨黑（非纯黑） |
| Panel | `#161B22` | 卡片 / 面板 |
| Paper | `#E6E9EF` | 正文文字 |
| Muted | `#8B95A7` | 次要文字 / 边框 |
| Signal | `#FFB454` | 琥珀高亮（类荧光笔标注）——**唯一主强调色**，非荧光绿 / 紫蓝渐变 |
| Data | `#5BC8B0` | 青绿，用于链接 / 数据 / 流式光点（次级） |

## 4. 字体（≥2 角色）
- **显示 / 标题**：`Space Grotesk`（拉丁）+ `Noto Sans SC` Bold（中文）——几何感、有性格；**不用 Inter / Roboto / system-ui 当标题**。
- **正文**：`IBM Plex Sans` + `IBM Plex Sans SC`——技术气质，与代码族同源。
- **等宽 / 代码**：`JetBrains Mono`——代码块、token、阶段编号。

## 5. 布局概念
- **左轨「阶段时间线 Stage Rail」**：竖向 `00 → 18` 进度轨。编号在此是**真实序列**（课程阶段本就有强顺序），因此编号标记是合理的结构装置。
- **主区**：研究控制台（对话 / 检索 / 流式输出）。
- **顶栏**：课程名 + 当前阶段。
- **移动端**：Stage Rail 收为顶部横向步骤条。

```
┌─────────┬──────────────────────────────┐
│ 00 ●    │  顶栏：全栈 AI 研究助手 · 阶段 03 │
│ 01 ●    ├──────────────────────────────┤
│ 02 ●    │  研究控制台                    │
│ 03 ▶    │  ┌────────────────────────┐   │
│ 04 ○    │  │ 对话 / 检索 / 流式输出   │   │
│ 05 ○    │  └────────────────────────┘   │
│ ...     │  [输入框] [发送]              │
│ 18 ○    │                              │
└─────────┴──────────────────────────────┘
```

## 6. Signature（记忆点）
- **「流式光标」**：输出区以 `JetBrains Mono` 呈现闪烁 caret，呼应 LLM 流式生成；阶段完成时 caret 收为实心句点。
- 配合 Stage Rail 的进度脉冲，构成本课程**专属视觉锚**，一眼可辨。

## 7. 开发规范（vue-best-practices）
- 标准栈：**Vue 3 + Composition API + `<script setup lang="ts">`**。
- 组件小而聚焦；状态 / 副作用抽到 **composables**（`useChat.ts`、`useStream.ts` 等）。
- 数据流向：**props down / events up**；`v-model` 仅用于真正双向契约。
- 根 / 路由级组件保持"组合面"，特性逻辑移出。
- **构建前须读** vue-best-practices 的 `references/`：`reactivity.md`、`sfc.md`、`component-data-flow.md`、`composables.md`。
- 质量底线：响应式到移动端、键盘焦点可见、`prefers-reduced-motion` 尊重。

## 8. 反模式（Frontend Design 明令避免）
- Inter / Roboto / system-ui 当显示字体
- 白底紫蓝渐变（"科技创业"默认脸）
- 居中 hero + 圆角卡片（"SaaS 落地页"默认脸）
- 过度 box-shadow、满屏 `border-radius: 9999px` 胶囊
- Unsplash 占位图、Lorem ipsum
