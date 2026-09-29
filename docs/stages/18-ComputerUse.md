# 阶段 18 · Computer Use

> 这是**最后一个阶段**。到这里，模型已经能读文字、检索资料、看图片、听声音 —— 但始终有一个东西它是碰不到的：**别人的电脑**。
>
> Computer Use 就是把这最后一层补上：让模型**看着屏幕截图，自己决定点哪里、打什么字**。
>
> 但动笔之前必须先把一个事实查清楚，而不是凭印象：**这个能力归谁？**
>
> 按 DESIGN.md 的原定方案，这一阶段要引入 **Claude**（官方 `computer_20251124` 工具），并把它当作全课「唯一例外」。真正动手前我重新核对了一遍，结论变了：
>
> - `.env` 里 `CLAUDE_API_KEY` **是空的**，且 `CLAUDE_BASE_URL` 指向 Anthropic **原生**端点（`/v1/messages` + `anthropic-beta` 头 + `x-api-key` 鉴权）—— 而本项目的 `llm.py` 只会说 **OpenAI 协议**；
> - 官方还要求 Computer Use 必须跑在 **Docker/VM 沙箱**里（否则模型能在你本机随便点）；
> - 更关键的是：**Computer Use 的闭环里只有一步是模型厂商卖的**。
>
> 于是本阶段改走**方案 C · DeepSeek 视觉仿制**：零新依赖、零新密钥、零 Docker，把闭环完整跑通。
> 代价是**精度** —— 而精度差恰好是本阶段最好的教材。

---

## 1. 阶段目标

- 学会把「一个听起来要买的东西」拆成**闭环**，再判断**闭环里哪一步才是真正值钱的**。
- 实测确认边界：本项目**够不着** Claude Computer Use（无 Key + 协议形状不同 + 需要沙箱），但**够得着**它的机制。
- 掌握 Computer Use 的四步闭环：**① 截图 → ② 模型决策 → ③ 执行动作 → ④ 回灌结果**，并说清每一步归谁。
- 踩并解决本阶段**最容易踩的协议坑**：OpenAI 兼容协议里 `role="tool"` 的消息**只能装文本**，装不了图片 —— 新截图必须另起一条 `role="user"` 多模态消息回灌，而每个 `tool_call` 又必须有配对的 `tool` 消息。
- 学会把**误操作**变成安全边界：动作白名单 + 坐标钳制 + **危险动作默认拒绝**（人工确认闸门）。
- ⭐ 掌握本阶段的判据思路：因为「被操作的电脑」是我们自己渲染的，**每个元素的 bbox 就是已知真值**，于是模型的每一次点击都能被**确定性地**量出偏差 —— 不必问它「你点准了吗」。
- 学会区分**两个容易混淆的读数**：「命中率」（有没有点中）与「定位偏差」（点得多准），并理解为什么只有后者在「全部点中」时仍然有意义。

---

## 2. 前置知识 / 环境

- **已完成**：阶段 05（工具注册表 + function calling）、10（ReAct 循环）、16（多模态消息结构）。
- **复用资产**：
  - `llm.LLMClient.stream_message()` —— 阶段 10 就有的「流式 + tool_calls」，**本阶段对其零改动**；
  - 阶段 16 验证过的图像块消息结构 `{"type":"image_url","image_url":{"url": <data URL>}}`；
  - 阶段 10 的 ReAct 循环骨架（本阶段是它的「带眼睛」版本）。
- **环境**：后端依赖与阶段 07 一致（`uv`）；`.env` 里需有 `DEEPSEEK_API_KEY`。**本阶段不需要知识库**。
- **新增依赖**：**一个也没有**。`Pillow` 原本只是被 `chromadb` 间接带进虚拟环境的，本阶段要直接用它渲染虚拟屏幕，因此在 `pyproject.toml` 里**显式声明**（见 §6 第 3 条）。
- **⚠️ 安全前提**：本阶段的一切动作只作用于**内存里的一个 `dict`**，碰不到真实文件系统、真实鼠标、真实网络。这不是省事，是 Computer Use 的第一原则。
- **模型基线**：继续用 DeepSeek（视觉能力已在阶段 16 验证），因此「00–18 全程 DeepSeek」的基线**没有破**。

---

## 3. 核心概念

### 3.1 把「要买的东西」拆成闭环

先把 Computer Use 拆开看。它说到底就是一个循环：

```
   ① 截图 ─▶ ② 模型决策 ─▶ ③ 执行动作 ─▶ ④ 回灌结果 ──┐
              ▲                                          │
              └──────────────────────────────────────────┘
```

**这四步里只有第 ② 步是厂商卖的** ——「看一张屏幕截图，说出点哪里、打什么字」。而 ①③④ 全是应用侧自己的代码，并且**本课早就建好了**：

| 步骤 | 谁提供 | 本课在哪学过 |
|---|---|---|
| ① 截图 | 你自己 | 阶段 16（图像块怎么递给模型）+ 本阶段用 Pillow 渲染 |
| ② 模型决策 | **厂商**（Claude / GPT / Gemini） | 阶段 16 已验证 DeepSeek 也能看 |
| ③ 执行动作 | 你自己 | 阶段 05 的工具注册表、阶段 10 的循环 |
| ④ 回灌结果 | 你自己 | 阶段 10 的 `tool` 消息 + 阶段 16 的 `user` 图像消息 |

看清这张表，方案 C 就不需要说服力了：**只要第 ② 步能用 DeepSeek 接上，整条闭环就通了。**

⚠️ 同时必须**如实标注差别**，不能冒充真 Claude Computer Use：

| 维度 | 真 Claude Computer Use | 本阶段（仿制） |
|---|---|---|
| 模型 | Claude Opus/Sonnet，官方 `computer_20251124` 工具 | DeepSeek 视觉 |
| 工具形状 | Anthropic 内置、schema-less（模型直接吐坐标） | 自定义 function tool（`action` 枚举 + `x/y`） |
| 操作对象 | 真实桌面 / 浏览器 | **我们自己渲染的虚拟表单** |
| 精度与稳健性 | 差距明显，且本阶段只做**单个**确定性小任务 | —— |

另外那两句行业说法也需要纠正：「Claude 是唯一例外」在 2026 年**已经不准确**（OpenAI 的 Responses API `computer` 工具、Gemini 的 `computer_use` 都可用），但对本项目结论不变 —— 它们各自都不在 OpenAI Chat Completions 的形状里。

### 3.2 ⭐ 自己渲染屏幕，是为了把「主观」变成「可测」

这是本阶段最关键的一个设计决定。

真实 Computer Use 里，「点得准不准」是**没法验证**的：模型说点 `(480, 320)`，你不知道那个坐标上应该有什么。于是你只能靠任务成败**间接**推断。

但如果**操作的屏幕是我们自己画的**，那么每个元素的 `(x, y, w, h)` 在我们的代码里就是**已知真值**：

```python
_ELEMENTS = [
    {"name": "field_order",  "kind": "text_field", "x": 80, "y": 124, "w": 560, "h": 50, "label": "ORDER ID"},
    {"name": "btn_submit",   "kind": "button",     "x": 80, "y": 410, "w": 180, "h": 56, "label": "SUBMIT"},
    ...
]
```

于是每一次点击都能被**确定性地**量出偏差，全程不问模型一句「你点准了吗」：

```python
def element_at(x, y):  ...      # 倒序遍历：后画的在上层
def rect_distance(el, x, y):    # 点到矩形的最短距离（点在矩形内为 0）
def nearest_distance(x, y):     # 点空时：离所有元素里最近那个还有多远
def center_offset(el, x, y):    # 命中时：离**这个元素中心**多远
```

这就是本阶段接上那条暗线的地方 —— 每个阶段都留一个**确定性数字**，而且一个都不用问模型：

| 阶段 | 确定性判据 | 它在回答什么 |
|---|---|---|
| 14 | `coverage` | 你**声称**引用了吗 |
| 15 | `faithfulness` | 你**声称**被资料支持吗 |
| 17 | `termCheck` | 你**自述**的改写真的发生了吗 |
| **18** | `clickHitRate` / `avgCenterOffsetPx` / `lostKeystrokes` | 你到底**点到了哪里** —— 纯观测 |

### 3.3 两个容易混淆的读数：命中率 vs 定位偏差

设计评分时我踩了一个坑，值得单独讲。

最早的定位判据只有一个：`avgClickErrorPx` ——「**点空时**离最近元素还有多少像素」。它看起来挺合理，直到真实那次运行**全部点中**，它返回了 `0.0`。

问题就出在这里：**这个 0 不是「零误差」，是「没有样本」。** 而且它和另一种情况完全无法区分 —— 一个模型**一次都不点**，同样会得到 `0.0`。一个在成功时退化成 0 的指标，没法用来比较。

于是补上第二个读数：

```python
def center_offset(el, x, y):
    """点到**这个元素中心**的像素距离。

    ⭐ 命中率高不代表点得准 —— 一个 560×50 的大输入框，随便点哪儿都算"命中"。
    """
    cx = el["x"] + el["w"] / 2
    cy = el["y"] + el["h"] / 2
    return round(math.hypot(x - cx, y - cy), 1)
```

现在两个读数各管一件事，**互补**：

| 读数 | 何时有值 | 回答的问题 |
|---|---|---|
| `clickHitRate` | 只要点过就有 | 有没有点中（**离散**：命中 / 点空） |
| `avgCenterOffsetPx` | **只要命中就有**（全中也算） | 点得多准（**连续**：像素偏差） |
| `avgClickErrorPx` | 只有点空才有 | 点空时差多远（失败有多严重） |
| `lostKeystrokes` | 打字被丢弃时 | **定位失败最硬的证据** |

最后那个 `lostKeystrokes` 是本阶段最锋利的判据，而且它是**免费**的：模型的键盘只会打进**被聚焦**的输入框，所以「它想打字，但前一次点击没落在任何输入框上」这件事**必然**会表现为按键全部丢弃。这是一个**纯观测**的事实 —— 不需要模型承认，也不需要任务失败。

---

## 4. 动手实现

### 4.1 后端 `computer.py`：虚拟屏幕 + 状态机 + 沙箱 + 循环

**① 渲染**（模型看的就这张图，`Pillow` 确定性渲染）：

```python
def render_screen(state: dict) -> bytes:
    """把虚拟应用画成 PNG 字节。**确定性**：同样的状态永远画出同样的图。"""
    img = Image.new("RGB", (SCREEN_W, SCREEN_H), (246, 246, 243))
    ...
    # 光标：把上一次点击画出来，让模型能自己核对"我刚才点在哪"
    cur = state.get("cursor")
    if cur:
        d.line([cx - 14, cy, cx + 14, cy], fill=(226, 60, 120), width=3)
        d.line([cx, cy - 14, cx, cy + 14], fill=(226, 60, 120), width=3)
```

字体用 `ImageFont.load_default(size)` —— Pillow 9.2 起它返回**真正的矢量字体**，因此**不依赖任何系统字体文件**（比写死 `arial.ttf` / `msyh.ttc` 可移植得多）。

屏幕上的品红十字准星是个小设计：它让模型**能自己发现点偏了**，否则模型只有「点完之后屏幕没变化」这一条线索。

**② 状态机**：整个「虚拟电脑」的状态就是**一个 dict**：

```python
def initial_state() -> dict:
    return {
        "field_order": "", "field_amount": "", "field_date": "",
        "focus": "", "submitted": False,
        "status": "Idle. Fill the three fields, then press SUBMIT.",
        "cursor": None, "revision": 0,
    }
```

**③ 动作白名单 + 沙箱**（`_step()` 是唯一的执行入口）：

```python
ALLOWED_ACTIONS = ("screenshot", "left_click", "type", "key", "wait")
```

三条安全设计，每一条都刻意做了「能继续跑」而不是「报错卡死」：

```python
# ① 坐标钳制：模型给出越界坐标很常见，钳制而不是报错（报错会让它卡死）
nx = min(max(xi, 0), SCREEN_W - 1)
ny = min(max(yi, 0), SCREEN_H - 1)

# ② 危险动作默认被拒 —— 这就是"人工确认"那道闸门
if el is not None and el.get("dangerous") and not allow_dangerous:
    return {...}, f"{el['label']} 是危险操作，沙箱已直接拒绝（需要人工确认）。", None

# ③ 动作名归一化：模型常写 Left_Click / left-click / LEFT CLICK
def _norm_action(name): return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
```

键盘只能打 ASCII，也刻意做成**如实反馈**而不是静默失败：

```python
ascii_only = "".join(ch for ch in text if ch.isascii())
dropped = len(text) - len(ascii_only)
...
if dropped:
    note += f"（{dropped} 个非 ASCII 字符被键盘丢弃）"
```

**④ ⭐ 主循环 —— 本阶段最容易踩的协议坑**

ReAct 循环已经做过一遍（阶段 10），但这里多了一个图像回灌的问题：

```python
def _result_messages(step: int, tool_notes: list[tuple[str | None, str]], shot: str) -> list[dict]:
    """执行完动作后要追加的两类消息。

    ⚠️ 这里是本阶段**最容易踩的协议坑**：
      `role="tool"` 的消息只能装文本，装不了图片。所以新截图不能当 tool 结果回传，
      必须再补一条 `role="user"` 的多模态消息把图带上。
      而每个 tool_call 又**必须**有配对的 tool 消息，否则 API 直接报错。
      两件事都得做，顺序也不能反。
    """
    msgs = []
    for call_id, note in tool_notes:
        msgs.append({"role": "tool", "tool_call_id": call_id, "content": note})
    msgs.append({
        "role": "user",
        "content": [
            {"type": "text", "text": f"第 {step} 轮动作已执行完毕。这是**执行后**的新屏幕截图："},
            {"type": "image_url", "image_url": {"url": shot}},
        ],
    })
    return msgs
```

也就是说**每一轮要追加 3 类消息**：`assistant`（带 `tool_calls`）→ 若干 `tool`（文本结果，必须一一配对）→ **一条 `user`（多模态，装新截图）**。

同时沿用阶段 17 的教训：生成器里抛异常只会**断流**，前端连半截结果都看不到。所以模型调用失败时发一帧 `error`，然后**仍然走到 `finish`**（带上已有的部分结果）。

### 4.2 端点 `main.py`：两个端点，以及一条血的教训

```python
@app.post("/api/computer/run", response_model=ComputerResponse)
def computer_run(req: ComputerRequest) -> ComputerResponse: ...

@app.post("/api/computer/stream")
def computer_stream(req: ComputerRequest) -> StreamingResponse: ...
```

`main.py` 顶部的 import 区长这样（注释不是装饰，是阶段 16 的真实事故）：

```python
import vision   # noqa: F401  端点函数体里用到；漏了它 import main 照样成功，首次调用才 500
import voice    # noqa: F401  同上：这行少了，页面点「开始」才会 500
import computer # noqa: F401  同上
```

**为什么这几行必须有注释**：阶段 16 真的漏写过一行 `import vision`。当时的四项验收**全部通过** —— `import main` 成功、静态测试 11/11 绿、`vue-tsc` 0 错、`vite build` 通过 —— 因为 Python 的**函数体全局名在「调用时」才解析**，导入阶段只编译不解析。bug 完整活到了用户第一次点「开始解读」才炸成 `NameError`。

从那以后，每个阶段的标准验收里多了一道**零依赖的「未定义全局」检查器**（用 `dis` 扫函数体的 `LOAD_GLOBAL`，按 `co_filename` 过滤避免误报），见 §5。

### 4.3 前端：点击落点叠加层是签名元素

`ComputerConsole.vue` 的核心是一个 `<img>`（屏幕截图）**叠一层 SVG**（元素真值与落点）：

```vue
<div class="screen-wrap" :style="s.aspectStyle.value">
  <img class="screen-img" :src="s.viewShot.value.image" alt="虚拟屏幕截图" />
  <svg class="overlay" :viewBox="viewBox">
    <!-- 元素真值：按钮/输入框真实所在的矩形 + 中心点 -->
    <rect v-for="el in s.screen.value.elements" :x="el.x" :y="el.y" :width="el.w" :height="el.h" class="el-box" />
    <circle v-for="el in s.screen.value.elements" :cx="s.centerOf(el).cx" :cy="s.centerOf(el).cy" r="4" class="el-center" />

    <!-- ⭐ 签名元素：模型的落点。绿=点中，红=点空 -->
    <g v-for="(a, i) in s.clicksOnView.value" :class="{ miss: !a.hit }">
      <line :x1="(a.x ?? 0) - 26" :y1="a.y ?? 0" :x2="(a.x ?? 0) + 26" :y2="a.y ?? 0" class="cross" />
      <line :x1="a.x ?? 0" :y1="(a.y ?? 0) - 26" :x2="a.x ?? 0" :y2="(a.y ?? 0) + 26" class="cross" />
      <circle :cx="a.x ?? 0" :cy="a.y ?? 0" r="19" class="cross-ring" />
    </g>
  </svg>
</div>
```

**为什么这层叠加有意义**：真值框和落点标记画在**同一张图**上，「点偏了」就不再是一句形容词，而是一条肉眼可见的偏差线。再配上一排「回看 step」按钮，可以逐步重放整个操作过程。

左侧是屏幕，右侧是「任务输入 + 确定性读数」面板，下面依次是**动作时间线**（每条都能看到模型收到的反馈 `note`）、**模型自述**、**耗时**。强调色用了酸橙绿 `#a9e34b`（阶段 13 青绿 / 14 琥珀 / 15 紫 / 16 品红 / 17 蔚蓝都不重复）。

---

## 5. 运行验证

### ① 静态冒烟（不需要 Key）

```bash
cd backend && uv run python _t_computer_static.py
```

14 项全绿：

| # | 验证内容 |
|---|---|
| 1 | 纯几何：`element_at` 命中判定、`rect_distance` / `nearest_distance` 偏差测量 |
| 2 | 渲染确定性：同状态渲染出**逐字节相同**的 PNG（18 763 B）；状态一变图就变 |
| 3 | 正常路径：`success=True`、3/3 字段、命中率 100% |
| 4 | ⭐ 协议：`tool` 消息只装文本；8 张截图全走 `user` 多模态消息且 `tool_call` 配对完整 |
| 5 | ⭐ 点空 → 键盘被丢弃：`lostKeystrokes` 计数 |
| 6 | 坐标越界被**钳制**为 `(1023, 0)` 而不是报错 |
| 7 | 危险动作默认被拒且**不改状态**；显式授权后才放行 |
| 8 | 未知工具 / 非法 action / 坏 JSON / 缺参 / 键名错 —— 全部安全降级且流程不崩 |
| 9 | 非 ASCII 被丢弃，但**不算** `lostKeystrokes`（那是「点空」的判据，两件事别混） |
| 10 | 未填完提交被拒；填满后 `Enter` 可提交；`Tab` / `Backspace` 正常 |
| 11 | `screenshot` / `wait` 无副作用（不改状态、不动 `revision`） |
| 12 | 上游中途报错：发 `error` 帧 **且** 仍发 `finish`，`ComputerResponse` 能收下部分结果 |
| 13 | 模型一次工具都不调：0 动作、不崩、评分全 0 且 `error=None`；耗时自洽、blocking 形状完整 |
| 14 | ⭐ 定位精度：中心偏差（正中心 0 / 偏 30px 得 30）；全中时 `avgClickErrorPx=0` 而 `avgCenterOffsetPx>0` |

**其余四项验收**（本项目每阶段固定底线）：

```bash
# 未定义全局检查器（阶段 16 的教训，阶段 17 起纳入标准验收）→ MISSING(main): none
uv run python _check_globals.py
# 实际 import 一次 + 路由数：51 → 53
uv run python -c "import main; print(len(main.app.routes))"
# 前端
node node_modules/vue-tsc/bin/vue-tsc.js -b --force    # 0 错
node node_modules/vite/bin/vite.js build               # ComputerPage 14.88 kB JS + 7.76 kB CSS
# 文档站
node node_modules/vitepress/bin/vitepress.js build     # 18-ComputerUse.html
```

### ② 真实调用（需要 Key）

用内置任务模板跑一次 **`deepseek-flash`**，全部 7 轮动作 + 1 轮收尾共 **8 次模型调用**：

| 读数 | 实测值 |
|---|---|
| `success` | **True** |
| `fieldScore` | **3 / 3**（`ORDER-2026-0917` / `2158.50` / `2026-09-21` 全对） |
| `submitted` | **True**（`status: "Submitted. All three fields filled."`） |
| `steps` | **7**（零重试、零浪费：3 组「点框 + 打字」+ 1 次点提交） |
| `clicks` / `hitClicks` | **4 / 4** |
| `clickHitRate` | **1.0** |
| `avgClickErrorPx` | **0.0**（因为**没有点空** —— 注意这是「没有样本」，不是「零误差」） |
| `avgCenterOffsetPx` | **2.10 px**（最大 3.0 px） |
| `lostKeystrokes` | **0** |
| 渲染耗时 | **486.11 ms**（8 张 → **60.8 ms/张**） |
| 模型耗时 | **8 556.59 ms**（8 次 → **1 069.6 ms/次**） |
| 合计 | **9 044.48 ms** |

**逐次点击的偏差**（元素中心是静态真值，落点坐标来自那次真实运行）：

| 步 | 模型点的坐标 | 落在 | 该元素中心 | 中心偏差 |
|---|---|---|---|---|
| 1 | `(360, 148)` | `field_order` | `(360, 149)` | **1.0 px** |
| 3 | `(360, 236)` | `field_amount` | `(360, 239)` | **3.0 px** |
| 5 | `(360, 326)` | `field_date` | `(360, 329)` | **3.0 px** |
| 7 | `(169, 437)` | `btn_submit` | `(170, 438)` | **1.4 px** |

> ⚠️ **数据来源要说清**：`avgCenterOffsetPx` 这一项是我用引擎里**同一个纯函数**，拿那次真实运行的落点坐标 + 静态元素真值**重算**出来的 —— 因为那次运行发生在加上这个字段之前。落点坐标和元素几何都是真的，函数是确定性的，所以这个数字是可信的；但它**不是**引擎当场吐出来的。

**三个值得注意的地方：**

1. **精度远超预期，而且这本身是个警告。** 平均偏差 2.1 px、最大 3.0 px，而这些目标的**容错半径**是 25–28 px（输入框半高 25 px、按钮半高 28 px）—— 也就是说还有约 **12 倍余量**。这说明模型不是在猜，它确实读懂了版面。但**别把这次当成能力上限**：任务是我刻意设计成「干净、高对比、大目标、单调版式」的，而视觉模型真正的失败模式通常是**整体点错元素**（不是 2px 抖动）。用一次 n=1 的顺利运行去推断 grounding 能力，是过度外推。

2. **真实运行只走了 14 项测试里的 1 项。** 这次没点空、没丢按键、没碰危险按钮、没给越界坐标 —— **所有失败路径都没被真实触发**。这恰好说明静态测试的价值：**它覆盖的正是顺利运行永远碰不到的那片空间。** 拿一次成功的 demo 当验收，是把运气当能力。

3. **模型的自述和它的动作是分离的。** `transcript` 里前 7 轮都是英文（`"I'll start by clicking on the ORDER ID input field."`），最后一轮才切回中文总结。**判断它做没做对，唯一可靠的依据是 `state` 和那几个确定性读数**，不是它说了什么 —— 这是本课从阶段 14 一路贯穿到这里的同一条原则。

---

## 6. 小结

**这一阶段真正的收获，不是「学会了 Computer Use」，而是「知道该向厂商买什么」。**

把 Computer Use 拆成四步之后，会发现只有第 ② 步值钱，而 ①③④ 本课早在阶段 05 / 10 / 16 就建好了。于是「引入 Claude」这个决定，从「必须」变成了「可选 —— 而且是在你需要真实 grounding 精度时才选」。这个判断过程比结论重要：**先测边界，再选方案**（阶段 17 也是同一个动作：先测出 `/audio/*` 是 404，才定下浏览器方案）。

**三个具体的收获：**

1. **安全边界是设计出来的，不是事后加的。** 动作白名单、坐标钳制、危险动作默认拒绝、只改内存 dict —— 这四件事都不是「顺手」，而是 Computer Use 的第一原则（官方要求跑在 Docker/VM 里）。演示「人工确认」这道闸门，比演示「点成功了」更有价值。

2. **「自己能渲染，就能自己量」。** 这是本阶段最可复用的一招：把自己变成环境的**作者**，真值就落到了你手里，主观评价（点得准吗）立刻变成确定性数字（偏差 px）。这和阶段 17 的 `markdownLeft` 是同一种手法 —— **能用一条规则算出来的，就不要问模型。**

3. **指标要在「顺利的那一次」也有意义。** `avgClickErrorPx` 在全部点中时退化成 0，看起来像满分，实际是「没有样本」，甚至和「一次都没点」无法区分。补上 `avgCenterOffsetPx` 之后，两个读数才各自说明一件事。**一个只在失败时才有值的指标，没法用来比较。**

**五个坑（都真实踩过）：**

1. **`role="tool"` 装不了图片。** 新截图必须另起一条 `role="user"` 多模态消息；同时每个 `tool_call` 必须有配对的 `tool` 消息。两件事都要做，顺序也不能反。
2. **`screen_spec()` 少一个键 = 对外契约撒谎。** 这张元素表会**原样经 SSE 发给前端**，而前端 TS 里声明的是 `dangerous: boolean`；`_ELEMENTS` 里只有危险按钮写了这个键，其余元素拿到的是 `undefined`。静态测试把它抓了出来 —— 修法是**在出口处补齐全部字段**（`e.get("label", "")` / `bool(e.get("dangerous", False))`），而不是让前端去容忍 `undefined`。
3. **`Pillow` 是间接依赖。** 它原本只被 `chromadb` 带进虚拟环境，**没有写在 `pyproject.toml` 里**。本阶段要直接用，就必须**显式声明** —— 依赖间接可用 ≠ 可以依赖。
4. **同一文件并发的编辑会互相覆盖。** 本阶段我一次发了多个针对 `computer.py` 的编辑，结果只有一处落地（工具对每一处都报了成功）。**同一个文件的多处修改，永远串行做，改完立刻核对。**
5. **多行语句别走 heredoc。** 用 `bash -c` + heredoc 传 Python 脚本时，续行的多行语句会被吃掉（`SyntaxError`），中文锚点字符串也可能匹配不上。**把补丁脚本写成文件再跑**，并且改完**立刻断言核对**（本阶段的 `screen_spec` 与 `useComputer.ts` 两处重复插入，都是靠断言当场发现的）。

---

## 7. 练习与验收

### 练习

1. **让任务变难，看指标怎么崩。** 把虚拟屏幕改成「两个相邻的小按钮」（各 24×24 px，间距 4 px），让模型在它们之间选择。观察 `avgCenterOffsetPx` 从 2.1 px 涨到哪里、什么时候开始出现 `clickHitRate < 1`、以及 `lostKeystrokes` 是否被触发。**这是本阶段最有价值的一次实验**：同样的模型，环境一变，「看起来没问题」就变成「明显不行」。

2. **给 `_ELEMENTS` 加一个遮挡元素。** 在输入框上方放一个半透明的浮层（后画 → 在上层）。由于 `element_at()` 是**倒序**遍历的，点击会落到浮层上而不是输入框 —— 验证一下「元素真的被挡住了」，并体会 `z-order` 在命中判定里的作用。

3. **把 `screenshot` 从白名单去掉，看看会怎样。** 模型失去主动重看屏幕的能力后，还能不能完成任务？它的自述会不会开始「猜测」屏幕内容？（提示：`SYSTEM_PROMPT` 里第 2 条承诺了「动作执行后你会收到新的屏幕截图」，改动后记得同步改提示词。）

4. **⭐ 把动作执行做成「人工确认」模式。** 在 `allow_dangerous` 之外再加一层：**每一个** `left_click` 都先暂停，等前端点「允许」才执行。这需要在 SSE 协议里加一对帧（`confirm_request` / `confirm_response`），是把它做成真实生产级 Computer Use 的必经一步。

5. **换一个模型，重跑 §5 的逐次点击表。** 如果手上还有别的视觉模型，用同样的 4 次点击对照它的 `avgCenterOffsetPx`。**这才能把「2.1 px」从一个数字变成一个基准。**

### 验收标准

- [ ] `uv run python _t_computer_static.py` —— **14/14 绿**
- [ ] `uv run python _check_globals.py` —— 输出 `MISSING(main): none`，退出码 `0`
- [ ] `uv run python -c "import main; print(len(main.app.routes))"` —— **53** 条
- [ ] `node node_modules/vue-tsc/bin/vue-tsc.js -b --force` —— **0 错**
- [ ] `node node_modules/vite/bin/vite.js build` —— 通过，且产出 `ComputerPage` 分包
- [ ] `node node_modules/vitepress/bin/vitepress.js build` —— 通过，`dist/stages/18-ComputerUse.html` 存在
- [ ] 前端页面能完成一次完整运行：**截图 → 落点叠加 → 分级评分 → 动作时间线**
- [ ] ⭐ 能**不看代码**说出：Computer Use 的四步里，「点哪里」这个判断为什么是唯一需要向厂商买的能力
- [ ] ⭐ 能解释：为什么 `avgClickErrorPx = 0.0` 在「全部点中」时**不能**读作「零误差」；`lostKeystrokes` 又为什么是「定位失败最硬的证据」

---

## 附：与 DESIGN.md 的差异记录

本阶段原本是 DESIGN.md 里唯一的「例外」——原定用 **Claude**（方案 A）。动笔前重新核查后改为**方案 C · DeepSeek 视觉仿制**，理由已同步写入 [`docs/DESIGN.md`](../DESIGN.md)（技术栈基线段落后的变更记录）：

1. `CLAUDE_API_KEY` 为空，且 `CLAUDE_BASE_URL` 指向 Anthropic **原生**端点，而 `llm.py` 只会说 OpenAI 协议 → 走 Claude 等于新增 SDK + 付费 Key + Docker 沙箱；
2. 闭环里**只有第 ② 步绑定厂商**，①③④ 本课已建好；
3. 第 ② 步用阶段 16 已验证的 DeepSeek 视觉即可接上 —— 零新依赖、零新密钥；
4. 「Claude 是唯一例外」在 2026 年已不准确（OpenAI `computer` 工具、Gemini `computer_use` 都可用），但对本项目结论不变：它们各自都不在 Chat Completions 的形状里。

**安全红线不变**：真实 Computer Use 必须跑在沙箱里。本阶段把「被操作的电脑」做成自渲染的虚拟桌面，所有动作只改内存状态，危险动作默认被拒。
