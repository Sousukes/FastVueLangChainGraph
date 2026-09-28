"""阶段 06 · 一个手写的最小 MCP Server（JSON-RPC 2.0 over stdio）。

为什么手写而不是直接用官方 SDK？
    因为 MCP 的全部"魔法"就是这份文件里的 200 行：
    读一行 JSON → 看 method 分发 → 写一行 JSON 回去。
    看完你再用官方 SDK，就知道它在替你做什么了。

三条铁律（新手最常踩）：
    1. **stdout 是协议通道**，只能输出 JSON-RPC 报文。任何 print 调试都会污染协议，
       所以本文件的日志一律写 stderr（见 log()）。
    2. **通知（notification）没有 id，必须不回应**。有 id 才是请求，才需要 result。
    3. **MCP server 完全不认识 LLM**：它不知道 DeepSeek、不知道 prompt，
       只认 method + params。正因如此，同一个 server 能被 Claude Desktop、Cursor、
       以及本课程的应用同时复用。

目录名为什么叫 `mcpkit` 而不是 `mcp`？
    避免与官方 `pip install mcp` 的包名冲突——本项目里 backend/ 排在 sys.path 首位，
    若目录叫 mcp，装完官方 SDK 后 `import mcp` 会解析到本地目录，非常难排查。

启动方式（由 client 以子进程拉起）：
    python backend/mcpkit/server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

# 让本文件能 import 到上层 backend/ 里的 tools.py（server 进程的工作目录不确定）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import TOOLS, all_notes, execute_tool  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "research-notes", "version": "0.1.0"}


def log(*parts: Any) -> None:
    """调试日志走 stderr——stdout 是协议通道，绝不能污染。"""
    print("[mcp-server]", *parts, file=sys.stderr, flush=True)


def send(message: dict) -> None:
    """把一条 JSON-RPC 报文写出去（一行一个 JSON + 立刻 flush）。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------- 三大原语之一：Tools（可调用的函数） ----------


def _list_tools(_params: dict) -> dict:
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                # MCP 的 inputSchema 就是 JSON Schema——和 OpenAI 的 parameters 同源
                "inputSchema": t.args_model.model_json_schema(),
            }
            for t in TOOLS.values()
        ]
    }


def _call_tool(params: dict) -> dict:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    log("tools/call", name, arguments)

    # 直接复用阶段 05 的执行器：参数校验、错误捕获都已经在里面了
    result, error = execute_tool(name, json.dumps(arguments, ensure_ascii=False))
    if error:
        # 注意：工具失败不是协议错误，而是"成功的响应 + isError: true"
        return {"content": [{"type": "text", "text": error}], "isError": True}
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "isError": False,
    }


# ---------- 三大原语之二：Resources（可读取的数据） ----------

_NOTE_RESOURCES = {
    "research://notes": {
        "name": "课程笔记",
        "description": "本课程各阶段的核心知识点速查",
        "mimeType": "text/markdown",
    },
    "research://glossary": {
        "name": "术语表",
        "description": "LLM 应用开发常见术语与一句话解释",
        "mimeType": "text/markdown",
    },
}

_GLOSSARY = """# 术语表

- **SSE**：Server-Sent Events，`data: ...\\n\\n` 分帧的单向推送协议。
- **Function Calling**：模型产出"调用意图"（name + arguments），执行由应用负责。
- **MCP**：Model Context Protocol，用 JSON-RPC 2.0 把外部能力标准化接入 LLM 应用的协议。
- **Host / Client / Server**：Host 是应用本体（本课程的后端），Client 是 Host 里负责与
  某个 Server 通信的连接器，Server 是提供能力的独立进程/服务。
- **Tool / Resource / Prompt**：MCP 的三大原语——可调用、可读取、可复用。
"""


def _list_resources(_params: dict) -> dict:
    return {
        "resources": [
            {"uri": uri, **meta} for uri, meta in _NOTE_RESOURCES.items()
        ]
    }


def _read_resource(params: dict) -> dict:
    uri = params.get("uri", "")
    if uri == "research://notes":
        text = "# 课程笔记\n\n" + "\n\n".join(f"## {t}\n\n{s}" for t, s in all_notes())
    elif uri == "research://glossary":
        text = _GLOSSARY
    else:
        raise ValueError(f"未知资源：{uri}")

    return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}


# ---------- 三大原语之三：Prompts（可复用的提示模板） ----------


def _list_prompts(_params: dict) -> dict:
    return {
        "prompts": [
            {
                "name": "research/explain",
                "description": "把某个概念讲给初学者听",
                "arguments": [
                    {"name": "topic", "description": "要讲解的主题", "required": True},
                    {"name": "level", "description": "受众水平", "required": False},
                ],
            },
            {
                "name": "research/compare",
                "description": "对比两个概念",
                "arguments": [
                    {"name": "a", "description": "概念 A", "required": True},
                    {"name": "b", "description": "概念 B", "required": True},
                ],
            },
        ]
    }


def _get_prompt(params: dict) -> dict:
    name = params.get("name", "")
    args = params.get("arguments") or {}

    if name == "research/explain":
        topic = args.get("topic", "未知主题")
        level = args.get("level", "零基础初学者")
        text = (
            f"你是一位耐心的技术讲师。请面向{level}解释「{topic}」：\n"
            "1. 先用一个生活化类比说明它解决什么问题；\n"
            "2. 再给出准确的一句话定义；\n"
            "3. 最后列出 3 个最容易混淆的点。"
        )
    elif name == "research/compare":
        text = (
            f"请对比「{args.get('a', 'A')}」与「{args.get('b', 'B')}」：\n"
            "用一张 Markdown 表格比较它们的定位、典型场景、代价；\n"
            "最后给出一句「什么时候用哪个」的建议。"
        )
    else:
        raise ValueError(f"未知提示模板：{name}")

    return {
        "description": f"MCP prompt: {name}",
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


# ---------- 分发 ----------

HANDLERS: dict[str, Callable[[dict], dict]] = {
    "tools/list": _list_tools,
    "tools/call": _call_tool,
    "resources/list": _list_resources,
    "resources/read": _read_resource,
    "prompts/list": _list_prompts,
    "prompts/get": _get_prompt,
    "ping": lambda _params: {},
}


def _initialize(_params: dict) -> dict:
    """握手：双方交换协议版本与能力清单，之后才知道"能问对方什么"。"""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "serverInfo": SERVER_INFO,
    }


def handle(message: dict) -> None:
    method = message.get("method")
    mid = message.get("id")

    # 通知（没有 id）：不回应。典型的是 initialize 之后的 notifications/initialized
    if mid is None:
        log("notification", method)
        return

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": _initialize(message.get("params") or {})})
        return

    handler = HANDLERS.get(method or "")
    if handler is None:
        send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )
        return

    try:
        send({"jsonrpc": "2.0", "id": mid, "result": handler(message.get("params") or {})})
    except Exception as e:  # 业务异常 → JSON-RPC 内部错误码
        send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"},
            }
        )


def main() -> None:
    log("started, protocol", PROTOCOL_VERSION)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                }
            )
            continue
        handle(message)
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
