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


# ---------- 阶段 07 · RAG（检索增强生成） ----------


class RagStatus(BaseModel):
    """知识库状态。embedModel 要露出来——换嵌入模型会让旧向量全部作废。"""

    collection: str
    documents: int
    chunks: int
    embedModel: str
    chunkSize: int
    chunkOverlap: int
    persistDir: str
    # 阶段 08 起
    rerankModel: str = ""
    rerankReady: bool = False


class RagDocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1)


class RagDocument(BaseModel):
    title: str
    chunks: int
    chars: int


class RagIngestResponse(BaseModel):
    """入库结果。preview 是**切块后的真实文本**，让用户看见"文档被切成了什么样"。"""

    title: str
    chunks: int
    chars: int
    preview: list[str] = Field(default_factory=list)


class RagHit(BaseModel):
    """一条检索命中。

    score 沿用阶段 07 的语义（**余弦相似度**，越大越像），这样两个阶段能直接对照。
    BM25 单独召回进来的块没有余弦分，score 记 0——"它不是靠语义进来的"。
    阶段 08 补充的三个字段用来解释**这一条为什么排在这里**。
    """

    title: str
    index: int
    score: float
    text: str
    vector_score: float | None = None
    bm25_score: float | None = None
    rerank_score: float | None = None


class RagRankRow(BaseModel):
    """排名对照表的一行：同一个块在各阶段的名次。

    这是阶段 08 前端的主角——把「向量第 25 名 → 融合第 3 名 → 重排第 1 名」
    这种变化直接摆出来，比任何文字解释都清楚。
    """

    title: str
    index: int
    text: str
    vector_rank: int | None = None
    bm25_rank: int | None = None
    fused_rank: int | None = None
    rerank_rank: int | None = None
    vector_score: float | None = None
    bm25_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    in_final: bool = False


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)
    # 阶段 08 起：默认仍是纯向量，保证阶段 07 的行为不变。
    mode: Literal["vector", "bm25", "hybrid"] = "vector"
    rerank: bool = False
    candidates: int = Field(default=10, ge=1, le=50)


class RagSearchResponse(BaseModel):
    query: str
    hits: list[RagHit] = Field(default_factory=list)
    mode: str = "vector"
    rerank: bool = False
    candidates: int = 0
    rows: list[RagRankRow] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class RagAskRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)
    model: str | None = None
    # 阶段 08 起
    mode: Literal["vector", "bm25", "hybrid"] = "vector"
    rerank: bool = False
    candidates: int = Field(default=10, ge=1, le=50)


class RagAskResponse(BaseModel):
    """RAG 问答结果。prompt 一并返回是本阶段的重点：

    「检索到的内容究竟是怎么进到模型里的」——把它摊开，RAG 才不是黑盒。
    """

    answer: str
    hits: list[RagHit] = Field(default_factory=list)
    prompt: str
    model: str
    mode: str = "vector"
    rerank: bool = False
    rows: list[RagRankRow] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class RagSeedResponse(BaseModel):
    imported: list[RagIngestResponse] = Field(default_factory=list)


class RagResetResponse(BaseModel):
    deleted: int


# ---------- 阶段 09 · GraphRAG（知识图谱） ----------


class GraphTypeCount(BaseModel):
    type: str
    count: int


class GraphRelationCount(BaseModel):
    relation: str
    count: int


class GraphStats(BaseModel):
    """图谱状态。`coverage` 是**最该看的一个数**：抽了多少块 / 总共多少块。

    它直接把本阶段的成本模型摆出来——覆盖率不是 100% 时，
    "图里没有"和"语料里没有"是两件事，回答时必须分清楚。
    """

    entities: int
    edges: int
    extractedChunks: int
    totalChunks: int
    coverage: float
    types: list[GraphTypeCount] = Field(default_factory=list)
    relations: list[GraphRelationCount] = Field(default_factory=list)
    # 封闭词表（图"可读"的前提）：类型用来配色，关系词用来做筛选
    entityTypes: list[str] = Field(default_factory=list)
    relationHints: list[str] = Field(default_factory=list)
    lastBuiltAt: str | None = None


class GraphBuildRequest(BaseModel):
    """构建请求。`limit` 是**必须存在的**旋钮，不是偷懒——

    每个块要跑一次 LLM，全量抽取是一笔真金白银的开销，
    必须让人能控制它，而且要让人**亲眼看到**它有多大。
    """

    limit: int = Field(default=24, ge=1, le=200, description="本轮最多抽多少块（每块一次 LLM 调用）")
    workers: int = Field(default=6, ge=1, le=12, description="并发数：抽取是 IO 密集，块间独立")
    model: str | None = None
    titles: list[str] | None = Field(
        default=None, description="只抽这些文档（不传=全语料）。收窄范围是让 GraphRAG 可用的关键一招"
    )


class GraphBuildResponse(BaseModel):
    processed: int
    alreadyExtracted: int
    remaining: int
    entities: int
    edges: int
    chunks: int
    seconds: float


class GraphNode(BaseModel):
    name: str
    type: str = "概念"
    mentions: int = 0
    # hop = 距离种子几跳。0 是种子本身，1 是直接相连，2 是隔一个节点
    hop: int = 0
    isSeed: bool = False
    degree: int = 0


class GraphEdge(BaseModel):
    """一条边。`sourceTitle` + `sourceIndex` 是**可溯源的锚点**——

    没有出处的三元组就是"没有引用的断言"，模型会把它当事实写进答案。
    """

    id: int
    head: str
    relation: str
    tail: str
    sourceTitle: str
    sourceIndex: int
    hop: int = 0


class GraphChunk(BaseModel):
    """回溯到的源块。`edgeCount` = 这个块被多少条边引用（越高越可能是枢纽）。"""

    id: str = ""
    title: str
    index: int
    text: str
    edgeCount: int = 0


class GraphEntityItem(BaseModel):
    name: str
    type: str
    mentions: int
    degree: int


class GraphEntitiesResponse(BaseModel):
    total: int
    items: list[GraphEntityItem] = Field(default_factory=list)


class GraphDocumentItem(BaseModel):
    title: str
    chunks: int
    extracted: int


class GraphSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    hops: int = Field(default=2, ge=1, le=3, description="扩展跳数：1 精确但窄，2 跨块但噪声多")
    top_k: int = Field(default=3, ge=1, le=10)


class GraphSearchResponse(BaseModel):
    """多跳检索结果。三段 timings 对应检索的三个环节：锚定 / 扩展 / 回溯。"""

    query: str
    seeds: list[str] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    chunks: list[GraphChunk] = Field(default_factory=list)
    hops: int = 2
    timings: dict[str, float] = Field(default_factory=dict)


class GraphAskRequest(BaseModel):
    question: str = Field(min_length=1)
    hops: int = Field(default=2, ge=1, le=3)
    top_k: int = Field(default=3, ge=1, le=10)
    model: str | None = None


class GraphAskResponse(BaseModel):
    """图谱问答。prompt 一并回传：**「图上的边是怎么变成 prompt 的」必须看得见**。"""

    question: str
    answer: str
    prompt: str
    model: str
    seeds: list[str] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    chunks: list[GraphChunk] = Field(default_factory=list)
    hops: int = 2
    timings: dict[str, float] = Field(default_factory=dict)


class GraphOverview(BaseModel):
    """全图（已截断）。300 个节点的图在屏幕上只是一团毛线，信息量为零。"""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class GraphResetResponse(BaseModel):
    deletedEntities: int
    deletedEdges: int
