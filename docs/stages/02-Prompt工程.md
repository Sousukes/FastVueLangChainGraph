# 阶段 02 · Prompt 工程（模板实验台）

> 独立练习（能力积木）｜预计 1–2 小时
> 配套代码：`frontend/src/composables/usePromptTemplate.ts`、`frontend/src/components/PromptLab.vue`

## 1. 阶段目标

- 建立核心认知：**Prompt 工程不发明新 API，它只研究"怎么把输入写对"**。
- 掌握三种最常用技法：角色设定（Role）、少样本（Few-shot）、结构化约束（Format）。
- 用 Vue3 搭一个「Prompt 模板实验台」：变量占位 → 实时渲染 → 一键发模型 → 对比效果。
- 学会把"调 Prompt"和"调代码"分离：模型不变，只改输入，看输出如何随之变化。

## 2. 前置知识 / 环境

| 项 | 要求 |
|---|---|
| 已完成 | 阶段 01（已理解 `/chat/completions` 调用与 `messages` 结构） |
| 运行环境 | Node 18+、pnpm（`cd frontend && pnpm install && pnpm dev`） |
| 密钥 | 一个有效的 `DeepSeek API Key` |
| 本阶段后端 | **不需要**。与阶段 01 一致，前端直连，只为看清 Prompt 本身。 |

## 3. 核心概念

### 3.1 Prompt 是什么

它就是你发给模型的 `user` / `system` 消息的**文本**。模型的能力是固定的，"会做什么"很大程度上取决于你"怎么问"。Prompt 工程即：在**不改动模型**的前提下，通过组织文字最大化模型产出符合预期结果的概率。

### 3.2 三种必会技法

1. **角色设定（Role）**：用 `system` 式口吻给模型一个人设与受众约束。
   > 你是一位资深的{{领域}}科普作者，面向{{读者}}。请用{{语气}}的口吻解释……
2. **少样本（Few-shot）**：给 1–2 个「输入→输出」样例，模型会照章办事，比纯文字描述更稳。
   > 示例1：{{示例输入1}} → {{示例输出1}} ／ 现在请处理：{{待处理}}
3. **结构化约束（Format）**：强制 JSON 等机器可读格式，抑制自由发挥。
   > 请整理为 JSON，字段为 title、points、summary。只输出 JSON。

### 3.3 模板 + 变量的价值

把"会变的部分"抽成 `{{变量}}`，模板就能复用：同一套结构，换变量值即可批量生成不同 Prompt，也便于做 A/B 对比——这正是实验台要解决的痛点。

## 4. 动手实现（前端分步）

### 4.1 模板引擎：`usePromptTemplate.ts`

纯函数 + 组合式，遵循 `vue-best-practices`：

```ts
// 提取 {{变量}}（去重）
export function extractVariables(template: string): string[] {
  const re = /\{\{\s*([\w一-龥]+)\s*\}\}/g
  const found = new Set<string>()
  let m: RegExpExecArray | null
  while ((m = re.exec(template))) found.add(m[1])
  return [...found]
}

// 渲染：未填值的变量保留占位，便于发现遗漏
export function renderTemplate(t: string, values: Record<string, string>): string {
  return t.replace(/\{\{\s*([\w一-龥]+)\s*\}\}/g, (_, n: string) =>
    values[n]?.trim() ? values[n] : `{{${n}}}`,
  )
}

export function usePromptTemplate(initial = '') {
  const template = ref(initial)
  const values = reactive<Record<string, string>>({})
  const variables = computed(() => extractVariables(template.value))
  const rendered = computed(() => renderTemplate(template.value, values))
  return { template, values, variables, rendered }
}
```

要点：`variables` 与 `rendered` 都是 `computed`，模板或变量一变即重算；`values` 用 `reactive` 按变量名存值，新增变量自动出现在面板。

### 4.2 实验台 UI：`PromptLab.vue`

- **预设技法**（`PRESETS`）：角色设定 / 少样本 / 结构化约束，一键切换模板。
- **模板编辑**：`<el-input type="textarea">` 编辑含 `{{变量}}` 的模板（等宽字体）。
- **变量填充**：`variables` 驱动动态生成输入框；`unfilled` 实时提示漏填项。
- **渲染预览**：只读 `<pre>` 显示 `rendered`，未填变量以 `{{name}}` 高亮提示。
- **发送**：复用阶段 01 的 `useDeepSeek.send({ user: rendered })`——证明"换 Prompt 不改调用代码"。
- **输出控制台**：沿用阶段 01 的流式光标签名（生成中闪烁、完成收实心句点）。

### 4.3 接入主壳：`App.vue`

将 `CURRENT_STAGE = 2`、标题改「Prompt 工程」，并把控制台组件由 `ChatPanel` 换成 `PromptLab`。Stage Rail / AppHeader 不动——主壳在各阶段复用。

## 5. 运行验证

```bash
cd frontend && pnpm dev   # http://localhost:5173
```

1. 进度轨高亮 `02 ▶`，主区显示"Prompt 模板实验台"。
2. 点击「角色设定」预设，填入 领域/读者/语气/主题，看右侧预览实时更新。
3. 粘贴 Key →「用此 Prompt 发送」→ 输出区出现模型回答（青绿句点收尾）。
4. 切到「结构化约束」预设，填主题+内容，验证返回是否为合法 JSON。

**验证清单**
- [ ] 模板改一个字，预览立即跟着变。
- [ ] 漏填变量时，预览保留 `{{变量}}` 且发送按钮提示缺项。
- [ ] 同一模板换不同变量值，模型输出随之变化（证明输入决定输出）。
- [ ] 三种预设都能跑通，结构化约束返回可解析 JSON。

## 6. 小结

- Prompt 工程的杠杆在**输入组织**，不在 API 本身；本阶段刻意不引入后端，让注意力聚焦文本。
- 模板 + 变量 = 可复用、可对比的 Prompt 工作流。
- 复用 `useDeepSeek` 说明：调优 Prompt 时，调用代码可以一行不动。
- 设计系统延续：Signal 琥珀主强调、Data 青绿流式光标、`--font-mono` 呈现模板与预览。

## 7. 练习与验收

**改造题**：给实验台加一个"System Prompt"独立输入框，并在发送时把它作为 `system` 消息传入 `useDeepSeek.send({ system, user })`（提示：阶段 01 的 `send` 已支持 `system` 参数）。再用"角色设定"预设验证：把人设从 system 移到 user，输出风格是否有差异。

**验收标准**
1. System 框内容确实进入请求体的 `messages[0].role === 'system'`；
2. 同一 user 模板下，改 system 人设能观察到输出语气/结构变化；
3. 不破坏既有模板渲染与错误提示逻辑。

> 进阶思考（阶段 04 会展开）：当模型返回 JSON，你怎么**强制并校验**它一定是 JSON？靠 Prompt 说"只输出 JSON"够吗？
