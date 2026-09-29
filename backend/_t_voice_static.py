"""阶段 17 静态冒烟测试（不需要 Key；确定性体检 + 脚本化桩 client）。

验证十四件事：
  1. 输入守卫：空 / 全空白 / 超长 → VoiceInputError（供路由层转 400）。
  2. Markdown 计数：结构化标记逐个数对；纯中文口语稿 → 0（这是可断言的核心约束）。
  3. 朗读体检：中文字符数 + 拉丁词数 + 时长估算公式自洽。
  4. 口语化改写：流式增量拼成完整 answer；rewrite 帧的字数/Markdown 数与文本一致。
  4b. 改写稿里混进 Markdown 时，markdownLeft 如实计数（不粉饰）。
  5. 朗读稿规范化：speak 帧带朗读稿 + terms 对照 + 体检数字 + termCheck 核对结果。
  6. 规范化保守降级：输出不是合法 JSON（自纠也失败）→ speak 退回口语稿原文，terms 空。
  7. 规范化抛错：chat 通道不可用 → error 帧 + speak 退回口语稿，流程仍走到 finish。
  8. 改写抛错：stream 通道不可用 → error 帧 + finish 仍有（answer 为空）+ 不发 speak 帧。
  9. terms 过滤：from 为空白的条目被丢掉。
  9b. ⭐ termCheck（audit_terms）：一致通过 / 凭空多报 / 声称改了却没改，三种都判对。
  10. 耗时自洽：llmMs == rewrite + normalize，totalMs >= llmMs。
  11. blocking 正常路径：error=None，字段齐全，可被 VoiceResponse 收下。
  12. blocking 失败路径：error 有值，**同时**保留部分结果（这是本阶段刻意设计的语义）。

桩 client 按**系统提示词**分流（与阶段 11/13/14/15/16 的 ScriptedClient 同思路）：
本阶段两条 system —— voice.VOICE_SYSTEM（口语化改写，走 stream）
与 voice.TN_SYSTEM（文本规范化，走 chat + JSON）。
"""

from __future__ import annotations

import json

import voice


# ---------- 桩 client ----------


class ScriptedClient:
    """按 system 提示词分流的确定性 client（用到 stream + chat）。"""

    model = "stub-voice-model"

    def __init__(
        self,
        rewrite_text: str = "先给结论。再补一句理由。",
        tn_raw: str | None = None,
        *,
        raise_stream: bool = False,
        raise_chat: bool = False,
    ) -> None:
        self.rewrite_text = rewrite_text
        self.tn_raw = tn_raw if tn_raw is not None else TN_OK
        self.raise_stream = raise_stream
        self.raise_chat = raise_chat
        self.calls: list[str] = []

    def _which(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        if system == voice.VOICE_SYSTEM:
            return "rewrite"
        if system == voice.TN_SYSTEM:
            return "normalize"
        raise AssertionError(f"未预期的 system 提示词：{system[:60]}")

    def stream(self, messages, model=None, temperature=0.3):
        kind = self._which(messages)
        self.calls.append(f"stream:{kind}")
        if self.raise_stream:
            raise RuntimeError("上游流式通道不可用")
        for ch in self.rewrite_text:
            yield ch

    def chat(self, messages, model=None, temperature=0.3, response_format=None):
        kind = self._which(messages)
        self.calls.append(f"chat:{kind}")
        if self.raise_chat:
            raise RuntimeError("上游 chat 通道不可用")
        return self.tn_raw


TN_OK = json.dumps(
    {
        "speak": "单号是 B X 二零二六 零九一七，金额两千一百五十八元五角。",
        "terms": [
            {"from": "BX-2026-0917", "to": "B X 二零二六 零九一七"},
            {"from": "¥2,158.50", "to": "两千一百五十八元五角"},
        ],
    },
    ensure_ascii=False,
)


def _finish(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "finish")


def _errors(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "error"]


ASK = "帮我讲一下报销流程"


# ---------- 测试 ----------


def test_transcript_guard():
    """① 空 / 全空白 / 超长 → VoiceInputError。"""
    for label, bad in {"空串": "", "全空白": "   \n  ", "超长": "啊" * 2001}.items():
        try:
            voice._check_transcript(bad)
        except voice.VoiceInputError:
            continue
        raise AssertionError(f"{label} 未被拒绝")
    # 边界：正好 2000 字应通过
    assert len(voice._check_transcript("啊" * 2000)) == 2000
    print("✅ 1. 输入守卫：空/空白/超长被拒，2000 字边界通过")


def test_markdown_left():
    """② Markdown 计数 —— 把「口语稿必须无 Markdown」变成可断言的数字。"""
    assert voice.markdown_left("") == 0
    assert voice.markdown_left("先给结论。再补一句理由。") == 0
    assert voice.markdown_left("第一，先做这个。第二，再做那个。") == 0
    assert voice.markdown_left("**加粗**") == 2  # 一对 **
    assert voice.markdown_left("`code`") == 2  # 一对反引号
    assert voice.markdown_left("## 标题") == 1  # 行首标题
    assert voice.markdown_left("- 一项\n- 又一项") == 2  # 两行列表
    assert voice.markdown_left("1. 第一\n2. 第二") == 2  # 有序列表
    assert voice.markdown_left("## 结论\n- 第一点") == 2  # 行首 # + 行首 -
    assert voice.markdown_left("[链接](https://a.b)") == 1
    assert voice.markdown_left("| a | b |") == 3  # 表格竖线
    print("✅ 2. Markdown 计数：结构化标记数对，纯中文口语稿为 0")


def test_spoken_profile():
    """③ 朗读体检：中文字符数 + 拉丁词数 + 时长公式。"""
    p = voice.spoken_profile("二零二六年")
    assert p["cjk"] == 5 and p["words"] == 0, p  # 二/零/二/六/年 —— 5 个字
    assert p["plainChars"] == 5, p
    assert p["estSeconds"] == round(5 / 4.5, 1), p

    p2 = voice.spoken_profile("AI 系统")
    assert (p2["cjk"], p2["words"]) == (2, 1), p2
    assert p2["plainChars"] == 3, p2
    assert p2["estSeconds"] == round(2 / 4.5 + 1 / 2.5, 1), p2

    # 连字符型号只算一个「词」
    p3 = voice.spoken_profile("BX-2026-0917")
    assert p3["words"] == 1, p3

    assert voice.spoken_profile("")["estSeconds"] == 0.0
    print("✅ 3. 朗读体检：字数/词数/时长公式自洽，空文本为 0")


def test_rewrite_streams_and_counts():
    """④ 流式改写：增量拼成完整 answer，rewrite 帧数字与文本一致。"""
    client = ScriptedClient(rewrite_text="先给结论。再补一句理由。")
    events = list(voice.run_voice(client, ASK))
    fin = _finish(events)

    deltas = [e["text"] for e in events if e["type"] == "answer_delta"]
    assert "".join(deltas) == "先给结论。再补一句理由。", deltas
    rw = next(e for e in events if e["type"] == "rewrite")
    assert rw["chars"] == len("先给结论。再补一句理由。"), rw
    assert rw["markdownLeft"] == 0, rw
    assert fin["answer"] == "先给结论。再补一句理由。"
    # start 帧带转写原文与风格
    start = next(e for e in events if e["type"] == "start")
    assert start["transcript"] == ASK and start["style"] == "brief"
    print("✅ 4. 流式改写：delta 拼接 == 完整 answer，字数/Markdown 数与文本一致")


def test_rewrite_markdown_is_counted():
    """④b 改写稿里若混进 Markdown，rewrite 帧必须如实报出来（不粉饰）。"""
    client = ScriptedClient(rewrite_text="## 结论\n- 第一点")
    events = list(voice.run_voice(client, ASK))
    rw = next(e for e in events if e["type"] == "rewrite")
    assert rw["markdownLeft"] == 2, rw
    print("✅ 4b. 改写稿含 Markdown 时如实计数（markdownLeft=2）")


def test_speak_frame_and_terms():
    """⑤ 规范化：speak 帧带朗读稿 + terms 对照 + 体检数字 + termCheck 核对结果。"""
    # 口语稿里真的含这两个原文，对照表才可能"核对通过"
    client = ScriptedClient(rewrite_text="单号是 BX-2026-0917，金额 ¥2,158.50。")
    events = list(voice.run_voice(client, ASK))
    fin = _finish(events)

    sp = next(e for e in events if e["type"] == "speak")
    assert sp["speak"] == json.loads(TN_OK)["speak"], sp
    assert len(sp["terms"]) == 2, sp
    assert sp["terms"][0] == {"from": "BX-2026-0917", "to": "B X 二零二六 零九一七"}, sp["terms"]
    assert sp["plainChars"] > 0 and sp["estSeconds"] > 0, sp
    # ⭐ termCheck：两条自述都真的发生在文本里
    assert sp["termCheck"] == {"total": 2, "verified": 2, "suspect": []}, sp["termCheck"]
    # finish 与 speak 帧一致
    assert fin["speak"] == sp["speak"]
    assert fin["terms"] == sp["terms"]
    assert fin["termCheck"] == sp["termCheck"]
    # 规范化确实被调用过（走 chat，不是 stream）
    assert "chat:normalize" in client.calls, client.calls
    print("✅ 5. 规范化：speak 帧带朗读稿 + 2 条对照 + termCheck 全部核对通过")


def test_normalize_conservative_fallback():
    """⑥ 输出不是合法 JSON（自纠也失败）→ speak 退回口语稿原文，terms 空。"""
    client = ScriptedClient(tn_raw="好的，这是结果：{不是合法 JSON}")
    events = list(voice.run_voice(client, ASK))
    fin = _finish(events)
    assert fin["speak"] == fin["answer"] == "先给结论。再补一句理由。", fin
    assert fin["terms"] == []
    # 自纠重试过（chat 至少两次）
    assert client.calls.count("chat:normalize") >= 2, client.calls
    print("✅ 6. 规范化保守降级：坏 JSON → speak 退回口语稿原文，内容不丢")


def test_normalize_raises_degrades():
    """⑦ chat 通道不可用 → error 帧 + speak 退回口语稿，流程仍走到 finish。"""
    client = ScriptedClient(raise_chat=True)
    events = list(voice.run_voice(client, ASK))
    fin = _finish(events)
    errs = _errors(events)
    assert len(errs) == 1 and "文本规范化失败" in errs[0]["message"], errs
    assert fin["speak"] == fin["answer"], fin  # 保守降级：直接用口语稿念
    assert fin["terms"] == []
    print("✅ 7. 规范化抛错：error 帧已发，speak 退回口语稿，finish 照常收尾")


def test_rewrite_raises_degrades():
    """⑧ stream 通道不可用 → error 帧 + finish 仍有 + 不发 speak 帧（没东西可规范化）。"""
    client = ScriptedClient(raise_stream=True)
    events = list(voice.run_voice(client, ASK))
    fin = _finish(events)
    errs = _errors(events)
    assert len(errs) == 1 and "口语化改写失败" in errs[0]["message"], errs
    assert fin["answer"] == "" and fin["speak"] == "", fin
    assert not any(e["type"] == "speak" for e in events), events
    assert not any(e["type"] == "answer_delta" for e in events), events
    print("✅ 8. 改写抛错：error 帧已发，finish 仍在（answer 空），不发 speak 帧")


def test_terms_blank_filtered():
    """⑨ from 为空白的对照条目被丢掉。"""
    raw = json.dumps(
        {
            "speak": "念这一句。",
            "terms": [
                {"from": "   ", "to": "噪声"},
                {"from": "USB-C", "to": "U S B C"},
            ],
        },
        ensure_ascii=False,
    )
    client = ScriptedClient(tn_raw=raw)
    events = list(voice.run_voice(client, ASK))
    sp = next(e for e in events if e["type"] == "speak")
    assert sp["terms"] == [{"from": "USB-C", "to": "U S B C"}], sp["terms"]
    print("✅ 9. terms 过滤：from 为空白的条目被丢掉")


def test_audit_terms_is_deterministic_check():
    """⑨b ⭐ 对 `terms` 的确定性核对：自述了却对不上文本的条目要被挑出来。

    这是「模型自述 ≠ 可核对」的落点：阶段 14 grounded → 15 faithful → 17 termCheck。
    """
    ans = "单号 BX-2026-0917，金额两千一百五十八块五。"
    spk = "单号 B X 二零二六 零九一七，金额两千一百五十八块五。"

    good = [{"from": "BX-2026-0917", "to": "B X 二零二六 零九一七"}]
    r = voice.audit_terms(ans, spk, good)
    assert r == {"total": 1, "verified": 1, "suspect": []}, r

    # 第二条：from 不在口语稿里（凭空多报）→ suspect
    r2 = voice.audit_terms(ans, spk, good + [{"from": "不存在的原文", "to": "两千一百五十八块五"}])
    assert r2["verified"] == 1 and len(r2["suspect"]) == 1, r2
    assert r2["suspect"][0]["from"] == "不存在的原文"

    # 第三条：from 在、to 不在朗读稿里（声称改了其实没改）→ suspect
    r3 = voice.audit_terms(ans, spk, [{"from": "BX-2026-0917", "to": "根本没这么念"}])
    assert r3["verified"] == 0 and len(r3["suspect"]) == 1, r3

    assert voice.audit_terms(ans, spk, []) == {"total": 0, "verified": 0, "suspect": []}
    print("✅ 9b. audit_terms：一致通过 / 凭空多报 / 声称改了却没改 —— 三种情况都判对")


def test_times_self_consistent():
    """⑩ 耗时自洽：llmMs == rewrite + normalize，totalMs >= llmMs。"""
    client = ScriptedClient()
    events = list(voice.run_voice(client, ASK))
    fin = _finish(events)
    t = fin["times"]
    assert abs(fin["llmMs"] - (t["rewrite"] + t["normalize"])) < 0.01, (fin["llmMs"], t)
    assert fin["totalMs"] >= fin["llmMs"], (fin["totalMs"], fin["llmMs"])
    assert t["rewrite"] >= 0.0 and t["normalize"] >= 0.0, t
    print("✅ 10. 耗时自洽：llmMs == rewrite + normalize，totalMs >= llmMs")


def test_blocking_ok_shape():
    """⑪ blocking 正常路径：error=None，字段齐全。"""
    client = ScriptedClient()
    result = voice.run_voice_blocking(client, ASK, style="explain", max_chars=120)
    assert result["error"] is None, result
    assert result["transcript"] == ASK
    assert result["style"] == "explain"
    assert result["model"] == "stub-voice-model"
    assert result["speak"] and result["terms"], result
    assert result["termCheck"] is not None, result
    assert result["estSeconds"] > 0
    print("✅ 11. blocking 正常路径：error=None，transcript/style/model/体检数字齐全")


def test_blocking_soft_error_keeps_partial():
    """⑫ blocking 失败路径：error 有值，**同时**保留部分结果。"""
    client = ScriptedClient(raise_chat=True)
    result = voice.run_voice_blocking(client, ASK)
    assert result["error"] and "文本规范化失败" in result["error"], result
    assert result["answer"] == "先给结论。再补一句理由。", result  # 部分结果仍在
    assert result["speak"] == result["answer"], result  # 降级到口语稿
    print("✅ 12. blocking 失败路径：error 有值且部分结果保留（answer/speak 都可用）")


if __name__ == "__main__":
    test_transcript_guard()
    test_markdown_left()
    test_spoken_profile()
    test_rewrite_streams_and_counts()
    test_rewrite_markdown_is_counted()
    test_speak_frame_and_terms()
    test_normalize_conservative_fallback()
    test_normalize_raises_degrades()
    test_rewrite_raises_degrades()
    test_terms_blank_filtered()
    test_audit_terms_is_deterministic_check()
    test_times_self_consistent()
    test_blocking_ok_shape()
    test_blocking_soft_error_keeps_partial()
    print("\n🎉 阶段 17 静态冒烟全部通过")
