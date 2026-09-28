"""自封装 LLM client（阶段 03 起用；阶段 05 增加工具调用）。

三条设计约定，对应教程「核心概念」一节：

1. **只依赖 OpenAI 的"协议层"**：DeepSeek、智谱、月之暗面等厂商都兼容 OpenAI 的
   `/chat/completions` 协议，因此换模型只需换 `base_url` + `api_key`，业务代码零改动。
2. **对外只暴露少数几个动词**：`chat()` 等全部结果、`stream()` 逐块产出增量文本、
   `chat_message()` 拿完整 message（阶段 05 用它读 tool_calls）。路由层不感知 SDK 细节。
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
