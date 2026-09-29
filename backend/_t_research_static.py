"""阶段 15 静态冒烟测试（不需要 Key，用脚本化桩 client + 桩检索跑通研究链路）。

验证六件事：
  1. 规划（plan）：桩 client 按 PLAN_SYSTEM 返回大纲，outline 条数 == max_sections。
  2. 引用合并 + 全局重编号：两节命中片段有重叠时，全局来源表去重；每节本地 [n]
     被重编号到全局编号，最终 citations 连续且无重复。
  3. 越界 / 非法引用（如 [9] 但只有 2 段）被安全丢弃，该节 grounded=False，不崩溃。
  4. 检索为空的节：合成被跳过、答案如实交代、该节 grounded=False，整流程仍正常收尾。
  5. 忠实性核查：桩 client 按 FAITH_SYSTEM 返回 verdict，finish 携带 faithful/score/unsupported。
  6. 耗时分项自洽：llmMs 与各分项之和一致，totalMs >= llmMs。

桩 client 按**系统提示词**分流（与阶段 11/13/14 的 ScriptedClient 同思路）：
本阶段涉及 PLAN_SYSTEM（规划）、search.SEARCH_SYSTEM（带 [n] 合成）、FAITH_SYSTEM（核查）。
检索则直接 patch `research._section_retrieve`，不依赖 ChromaDB / 真实嵌入。
"""

from __future__ import annotations

import json

import research
import search


class ScriptedClient:
    """按 system 提示词分流的确定性 client（用到 chat + stream）。"""

    model = "stub-model"

    def __init__(self, synth: str = "[1][2]") -> None:
        self.synth = synth
        self.calls: list[str] = []

    def _which(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        if system == research.PLAN_SYSTEM:
            return "plan"
        if system == search.SEARCH_SYSTEM:
            return "synthesize"
        if system == research.FAITH_SYSTEM:
            return "faith"
        raise AssertionError(f"未预期的 system 提示词：{system[:60]}")

    def chat(self, messages, model=None, temperature=0.3, response_format=None):
        kind = self._which(messages)
        self.calls.append(kind)
        if kind == "plan":
            return json.dumps(
                {"sections": [{"title": "小节一", "question": "子问题一"}, {"title": "小节二", "question": "子问题二"}]}
            )
        if kind == "faith":
            return json.dumps({"faithful": True, "score": 0.95, "unsupported": []})
        raise AssertionError(f"chat 不应收到 {kind}")

    def stream(self, messages, model=None, temperature=0.3):
        kind = self._which(messages)
        self.calls.append(kind)
        assert kind == "synthesize"
        for ch in self.synth:
            yield ch


def _hit(title: str, index: int, text: str) -> dict:
    return {
        "title": title,
        "index": index,
        "text": text,
        "score": 0.9,
        "rerankScore": 0.85,
        "channel": "hybrid",
    }


def _install_fake_retrieve(plan: str = "overlap") -> None:
    """把真实自适应检索换成确定性桩（验证的是「合并/重编号/核查」而非检索准不准）。

    overlap：两节共享第一段（测去重）；empty：第二节检索为空（测空检索路径）。
    """
    original = research._section_retrieve

    def fake(client, sub_q, *, model, top_k, rerank, hops, max_rounds, temperature) -> dict:
        if plan == "overlap":
            # 第一节：A, B；第二节：A（同）, C  → 全局应去重为 A/B/C 三条
            if "一" in sub_q or sub_q == "子问题一":
                hits = [_hit("DocA", 1, "关于 A 的资料"), _hit("DocB", 2, "关于 B 的资料")]
            else:
                hits = [_hit("DocA", 1, "关于 A 的资料"), _hit("DocC", 3, "关于 C 的资料")]
        elif plan == "empty":
            hits = [] if "二" in sub_q or sub_q == "子问题二" else [_hit("DocA", 1, "关于 A 的资料")]
        else:
            hits = [_hit("DocA", 1, "关于 A 的资料"), _hit("DocB", 2, "关于 B 的资料")]
        return {
            "hits": hits,
            "rounds": 2,
            "basedOn": "hybrid",
            "channel": "hybrid",
            "query": sub_q,
            "fallback": False,
            "ms": 12.3,
        }

    research._section_retrieve = fake  # type: ignore[assignment]
    globals()["_ORIGINAL_RETRIEVE"] = original


def _finish(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "finish")


def test_plan_outline_count():
    """① 规划产出大纲，条数 == max_sections。"""
    _install_fake_retrieve("overlap")
    client = ScriptedClient()
    events = list(research.run_research(client, "研究问题", max_sections=2))
    plan = next(e for e in events if e["type"] == "plan")
    assert len(plan["sections"]) == 2, plan["sections"]
    assert plan["sections"][0]["title"] == "小节一"
    print("✅ 1. 规划产出大纲，条数 == max_sections")


def test_global_renumber_and_dedup():
    """② 两节重叠片段全局去重为 3 条；本地 [n] 重编号后 citations == [1,2,3]。"""
    _install_fake_retrieve("overlap")
    client = ScriptedClient("[1][2]")
    events = list(research.run_research(client, "研究问题", max_sections=2))
    fin = _finish(events)

    # 第一节 [1][2]→全局 [1][2]；第二节 [1][2] 中本地1=A 已是全局1、本地2=C 是全局3 → [1][3]
    assert len(fin["sources"]) == 3, fin["sources"]
    assert fin["citations"] == [1, 2, 3], fin["citations"]
    assert fin["grounded"] is True
    assert fin["coverage"] == 1.0, fin["coverage"]
    # 全局来源里 A 应只出现一次（去重生效）
    titles = [s["title"] for s in fin["sources"]]
    assert titles.count("DocA") == 1, titles
    print("✅ 2. 重叠去重为 3 条全局来源；本地 [n] 重编号 → citations=[1,2,3]")


def test_out_of_range_citation_dropped():
    """③ 越界引用 [9]（只有 2 段）被丢弃，该节 grounded=False，不崩溃。"""
    _install_fake_retrieve("plain")
    client = ScriptedClient("[9]")
    events = list(research.run_research(client, "研究问题", max_sections=1))
    fin = _finish(events)
    # 单节、2 段、合成写 [9] → 解析后本地无合法引用 → 该节 grounded False
    assert fin["citations"] == [], fin["citations"]
    assert fin["grounded"] is False
    print("✅ 3. 越界/非法引用 [9] 被安全丢弃，该节 grounded=False")


def test_empty_retrieval_still_terminates():
    """④ 某节检索为空时合成被跳过、如实交代，整流程正常收尾。"""
    _install_fake_retrieve("empty")
    client = ScriptedClient("[1][2]")
    events = list(research.run_research(client, "研究问题", max_sections=2))
    fin = _finish(events)
    # 第二节为空 → 它贡献 0 来源；第一节 1 段 → 全局 1 条来源；报告里如实写出「未检索到」
    assert len(fin["sources"]) == 1, fin["sources"]
    assert "未检索到" in (fin["answer"] or ""), fin["answer"]
    assert fin["sections"][1]["hitCount"] == 0 and fin["sections"][1]["grounded"] is False
    print("✅ 4. 空检索节被安全跳过，整流程正常收尾")


def test_faithfulness_attached():
    """⑤ 忠实性核查结果进入 finish（faithful / faithfulness / unsupported）。"""
    _install_fake_retrieve("overlap")
    client = ScriptedClient("[1][2]")
    events = list(research.run_research(client, "研究问题", max_sections=2))
    faith = next(e for e in events if e["type"] == "faithfulness")
    fin = _finish(events)
    assert fin["faithful"] is True and fin["faithfulness"] == 0.95, (fin["faithful"], fin["faithfulness"])
    assert faith["unsupported"] == []
    print("✅ 5. 忠实性核查结果进入 finish（faithful=True, score=0.95）")


def test_times_self_consistent():
    """⑥ 耗时分项自洽：llmMs 与 plan+retrieve+synthesize+faithfulness 之和一致，totalMs>=llmMs。"""
    _install_fake_retrieve("overlap")
    client = ScriptedClient("[1][2]")
    events = list(research.run_research(client, "研究问题", max_sections=2))
    fin = _finish(events)
    t = fin["times"]
    expected = t["plan"] + t["retrieve"] + t["synthesize"] + t["faithfulness"]
    assert abs(expected - fin["llmMs"]) < 0.01, (t, fin["llmMs"])
    assert fin["totalMs"] >= fin["llmMs"], (fin["totalMs"], fin["llmMs"])
    print("✅ 6. 耗时分项自洽：llmMs==Σ分项，totalMs>=llmMs")


def test_blocking_response_shape():
    """顺带验证 blocking 包装结构可被 ResearchResponse 收下。"""
    _install_fake_retrieve("overlap")
    client = ScriptedClient("[1][2]")
    result = research.run_research_blocking(client, "研究问题", max_sections=2)
    assert result["grounded"] is True
    assert isinstance(result["sources"], list) and len(result["sources"]) == 3
    assert result["faithful"] is True
    assert result["error"] is None
    print("✅ 7. blocking 包装结构正确（sources/grounded/faithful/error 齐全）")


if __name__ == "__main__":
    test_plan_outline_count()
    test_global_renumber_and_dedup()
    test_out_of_range_citation_dropped()
    test_empty_retrieval_still_terminates()
    test_faithfulness_attached()
    test_times_self_consistent()
    test_blocking_response_shape()
    print("\n🎉 阶段 15 静态冒烟全部通过")
