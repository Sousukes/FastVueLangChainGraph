"""请求 / 响应契约。

阶段 03：多轮对话（messages 全量回放）
阶段 04：结构化抽取（动态字段定义 + Pydantic 校验结果回传）
阶段 05：函数调用（工具说明书 + 调用 trace）
阶段 09：GraphRAG（实体 / 边 / 源块 / 三段时间）
阶段 10：单智能体（工具目录 + 步数预算 + 结构化 trace）
阶段 11：多智能体（角色工具面 + 任务拓扑分层 + 评审意见）
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


# ---------- 阶段 10 · 单智能体（ReAct） ----------


class AgentToolInfo(BaseModel):
    """工具说明书的一条。`server` 只有 MCP 工具才有值。

    `description` 单独回传，是因为它**不是装饰**——对 agent 来说 description 就是 prompt，
    它决定了模型会不会、以及什么时候选这个工具。前端把原文摊开显示，
    用户才能理解"改一句话，命中率就变了"。
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    server: str | None = None


class AgentToolGroup(BaseModel):
    """一组可开关的工具。

    让用户**亲手勾掉几组**、再问同一个问题，是理解"工具面"最直观的办法——
    比任何"工具越多越容易选错"的说教都管用。
    """

    group: str
    label: str
    tools: list[AgentToolInfo] = Field(default_factory=list)
    error: str | None = None


class AgentToolOverlap(BaseModel):
    """被**多个组同时提供**的工具。

    阶段 06 的 MCP server 就是阶段 05 那批工具的封装（`mcpkit/server.py` 里
    `for t in TOOLS.values()`），所以默认就存在 4 个重名。而 OpenAI 协议不允许重名工具，
    必须按优先级去重——`winner` 就是最终生效的那一组。

    **把重叠照实报出来，比悄悄藏起来有价值**：它正好说明"工具面是需要设计的"。
    """

    name: str
    groups: list[str] = Field(default_factory=list)
    winner: str


class AgentToolsResponse(BaseModel):
    """工具目录。`priority` 是去重优先级（即生效顺序）。"""

    groups: list[AgentToolGroup] = Field(default_factory=list)
    overlaps: list[AgentToolOverlap] = Field(default_factory=list)
    priority: list[str] = Field(default_factory=list)


class AgentRunRequest(BaseModel):
    """跑一次 ReAct。这几个旋钮**每一个都能单独做一次实验**：

    - `groups`：勾掉 RAG，看模型还会不会硬答课程问题
    - `max_steps`：调到 1，看它在信息不足时怎么收口
    - `observation_limit`：调到 200，看截断会不会让它漏掉关键信息
    - `max_repeat`：调到 1，死循环检测会立刻触发
    """

    question: str = Field(min_length=1)
    model: str | None = None
    max_steps: int = Field(default=6, ge=1, le=12, description="步数预算：用完就禁用工具、逼它收口")
    temperature: float = Field(default=0.2, ge=0.0, le=1.5)
    # 不在这里写默认值：默认组由 react.DEFAULT_GROUPS 定义，写两份迟早会不一致。
    # None = "用默认组"。
    groups: list[str] | None = None
    observation_limit: int = Field(
        default=1200, ge=200, le=4000, description="单条 Observation 的字符上限（防上下文膨胀）"
    )
    max_repeat: int = Field(
        default=2, ge=1, le=5, description="同一 (工具, 参数) 允许重复几次；超过就硬停"
    )


class AgentAction(BaseModel):
    """一次工具调用。`ok` 与 `ms` 是**把工具当"外部世界"来看**的两个数：

    调用可能失败（世界不总配合），也可能很慢（走 MCP 要跨进程）。
    """

    tool: str
    arguments: Any = None
    ok: bool
    ms: float = 0.0
    chars: int = 0


class AgentStep(BaseModel):
    """一轮 Thought → Action → Observation。`thought` 是这一轮模型写下的推理。"""

    step: int
    thought: str = ""
    llmMs: float = 0.0
    actions: list[AgentAction] = Field(default_factory=list)


class AgentRunResponse(BaseModel):
    """一次完整运行的收口结果。

    `reason` 有四种，**它们本身就是本阶段要讲的知识点**：
        answered   —— 模型自己认为信息够了（正常收口）
        exhausted  —— 步数用尽，被强制收口（预算纪律）
        loop       —— 检测到重复调用，被拒绝后收口（死循环纪律）
        error      —— 链路出错
    """

    question: str
    answer: str | None = None
    reason: str
    steps: int = 0
    model: str
    totalMs: float = 0.0
    llmMs: float = 0.0
    groups: list[str] = Field(default_factory=list)
    # 本轮**实际**交给模型的工具面。把它和工具目录对照，
    # 就能一眼看出哪些工具被去重挤掉了。
    tools: list[str] = Field(default_factory=list)
    trace: list[AgentStep] = Field(default_factory=list)
    error: str | None = None


# ---------- 阶段 11 · 多智能体 ----------


class TeamRoleInfo(BaseModel):
    """一个专职角色，**关键是 `tools`**。

    ⭐ 分工成不成立，一眼就能从这个字段看出来：如果两个角色的 `tools` 一模一样，
    它们其实是同一个智能体换了两个名字（"伪多智能体"）。
    所以工具面必须回传、必须显示。
    """

    name: str
    label: str
    groups: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    maxSteps: int = 1
    temperature: float = 0.2
    # 这个角色接哪些 kind 的任务（research / compute）
    kinds: list[str] = Field(default_factory=list)


class TeamRolesResponse(BaseModel):
    roles: list[TeamRoleInfo] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)


class TeamTask(BaseModel):
    """Planner 拆出来的一个子任务。

    `dependsOn` 是调度器唯一关心的字段——**它决定了哪些任务能并行**。
    留空 = 可以立刻开工 = 能吃到并行红利。
    """

    id: str
    kind: str
    description: str = ""
    dependsOn: list[str] = Field(default_factory=list)


class TeamRunRequest(BaseModel):
    """跑一次多智能体。`parallel` 这个开关是本阶段的核心实验：

    关掉它，所有任务串行执行——**耗时立刻变成并行的数倍**，
    这是理解"并行红利从哪来"最直接的一次对照。
    """

    question: str = Field(min_length=1)
    model: str | None = None
    parallel: bool = True
    max_tasks: int = Field(default=4, ge=1, le=6, description="最多拆几个子任务")
    observation_limit: int = Field(
        default=1200, ge=200, le=4000, description="单条 Observation 的字符上限"
    )


class TeamTaskResult(BaseModel):
    """一个子任务的执行结果。"""

    id: str
    kind: str = ""
    description: str = ""
    dependsOn: list[str] = Field(default_factory=list)
    role: str = ""
    answer: str | None = None
    ms: float = 0.0
    steps: int = 0
    reason: str | None = None
    # 这个角色**实际**拿到的工具面——用来验证分工是真的
    tools: list[str] = Field(default_factory=list)


class TeamIssue(BaseModel):
    """评审员挑出的一条问题。"""

    taskId: str
    problem: str = ""


class TeamRunResponse(BaseModel):
    """一次完整的多智能体运行。

    ⭐ `planMs` / `reviewMs` / `writeMs` 分开回传，是为了让"钱花在谁身上"可见——
    本阶段真正要回答的是「多付这几倍时间到底买到了什么」。
    """

    question: str
    answer: str | None = None
    model: str
    parallel: bool = True
    tasks: list[TeamTask] = Field(default_factory=list)
    # 拓扑分层结果：[[t1], [t2,t3], [t4]] —— 同层并行、跨层串行
    layers: list[list[str]] = Field(default_factory=list)
    results: list[TeamTaskResult] = Field(default_factory=list)
    handoffs: list[dict] = Field(default_factory=list)
    verdict: str | None = None
    issues: list[TeamIssue] = Field(default_factory=list)
    totalMs: float = 0.0
    llmMs: float = 0.0
    planMs: float = 0.0
    reviewMs: float = 0.0
    writeMs: float = 0.0
    error: str | None = None


# ---------- 阶段 12 · Agent Harness 框架 ----------


class HarnessRoleInfo(BaseModel):
    """harness 暴露的「一个可独立运行的角色」。

    和阶段 11 的 TeamRoleInfo 同构——因为多智能体的角色本就是 harness.Agent 的实例。
    这里再给一份，是让「选一个角色单独跑」的控制台有数据可绑，也顺手把
    `tools` 字段亮出来（分工成不成立，一眼看工具面）。
    """

    name: str
    label: str
    groups: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    maxSteps: int = 6
    temperature: float = 0.2
    description: str = ""


class HarnessRolesResponse(BaseModel):
    roles: list[HarnessRoleInfo] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class HarnessRunRequest(BaseModel):
    """跑一个独立的 harness Agent（单角色）。

    这既是教学控制台的后端，也是「harness 真能复用」的最小证明：
    多智能体里的检索员，和这里单独跑的检索员，是同一段 Agent 代码，
    区别只在构造参数。
    """

    role: str = Field(min_length=1, description="预设角色名：researcher / analyst / pure")
    question: str = Field(min_length=1)
    model: str | None = None
    maxSteps: int = Field(default=6, ge=1, le=12)
    temperature: float = 0.2
    observationLimit: int = Field(default=1200, ge=200, le=4000)


class HarnessRunResponse(BaseModel):
    """单次 harness 运行的结果（与 TeamTaskResult 同构，统一前端渲染）。"""

    role: str
    question: str
    answer: str | None = None
    model: str
    steps: int = 0
    totalMs: float = 0.0
    llmMs: float = 0.0
    tools: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    error: str | None = None


# ---------- 阶段 13 · Agentic RAG ----------


class AgenticChannelInfo(BaseModel):
    """一条可按轮次升级的检索通道。

    关键是 `round` 与 `why`：把「为什么会有下一轮」讲明白，用户才能预判
    自己在第几轮会看到什么，而不是遇到一个黑盒重试。
    """

    name: str
    label: str
    why: str = ""
    round: int = 1


class AgenticChannelsResponse(BaseModel):
    channels: list[AgenticChannelInfo] = Field(default_factory=list)


class AgenticRunRequest(BaseModel):
    """跑一次 Agentic RAG。

    注意这里**没有** `top_k` 之外的检索参数——检索方式由轮次决定（见 agentic.CHANNELS），
    不由调用方指定。这是有意的：**"该不该换个检索法"正是本阶段要交给智能体判断的事**。
    """

    question: str = Field(min_length=1)
    model: str | None = None
    maxRounds: int = Field(default=3, ge=1, le=3, description="最多几轮检索（每轮换一个通道）")
    topK: int = Field(default=3, ge=1, le=10)
    hops: int = Field(default=2, ge=1, le=3, description="图谱通道的扩展跳数")
    gradeLimit: int = Field(default=400, ge=100, le=1200, description="评级时每段截断的字符数")
    temperature: float = 0.2


class AgenticRunResponse(BaseModel):
    """Agentic RAG 的完整结果，带**全过程时间线**（前端据此渲染决策链路）。"""

    question: str
    answer: str | None = None
    model: str
    needRetrieval: bool | None = None
    routeReason: str = ""
    rounds: int = 0
    finalQuery: str = ""
    basedOn: str = "none"
    hits: list[dict] = Field(default_factory=list)
    times: dict[str, float] = Field(default_factory=dict)
    totalMs: float = 0.0
    llmMs: float = 0.0
    timeline: dict[str, list[dict]] = Field(default_factory=dict)
    error: str | None = None


class SearchRequest(BaseModel):
    """跑一次 AI 搜索（混合检索 + 带引用答案）。

    与阶段 13 不同，这里**不**暴露检索方式——固定走阶段 08 的混合检索 + 重排，
    因为搜索产品要的是「快而准」的即时响应，多轮决策回路反而是负担（见 search.py 顶部说明）。
    """

    question: str = Field(min_length=1)
    model: str | None = None
    topK: int = Field(default=6, ge=1, le=10, description="检索返回的候选片段数（即来源面板条数）")
    rerank: bool = Field(default=True, description="是否对混合检索结果做 Cross-encoder 重排")
    snippet: int = Field(default=220, ge=60, le=600, description="来源面板里每段预览截断的字符数")
    temperature: float = 0.3


class SearchSource(BaseModel):
    """答案里的一条来源（检索命中的一段），带「是否被引用」标记。

    `cited` 是本阶段的关键字段：它把「模型**声称**引用了」这件事**如实**标出来，
    但不保证忠实——忠实性留给阶段 15 Deep Research。
    """

    rank: int
    index: int
    title: str
    snippet: str
    score: float | None = None
    rerankScore: float | None = None
    cited: bool = False


class SearchResponse(BaseModel):
    """AI 搜索的完整结果：带引用的答案 + 可核对的来源面板。"""

    question: str
    answer: str | None = None
    model: str
    sources: list[SearchSource] = Field(default_factory=list)
    citations: list[int] = Field(default_factory=list, description="答案中实际出现的引用编号（已校验、去重、按序）")
    grounded: bool = False
    coverage: float = 0.0
    retrieved: int = 0
    times: dict[str, float] = Field(default_factory=dict)
    totalMs: float = 0.0
    llmMs: float = 0.0
    error: str | None = None


# ---------- 阶段 15 · Deep Research ----------


class ResearchRequest(BaseModel):
    """跑一次深度研究（规划大纲 → 逐节自适应检索 → 带引用报告 → 忠实性校验）。

    与阶段 14 不同，这里把"一个问题"拆成"一组子问题"，每个子问题各自跑一遍
    阶段 13 的自适应检索（route→grade→rewrite 升级通道），最后把所有小节合并成
    一篇全局重新编号引用的长报告，并用一次额外 LLM 调用做忠实性核查。
    """

    question: str = Field(min_length=1)
    model: str | None = None
    topK: int = Field(default=4, ge=1, le=10, description="每个子问题检索返回的候选片段数")
    rerank: bool = Field(default=True, description="是否对混合检索结果做 Cross-encoder 重排")
    snippet: int = Field(default=240, ge=60, le=600, description="来源面板里每段预览截断的字符数")
    maxSections: int = Field(
        default=4, ge=1, le=6, description="研究大纲的小节数（即要把问题拆成几个子问题去查）"
    )
    hops: int = Field(default=2, ge=1, le=3, description="图谱通道的扩展跳数")
    maxRounds: int = Field(default=3, ge=1, le=3, description="每个子问题最多几轮自适应检索")
    temperature: float = 0.3


class ResearchSectionSummary(BaseModel):
    """研究报告里的一节：它的子问题、检索情况与本地引用。"""

    index: int
    title: str
    subQuestion: str
    rounds: int = 0
    basedOn: str = "none"
    hitCount: int = 0
    citations: list[int] = Field(default_factory=list, description="本节答案里出现的（重编号后的）引用编号")
    grounded: bool = False


class ResearchSource(BaseModel):
    """全局来源面板里的一条（跨所有小节去重合并后）。"""

    rank: int
    index: int
    title: str
    snippet: str
    score: float | None = None
    rerankScore: float | None = None
    channel: str | None = None
    section: int | None = None
    cited: bool = False


class ResearchResponse(BaseModel):
    """深度研究的完整结果：带全局引用的大报告 + 逐节小结 + 来源面板 + 忠实性核查。"""

    question: str
    answer: str | None = None
    model: str
    sections: list[ResearchSectionSummary] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    citations: list[int] = Field(default_factory=list, description="全局引用编号（已校验、去重、按序）")
    grounded: bool = False
    coverage: float = 0.0
    faithful: bool = False
    faithfulness: float = 0.0
    unsupported: list[str] = Field(default_factory=list, description="忠实性核查中判为「资料无法支持」的结论原文")
    times: dict[str, float] = Field(default_factory=dict)
    totalMs: float = 0.0
    llmMs: float = 0.0
    error: str | None = None
