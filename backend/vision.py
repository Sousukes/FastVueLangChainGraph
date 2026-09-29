"""阶段 16 · 多模态·图像（视觉问答 + 结构化抽取）。

前面所有阶段，模型看到的都只有**文字**。本阶段把图片也变成一种输入：

    ① 确定性解析（parse）  服务端拿到 data URL / base64 → 拆出字节 → 用**魔数**
       判断真实格式（不信任声明的 mime）→ 校验格式白名单与大小上限 → 读文件头拿尺寸；
    ② 拼多模态消息（build） 把一条 user 消息的 `content` 从"字符串"升级成"数组"：
       `[{type:text}, {type:image_url, image_url:{url: data URL, detail}}]`；
    ③ 模型调用（call）      仍是阶段 03 那个 `llm.LLMClient`，一行没改；
    ④ 两路输出              `qa` 流式回答；`extract` 流式吐出 JSON 再解析成字段表。

**本阶段最重要的认知：**「让模型看图」的秘密**不在模型调用，而在消息结构**。
阶段 03 起 `client.chat/stream` 收的就是 `messages`；我们只是把其中一条 user 消息的
`content` 换成了数组——协议层零改动。理解这一点，就理解了多模态的工程本质：
它是**输入编码**的一次扩展，不是一次新的模型能力接入。

为什么要在服务端做那个"确定性解析"？因为它把三件**不该交给模型**的事挡在门外：
  - 伪造格式（改了扩展名、data URL 里乱写 mime）→ 用魔数识破；
  - 超大体积（一张 50MB 的图会把请求撑爆）→ 先量字节再决定收不收；
  - 非图片内容（伪装成 png 的脚本）→ 魔数不匹配直接拒。
这正是本项目一贯的「**确定性工程流水线 + LLM**」分工：能算的别问模型。

统一 SSE 事件协议（与阶段 10–15 一致，一帧一行）：
    start / answer_delta / structured / finish / error
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import time
from typing import Any, Iterator

from pydantic import BaseModel, Field, ValidationError

from llm import LLMClient

# ---------- ① 确定性解析：格式白名单 + 魔数嗅探 + 尺寸提取 ----------

# 允许的图片格式白名单：键是魔数嗅探出的 mime，值是短的格式名
_ALLOWED: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

# 单张图上限 8 MB（原始字节）。再大就该压缩或分块，而不是把请求撑爆。
_MAX_BYTES = 8 * 1024 * 1024

_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)

# 需要剔除 base64 里可能夹带的空白（换行 / 空格）
_WS_RE = re.compile(r"\s+")


class VisionInputError(ValueError):
    """输入图片不合法（编码 / 格式 / 大小）。路由层会把它转成 400，而不是 502。"""


def _sniff(data: bytes) -> str | None:
    """用**魔数**（文件头固定字节）判断真实格式，与文件名 / data URL 里写的无关。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_size(d: bytes) -> tuple[int, int] | None:
    """PNG：8 字节签名 + 4 字节长度 + 4 字节 "IHDR"，宽高是随后两个大端 u32。"""
    if len(d) < 24:
        return None
    w, h = struct.unpack(">II", d[16:24])
    return int(w), int(h)


def _gif_size(d: bytes) -> tuple[int, int] | None:
    """GIF：逻辑屏幕描述符里的宽高是**小端** u16。"""
    if len(d) < 10:
        return None
    w, h = struct.unpack("<HH", d[6:10])
    return int(w), int(h)


def _jpeg_size(d: bytes) -> tuple[int, int] | None:
    """JPEG：没有固定偏移，要顺序遍历段，找到 SOF（帧开始）段才拿得到宽高。"""
    i, n = 2, len(d)
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        # 无参数标记（如 SOI/EOI/RSTn）没有长度字段，跳过
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", d[i + 2 : i + 4])[0]
        if marker in sof_markers:
            h, w = struct.unpack(">HH", d[i + 5 : i + 9])
            return int(w), int(h)
        i += 2 + seg_len
    return None


def _webp_size(d: bytes) -> tuple[int, int] | None:
    """WebP：三种子格式（VP8X 有损扩展 / VP8 有损 / VP8L 无损）读法各不同。"""
    if len(d) < 30:
        return None
    fourcc = d[12:16]
    if fourcc == b"VP8X":
        w = 1 + int.from_bytes(d[24:27], "little")
        h = 1 + int.from_bytes(d[27:30], "little")
        return w, h
    if fourcc == b"VP8 ":
        w = struct.unpack("<H", d[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", d[28:30])[0] & 0x3FFF
        return int(w), int(h)
    if fourcc == b"VP8L":
        bits = int.from_bytes(d[21:25], "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return int(w), int(h)
    return None


_SIZE_READERS = {"png": _png_size, "jpeg": _jpeg_size, "gif": _gif_size, "webp": _webp_size}


def _parse_image(image: str, *, max_bytes: int = _MAX_BYTES) -> dict:
    """把 data URL / 纯 base64 解析成 `{mime, format, bytes, width, height, dataUrl, declared}`。

    三件事：① 拆出 base64 并解码；② 用**魔数**（而非声明的 mime）判断真实格式；
    ③ 校验格式白名单 + 大小上限。任一失败都抛 `VisionInputError`（路由层转 400）。
    """
    s = image.strip()
    declared: str | None = None
    m = _DATA_URL_RE.match(s)
    if m:
        declared = m.group(1).lower()
        s = m.group(2)

    s = _WS_RE.sub("", s)
    if not s:
        raise VisionInputError("图片内容为空")
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise VisionInputError(f"图片不是合法的 base64：{e}") from e

    if not raw:
        raise VisionInputError("图片内容为空")
    if len(raw) > max_bytes:
        raise VisionInputError(
            f"图片过大：{len(raw) / 1024 / 1024:.1f} MB，上限 {max_bytes // 1024 // 1024} MB"
        )

    sniffed = _sniff(raw)
    if sniffed is None:
        raise VisionInputError("无法识别的图片格式（仅支持 PNG / JPEG / GIF / WebP）")
    if sniffed not in _ALLOWED:
        raise VisionInputError(f"不支持的图片格式：{sniffed}")

    fmt = _ALLOWED[sniffed]
    size = _SIZE_READERS[fmt](raw)
    w, h = size if size else (None, None)
    return {
        "mime": sniffed,
        "format": fmt,
        "bytes": len(raw),
        "width": w,
        "height": h,
        # 统一重编码成「规范 data URL」，保证交给模型的一定是干净、格式正确的 data URL
        "dataUrl": f"data:{sniffed};base64,{base64.b64encode(raw).decode()}",
        "declared": declared,
    }


def parse_image(image: str) -> dict:
    """公开入口：解析并校验图片（供端点"先校验、再开流"，让非法输入得到 400）。"""
    return _parse_image(image)


def image_meta(img: dict) -> dict:
    """把内部解析结果收成可回给前端的元数据（含「声明 mime 与魔数是否不符」）。"""
    return {
        "mime": img["mime"],
        "format": img["format"],
        "bytes": img["bytes"],
        "width": img["width"],
        "height": img["height"],
        "declared": img["declared"],
        "mismatch": bool(img["declared"] and img["declared"] != img["mime"]),
    }


# ---------- ② 多模态消息：content 从字符串升级为数组 ----------

VIS_SYSTEM = (
    "你是一个图像理解助手。用户会给你一张图片和一个问题。\n"
    "请**只依据图片中实际可见的内容**作答：\n"
    "1. 图中能看到什么就说什么，不要脑补、不要用常识补全；\n"
    "2. 若问题所需的答案在图中根本看不到，直接回答「图中未提供该信息」，不要猜测；\n"
    "3. 描述要具体（位置、颜色、文字、数量），必要时用简短列表。"
)

QA_HINT = "请用简洁、准确的中文回答，不要复述这段说明。"

EXTRACT_SYSTEM = (
    "你是一个图像信息抽取助手。请把图片中可见的关键信息抽成**结构化的字段表**。\n"
    "要求：\n"
    "1. 每个字段是一对 label/value：label 是字段名（中文），value 是图中读到的原文；\n"
    "2. 只抽图中**真实可见**的内容，读不清的写「（不清晰）」或省略，禁止推测；\n"
    "3. 表格类图片按行列逐项抽取；连续文本可整段放进一个 value；\n"
    "4. 用 summary 一句话概括这块内容是什么。\n"
    "只输出 JSON，不要解释。"
)

EXTRACT_HINT = '{"summary": "一句话概括", "fields": [{"label": "字段名", "value": "字段值"}]}'


class ExtractField(BaseModel):
    label: str = Field(min_length=1)
    value: str = ""


class ExtractResult(BaseModel):
    summary: str = ""
    fields: list[ExtractField] = Field(default_factory=list)


def _strip_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def build_messages(
    img: dict, question: str, mode: str, schema_hint: str | None, detail: str
) -> list[dict]:
    """按 OpenAI 协议拼多模态消息。

    ⭐ 关键只有一行：user 的 `content` 不是字符串，而是**块数组**——
       文本块 `{"type":"text"}` + 图像块 `{"type":"image_url","image_url":{"url":data URL}}`。
    其余与阶段 03 的单模态消息完全一样，`LLMClient` 不需要任何改动。
    """
    if mode == "extract":
        system = EXTRACT_SYSTEM
        ask = (schema_hint or "").strip() or "请抽取图中所有可见的关键信息字段。"
        text = f"{ask}\n\n请严格按下面的 JSON 输出：\n{EXTRACT_HINT}"
    else:
        system = VIS_SYSTEM
        text = (question.strip() or "请详细描述这张图片的内容。") + f"\n\n{QA_HINT}"

    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": img["dataUrl"], "detail": detail}},
            ],
        },
    ]


# ---------- ④ extract：从流式 JSON 解析字段表（一次自纠） ----------


def _parse_extract(answer: str) -> ExtractResult | None:
    try:
        return ExtractResult.model_validate_json(_strip_fence(answer))
    except (ValidationError, json.JSONDecodeError):
        return None


def _extract_with_repair(
    client: LLMClient,
    messages: list[dict],
    answer: str,
    model: str | None,
    temperature: float,
) -> ExtractResult | None:
    """先直接解析；不是合法 JSON 就**自纠一次**（把坏输出回传，要求严格重来）。"""
    parsed = _parse_extract(answer)
    if parsed is not None:
        return parsed

    fix_messages = messages + [
        {"role": "assistant", "content": answer},
        {
            "role": "user",
            "content": (
                "你上面的输出不是合法 JSON。请严格按下面的结构重来一次，"
                f"不要加任何解释或代码块标记：\n{EXTRACT_HINT}"
            ),
        },
    ]
    try:
        raw = client.chat(
            fix_messages,
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
    except Exception:  # noqa: BLE001  某些厂商不认 response_format，退化为普通调用
        try:
            raw = client.chat(fix_messages, model=model, temperature=temperature)
        except Exception:  # noqa: BLE001  自纠也失败就如实放弃，不阻塞主流程
            return None
    return _parse_extract(raw)


# ---------- 主流程 ----------


def run_vision(
    client: LLMClient,
    image: str,
    *,
    question: str = "",
    mode: str = "qa",
    schema_hint: str | None = None,
    model: str | None = None,
    detail: str = "auto",
    temperature: float = 0.2,
) -> Iterator[dict]:
    """跑一次图像理解，**逐事件 yield**。

    事件类型（延续阶段 10–15 的"一帧一行"协议，新增 `structured` 一类）：

        start        模式、模型、图像元数据（格式/尺寸/字节数/声明 mime 是否不符）
        answer_delta 流式增量（qa：解读正文；extract：JSON 原文）
        structured   extract 模式的解析结果（字段表 + 概括）
        finish       完整结果 + 元数据 + 耗时
        error        失败原因
    """
    started = time.perf_counter()
    buckets = {"encode": 0.0, "llm": 0.0}

    # ---------- ① 确定性解析 ----------
    t0 = time.perf_counter()
    img = _parse_image(image)
    buckets["encode"] = round((time.perf_counter() - t0) * 1000, 2)
    meta = image_meta(img)

    yield {"type": "start", "mode": mode, "model": model or client.model, "image": meta}

    messages = build_messages(img, question, mode, schema_hint, detail)

    # ---------- ③ 多模态调用（协议层零改动） ----------
    t0 = time.perf_counter()
    answer = ""
    for piece in client.stream(messages, model=model, temperature=temperature):
        answer += piece
        yield {"type": "answer_delta", "text": piece}
    buckets["llm"] = round((time.perf_counter() - t0) * 1000, 2)

    # ---------- ④ extract：解析（必要时自纠一次） ----------
    fields: list[dict] = []
    summary: str | None = None
    extracted = False
    if mode == "extract":
        parsed = _extract_with_repair(client, messages, answer, model, temperature)
        if parsed is not None:
            fields = [f.model_dump() for f in parsed.fields]
            summary = parsed.summary or None
            extracted = True
            yield {"type": "structured", "summary": summary, "fields": fields, "raw": answer}

    yield {
        "type": "finish",
        "mode": mode,
        "model": model or client.model,
        "question": question or None,
        "answer": answer,
        "summary": summary,
        "fields": fields,
        "extracted": extracted,
        "image": meta,
        "times": dict(buckets),
        "totalMs": round((time.perf_counter() - started) * 1000, 2),
        "llmMs": buckets["llm"],
    }


def run_vision_blocking(client: LLMClient, image: str, **kwargs: Any) -> dict:
    """把生成器跑完，收成一份完整结果（给非流式调用方用）。"""
    events = list(run_vision(client, image, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)

    base: dict[str, Any] = {
        "mode": (finish or {}).get("mode") or start.get("mode") or kwargs.get("mode") or "qa",
        "model": (finish or {}).get("model") or start.get("model") or kwargs.get("model") or client.model,
        "question": kwargs.get("question") or None,
        "image": start.get("image"),
    }
    if finish is None:
        err = next((e for e in events if e["type"] == "error"), None)
        return {
            **base,
            "answer": None,
            "summary": None,
            "fields": [],
            "extracted": False,
            "times": {},
            "totalMs": 0.0,
            "llmMs": 0.0,
            "error": (err or {}).get("message", "未知错误"),
        }
    return {
        **base,
        "answer": finish.get("answer"),
        "summary": finish.get("summary"),
        "fields": finish.get("fields", []),
        "extracted": finish.get("extracted", False),
        "image": finish.get("image") or base["image"],
        "times": finish.get("times", {}),
        "totalMs": finish.get("totalMs", 0.0),
        "llmMs": finish.get("llmMs", 0.0),
        "error": None,
    }
