"""阶段 14 · AI 搜索应用（把检索结果变成可被验证的答案）。

⭐ 本阶段要解决什么

阶段 07–09 教会了"怎么把资料找回来"，阶段 13 教会了"要不要查、查够了没、
不够怎么换着查"。但有一个产品问题一直没解决：**找回来之后，怎么让用户相信答案？**

单纯把 top-k 拼进 prompt 让模型生成，用户拿到的只是一段看起来很对的文字，
既看不到依据，也不知道哪句是编的。这正是「AI 搜索」（Perplexity 那种）要解决的：
**答案每一句都挂上出处，用户点一下就能跳到原文核对。**

本阶段做三件事：

  ① 一次有力的检索（复用阶段 08 的混合检索 + 重排，作为搜索产品的"快而准"基线）
  ② 带引用的合成：要求模型**只用检索到的资料、且每句都标 [n] 出处**
  ③ 引用解析 + 校验：把模型吐出的 [1][2] 解析出来，映射回来源片段，
     并校验下标合法——这一步才是"可被验证"的真正开关

⭐ 和阶段 13 的关系（刻意不复用）

阶段 13 是"检索引擎"（决定怎么查），本阶段是"产品外壳"（决定查回来怎么呈现）。
两者可以组合：把阶段 13 的 `run_agentic_blocking` 当成检索器替换掉本文件的
`_search_retrieve` 即可获得"自适应检索 + 带引用答案"。但本阶段自选**单轮混合检索**，
理由有二：① 搜索产品要的是"快而准"的即时响应，多轮决策回路反而是负担；
② 让本阶段的 diff 干净——`git diff stage-13 stage-14` 就是"多了一个 search.py"，
而不是在 agentic 上打补丁。§7 练习里会要求完成这个组合。

⭐ 关于"引用 ≠ 可信"

本阶段能验证的只是"模型**声称**引用了哪几段"。它不保证模型真的只用了那些段、
也没检查某段是否被断章取义——那是"忠实性（faithfulness）"问题，留给阶段 15 Deep Research。
所以前端会把 `grounded` 状态如实标出来，而不是假装"已验证"。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterator

import rag
from llm import LLMClient

# 与阶段 13 一致的"一帧一行"事件协议（start / retrieve / answer_delta / finish / error）。


# ---------- 提示词：带引用的合成 ----------

SEARCH_SYSTEM = (
    "你是一个 AI 搜索回答助手。请**只依据**下面提供的【资料】回答用户的问题。\n"
    "规则：\n"
    "1. 每个事实性结论后，用 [n] 标注它引用的资料编号（n 是资料前的数字编号）；\n"
    "2. 如果多条资料都支持同一句，可以写 [1][2]；编号必须是资料里真实存在的；\n"
    "3. 资料里没有的内容，明确说「资料里未提及」，不要凭记忆补充，也不要编造；\n"
    "4. 回答要直接、结构化（用小标题或要点），不要复述问题。\n"
    "宁可承认不知道，也不要给一个看起来很像样的答案。"
)

SEARCH_HINT = "每段结论后用 [n] 标注出处编号；资料未提及的就说未提及。"

_CITE_RE = re.compile(r"\[(\d+)\]")


def _parse_citations(answer: str, max_n: int) -> list[int]:
    """从答案文本里提取被引用的编号，按出现顺序去重，并丢弃越界/非法下标。

    越界（如模型写了 [9] 但只有 3 段）必须丢弃：否则前端按编号定位来源会越界崩溃。
    这与阶段 13 过滤评级越界下标的思路一致——**模型给的编号不可全信**。
    """
    seen: list[int] = []
    for m in _CITE_RE.finditer(answer):
        n = int(m.group(1))
        if 1 <= n <= max_n and n not in seen:
            seen.append(n)
    return seen


# ---------- 检索：单轮混合 + 重排，复用阶段 08 ----------


def _search_retrieve(query: str, top_k: int, rerank: bool) -> dict:
    """跑一次混合检索并统一归一成 `[{title, index, text, score, rerankScore, channel}]`。

    归一与阶段 13 的 `_retrieve` 同理：检索层返回的字段各异，在这里一次性收口，
    下游（引用解析、来源面板）就不必为每个来源写分支。
    """
    started = time.perf_counter()
    found = rag.hybrid_search(query, top_k=top_k, mode="hybrid", rerank=rerank)
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
        "detail": {"timings": found.get("timings", {})},
        "ms": round((time.perf_counter() - started) * 1000, 2),
    }


# ---------- 主流程 ----------


def run_search(
    client: LLMClient,
    question: str,
    *,
    model: str | None = None,
    top_k: int = 6,
    rerank: bool = True,
    snippet: int = 220,
    temperature: float = 0.3,
) -> Iterator[dict]:
    """跑一次 AI 搜索，**逐事件 yield**。

    事件类型（与阶段 10/11/12/13 统一的"一帧一行"协议）：

        start        问题、模型、topK
        retrieve     检索到的片段 + 数量 + 耗时            ← ① 一次有力检索
        synthesize   合成状态（有/无资料）
        answer_delta 带 [n] 引用的答案增量文本           ← ② 带引用的合成
        finish       答案 + 来源列表 + 引用编号 + grounded ← ③ 引用解析 + 校验
        error        失败原因

    ⭐ 写成生成器同样是与阶段 09/13 那条经验呼应：**能自己拆开的流程，就别用线程去绕。**
    """
    started = time.perf_counter()
    buckets = {"retrieve": 0.0, "synthesize": 0.0}

    yield {
        "type": "start",
        "question": question,
        "model": model or client.model,
        "topK": top_k,
    }

    # ---------- ① 一次有力的检索 ----------
    t0 = time.perf_counter()
    found = _search_retrieve(question, top_k, rerank)
    buckets["retrieve"] = round((time.perf_counter() - t0) * 1000, 2)
    hits = found["hits"]
    yield {
        "type": "retrieve",
        "query": question,
        "hits": hits,
        "count": len(hits),
        "ms": found["ms"],
    }

    # ---------- ② 带引用的合成 ----------
    if not hits:
        answer = "（本次检索没有返回任何资料，无法基于资料作答。）"
        sources: list[dict] = []
        citations: list[int] = []
        grounded = False
        yield {"type": "synthesize", "status": "empty"}
    else:
        context = "\n\n".join(
            f"[{i + 1}] 《{h['title']}》第 {h['index']} 段\n{h['text']}" for i, h in enumerate(hits)
        )
        messages = [
            {"role": "system", "content": SEARCH_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n【资料】\n{context}\n\n{SEARCH_HINT}"
                ),
            },
        ]
        answer = ""
        t0 = time.perf_counter()
        for piece in client.stream(messages, model=model, temperature=temperature):
            answer += piece
            yield {"type": "answer_delta", "text": piece}
        buckets["synthesize"] = round((time.perf_counter() - t0) * 1000, 2)

        # ---------- ③ 引用解析 + 校验 ----------
        citations = _parse_citations(answer, len(hits))
        cited_set = set(citations)
        sources = [
            {
                "rank": i + 1,
                "index": h["index"],
                "title": h["title"],
                "snippet": h["text"][:snippet],
                "score": h.get("score"),
                "rerankScore": h.get("rerankScore"),
                "cited": (i + 1) in cited_set,
            }
            for i, h in enumerate(hits)
        ]
        grounded = bool(citations)

    coverage = round(len(citations) / len(hits), 2) if hits else 0.0
    yield {
        "type": "finish",
        "answer": answer,
        "sources": sources,
        "citations": citations,
        "grounded": grounded,
        "coverage": coverage,
        "times": {**buckets},
        "totalMs": round((time.perf_counter() - started) * 1000, 2),
        "llmMs": buckets["synthesize"],
    }


def run_search_blocking(client: LLMClient, question: str, **kwargs: Any) -> dict:
    """把生成器跑完，收成一份完整结果（给非流式调用方用）。"""
    events = list(run_search(client, question, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    retrieve = next((e for e in events if e["type"] == "retrieve"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)

    base: dict[str, Any] = {
        "question": question,
        "model": (finish or {}).get("model") or start.get("model") or kwargs.get("model") or client.model,
        "retrieved": retrieve.get("count", 0),
    }
    if finish is None:
        err = next((e for e in events if e["type"] == "error"), None)
        return {
            **base,
            "answer": None,
            "sources": [],
            "citations": [],
            "grounded": False,
            "coverage": 0.0,
            "times": {},
            "totalMs": 0.0,
            "llmMs": 0.0,
            "error": (err or {}).get("message", "未知错误"),
        }
    return {
        **base,
        "answer": finish["answer"],
        "sources": finish["sources"],
        "citations": finish["citations"],
        "grounded": finish["grounded"],
        "coverage": finish["coverage"],
        "times": finish["times"],
        "totalMs": finish["totalMs"],
        "llmMs": finish["llmMs"],
        "error": None,
    }
