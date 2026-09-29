"""阶段 10 · 单智能体（ReAct）：把「想」和「做」交替起来。

## ReAct 是什么

ReAct = **Reason + Act**（Yao et al., 2022）。一句话说清它的贡献：
**让模型在每一步行动之前，先把推理写出来，并让这段推理留在上下文里。**

    Thought      我现在知道什么、还缺什么、下一步为什么选这个工具
    Action       调哪个工具、传什么参数
    Observation  工具返回了什么
    ...（循环）
    Finish       信息够了，给出答案

## 和阶段 05 的 tool loop 差在哪

阶段 05 已经能"调工具"了，但那个循环缺三样东西，这三样正是本阶段的内容：

1. **显式的 Thought。** 阶段 05 里模型的输出只有两种可能——给答案，或给 tool_calls。
   推理过程**完全不可见**，也不进入上下文。于是下一步只能看着"上一步的原始结果"做决定，
   而不是"上一步的结论"。ReAct 要求它先写想法再动手，**把推理外化进上下文**。

2. **统一的工具面。** 阶段 05 只有 4 个玩具工具。到阶段 10，我们已经攒下了
   MCP（阶段 06）、RAG（阶段 07/08）、GraphRAG（阶段 09）——这一阶段把它们
   **收进同一张工具清单**，让模型自己决定用哪个。这就是"Agentic RAG"的起点。

3. **停止条件的纪律。** 阶段 05 只有 `max_steps`。真实的 agent 必须还会处理：
   **死循环**（同工具同参数反复调）、**上下文膨胀**（Observation 比 Thought 长得多）、
   **预算耗尽**（步数用完怎么收口）。

## 为什么 ReAct 能减少幻觉

因为**每一次断言都有机会被 Observation 打脸**。
模型在 Thought 里说"我记得切块是 400"，Action 去查一下，Observation 回"400"——
对上就继续，对不上就得改。而阶段 03 那种"直接回答"没有任何纠错机会。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

import graph
import rag
from llm import LLMClient
from tools import TOOLS, execute_tool

# ---------- 工具分组 ----------

GROUP_LOCAL = "local"
GROUP_RAG = "rag"
GROUP_GRAPH = "graph"
GROUP_MCP = "mcp"

GROUP_LABEL: dict[str, str] = {
    GROUP_LOCAL: "本地工具",
    GROUP_RAG: "课程检索（RAG）",
    GROUP_GRAPH: "知识图谱（GraphRAG）",
    GROUP_MCP: "MCP Server",
}
ALL_GROUPS = (GROUP_LOCAL, GROUP_RAG, GROUP_GRAPH, GROUP_MCP)
DEFAULT_GROUPS = (GROUP_LOCAL, GROUP_RAG, GROUP_GRAPH)


# ---------- 把已有能力包成工具 ----------
#
# ⚠️ 注意这里的 description 写法。对 agent 来说，**工具的 description 就是 prompt**——
# 它决定了模型会不会、以及什么时候选这个工具。
# 写得含糊，模型就会在错误的时候调它；写得具体（"什么时候该用我"），命中率立刻不一样。


class RagSearchArgs(BaseModel):
    """在课程文档库里做混合检索（向量 + BM25），返回最相关的段落。"""

    query: str = Field(description="要检索的问题或关键词。用自然语言问句效果最好")
    top_k: int = Field(default=3, ge=1, le=5, description="返回段落数，默认 3")


class GraphSearchArgs(BaseModel):
    """在课程知识图谱里查**关系**：谁依赖谁、谁属于谁、谁包含谁。"""

    query: str = Field(description="关系型问题，例如「重排是哪个阶段引入的」")
    hops: int = Field(default=2, ge=1, le=3, description="扩展跳数：1 精确但窄，2 平衡，3 噪声大")


# 每条命中最多留这么多字。**Observation 会被塞进上下文**，
# 一次返回 3 段 1000 字的原文，几步下来上下文就爆了。
_HIT_TEXT_LIMIT = 400


def _run_rag_search(query: str, top_k: int = 3) -> dict:
    """RAG 工具的实现。

    ⚠️ 这里**故意关掉了重排**（`rerank=False`）。重排一次要 600ms 左右，
    而 agent 可能在一个问题里调好几次检索——单次请求的延迟会被放大成总时长。
    **agent 的工具要在"准"和"快"之间重新取平衡**：阶段 08 里那个"重排必须开"
    的结论，到了 agent 场景就不再是无条件的了。
    """
    found = rag.hybrid_search(query, top_k=top_k, mode="hybrid", rerank=False, candidates=10)
    hits = found["hits"]
    if not hits:
        return {
            "query": query,
            "found": 0,
            "hint": "知识库为空或没有命中。可以先提示用户导入文档（/api/rag/seed）。",
        }
    return {
        "query": query,
        "found": len(hits),
        "hits": [
            {
                "source": f"{h['title']}#{h['index']}",
                "score": h["score"],
                "text": h["text"][:_HIT_TEXT_LIMIT],
            }
            for h in hits
        ],
    }


def _run_graph_search(query: str, hops: int = 2) -> dict:
    """GraphRAG 工具的实现。"""
    found = graph.graph_search(query, hops=hops, top_k=3)
    if not found["seeds"]:
        # ⚠️ 这个 hint 很关键。锚定失败时如果只回一个空数组，模型很可能**自己编一个答案**。
        # 明确告诉它"图上没有入口、这不是语料里没有"，它才会去换工具或如实说明。
        # **工具返回值是给模型读的 prompt，不是给程序读的日志。**
        return {
            "query": query,
            "anchored": [],
            "found": 0,
            "hint": (
                "图谱里没有匹配到这个问题的实体，整条链路空转。"
                "注意：这只说明「图谱里没有」，**不等于「课程里没有」**。"
                "可以改用 search_course_docs 检索原文，或换一种说法。"
            ),
        }
    return {
        "query": query,
        "anchored": found["seeds"],
        "found": len(found["edges"]),
        "relations": [
            f"{e['head']} —{e['relation']}→ {e['tail']}"
            f"（出处 {e['sourceTitle']}#{e['sourceIndex']}，{e['hop']} 跳）"
            for e in found["edges"][:20]
        ],
        "source_texts": [
            {"source": f"{c['title']}#{c['index']}", "text": c["text"][:_HIT_TEXT_LIMIT]}
            for c in found["chunks"]
        ],
    }


_WRAPPED: dict[str, tuple[str, type[BaseModel], Callable[..., dict], str]] = {
    "search_course_docs": (
        GROUP_RAG,
        RagSearchArgs,
        _run_rag_search,
        "在课程文档库里检索段落原文。**回答课程内容相关的问题时优先用它**——"
        "它检索的是全部 11 篇文档、412 个块。返回里带 source 字段（如 07-RAG基础#25），"
        "引用时请写出来。",
    ),
    "query_knowledge_graph": (
        GROUP_GRAPH,
        GraphSearchArgs,
        _run_graph_search,
        "在课程知识图谱里查**关系**（谁依赖谁、谁属于谁、谁包含谁）。"
        "适合「X 属于哪个阶段」「X 用了什么」这类问题——这类问题的答案往往"
        "不在任何一段原文里，只有图上才有。问的是事实而非关系时用 search_course_docs。",
    ),
}


def _group_tools(groups: Iterable[str] | None = None) -> tuple[dict[str, list[dict]], dict[str, str | None]]:
    """**唯一的"组 → 工具"真相来源。** 返回 `(按组分的工具清单, 每组的错误)`。

    为什么要收敛成一处？因为在写这个文件的过程中，`build_schemas` 和 `tool_catalog`
    各自维护了一份"哪个工具属于哪一组"的映射——两份一旦不同步，就会出现
    "schema 里有这个工具、执行器却不认"的幽灵 bug。**同一份事实只能有一个出处。**

    工具描述用统一的 dict 形态（而不是直接产出 OpenAI schema），
    因为前端目录需要多两个字段：`server`（MCP 工具来自哪个 server）。
    """
    wanted = set(ALL_GROUPS if groups is None else groups)
    by_group: dict[str, list[dict]] = {g: [] for g in ALL_GROUPS}
    errors: dict[str, str | None] = {g: None for g in ALL_GROUPS}

    for t in TOOLS.values():
        by_group[GROUP_LOCAL].append(
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.args_model.model_json_schema(),
                "server": None,
            }
        )

    for name, (group, model, _fn, desc) in _WRAPPED.items():
        by_group[group].append(
            {
                "name": name,
                "description": desc,
                "parameters": model.model_json_schema(),
                "server": None,
            }
        )

    # ⚠️ MCP 只在真的勾了它时才去碰——`get_host().describe()` 会**拉起一个子进程**，
    # 而 build_schemas 在每轮 run_react 开头、甚至拼错误信息时都会调。
    # 不勾 MCP 却每次都 fork 一个进程，是白白付的启动成本。
    if GROUP_MCP in wanted:
        try:
            from mcpkit.host import get_host

            for server in get_host().describe().get("servers", []):
                for t in server.get("tools", []):
                    by_group[GROUP_MCP].append(
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("inputSchema") or {},
                            "server": server.get("name", ""),
                        }
                    )
        except Exception as e:  # noqa: BLE001  MCP server 起不来不该让整个 agent 不可用
            errors[GROUP_MCP] = f"{type(e).__name__}: {e}"

    return by_group, errors


def _to_schema(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        },
    }


def build_schemas(groups: Iterable[str] = DEFAULT_GROUPS) -> list[dict]:
    """按勾选的组，拼出给模型的"工具说明书"。

    ⚠️ **这里必须去重，而且这不是洁癖。** 阶段 06 的 MCP server 把阶段 05 的同一批工具
    又暴露了一遍（`mcpkit/server.py` 里就是 `for t in TOOLS.values()`），所以"本地 + MCP
    全勾上"会让同一个 `calculator` 在清单里出现两次。而 OpenAI 协议**不允许重名工具**——
    DeepSeek 会直接返回 400，整个 agent 起不来。

    真实的 MCP host（Claude Desktop、Claude Code）用**命名空间**隔离：工具名会变成
    `mcp__<server>__<tool>`。本阶段用更朴素的做法：**同名去重，按组优先级保留第一个**
    （local > rag > graph > mcp，即 `ALL_GROUPS` 的顺序）。

    为什么本地优先？因为它**是进程内的一次函数调用**，而 MCP 版本要经过
    JSON-RPC 序列化 → 管道 → 子进程 → 再原路返回。同样一件事，本地快一个数量级。
    名字相同时把便宜的那个留给模型，是划算的。

    被挤掉的名字不会消失——`tool_catalog()["overlaps"]` 会把重叠情况照实列出来，
    前端显示给用户。**让用户看见"工具面重叠"，比悄悄藏起来有价值。**
    """
    wanted = set(groups)
    by_group, _errors = _group_tools(wanted)

    out: list[dict] = []
    seen: set[str] = set()
    for group in ALL_GROUPS:  # 顺序即优先级
        if group not in wanted:
            continue
        for tool in by_group[group]:
            if tool["name"] in seen:
                continue
            seen.add(tool["name"])
            out.append(_to_schema(tool))
    return out


def make_executor(groups: Iterable[str] = DEFAULT_GROUPS) -> Callable[[str, str], tuple[Any, str | None]]:
    """统一的工具执行器：`(name, raw_arguments) -> (result, error)`。

    和阶段 05/06 的执行器**签名完全一致**，所以上层循环不用改。
    """
    groups = set(groups)
    mcp_exec: Callable[[str, str], tuple[Any, str | None]] | None = None
    if GROUP_MCP in groups:
        from mcpkit.host import get_host

        try:
            mcp_exec = get_host().make_executor()
        except Exception:
            mcp_exec = None

    def execute(name: str, raw_arguments: str) -> tuple[Any, str | None]:
        # ① 包成工具的 RAG / GraphRAG
        entry = _WRAPPED.get(name)
        if entry is not None:
            group, model, fn, _desc = entry
            if group not in groups:
                return None, f"工具 {name} 当前未启用（没勾选「{GROUP_LABEL[group]}」）"
            try:
                raw = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as e:
                return None, f"arguments 不是合法 JSON：{e}（原始内容：{raw_arguments!r}）"
            try:
                args = model.model_validate(raw)
            except ValidationError as e:
                hints = "; ".join(
                    f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
                )
                return None, f"参数不符合 schema：{hints}"
            try:
                return fn(**args.model_dump()), None
            except Exception as e:  # noqa: BLE001
                return None, f"{type(e).__name__}: {e}"

        # ② 阶段 05 的本地工具
        #
        # ⚠️ 这里的条件必须带上 `GROUP_LOCAL in groups`，不能只判断 `name in TOOLS`。
        # 否则"只勾 MCP、不勾本地"时，`calculator` 会被这一支**抢先认领**，
        # 然后立刻因为本地组没启用而被拒绝——MCP 那一支永远走不到，
        # 模型会一直收到"工具未启用"，而它明明勾着 MCP。
        # 判断顺序要和 `build_schemas` 的去重优先级严格一致，否则 schema 和
        # 执行器就会各说各话（这是最容易埋进去、又最难查的一类 bug）。
        if name in TOOLS and GROUP_LOCAL in groups:
            return execute_tool(name, raw_arguments)

        # ③ MCP Server 提供的工具
        if mcp_exec is not None:
            result, error = mcp_exec(name, raw_arguments)
            if error is None or "未知工具" not in error:
                return result, error

        return None, f"未知工具 {name}。可用工具：{', '.join(s['function']['name'] for s in build_schemas(groups))}"

    return execute


def tool_catalog() -> dict:
    """给前端渲染工具勾选框的完整目录。

    让用户**亲手勾掉几组工具**，再问同一个问题——模型的表现会明显不同。
    这比任何"工具越多越容易选错"的说教都直观。

    返回三段：
      - `groups`    每组有哪些工具（勾选框用）
      - `overlaps`  **被多个组同时提供的工具**。阶段 06 的 MCP server 就是阶段 05
                    那批工具的封装，所以默认就存在 4 个重名。这里照实报出来，
                    并给出 `winner`——按优先级它最终会由哪一组生效。
      - `priority`  去重优先级（即 `ALL_GROUPS` 顺序），前端显示提示文案用。
    """
    by_group, errors = _group_tools()  # 不传 groups = 全都要（目录要显示全部）

    groups = [
        {
            "group": g,
            "label": GROUP_LABEL[g],
            "tools": by_group[g],
            "error": errors[g],
        }
        for g in ALL_GROUPS
    ]

    # 找出被多组同时提供的名字
    providers: dict[str, list[str]] = {}
    for g in ALL_GROUPS:
        for tool in by_group[g]:
            providers.setdefault(tool["name"], []).append(g)

    overlaps = [
        {
            "name": name,
            "groups": gs,
            # 按 ALL_GROUPS 的先后取第一个——和 build_schemas 的去重结果严格一致
            "winner": next(g for g in ALL_GROUPS if g in gs),
        }
        for name, gs in sorted(providers.items())
        if len(gs) > 1
    ]

    return {"groups": groups, "overlaps": overlaps, "priority": list(ALL_GROUPS)}


# ---------- ReAct 循环 ----------

REACT_SYSTEM = (
    "你是一个 ReAct 智能体。每一轮你必须严格按下面的顺序工作：\n"
    "\n"
    "1. **Thought（想）**：在正文里用一两句话写清楚——你现在知道什么、还缺什么、"
    "下一步为什么选这个工具。**每次调用工具之前都必须先写 Thought。**\n"
    "2. **Action（做）**：调用工具。只有多个工具**互不依赖**时才一次调多个；"
    "后一步要用到前一步结果的，必须分两轮。\n"
    "3. **Observation（看）**：工具结果会回给你。\n"
    "\n"
    "然后回到第 1 步，直到你有足够信息给出最终答案（不再调用工具即为结束）。\n"
    "\n"
    "纪律：\n"
    "- **课程相关的任何事实都必须先检索查证，不要凭记忆回答。**\n"
    "- 工具返回空或报错时，先读懂原因，再决定是换参数、换工具，还是如实说明。\n"
    "- **不要用完全相同的参数重复调用同一个工具**——结果不会变。\n"
    "- 信息够了就直接回答，不要为了多调一次工具而调。\n"
    "- 最终答案要写清依据（引用了哪些 source，如 `07-RAG基础#25`）。"
)

FORCE_FINISH = (
    "已经到达步数上限。**请不要再调用任何工具**，直接根据上面已有的信息给出最终答案。"
    "如果信息不足以回答，就如实说明缺什么，不要编造。"
)


def _render(result: Any, error: str | None) -> tuple[str, bool]:
    """把工具结果渲染成 observation 文本。返回 (文本, 是否成功)。"""
    if error is not None:
        return json.dumps({"error": error}, ensure_ascii=False), False
    if isinstance(result, str):
        return result, True
    return json.dumps(result, ensure_ascii=False), True


def _safe_args(raw: str) -> Any:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw


def _signature(name: str, args: Any) -> str:
    """调用签名，用来检测"完全相同的重复调用"。"""
    try:
        return f"{name}::{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
    except TypeError:
        return f"{name}::{args!r}"


def _forced_finish(
    client: LLMClient,
    messages: list[dict],
    model: str | None,
    temperature: float,
    reason: str,
    step: int,
    steps: list[dict],
    total_ms: float,
    t_start: float,
) -> Iterable[dict]:
    """步数耗尽 / 死循环之后，**禁用工具**逼模型收口。

    两件事值得说：

    1. **不给 `tools` 参数**，所以模型没有任何可调的东西，只能输出文本。
       这比"直接返回一个 None 表示未收敛"有用得多——用户至少拿到一个
       基于现有信息的答案，而不是一句"抱歉我没做完"。

    2. ⭐ **这里仍然流式。** 很容易顺手写成 `client.chat(...)`（反正就一句话），
       但那样用户会看到时间线**卡住好几秒，然后答案整段冒出来**，
       和前面几步逐字吐出的观感完全对不上。
       **一旦决定流式，就得全程流式**——一致性的价值大于省那几行代码。

    产出的 `delta` 用的是**同一个 step 号**，所以前端那条"收到 finish 就丢掉
    当前 step 的缓冲区"的规则照样适用，不需要为收口路径写特例。
    """
    messages.append({"role": "user", "content": FORCE_FINISH})
    answer = ""
    t_llm = time.perf_counter()
    for event in client.stream_message(messages, model=model, temperature=temperature):
        if "delta" in event:
            answer += event["delta"]
            yield {"type": "delta", "step": step, "text": event["delta"]}
        else:
            answer = event["message"].get("content") or answer
    llm_ms = round((time.perf_counter() - t_llm) * 1000, 1)

    yield {
        "type": "finish",
        "reason": reason,
        "answer": answer.strip(),
        "steps": step,
        "model": model or client.model,
        "totalMs": round((time.perf_counter() - t_start) * 1000, 1),
        "llmMs": round(total_ms + llm_ms, 1),
        "trace": steps,
    }


def run_react(
    client: LLMClient,
    question: str,
    *,
    model: str | None = None,
    max_steps: int = 6,
    temperature: float = 0.2,
    groups: Iterable[str] = DEFAULT_GROUPS,
    observation_limit: int = 1200,
    max_repeat: int = 2,
) -> Iterable[dict]:
    """跑一个完整的 ReAct 循环，**逐事件 yield**。

    写成生成器是刻意的：ReAct 的演示价值全在"看着它想"。
    路由层把它直接接进 SSE，每一帧就是时间线上的一行。

    ⭐ 顺带对比一下阶段 09：那边 `graph.build()` 是个**阻塞的黑盒**，
    所以必须"后台线程 + 队列"才能推进度；这里循环是**我们自己写的**，
    每一步都在我们的控制流里，直接 `yield` 就行。
    **能自己拆开的流程，就别用线程去绕。**

    事件类型：
        start / delta / action / observation / step_end / finish / error

    其中 `delta` 是**中性**的增量文本——它是 Thought 还是最终答案，
    要等这一步结束时看有没有 `action` 才能定性（见主循环里的长注释）。
    """
    groups = list(groups) or list(DEFAULT_GROUPS)
    schemas = build_schemas(groups)
    executor = make_executor(groups)

    yield {
        "type": "start",
        "question": question,
        "model": model or client.model,
        "groups": groups,
        "tools": [s["function"]["name"] for s in schemas],
        "maxSteps": max_steps,
        "observationLimit": observation_limit,
    }

    messages: list[dict] = [
        {"role": "system", "content": REACT_SYSTEM},
        {"role": "user", "content": question},
    ]
    steps: list[dict] = []
    used: dict[str, int] = {}
    total_ms = 0.0
    t_start = time.perf_counter()

    for step in range(1, max_steps + 1):
        # ---------- Thought（流式）+ Action（分片拼装） ----------
        thought = ""
        message: dict | None = None
        t_llm = time.perf_counter()
        for event in client.stream_message(
            messages, model=model, temperature=temperature, tools=schemas
        ):
            if "delta" in event:
                thought += event["delta"]
                # ⚠️ 这里**故意**不叫 `thought_delta`，而叫中性的 `delta`。
                #
                # 因为流式下有一个绕不开的歧义：**你没法提前知道这一轮的文本是
                # "思考"还是"最终答案"**——两者的开头长得一模一样，唯一的区别是
                # 这一轮结束时**有没有 tool_calls**。而流式恰恰意味着"结束之前就得吐出来"。
                #
                # 所以只能**先流出来、再定性**：
                #   - 如果随后收到该 step 的 `action` → 这段文本是 Thought，前端把它落进思考区；
                #   - 如果随后收到 `finish`        → 这段文本就是答案本身，前端**丢掉**
                #     这个缓冲区，改用 `finish.answer`（那是权威版本，还带了收口原因和 trace）。
                #
                # 这不是设计缺陷，是流式 agent 的常态。**"先输出、后标注"** 是它的基本手法。
                yield {"type": "delta", "step": step, "text": event["delta"]}
            else:
                message = event["message"]
        llm_ms = round((time.perf_counter() - t_llm) * 1000, 1)
        total_ms += llm_ms

        if message is None:  # 理论上不会发生；防御性处理
            yield {"type": "error", "message": "模型没有返回任何内容"}
            return

        calls = message.get("tool_calls") or []

        # 没有 Action = 模型认为信息够了 → 这就是 Finish
        if not calls:
            answer = (message.get("content") or "").strip()
            yield {
                "type": "finish",
                "reason": "answered",
                "answer": answer,
                "steps": step,
                "model": model or client.model,
                "totalMs": round((time.perf_counter() - t_start) * 1000, 1),
                "llmMs": round(total_ms, 1),
                "trace": steps,
            }
            return

        # assistant 这轮的话（含 tool_calls）必须原样写回，顺序不能反
        messages.append(message)

        step_record: dict = {"step": step, "thought": thought, "llmMs": llm_ms, "actions": []}
        loop_hit = False

        # ---------- 逐个执行 Action ----------
        for call in calls:
            name = call["function"]["name"]
            raw = call["function"]["arguments"]
            args = _safe_args(raw)
            sig = _signature(name, args)
            used[sig] = used.get(sig, 0) + 1
            repeats = used[sig] - 1  # 之前已经调过几次

            # ⚠️ 死循环硬停：同一个 (工具, 参数) 第 3 次出现就**拒绝执行**。
            # 继续跑只是烧钱——结果一定和上两次一样。
            if used[sig] > max_repeat:
                content = (
                    f"⛔ 拒绝执行：{name} 用**完全相同的参数**已经被调用过 {repeats} 次，"
                    "结果不会有任何变化。请换参数、换工具，或直接用已有信息回答。"
                )
                loop_hit = True
                ok = False
                ms = 0.0
            else:
                t_tool = time.perf_counter()
                result, error = executor(name, raw)
                ms = round((time.perf_counter() - t_tool) * 1000, 1)
                total_ms += ms
                content, ok = _render(result, error)
                # 重复调用（但还没到硬停）：明确告诉它"这次和上次一样"
                if repeats > 0:
                    content += (
                        f"\n\n⚠️ 注意：{name} 用完全相同的参数你已经调用过 {repeats} 次了，"
                        "本次结果与上次相同。请换一种做法。"
                    )

            raw_chars = len(content)
            truncated = raw_chars > observation_limit
            if truncated:
                # 截断会破坏 JSON 结构，但 observation 是**给模型读的**，不是给程序解析的，
                # 所以可以接受。真正该做的是在工具内部就限制返回量（见 _HIT_TEXT_LIMIT）。
                content = content[:observation_limit] + f"\n…（已截断，原始 {raw_chars} 字符）"

            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})

            yield {
                "type": "action",
                "step": step,
                "callId": call["id"],
                "tool": name,
                "arguments": args,
                "repeat": repeats,
            }
            yield {
                "type": "observation",
                "step": step,
                "callId": call["id"],
                "ok": ok,
                "content": content,
                "rawChars": raw_chars,
                "truncated": truncated,
                "ms": ms,
            }
            step_record["actions"].append(
                {"tool": name, "arguments": args, "ok": ok, "ms": ms, "chars": raw_chars}
            )

        steps.append(step_record)

        # ---------- 上下文预算 ----------
        # 用字符数当预算的代理指标。真实项目会换成 tokenizer，
        # 但**"Observation 比 Thought 长得多"这个结论与度量单位无关**。
        context_chars = sum(len(m.get("content") or "") for m in messages)
        yield {"type": "step_end", "step": step, "contextChars": context_chars}

        # ---------- 死循环收口 ----------
        if loop_hit:
            yield from _forced_finish(
                client, messages, model, temperature, "loop", step, steps, total_ms, t_start
            )
            return

        # ---------- 最后一轮：禁用工具，逼它收口 ----------
        # 比"直接返回 None（未收敛）"有用得多——用户至少拿到一个基于现有信息的答案。
        if step == max_steps:
            yield from _forced_finish(
                client, messages, model, temperature, "exhausted", step, steps, total_ms, t_start
            )
            return


def run_react_blocking(client: LLMClient, question: str, **kwargs) -> dict:
    """把生成器跑完，收成一个完整结果（给不需要流式的调用方用）。

    事件流本身信息很全，但直接甩给前端太啰嗦——这里把它折成一份
    "结果 + 结构化 trace"。**trace 必须留下**：没有它，agent 就还是个黑盒，
    而这门课从头到尾在讲的就是"把黑盒拆开看"。
    """
    events = list(run_react(client, question, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)

    base = {
        "question": question,
        "model": (finish or {}).get("model") or kwargs.get("model") or client.model,
        "groups": start.get("groups", []),
        "tools": start.get("tools", []),
        "events": events,
    }
    if finish is None:
        err = next((e for e in events if e["type"] == "error"), None)
        return {
            **base,
            "answer": None,
            "reason": "error",
            "steps": 0,
            "trace": [],
            "error": (err or {}).get("message", "未知错误"),
        }
    return {
        **base,
        "answer": finish["answer"],
        "reason": finish["reason"],
        "steps": finish["steps"],
        "totalMs": finish.get("totalMs", 0.0),
        "llmMs": finish.get("llmMs", 0.0),
        "trace": finish.get("trace", []),
        "error": None,
    }
