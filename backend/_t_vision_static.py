"""阶段 16 静态冒烟测试（不需要 Key；确定性解码 + 脚本化桩 client）。

验证九件事：
  1. 确定性解析：真 PNG → format=png，宽高从文件头读出，bytes 一致。
  2. 魔数优先：字节是 GIF、data URL 却声明成 png → 以魔数为准（gif），mismatch=True。
  3. 非法输入（坏 base64 / 非图片内容）→ VisionInputError，而不是崩溃。
  4. 体积上限：小上限下超限即拒（测守卫逻辑本身，不真造 8MB）。
  5. 多模态消息结构：build_messages 把 user.content 拼成「文本块 + 图像块」数组（本阶段重点）。
  6. qa 模式：流式产出拼成完整回答；extracted=False、fields 为空。
  7. extract 模式：流式 JSON 被解析成字段表；structured 帧存在、extracted=True。
  8. extract 自纠：流式输出不是 JSON → 一次 chat 自纠后仍能解析成功。
  9. 自纠也失败：chat 抛错 → extracted=False，但原始 answer 保留、整流程不崩。
  10. 耗时自洽：llmMs == times["llm"]，totalMs >= llmMs。

桩 client 按**系统提示词**分流（与阶段 11/13/14/15 的 ScriptedClient 同思路）：
本阶段只有两条 system：vision.VIS_SYSTEM（问答）与 vision.EXTRACT_SYSTEM（抽取）。
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

import vision


# ---------- 构造真 PNG / GIF（只用到标准库，不依赖 Pillow） ----------


def _png(w: int, h: int, rgb: tuple[int, int, int] = (220, 30, 30)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 20


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# ---------- 桩 client ----------


class ScriptedClient:
    """按 system 提示词分流的确定性 client（用到 stream + chat）。"""

    model = "stub-vision-model"

    def __init__(self, qa_text: str = "图中是一个红色方块。", extract_raw: str | None = None) -> None:
        self.qa_text = qa_text
        self.extract_raw = extract_raw
        self.calls: list[str] = []

    def _which(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        if system == vision.VIS_SYSTEM:
            return "qa"
        if system == vision.EXTRACT_SYSTEM:
            return "extract"
        raise AssertionError(f"未预期的 system 提示词：{system[:60]}")

    def stream(self, messages, model=None, temperature=0.3):
        kind = self._which(messages)
        self.calls.append(f"stream:{kind}")
        text = self.qa_text if kind == "qa" else (self.extract_raw or "")
        for ch in text:
            yield ch

    def chat(self, messages, model=None, temperature=0.3, response_format=None):
        kind = self._which(messages)  # 自纠时第一条仍是同一 system
        self.calls.append(f"chat:{kind}")
        if self.extract_raw == "__RAISE__":
            raise RuntimeError("自纠通道也不可用")
        return json.dumps(
            {"summary": "表格", "fields": [{"label": "标题", "value": "月度报表"}]},
            ensure_ascii=False,
        )


def _finish(events: list[dict]) -> dict:
    return next(e for e in events if e["type"] == "finish")


QA_IMAGE = "data:image/png;base64," + _b64(_png(64, 32))


# ---------- 测试 ----------


def test_parse_png_dimensions():
    """① 真 PNG：格式、宽高、字节数都对。"""
    meta = vision.parse_image(QA_IMAGE)
    assert meta["format"] == "png", meta
    assert (meta["width"], meta["height"]) == (64, 32), meta
    assert meta["mime"] == "image/png"
    assert meta["bytes"] == len(_png(64, 32))
    assert not vision.image_meta(meta)["mismatch"]
    print("✅ 1. 确定性解析：PNG → 64x32，格式/字节数正确")


def test_magic_number_beats_declared_mime():
    """② 字节是 GIF 却声明成 png → 以魔数为准，并标出 mismatch。"""
    spoof = "data:image/png;base64," + _b64(_gif(10, 20))
    meta = vision.image_meta(vision.parse_image(spoof))
    assert meta["format"] == "gif", meta
    assert (meta["width"], meta["height"]) == (10, 20), meta
    assert meta["declared"] == "image/png"
    assert meta["mismatch"] is True, meta
    print("✅ 2. 魔数优先：声明 png 实为 gif → format=gif、mismatch=True")


def test_invalid_inputs_rejected():
    """③ 坏 base64 / 非图片内容 → VisionInputError（供路由层转 400）。"""
    cases = {
        "坏 base64": "data:image/png;base64,@@@not-base64@@@",
        "非图片内容": "data:image/png;base64," + _b64(b"hello, i am not an image"),
        "空内容": "data:image/png;base64," + _b64(b""),
    }
    for label, payload in cases.items():
        try:
            vision.parse_image(payload)
        except vision.VisionInputError:
            continue
        raise AssertionError(f"{label} 未被拒绝")
    print("✅ 3. 非法输入（坏 base64 / 非图 / 空）均抛 VisionInputError")


def test_size_limit_guard():
    """④ 体积守卫：用小上限测逻辑本身（不真造 8MB）。"""
    raw = _png(8, 8)
    try:
        vision._parse_image("data:image/png;base64," + _b64(raw), max_bytes=4)
    except vision.VisionInputError as e:
        assert "过大" in str(e), str(e)
        print("✅ 4. 体积上限生效（小上限下超限即拒，提示「过大」）")
        return
    raise AssertionError("超限图片未被拒绝")


def test_multimodal_message_shape():
    """⑤ ⭐ 本阶段重点：user.content 是「文本块 + 图像块」数组，而不是字符串。"""
    img = vision.parse_image(QA_IMAGE)
    msgs = vision.build_messages(img, "这是什么？", "qa", None, "auto")
    assert msgs[0]["role"] == "system" and isinstance(msgs[0]["content"], str)
    user = msgs[1]
    assert user["role"] == "user" and isinstance(user["content"], list), user
    kinds = [b["type"] for b in user["content"]]
    assert kinds == ["text", "image_url"], kinds
    image_block = user["content"][1]["image_url"]
    assert image_block["url"].startswith("data:image/png;base64,"), image_block
    assert image_block["detail"] == "auto"
    # 抽取模式换成 EXTRACT_SYSTEM，且把 JSON 结构写进文本块
    msgs2 = vision.build_messages(img, "", "extract", "抽标题", "high")
    assert msgs2[0]["content"] == vision.EXTRACT_SYSTEM
    assert vision.EXTRACT_HINT.split(":")[0] in msgs2[1]["content"][0]["text"]
    print("✅ 5. 多模态消息结构正确：content=[text, image_url]，detail 透传")


def test_qa_mode_streams_answer():
    """⑥ qa 模式：流式拼成完整回答，extracted=False、fields 为空。"""
    client = ScriptedClient(qa_text="图中是一个红色方块。")
    events = list(vision.run_vision(client, QA_IMAGE, question="图里有什么？", mode="qa"))
    fin = _finish(events)
    assert fin["mode"] == "qa"
    assert fin["answer"] == "图中是一个红色方块。", fin["answer"]
    assert fin["extracted"] is False and fin["fields"] == []
    assert any(e["type"] == "answer_delta" for e in events)
    start = next(e for e in events if e["type"] == "start")
    assert start["image"]["format"] == "png"
    print("✅ 6. qa 模式：流式回答拼接正确，extracted=False")


def test_extract_mode_parses_fields():
    """⑦ extract 模式：流式 JSON 解析成字段表，并发出 structured 帧。"""
    raw = json.dumps(
        {"summary": "一张月度报表", "fields": [{"label": "月份", "value": "2026-09"}, {"label": "金额", "value": "¥1,200"}]},
        ensure_ascii=False,
    )
    client = ScriptedClient(extract_raw=raw)
    events = list(vision.run_vision(client, QA_IMAGE, mode="extract", schema_hint="抽表格"))
    fin = _finish(events)
    assert fin["extracted"] is True, fin
    assert len(fin["fields"]) == 2, fin["fields"]
    assert fin["fields"][0] == {"label": "月份", "value": "2026-09"}, fin["fields"]
    assert fin["summary"] == "一张月度报表"
    structured = next((e for e in events if e["type"] == "structured"), None)
    assert structured is not None and structured["summary"] == "一张月度报表"
    print("✅ 7. extract 模式：JSON → 2 个字段 + 概括，structured 帧已发出")


def test_extract_repair_path():
    """⑧ 流式输出不是合法 JSON → 一次 chat 自纠后仍解析成功。"""
    client = ScriptedClient(extract_raw="好的！这是结果：{不是合法 JSON}")  # prefix 让 JSON 解析失败
    events = list(vision.run_vision(client, QA_IMAGE, mode="extract"))
    fin = _finish(events)
    assert fin["extracted"] is True, fin
    assert fin["fields"] == [{"label": "标题", "value": "月度报表"}], fin["fields"]
    assert "chat:extract" in client.calls, client.calls  # 确实走了自纠
    print("✅ 8. extract 自纠：坏 JSON → chat 修复后解析成功")


def test_extract_repair_failure_is_safe():
    """⑨ 自纠也失败 → extracted=False，但保留原始 answer、流程不崩。"""
    client = ScriptedClient(extract_raw="__RAISE__")
    # 让流式也吐出非 JSON 内容（否则不会触发自纠）
    client.extract_raw = "__RAISE__"

    def stream(messages, model=None, temperature=0.3):
        yield "抱歉，我无法输出 JSON"

    client.stream = stream  # type: ignore[method-assign]
    events = list(vision.run_vision(client, QA_IMAGE, mode="extract"))
    fin = _finish(events)
    assert fin["extracted"] is False, fin
    assert fin["fields"] == []
    assert "无法输出 JSON" in (fin["answer"] or ""), fin["answer"]
    print("✅ 9. 自纠亦失败：extracted=False，原始输出保留，流程正常收尾")


def test_times_self_consistent():
    """⑩ 耗时自洽：llmMs == times['llm']，totalMs >= llmMs，encode 非负。"""
    client = ScriptedClient(qa_text="红")
    events = list(vision.run_vision(client, QA_IMAGE, question="颜色？"))
    fin = _finish(events)
    t = fin["times"]
    assert abs(t["llm"] - fin["llmMs"]) < 0.01, (t, fin["llmMs"])
    assert fin["totalMs"] >= fin["llmMs"], (fin["totalMs"], fin["llmMs"])
    assert t["encode"] >= 0.0
    print("✅ 10. 耗时自洽：llmMs==times['llm']，totalMs>=llmMs")


def test_blocking_response_shape():
    """顺带验证 blocking 包装可被 VisionResponse 收下。"""
    client = ScriptedClient(qa_text="一只猫")
    result = vision.run_vision_blocking(client, QA_IMAGE, question="是什么？", mode="qa")
    assert result["mode"] == "qa"
    assert result["answer"] == "一只猫"
    assert result["error"] is None
    assert result["image"]["format"] == "png"
    print("✅ 11. blocking 包装结构正确（mode/answer/image/error 齐全）")


if __name__ == "__main__":
    test_parse_png_dimensions()
    test_magic_number_beats_declared_mime()
    test_invalid_inputs_rejected()
    test_size_limit_guard()
    test_multimodal_message_shape()
    test_qa_mode_streams_answer()
    test_extract_mode_parses_fields()
    test_extract_repair_path()
    test_extract_repair_failure_is_safe()
    test_times_self_consistent()
    test_blocking_response_shape()
    print("\n🎉 阶段 16 静态冒烟全部通过")
