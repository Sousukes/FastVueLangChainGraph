# 阶段 18B · Computer Use（Claude 版 · 方案 A）

> 这是**阶段 18 的补充**，不是第 19 个阶段。阶段 18 按方案 C（DeepSeek 视觉仿制）把闭环跑通了，
> 并留下一句待办：DESIGN.md 原定的方案 A（真 Claude Computer Use）没有做，因为本机没有 Key。
>
> 本阶段把那句待办补上 —— 并且刻意保持「**没有 Key 也能完整交付**」这个性质。

---

## 1. 阶段目标

1. 把阶段 18 那句论证 ——「四步里只有第②步是厂商卖的」—— **改造成代码**：
   第②步抽成可插拔的大脑，①③④ 对 provider 完全无感知。
2. 接上真正的 Anthropic Computer Use：`/v1/messages` + `anthropic-beta` 头 + 内置 `computer` 工具。
3. 在**没有 `CLAUDE_API_KEY`** 的前提下，仍然把「我们发出去的线格式」验证到位。
4. 换供应商不改变读数：同一个任务、同一块屏幕、同一套评分函数，两条路的结果**直接可比**。

一句话概括本阶段的教学点：

> **换一个模型供应商，到底要改多少东西？**
> 答案不是「改很多」，也不是「改一行」，而是 —— **只改第②步，但第②步里有 6 处协议差异**。

---

## 2. 前置知识 / 环境

| 项 | 状态 | 说明 |
|---|---|---|
| `httpx` | **已有** 0.28.1 | `openai` SDK 本来就基于它，所以本阶段**零新增依赖** |
| `anthropic` SDK | **未安装** | 刻意不装：只为发一次请求装一个 SDK 不划算，而且把 beta 头、内置工具、SSE 事件藏在 SDK 后面反而看不清形状 |
| `CLAUDE_API_KEY` | **为空** | 这正是本阶段要处理的核心约束 |
| `CLAUDE_BASE_URL` | `https://api.anthropic.com/v1` | 指向 Anthropic **原生**端点，不是 OpenAI 兼容网关 |

`.env` 里相关配置（本阶段之后新增几项，都带默认值）：

```bash
CLAUDE_API_KEY=            # 留空 = 方案 A 不可用（界面会显示原因，不是点了才报错）
CLAUDE_BASE_URL=https://api.anthropic.com/v1
CLAUDE_MODEL=claude-sonnet-4-5      # 可选
CLAUDE_COMPUTER_TOOL=computer_20250124   # 可选，必须与 beta 头成对
CLAUDE_MAX_TOKENS=2048              # 可选
```

### ⚠️ 一个必须说明的环境事实

**Anthropic 官方文档在本网络被地区屏蔽。** `docs.anthropic.com` 直接重定向到
`anthropic.com/app-unavailable-in-region`，`platform.claude.com` 同样。

所以本阶段的协议细节来自**多个二手来源的交叉印证**，不是一手文档。这不影响本阶段的可交付性
（本阶段交付的核心是「可插拔架构 + 线格式验证」），但必须影响你对下面这些细节的信任程度 ——
§5 会明确列出「验证了什么 / 没验证什么」。

正因为 tool 版本表是个**会动的目标**，它被做成了配置项而不是硬编码：

| tool 版本 | 必须配的 beta 头 | 备注 |
|---|---|---|
| `computer_20251124` | `computer-use-2025-11-24` | 更新，多出 `zoom` 之类的动作 |
| `computer_20250124` | `computer-use-2025-01-24` | **本阶段默认**，动作词表较宽 |
| `computer_20241022` | `computer-use-2024-10-22` | 初代 |

> 配错会直接 **400**，而且报错信息**不会**告诉你「少了个 beta 头」—— 这是接入时最坑的第一个坑，
> 所以它在代码里是一张表（`brains.CLAUDE_BETA_BY_TOOL`），不是两处魔法字符串。

---

## 3. 核心概念

### 3.1 把「只有第②步是厂商卖的」做成代码

阶段 18 的闭环长这样，而本阶段要证明加粗的那句：

```
① 截图 ──▶ ② 决策 ──▶ ③ 执行 ──▶ ④ 回灌 ──┐
                ▲ 只有这一步    │
                └───────────────┘
```

做法就是给第②步定一个**三个方法的小协议**：

```python
class BaseBrain:
    def open(self, task_text, first_shot) -> list[dict]:   # ① 开场：初始 messages 长什么样
    def decide(self, messages) -> Iterator[dict]:          # ② 要一个决策（流式增量 + 归一化动作）
    def feedback(self, step, notes, shot) -> list[dict]:   # ④ 回灌：动作执行后追加什么消息
```

然后 `computer.py` 的主循环里，**碰模型的地方只剩一行**：

```python
for ev in brain.decide(messages):     # 换哪个大脑，循环一无所知
    ...
messages.extend(brain.feedback(step, notes, shot))
```

一句话检验这个设计成不成立：**在 `computer.py` 里搜 `provider`，你会发现沙箱、几何、评分三段
一次都没读它。** 这就是「换供应商不影响读数」的机械保证 —— 不是承诺，是结构。

### 3.2 两种协议逐项对照

「只改第②步」听起来像只改一行。实际是**六处**差异，全部集中在第②步内部：

| 维度 | DeepSeek（OpenAI 协议） | Claude（Anthropic Messages） |
|---|---|---|
| 端点 | `POST /chat/completions` | `POST /v1/messages` |
| 鉴权 | `Authorization: Bearer <key>` | `x-api-key` + `anthropic-version: 2023-06-01` |
| 额外头 | 无 | **必须** `anthropic-beta: computer-use-...` |
| 系统提示词 | `messages` 里的一条 `role="system"` | **顶层独立参数** `system` |
| 工具来源 | **我们自己写 schema**（枚举 5 个动作） | **内置工具**，schema 烧在模型里 |
| 必填项 | 无特殊 | `max_tokens` 必填，漏了 422 |
| 坐标 | `"x": 360, "y": 149` | `"coordinate": [360, 149]` |
| 按键 | `"keys": "Return"` | `"text": "Return"`（当 `action="key"`） |
| 工具结果 | `role="tool"` 装文本（**装不了图**）→ **再补一条** `role="user"` 带图 | `tool_result` 里**直接塞 image 块**，一条消息搞定 |
| 流式分片 | `tool_calls` 按 `index` 拼 `arguments` | `input_json_delta` 按块拼 `partial_json` |

⭐ 最后三行是本阶段最有价值的三条。

### 3.3 内置工具 vs 自写 schema：**谁定动作词表**

这是整个阶段最值得想清楚的一点。

- **DeepSeek 那边**：`COMPUTER_TOOL` 的 `parameters.enum` 是我们写的，只列了
  `screenshot / left_click / type / key / wait`。于是模型**天生只知道这 5 个动作**。
- **Claude 那边**：`computer` 是内置工具，**没有 `input_schema`**（我们只能声明屏幕宽高）。
  动作词表在模型里 —— 它会自然地说 `mouse_move`、`scroll`、`double_click`、`left_click_drag`。

于是 Claude 一定会去请求宿主不支持的动作。**而沙箱会把它挡回去**：

```
不支持的动作 'mouse_move'。可用动作：screenshot, left_click, type, key, wait。
```

这不是 bug，也不是「我们没实现」。这正是真实 Computer Use 的常态：

> **动作词表由宿主决定，不由模型决定。** 宿主只承诺执行它愿意执行的那些，
> 并把拒绝理由回给模型，让模型自己改。

所以本阶段刻意**没有**去扩宽 `ALLOWED_ACTIONS`（那会改动阶段 18 已测过的读数），
而是把这个差异变成了一个可见的读数：`rejectedActions`。走 Claude 时它通常 > 0，
走 DeepSeek 时它恒为 0 —— 一句话就把「自写 schema vs 内置工具」的差别量出来了。

### 3.4 最反直觉的一处：图放在 `tool_result` 里

阶段 18 花了一整段讲那个「协议坑」：

> OpenAI 协议里 `role="tool"` 的消息**只能装文本**，装不了图片。
> 所以新截图不能当 tool 结果回传，必须再追加一条 `role="user"` 多模态消息。

当时读起来像是 Computer Use 的固有约束。**它不是** —— 它只是 OpenAI 协议的形状。
Anthropic 允许把 `image` 块直接放进 `tool_result`，一条消息就够：

```jsonc
// 回灌：Anthropic 写法（一条 user 消息，注意 tool_result 必须排在最前）
{"role": "user", "content": [
  {"type": "tool_result", "tool_use_id": "toolu_01", "content": [
      {"type": "text",  "text": "点击 (360, 149)：已聚焦 ORDER ID 输入框…"},
      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
  ]},
  {"type": "text", "text": "（第 1 轮动作已执行完毕）"}
]}
```

于是同一个动作之后，两边的消息序列长度都不一样：

| | 追加了哪些消息 |
|---|---|
| OpenAI 协议 | `["tool", "user"]` —— **两条**（`tool` 装文本，`user` 带图） |
| Anthropic | `["user"]` —— **一条**（图在 `tool_result` 里） |

`_t_claude_computer_static.py` 里就断着这个：**整个对话只有 3 条消息、没有 `tool` 角色**。

### 3.5 没有 Key，能验证什么？

这是本阶段最需要诚实回答的问题。

线格式（wire format）是**我方**的产物：请求头、工具定义、消息结构、SSE 解析、`tool_result` 的形状,
全都由我们的代码决定。**既然决定权在我们手里，就不必等对方回话才能测。**

做法：起一个本地 `http.server` **假扮 Anthropic Messages API**，它做两件事：

1. 记录我们**真实发出的** HTTP 请求（头 + body）；
2. 按脚本回放**真实形状的 SSE**（含被切成三段的 `input_json_delta`）。

于是这条链路被完整验证：

```
ClaudeBrain._payload ─▶ httpx ─▶ 真实 HTTP ─▶ mock 记录并断言
ClaudeBrain.decide 的 SSE 解析 ◀─ 脚本化事件流 ◀─┘
```

**但必须说清楚它证明不了什么**：mock 是按「我以为的协议」写的。它只能证明
「**我的实现和我的理解自洽**」，**不能**证明「我的理解与 Anthropic 一致」。
真正的交叉验证需要一把 Key 打一次真接口 —— 本机没有，所以 §5 里如实标注为**未实测**。

这不是自我否定。把「能验证的」验证干净、把「不能验证的」标清楚，比含糊地说「已支持 Claude」有价值得多。

---

## 4. 动手实现

### 4.1 `backend/brains.py`（新，745 行）

两个大脑，共享三个方法：

- `DeepSeekBrain` —— 阶段 18 的行为原样搬过来。**顺带搬走了一个函数**：
  `_result_messages` 从 `computer.py` 挪进了 `DeepSeekBrain.feedback`，因为
  「tool 消息装文本 + 另起 user 消息带图」是 **OpenAI 协议特有**的细节，
  它待在通用循环里会让人误以为 Computer Use 本身需要这么绕。
- `ClaudeBrain` —— 原生 HTTP 说 Anthropic Messages 协议。

### 4.2 Anthropic SSE 解析（`input_json_delta` 分片）

这是本阶段最容易写错的一段，和阶段 10 拼 `tool_calls` 分片是同一个坑：

> 工具入参是**逐片吐**的（模型一个 token 一个 token 生成 JSON），
> 必须按 `index` **累加拼接**，不能覆盖。

```python
elif kind == "content_block_delta":
    d = ev.get("delta") or {}
    if d.get("type") == "text_delta":
        ...                                    # 文本增量 → 直接吐给前端
    elif d.get("type") == "input_json_delta":
        slot = tools_by_index.setdefault(idx, {...})
        slot["partial"] += d.get("partial_json") or ""     # ⚠️ 累加，不是赋值
```

拼完之后还要**原样回放**：assistant 消息里的 `tool_use.input` 必须是重建出来的完整对象，
否则下一轮 API 会认为「工具调用与结果不匹配」。

测试里刻意把 JSON 切成三段、并且**在词中间断开**（`{"action": "left` + `_click", ...`），
用「覆盖式」实现会立刻拼坏。

解析器只认 `data:` 行 —— 因为 Anthropic 每个事件负载里都带 `type` 字段，
所以不必另外维护 `event:` 行与 JSON 的对应关系（少一处能写错的地方）。
非 `data:` 行、坏 JSON、`[DONE]`、`ping` 全部安静略过。

### 4.3 动作入参归一化（`claude_input_to_args`）

三处真实协议差异，一行一处理：

```python
coord = inp.get("coordinate")          # Claude: [360, 149]
if isinstance(coord, (list, tuple)) and len(coord) >= 2:
    args["x"], args["y"] = coord[0], coord[1]

text = inp.get("text")                 # Claude: key 的键名也放在 text 里
if isinstance(text, str) and text:
    if action == "key":
        args["keys"] = text            # → 我们内部叫 keys
    else:
        args["text"] = text
```

其余动作（`scroll` / `double_click` / `left_click_drag` …）**原样透传**，交给白名单拒绝。
这是设计，不是遗漏：让拒绝发生在**一个地方**（沙箱），而不是散落在解析层。

### 4.4 `computer.py` 只改两处

| 位置 | 改动 |
|---|---|
| 第②步 | `client.stream_message(...)` → `brain.decide(messages)` |
| 回灌 | `_result_messages(...)` → `brain.feedback(...)` |

外加：`provider` 参数（默认 `"auto"`）、`start`/`finish` 帧带上 `provider` / `protocol` / `rejectedActions`、
删掉 `_result_messages`（搬进 `DeepSeekBrain.feedback`）。

**关键验收：阶段 18 的 14 项静态测试必须继续 14/14 全绿** —— 改完确实全绿，
证明这次重构对 DeepSeek 那条路是**行为保持**的。

### 4.5 端点与前端

- `main.py`：两个端点透传 `provider`；新增 `GET /api/computer/providers`（报告可用性）。
- 流式端点**先校验再开流**：provider 非法 / 缺 Key 是**开流前**就该 400 的事，
  否则错误只能以「迟到的 error 帧」出现 —— 用户得先建好连接才知道自己配错了。
- 校验方式是 `brains.preflight()`：**真的造一次大脑再丢掉**，而不是另写一套校验规则。
  两套规则迟早会漂移，然后出现「校验说没问题、真跑起来才炸」；而大脑构造函数是纯配置，代价可忽略。
- 前端：`auto / Claude / DeepSeek` 三档分段控件（不可用的直接置灰）、
  进页面就拉一次可用性、缺 Key 时把「填哪个变量、改哪个文件、端点是什么、
  tool 版本必须配哪个 beta 头」全部摊在界面上；评分面板新增「动作被沙箱拒绝」。

---

## 5. 运行验证

### ① 静态冒烟（不需要 Key）—— 16 项全绿

```bash
cd backend
uv run python _t_claude_computer_static.py
```

```
✅ 1. 无 Key → ClaudeNotConfigured（显式报错，绝不静默降级）
✅ 2. beta 头与 tool 版本成对（三种版本），未知版本 → ClaudeConfigError，空值→默认
✅ 3. messages_url 归一化：4 种写法 → https://api.anthropic.com/v1/messages
✅ 4. split_data_url / image_block：data URL 正确拆成 media_type + base64
✅ 5. claude_input_to_args：coordinate→x/y、key 的 text→keys、未知动作原样透传
✅ 6. SSE 解析健壮性：6 种坏行全部安静略过，正常行照常解析
✅ 7-12. 完整闭环（Claude）：8 次请求 / 7 步 / 3 字段全对 / 4 次点击全中 / rejectedActions=0
        · 请求头 x-api-key + anthropic-version + anthropic-beta 齐全
        · 内置工具**没有** input_schema（与 DeepSeek 自写 schema 的对照）
        · 工具入参被切成 3 段下发 → 拼装回完整对象并原样回放
        · tool_result 排在文字前、图塞在 tool_result 里、tool_use_id 配对
✅ 13. 白名单挡住 Claude 的 mouse_move / double_click：2 次被拒、状态不变、流程走到 finish
✅ 14. 401 → ClaudeAPIError（大脑层硬失败）→ 循环层兜住 → 端点 200 + error 字段
✅ 15. 流式端点先校验再开流：无 Key → 立刻 400（不是开流后才发 error 帧）
✅ 16. blocking：provider=claude / protocol 齐全 / ComputerResponse 校验通过 / 落点目标依次正确
```

**这次「全部点中」时 `avgCenterOffsetPx = 0.0`** —— 因为 mock 脚本里的坐标就是元素中心，
是刻意取的精确值，不是"没样本"（阶段 18 §3.3 讲过这两个 0 的区别）。

### ② 回归：阶段 18 的 14 项

```bash
uv run python _t_computer_static.py     # 14/14 ✅（重构对 DeepSeek 路径行为保持）
```

### ③ 全量验收

```bash
uv run python _check_globals.py         # MISSING(main): none   ← 期望 EXIT=0
uv run python -c "import main; print(len(main.app.routes))"    # 53 → 54
cd ../frontend && node node_modules/vue-tsc/bin/vue-tsc.js -b --force   # 0 错
node node_modules/vite/bin/vite.js build     # 通过（ComputerPage 18.89 kB JS + 8.89 kB CSS）
cd ../docs && node node_modules/vitepress/bin/vitepress.js build        # 通过
```

> 📌 **`_check_globals.py` 在本阶段又抓到一个真 bug**：`main.py` 的三个端点函数体里用了 `brains.`，
> 但**忘了 `import brains`**。而 `import main` 依然成功、54 条路由照样注册 ——
> 这正是阶段 16 那个 `import vision` bug 的同一类。**检查器建对了，第二次就自动抓住了。**

### ④ 真实 Claude 调用 —— **未实测**

**本机没有 `CLAUDE_API_KEY`，所以没有跑过任何一次真实的 Claude 调用。** 这一点不掩饰。

| 项目 | 状态 |
|---|---|
| 我们发出的请求头 / 请求体 / 消息结构 / SSE 解析 | ✅ 已用本地 mock 端到端断言 |
| 真实 Anthropic 接口能否接受这些请求 | ❌ **未验证** |
| 真实 Claude 的 grounding 精度（对比阶段 18 的 2.10 px） | ❌ **未验证** |
| tool 版本 ↔ beta 头 ↔ 模型 id 的配对 | ⚠️ 来自二手资料交叉印证（官方文档在本网络被屏蔽） |

**怎么补测**：把 Key 填进 `backend/.env` 的 `CLAUDE_API_KEY`，重启后端，
界面上的 `Claude（方案 A）` 按钮就会从置灰变可用；点「开始操作」即可。
若返回 400 且提示与 tool / beta 有关，先改 `CLAUDE_COMPUTER_TOOL` 与 `CLAUDE_MODEL` 的配对。

---

## 6. 小结

**这一阶段真正教的不是「怎么调 Anthropic」，而是「换供应商的边界在哪」。**

1. **「只有第②步是厂商卖的」可以从口号变成结构。** 判据很硬：在 `computer.py` 里搜 `provider`，
   渲染、沙箱、评分三段一次都不读它。所以两条路的命中率 / 定位偏差是**可直接对比**的读数，
   而不是「大概都能跑」。
2. **换供应商的真实成本集中在第②步内部，而且是 6 处协议差异**，不是 1 处。
   凡是说「换个 base_url 就行」的说法，都默认了对方兼容 OpenAI 协议 —— Anthropic 不兼容。
3. **动作词表由宿主决定。** 内置工具意味着模型会说出宿主不认的动作，沙箱拒绝它、
   把可用动作回给模型、让模型自己改 —— `rejectedActions` 就是这个差别的读数。
4. **「协议坑」要分清是协议的还是产品的。** 阶段 18 那句「tool 消息装不了图」是**OpenAI 协议**的坑，
   不是 Computer Use 的坑；换到 Anthropic 就完全不存在。**把协议的约束误当成领域的约束，
   是很容易犯、又很贵的一类错。**
5. **没有凭据也要把能验的验干净。** 线格式是我方产物 → 用 mock 断言到字节级；
   真实调用 → 如实标「未实测」。含糊地说「已支持」比明确地说「未实测」有害得多。

---

## 7. 练习与验收

### 练习

1. **扩宽动作词表**：给 `computer._step` 加上 `mouse_move`（只移动准星、不点击）。
   注意 `ALLOWED_ACTIONS` 是 DeepSeek 那侧 tool schema 的 `enum` 来源 ——
   加了它，DeepSeek 可能会多出动作，阶段 18 §5 的读数就不再可比。**先想清楚这个代价再动手。**
2. **把 `rejectedActions` 变成提示**：连续被拒 2 次后，在系统提示词里追加一句
   「宿主只支持这些动作」。观察 Claude 是否更快收敛。
3. **接第三方 OpenAI 兼容网关**：如果再写一个 `OpenAICompatBrain`，你会发现它与
   `DeepSeekBrain` 只差 `base_url` —— 那时可以合并成一个大脑。**这就是「兼容协议」
   和「自定义协议」的成本差。**
4. **给 mock 加上 `201` / 429 / 超时**：看循环层的兜底策略是否都合适
   （当前 401 → error 帧 + finish；429 大概也应该如此，但值得确认）。

### 验收标准

- [ ] `uv run python _check_globals.py` → `MISSING(main): none`，且退出码 0
- [ ] `uv run python _t_computer_static.py` → **14/14**（阶段 18 回归，必须不退化）
- [ ] `uv run python _t_claude_computer_static.py` → **16/16**
- [ ] `import main` 成功，路由数 53 → **54**
- [ ] `vue-tsc -b --force` **0 错**；`vite build` 通过；`vitepress build` 通过
- [ ] 无 Key 时：界面 `Claude（方案 A）` 按钮**置灰并给出配置指引**；点 `auto` 仍能正常跑 DeepSeek
- [ ] 无 Key 时：`POST /api/computer/stream` 带 `provider=claude` → **400**（不是开流后才报错）

---

## 附：与 DESIGN.md 的差异记录

阶段 18 曾把 DESIGN.md 的方案 A 改成方案 C，并写了变更记录（理由：无 Key、要装 SDK、要 Docker 沙箱）。

**本阶段（18B）把方案 A 补回来了**，但**没有推翻那条变更记录**，而是把它精确化：

| | 阶段 18 的结论 | 18B 修正后的结论 |
|---|---|---|
| 方案 C（DeepSeek 仿制） | 改为默认 | **仍是默认**（`provider="auto"` 且无 Key 时的选择）—— 保证零密钥可跑 |
| 方案 A（Claude 原生） | 放弃 | **作为可选 provider 实现**，有 Key 即启用 |
| 是否新增依赖 | 担心要装 `anthropic` SDK | 不装 —— 用已有的 `httpx` 直接说 HTTP，**零新增依赖** |
| 是否要 Docker 沙箱 | 担心 | 不需要 —— 两边共用同一个**自渲染虚拟屏幕**，动作只改内存状态 |

也就是说：18B **保住了方案 A 的全部教学价值**（真实的内置工具、beta 头、Anthropic 协议形状），
同时**没有付出**当初担心的任何一项代价。

唯一真正没解决的是**真实精度对比** —— 那需要一个 Key，如实记录在 §5④。
