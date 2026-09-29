"""阶段 18B：Computer Use 的两个「大脑」—— 闭环里**唯一**绑定模型厂商的那一步。

        ① 截图 ──▶ ② 决策 ──▶ ③ 执行 ──▶ ④ 回灌 ──┐
                       ▲ 本模块                      │
                       └─────────────────────────────┘

阶段 18 用 DeepSeek（OpenAI 协议的工具调用）走通了整条闭环，并论证了
「四步里只有第②步是厂商卖的」。本模块把这个论证**做成代码**：
①（渲染）、③（执行 + 沙箱）、④（回灌）留在 `computer.py` 里对 provider 无感知，
只有第②步被抽成可插拔的 brain。

阶段 18B 按 DESIGN.md 的**原计划**补上 Claude（方案 A），但本机没有 Key ——
于是同时要回答两个问题：

  1. 同一个循环怎么容纳两种形状差异极大的模型协议？
  2. 没有 Key 的时候代码该怎么表现？

第 2 问的答案是**显式报错**（`ClaudeNotConfigured`），不是静默降级、也不是假装成功。
静默降级最坏：用户以为跑的是 Claude，实际是 DeepSeek，所有读数都失去了意义。

──────────────────────────────────────────────────────────────────────
两种协议的差别（这就是「只有第②步换」的具体含义）
──────────────────────────────────────────────────────────────────────

| 维度        | DeepSeek（OpenAI 协议）              | Claude（Anthropic Messages）        |
|-------------|--------------------------------------|-------------------------------------|
| 端点        | POST /chat/completions               | POST /v1/messages                   |
| 鉴权        | Authorization: Bearer <key>          | x-api-key + anthropic-version       |
| 工具来源    | **我们自己写 schema**（枚举 5 个动作）| **内置工具**，schema 在模型里       |
| 动作词表    | 我们说了算                           | 模型说了算（mouse_move/scroll/双击…）|
| 坐标形状    | `x` 与 `y` 两个平铺字段              | `coordinate: [x, y]` 一个数组       |
| 按键        | `keys` 字段                          | `text` 字段（action=key 时）        |
| 工具结果    | `role="tool"` 装文本 + **另起一条 user 消息**带图 | `tool_result` 里**直接塞 image 块** |
| 流式        | SSE 分片，tool_calls 按 `index` 拼    | SSE 事件，`input_json_delta` 按块拼 |
| 系统提示词  | 放在 messages 里的 system 角色        | 顶层独立的 `system` 参数            |
| beta        | 无                                    | 必须带 `anthropic-beta` 头          |

⭐ 最值得记的两条：

  · **工具来源**决定了「谁定动作词表」。DeepSeek 那边 schema 由我们写，所以模型
    天生只知道我们支持的 5 个动作；Claude 的 computer 工具是内置的，它会自然地
    说 mouse_move / scroll / double_click —— 而**我们的沙箱白名单会把它挡回去**。
    这不是 bug，这正是真实 Computer Use 的第一原则：**宿主决定自己愿意执行什么**，
    模型必须适应宿主的词表。见 `computer.ALLOWED_ACTIONS`。

  · **工具结果**的形状。OpenAI 协议里 `role="tool"` 只能装文本，装不了图，于是
    阶段 18 不得不「先补 tool 消息、再另起一条 user 多模态消息」；Anthropic 协议
    允许把 image 块直接放进 `tool_result`，一条消息搞定。同一件事，两种协议。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# `tool 版本` 与 `anthropic-beta` 头**必须成对**，配错会直接 400（不会自动降级）。
# 这是接入 Claude Computer Use 时最常见的第一个坑，所以把它写成一张表而不是两处魔法字符串。
CLAUDE_BETA_BY_TOOL: dict[str, str] = {
    "computer_20251124": "computer-use-2025-11-24",
    "computer_20250124": "computer-use-2025-01-24",
    "computer_20241022": "computer-use-2024-10-22",
}

DEFAULT_CLAUDE_TOOL = os.getenv("CLAUDE_COMPUTER_TOOL", "computer_20250124")
DEFAULT_CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
DEFAULT_CLAUDE_BASE_URL = os.getenv("CLAUDE_BASE_URL", "https://api.anthropic.com/v1")
DEFAULT_CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "2048"))

# Messages API 要求一个 API 版本头；这个值长期稳定，与 computer use 的 beta 头是两回事
ANTHROPIC_VERSION = "2023-06-01"

CLAUDE_TOOL_NAME = "computer"


class ClaudeNotConfigured(RuntimeError):
    """没有 CLAUDE_API_KEY 时抛出。

    ⚠️ 刻意**不做**静默降级到 DeepSeek：那会让「我以为在测 Claude」变成假象，
    所有耗时/精度读数都失去意义。路由层把它转成 400 + 明确的配置指引。
    """


class ClaudeConfigError(RuntimeError):
    """配置本身不自洽（例如 tool 版本没有配对的 beta 头）。"""


class ClaudeAPIError(RuntimeError):
    """Anthropic 返回了 4xx/5xx。带上状态码与响应正文片段，便于定位。"""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Anthropic API 返回 {status}：{body[:400]}")


def claude_key() -> str:
    return os.getenv("CLAUDE_API_KEY", "").strip()


def messages_url(base_url: str | None = None) -> str:
    """把 base_url 归一成 `<...>/v1/messages`。

    `.env` 里写的是 `https://api.anthropic.com/v1`，但用户也可能只写到域名 ——
    两种写法都接受，避免「只差一个 /v1」这种毫无信息量的失败。

    ⚠️ 环境变量在**调用时**读，不在导入时固化：否则测试里把 base_url 指向本地
    mock 服务就不生效了（这是个很容易被忽略、但会让"能测的东西"变少的设计细节）。
    """
    base = (base_url or os.getenv("CLAUDE_BASE_URL") or DEFAULT_CLAUDE_BASE_URL).strip().rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/messages"


def claude_tool_spec(tool_version: str | None = None, *, width: int, height: int) -> dict:
    """Anthropic 内置 computer 工具的**定义**。

    注意与 DeepSeek 那边的根本差别：这里**没有 parameters / input_schema**。
    动作的 schema 烧在模型里，我们只声明屏幕尺寸。这就是「内置工具」的含义。
    """
    ver = (tool_version or DEFAULT_CLAUDE_TOOL).strip()
    if ver not in CLAUDE_BETA_BY_TOOL:
        raise ClaudeConfigError(
            f"未知的 computer 工具版本 {ver!r}，可用：{', '.join(CLAUDE_BETA_BY_TOOL)}"
        )
    return {
        "type": ver,
        "name": CLAUDE_TOOL_NAME,
        "display_width_px": width,
        "display_height_px": height,
        "display_number": 1,
    }


def claude_beta_header(tool_version: str | None = None) -> str:
    ver = (tool_version or DEFAULT_CLAUDE_TOOL).strip()
    try:
        return CLAUDE_BETA_BY_TOOL[ver]
    except KeyError as e:
        raise ClaudeConfigError(
            f"工具版本 {ver!r} 没有配对的 beta 头，可用：{', '.join(CLAUDE_BETA_BY_TOOL)}"
        ) from e


# ---------------------------------------------------------------------------
# 小工具：data URL ←→ Anthropic 的 base64 source
# ---------------------------------------------------------------------------


def split_data_url(data_url: str) -> tuple[str, str]:
    """`data:image/png;base64,AAAA` → `("image/png", "AAAA")`。

    Anthropic 的图像块要 `media_type` 与 `data` 分开给，而我们在前端/内部一律传
    data URL（阶段 16 起就是），所以这里做一次转换。
    """
    head, _, payload = data_url.partition(",")
    if not payload:
        # 没有前缀：当成裸 base64 处理，媒体类型按 PNG 兜底
        return "image/png", data_url
    media = "image/png"
    if head.startswith("data:"):
        media = head[len("data:") :].split(";")[0] or "image/png"
    return media, payload


def image_block(data_url: str) -> dict:
    media, b64 = split_data_url(data_url)
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}


# ---------------------------------------------------------------------------
# 大脑基类
# ---------------------------------------------------------------------------


class BaseBrain:
    """一个 brain 只需要回答三个问题：怎么开场、怎么要决策、怎么回灌。

    刻意不用 abc.ABC —— 项目里这类「三个方法的小协议」一律用鸭子类型，
    少一层抽象，也方便测试时直接塞假对象。
    """

    name = "?"
    model = "?"
    #: 给前端展示的一句话（这个大脑的协议特征）
    protocol = ""

    # ---- ① 开场：初始 messages ----
    def open(self, task_text: str, first_shot: str) -> list[dict]:
        raise NotImplementedError

    # ---- ② 决策：产出 {"delta": ...} 若干次，最后产出一次 {"decision": {...}} ----
    def decide(self, messages: list[dict]) -> Iterator[dict]:
        raise NotImplementedError

    # ---- ④ 回灌：动作执行完之后追加哪些消息 ----
    def feedback(self, step: int, notes: list[tuple[str | None, str]], shot: str) -> list[dict]:
        raise NotImplementedError

    # ---- 给端点的诊断信息 ----
    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "protocol": self.protocol}


# ---------------------------------------------------------------------------
# 大脑 A：DeepSeek（OpenAI 协议的工具调用）—— 阶段 18 的默认大脑
# ---------------------------------------------------------------------------


class DeepSeekBrain(BaseBrain):
    """阶段 18 的行为原样搬过来，只是从 `run_computer` 里挪到了这里。

    为什么值得挪：`_result_messages` 那种「tool 消息装文本 + 另起 user 消息带图」
    的写法，是 **OpenAI 协议特有**的细节。它待在通用循环里会让人误以为
    Computer Use 本身需要这么绕 —— 其实只是这一种协议的形状而已。
    """

    name = "deepseek"
    protocol = "OpenAI /chat/completions + 自写工具 schema + role=tool 回传"

    def __init__(
        self,
        client: Any,
        *,
        tool_spec: dict,
        system_prompt: str,
        model: str | None = None,
        temperature: float = 0.2,
        tool_name: str = "computer",
    ) -> None:
        self.client = client
        self.tool_spec = tool_spec
        self.system_prompt = system_prompt
        self.tool_name = tool_name
        self.model = model or client.model
        self.temperature = temperature

    # ---- ① ----
    def open(self, task_text: str, first_shot: str) -> list[dict]:
        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": task_text},
                    {"type": "image_url", "image_url": {"url": first_shot}},
                ],
            },
        ]

    # ---- ② ----
    def decide(self, messages: list[dict]) -> Iterator[dict]:
        content = ""
        message: dict | None = None
        for ev in self.client.stream_message(
            messages,
            model=self.model,
            temperature=self.temperature,
            tools=[self.tool_spec],
            tool_choice="auto",
        ):
            if "delta" in ev:
                content += ev["delta"]
                yield {"delta": ev["delta"]}
            elif "message" in ev:
                message = ev["message"]

        if message is None:
            yield {"decision": None}
            return

        actions: list[dict] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            if name != self.tool_name:
                actions.append(
                    {
                        "call_id": call.get("id"),
                        "action": None,
                        "args": {},
                        "error": f"没有名为 {name!r} 的工具，请使用 {self.tool_name}。",
                    }
                )
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                # 模型偶尔吐出坏 JSON —— 当成空参数，让白名单去拒绝，别抛
                args = {}
            actions.append(
                {"call_id": call.get("id"), "action": args.get("action"), "args": args}
            )

        yield {
            "decision": {
                "narration": (content or "").strip(),
                "assistant": message,
                "actions": actions,
            }
        }

    # ---- ④ ----
    def feedback(self, step: int, notes: list[tuple[str | None, str]], shot: str) -> list[dict]:
        """OpenAI 协议的两条消息：先补齐 tool 结果（只能装文本），再用 user 消息带上新截图。

        ⚠️ 两件事都得做，顺序也不能反：每个 tool_call 必须有配对的 tool 消息，
        否则 API 直接报错；而图片又塞不进 tool 消息，只能另起一条。
        """
        msgs: list[dict] = [
            {"role": "tool", "tool_call_id": call_id, "content": note}
            for call_id, note in notes
        ]
        msgs.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"第 {step} 轮动作已执行完毕。这是**执行后**的新屏幕截图："},
                    {"type": "image_url", "image_url": {"url": shot}},
                ],
            }
        )
        return msgs


# ---------------------------------------------------------------------------
# 大脑 B：Claude（Anthropic Messages + 内置 computer 工具）—— 方案 A
# ---------------------------------------------------------------------------

CLAUDE_SYSTEM_PROMPT = """你正在通过 computer 工具操作一台**虚拟**电脑（1024x768，左上角为原点，x 向右、y 向下）。

屏幕上是一个「Refund Request」表单。规则：
1. 每次只发**一个**动作；动作执行后你会收到新的屏幕截图，再决定下一步。
2. 键盘只会打进**被聚焦**的输入框 —— 所以要填某个框，必须先点击它。
3. 敲入的文本必须是 ASCII。
4. 屏幕上有一根品红色十字准星，标出你上一次点击落在哪里，可用它自查有没有点偏。
5. RESET ALL 是危险按钮，未经人工确认会被沙箱直接拒绝，不要浪费步骤去点它。

⚠️ 这台机器上的宿主**只愿意执行以下动作**，其它动作会被直接驳回（这是真实
Computer Use 的常态：动作词表由宿主决定，不由模型决定）：

    screenshot / left_click / type / key / wait

`left_click` 的坐标用 `coordinate`（如 [360, 148]）；`key` 的键名放在 `text` 里
（如 "Return" 提交、"Tab" 切换焦点、"BackSpace" 删一个字符）。
宿主不执行 mouse_move / scroll / 双击 / 拖拽 —— 请直接用 left_click 表达你的意图。

确认任务全部完成后，**不要再调用工具**，用一句中文说明结果。
"""


class ClaudeBrain(BaseBrain):
    """用**原生 HTTP** 直接说 Anthropic Messages 协议，不引入 anthropic SDK。

    为什么不装 SDK：本机 `anthropic` 未安装、也没有 Key，装一个 SDK 只为写一次请求
    并不划算；而 `httpx` 本来就在依赖里（openai SDK 就基于它）。
    更实际的理由是**教学**：把 `anthropic-beta` 头、内置工具定义、SSE 事件序列
    都摆在明面上，比藏在 SDK 后面更容易看清「Computer Use 到底是什么形状」。
    """

    name = "claude"
    protocol = "Anthropic /v1/messages + 内置 computer 工具 + anthropic-beta 头 + tool_result 内置图"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        width: int,
        height: int,
        tool_version: str | None = None,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
    ) -> None:
        key = (api_key or claude_key()).strip()
        if not key:
            raise ClaudeNotConfigured(
                "未检测到 CLAUDE_API_KEY：请在 backend/.env 里填入 Claude 密钥后重试"
                "（阶段 18B 说明见 docs/stages/18B-ComputerUseClaude.md）"
            )
        self._key = key
        self.url = messages_url(base_url)
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.temperature = temperature
        self.tool_version = (tool_version or DEFAULT_CLAUDE_TOOL).strip()
        self.beta = claude_beta_header(self.tool_version)  # 顺带校验成对
        self.tool = claude_tool_spec(self.tool_version, width=width, height=height)
        self.max_tokens = max_tokens or DEFAULT_CLAUDE_MAX_TOKENS
        self.system_prompt = system_prompt or CLAUDE_SYSTEM_PROMPT
        self.timeout = float(os.getenv("CLAUDE_TIMEOUT", "120"))

    # ---- ① ----
    def open(self, task_text: str, first_shot: str) -> list[dict]:
        # Anthropic 没有 system 角色：系统提示词走顶层 `system` 参数（见 _payload）
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": task_text},
                    image_block(first_shot),
                ],
            }
        ]

    def _headers(self) -> dict:
        return {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            # ⚠️ 这个头漏了 → 400，且报错信息不会告诉你「少了个 beta 头」
            "anthropic-beta": self.beta,
            "content-type": "application/json",
        }

    def _payload(self, messages: list[dict]) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,  # Messages API 的必填项，漏了直接 422
            "system": self.system_prompt,
            "tools": [self.tool],
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
        }

    # ---- ② ----
    def decide(self, messages: list[dict]) -> Iterator[dict]:
        """发一次 Messages 请求，边收 SSE 边吐文本增量，最后给出归一化后的决策。"""
        text_by_index: dict[int, str] = {}
        tools_by_index: dict[int, dict] = {}
        stop_reason: str | None = None

        with httpx.Client(timeout=self.timeout) as http:
            with http.stream(
                "POST", self.url, headers=self._headers(), json=self._payload(messages)
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise ClaudeAPIError(resp.status_code, resp.text)
                for line in resp.iter_lines():
                    ev = _parse_sse_line(line)
                    if ev is None:
                        continue
                    kind = ev.get("type")

                    if kind == "content_block_start":
                        idx = int(ev.get("index", 0))
                        cb = ev.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            tools_by_index[idx] = {
                                "id": cb.get("id"),
                                "name": cb.get("name") or CLAUDE_TOOL_NAME,
                                "start_input": cb.get("input") if isinstance(cb.get("input"), dict) else {},
                                "partial": "",
                            }
                        elif cb.get("type") == "text":
                            text_by_index.setdefault(idx, "")

                    elif kind == "content_block_delta":
                        idx = int(ev.get("index", 0))
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            piece = d.get("text") or ""
                            if piece:
                                text_by_index[idx] = text_by_index.get(idx, "") + piece
                                yield {"delta": piece}
                        elif d.get("type") == "input_json_delta":
                            # ⚠️ 工具入参是**逐片吐**的（模型一个 token 一个 token 生成 JSON），
                            #    必须按 index 累加拼接，不能覆盖 —— 同阶段 10 拼 tool_calls 分片。
                            slot = tools_by_index.setdefault(
                                idx, {"id": None, "name": CLAUDE_TOOL_NAME, "start_input": {}, "partial": ""}
                            )
                            slot["partial"] += d.get("partial_json") or ""

                    elif kind == "message_delta":
                        stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason

                    elif kind == "error":
                        err = ev.get("error") or {}
                        raise ClaudeAPIError(
                            int(err.get("code") or 500) if str(err.get("code") or "").isdigit() else 500,
                            str(err.get("message") or err),
                        )

        # ---- 按 index 顺序还原 assistant 的 content 块（回放时必须原样带回）----
        blocks: list[dict] = []
        for idx in sorted(set(text_by_index) | set(tools_by_index)):
            if idx in text_by_index and text_by_index[idx]:
                blocks.append({"type": "text", "text": text_by_index[idx]})
            slot = tools_by_index.get(idx)
            if slot is None:
                continue
            raw = slot["partial"].strip()
            if raw:
                try:
                    inp = json.loads(raw)
                except json.JSONDecodeError:
                    inp = dict(slot["start_input"])
            else:
                inp = dict(slot["start_input"])
            if not isinstance(inp, dict):
                inp = {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": slot["id"],
                    "name": slot["name"],
                    # input 必须原样回放，否则下一轮 API 会认为工具调用与结果不匹配
                    "input": inp,
                }
            )

        narration = "".join(text_by_index[k] for k in sorted(text_by_index)).strip()
        actions: list[dict] = []
        for b in blocks:
            if b["type"] != "tool_use":
                continue
            if b["name"] != CLAUDE_TOOL_NAME:
                actions.append(
                    {
                        "call_id": b["id"],
                        "action": None,
                        "args": {},
                        "error": f"没有名为 {b['name']!r} 的工具，请使用 {CLAUDE_TOOL_NAME}。",
                    }
                )
                continue
            args = claude_input_to_args(b["input"])
            actions.append({"call_id": b["id"], "action": args.get("action"), "args": args})

        if not blocks:
            yield {"decision": None}
            return

        yield {
            "decision": {
                "narration": narration,
                "assistant": {"role": "assistant", "content": blocks},
                "actions": actions,
                "stopReason": stop_reason,
            }
        }

    # ---- ④ ----
    def feedback(self, step: int, notes: list[tuple[str | None, str]], shot: str) -> list[dict]:
        """Anthropic 只需要**一条** user 消息：每个 tool_result 里直接把截图塞进去。

        ⚠️ 两个细节：
          1. `tool_result` 块必须排在同一条 user 消息的**最前面**，文字只能跟在后面；
          2. 图片放在 tool_result 的 content 数组里（computer use 明确允许），
             这跟 OpenAI 协议「tool 消息只能装文本、必须另起一条 user 消息」正好相反。
        """
        results = [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": [
                    {"type": "text", "text": note},
                    image_block(shot),
                ],
            }
            for call_id, note in notes
        ]
        return [
            {
                "role": "user",
                "content": [
                    *results,
                    {"type": "text", "text": f"（第 {step} 轮动作已执行完毕，上面每张截图都是执行后的新屏幕）"},
                ],
            }
        ]

    def describe(self) -> dict:
        return {
            **super().describe(),
            "toolVersion": self.tool_version,
            "betaHeader": self.beta,
            "endpoint": self.url,
        }


def claude_input_to_args(inp: dict) -> dict:
    """把 Claude 的动作入参**归一化**成我们内部 `_step` 认的字段名。

    三处不一致（都是真实协议差异，不是笔误）：

      · 坐标：Claude 给 `coordinate: [x, y]` 一个数组；我们内部是 `x` / `y` 两个字段。
      · 按键：Claude 把键名放在 `text` 里（action=key）；我们内部叫 `keys`。
      · 其余：scroll_direction / duration / start_coordinate 等我们不认，
        原样留着即可 —— 白名单会拒绝它，并把可用动作列表回给模型让它自己改。
    """
    action = str(inp.get("action") or "")
    args: dict[str, Any] = {"action": action}

    coord = inp.get("coordinate")
    if isinstance(coord, (list, tuple)) and len(coord) >= 2:
        args["x"], args["y"] = coord[0], coord[1]
    # 有些版本会用 start_coordinate / 单点 cursor，这里不猜，交给白名单拒绝

    text = inp.get("text")
    if isinstance(text, str) and text:
        if action == "key":
            args["keys"] = text
        else:
            args["text"] = text

    return args


def _parse_sse_line(line: str) -> dict | None:
    """从一行 SSE 里取出 JSON 负载。

    只认 `data:` 行就够了：Anthropic 的每个事件负载里都带 `type` 字段，
    所以不必另外维护 `event:` 行与 JSON 的对应关系（少一处能写错的地方）。
    """
    if not line:
        return None
    if not line.startswith("data:"):
        return None  # event: / id: / 注释 行一律略过
    raw = line[len("data:") :].strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# 工厂 + 可用性
# ---------------------------------------------------------------------------


def make_brain(
    provider: str,
    *,
    client: Any = None,
    model: str | None = None,
    temperature: float = 0.2,
    width: int,
    height: int,
    deepseek_tool_spec: dict | None = None,
    deepseek_system_prompt: str | None = None,
    deepseek_tool_name: str = "computer",
) -> BaseBrain:
    """按 provider 造大脑。

    `auto` 的取舍：**有 Claude Key 就用 Claude，否则用 DeepSeek**。
    这样默认行为始终可用（零密钥可跑），而配了 Key 的人一进来就自动走方案 A。
    """
    want = (provider or "auto").strip().lower()
    if want not in ("auto", "deepseek", "claude"):
        raise ValueError(f"未知的 provider {provider!r}，可用：auto / deepseek / claude")

    if want == "claude" or (want == "auto" and claude_key()):
        return ClaudeBrain(
            model=model, temperature=temperature, width=width, height=height
        )

    if client is None:
        raise ClaudeNotConfigured(
            "provider=auto 且未配置 CLAUDE_API_KEY，但也没有可用的 DeepSeek client。"
        )
    return DeepSeekBrain(
        client,
        tool_spec=deepseek_tool_spec or {},
        system_prompt=deepseek_system_prompt or "",
        model=model,
        temperature=temperature,
        tool_name=deepseek_tool_name,
    )


def preflight(provider: str, **kwargs: Any) -> dict:
    """在**开流之前**把配置类错误暴露出来。

    做法就是真的造一次大脑再丢掉 —— 而不是另写一套校验规则。
    理由：两套规则迟早会漂移，于是出现「校验说没问题、真跑起来才炸」；
    而大脑的构造函数本来就是纯配置（不发网络请求），代价可以忽略。
    """
    brain = make_brain(provider, **kwargs)
    return brain.describe()


def provider_infos(*, width: int, height: int) -> list[dict]:
    """给前端看的「两个大脑现在能不能用」。

    ⭐ 为什么值得单做一个端点：没有 Key 时最糟的体验是**点了才发现不能用**。
    前端进页面就能拿到 `available=false` + 原因，直接把按钮置灰并说明怎么配。
    """
    claude = bool(claude_key())
    try:
        tool_version = DEFAULT_CLAUDE_TOOL
        beta = claude_beta_header(tool_version)
        cfg_err = None
    except ClaudeConfigError as e:
        tool_version, beta, cfg_err = DEFAULT_CLAUDE_TOOL, "", str(e)

    return [
        {
            "provider": "deepseek",
            "label": "DeepSeek（仿制）",
            "protocol": "OpenAI /chat/completions + 自写工具 schema",
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            "available": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
            "reason": None
            if os.getenv("DEEPSEEK_API_KEY", "").strip()
            else "未配置 DEEPSEEK_API_KEY",
            "keyEnv": "DEEPSEEK_API_KEY",
            "endpoint": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "toolVersion": None,
            "betaHeader": None,
        },
        {
            "provider": "claude",
            "label": "Claude（方案 A · 原生 Computer Use）",
            "protocol": "Anthropic /v1/messages + 内置 computer 工具",
            "model": DEFAULT_CLAUDE_MODEL,
            "available": claude and cfg_err is None,
            "reason": None
            if (claude and cfg_err is None)
            else (cfg_err or "未配置 CLAUDE_API_KEY（在 backend/.env 填入即可启用）"),
            "keyEnv": "CLAUDE_API_KEY",
            "endpoint": messages_url(),
            "toolVersion": tool_version,
            "betaHeader": beta,
        },
    ]
