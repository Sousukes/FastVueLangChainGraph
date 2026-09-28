"""阶段 06 自研 MCP 工具包：server / client / host。

目录名用 `mcpkit` 而非 `mcp`，避免与官方 `pip install mcp` 的包名冲突
（backend/ 在 sys.path 首位，若目录叫 mcp，装完官方 SDK 后 import mcp 会解析到本地目录）。
"""

from mcpkit.client import MCPClient, MCPError
from mcpkit.host import MCPHost, get_host

__all__ = ["MCPClient", "MCPError", "MCPHost", "get_host"]
