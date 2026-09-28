"""阶段 06 · MCP Host：把 MCP Server 的能力"翻译"成模型能用的工具。

三者的分工（MCP 规范里的经典划分）：

    Host    = 我们的应用（FastAPI 后端）——它拥有 LLM，也拥有用户
    Client  = Host 内与"某一个 Server"通信的连接器（client.py）
    Server  = 提供能力的独立进程（server.py，跑在子进程里）

Host 要做的翻译工作只有两件：
    1. MCP 的 `tools/list` 结果 → OpenAI 的 `tools` 参数格式（inputSchema 本来就是 JSON Schema，直接搬）
    2. 模型的 `tool_calls` → MCP 的 `tools/call`（拿到 `content[].text` 再喂回模型）

翻译完，阶段 05 的 `run_tool_loop` 一行都不用改——这就是"协议"的价值：
工具来自本地还是来自远端进程，对模型来说没有区别。
"""

from __future__ import annotations

import atexit
import json
from typing import Any, Callable

from agent import run_tool_loop
from llm import LLMClient
from mcpkit.client import MCPClient, MCPError

DEFAULT_SERVER = "research-notes"

HOST_SYSTEM_PROMPT = (
    "你是一个会使用 MCP 工具的研究助手。\n"
    "- 你的工具来自一个独立的 MCP Server（通过 JSON-RPC 调用），而不是本进程内的函数。\n"
    "- 需要查课程笔记、算数、查天气或时间时，调用对应工具，不要凭空编造。\n"
    "- 工具返回错误时读懂原因：能修正就重试，不能修正就如实告诉用户。\n"
    "- 不需要工具时直接回答，保持简洁。"
)


class MCPHost:
    """管理若干个 MCP Server 连接，并把它们的能力暴露给模型。"""

    def __init__(self) -> None:
        self.clients: dict[str, MCPClient] = {}
        self.tool_owner: dict[str, str] = {}  # 工具名 → 提供它的 server（多 server 时的路由表）
        self.catalog: dict[str, dict] = {}  # server → 工具/资源/提示模板清单
        atexit.register(self.close_all)

    # ---------- 连接管理 ----------

    def connect(self, name: str = DEFAULT_SERVER, command: list[str] | None = None) -> MCPClient:
        if name in self.clients:
            return self.clients[name]

        client = MCPClient(name=name, command=command)
        client.initialize()  # 握手：拿协议版本 + 能力清单
        self.clients[name] = client

        tools = client.list_tools()
        for tool in tools:
            self.tool_owner[tool["name"]] = name
        self.catalog[name] = {
            "name": name,
            "protocolVersion": client.protocol_version,
            "serverInfo": client.server_info,
            "capabilities": client.capabilities,
            "tools": tools,
            "resources": client.list_resources(),
            "prompts": client.list_prompts(),
        }
        return client

    def ensure_connected(self, name: str = DEFAULT_SERVER) -> MCPClient:
        return self.clients.get(name) or self.connect(name)

    def close_all(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    # ---------- 展示用：server 目录 ----------

    def describe(self) -> dict:
        self.ensure_connected()
        return {"servers": list(self.catalog.values()), "defaultServer": DEFAULT_SERVER}

    # ---------- 翻译层 ----------

    def openai_tools(self) -> list[dict]:
        """MCP tools → OpenAI tools 参数。inputSchema 就是 JSON Schema，直接搬。"""
        self.ensure_connected()
        out: list[dict] = []
        for server in self.catalog.values():
            for tool in server["tools"]:
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                    }
                )
        return out

    def make_executor(self) -> Callable[[str, str], tuple[Any, str | None]]:
        """返回一个"执行器"，签名与阶段 05 的 execute_tool 一致，可直接注入 run_tool_loop。"""

        def executor(name: str, raw_arguments: str) -> tuple[Any, str | None]:
            owner = self.tool_owner.get(name)
            if owner is None:
                return None, f"没有任何 MCP server 提供工具 {name}"
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as e:
                return None, f"arguments 不是合法 JSON：{e}"

            try:
                result = self.clients[owner].call_tool(name, arguments)
            except MCPError as e:
                return None, f"MCP 调用失败：{e}"

            text = "\n".join(
                part.get("text", "")
                for part in result.get("content", [])
                if part.get("type") == "text"
            )
            # MCP 用 isError 表达"工具业务失败"，同样当数据回传给模型
            if result.get("isError"):
                return None, text
            return text, None

        return executor

    # ---------- 跑一轮 ----------

    def run(
        self,
        llm: LLMClient,
        question: str,
        model: str | None = None,
        max_steps: int = 4,
    ) -> dict:
        self.ensure_connected()
        cursor = {name: len(c.log) for name, c in self.clients.items()}

        outcome = run_tool_loop(
            llm,
            question,
            model=model,
            max_steps=max_steps,
            tools=self.openai_tools(),
            executor=self.make_executor(),
            system=HOST_SYSTEM_PROMPT,
        )

        # 只回传本次请求新增的协议报文——前端要把它当"证据"展示
        protocol: list[dict] = []
        for name, client in self.clients.items():
            for entry in client.log[cursor[name] :]:
                protocol.append({"server": name, **entry})
        outcome["protocol"] = protocol
        return outcome


_host: MCPHost | None = None


def get_host() -> MCPHost:
    global _host
    if _host is None:
        _host = MCPHost()
    return _host
