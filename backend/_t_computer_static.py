"""阶段 18 静态冒烟测试（不需要 Key；确定性几何 + 脚本化桩 client）。

验证十四件事：
  1. 纯几何：element_at 命中判定、rect_distance / nearest_distance 偏差测量。
  2. 渲染确定性：同一状态渲染出**逐字节相同**的 PNG；状态一变图就变。
  3. 正常路径：点三个框 → 输入 → 提交 → success=True、fieldScore=3、命中率 100%。
  4. ⭐ 协议：`role="tool"` 只能装文本，新截图必须另起一条 `role="user"` 多模态消息；
     且每个 tool_call 都要有配对的 tool 消息（否则真实 API 直接报错）。
  5. ⭐ 点空 → 键盘被丢弃：模型想打字但没点中输入框 → lostKeystrokes 计数。
  6. 坐标越界被**钳制**而不是报错（报错会让模型卡死）。
  7. 危险动作默认被拒（沙箱），显式授权后才放行。
  8. 非法 action / 未知工具名 / args 是坏 JSON —— 都不许把流程炸掉。
  9. 非 ASCII 输入被键盘丢弃，但**不算** lostKeystrokes（那是"点空"的判据，别混）。
 10. 未填完就提交 → 被拒；填完按 Enter 也能提交。
 11. keep-alive 型动作（screenshot / wait）不改变状态。
 12. 模型中途报错 → 仍发出 finish（带部分结果），且 ComputerResponse 能收下这份结果。
 13. 耗时分项自洽：llmMs == times["llm"]，totalMs >= llmMs；blocking 形状完整。
14. ⭐ 定位精度：命中时的中心偏差（centerOffsetPx / avgCenterOffsetPx）—— 全中时也有值，
    因此只有它能横向比较；命中率高 ≠ 点得准。

桩 client 只需实现 `stream_message`（本阶段引擎只调它），按脚本逐轮吐 tool_calls。
"""

from __future__ import annotations

import io
import json

import computer as c
import schemas

ASK = None  # 本阶段没有"用户问题"参数，用内置任务模板


# ---------------------------------------------------------------------------
# 桩 client
# ---------------------------------------------------------------------------


def _chunks(text: str, n: int = 3):
    if not text:
        return
    size = max(1, len(text) // n)
    for i in range(0, len(text), size):
        yield text[i : i + size]


class ScriptedClient:
    """按脚本逐轮返回 message（含 tool_calls）。

    脚本每一项：{"content": "这轮说的话", "calls": [("computer", {"action": ...}), ...]}
    `calls` 里第二项可以直接给 dict，也可以给字符串（用来模拟坏 JSON）。
    """

    model = "deepseek-flash"

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.i = 0
        self.snapshots: list[list[dict]] = []
        self.raise_on_round: int | None = None

    def stream_message(self, messages, model=None, temperature=0.7, tools=None, tool_choice=None):
        self.i += 1
        if self.raise_on_round is not None and self.i >= self.raise_on_round:
            raise RuntimeError("模拟上游 500")
        self.snapshots.append(list(messages))

        if self.i > len(self.script):
            yield {"message": {"role": "assistant", "content": "（脚本用尽）", "tool_calls": []}}
            return

        item = self.script[self.i - 1]
        text = item.get("content", "")
        for piece in _chunks(text):
            yield {"delta": piece}

        calls = []
        for k, (name, args) in enumerate(item.get("calls", []), 1):
            raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            calls.append(
                {
                    "id": f"call_{self.i}_{k}",
                    "type": "function",
                    "function": {"name": name, "arguments": raw},
                }
            )
        yield {"message": {"role": "assistant", "content": text, "tool_calls": calls}}


def click(x, y):
    return ("computer", {"action": "left_click", "x": x, "y": y})


def typ(text, action="type"):
    return ("computer", {"action": action, "text": text})


def key(keys):
    return ("computer", {"action": "key", "keys": keys})


def act(action, **kw):
    return ("computer", {"action": action, **kw})


def run(client, **kw):
    kw.setdefault("max_steps", 10)
    return list(c.run_computer(client, ASK, **kw))


def finish_of(events):
    fin = next((e for e in events if e["type"] == "finish"), None)
    assert fin is not None, "必须总要发出 finish 帧"
    return fin


# 中心坐标（来自 computer._ELEMENTS，测试里写死以便断言"真值")
P_ORDER = (360, 148)
P_AMOUNT = (360, 238)
P_DATE = (360, 328)
P_SUBMIT = (170, 438)
P_RESET = (380, 438)
P_EMPTY = (900, 700)


def happy_script():
    return [
        {"content": "先点 ORDER ID。", "calls": [click(*P_ORDER)]},
        {"content": "输入单号。", "calls": [typ("ORDER-2026-0917")]},
        {"content": "再点金额。", "calls": [click(*P_AMOUNT)]},
        {"content": "输入金额。", "calls": [typ("2158.50")]},
        {"content": "点日期。", "calls": [click(*P_DATE)]},
        {"content": "输入日期。", "calls": [typ("2026-09-21")]},
        {"content": "点提交。", "calls": [click(*P_SUBMIT)]},
        {"content": "已完成，表单已提交。"},
    ]


# ---------------------------------------------------------------------------
# 1. 纯几何
# ---------------------------------------------------------------------------


def test_geometry():
    assert (c.element_at(*P_ORDER) or {}).get("name") == "field_order"
    assert (c.element_at(*P_SUBMIT) or {}).get("name") == "btn_submit"
    assert c.element_at(*P_EMPTY) is None
    # 点在矩形内 → 距离 0
    assert c.rect_distance(c.element_at(*P_ORDER), *P_ORDER) == 0.0
    # 点空 → 到最近元素是个正数
    far = c.nearest_distance(*P_EMPTY)
    assert far > 0, far
    # 元素表就是"真值"：5 个元素，且危险按钮只有 1 个
    spec = c.screen_spec()
    assert spec["width"] == 1024 and spec["height"] == 768
    assert len(spec["elements"]) == 5
    assert sum(1 for e in spec["elements"] if e["dangerous"]) == 1
    print(f"✅ 1. 几何自洽：5 个元素、点空时离最近元素 {far} px")


# ---------------------------------------------------------------------------
# 2. 渲染确定性
# ---------------------------------------------------------------------------


def test_render_deterministic():
    st = c.initial_state()
    a = c.render_screen(st)
    b = c.render_screen(c.initial_state())
    assert a == b, "同样状态必须渲染出逐字节相同的图"
    assert a[:8] == b"\x89PNG\r\n\x1a\n", "必须是合法 PNG 魔数"

    st2 = c.initial_state()
    st2["field_order"] = "X"
    assert c.render_screen(st2) != a, "状态变了图必须跟着变"

    url = c.to_data_url(a)
    assert url.startswith("data:image/png;base64,")
    print(f"✅ 2. 渲染确定性：同状态逐字节相同（{len(a)} B），状态一变图就变")


# ---------------------------------------------------------------------------
# 3 + 4. 正常路径 + 协议约束
# ---------------------------------------------------------------------------


def test_happy_path_and_protocol():
    client = ScriptedClient(happy_script())
    events = run(client)
    fin = finish_of(events)

    assert fin["success"] is True, fin["state"]
    assert fin["fieldScore"] == 3 and fin["fieldTotal"] == 3
    assert fin["submitted"] is True
    assert fin["clicks"] == 4 and fin["hitClicks"] == 4
    assert fin["clickHitRate"] == 1.0
    assert fin["avgClickErrorPx"] == 0.0, "全部点中 → 没有点空偏差"
    # ⭐ 全部点中 → 点空偏差为 0；但**中心偏差不为 0** —— 这正是新指标的用处
    assert fin["avgCenterOffsetPx"] > 0, "全中也要有精度读数，否则 0.0 会被误读成零误差"
    assert fin["lostKeystrokes"] == 0
    assert fin["steps"] == 7, fin["steps"]
    assert len(fin["actions"]) == 7
    assert fin["error"] is None

    # 每一步都要有截图帧：初始 1 张 + 每轮 1 张
    shots = [e for e in events if e["type"] == "screen"]
    assert len(shots) == 8, len(shots)
    assert shots[0]["step"] == 0 and shots[-1]["step"] == 7
    assert all(s["image"].startswith("data:image/png;base64,") for s in shots)

    # ⭐ 协议：图像绝不能走 tool 消息；tool 消息必须与 tool_call 一一配对
    msgs = client.snapshots[-1]

    # 先收集全部 tool 消息，再验配对 —— 必须两趟扫：一趟扫完就断言会误判“还没扫到”
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    tool_ids = {m.get("tool_call_id") for m in tool_msgs}
    assert all(isinstance(m["content"], str) for m in tool_msgs), "tool 消息只能装文本"
    assert all(not m["content"].startswith("data:image") for m in tool_msgs), "tool 消息里不许塞图片"

    user_with_image = 0
    for m in msgs:
        if m["role"] == "user" and isinstance(m["content"], list):
            if any(b.get("type") == "image_url" for b in m["content"]):
                user_with_image += 1

    unpaired = []
    for m in msgs:
        if m["role"] == "assistant":
            for call in m.get("tool_calls") or []:
                if call["id"] not in tool_ids:
                    unpaired.append(call["id"])
    assert not unpaired, f"这些 tool_call 没有配对的 tool 消息：{unpaired}"

    paired = len(tool_msgs)
    assert user_with_image == 8, f"初始 1 张 + 7 轮新截图都要经 user 消息回灌，实得 {user_with_image}"
    print(f"✅ 3. 正常路径：success=True、3/3 字段、命中率 100%、7 轮 4 次点击全中")
    print(f"✅ 4. 协议：{paired} 条 tool 消息全部只装文本，8 张截图全走 user 多模态消息且配对完整")


# ---------------------------------------------------------------------------
# 5. 点空 → 键盘被丢弃（本阶段最锋利的判据）
# ---------------------------------------------------------------------------


def test_missed_click_loses_keystrokes():
    script = [
        {"content": "点一下空白处。", "calls": [click(*P_EMPTY)]},
        {"content": "直接打字。", "calls": [typ("OOPS")]},
        {"content": "重新点输入框。", "calls": [click(*P_ORDER)]},
        {"content": "这次输入。", "calls": [typ("ORDER-2026-0917")]},
        {"content": "先到这里。"},
    ]
    events = run(ScriptedClient(script))
    fin = finish_of(events)

    assert fin["clicks"] == 2 and fin["hitClicks"] == 1
    assert fin["clickHitRate"] == 0.5
    assert fin["lostKeystrokes"] == 1, "点空后打字必须被记为键盘丢弃"
    assert abs(fin["avgClickErrorPx"] - c.nearest_distance(*P_EMPTY)) < 0.01
    # 那一帧 type 带 lostKeys 标记；普通那一帧没有
    typed = [a for a in fin["actions"] if a["action"] == "type"]
    assert typed[0].get("lostKeys") is True, typed[0]
    assert typed[1].get("lostKeys") is not True, typed[1]
    # 任务没完成：只填对了 1 项、没提交
    assert fin["fieldScore"] == 1 and fin["submitted"] is False and fin["success"] is False
    print(f"✅ 5. 点空判据：命中率 50%、键盘丢弃 1 次、点空偏差 {fin['avgClickErrorPx']} px、任务未完成")


# ---------------------------------------------------------------------------
# 6 + 7 + 8. 沙箱与健壮性
# ---------------------------------------------------------------------------


def test_clamp_and_dangerous_gate():
    # 越界坐标被钳制，而不是报错
    script = [{"content": "", "calls": [click(99999, -50)]}, {"content": "好了。"}]
    fin = finish_of(run(ScriptedClient(script)))
    a = fin["actions"][0]
    assert a["ok"] is True, a
    assert (a["x"], a["y"]) == (1023, 0), a
    assert a["target"] is None and a["errorPx"] is not None

    # 危险按钮：默认拒绝
    script = [{"content": "", "calls": [click(*P_RESET)]}, {"content": "算了。"}]
    fin = finish_of(run(ScriptedClient(script), allow_dangerous=False))
    a = fin["actions"][0]
    assert a["ok"] is False and "危险" in (a["error"] or ""), a
    assert a["target"] == "btn_reset", "被拒也要记录它想点哪儿"
    assert "危险" in (a["note"] or "")
    # 被拒的动作不进状态：光标没动
    assert fin["state"]["status"].startswith("Idle"), fin["state"]

    # 显式授权后放行
    fin2 = finish_of(run(ScriptedClient(script), allow_dangerous=True))
    assert fin2["actions"][0]["ok"] is True, fin2["actions"][0]
    print("✅ 6. 越界坐标被钳制为 (1023, 0)（不报错，避免模型卡死）")
    print("✅ 7. 危险动作默认被拒且不改状态；allow_dangerous=True 后才放行")


def test_malformed_calls_survive():
    script = [
        {
            "content": "",
            "calls": [
                ("not_a_tool", {}),  # 未知工具名
                ("computer", {"action": "double_click"}),  # 非法 action
                ("computer", "{这不是合法 JSON"),  # 坏 arguments
                ("computer", {"action": "left_click"}),  # 缺坐标
                ("computer", {"action": "left_click", "x": "abc", "y": 1}),  # 坐标不是数字
                ("computer", {"action": "type"}),  # 缺 text
                ("computer", {"action": "key", "keys": "F13"}),  # 不支持的键
            ],
        },
        {"content": "放弃了。"},
    ]
    events = run(ScriptedClient(script))
    fin = finish_of(events)
    # 未知工具名不进 actions（它连 computer 都不是）；其余 6 个都记下来且都 ok=False
    assert len(fin["actions"]) == 6, fin["actions"]
    assert all(a["ok"] is False for a in fin["actions"]), fin["actions"]
    assert all(a["error"] for a in fin["actions"])
    assert fin["error"] is None, "全是模型自己的错，不该算引擎失败"
    # 每一步都被回灌了 notes（数量配对）
    notes = [e for e in events if e["type"] == "action"]
    assert len(notes) == 6
    print("✅ 8. 未知工具 / 非法 action / 坏 JSON / 缺参 / 键名错 —— 全部安全降级且流程不崩")


# ---------------------------------------------------------------------------
# 9. 非 ASCII 与 lostKeystrokes 的边界（别把两件事混起来）
# ---------------------------------------------------------------------------


def test_non_ascii_is_not_lost_keystrokes():
    st = c.initial_state()
    c._step(st, "left_click", {"x": P_ORDER[0], "y": P_ORDER[1]}, False)
    rec, note, landed = c._step(st, "type", {"text": "林晓"}, False)
    assert landed == "field_order", "键盘确实送进了输入框（只是没有 ASCII 可打）"
    assert st["field_order"] == "", "非 ASCII 全被键盘丢弃"
    assert "丢弃" in note
    # 这一帧不该被打上 lostKeys：它不是"点空"导致的
    assert rec.get("lostKeys") is None

    # 反例：没聚焦 → 才是丢了
    st2 = c.initial_state()
    _, _, landed2 = c._step(st2, "type", {"text": "AB"}, False)
    assert landed2 is None
    print("✅ 9. 非 ASCII 被丢弃但不算 lostKeystrokes；无焦点打字才算 —— 两件事分得清")


# ---------------------------------------------------------------------------
# 10. 提交闸门
# ---------------------------------------------------------------------------


def test_submit_gate_and_enter_key():
    st = c.initial_state()
    _, note, _ = c._step(st, "left_click", {"x": P_SUBMIT[0], "y": P_SUBMIT[1]}, False)
    assert st["submitted"] is False and "拒绝" in note, note

    # 填满三项后，用 Enter 也要能提交（键盘路径）
    st2 = c.initial_state()
    for name, val in zip(c.FIELDS, ("ORDER-2026-0917", "2158.50", "2026-09-21")):
        st2["focus"] = name
        c._step(st2, "type", {"text": val}, False)
    assert st2["field_order"] and st2["field_amount"] and st2["field_date"]
    _, note, _ = c._step(st2, "key", {"keys": "Enter"}, False)
    assert st2["submitted"] is True, note

    # Tab 轮转焦点、Backspace 删字符
    st3 = c.initial_state()
    c._step(st3, "key", {"keys": "Tab"}, False)
    assert st3["focus"] == c.FIELDS[0], st3["focus"]
    c._step(st3, "type", {"text": "ABC"}, False)
    c._step(st3, "key", {"keys": "Backspace"}, False)
    assert st3["field_order"] == "AB", st3["field_order"]
    print("✅ 10. 未填完提交被拒；填满后 Enter 可提交；Tab/Backspace 正常")


# ---------------------------------------------------------------------------
# 11. 无副作用动作
# ---------------------------------------------------------------------------


def test_keepalive_actions():
    st = c.initial_state()
    before = dict(st)
    r1, _, _ = c._step(st, "screenshot", {}, False)
    r2, _, _ = c._step(st, "wait", {}, False)
    assert r1["ok"] and r2["ok"]
    assert st["cursor"] == before["cursor"], "screenshot/wait 不该动状态"
    assert st["revision"] == before["revision"]
    print("✅ 11. screenshot / wait 无副作用（不改状态、不动 revision）")


# ---------------------------------------------------------------------------
# 12 + 13. 软失败、blocking 形状、耗时自洽
# ---------------------------------------------------------------------------


def test_partial_failure_still_finishes():
    client = ScriptedClient(happy_script())
    client.raise_on_round = 2  # 第二轮开始上游报错
    events = run(client)
    fin = finish_of(events)

    errs = [e for e in events if e["type"] == "error"]
    assert errs, "第 2 轮报错必须发 error 帧"
    assert fin["error"] and "失败" in fin["error"], fin["error"]
    assert len(fin["actions"]) == 1, "第 1 轮的动作要保住"
    assert fin["success"] is False
    # 关键：Response 能收下这份"带 error 的部分结果"
    resp = schemas.ComputerResponse(**{k: v for k, v in fin.items() if k != "type"})
    assert resp.error and resp.actions
    print("✅ 12. 上游中途报错：发 error 帧 **且** 仍发 finish（保住第 1 轮动作），Response 可收下")


def test_no_tool_call_at_all():
    events = run(ScriptedClient([{"content": "我什么都不做。"}]))
    fin = finish_of(events)
    assert fin["actions"] == [] and fin["steps"] == 0
    assert fin["success"] is False and fin["clickHitRate"] == 0.0
    assert fin["avgClickErrorPx"] == 0.0 and fin["lostKeystrokes"] == 0
    assert fin["error"] is None
    print("✅ 13a. 模型一次工具都不调：0 动作、不崩、评分全 0 且 error=None")


def test_times_and_blocking_shape():
    fin = finish_of(run(ScriptedClient(happy_script())))
    assert set(fin["times"]) == {"render", "llm"}, fin["times"]
    assert abs(fin["llmMs"] - fin["times"]["llm"]) < 0.01, (fin["llmMs"], fin["times"])
    assert fin["totalMs"] >= fin["llmMs"], (fin["totalMs"], fin["llmMs"])
    assert fin["times"]["render"] > 0, "渲染必然花了时间"

    result = c.run_computer_blocking(ScriptedClient(happy_script()), ASK, max_steps=10)
    assert "type" not in result, "blocking 结果里不该残留帧类型字段"
    resp = schemas.ComputerResponse(**result)
    assert resp.success is True and resp.clickHitRate == 1.0
    assert len(resp.screen.elements) == 5
    assert resp.screen.elements[0].name == "field_order"
    assert resp.actions and resp.actions[0].note, "动作要带上模型的观察结果"
    print(f"✅ 13b. 耗时自洽（render {fin['times']['render']} ms / llm {fin['llmMs']} ms，"
          f"合计 {fin['totalMs']} ms）、blocking 形状完整且 ComputerResponse 校验通过")




# ---------------------------------------------------------------------------
# 14. ⭐ 定位精度：命中时的中心偏差（与「命中率」互补）
# ---------------------------------------------------------------------------


def test_center_offset_precision():
    """中心偏差回答「点得多准」—— 它在全部点中时仍有值，所以只有它能横向比较。"""
    el = c.element_at(*P_ORDER)
    cx = el["x"] + el["w"] / 2
    cy = el["y"] + el["h"] / 2

    # 正中心 → 0；偏 30px → 30
    assert c.center_offset(el, round(cx), round(cy)) == 0.0
    assert c.center_offset(el, round(cx) + 30, round(cy)) == 30.0

    # 正常路径：4 次点击的均值，手算一遍对照
    fin = finish_of(run(ScriptedClient(happy_script())))
    clicks = [a for a in fin["actions"] if a["action"] == "left_click"]
    assert len(clicks) == 4
    offs = [a["centerOffsetPx"] for a in clicks]
    assert all(o is not None for o in offs), "命中的点击必须有中心偏差"
    assert abs(fin["avgCenterOffsetPx"] - round(sum(offs) / len(offs), 1)) < 0.05
    assert fin["avgClickErrorPx"] == 0.0, "全中 → 点空偏差是 0"
    assert fin["avgCenterOffsetPx"] > 0, "全中 → 精度仍然可量，这就是它存在的理由"

    # 点空那次：centerOffsetPx 为 None（点空要看 errorPx）
    script_miss = [
        {"content": "", "calls": [click(*P_EMPTY)]},
        {"content": "算了。"},
    ]
    fin2 = finish_of(run(ScriptedClient(script_miss)))
    miss = fin2["actions"][0]
    assert miss["centerOffsetPx"] is None, miss
    assert miss["errorPx"] is not None, miss

    # 一次都不点 → 两个偏差都是 0，但 clicks 也是 0（「没样本」不等于「零误差」）
    fin3 = finish_of(run(ScriptedClient([{"content": "不动。"}])))
    assert fin3["clicks"] == 0 and fin3["avgCenterOffsetPx"] == 0.0

    print(
        "✅ 14. 定位精度：4 次点击平均偏中心 "
        + str(fin["avgCenterOffsetPx"])
        + " px（点空偏差 "
        + str(fin["avgClickErrorPx"])
        + " px）—— 命中率与精度是两件事，各自有值"
    )


if __name__ == "__main__":
    test_geometry()
    test_render_deterministic()
    test_happy_path_and_protocol()
    test_missed_click_loses_keystrokes()
    test_clamp_and_dangerous_gate()
    test_malformed_calls_survive()
    test_non_ascii_is_not_lost_keystrokes()
    test_submit_gate_and_enter_key()
    test_keepalive_actions()
    test_partial_failure_still_finishes()
    test_no_tool_call_at_all()
    test_times_and_blocking_shape()
    test_center_offset_precision()
    print("\n🎉 阶段 18 静态冒烟全部通过")
