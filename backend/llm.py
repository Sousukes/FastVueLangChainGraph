"""自封装 LLM client（阶段 03 起用；阶段 05 增加工具调用；阶段 10 增加流式工具调用）。

三条设计约定，对应教程「核心概念」一节：

1. **只依赖 OpenAI 的"协议层"**：DeepSeek、智谱、月之暗面等厂商都兼容 OpenAI 的
   `/chat/completions` 协议，因此换模型只需换 `base_url` + `api_key`，业务代码零改动。
2. **对外只暴露少数几个动词**：`chat()` 等全部结果、`stream()` 逐块产出增量文本、
   `chat_message()` 拿完整 message（阶段 05 用它读 tool_calls）、
   `stream_message()` 流式拿完整 message（阶段 10 的 ReAct 靠它"看着 agent 想"）。
   路由层不感知 SDK 细节。
3. **密钥只在服务端**：从环境变量读取，绝不写进代码、绝不返回给前端。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# 默认模型：课程 00–17 统一使用 DeepSeek-Flash（见 docs/DESIGN.md 二·技术栈基线）
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
DEFAULT_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 本 client 接受的角色，与 OpenAI 协议一致
Role = str  # 'system' | 'user' | 'assistant'


class LLMNotConfiguredError(RuntimeError):
    """未配置 API Key 时抛出，路由层会转成友好提示。"""


class LLMClient:
    """极薄的一层封装：把"发消息给模型"收敛成两个方法。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.base_url = base_url or DEFAULT_BASE_URL
        key = api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise LLMNotConfiguredError(
                "未检测到 DEEPSEEK_API_KEY：请复制 backend/.env.example 为 .env 并填入密钥"
            )
        self._client = OpenAI(api_key=key, base_url=self.base_url)

    def _payload(
        self,
        messages: list[dict],
        model: str | None,
        temperature: float,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> dict:
        payload: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        # 阶段 04 用：DeepSeek/DeepSeek-V3 支持 {"type": "json_object"}；
        # 其它厂商不识别时 SDK 会抛 400，由 extract 流程做降级重试。
        if response_format:
            payload["response_format"] = response_format
        # 阶段 05 用：把"工具说明书"交给模型。注意——模型只会回 tool_calls（调用意图），
        # 真正执行由我们自己的代码负责（见 tools.py）。
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        return payload

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.7,
        response_format: dict | None = None,
    ) -> str:
        """一次性拿到完整回复（非流式）。可选 response_format：见 _payload 注释。"""
        resp = self._client.chat.completions.create(
            **self._payload(messages, model, temperature, response_format),
            stream=False,
        )
        return resp.choices[0].message.content or ""

    def chat_message(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ):
        """返回**完整 message 对象**（可能带 tool_calls）。

        阶段 05 的工具循环必须拿到原始 message：只有它同时带着 content 与 tool_calls，
        且 tool_calls 里的 id 要与后续 role="tool" 的消息一一对应。
        """
        resp = self._client.chat.completions.create(
            **self._payload(messages, model, temperature, None, tools, tool_choice),
            stream=False,
        )
        return resp.choices[0].message

    def stream(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.7,
    ) -> Iterator[str]:
        """逐块产出"增量文本"（delta），生成器语义：来一块吐一块。"""
        resp = self._client.chat.completions.create(
            **self._payload(messages, model, temperature),
            stream=True,
        )
        for chunk in resp:
            # 流式响应里每一片都装在 choices[0].delta.content，可能为 None（如首片的 role）
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    def stream_message(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> Iterator[dict]:
        """流式拿**完整 message**：文本增量逐块产出，tool_calls 分片在内部拼装。

        产出两种事件：
            {"delta": "增量文本"}       —— 给前端实时显示"它正在想什么"
            {"message": <完整 message>} —— 流结束时给一次，tool_calls 已拼装完整

        **为什么需要这个方法？** 前两个方法各差一半：
          - 阶段 03 的 `stream()` 只能流式拿**纯文本**，读不到 tool_calls；
          - 阶段 05 的 `chat_message()` 能读到 tool_calls，但必须**等整轮跑完**。

        ReAct 的演示价值恰恰在"**看着它想**"——既要 tool_calls，又要实时增量，
        所以只能自己拼分片。这也是整条链路上最容易写错的一段：

        ⚠️ **`tool_calls` 的分片不是"每个 chunk 一个完整调用"**，而是
        按 `index` 分组的**字段级碎片**——第一个 chunk 可能只给了 `id` 和 `name`，
        `arguments` 要跨好几个 chunk 才拼得完整（模型是一个 token 一个 token 吐 JSON 的）。
        所以必须按 `index` 建槽位、逐字段**累加**，而不是覆盖。
        """
        resp = self._client.chat.completions.create(
            **self._payload(messages, model, temperature, None, tools, tool_choice),
            stream=True,
        )

        content = ""
        # index -> {"id":..., "name":..., "arguments":...}
        slots: dict[int, dict[str, str]] = {}

        for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            if delta.content:
                content += delta.content
                yield {"delta": delta.content}

            for frag in getattr(delta, "tool_calls", None) or []:
                # `index` 是唯一可靠的槽位键：id / name 可能只在第一片出现
                slot = slots.setdefault(frag.index, {"id": "", "name": "", "arguments": ""})
                if frag.id:
                    slot["id"] = frag.id
                fn = getattr(frag, "function", None)
                if fn is not None:
                    if fn.name:
                        slot["name"] += fn.name
                    if fn.arguments:
                        slot["arguments"] += fn.arguments

        yield {
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {"name": slot["name"], "arguments": slot["arguments"]},
                    }
                    for _, slot in sorted(slots.items())
                ],
            }
        }


def message_to_dict(msg) -> dict:
    """把 SDK 的 message 对象转成可以塞回 `messages` 的普通 dict。

    关键点：`tool_calls` 必须**原样回传**（含每个 call 的 id），因为随后的
    `role="tool"` 消息要靠 `tool_call_id` 与它配对——少了 id，模型就无法把
    "工具返回的结果"对应回"我当初发起的那次调用"。
    """
    out: dict = {"role": "assistant", "content": msg.content or ""}
    calls = getattr(msg, "tool_calls", None)
    if calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            }
            for c in calls
        ]
    return out
