"""阶段 15 · Deep Research（把检索交还给规划 + 忠实性校验）。

阶段 14 做到了「答案带引用、可核对」，但它只能验证「模型**声称**引用了」，
不保证模型真的只用了那些资料、那段话真的支持结论（忠实性 / faithfulness）。
阶段 13 做到了「自适应检索」，但它只为单轮问答服务。

本阶段把两者组合，并加上**长程规划**与**忠实性校验**，做成一个「深度研究代理」：

    ① 规划（plan）     把研究问题拆成若干子问题（研究大纲）；
    ② 逐节研究（research）每个子问题跑**自适应检索**（复用阶段 13 的
       agentic.run_agentic_blocking 取命中片段），再用阶段 14 的引用合成
       把片段变成带 [n] 的小结；
    ③ 合并（merge）    把各节小结**全局重新编号引用**，拼成一篇带出处的长报告；
    ④ 忠实性校验（faithfulness）用一次额外 LLM 调用，逐句核对报告是否真的被
       检索资料支持——这是本阶段相对阶段 14 真正新增的能力。

复用资产（刻意不重造）：
    agentic.run_agentic_blocking  —— 多轮自适应检索（route→grade→rewrite + 通道升级）
    search.SEARCH_SYSTEM / SEARCH_HINT —— 带 [n] 引用合成提示词
    search._parse_citations       —— 引用解析 + 越界校验
    rag.hybrid_search             —— 自适应检索落空时的兜底单轮检索

⚠️ 关于「JSON + 校验 + 一次自纠」：与阶段 11（team）、13（agentic）、14（search）
   同源。阶段 12 立过一条规矩「第三次才抽框架」——前两次手写是为了讲清原理。
   本阶段曾是第四处，当时判断「下沉会波及已发布阶段与其静态测试，收益不抵风险」
   而记了 TECH_DEBT 账；**阶段 18B 已结清**：实现下沉到 _jsonutil.py，
   各阶段的差异（hint 风格、自纠提示语）改为参数保留。

统一 SSE 事件协议（与阶段 10–14 一致，一帧一行）：
    start / plan / section_start / section_retrieve / section_answer_delta /
    answer_delta / section_finish / synthesize / faithfulness / finish / error
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

from pydantic import BaseModel, Field

from _jsonutil import json_call as _json_call
import rag
import agentic
import search
from llm import LLMClient

# 与阶段 10–14 一致的「一帧一行」事件协议。

_CITE_RE = re.compile(r"\[(\d+)\]")


# ---------- 结构化输出：规划 + 忠实性（阶段 04 的招式，已下沉到 _jsonutil） ----------

# ✅ 阶段 18B 结清：原先这里记着一笔技术债 ——
#   「阶段 11/13/14/15 各自持有一份 _json_call，按阶段 12 规矩应在第三次抽框架，
#     现第四次仍分散 —— 下沉到 llm.py 会波及已发布的 team/agentic 与各自静态测试，
#     收益不抵风险。」
#   当时的判断没错，但拦路的两个成本后来消失了：差异被显式参数化（hint/retry 风格），
#   静态测试也只走公开 API。故下沉为 _jsonutil.py，本阶段用标准版（retry_style="std"）。
#   记在这里是因为「第三次才抽框架」这条规矩本身值得记住：
#   **前两次手写是为了讲清原理，不是为了偷懒。**


# ---------- 规划：把问题拆成研究大纲 ----------

PLAN_SYSTEM = (
    "你是一个研究规划助手。给定用户的一个**研究性问题**，请把它拆成若干**子问题**，"
    "每个子问题都要能用资料库的检索来回答。\n"
    "要求：\n"
    "1. 子问题之间尽量**互不重复、覆盖全面**，合起来能完整回答原问题；\n"
    "2. 每个子问题要具体、可检索（不要写「背景介绍」这种空话）；\n"
    "3. 数量控制在要求范围内；按逻辑顺序排好。\n"
    "只输出 JSON，不要解释。"
)

PLAN_HINT = (
    '{"sections": [{"title": "小节标题", "question": "要查的具体子问题"}]}'
)


class PlanOutline(BaseModel):
    sections: list[PlanSection] = Field(default_factory=list)


class PlanSection(BaseModel):
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)


def _plan(
    client: LLMClient, question: str, model: str | None, max_sections: int, temperature: float
) -> list[dict]:
    """生成研究大纲。解析失败则降级为「整问题即唯一一节」。"""
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                f"研究问题：{question}\n\n"
                f"请拆成 {max(1, min(max_sections, 6))} 个子问题（不要多也不要少），"
                f"严格按下面的 JSON 输出：\n{PLAN_HINT}"
            ),
        },
    ]
    outline, raw = _json_call(client, messages, model, temperature, PlanOutline, PLAN_HINT)
    if outline is None or not outline.sections:
        return [{"title": "研究概述", "question": question}]
    sections = [
        {"title": s.title.strip(), "question": s.question.strip()}
        for s in outline.sections[:6]
        if s.title.strip() and s.question.strip()
    ]
    if not sections:
        return [{"title": "研究概述", "question": question}]
    return sections


# ---------- 逐节检索：复用阶段 13 的自适应检索 ----------

CHANNEL_LABEL = {
    "vector": "向量检索（阶段 07）",
    "hybrid": "混合检索 + 重排（阶段 08）",
    "graph": "知识图谱（阶段 09）",
    "none": "未检索",
}


def _section_retrieve(
    client: LLMClient,
    sub_q: str,
    *,
    model: str | None,
    top_k: int,
    rerank: bool,
    hops: int,
    max_rounds: int,
    temperature: float,
) -> dict:
    """跑一次自适应检索取命中片段；若智能体判定无需检索或落空，则兜底单轮混合检索。

    返回归一化 hits：[{title, index, text, score, rerankScore, channel}]，
    与阶段 13 的 _retrieve / 阶段 14 的 _search_retrieve 同一形状，下游无需分支。
    """
    started = time.perf_counter()
    res = agentic.run_agentic_blocking(
        client,
        sub_q,
        model=model,
        top_k=top_k,
        hops=hops,
        max_rounds=max_rounds,
        temperature=temperature,
    )
    hits: list[dict] = res.get("hits") or []

    if not hits:
        # 自适应检索落空（含「无需检索」判定），兜底单轮混合检索，确保研究有资料可引
        found = rag.hybrid_search(sub_q, top_k=top_k, mode="hybrid", rerank=rerank)
        hits = [
            {
                "title": h["title"],
                "index": int(h["index"]),
                "text": h["text"],
                "score": h.get("score"),
                "rerankScore": h.get("rerank_score"),
                "channel": "hybrid",
            }
            for h in found.get("hits", [])
        ]
        return {
            "hits": hits,
            "rounds": 0,
            "basedOn": "hybrid",
            "channel": "hybrid",
            "query": sub_q,
            "fallback": True,
            "ms": round((time.perf_counter() - started) * 1000, 2),
        }

    return {
        "hits": hits,
        "rounds": res.get("rounds", 0),
        "basedOn": res.get("basedOn", "hybrid"),
        "channel": res.get("basedOn", "hybrid"),
        "query": res.get("finalQuery", sub_q),
        "fallback": False,
        "ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _build_synth_messages(question: str, hits: list[dict]) -> list[dict]:
    """复用阶段 14 的引用合成提示词，把命中片段变成带 [n] 的小结。"""
    context = "\n\n".join(
        f"[{i + 1}] 《{h['title']}》第 {h['index']} 段\n{h['text']}" for i, h in enumerate(hits)
    )
    return [
        {"role": "system", "content": search.SEARCH_SYSTEM},
        {
            "role": "user",
            "content": (
                f"用户问题：{question}\n\n【资料】\n{context}\n\n{search.SEARCH_HINT}"
            ),
        },
    ]


# ---------- 忠实性校验：阶段 15 的签名能力 ----------

FAITH_SYSTEM = (
    "你是事实核查员。下面是一篇带引用编号 [n] 的研究报告，以及对应的【资料】片段。\n"
    "请逐句检查报告中的**事实性结论**是否真的能被【资料】中的对应片段支持：\n"
    "1. 如果某句结论在资料里找不到依据（凭模型记忆补的、或与资料原意不符、断章取义的），"
    "   记为一条 unsupported 指控，并原样摘录那句结论；\n"
    "2. 只检查事实性结论，不检查行文风格、是否口语化；\n"
    "3. 如果整篇报告都能被资料支持，unsupported 为空、faithful=true；\n"
    "4. score 是「被支持的结论数 / 全部事实性结论数」的估计（0.0~1.0）。\n"
    "只输出 JSON，不要解释。"
)

FAITH_HINT = (
    '{"faithful": true/false, "score": 0.0~1.0, '
    '"unsupported": ["不被资料支持的具体结论原文"]}'
)


class FaithVerdict(BaseModel):
    faithful: bool = False
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    unsupported: list[str] = Field(default_factory=list)


def _faithfulness(
    client: LLMClient, report: str, sources: list[dict], model: str | None, temperature: float
) -> dict:
    """逐句核对报告是否被检索资料支持，返回 {faithful, score, unsupported}。"""
    if not sources:
        return {"faithful": True, "score": 1.0, "unsupported": []}
    numbered = "\n\n".join(
        f"[{s['rank']}] 《{s['title']}》第 {s['index']} 段\n{s['snippet']}" for s in sources
    )
    messages = [
        {"role": "system", "content": FAITH_SYSTEM},
        {
            "role": "user",
            "content": (
                f"【资料】\n{numbered}\n\n"
                f"【研究报告】\n{report}\n\n"
                f"请严格按下面的 JSON 输出：\n{FAITH_HINT}"
            ),
        },
    ]
    verdict, _ = _json_call(client, messages, model, temperature, FaithVerdict, FAITH_HINT)
    if verdict is None:
        # 核查失败不阻塞：保守标为「无法确认」，unsupported 留空，由前端如实显示
        return {"faithful": False, "score": 0.0, "unsupported": []}
    return {
        "faithful": verdict.faithful,
        "score": round(verdict.score, 2),
        "unsupported": verdict.unsupported,
    }


# ---------- 主流程 ----------


def run_research(
    client: LLMClient,
    question: str,
    *,
    model: str | None = None,
    top_k: int = 4,
    rerank: bool = True,
    snippet: int = 240,
    max_sections: int = 4,
    hops: int = 2,
    max_rounds: int = 3,
    temperature: float = 0.3,
) -> Iterator[dict]:
    """跑一次深度研究，**逐事件 yield**。

    事件类型（与阶段 10–14 统一的"一帧一行"协议，并新增研究特有的几类）：

        start            问题、模型、小节数
        plan             研究大纲（各子问题）
        section_start    第 i 节开始：标题 + 子问题
        section_retrieve 该节自适应检索结果（轮次 / 通道 / 命中数）
        section_answer_delta 该节小结的流式增量（本地 [n]）
        answer_delta     合并后、全局重新编号的报告增量
        section_finish   该节小结收尾（本地引用 + grounded）
        synthesize       合并状态（小节数 / 全局来源数）
        faithfulness     忠实性核查结果（是否可信 / 分值 / 存疑结论）
        finish           完整报告 + 逐节小结 + 来源面板 + 忠实性
        error            失败原因
    """
    started = time.perf_counter()
    buckets = {"plan": 0.0, "retrieve": 0.0, "synthesize": 0.0, "faithfulness": 0.0}

    # ---------- ① 规划 ----------
    t0 = time.perf_counter()
    outline = _plan(client, question, model, max_sections, temperature)
    buckets["plan"] = round((time.perf_counter() - t0) * 1000, 2)

    yield {"type": "start", "question": question, "model": model or client.model, "maxSections": len(outline)}
    yield {"type": "plan", "sections": outline}

    global_sources: list[dict] = []
    seen_keys: dict[tuple, int] = {}
    all_cited: set[int] = set()
    sections_summary: list[dict] = []
    report_parts: list[str] = []

    for i, sec in enumerate(outline, start=1):
        title = sec["title"]
        sub_q = sec["question"]
        yield {
            "type": "section_start",
            "index": i,
            "title": title,
            "subQuestion": sub_q,
            "total": len(outline),
        }

        # ---------- ② 自适应检索 ----------
        t0 = time.perf_counter()
        retr = _section_retrieve(
            client,
            sub_q,
            model=model,
            top_k=top_k,
            rerank=rerank,
            hops=hops,
            max_rounds=max_rounds,
            temperature=temperature,
        )
        buckets["retrieve"] = round(buckets["retrieve"] + (time.perf_counter() - t0) * 1000, 2)
        hits = retr["hits"]
        yield {
            "type": "section_retrieve",
            "index": i,
            "subQuestion": sub_q,
            "rounds": retr["rounds"],
            "basedOn": retr["basedOn"],
            "channel": retr["channel"],
            "hitCount": len(hits),
            "query": retr["query"],
            "fallback": retr["fallback"],
            "ms": retr["ms"],
        }

        # ---------- ③ 引用合成（带本地 [n]） ----------
        if not hits:
            sub_answer = "（该子问题未检索到相关资料，无法基于资料作答。）"
            local_citations: list[int] = []
        else:
            sub_answer = ""
            t0 = time.perf_counter()
            for piece in client.stream(
                _build_synth_messages(sub_q, hits), model=model, temperature=temperature
            ):
                sub_answer += piece
                yield {"type": "section_answer_delta", "index": i, "text": piece}
            buckets["synthesize"] = round(
                buckets["synthesize"] + (time.perf_counter() - t0) * 1000, 2
            )
            local_citations = search._parse_citations(sub_answer, len(hits))

        # ---------- 合并：重编号到全局来源表（按 title+index+text 去重） ----------
        mapping: dict[int, int] = {}
        for local_i in range(1, len(hits) + 1):
            h = hits[local_i - 1]
            key = (h["title"], h["index"], h["text"])
            if key in seen_keys:
                mapping[local_i] = seen_keys[key]
            else:
                rank = len(global_sources) + 1
                global_sources.append({**h, "section": i, "rank": rank})
                seen_keys[key] = rank
                mapping[local_i] = rank

        def _renumber(m: re.Match, mp: dict[int, int] = mapping) -> str:
            n = int(m.group(1))
            return f"[{mp.get(n, n)}]"

        renumbered = _CITE_RE.sub(_renumber, sub_answer)
        for n in local_citations:
            if n in mapping:
                all_cited.add(mapping[n])

        grounded = bool(local_citations)
        sections_summary.append(
            {
                "index": i,
                "title": title,
                "subQuestion": sub_q,
                "rounds": retr["rounds"],
                "basedOn": retr["basedOn"],
                "hitCount": len(hits),
                "citations": sorted(mapping.get(c, c) for c in local_citations),
                "grounded": grounded,
            }
        )

        block = f"## {title}\n\n{renumbered}\n\n"
        report_parts.append(block)
        yield {"type": "answer_delta", "text": block}
        yield {
            "type": "section_finish",
            "index": i,
            "answer": sub_answer,
            "citations": local_citations,
            "grounded": grounded,
        }

    report = "".join(report_parts)

    sources = [
        {
            "rank": g["rank"],
            "index": g["index"],
            "title": g["title"],
            "snippet": g["text"][:snippet],
            "score": g.get("score"),
            "rerankScore": g.get("rerankScore"),
            "channel": g.get("channel"),
            "section": g.get("section"),
            "cited": g["rank"] in all_cited,
        }
        for g in global_sources
    ]

    yield {
        "type": "synthesize",
        "status": "ok" if global_sources else "empty",
        "sectionCount": len(outline),
        "sourceCount": len(global_sources),
    }

    # ---------- ④ 忠实性校验 ----------
    t0 = time.perf_counter()
    faith = _faithfulness(client, report, sources, model, temperature)
    buckets["faithfulness"] = round((time.perf_counter() - t0) * 1000, 2)
    yield {
        "type": "faithfulness",
        "score": faith["score"],
        "faithful": faith["faithful"],
        "unsupported": faith["unsupported"],
        "ms": buckets["faithfulness"],
    }

    yield {
        "type": "finish",
        "question": question,
        "answer": report,
        "model": model or client.model,
        "sections": sections_summary,
        "sources": sources,
        "citations": sorted(all_cited),
        "grounded": bool(all_cited),
        "coverage": round(len(all_cited) / len(global_sources), 2) if global_sources else 0.0,
        "faithful": faith["faithful"],
        "faithfulness": faith["score"],
        "unsupported": faith["unsupported"],
        "times": {**buckets},
        "totalMs": round((time.perf_counter() - started) * 1000, 2),
        "llmMs": round(
            buckets["plan"] + buckets["retrieve"] + buckets["synthesize"] + buckets["faithfulness"], 2
        ),
    }


def run_research_blocking(client: LLMClient, question: str, **kwargs: Any) -> dict:
    """把生成器跑完，收成一份完整结果（给非流式调用方用）。"""
    events = list(run_research(client, question, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)

    base: dict[str, Any] = {
        "question": question,
        "model": (finish or {}).get("model") or start.get("model") or kwargs.get("model") or client.model,
    }
    if finish is None:
        err = next((e for e in events if e["type"] == "error"), None)
        return {
            **base,
            "answer": None,
            "sections": [],
            "sources": [],
            "citations": [],
            "grounded": False,
            "coverage": 0.0,
            "faithful": False,
            "faithfulness": 0.0,
            "unsupported": [],
            "times": {},
            "totalMs": 0.0,
            "llmMs": 0.0,
            "error": (err or {}).get("message", "未知错误"),
        }
    return {
        **base,
        "answer": finish.get("answer"),
        "sections": finish.get("sections", []),
        "sources": finish.get("sources", []),
        "citations": finish.get("citations", []),
        "grounded": finish.get("grounded", False),
        "coverage": finish.get("coverage", 0.0),
        "faithful": finish.get("faithful", False),
        "faithfulness": finish.get("faithfulness", 0.0),
        "unsupported": finish.get("unsupported", []),
        "times": finish.get("times", {}),
        "totalMs": finish.get("totalMs", 0.0),
        "llmMs": finish.get("llmMs", 0.0),
        "error": None,
    }
