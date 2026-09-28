# 阶段 01 · 大模型 API 编程基础

> 独立练习（能力积木）｜预计 1–2 小时
> 配套代码：`frontend/src/composables/useDeepSeek.ts`、`frontend/src/components/ChatPanel.vue`

## 1. 阶段目标

- 理解「大模型 API」到底是什么：一次普通的 HTTP POST 请求。
- 掌握 OpenAI 兼容接口的核心字段：`model` / `messages` / `stream`。
- 用 Vue3 从浏览器**直接**调用 DeepSeek，跑通一次最小对话（刻意不经由后端）。
- 建立第一个关键认知：**前端直连只是教学手段**，真实生产必须走后端代理（阶段 03 接管）。

## 2. 前置知识 / 环境

| 项 | 要求 |
|---|---|
| 已完成 | 阶段 00（已装 `uv`/Node/pnpm、已申请 DeepSeek Key、VitePress 可预览） |
| 运行环境 | Node 18+、pnpm |
| 密钥 | 一个有效的 `DeepSeek API Key`（`sk-...`） |
| 本阶段后端 | **不需要**。阶段 01–02 故意前端直连，讲清原理。 |

> ⚠️ 安全红线：本阶段把 Key 直接写进浏览器请求头，仅用于教学。任何生产代码都**不能**这样做——下一处你会学到为什么。

## 3. 核心概念

### 3.1 它就是一个 REST 接口

大模型「对话」在传输层上毫无神秘感：

```
POST https://api.deepseek.com/chat/completions
Authorization: Bearer sk-xxxx
Content-Type: application/json

{
  "model": "deepseek-flash",
  "messages": [
    { "role": "system", "content": "你是一个严谨的研究助手" },
    { "role": "user",   "content": "什么是大模型 API？" }
  ],
  "stream": false
}
```

返回（节选）：

```json
{
  "choices": [{
    "message": { "role": "assistant", "content": "大模型 API 是一组……" }
  }]
}
```

### 3.2 三个必须记住的字段

- **`model`**：用哪个模型。本课默认 `deepseek-flash`（便宜、快、够用）。
- **`messages`**：对话历史数组，`role` 只可能是 `system` / `user` / `assistant`。
  - `system`：设定人设与约束（厨师菜谱，不在台面上）。
  - `user`：用户当前说的话。
  - `assistant`：模型历史回复（多轮对话时由你回填，阶段 03 展开）。
- **`stream`**：是否流式。`false` 一次性返回；`true` 逐字推送（阶段 03 重点）。

### 3.3 为什么阶段 01 要"前端直连"

只有亲手把 `Authorization: Bearer sk-...` 写进 `fetch`，你才会真正理解：
1. 所谓「调用大模型」= 拼一个 JSON 发 HTTP 请求；
2. 密钥一旦进前端就等于公开；
3. 后端代理的价值——隐藏密钥、统一限流、加流式——才有说服力。

## 4. 动手实现（前端分步）

### 4.1 封装调用逻辑：`useDeepSeek.ts`

遵循 `vue-best-practices`：把"副作用 + 状态"抽进 composable，组件只负责展示。

```ts
// frontend/src/composables/useDeepSeek.ts
import { ref, shallowRef, readonly } from 'vue'

export function useDeepSeek(options: { baseUrl?: string; model?: string } = {}) {
  const baseUrl = options.baseUrl ?? 'https://api.deepseek.com'
  const model = options.model ?? 'deepseek-flash'

  const apiKey = ref('')              // 仅存内存，刷新即清空
  const response = ref('')
  const loading = shallowRef(false)   // 布尔量用 shallowRef 足够
  const error = shallowRef<string | null>(null)

  async function send(payload: { system?: string; user: string }) {
    if (!apiKey.value) { error.value = '请先填写 DeepSeek API Key'; return }
    loading.value = true
    error.value = null
    response.value = ''
    try {
      const messages = []
      if (payload.system?.trim()) messages.push({ role: 'system', content: payload.system.trim() })
      messages.push({ role: 'user', content: payload.user })

      const resp = await fetch(`${baseUrl}/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${apiKey.value}`,
        },
        body: JSON.stringify({ model, messages, stream: false }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      response.value = data?.choices?.[0]?.message?.content ?? ''
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  return {
    apiKey,
    response: readonly(response),
    loading: readonly(loading),
    error: readonly(error),
    send,
  }
}
```

要点：
- `loading` / `error` 是原始布尔/字符串，用 `shallowRef` 即可，不必 `ref` 深层响应。
- 返回给组件的状态用 `readonly()` 包裹——组件能读不能改，保证唯一写入入口在 `send` 内。
- Key 不写进代码、不进 `localStorage`，只在 `ref` 里活到刷新为止。

### 4.2 对话台 UI：`ChatPanel.vue`

`ChatPanel` 消费 composable，提供 Key 输入框、System/User 文本域、发送按钮与输出控制台。输出区是本课**签名元素「流式光标」**的首秀：

- 生成中：闪烁的 Data 青绿 caret（`▍`）；
- 完成后：caret 收为实心句点（`.`），呼应"模型已说完"。

```vue
<!-- 关键片段 -->
<pre v-if="response" class="console-text">
  {{ response }}<span class="caret-done" />
</pre>
<div v-else-if="loading" class="console-loading">
  <span class="caret" /> 正在生成…
</div>
```

样式取自 `design-system.md`：Ink 背景、`--data` 青绿 caret、`--signal` 琥珀作唯一主强调。

### 4.3 把部件拼进页面：`App.vue` + `StageRail` + `AppHeader`

`App.vue` 用网格布局组合三块：左侧 `StageRail`（00→18 进度轨）、顶栏 `AppHeader`、主区 `ChatPanel`。当前阶段常量 `CURRENT_STAGE = 1`，与 `docs/DESIGN.md` 阶段映射表对齐。

## 5. 运行验证

```bash
cd frontend
pnpm install
pnpm dev          # 启动 http://localhost:5173
```

1. 打开页面，左侧进度轨高亮 `01 ▶`，主区显示"大模型 API 调用台"。
2. 在 `DeepSeek API Key` 框粘贴你的 `sk-...`。
3. 点击「发送请求」。
4. 预期：caret 闪烁 → 输出区出现模型回答，末尾为青绿实心句点。

**验证清单**
- [ ] 未填 Key 点击发送 → 提示"请先填写 DeepSeek API Key"，不发请求。
- [ ] 填错 Key → 输出区出现琥珀色错误块（含 HTTP 状态码）。
- [ ] 正常对话 → 回复以流式光标签名呈现。
- [ ] 刷新页面 → Key 清空（证明未落盘）。

## 6. 小结

- 大模型 API = 一次带 `Authorization` 的 HTTP POST，`messages` 数组描述对话。
- 阶段 01 前端直连是为了"看见原理"，不是生产范式。
- `vue-best-practices` 落点：逻辑进 composable、状态 `readonly` 外放、组件只管展示。
- 签名视觉（流式光标）从本阶段开始贯穿全课。

## 7. 练习与验收

**改造题**：给 `useDeepSeek` 增加 `temperature` 与 `max_tokens` 两个可选参数，并在 `ChatPanel` 加两个滑块控制它们；观察不同 `temperature` 下同一 prompt 的输出差异。

**验收标准**
1. 滑块值能传入请求体对应字段；
2. `temperature` 调高后，连续两次相同提问的回答明显不同；
3. 不破坏既有 `readonly` 状态契约与错误提示。

> 进阶思考（阶段 03 会回答）：如果把 Key 留在前端，别人用 DevTools 两秒钟就能偷走它——那生产环境该把 Key 放哪？又该让谁去调大模型？
