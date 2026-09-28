"""阶段 03 后端入口：FastAPI 代理大模型 + SSE 流式。

为什么从这一阶段起必须有后端？（docs/DESIGN.md 二·前后端协作）
- 阶段 01–02 让前端直连模型，是为了看清「请求体长什么样」；
- 一旦要做多轮对话与流式，密钥不能留在浏览器、历史要能拼装、流式要能转成 SSE，
  这些都该由服务端承担。前端从此只做一件事：渲染。

三个接口：
    GET  /api/health         健康检查（含当前模型）
    POST /api/chat           非流式：等模型说完，一次性返回
    POST /api/chat/stream    流式：SSE 逐块推送增量，前端边收边渲染
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from llm import LLMClient, LLMNotConfiguredError
from schemas import (
    ChatRequest,
    ChatResponse,
    ExtractRequest,
    ExtractResponse,
    MCPCatalog,
    MCPPromptRequest,
    MCPResourceContent,
    MCPRunRequest,
    MCPRunResponse,
    ToolInfo,
    ToolRunRequest,
    ToolRunResponse,
    to_dicts,
)
from extract import extract_one
from agent import run_tool_loop
from tools import TOOLS
from mcpkit.host import get_host

app = FastAPI(title="全栈 AI 研究助手 · FastAPI + Vue3 全栈 LLM 实战")

# 阶段 03 前端由 Vite 代理转发（/api -> :8000），生产环境请改为具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: LLMClient | None = None


def get_client() -> LLMClient:
    """惰性构造 client：没配 Key 时给出可执行的提示，而不是裸 500。"""
    global _client
    if _client is None:
        try:
            _client = LLMClient()
        except LLMNotConfiguredError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
    return _client


def sse(payload: dict) -> str:
    """把一帧 SSE 数据编码成协议要求的文本。

    SSE 的全部语法就是：`data: <内容>\n\n`。
    内容里不能有裸换行，所以统一 JSON 序列化（ensure_ascii=False 保证中文可读）。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_headers() -> dict:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # 关掉 Nginx 之类反向代理的响应缓冲，否则流式会变成"最后一次性吐出"
        "X-Accel-Buffering": "no",
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "stage": 3, "model": get_client().model}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """非流式：等模型生成完毕，一次性返回。适合对照理解与程序化调用。"""
    client = get_client()
    try:
        content = client.chat(to_dicts(req.messages), model=req.model, temperature=req.temperature)
    except Exception as e:  # 上游报错统一转成可读错误
        raise HTTPException(status_code=502, detail=f"模型调用失败：{e}") from e
    return ChatResponse(content=content, model=req.model or client.model)


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """流式：把模型的逐块 delta 包装成 SSE 帧推给前端。

    帧协议（三种）：
        data: {"delta": "增量文本"}
        data: {"done": true, "model": "deepseek-flash"}
        data: {"error": "..."}
    """
    client = get_client()

    def event_gen() -> Iterator[str]:
        try:
            for delta in client.stream(
                to_dicts(req.messages), model=req.model, temperature=req.temperature
            ):
                yield sse({"delta": delta})
            yield sse({"done": True, "model": req.model or client.model})
        except Exception as e:
            # 注意：流已经开始，HTTP 状态码已经发出去了，只能靠帧内容报错
            yield sse({"error": f"模型调用失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


@app.post("/api/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest) -> ExtractResponse:
    """结构化抽取：JSON 模式 + Pydantic 校验 + 自纠重试。

    失败也不会 5xx：返回 `valid=False` + `error` 让前端照原样展示原始输出与失败原因。
    只有真正的"调用前出错"（如字段定义非法）才走 4xx。
    """
    client = get_client()
    try:
        return extract_one(client, req)
    except Exception as e:  # 上游模型调用 / schema 编译等真异常
        raise HTTPException(status_code=502, detail=f"抽取失败：{e}") from e


@app.get("/api/tools", response_model=list[ToolInfo])
def list_tools() -> list[ToolInfo]:
    """工具清单（不依赖模型，没有 Key 也能看）。

    内容与发给模型的 tools 参数同源：都来自 Pydantic 模型的 model_json_schema()。
    """
    return [
        ToolInfo(name=t.name, description=t.description, parameters=t.args_model.model_json_schema())
        for t in TOOLS.values()
    ]


@app.post("/api/tools/run", response_model=ToolRunResponse)
def tools_run(req: ToolRunRequest) -> ToolRunResponse:
    """跑一轮带工具的对话：模型决定调什么 → 我们执行 → 结果回喂 → 直到给出答案。"""
    client = get_client()
    try:
        outcome = run_tool_loop(client, req.question, model=req.model, max_steps=req.max_steps)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"工具调用失败：{e}") from e
    return ToolRunResponse(**outcome)


# ---------- 阶段 06 · MCP（Model Context Protocol） ----------


@app.get("/api/mcp/servers", response_model=MCPCatalog)
def mcp_servers() -> MCPCatalog:
    """已连接的 MCP Server 目录：协议版本、能力清单、工具 / 资源 / 提示模板。

    不依赖 LLM，所以没有 Key 也能看——这正是 MCP 的意义：能力与模型解耦。
    """
    try:
        return MCPCatalog(**get_host().describe())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MCP server 连接失败：{e}") from e


@app.post("/api/mcp/run", response_model=MCPRunResponse)
def mcp_run(req: MCPRunRequest) -> MCPRunResponse:
    """用 MCP Server 提供的工具跑一轮对话，并把 JSON-RPC 报文一并回传。"""
    client = get_client()
    try:
        outcome = get_host().run(client, req.question, model=req.model, max_steps=req.max_steps)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MCP 调用失败：{e}") from e
    return MCPRunResponse(**outcome)


@app.get("/api/mcp/resource", response_model=MCPResourceContent)
def mcp_resource(uri: str) -> MCPResourceContent:
    """读一个 Resource（resources/read）。资源是"可读取的数据"，不是函数。"""
    try:
        result = get_host().ensure_connected().read_resource(uri)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"读取资源失败：{e}") from e
    contents = (result.get("contents") or [{}])[0]
    return MCPResourceContent(
        uri=contents.get("uri", uri),
        mimeType=contents.get("mimeType"),
        text=contents.get("text", ""),
    )


@app.post("/api/mcp/prompt")
def mcp_prompt(req: MCPPromptRequest) -> dict:
    """取一个 Prompt 模板（prompts/get）——注意它返回的是 messages，不是文本。"""
    try:
        return get_host().ensure_connected().get_prompt(req.name, req.arguments)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"取提示模板失败：{e}") from e
