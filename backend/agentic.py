"""阶段 13 · Agentic RAG（把检索交还给智能体决定）。

⭐ 先说清楚它要解决什么问题——否则「Agentic RAG」只是个时髦词

阶段 07/08 的 RAG 是一条**固定管道**：

    用户提问 → 无论如何都检索 top-k → 拼进 prompt → 生成

这条管道有三个**结构性**缺陷，而且都会**静默失败**（答得理直气壮，但其实是错的）：

    1. **不该查的也查**：问「1+1 等于几」照样去向量库里捞一圈，
       捞回来的东西既浪费 token，还可能把模型带偏。
    2. **检索失手没人知道**：召回错了 top-k，答案就悄悄降质——
       既不会报错，也不会重试。
    3. **没有「够不够」的判断**：没有人问过「这几段到底能不能回答这个问题」。

本阶段把这三处「硬编码的决定」换成**三个可以被审视的决策**：

    ① 要不要检索（route）   —— 这一步省下的是"不该花的钱"
    ② 检索到的够不够用（grade）—— 这一步省下的是"悄悄降质的答案"
    ③ 不够用怎么办（rewrite / 升级通道）—— 这一步省下的是"一次失手就认命"

    Agentic RAG 的实质不是"用了智能体"，而是**把检索从固定管道改成了
    带反馈回路的控制系统**。

⭐ 为什么不是「给 ReAct 加个检索工具」就好了

阶段 10 的 ReAct 配上 rag 工具组，模型确实**能**决定要不要搜。但那条路上，
检索质量这件事仍然**只存在于模型的上下文里**——它搜得准不准、为什么重试，
外部一概不可见。本阶段刻意把「评级」和「改写」做成**独立的、可观测的步骤**，
代价是多几次 LLM 调用，换来的是每一步都能摆在时间线上给人看。

⭐ 检索通道按轮次升级（复用阶段 07 / 08 / 09）

    第 1 轮  纯向量（阶段 07）          —— 最便宜，先试
    第 2 轮  混合 + Cross-encoder 重排（阶段 08）—— 语义漏掉的关键词在这里补回来
    第 3 轮  知识图谱（阶段 09）        —— 多跳 / 关系型问题在这里救场

这是本阶段最值得记住的一点：**被反思的不只是 query，还可以是"检索方式"本身。**
前三阶段的检索资产因此各归其位，而不是被一个"更强的智能体"替代掉。

⭐ 关于「结构化输出 + 自纠」这段逻辑的重用

这里又需要一次「JSON 模式 + Pydantic 校验 + 一次自纠」（阶段 04 的招式，
阶段 11 的 team._chat_json 是第二处）。阶段 12 定过规矩：
**一份逻辑出现第三次，才值得抽成框架**——agentic 是第三处了，本该抽。
但抽到 `llm.py` 会波及阶段 11 已发布的代码与两处殉测试，收益不抵风险。
所以本文件自带一份紧凑实现，并把这笔技术债显式记在下方的 TECH_DEBT 注释里。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from pydantic import BaseModel, Field, ValidationError

import graph
import rag
from llm import LLMClient

# ⚠️ 技术债（阶段 13 记账）：_json_call / _strip_fence / _validation_hint
#    与 team.py 里的同名逻辑重复。下一处再需要时（第三次），应连同 team.py
#    一起下沉到 llm.py 或新建 llmkit/，并同步更新 _t_team_static.py。


# ---------- 提示词：每个决策点一个，职责单一 ----------

ROUTE_SYSTEM = (
    "你要判断：回答用户的问题，**是否需要查阅外部资料库**。\n"
    "需要查阅的情形：涉及具体事实、专有名词、课程里讲过的某个做法/章节/数值，\n"
    "                或者你自己没有把握、凭常识可能答错的内容。\n"
    "不需要查阅的情形：通用常识、纯计算、纯代码写法、对用户自己给出的文本做操作。\n"
    "不要因为「多查总没错」就选需要——不必要的检索会引入噪声，也会拖慢回答。\n"
    "⚠️ 但请特别注意方向相反的那个错误，它更隐蔽：只要问题落在资料库可能覆盖的\n"
    "主题域内（检索、切块、重排、工具调用、智能体这类课程主题），即使你觉得自己\n"
    "答得上来，也应当检索——**资料里的讲法可能和你的常识不同**，凭记忆作答等于\n"
    "替用户做了一次他并不知情的取舍。只在「纯计算 / 通用常识 / 与资料库主题无关」\n"
    "这类明确情形才选不需要。"
)

GRADE_SYSTEM = (
    "你是检索质量评审员。给你**用户问题**和若干条**候选片段**，请判断：\n"
    "1. 哪些片段真的有助于回答这个问题（只看相关性，不看文笔）；\n"
    "2. 综合起来，这些片段**是否足够**支撑一个完整回答。\n"
    "判定标准要严格：只沾一点边的、只提到关键词但没给出实质信息的，都不算有用。\n"
    "如果不足，请在 missing 里说明**具体还缺什么**（一句话），不要写「信息不足」这种空话。"
)

REWRITE_SYSTEM = (
    "上一次检索没能找到足够的资料。请**改写检索词**，让它更可能命中目标。\n"
    "常见改法：换成资料里更可能出现的说法/术语、补充限定范围、去掉修饰性的口语、\n"
    "          把代词指代的对象补回来、把蕴含的意图说破。\n"
    "只输出**用于检索的短语**，不要写成完整问句，也不要回答用户的问题。"
)

ANSWER_SYSTEM = (
    "你是一个严谨的研究助手。请**只依据**提供的「参考资料」回答用户问题。\n"
    "规则：\n"
    "1. 每条结论都要注明出处，格式：《标题》第 N 段；\n"
    "2. 资料里没有的，直接说「资料里没有相关内容」，**不要凭记忆补充**；\n"
    "3. 不要编造资料里不存在的细节。\n"
    "宁可承认不知道，也不要给一个看起来很像样的答案。"
)

DIRECT_SYSTEM = (
    "你是一个严谨的助手。这个问题不需要查阅资料，请直接作答。\n"
    "简洁、准确、有结构即可；如果问题本身有歧义，先用一句话点明。"
)

ROUTE_HINT = '{"needRetrieval": true/false, "reason": "一句话说明理由"}'
GRADE_HINT = '{"useful": true/false, "keep": [0, 2], "missing": "还缺什么"}'
REWRITE_HINT = '{"query": "改写后的检索词", "why": "为什么这么改"}'


# ---------- 结构化输出模型（阶段 04 的招式） ----------


class RouteDecision(BaseModel):
    needRetrieval: bool
    reason: str = ""


class GradeVerdict(BaseModel):
    useful: bool
    keep: list[int] = Field(default_factory=list)
    missing: str = ""


class RewritePlan(BaseModel):
    query: str = Field(min_length=1)
    why: str = ""


# ---------- 「JSON + 校验 + 一次自纠」 ----------


def _strip_fence(text: str) -> str:
    """模型有时会把 JSON 包在 ```json ... ``` 里，剥掉再解析。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _validation_hint(e: ValidationError) -> str:
    return "\n".join(
        f"- 字段 {'.'.join(str(x) for x in err['loc'])}：{err['msg']}" for err in e.errors()
    )


def _json_call(
    client: LLMClient,
    messages: list[dict],
    model: str | None,
    temperature: float,
    validator: type[BaseModel],
    schema_hint: str,
) -> tuple[BaseModel | None, str]:
    """JSON 模式 + Pydantic 校验 + **一次自纠重试**，返回 (对象 | None, 原文)。

    三个决策点（route / grade / rewrite）都收口到这里：让多智能体那边的经验
    （角色之间的接口要像函数签名一样硬）在这里同样成立——**决策结果必须是结构化对象**，
    绝不能让模型自由发挥一段话，然后由下游去猜。
    """
    try:
        raw = client.chat(
            messages, model=model, temperature=temperature, response_format={"type": "json_object"}
        )
    except Exception:  # noqa: BLE001  某些厂商不认 response_format，直接降级
        raw = client.chat(messages, model=model, temperature=temperature)

    for attempt in (1, 2):
        try:
            return validator.model_validate_json(_strip_fence(raw)), raw
        except (ValidationError, json.JSONDecodeError) as e:
            if attempt == 2:
                return None, raw
            hint = (
                _validation_hint(e)
                if isinstance(e, ValidationError)
                else f"- 输出不是合法 JSON：{e}"
            )
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"你的输出没能通过校验：\n{hint}\n\n"
                        f"请严格按下面的 JSON 重来一次，不要加解释：\n{schema_hint}"
                    ),
                },
            ]
            raw = client.chat(messages, model=model, temperature=temperature)
    return None, raw  # pragma: no cover


# ---------- 检索通道：按轮次升级，复用阶段 07 / 08 / 09 ----------

CHANNELS: list[dict] = [
    {"name": "vector", "label": "向量检索（阶段 07）", "mode": "vector", "rerank": False},
    {"name": "hybrid", "label": "混合检索 + 重排（阶段 08）", "mode": "hybrid", "rerank": True},
    {"name": "graph", "label": "知识图谱（阶段 09）", "mode": "graph", "rerank": False},
]

CHANNEL_WHY = {
    "vector": "语义相近优先，最便宜，先试一轮",
    "hybrid": "向量漏掉的关键词用 BM25 补回来，再用 Cross-encoder 重排",
    "graph": "换成关系视角：先锚定实体再扩展邻居，适合多跳 / 关系型问题",
}


def channel_catalog() -> dict:
    """给前端看的通道清单（`/api/agentic/channels`）。

    把「为什么会有下一轮」讲明白，比单纯列个下拉框有用——用户要能预判
    自己在第几轮会看到什么。
    """
    return {
        "channels": [
            {"name": c["name"], "label": c["label"], "why": CHANNEL_WHY[c["name"]], "round": i + 1}
            for i, c in enumerate(CHANNELS)
        ]
    }


def _retrieve(channel: dict, query: str, top_k: int, hops: int) -> dict:
    """跑一次检索，统一归一成 `[{title, index, text, score, channel}]`。

    归一这一步不是啰嗦：三个通道的返回原本各不相同（hybrid 给 score/各种 _rank，
    graph 给 chunks + edgeCount），不归一的话，后面的「评级」和「引用」就得
    为每个来源各写一份分支——那才是真正的技术债。
    """
    started = time.perf_counter()
    name = channel["name"]

    if name == "graph":
        found = graph.graph_search(query, hops=hops, top_k=top_k)
        hits = [
            {
                "title": c["title"],
                "index": int(c["index"]),
                "text": c["text"],
                "score": None,  # 图谱走的是"被几条边引用"，没有余弦相似度
                "edgeCount": c.get("edgeCount", 0),
                "channel": name,
            }
            for c in found.get("chunks", [])
        ]
        detail = {
            "seeds": found.get("seeds", []),
            "edgeCount": len(found.get("edges", [])),
            "timings": found.get("timings", {}),
        }
    else:
        found = rag.hybrid_search(
            query, top_k=top_k, mode=channel["mode"], rerank=channel["rerank"]
        )
        hits = [
            {
                "title": h["title"],
                "index": int(h["index"]),
                "text": h["text"],
                "score": h.get("score"),
                "rerankScore": h.get("rerank_score"),
                "channel": name,
            }
            for h in found.get("hits", [])
        ]
        detail = {"timings": found.get("timings", {})}

    return {
        "hits": hits,
        "detail": detail,
        "ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _grade_messages(question: str, hits: list[dict], limit: int) -> list[dict]:
    blocks = [
        f"[{i}] 《{h['title']}》第 {h['index']} 段\n{h['text'][:limit]}"
        for i, h in enumerate(hits)
    ]
    return [
        {"role": "system", "content": GRADE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"用户问题：{question}\n\n候选片段：\n\n" + "\n\n".join(blocks)
                if blocks
                else f"用户问题：{question}\n\n候选片段：（本次检索没有返回任何内容）"
            ),
        },
        {"role": "user", "content": f"请严格按下面的 JSON 输出：\n{GRADE_HINT}"},
    ]


def _answer_messages(question: str, hits: list[dict]) -> list[dict]:
    """生成阶段的 prompt——**直接复用**阶段 07 的 `rag.build_prompt`。

    这是有意不为：拼 prompt 的格式必须和阶段 07/08 一字不差，
    否则「Agentic RAG 只是多了几层决策」这件事就说不清了——差异会被格式噪声盖住。
    """
    return [
        {"role": "system", "content": ANSWER_SYSTEM},
        {"role": "user", "content": rag.build_prompt(question, hits)},
    ]


# ---------- 主流程 ----------


def run_agentic(
    client: LLMClient,
    question: str,
    *,
    model: str | None = None,
    max_rounds: int = 3,
    top_k: int = 3,
    hops: int = 2,
    grade_limit: int = 400,
    temperature: float = 0.2,
) -> Iterator[dict]:
    """跑一次 Agentic RAG，**逐事件 yield**。

    事件类型（与阶段 10/11/12 统一的"一帧一行"协议）：

        start          问题、模型、检索计划
        route          是否检索 + 理由  ← 决策点 ①
        retrieve       第几轮、走了哪个通道、拿到什么  ← 每轮一次
        grade          够不够、留下了哪几段、还缺什么  ← 决策点 ②
        rewrite        检索词从什么改成了什么、为什么  ← 决策点 ③
        answer_delta   最终答案的增量文本
        finish         答案 + 引用 + 各阶段耗时
        error          失败原因

    ⭐ 写成生成器同样是与阶段 09 那条经验呼应：**能自己拆开的流程，就别用线程去绕。**
    """
    started = time.perf_counter()
    buckets = {"route": 0.0, "retrieve": 0.0, "grade": 0.0, "rewrite": 0.0, "answer": 0.0}
    rounds_used = 0

    yield {
        "type": "start",
        "question": question,
        "model": model or client.model,
        "maxRounds": max_rounds,
        "topK": top_k,
        "plan": [{"round": i + 1, "channel": c["label"]} for i, c in enumerate(CHANNELS[:max_rounds])],
    }

    # ---------- 决策点 ①：要不要检索 ----------
    t0 = time.perf_counter()
    decision, raw = _json_call(
        client,
        [
            {"role": "system", "content": ROUTE_SYSTEM},
            {"role": "user", "content": f"用户问题：{question}"},
            {"role": "user", "content": f"请严格按下面的 JSON 输出：\n{ROUTE_HINT}"},
        ],
        model,
        temperature,
        RouteDecision,
        ROUTE_HINT,
    )
    buckets["route"] = round((time.perf_counter() - t0) * 1000, 2)

    if decision is None:
        # 连"要不要查"都判断不出来时，保守起见照查——宁可多查，不能少查。
        # （这与 stage 04 的"解析失败就如实报错"不同：这里失败并不阻塞主流程，
        #   降级到"总是检索"仍等价于阶段 07 的行为，不会更差。）
        yield {
            "type": "route",
            "needRetrieval": True,
            "reason": f"路由解析失败，降级为「照常检索」：{raw[:120]}",
            "degraded": True,
            "ms": buckets["route"],
        }
        need = True
    else:
        need = decision.needRetrieval
        yield {
            "type": "route",
            "needRetrieval": need,
            "reason": decision.reason,
            "degraded": False,
            "ms": buckets["route"],
        }

    # ---------- 不需要检索：直接回答 ----------
    if not need:
        answer = ""
        t0 = time.perf_counter()
        for piece in client.stream(
            [
                {"role": "system", "content": DIRECT_SYSTEM},
                {"role": "user", "content": question},
            ],
            model=model,
            temperature=temperature,
        ):
            answer += piece
            yield {"type": "answer_delta", "text": piece}
        buckets["answer"] = round((time.perf_counter() - t0) * 1000, 2)
        yield {
            "type": "finish",
            "answer": answer,
            "hits": [],
            "rounds": 0,
            "basedOn": "none",
            "times": {**buckets},
            "totalMs": round((time.perf_counter() - started) * 1000, 2),
            "llmMs": round(buckets["route"] + buckets["answer"], 2),
        }
        return

    # ---------- 需要检索：检索 → 评级 → 改写 的反馈回路 ----------
    query = question
    best_hits: list[dict] = []
    rounds_used = 0

    for round_no in range(1, max_rounds + 1):
        rounds_used = round_no
        channel = CHANNELS[min(round_no - 1, len(CHANNELS) - 1)]

        found = _retrieve(channel, query, top_k, hops)
        buckets["retrieve"] = round(buckets["retrieve"] + found["ms"], 2)
        hits = found["hits"]
        yield {
            "type": "retrieve",
            "round": round_no,
            "channel": channel["name"],
            "channelLabel": channel["label"],
            "query": query,
            "hits": hits,
            "detail": found["detail"],
            "ms": found["ms"],
        }

        # 决策点 ②：够不够
        t0 = time.perf_counter()
        if not hits:
            # 空结果不必问模型——没有东西可评，直接判不足，省一次调用
            verdict: GradeVerdict | None = GradeVerdict(
                useful=False, keep=[], missing="本次检索没有返回任何片段"
            )
            raw = ""
        else:
            verdict, raw = _json_call(
                client,
                _grade_messages(question, hits, grade_limit),
                model,
                temperature,
                GradeVerdict,
                GRADE_HINT,
            )
        grade_msgs = round((time.perf_counter() - t0) * 1000, 2)
        buckets["grade"] = round(buckets["grade"] + grade_msgs, 2)

        if verdict is None:
            # 评不出来：保守地**全部留下**，让人来判断，而不是擅自丢弃资料
            verdict = GradeVerdict(
                useful=True, keep=list(range(len(hits))), missing=f"评级解析失败：{raw[:120]}"
            )

        keep = sorted({i for i in verdict.keep if 0 <= i < len(hits)})
        yield {
            "type": "grade",
            "round": round_no,
            "useful": verdict.useful,
            "kept": keep,
            "missing": verdict.missing,
            "degraded": verdict.missing.startswith("评级解析失败"),
            "ms": grade_msgs,
        }

        if keep:
            best_hits = [hits[i] for i in keep]

        if verdict.useful and keep:
            break
        if round_no == max_rounds:
            break

        # 决策点 ③：改写检索词，换下一轮（通道也升级）
        t0 = time.perf_counter()
        plan, praw = _json_call(
            client,
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"用户问题：{question}\n"
                        f"上一轮用的检索词：{query}\n"
                        f"没找到的关键信息：{verdict.missing or '未知'}\n"
                        f"下一轮将改用通道：{CHANNELS[round_no]['label']}"
                    ),
                },
                {"role": "user", "content": f"请严格按下面的 JSON 输出：\n{REWRITE_HINT}"},
            ],
            model,
            temperature,
            RewritePlan,
            REWRITE_HINT,
        )
        buckets["rewrite"] = round(buckets["rewrite"] + (time.perf_counter() - t0) * 1000, 2)

        if plan is None:
            yield {
                "type": "rewrite",
                "round": round_no,
                "from": query,
                "to": query,
                "why": f"改写失败，下一轮沿用原检索词：{praw[:120]}",
                "degraded": True,
            }
            continue
        yield {
            "type": "rewrite",
            "round": round_no,
            "from": query,
            "to": plan.query,
            "why": plan.why,
            "degraded": False,
        }
        query = plan.query

    # ---------- 生成最终答案 ----------
    answer = ""
    t0 = time.perf_counter()
    messages = (
        _answer_messages(question, best_hits)
        if best_hits
        else [
            {"role": "system", "content": ANSWER_SYSTEM},
            {
                "role": "user",
                "content": (
                    rag.build_prompt(question, [])
                    + "\n\n补充：多轮检索均未取到足够资料，请如实说明无法回答，不要编造。"
                ),
            },
        ]
    )
    for piece in client.stream(messages, model=model, temperature=temperature):
        answer += piece
        yield {"type": "answer_delta", "text": piece}
    buckets["answer"] = round((time.perf_counter() - t0) * 1000, 2)

    yield {
        "type": "finish",
        "answer": answer,
        "hits": best_hits,
        "rounds": rounds_used,
        "finalQuery": query,
        "basedOn": best_hits[0]["channel"] if best_hits else "none",
        "times": {**buckets},
        "totalMs": round((time.perf_counter() - started) * 1000, 2),
        "llmMs": round(buckets["route"] + buckets["grade"] + buckets["rewrite"] + buckets["answer"], 2),
    }


def run_agentic_blocking(client: LLMClient, question: str, **kwargs: Any) -> dict:
    """把生成器跑完，收成一份完整结果（给不需要流式的调用方用）。"""
    events = list(run_agentic(client, question, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)
    route = next((e for e in events if e["type"] == "route"), {})
    grades = [e for e in events if e["type"] == "grade"]
    rewrites = [e for e in events if e["type"] == "rewrite"]
    retrieves = [e for e in events if e["type"] == "retrieve"]

    base: dict[str, Any] = {
        "question": question,
        "model": (finish or {}).get("model") or start.get("model") or kwargs.get("model") or client.model,
        "needRetrieval": route.get("needRetrieval"),
        "routeReason": route.get("reason", ""),
        "events": events,
        "timeline": {
            "retrieve": retrieves,
            "grade": grades,
            "rewrite": rewrites,
        },
    }
    if finish is None:
        err = next((e for e in events if e["type"] == "error"), None)
        return {**base, "answer": None, "error": (err or {}).get("message", "未知错误")}
    return {
        **base,
        "answer": finish["answer"],
        "hits": finish["hits"],
        "rounds": finish["rounds"],
        "finalQuery": finish.get("finalQuery", question),
        "basedOn": finish["basedOn"],
        "times": finish["times"],
        "totalMs": finish["totalMs"],
        "llmMs": finish["llmMs"],
        "error": None,
    }
