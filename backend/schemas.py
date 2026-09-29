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

from pydantic import BaseModel, ConfigDict, Field

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


# ---------- 阶段 16 · 多模态·图像 ----------


class VisionRequest(BaseModel):
    """跑一次图像理解（视觉问答 qa / 结构化抽取 extract）。

    与阶段 14/15 最大的不同：输入不再只有文本——多了一张图。`image` 接受两种形态：
    完整 data URL（`data:image/png;base64,...`）或纯 base64 字符串；服务端会**以魔数**
    （而不是声明的 mime）判断真实格式。`mode` 决定输出是一段「流式回答」还是一张「字段表」。
    """

    image: str = Field(min_length=16, description="图片：data URL 或纯 base64")
    question: str = Field(default="", description="qa 模式下的提问；extract 模式下可留空")
    mode: Literal["qa", "extract"] = Field(default="qa", description="qa=视觉问答；extract=结构化抽取")
    schemaHint: str | None = Field(default=None, description="extract 模式下描述要抽取哪些字段")
    model: str | None = None
    detail: Literal["auto", "low", "high"] = Field(default="auto", description="传给协议的图像精细度")
    temperature: float = 0.2


class VisionImageMeta(BaseModel):
    """图像解析出来的客观元数据（服务端嗅探魔数 + 读文件头得到，不依赖模型）。"""

    mime: str = Field(description="魔数嗅探出的真实 mime")
    format: str = Field(description="png / jpeg / gif / webp")
    bytes: int
    width: int | None = None
    height: int | None = None
    declared: str | None = Field(default=None, description="data URL 里**声明**的 mime（可能与魔数不符）")
    mismatch: bool = Field(default=False, description="声明的 mime 与魔数嗅探结果是否不一致")


class VisionField(BaseModel):
    """extract 模式抽取出的一个字段。"""

    label: str
    value: str = ""


class VisionResponse(BaseModel):
    """图像理解的完整结果：回答（或字段表）+ 图像元数据 + 耗时。"""

    mode: str
    model: str
    question: str | None = None
    answer: str | None = None
    summary: str | None = None
    fields: list[VisionField] = Field(default_factory=list)
    extracted: bool = Field(default=False, description="extract 模式下是否成功解析出结构化字段")
    image: VisionImageMeta | None = None
    times: dict[str, float] = Field(default_factory=dict)
    totalMs: float = 0.0
    llmMs: float = 0.0
    error: str | None = None


# ---------- 阶段 17 · 多模态·语音 ----------


class VoiceRequest(BaseModel):
    """跑一次语音问答的**文本侧编排**。

    ⚠️ 本阶段的关键边界（实测得出）：DeepSeek **没有**音频接口——
    `/audio/transcriptions`（语音转写）与 `/audio/speech`（语音合成）都返回 **404**。
    所以**音频的采集与播放由浏览器承担**（Web Speech API：`SpeechRecognition` 转写、
    `speechSynthesis` 朗读）。服务端拿到时**已经是文本**，它负责两件真正值得 LLM 做的事：

      ① **口语化改写**：把书面语改成**适合朗读**的短句，并去掉一切 Markdown 标记；
      ② **朗读稿规范化**（Text Normalization，TTS 前的真实环节）：把 `2026-09-21`、
         `¥2,158.50`、`USB-C` 这类**符号写法**改成「读出来的样子」。

    外加一件**确定性**的事：体检口语稿里残留了多少 Markdown 标记、朗读大约要几秒。
    """

    transcript: str = Field(min_length=1, description="ASR 转写文本（或用户直接输入的提问）")
    model: str | None = None
    style: Literal["brief", "explain", "step"] = Field(
        default="brief", description="口语风格：brief 简洁 / explain 展开 / step 分步口述"
    )
    maxChars: int = Field(default=220, ge=40, le=600, description="口语稿目标字数上限")
    temperature: float = 0.3


class VoiceTerm(BaseModel):
    """朗读稿规范化的一条对照：`2026-09-21` → `二零二六年九月二十一日`。

    ⚠️ 字段名用 `from_` + 别名 `"from"`：`from` 是 Python 关键字，不能直接当字段名。
    `populate_by_name=True` 让它既吃模型返回的 `{"from": ...}`，也能用字段名构造。
    FastAPI 的 `response_model_by_alias` **默认为 True**，所以响应 JSON 里仍是 `"from"`。
    """

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str = ""


class VoiceTermCheck(BaseModel):
    """对 `terms` 的**确定性核对**结果（把「模型自述」降级为「可核对」）。

    为什么需要它：`terms` 是模型的自述，**不是 diff**。实测抓到过——改写阶段自己就把
    `2026-09-17` 写成了汉字，于是规范化阶段只改了 `BX → B X`，对照表看起来像
    「数字没被动过」。这里的 `suspect` 就是那些「自述了、但对不上文本」的条目：

        阶段 14 `grounded`  声称引用了吗 → 阶段 15 `faithful` 声称被支持吗
        → 阶段 17 `termCheck` 自述的改写真的发生了吗
    """

    total: int = 0
    verified: int = Field(default=0, description="from 出现在口语稿、且 to 出现在朗读稿的条目数")
    suspect: list[VoiceTerm] = Field(default_factory=list, description="核对不上的条目（原样带回）")


class VoiceResponse(BaseModel):
    """语音问答的完整结果：转写原文 + 口语稿 + 朗读稿 + 规范化对照 + 体检数字。"""

    transcript: str
    answer: str | None = Field(default=None, description="口语化改写稿（无 Markdown、短句）")
    speak: str | None = Field(default=None, description="朗读稿（已做文本规范化，可直接交给 TTS）")
    terms: list[VoiceTerm] = Field(default_factory=list, description="文本规范化对照表")
    termCheck: VoiceTermCheck | None = Field(default=None, description="对 terms 的确定性核对结果")
    style: str = "brief"
    model: str
    markdownLeft: int = Field(default=0, description="口语稿里残留的 Markdown 标记数（确定性体检）")
    plainChars: int = Field(default=0, description="朗读稿的有效字数（中日韩字符数 + 拉丁词数）")
    estSeconds: float = Field(default=0.0, description="估算朗读时长（中文 4.5 字/秒 + 英文 2.5 词/秒）")
    times: dict[str, float] = Field(default_factory=dict)
    totalMs: float = 0.0
    llmMs: float = 0.0
    error: str | None = None


# ---------- 阶段 18 · Computer Use（仿制） ----------


class ComputerElement(BaseModel):
    """虚拟屏幕上的一个元素。

    ⭐ 本阶段最关键的设计：**这块 bbox 就是真值**。
    被操作的"电脑"是我们自己渲染的，所以每个按钮的中心坐标我们精确知道——
    于是模型的每一次点击都能被**确定性地**量出偏差，不用问它"你点得准吗"。
    """

    name: str = Field(description="元素标识，如 field_order / btn_submit")
    kind: str = Field(default="other", description="text_field / button / label / other")
    x: int
    y: int
    w: int
    h: int
    label: str = Field(default="", description="屏幕上显示的文案")
    dangerous: bool = Field(default=False, description="危险元素：未授权时点击会被拒绝")


class ComputerScreen(BaseModel):
    """虚拟屏幕规格。前端拿它画「点击落点叠加层」。"""

    width: int
    height: int
    elements: list[ComputerElement] = Field(default_factory=list)


class ComputerAction(BaseModel):
    """模型发出并被实际执行（或拒绝）的一次动作，附带确定性评分。"""

    step: int
    action: str = Field(description="screenshot / left_click / type / key / wait")
    x: int | None = None
    y: int | None = None
    text: str | None = None
    keys: str | None = None
    ok: bool = Field(default=True, description="是否被接受执行；False 表示非法/被沙箱拒绝")
    error: str | None = Field(default=None, description="被拒绝的原因（会原样回传给模型）")

    # ---- 定位判据：只有点击动作才有 ----
    target: str | None = Field(default=None, description="点击**实际**落在了哪个元素上；点在空白处则为 None")
    hit: bool = Field(default=False, description="是否落在某个元素内（False = 点空了）")
    errorPx: float | None = Field(
        default=None,
        description="仅点空时有值：落点到**最近元素矩形**的像素距离 —— 定位误差的直接读数",
    )
    centerOffsetPx: float | None = Field(
        default=None,
        description="仅命中时有值：落点到**目标元素中心**的像素距离。命中率高≠点得准 —— 这项才是精度",
    )
    lostKeys: bool = Field(
        default=False,
        description="这次 type 的按键被丢弃了（前一次点击没落在任何输入框）—— 定位失败最硬的证据",
    )
    note: str | None = Field(
        default=None,
        description="执行后回传给模型的那句观察结果 —— 前端动作时间线直接显示它，前端不必自己编话术",
    )


class ComputerRequest(BaseModel):
    """一次 Computer Use 会话的请求。"""

    task: str | None = Field(default=None, description="自然语言任务；留空则用内置模板")
    orderId: str = Field(default="ORDER-2026-0917", description="期望填进 ORDER ID 的值（ASCII，键盘只能打 ASCII）")
    amount: str = Field(default="2158.50", description="期望填进 AMOUNT 的值")
    date: str = Field(default="2026-09-21", description="期望填进 DATE 的值")
    model: str | None = None
    temperature: float = 0.2
    maxSteps: int = Field(default=8, ge=1, le=16)
    allowDangerous: bool = Field(
        default=False,
        description="是否允许点击危险按钮。默认 False —— 沙箱默认拦截，要求人工确认（真实 Computer Use 的安全红线）",
    )
    provider: Literal["auto", "deepseek", "claude"] = Field(
        default="auto",
        description=(
            "用哪个「大脑」（阶段 18B 新增）。auto = 配了 CLAUDE_API_KEY 就走 claude，否则 deepseek。"
            "⚠️ 这个参数只影响第②步「向模型要一个决策」——屏幕、沙箱、评分三段与它无关"
        ),
    )


class ComputerResponse(BaseModel):
    """一次 Computer Use 的完整结果 + 两套确定性读数。"""

    task: str
    model: str
    provider: str = Field(default="deepseek", description="这一轮实际用了哪个大脑：deepseek / claude")
    protocol: str | None = Field(
        default=None, description="该大脑说的模型协议（一句话），便于并排对照两种形状"
    )
    screen: ComputerScreen
    actions: list[ComputerAction] = Field(default_factory=list)
    steps: int = Field(default=0, description="实际跑了几轮（不含被拒的动作）")

    # ---- 任务级判据 ----
    state: dict[str, str] = Field(default_factory=dict, description="虚拟应用最终状态（各字段实际值）")
    fieldScore: int = Field(default=0, description="填对了几项（0–3）")
    fieldTotal: int = 3
    submitted: bool = False
    success: bool = Field(default=False, description="三项全对 **且** 已提交")

    # ---- 定位判据（本阶段新增的暗线）----
    clicks: int = Field(default=0, description="点击次数")
    hitClicks: int = Field(default=0, description="落在某个元素内的点击数")
    clickHitRate: float = Field(default=0.0, description="点击命中率 —— 有没有点中")
    avgClickErrorPx: float = Field(default=0.0, description="点空时离最近元素的平均像素距离（全部点中时为 0）")
    avgCenterOffsetPx: float = Field(
        default=0.0,
        description="命中时离目标元素中心的平均像素距离 —— 点得多准；全部点中的运行也有值，可横向比较",
    )
    lostKeystrokes: int = Field(
        default=0,
        description="键盘被丢弃的次数 —— 模型想打字但前一次点击没点中输入框；定位失败最硬的证据（纯观测，非模型自述）",
    )
    rejectedActions: int = Field(
        default=0,
        description=(
            "被沙箱/白名单拒绝的动作数。Claude 的动作词表比宿主宽（mouse_move / scroll / 双击…），"
            "所以走 Claude 时通常 > 0 —— 这是「动作词表由宿主决定」的正常表现，不是故障"
        ),
    )
    transcript: list[str] = Field(default_factory=list, description="模型每一轮的说明文字（它的'自述'）")

    times: dict[str, float] = Field(default_factory=dict)
    totalMs: float = 0.0
    llmMs: float = 0.0
    error: str | None = None


# ---------- 阶段 18B · provider 可用性 ----------


class ComputerProviderInfo(BaseModel):
    """一个可用大脑的自述：用不用得上、为什么用不上、走哪个端点。"""

    provider: str
    label: str
    protocol: str
    model: str
    available: bool = Field(description="现在能不能用（缺 Key 时为 False）")
    reason: str | None = Field(default=None, description="不可用的原因，直接展示给用户")
    keyEnv: str = Field(description="需要哪个环境变量（前端据此给配置指引）")
    endpoint: str
    toolVersion: str | None = Field(default=None, description="Claude 的 computer 工具版本")
    betaHeader: str | None = Field(default=None, description="与之**成对**的 anthropic-beta 头")


class ComputerProvidersResponse(BaseModel):
    """GET /api/computer/providers 的响应。"""

    providers: list[ComputerProviderInfo] = Field(default_factory=list)
    default: str = Field(description="provider=auto 时实际会选中哪个")
    note: str = Field(default="", description="一句话说明 auto 的取舍规则")
