"""阶段 14 静态冒烟测试（不需要 Key，用脚本化桩 client 跑通引用链路）。

验证五件事：
  1. 来源面板条数 == top_k，且 `cited` 标记与答案里的 [n] 完全一致。
  2. 答案里的引用编号被**正确解析**（去重、保序）。
  3. 越界 / 非法引用（如 [9] 但只有 3 段）被安全丢弃，不崩溃、不越界。
  4. 答案完全不引用时 `grounded=False`（如实标出"未挂出处"）。
  5. 耗时分项自洽：llmMs == synthesize，totalMs >= llmMs。

桩 client 按**系统提示词**分流（与阶段 11/13 的 ScriptedClient 同思路）：
本阶段只有一段合成 system，client 认出它就返回预设的带引用答案。
"""

from __future__ import annotations

import search


class ScriptedClient:
    """按 system 提示词分流的确定性 client（只用到 stream）。"""

    model = "stub-model"

    def __init__(self, answer: str = "结论一见[1]。结论三见[3]。") -> None:
        self.answer = answer
        self.calls: list[str] = []

    def _which(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        if system == search.SEARCH_SYSTEM:
            return "synthesize"
        raise AssertionError(f"未预期的 system 提示词：{system[:60]}")

    def stream(self, messages, model=None, temperature=0.3):
        kind = self._which(messages)
        self.calls.append(kind)
        assert kind == "synthesize"
        for ch in self.answer:
            yield ch


def _install_fake_retrieve(top_k: int = 3, empty: bool = False) -> None:
    """把真实检索换成确定性桩（验证的是**引用链路**，不是底层检索准不准）。

    与阶段 13 的 _install_fake_retrieve 同理：不替换的话测试会依赖本地 ChromaDB 状态，
    结论随机器漂移，那就不是"静态冒烟"了。
    """
    original = search._search_retrieve

    def fake(query: str, k: int, rerank: bool) -> dict:
        if empty:
            return {"hits": [], "detail": {"timings": {}}, "ms": 0.5}
        hits = [
            {
                "title": f"07-RAG基础#{i}",
                "index": i,
                "text": f"关于「{query}」的资料片段 {i}（完整内容……）",
                "score": 0.9 - i * 0.1,
                "rerankScore": 0.85 - i * 0.1,
                "channel": "hybrid",
            }
            for i in range(top_k)
        ]
        return {"hits": hits, "detail": {"timings": {}}, "ms": 12.3}

    search._search_retrieve = fake  # type: ignore[assignment]
    globals()["_ORIGINAL_SEARCH_RETRIEVE"] = original


def _finish(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "finish")


def test_sources_match_citations():
    """① + ② 来源条数 == top_k，cited 标记与 [n] 一致，且解析去重保序。"""
    _install_fake_retrieve(top_k=3)
    client = ScriptedClient("结论一见[1]。结论三见[3]。又回到[1]。")
    events = list(search.run_search(client, "检索到底要不要查？", top_k=3))
    fin = _finish(events)

    assert len(fin["sources"]) == 3, fin["sources"]
    assert fin["citations"] == [1, 3], fin["citations"]  # 去重、保序
    # cited 标记必须和 citations 对齐
    cited_ranks = [s["rank"] for s in fin["sources"] if s["cited"]]
    assert cited_ranks == [1, 3], cited_ranks
    assert fin["grounded"] is True
    assert fin["coverage"] == round(2 / 3, 2), fin["coverage"]
    print("✅ 1. 来源面板条数==topK，cited 与 [n] 一致，引用去重保序")


def test_out_of_range_citation_dropped():
    """③ 越界引用 [9]（只有 2 段）被丢弃，只留合法 [2]，不崩溃、不越界。"""
    _install_fake_retrieve(top_k=2)
    client = ScriptedClient("见[9]和[2]，越界的下标要丢掉。")
    events = list(search.run_search(client, "重排怎么做？", top_k=2))
    fin = _finish(events)

    assert fin["citations"] == [2], fin["citations"]
    assert all(1 <= c <= 2 for c in fin["citations"])
    assert fin["sources"][1]["cited"] is True
    assert fin["sources"][0]["cited"] is False
    print("✅ 2. 越界/非法引用 [9]/[0] 被安全丢弃，只留合法编号")


def test_no_citation_is_grounded_false():
    """④ 答案完全不引用时 grounded=False（如实标出"未挂出处"）。"""
    _install_fake_retrieve(top_k=3)
    client = ScriptedClient("资料里未提及此事的具体做法。")
    events = list(search.run_search(client, "某某做法是什么？", top_k=3))
    fin = _finish(events)

    assert fin["citations"] == [], fin["citations"]
    assert fin["grounded"] is False
    assert all(not s["cited"] for s in fin["sources"])
    print("✅ 3. 无引用时 grounded=False，来源面板全部未引用")


def test_empty_retrieval_still_terminates():
    """检索为空时也能收尾：sources 为空、grounded=False、答案如实交代。"""
    _install_fake_retrieve(top_k=3, empty=True)
    client = ScriptedClient("")
    events = list(search.run_search(client, "任何问题", top_k=3))
    fin = _finish(events)

    assert fin["sources"] == [], fin["sources"]
    assert fin["grounded"] is False
    assert "无法基于资料" in (fin["answer"] or "")
    print("✅ 4. 检索为空时正常收尾：来源为空、如实说明")


def test_times_self_consistent():
    """⑤ 耗时分项自洽：llmMs == synthesize，totalMs >= llmMs。"""
    _install_fake_retrieve(top_k=3)
    client = ScriptedClient("见[1]。")
    events = list(search.run_search(client, "q", top_k=3))
    fin = _finish(events)
    t = fin["times"]
    assert abs(t["synthesize"] - fin["llmMs"]) < 0.01, (t, fin["llmMs"])
    assert fin["totalMs"] >= fin["llmMs"], (fin["totalMs"], fin["llmMs"])
    print("✅ 5. 耗时分项自洽：llmMs==synthesize，totalMs>=llmMs")


def test_blocking_response_shape():
    """顺带验证 blocking 包装返回的结构可被 SearchResponse 收下（sources 为 dict 列表）。"""
    _install_fake_retrieve(top_k=3)
    client = ScriptedClient("见[1]和[2]。")
    result = search.run_search_blocking(client, "q", top_k=3)
    assert result["grounded"] is True
    assert result["retrieved"] == 3
    assert isinstance(result["sources"], list) and len(result["sources"]) == 3
    assert result["error"] is None
    print("✅ 6. blocking 包装返回结构正确（retrieved/sources/error 齐全）")


if __name__ == "__main__":
    test_sources_match_citations()
    test_out_of_range_citation_dropped()
    test_no_citation_is_grounded_false()
    test_empty_retrieval_still_terminates()
    test_times_self_consistent()
    test_blocking_response_shape()
    print("\n🎉 阶段 14 静态冒烟全部通过")
