"""阶段 06 · MCP Client：Host 里负责"与某个 Server 通信"的那一层。

它做的事很少，但每一件都是协议的关键：

    拉起 server 子进程 → 往它的 stdin 写 JSON-RPC 请求 → 从它的 stdout 读回响应

两个工程细节值得注意：

1. **读 stdout 必须放在独立线程**。主线程要能带超时地等响应，而管道读是阻塞的；
   用一个队列把"读到的每一行"转交给主线程，是最简单可靠的跨平台做法
   （Windows 上 select 不支持管道，别指望它）。
2. **server 的 stderr 要单独收走**。它是 server 的调试日志通道，
   我们既不污染协议，也不让它把管道写满后卡死。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

SERVER_PATH = Path(__file__).resolve().parent / "server.py"


class MCPError(RuntimeError):
    """协议层或传输层错误。"""


class MCPClient:
    def __init__(
        self,
        name: str = "research-notes",
        command: list[str] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.server_info: dict = {}
        self.protocol_version: str = ""
        self.capabilities: dict = {}
        # 协议报文日志：前端要把它摊开给用户看，这是本阶段的教学主角
        self.log: list[dict] = []
        self.server_log: list[str] = []

        cmd = command or [sys.executable, str(SERVER_PATH)]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._out: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0

        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    # ---------- 后台泵 ----------

    def _pump_stdout(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._out.put(line)
        self._out.put(None)  # 进程结束标记

    def _pump_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self.server_log.append(line.rstrip("\n"))

    # ---------- 收发 ----------

    def _record(self, direction: str, payload: Any) -> None:
        self.log.append({"dir": direction, "payload": payload})

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        return self._exchange(message)

    def _notify(self, method: str, params: dict | None = None) -> None:
        """通知：有 method 没有 id，server 不回应。"""
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._record("→", message)
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _exchange(self, message: dict) -> dict:
        self._record("→", message)
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

        while True:
            try:
                line = self._out.get(timeout=self.timeout)
            except queue.Empty as e:
                raise MCPError(f"等待 {message['method']} 响应超时（{self.timeout}s）") from e
            if line is None:
                raise MCPError("MCP server 已退出（stdout 关闭）")
            line = line.strip()
            if not line:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非协议行（不该出现，但别让它炸掉客户端）

            # 只接受与自己 id 配对的响应
            if response.get("id") != message["id"]:
                self._record("←(stale)", response)
                continue

            self._record("←", response)
            if "error" in response:
                err = response["error"]
                raise MCPError(f"{err.get('code')}: {err.get('message')}")
            return response.get("result") or {}

    # ---------- 生命周期 ----------

    def initialize(self) -> dict:
        """握手：交换协议版本与能力清单。之后 client 才知道 server 能提供什么。"""
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"roots": {}, "sampling": {}},
                "clientInfo": {"name": "fastvue-research-assistant", "version": "0.1.0"},
            },
        )
        self.protocol_version = result.get("protocolVersion", "")
        self.capabilities = result.get("capabilities", {})
        self.server_info = result.get("serverInfo", {})
        # 规范要求：initialize 之后必须发一条 initialized 通知，握手才算完成
        self._notify("notifications/initialized")
        return result

    def ping(self) -> dict:
        return self._request("ping")

    # ---------- 三大原语 ----------

    def list_tools(self) -> list[dict]:
        return self._request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def list_resources(self) -> list[dict]:
        return self._request("resources/list").get("resources", [])

    def read_resource(self, uri: str) -> dict:
        return self._request("resources/read", {"uri": uri})

    def list_prompts(self) -> list[dict]:
        return self._request("prompts/list").get("prompts", [])

    def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        return self._request("prompts/get", {"name": name, "arguments": arguments or {}})

    # ---------- 收尾 ----------

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
