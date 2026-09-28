"""请求 / 响应契约。

阶段 03：多轮对话（messages 全量回放）
阶段 04：结构化抽取（动态字段定义 + Pydantic 校验结果回传）
阶段 05：函数调用（工具说明书 + 调用 trace）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    """一条对话消息。多轮对话的本质，就是这个数组的累积与回放。"""

    role: Role
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """前端每轮把完整历史带上——HTTP 无状态，服务端不替你记。"""

    messages: list[Message] = Field(min_length=1)
    model: str | None = None
    temperature: float = 0.7


class ChatResponse(BaseModel):
    """非流式响应。"""

    content: str
    model: str


def to_dicts(messages: list[Message]) -> list[dict]:
    """Pydantic 模型 → OpenAI SDK 需要的普通 dict 列表。"""
    return [{"role": m.role, "content": m.content} for m in messages]


# ---------- 阶段 04 · 结构化抽取 ----------

FieldType = Literal["string", "int", "number", "bool", "array"]


class FieldSpec(BaseModel):
    """一条字段定义。

    - `name` 必须是合法 Python 标识符：pydantic.create_model 会用它做属性名，
      校验失败就能提前把"非法字段名"挡在调用上游之外。
    - `enum` 仅对 `string` 生效；其余类型若提供 enum 会被忽略。
    """

    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: FieldType
    description: str = ""
    required: bool = True
    enum: list[str] | None = None


class ExtractRequest(BaseModel):
    """前端每次请求：把"待抽取文本 + 字段定义"一起交给后端。"""

    text: str = Field(min_length=1)
    fields: list[FieldSpec] = Field(min_length=1)
    model: str | None = None


class ExtractResponse(BaseModel):
    """结构化抽取的完整结果：成功给 data，失败也把 raw 留回来给用户对照。"""

    data: dict | None = None
    raw: str = ""
    attempts: int = 1
    valid: bool = False
    model: str
    error: str | None = None
    warning: str | None = None  # 例如「JSON 模式不支持，已降级」


# ---------- 阶段 05 · 函数调用 ----------


class ToolInfo(BaseModel):
    """给前端展示的"工具说明书"，与发给模型的内容同源（都来自 Pydantic 模型）。"""

    name: str
    description: str
    parameters: dict


class ToolRunRequest(BaseModel):
    question: str = Field(min_length=1)
    model: str | None = None
    max_steps: int = Field(default=4, ge=1, le=8, description="工具循环的最大轮数（防跑飞）")


class TraceStep(BaseModel):
    """一次工具调用的完整记录——前端把它摊成时间线，回答就变得可解释了。"""

    step: int
    tool: str
    arguments: Any = None
    result: Any = None
    error: str | None = None
    ms: float = 0.0


class ToolRunResponse(BaseModel):
    answer: str | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    steps: int = 0
    model: str
    exhausted: bool = False  # True = 到了 max_steps 仍未给出最终答案


# ---------- 阶段 06 · MCP ----------


class MCPTool(BaseModel):
    name: str
    description: str = ""
    inputSchema: dict = Field(default_factory=dict)


class MCPResource(BaseModel):
    uri: str
    name: str = ""
    description: str = ""
    mimeType: str | None = None


class MCPPrompt(BaseModel):
    name: str
    description: str = ""
    arguments: list[dict] = Field(default_factory=list)


class MCPServerInfo(BaseModel):
    name: str
    protocolVersion: str = ""
    serverInfo: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    tools: list[MCPTool] = Field(default_factory=list)
    resources: list[MCPResource] = Field(default_factory=list)
    prompts: list[MCPPrompt] = Field(default_factory=list)


class MCPCatalog(BaseModel):
    """已连接的 server 目录：这就是"模型能用的全部外部能力"。"""

    servers: list[MCPServerInfo] = Field(default_factory=list)
    defaultServer: str = ""


class MCPLogEntry(BaseModel):
    """一条 JSON-RPC 报文。dir: "→" 是我们发出、"←" 是 server 回应。"""

    server: str
    dir: str
    payload: Any = None


class MCPRunRequest(BaseModel):
    question: str = Field(min_length=1)
    model: str | None = None
    max_steps: int = Field(default=4, ge=1, le=8)


class MCPRunResponse(BaseModel):
    answer: str | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    steps: int = 0
    model: str
    exhausted: bool = False
    protocol: list[MCPLogEntry] = Field(default_factory=list)


class MCPResourceContent(BaseModel):
    uri: str
    mimeType: str | None = None
    text: str = ""


class MCPPromptRequest(BaseModel):
    name: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
