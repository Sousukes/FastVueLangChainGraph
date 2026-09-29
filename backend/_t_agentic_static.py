"""阶段 13 静态冒烟测试（不需要 Key，用脚本化桩 client 跑通决策链路）。

验证四件事：
  1. 「不需要检索」的问题不会被拉去检索（这条最容易被写坏：多数实现默认全都查）。
  2. 「需要检索」且第一轮就够用时，**不会**发生第二轮——该省的省下来。
  3. 第一轮评级为「不足」时，确实会 rewrite + 换通道进入第二轮（反馈回路真的闭环）。
  4. 三轮都不够用时也能收尾：不抛异常、答案如实、时间为各项之和且不虚报。

桩 client 的设计要点：它按**系统提示词**分流（agentic 里四个决策点各有一段 system），
所以同一段业务代码无需改动就能被确定性驱动。这也是阶段 11 那套 ScriptedClient 的思路复用。
"""

from __future__ import annotations

import json

import agentic


class ScriptedClient:
    """按 system 提示词分流的确定性 client。

    - 路由器 / 评级员 / 改写员：各自返回预设 JSON
    - 生成：**这里改动偿债给 yield 一段文本**（模拟 client.stream）
    """

    model = "stub-model"

    def __init__(
        self,
        *,
        need_retrieval: bool = True,
        route_reason: str = "涉及课程里的具体做法",
        grade_useful_by_round: dict[int, bool] | None = None,
        keep: list[int] | None = None,
        missing: str = "缺少重排的具体做法",
        answer: str = "依据《07-RAG基础》第 3 段，答案是……",
    ) -> None:
        self.need_retrieval = need_retrieval
        self.route_reason = route_reason
        # 键是第几轮，值是这一轮评级是否"够了"
        self.grade_useful_by_round = grade_useful_by_round or {1: True}
        self.keep = keep if keep is not None else [0]
        self.missing = missing
        self.answer = answer
        self.calls: list[str] = []

    def _which(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        for key, marker in (
            ("route", agentic.ROUTE_SYSTEM),
            ("grade", agentic.GRADE_SYSTEM),
            ("rewrite", agentic.REWRITE_SYSTEM),
            ("answer", agentic.ANSWER_SYSTEM),
            ("direct", agentic.DIRECT_SYSTEM),
        ):
            if system == marker:
                return key
        raise AssertionError(f"未预期的 system 提示词：{system[:60]}")

    def chat(self, messages, model=None, temperature=0.2, response_format=None) -> str:
        kind = self._which(messages)
        self.calls.append(kind)
        if kind == "route":
            return json.dumps({"needRetrieval": self.need_retrieval, "reason": self.route_reason})
        if kind == "grade":
            round_no = self.calls.count("grade")
            useful = self.grade_useful_by_round.get(round_no, False)
            # 故意多塞一个越界下标，检验上层会不会过滤掉
            return json.dumps(
                {"useful": useful, "keep": self.keep + [99], "missing": self.missing}
            )
        if kind == "rewrite":
            return json.dumps({"query": "重排 Cross-encoder 的做法", "why": "换成资料里的术语"})
        raise AssertionError(f"{kind} 不应该走 chat（应走 stream）")

    def stream(self, messages, model=None, temperature=0.2):
        kind = self._which(messages)
        self.calls.append(kind)
        assert kind in ("answer", "direct"), kind
        for ch in self.answer:
            yield ch


def _install_fake_retrieve() -> None:
    """把真实检索换成确定性桩。

    ⭐ 为什么必须替掉：本测试要验的是**决策链路**要不要/该怎么走，不是底层检索准不准
    （那是阶段 07/08/09 各自的事）。不替换的话，测试就得依赖本地 ChromaDB 里有没有料、
    嵌入模型有没有下好——结论会随着机器状态漂移，那就不是"静态冒烟"了。
    """
    original = agentic._retrieve

    def fake(channel: dict, query: str, top_k: int, hops: int) -> dict:
        hits = [
            {
                "title": f"07-RAG基础#{i}",
                "index": i,
                "text": f"关于「{query}」的资料片段 {i}",
                "score": 0.9 - i * 0.1,
                "channel": channel["name"],
            }
            for i in range(3)
        ]
        return {"hits": hits, "detail": {"seeds": [], "timings": {}}, "ms": 1.23}

    agentic._retrieve = fake  # type: ignore[assignment]
    globals()["_ORIGINAL_RETRIEVE"] = original


def _run(client: ScriptedClient, **kwargs) -> list[dict]:
    _install_fake_retrieve()
    return list(agentic.run_agentic(client, "重排是哪个阶段引入的？", **kwargs))


def test_no_retrieval_skips_search():
    """① 不该查的就不查——检索被跳过，且没浪费任何 retrieve / grade / rewrite。"""
    client = ScriptedClient(need_retrieval=False, answer="直接作答即可。")
    events = _run(client)

    types = [e["type"] for e in events]
    assert "route" in types, types
    route = next(e for e in events if e["type"] == "route")
    assert route["needRetrieval"] is False, route

    # 关键不变量：没有任何检索相关事件
    for banned in ("retrieve", "grade", "rewrite"):
        assert banned not in types, f"不需要检索却出现了 {banned} 事件"

    finish = next(e for e in events if e["type"] == "finish")
    assert finish["rounds"] == 0, finish
    assert finish["hits"] == [], finish
    assert finish["basedOn"] == "none", finish
    assert finish["answer"] == "直接作答即可。"
    print("✅ 1. 无需检索时跳过整条检索链路（route → 直接生成）")


def test_first_round_enough_no_second_round():
    """② 第一轮够用就收手，不白跑第二轮。"""
    client = ScriptedClient(grade_useful_by_round={1: True}, keep=[0, 2])
    events = _run(client)

    retrieves = [e for e in events if e["type"] == "retrieve"]
    rewrites = [e for e in events if e["type"] == "rewrite"]
    assert len(retrieves) == 1, retrieves
    assert rewrites == [], "够用了还在改写，说明循环没正确 break"
    assert retrieves[0]["round"] == 1
    assert retrieves[0]["channel"] == "vector", retrieves[0]

    grade = next(e for e in events if e["type"] == "grade")
    assert grade["useful"] is True, grade
    # 越界的 99 必须被过滤掉
    assert grade["kept"] == [0, 2], grade

    finish = next(e for e in events if e["type"] == "finish")
    assert finish["rounds"] == 1, finish
    print("✅ 2. 第一轮足够时不再进第二轮，且评级下标越界被安全过滤")


def test_insufficient_triggers_rewrite_and_channel_upgrade():
    """③ 反馈回路闭环：不足 → 改写检索词 → 换通道再战。"""
    client = ScriptedClient(grade_useful_by_round={1: False, 2: True})
    events = _run(client)

    retrieves = [e for e in events if e["type"] == "retrieve"]
    assert len(retrieves) == 2, retrieves
    # ⭐ 通道必须升级：第二轮不能再是 vector
    assert retrieves[0]["channel"] == "vector", retrieves[0]
    assert retrieves[1]["channel"] == "hybrid", retrieves[1]

    rw = [e for e in events if e["type"] == "rewrite"]
    assert len(rw) == 1, rw
    assert rw[0]["round"] == 1
    assert rw[0]["from"] != rw[0]["to"], rw
    assert rw[0]["to"] == "重排 Cross-encoder 的做法", rw[0]

    # 第二轮用的必须是改写后的检索词
    assert retrieves[1]["query"] == rw[0]["to"], retrieves[1]

    finish = next(e for e in events if e["type"] == "finish")
    assert finish["rounds"] == 2, finish
    assert finish["basedOn"] == "hybrid", finish
    print("✅ 3. 评级不足 → 改写检索词 → 通道由 vector 升级到 hybrid 后命中")


def test_all_rounds_fail_still_terminates():
    """④ 三轮都不够也要能收尾：不抛异常、如实交代、耗时各项自洽。"""
    client = ScriptedClient(grade_useful_by_round={1: False, 2: False, 3: False})
    events = _run(client, max_rounds=3)

    retrieves = [e for e in events if e["type"] == "retrieve"]
    assert len(retrieves) == 3, retrieves
    channels = [r["channel"] for r in retrieves]
    assert channels == ["vector", "hybrid", "graph"], channels

    # 最后一轮之后不再改写（没有第 4 轮了）
    assert len([e for e in events if e["type"] == "rewrite"]) == 2

    finish = next(e for e in events if e["type"] == "finish")  # 不抛异常就是合格
    t = finish["times"]
    # 分项之和应约等于 LLM 用时（retrieve 是本地检索，不计入 llmMs）
    llmish = t["route"] + t["grade"] + t["rewrite"] + t["answer"]
    assert abs(llmish - finish["llmMs"]) < 0.01, (t, finish["llmMs"])
    assert finish["totalMs"] >= finish["llmMs"], (finish["totalMs"], finish["llmMs"])
    print("✅ 4. 三轮均不足时正常收尾：通道用尽、耗时分项自洽")


def test_catalog_matches_channels():
    cat = agentic.channel_catalog()
    assert [c["name"] for c in cat["channels"]] == [c["name"] for c in agentic.CHANNELS]
    assert [c["round"] for c in cat["channels"]] == [1, 2, 3]
    assert all(c["why"] for c in cat["channels"]), "每个通道都要说清为什么会在这一轮"
    print("✅ 5. 通道清单与 CHANNELS 一致，且每轮都给了升级理由")


if __name__ == "__main__":
    test_no_retrieval_skips_search()
    test_first_round_enough_no_second_round()
    test_insufficient_triggers_rewrite_and_channel_upgrade()
    test_all_rounds_fail_still_terminates()
    test_catalog_matches_channels()
    print("\n🎉 阶段 13 静态冒烟全部通过")
