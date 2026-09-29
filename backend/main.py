"""阶段 03 后端入口：FastAPI 代理大模型 + SSE 流式。

为什么从这一阶段起必须有后端？（docs/DESIGN.md 二·前后端协作）
- 阶段 01–02 让前端直连模型，是为了看清「请求体长什么样」；
- 一旦要做多轮对话与流式，密钥不能留在浏览器、历史要能拼装、流式要能转成 SSE，
  这些都该由服务端承担。前端从此只做一件事：渲染。

三个接口：
    GET  /api/health         健康检查（含当前模型）
    POST /api/chat           非流式：等模型说完，一次性返回
    POST /api/chat/stream    流式：SSE 逐块推送增量，前端边收边渲染

后续阶段在这条主干上继续加路由（06 MCP / 07-08 RAG / 09 GraphRAG），
但**上层的三个约定始终没变**：密钥只在服务端、请求体由 Pydantic 校验、
长任务用 SSE 把中间过程推出去。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from llm import LLMClient, LLMNotConfiguredError
from schemas import (
    AgentRunRequest,
    AgentRunResponse,
    AgentToolsResponse,
    AgenticChannelsResponse,
    AgenticRunRequest,
    AgenticRunResponse,
    SearchRequest,
    SearchResponse,
    ResearchRequest,
    ResearchResponse,
    ResearchSectionSummary,
    ResearchSource,
    ChatRequest,
    ChatResponse,
    ExtractRequest,
    ExtractResponse,
    GraphAskRequest,
    GraphAskResponse,
    GraphBuildRequest,
    GraphBuildResponse,
    GraphDocumentItem,
    GraphEntitiesResponse,
    GraphOverview,
    GraphResetResponse,
    GraphSearchRequest,
    GraphSearchResponse,
    GraphStats,
    HarnessRolesResponse,
    HarnessRunRequest,
    HarnessRunResponse,
    MCPCatalog,
    MCPPromptRequest,
    MCPResourceContent,
    MCPRunRequest,
    MCPRunResponse,
    RagAskRequest,
    RagAskResponse,
    RagDocument,
    RagDocumentIn,
    RagIngestResponse,
    RagResetResponse,
    RagSearchRequest,
    RagSearchResponse,
    RagSeedResponse,
    RagStatus,
    TeamRolesResponse,
    TeamRunRequest,
    TeamRunResponse,
    ToolInfo,
    ToolRunRequest,
    ToolRunResponse,
    to_dicts,
)
from extract import extract_one
from agent import run_tool_loop
from tools import TOOLS
from mcpkit.host import get_host
import rag
import graph
import react
import team
import harness
import agentic
import search
import research

app = FastAPI(title="全栈 AI 研究助手 · FastAPI + Vue3 全栈 LLM 实战")

# 阶段 03 前端由 Vite 代理转发（/api -> :8000），生产环境请改为具体域名。
#
# ⚠️ 安全：这里**不能**写 allow_origins=["*"]。
# 本项目有 /api/rag/reset 这类破坏性接口，而浏览器不会拦截跨站请求的**发送**——
# 一旦放开 *，用户访问的任意网页都能悄悄 POST 到 127.0.0.1:8000 把知识库清空。
# 所以只放行本机开发端口（Vite 的 5173/5174/4173 等都覆盖到）。
# 用 regex 而不是写死列表，是因为 Vite 端口被占用时会自动顺延。
_LOCAL_ORIGIN = r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_LOCAL_ORIGIN,
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


# ---------- 阶段 07 · RAG（检索增强生成） ----------

# 一键导入的示例语料：直接用本仓库自己的文档，
# 让学员可以问"这门课讲了什么"——答案必须来自检索，而不是模型的记忆。
# 只喂一句话的假数据看不出切块的效果，必须用真实长文档。
SEED_FILES = ["DESIGN.md", "design-system.md"]
# 再把各阶段教程也喂进去：学员读到这里时，前面几个阶段的正文已经是一份
# 现成的语料。问"阶段 06 讲了什么"，答案就来自他自己刚读完的那一页。
SEED_GLOB = "stages/*.md"


@app.get("/api/rag/status", response_model=RagStatus)
def rag_status() -> RagStatus:
    """知识库状态：文档数 / 块数 / 嵌入模型 / 切块参数。

    不依赖 LLM——和 MCP 目录一样，检索这条链路和模型是解耦的。
    """
    return RagStatus(**rag.stats())


@app.get("/api/rag/documents", response_model=list[RagDocument])
def rag_documents() -> list[RagDocument]:
    return [RagDocument(**d) for d in rag.list_documents()]


@app.post("/api/rag/documents", response_model=RagIngestResponse)
def rag_add_document(req: RagDocumentIn) -> RagIngestResponse:
    """入库一个文档：切块 → 嵌入 → 存进向量库。返回切块结果供前端展示。"""
    try:
        info = rag.add_document(req.title, req.text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"入库失败：{e}") from e
    return RagIngestResponse(**info)


@app.delete("/api/rag/documents/{title}", response_model=RagResetResponse)
def rag_delete_document(title: str) -> RagResetResponse:
    return RagResetResponse(deleted=rag.delete_document(title))


@app.post("/api/rag/seed", response_model=RagSeedResponse)
def rag_seed() -> RagSeedResponse:
    """一键导入本仓库的课程文档（docs/ 下的 md + 各阶段教程）。

    真实长文档才能演示出"切块"这件事——一句话的示例文档切不出东西来。
    """
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    paths = [docs_dir / name for name in SEED_FILES]
    paths += sorted(docs_dir.glob(SEED_GLOB))

    imported: list[dict] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            imported.append(rag.add_document(path.stem, path.read_text(encoding="utf-8")))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"导入 {path.name} 失败：{e}") from e
    if not imported:
        raise HTTPException(status_code=404, detail="没有找到可导入的示例文档")
    return RagSeedResponse(imported=[RagIngestResponse(**i) for i in imported])


@app.post("/api/rag/search", response_model=RagSearchResponse)
def rag_search(req: RagSearchRequest) -> RagSearchResponse:
    """**纯检索**：不调用 LLM，只回答"库里的哪些块跟这个问题最像"。

    RAG 调优的第一条纪律就是先看这一步——检索错了，生成再强也救不回来。

    阶段 08 起多了三个旋钮：`mode`（vector / bm25 / hybrid）、`rerank`、
    `candidates`。默认值仍是纯向量，所以阶段 07 的行为不变。
    返回值里的 `rows` 是**排名对照表**：同一个块在各阶段的名次。
    """
    try:
        found = rag.hybrid_search(
            req.query,
            top_k=req.top_k,
            mode=req.mode,
            rerank=req.rerank,
            candidates=req.candidates,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"检索失败：{e}") from e
    return RagSearchResponse(
        query=req.query,
        hits=found["hits"],
        mode=req.mode,
        rerank=req.rerank,
        candidates=req.candidates,
        rows=found["rows"],
        timings=found["timings"],
    )


@app.post("/api/rag/ask", response_model=RagAskResponse)
def rag_ask(req: RagAskRequest) -> RagAskResponse:
    """完整 RAG：检索 → 拼 prompt → 生成。返回里带上 prompt，让"增强"这一步可见。"""
    client = get_client()
    try:
        outcome = rag.ask(
            client,
            req.question,
            top_k=req.top_k,
            model=req.model,
            mode=req.mode,
            rerank=req.rerank,
            candidates=req.candidates,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RAG 问答失败：{e}") from e
    return RagAskResponse(**outcome)


@app.post("/api/rag/reset", response_model=RagResetResponse)
def rag_reset() -> RagResetResponse:
    """清空知识库（教学用：调完切块参数可以重来一遍）。"""
    before = rag.stats()["chunks"]
    rag.reset()
    return RagResetResponse(deleted=before)


# ---------- 阶段 09 · GraphRAG（知识图谱） ----------
#
# 这一组接口和 RAG 那组的**结构差异**本身就是知识点：
# 检索（search / ask）是**读**，几十毫秒就回来；
# 构建（build）是**写**，每个块一次 LLM 调用，动辄几分钟。
# 所以构建必须走 SSE 把进度推出来，而不是让一个 HTTP 请求干等。


@app.get("/api/graph/stats", response_model=GraphStats)
def graph_stats() -> GraphStats:
    """图谱状态：实体数 / 边数 / 覆盖率 / 类型分布 / 关系词分布。

    不依赖 LLM——和 MCP 目录、知识库状态一样，**图的读取和模型是解耦的**。
    """
    return GraphStats(**graph.stats())


@app.get("/api/graph/entities", response_model=GraphEntitiesResponse)
def graph_entities(limit: int = 200, q: str = "") -> GraphEntitiesResponse:
    """实体清单（按提及次数排序）。`degree` 比 `mentions` 更能说明"枢纽"程度。"""
    return GraphEntitiesResponse(**graph.list_entities(limit=limit, q=q))


@app.get("/api/graph/documents", response_model=list[GraphDocumentItem])
def graph_documents() -> list[GraphDocumentItem]:
    """语料文档 + 每篇已抽多少块。前端用它渲染"构建范围"的勾选框。"""
    return [GraphDocumentItem(**d) for d in graph.corpus_documents()]


@app.get("/api/graph/overview", response_model=GraphOverview)
def graph_overview(limit: int = 40) -> GraphOverview:
    """全图快照（**已截断**）：按度数取最枢纽的若干节点及其之间的边。

    为什么要截断：一张 300 节点的图在屏幕上只是一团毛线，信息量为零。
    """
    return GraphOverview(**graph.top_nodes(limit=limit))


@app.post("/api/graph/build", response_model=GraphBuildResponse)
def graph_build(req: GraphBuildRequest) -> GraphBuildResponse:
    """**同步**构建：抽三元组 → 落 SQLite。

    只适合小批量（`limit` 默认 24）。要看着进度跑长任务，用下面的 `/build/stream`。
    """
    client = get_client()
    t0 = time.perf_counter()
    try:
        result = graph.build(
            client,
            limit=req.limit,
            model=req.model,
            workers=req.workers,
            titles=req.titles,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图谱构建失败：{e}") from e
    return GraphBuildResponse(**result, seconds=round(time.perf_counter() - t0, 2))


@app.post("/api/graph/build/stream")
def graph_build_stream(req: GraphBuildRequest) -> StreamingResponse:
    """**流式**构建：边抽边把进度推出去。

    帧协议（四种）：
        data: {"phase": "start",   "total": 12}
        data: {"phase": "progress","done": 3, "total": 12, "chunk": "DESIGN#7",
               "entities": 41, "edges": 38, "ms": 4100}
        data: {"phase": "done",    "processed": 12, "entities": 211, "edges": 217,
               "seconds": 48.9, "chunks": 12}
        data: {"error": "..."}

    为什么不用普通的同步请求：全量抽取要几分钟，中间**什么反馈都没有**。
    进度条不只是体验问题——它是"抽取是入库时的一次性成本"这句话的唯一证据。

    实现上有个必须绕开的坑：阶段 03 的 SSE 之所以能直接 `yield`，
    是因为 `client.stream()` 本身就是个生成器。而 `graph.build()` 是**阻塞函数**，
    在生成器里直接调它，就得等它整跑完才轮得到第一帧 `yield`——
    进度会**在结束的瞬间一起涌出来**，等于没有进度。
    所以这里把 build 丢进后台线程，用 `queue.Queue` 把进度搬回生成器。
    """
    client = get_client()
    frames: queue.Queue = queue.Queue()

    def event_gen() -> Iterator[str]:
        outcome: dict = {}

        def worker() -> None:
            try:
                outcome["result"] = graph.build(
                    client,
                    limit=req.limit,
                    model=req.model,
                    workers=req.workers,
                    titles=req.titles,
                    on_progress=lambda done, total, key: frames.put(
                        {
                            "phase": "progress",
                            "done": done,
                            "total": total,
                            "chunk": key,
                            **graph.counts(),
                        }
                    ),
                )
            except Exception as e:  # noqa: BLE001
                outcome["error"] = str(e)
            finally:
                frames.put(None)  # 哨兵：告诉消费端"没有更多了"

        t0 = time.perf_counter()
        yield sse({"phase": "start", "total": req.limit})
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            item = frames.get()
            if item is None:
                break
            yield sse(item)
        thread.join()

        if "error" in outcome:
            yield sse({"error": f"图谱构建失败：{outcome['error']}"})
            return
        yield sse(
            {"phase": "done", **outcome["result"], "seconds": round(time.perf_counter() - t0, 2)}
        )

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


@app.post("/api/graph/search", response_model=GraphSearchResponse)
def graph_search(req: GraphSearchRequest) -> GraphSearchResponse:
    """**纯图谱检索**：不调用 LLM。

    问题 → 锚定实体 → 沿边扩 N 跳 → 把边回溯到源块。
    看这一步能立刻明白 GraphRAG 和向量检索的分工：
    **向量找"像不像"，图找"连不连"。**
    """
    try:
        found = graph.graph_search(req.query, hops=req.hops, top_k=req.top_k)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图谱检索失败：{e}") from e
    return GraphSearchResponse(query=req.query, hops=req.hops, **found)


@app.post("/api/graph/ask", response_model=GraphAskResponse)
def graph_ask(req: GraphAskRequest) -> GraphAskResponse:
    """图谱问答：检索 → 拼 prompt → 生成。返回里带上 prompt 与整条关系链。"""
    client = get_client()
    try:
        outcome = graph.ask(client, req.question, hops=req.hops, top_k=req.top_k, model=req.model)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图谱问答失败：{e}") from e
    return GraphAskResponse(question=req.question, **outcome)


@app.post("/api/graph/reset", response_model=GraphResetResponse)
def graph_reset() -> GraphResetResponse:
    """清空图谱（教学用：换个抽取词表可以重来一遍）。"""
    before = graph.stats()
    graph.reset()
    return GraphResetResponse(
        deletedEntities=before["entities"], deletedEdges=before["edges"]
    )


# ---------- 阶段 10 · 单智能体（ReAct） ----------


@app.get("/api/agent/tools", response_model=AgentToolsResponse)
def agent_tools() -> AgentToolsResponse:
    """工具目录：给前端渲染勾选框，并把"重叠"照实报出来。

    注意这里**会拉起 MCP server 子进程**（要问它有哪些工具）。这是目录接口该付的成本——
    它只在页面加载时调一次，而 `build_schemas()` 在每轮对话开头都会调，
    那边就必须懒加载（见 `react._group_tools`）。
    """
    return AgentToolsResponse(**react.tool_catalog())


@app.post("/api/agent/run", response_model=AgentRunResponse)
def agent_run(req: AgentRunRequest) -> AgentRunResponse:
    """非流式：跑完整个 ReAct 循环，一次性返回结果 + 结构化 trace。

    适合程序化调用和对照实验——**同样的参数，`/stream` 与 `/run` 结果完全一致**，
    只是后者要等到最后才给你看。
    """
    client = get_client()
    groups = req.groups or list(react.DEFAULT_GROUPS)
    try:
        result = react.run_react_blocking(
            client,
            req.question,
            model=req.model,
            max_steps=req.max_steps,
            temperature=req.temperature,
            groups=groups,
            observation_limit=req.observation_limit,
            max_repeat=req.max_repeat,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"智能体运行失败：{e}") from e
    return AgentRunResponse(**{k: v for k, v in result.items() if k != "events"})


@app.post("/api/agent/stream")
def agent_stream(req: AgentRunRequest) -> StreamingResponse:
    """**流式**：逐事件推。ReAct 的演示价值全在"看着它想"。

    帧协议（事件类型即帧的 `type`）：
        data: {"type":"start",       "tools":[...], "groups":[...], "maxSteps":6}
        data: {"type":"delta",       "step":1, "text":"..."}        ← 可连续多帧
        data: {"type":"action",      "step":1, "tool":"...", "arguments":{...}, "repeat":0}
        data: {"type":"observation", "step":1, "ok":true, "content":"...", "truncated":false, "ms":12.3}
        data: {"type":"step_end",    "step":1, "contextChars":2971}
        data: {"type":"finish",      "reason":"answered", "answer":"...", "steps":2, "trace":[...]}
        data: {"type":"error",       "message":"..."}

    ⚠️ `delta` 是**中性**的：流式下没法提前知道这一轮文本是"思考"还是"最终答案"，
    唯一的分界线是这一步结束时**有没有 `action`**。所以前端要"先流出来、再定性"——
    收到该 step 的 `action` 就把缓冲落成 Thought；收到 `finish` 就丢掉缓冲、改用
    `finish.answer`（权威版本，还带收口原因与 trace）。

    ⭐ **请和阶段 09 的 `/api/graph/build/stream` 对照着读。**
    那一边要"后台线程 + `queue.Queue` + 哨兵"，这一边没有——直接 for 循环 `yield` 就完了。
    差别不在技巧，在**控制权**：阶段 09 的 `graph.build()` 是个阻塞的黑盒，
    我们没法从它肚子里掏进度，只能另开一条线程；而 ReAct 这个循环**是我们自己写的**，
    每一步都在我们的控制流里，想在哪 `yield` 就在哪 `yield`。
    **能自己拆开的流程，就别用线程去绕。** 线程带来的每一个队列和哨兵，
    都是将来某次"卡住不动"的伏笔。
    """
    client = get_client()
    groups = req.groups or list(react.DEFAULT_GROUPS)

    def event_gen() -> Iterator[str]:
        try:
            for event in react.run_react(
                client,
                req.question,
                model=req.model,
                max_steps=req.max_steps,
                temperature=req.temperature,
                groups=groups,
                observation_limit=req.observation_limit,
                max_repeat=req.max_repeat,
            ):
                yield sse(event)
        except Exception as e:  # noqa: BLE001  生成器里抛异常只会断流，前端什么都看不到
            yield sse({"type": "error", "message": f"智能体运行失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


# ---------- 阶段 11 · 多智能体 ----------


@app.get("/api/team/roles", response_model=TeamRolesResponse)
def team_roles() -> TeamRolesResponse:
    """角色目录：把每个角色的**工具面**摊开给前端看。

    ⭐ `tools` 必须显示。分工成不成立，一眼就能从这个清单看出来——
    如果两个角色的 tools 一模一样，那就是"伪多智能体"（同一个智能体换两个名字）。
    """
    return TeamRolesResponse(**team.role_catalog())


@app.post("/api/team/run", response_model=TeamRunResponse)
def team_run(req: TeamRunRequest) -> TeamRunResponse:
    """非流式：跑完整个多智能体流程，一次性返回结果 + 分角色计时。

    和 `/stream` 结果完全一致，只是要等最后才给你看——适合对照实验
    （同一个问题跑两遍，并排看单智能体 vs 多智能体的耗时与答案）。
    """
    client = get_client()
    try:
        result = team.run_team_blocking(
            client,
            req.question,
            model=req.model,
            parallel=req.parallel,
            max_tasks=req.max_tasks,
            observation_limit=req.observation_limit,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"多智能体运行失败：{e}") from e
    # `events` 是给 /stream 用的中间帧，阻塞接口不需要，去掉避免超 schema
    return TeamRunResponse(**{k: v for k, v in result.items() if k != "events"})


@app.post("/api/team/stream")
def team_stream(req: TeamRunRequest) -> StreamingResponse:
    """**流式**：逐事件推。多智能体相对单智能体多了一个 `agent` 字段，

    并行时必须有它，否则前端没法把交织的事件分回各自的角色列。

    帧协议（事件类型即帧的 `type`，每一帧都带 `agent` 表示归属角色）：
        data: {"type":"start",        "roles":[...], "parallel":true}
        data: {"type":"phase",        "phase":"plan", "label":"规划员拆解任务"}
        data: {"type":"plan",         "tasks":[...], "layers":[[...]], "ms":..}
        data: {"type":"layer",        "layer":1, "agents":[...]}
        data: {"type":"agent_start",  "agent":"t1", "role":"researcher",
               "groups":["rag","graph"], "tools":["rag_search",...]}
        data: {"type":"delta",        "agent":"t1", "step":1, "text":"..."}
        data: {"type":"action",       "agent":"t1", "step":1, "tool":"rag_search", ...}
        data: {"type":"observation",  "agent":"t1", ...}
        data: {"type":"agent_result", "agent":"t1", "result":{...}}
        data: {"type":"agent_end",    "agent":"t1"}
        data: {"type":"review",       "verdict":"pass", "issues":[...]}
        data: {"type":"write",        "text":"..."}
        data: {"type":"finish",       "answer":"...", "totalMs":.., "planMs":.., ...}
        data: {"type":"error",        "message":"..."}

    ⭐ 和阶段 09 对照：这里的并行**也是**「后台线程 + queue.Queue + 哨兵」，
    因为多个 worker 天然并发、谁先出帧不确定。阶段 10 的「能自己拆开就别用线程」
    在这里**反例成立**——判据是"流程本身是否并发"，不是"能不能用生成器拆开"。
    """
    client = get_client()

    def event_gen() -> Iterator[str]:
        try:
            for event in team.run_team(
                client,
                req.question,
                model=req.model,
                parallel=req.parallel,
                max_tasks=req.max_tasks,
                observation_limit=req.observation_limit,
            ):
                yield sse(event)
        except Exception as e:  # noqa: BLE001  生成器里抛异常只会断流，前端什么都看不到
            yield sse({"type": "error", "message": f"多智能体运行失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


# ---------- 阶段 12 · Agent Harness 框架 ----------


@app.get("/api/harness/roles", response_model=HarnessRolesResponse)
def harness_roles() -> HarnessRolesResponse:
    """角色目录 + 可用工具组。

    ⭐ 这份目录和阶段 11 的 `/api/team/roles` 同构——因为多智能体的角色、
    这里单独跑的角色，背后是**同一个 harness.Agent**。区别只是构造参数。
    """
    return HarnessRolesResponse(**harness.role_catalog())


@app.post("/api/harness/run", response_model=HarnessRunResponse)
def harness_run(req: HarnessRunRequest) -> HarnessRunResponse:
    """非流式：跑一个独立的 harness Agent，返回结果 + 结构化 trace。

    这是「harness 真能复用」的最小证明：选一个预设角色（检索员 / 分析员 / 纯净助手），
    它和阶段 11 里同名的 worker 跑的是同一段 Agent 代码。
    """
    client = get_client()
    preset = harness.PRESETS.get(req.role)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"未知角色：{req.role}")
    try:
        result = harness.Agent(
            client,
            system=preset["system"],
            groups=preset["groups"],
            max_steps=req.maxSteps,
            temperature=req.temperature,
            observation_limit=req.observationLimit,
        ).run_blocking(req.question, model=req.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"harness 运行失败：{e}") from e
    return HarnessRunResponse(
        role=req.role,
        question=req.question,
        answer=result.get("answer"),
        model=result.get("model", client.model),
        steps=result.get("steps", 0),
        totalMs=result.get("totalMs", 0.0),
        llmMs=result.get("llmMs", 0.0),
        tools=result.get("tools", []),
        trace=result.get("trace", []),
        error=result.get("error"),
    )


@app.post("/api/harness/stream")
def harness_stream(req: HarnessRunRequest) -> StreamingResponse:
    """**流式**：逐事件推。每一帧带 `agent` 字段（值为角色名），前端据此分列。

    帧协议（事件类型即帧的 `type`）：
        data: {"type":"start", "question":"...", "tools":[...], "maxSteps":..}
        data: {"type":"delta", "step":1, "text":"..."}
        data: {"type":"action", "step":1, "tool":"rag_search", "arguments":{...}}
        data: {"type":"observation", "step":1, "callId":"...", "ok":true, "content":"..."}
        data: {"type":"step_end", "step":1, "actions":[...]}
        data: {"type":"finish", "answer":"...", "totalMs":.., "llmMs":.., "steps":..}
        data: {"type":"error", "message":"..."}
    """
    client = get_client()
    preset = harness.PRESETS.get(req.role)
    if preset is None:
        return StreamingResponse(
            (sse({"type": "error", "message": f"未知角色：{req.role}"}),),
            media_type="text/event-stream",
            headers=sse_headers(),
        )

    def event_gen() -> Iterator[str]:
        try:
            for event in harness.Agent(
                client,
                system=preset["system"],
                groups=preset["groups"],
                max_steps=req.maxSteps,
                temperature=req.temperature,
                observation_limit=req.observationLimit,
            ).run(req.question, model=req.model):
                yield sse({**event, "agent": req.role})
        except Exception as e:  # noqa: BLE001  生成器里抛异常只会断流，前端什么都看不到
            yield sse({"type": "error", "message": f"harness 运行失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


# ---------- 阶段 13 · Agentic RAG ----------


@app.get("/api/agentic/channels", response_model=AgenticChannelsResponse)
def agentic_channels() -> AgenticChannelsResponse:
    """检索通道清单 + 每个通道出现在第几轮。

    把这个端点单独开出来（而不是写死在前端），是为了让「三轮分别用哪种检索」
    这件事有唯一事实来源——`agentic.CHANNELS` 改了，控制台的计划说明同步变。
    """
    return AgenticChannelsResponse(**agentic.channel_catalog())


@app.post("/api/agentic/run", response_model=AgenticRunResponse)
def agentic_run(req: AgenticRunRequest) -> AgenticRunResponse:
    """非流式：跑完整个「路由 → 检索 → 评级 →（改写重试）→ 生成」。

    `timeline` 把 retrieve / grade / rewrite 三类事件分好组返回——后台到底做了几次决策、
    每次为什么，都在这份结构里，不必去看日志。
    """
    client = get_client()
    try:
        result = agentic.run_agentic_blocking(
            client,
            req.question,
            model=req.model,
            max_rounds=req.maxRounds,
            top_k=req.topK,
            hops=req.hops,
            grade_limit=req.gradeLimit,
            temperature=req.temperature,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Agentic RAG 运行失败：{e}") from e

    return AgenticRunResponse(
        question=result["question"],
        answer=result.get("answer"),
        model=result.get("model", client.model),
        needRetrieval=result.get("needRetrieval"),
        routeReason=result.get("routeReason", ""),
        rounds=result.get("rounds", 0),
        finalQuery=result.get("finalQuery", req.question),
        basedOn=result.get("basedOn", "none"),
        hits=result.get("hits", []),
        times=result.get("times", {}),
        totalMs=result.get("totalMs", 0.0),
        llmMs=result.get("llmMs", 0.0),
        timeline=result.get("timeline", {}),
        error=result.get("error"),
    )


@app.post("/api/agentic/stream")
def agentic_stream(req: AgenticRunRequest) -> StreamingResponse:
    """**流式**：逐事件推，前端据此画「决策链路」时间线。

    帧协议（`type` 即帧类型）：

        data: {"type":"start",    "question":"...", "plan":[{round,channel}]}
        data: {"type":"route",    "needRetrieval":true, "reason":"...", "ms":..}
        data: {"type":"retrieve", "round":1, "channel":"vector", "query":"...", "hits":[...], "ms":..}
        data: {"type":"grade",    "round":1, "useful":false, "kept":[0], "missing":"...", "ms":..}
        data: {"type":"rewrite",  "round":1, "from":"...", "to":"...", "why":"...", "ms":..}
        data: {"type":"answer_delta", "text":"..."}
        data: {"type":"finish",   "answer":"...", "rounds":2, "basedOn":"hybrid", "times":{...}}
        data: {"type":"error",    "message":"..."}

    ⭐ `route` 帧是本阶段最关键的一屏：它让「这一步要不要检索」这个过去藏在
    代码里的 `if`，第一次变成可以给用户看、也可以被质疑的判断。
    """
    client = get_client()

    def event_gen() -> Iterator[str]:
        try:
            for event in agentic.run_agentic(
                client,
                req.question,
                model=req.model,
                max_rounds=req.maxRounds,
                top_k=req.topK,
                hops=req.hops,
                grade_limit=req.gradeLimit,
                temperature=req.temperature,
            ):
                yield sse(event)
        except Exception as e:  # noqa: BLE001  生成器里抛异常只会断流，前端什么都看不到
            yield sse({"type": "error", "message": f"Agentic RAG 运行失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


# ---------- 阶段 14 · AI 搜索应用 ----------


@app.post("/api/search/run", response_model=SearchResponse)
def search_run(req: SearchRequest) -> SearchResponse:
    """非流式：跑完「混合检索 → 带引用合成 → 引用解析」。

    `sources` 是答案的"依据面板"——`cited=true` 的条目就是答案里 [n] 实际指向的来源；
    `grounded` 表示答案**声称**至少引用了一段资料（不保证忠实，见阶段 15）。
    """
    client = get_client()
    try:
        result = search.run_search_blocking(
            client,
            req.question,
            model=req.model,
            top_k=req.topK,
            rerank=req.rerank,
            snippet=req.snippet,
            temperature=req.temperature,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"AI 搜索运行失败：{e}") from e

    return SearchResponse(
        question=result["question"],
        answer=result.get("answer"),
        model=result.get("model", client.model),
        sources=result.get("sources", []),
        citations=result.get("citations", []),
        grounded=result.get("grounded", False),
        coverage=result.get("coverage", 0.0),
        retrieved=result.get("retrieved", 0),
        times=result.get("times", {}),
        totalMs=result.get("totalMs", 0.0),
        llmMs=result.get("llmMs", 0.0),
        error=result.get("error"),
    )


@app.post("/api/search/stream")
def search_stream(req: SearchRequest) -> StreamingResponse:
    """**流式**：逐事件推，前端据此渲染答案 + 来源面板。

    帧协议（`type` 即帧类型）：

        data: {"type":"start",     "question":"...", "topK":6}
        data: {"type":"retrieve",  "query":"...", "hits":[...], "count":6, "ms":..}
        data: {"type":"synthesize","status":"ok"}
        data: {"type":"answer_delta", "text":"..."}
        data: {"type":"finish",    "answer":"...", "sources":[...], "citations":[1,3], "grounded":true, "coverage":0.6, "times":{...}}
        data: {"type":"error",     "message":"..."}

    ⭐ `finish.sources` 里每条带 `cited` 字段，前端据此把答案里的 [n] 标成可点击的引用 chip。
    """
    client = get_client()

    def event_gen() -> Iterator[str]:
        try:
            for event in search.run_search(
                client,
                req.question,
                model=req.model,
                top_k=req.topK,
                rerank=req.rerank,
                snippet=req.snippet,
                temperature=req.temperature,
            ):
                yield sse(event)
        except Exception as e:  # noqa: BLE001  生成器里抛异常只会断流，前端什么都看不到
            yield sse({"type": "error", "message": f"AI 搜索运行失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())


# ---------- 阶段 15 · Deep Research ----------


@app.post("/api/research/run", response_model=ResearchResponse)
def research_run(req: ResearchRequest) -> ResearchResponse:
    """非流式：跑完「规划 → 逐节自适应检索 → 带引用报告 → 忠实性校验」。

    `faithful` / `faithfulness` 是阶段 15 相对阶段 14 新增的字段：阶段 14 只验证
    「模型**声称**引用了」，本阶段额外用一次 LLM 调用逐句核对报告是否真的被资料支持。
    `unsupported` 列出了核查中判为「资料无法支持」的结论原文，供前端标红。
    """
    client = get_client()
    try:
        result = research.run_research_blocking(
            client,
            req.question,
            model=req.model,
            top_k=req.topK,
            rerank=req.rerank,
            snippet=req.snippet,
            max_sections=req.maxSections,
            hops=req.hops,
            max_rounds=req.maxRounds,
            temperature=req.temperature,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"深度研究运行失败：{e}") from e

    return ResearchResponse(
        question=result["question"],
        answer=result.get("answer"),
        model=result.get("model", client.model),
        sections=[ResearchSectionSummary(**s) for s in result.get("sections", [])],
        sources=[ResearchSource(**s) for s in result.get("sources", [])],
        citations=result.get("citations", []),
        grounded=result.get("grounded", False),
        coverage=result.get("coverage", 0.0),
        faithful=result.get("faithful", False),
        faithfulness=result.get("faithfulness", 0.0),
        unsupported=result.get("unsupported", []),
        times=result.get("times", {}),
        totalMs=result.get("totalMs", 0.0),
        llmMs=result.get("llmMs", 0.0),
        error=result.get("error"),
    )


@app.post("/api/research/stream")
def research_stream(req: ResearchRequest) -> StreamingResponse:
    """**流式**：逐事件推，前端据此渲染大纲、分段进度、报告与忠实性结果。

    帧协议（`type` 即帧类型）：

        data: {"type":"start",     "question":"...", "maxSections":4}
        data: {"type":"plan",      "sections":[{"title":"...","question":"..."}]}
        data: {"type":"section_start", "index":1, "title":"...", "subQuestion":"..."}
        data: {"type":"section_retrieve", "index":1, "rounds":2, "basedOn":"hybrid", "hitCount":4, "ms":..}
        data: {"type":"section_answer_delta", "index":1, "text":"..."}
        data: {"type":"answer_delta", "text":"## 标题\\n\\n..."}
        data: {"type":"section_finish", "index":1, "citations":[1,3], "grounded":true}
        data: {"type":"synthesize", "status":"ok", "sectionCount":4, "sourceCount":12}
        data: {"type":"faithfulness", "score":0.92, "faithful":true, "unsupported":[]}
        data: {"type":"finish", "answer":"...", "sources":[...], "citations":[1,3,5], "grounded":true, "faithful":true, "faithfulness":0.92, "unsupported":[], "times":{...}}
        data: {"type":"error",     "message":"..."}

    ⭐ `finish.sources` 每条带 `cited` 与 `channel`；`finish.faithful` / `faithfulness` /
       `unsupported` 是阶段 15 的签名产出，前端据此把"可验证"再往前推一步到"已核查"。
    """
    client = get_client()

    def event_gen() -> Iterator[str]:
        try:
            for event in research.run_research(
                client,
                req.question,
                model=req.model,
                top_k=req.topK,
                rerank=req.rerank,
                snippet=req.snippet,
                max_sections=req.maxSections,
                hops=req.hops,
                max_rounds=req.maxRounds,
                temperature=req.temperature,
            ):
                yield sse(event)
        except Exception as e:  # noqa: BLE001  生成器里抛异常只会断流，前端什么都看不到
            yield sse({"type": "error", "message": f"深度研究运行失败：{e}"})

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=sse_headers())
