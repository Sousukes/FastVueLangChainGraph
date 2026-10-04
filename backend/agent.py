"""阶段 05 · 工具调用循环（tool loop）。

一轮"带工具的对话"其实是这样的往复：

    ┌────────────┐  messages + tools 说明书   ┌────────┐
    │  我们的代码 │ ─────────────────────────▶ │  模型   │
    │            │ ◀───────────────────────── │        │
    └────────────┘  要么给答案，要么给 tool_calls └────────┘
          │
          │ 有 tool_calls：执行工具，把结果以 role="tool" 追加进 messages，再来一轮
          ▼
      直到模型给出纯文本答案（或触到 max_steps 上限）

模型始终不执行任何代码；它只是"提出请求"。这个循环就是整条链路的心跳。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from _jsonutil import safe_args as _safe_args
from llm import LLMClient, message_to_dict
from tools import execute_tool, tools_schema

SYSTEM_PROMPT = (
    "你是一个会使用工具的研究助手。\n"
    "- 需要精确计算、查询课程笔记、查天气或时间时，调用对应工具，不要凭空编造。\n"
    "- 一次可以调用一个或多个工具；拿到工具结果后再组织最终回答。\n"
    "- 工具返回错误时，读懂错误原因：能修正就重试，不能修正就如实告诉用户。\n"
    "- 不需要工具时直接回答，保持简洁。"
)


def run_tool_loop(
    client: LLMClient,
    question: str,
    model: str | None = None,
    max_steps: int = 4,
    temperature: float = 0.2,
    tools: list[dict] | None = None,
    executor: Callable[[str, str], tuple[Any, str | None]] | None = None,
    system: str | None = None,
) -> dict[str, Any]:
    """跑完一整轮"提问 → 工具调用 → 回答"，并把全过程记录成 trace。

    trace 是本阶段的主角：前端要能把"模型为什么这么答"摊开给用户看。

    三个可注入点（阶段 06 用它们把工具来源换成 MCP）：
      - `tools`：工具说明书，默认用本地注册表；传 MCP 转换来的 schema 即可
      - `executor`：工具执行器 `(name, raw_arguments) -> (result, error)`，
        默认走本地 `execute_tool`；传 MCP 版即可让模型去调远程 server
      - `system`：系统提示，默认是本文件的 SYSTEM_PROMPT
    """
    tools = tools if tools is not None else tools_schema()
    executor = executor or execute_tool

    messages: list[dict] = [
        {"role": "system", "content": system or SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace: list[dict] = []

    for step in range(1, max_steps + 1):
        msg = client.chat_message(messages, model=model, temperature=temperature, tools=tools)
        calls = getattr(msg, "tool_calls", None)

        # 没有 tool_calls = 模型认为可以直接回答，循环结束
        if not calls:
            return {
                "answer": msg.content or "",
                "trace": trace,
                "steps": step,
                "model": model or client.model,
                "exhausted": False,
            }

        # 1) 先把"模型的这轮话"（含 tool_calls）原样写回历史——顺序不能反
        messages.append(message_to_dict(msg))

        # 2) 逐个执行工具，并把结果作为 role="tool" 追加
        for call in calls:
            t0 = time.perf_counter()
            result, error = executor(call.function.name, call.function.arguments)
            ms = round((time.perf_counter() - t0) * 1000, 1)

            trace.append(
                {
                    "step": step,
                    "tool": call.function.name,
                    "arguments": _safe_args(call.function.arguments),
                    "result": result,
                    "error": error,
                    "ms": ms,
                }
            )

            # 工具结果必须以字符串形式回传；错误也一样回传（让模型有机会自纠）
            if error is not None:
                content = json.dumps({"error": error}, ensure_ascii=False)
            elif isinstance(result, str):
                content = result  # 已经是文本（如 MCP server 返回的 content）就直接用
            else:
                content = json.dumps(result, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    # 超出步数上限：把最后一轮的"半成品"交出去，让前端提示"未收敛"
    return {
        "answer": None,
        "trace": trace,
        "steps": max_steps,
        "model": model or client.model,
        "exhausted": True,
    }


