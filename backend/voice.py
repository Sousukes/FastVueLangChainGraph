"""阶段 17 · 多模态·语音（语音管线的**文本侧编排**）。

阶段 16 把图片变成了输入；本阶段处理另一种模态：**声音**。但在动手之前，
必须先问清一个工程边界问题——**语音能力到底归谁？**

实测结论（本机跑过）：DeepSeek **没有**音频接口。
    POST /audio/transcriptions  → 404
    POST /audio/speech          → 404
也就是说，**DeepSeek 只能读文本**。于是本阶段的设计被这个事实唯一地决定了：

    ┌──────────── 浏览器（Web Speech API，零依赖零密钥）────────────┐
    │  🎤 SpeechRecognition（ASR）──转写文本──▶                    │
    │                              ◀──朗读稿── speakSynthesis（TTS）│
    └───────────────────────────────────────────────────────────────┘
                                   │            ▲
                              转写文本           朗读稿
                                   ▼            │
    ┌──────────────────── 后端：语音管线的文本侧编排 ─────────────────┐
    │  ① 口语化改写（LLM，流式）  书面语 → 适合朗读的短句，且**无 Markdown** │
    │  ② 朗读稿规范化（LLM，JSON）Text Normalization：符号写法 → 读出来的样子 │
    │  ③ 确定性体检（不用模型）   残留 Markdown 计数 + 朗读时长估算        │
    └───────────────────────────────────────────────────────────────┘

**为什么音频边界要放在浏览器？** 因为「能用平台能力解决的事就别自己造」。
Web Speech API 在 Chrome/Edge 上开箱即用；把它硬搬到服务端，就得引入一套
ASR/TTS 服务与密钥，收益为零、复杂度暴涨。**先想清楚边界，再写代码。**

**为什么服务端仍然不可省？** 因为「能读出来」和「读得好」是两件事：
  - 大模型默认输出 Markdown（`## 标题`、`- 列表`、`**加粗**`）——**念出来全是噪声**；
  - `2026-09-21`、`¥2,158.50`、`USB-C` 直接送进 TTS，读出来往往是错的。
这两件事都值得一次 LLM 调用，也都**可被验证**（前者甚至可以用确定性规则验证）。

复用资产：`llm.LLMClient.stream`（口语化改写要**边想边念**）、阶段 04 的
「JSON + Pydantic 校验 + 一次自纠」招式（本文件自带一份紧凑实现，见 `_json_call`）。

统一 SSE 事件协议（与阶段 10–16 一致，一帧一行）：
    start / answer_delta / rewrite / speak / finish / error
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

from pydantic import BaseModel, Field, ValidationError

from llm import LLMClient

# ---------- 输入校验 ----------

_MAX_TRANSCRIPT = 2000  # 单次提问的转写文本上限（语音提问本就不会很长）


class VoiceInputError(ValueError):
    """输入文本不合法（空 / 过长）。路由层会把它转成 400，而不是 502。"""


def _check_transcript(transcript: str, *, max_chars: int = _MAX_TRANSCRIPT) -> str:
    text = (transcript or "").strip()
    if not text:
        raise VoiceInputError("转写文本为空：请先说一句，或直接在输入框里打字")
    if len(text) > max_chars:
        raise VoiceInputError(f"转写文本过长：{len(text)} 字，上限 {max_chars} 字")
    return text


# ---------- ③ 确定性体检（不用模型的部分） ----------

# 行首的 Markdown 结构标记：标题 / 列表 / 引用 / 有序列表
_MD_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|>\s|\d+[.)]\s)", re.MULTILINE)
# 行内标记：加粗 / 斜体 / 行内代码 / 链接 / 图片 / 表格分隔
_MD_INLINE_RE = re.compile(r"\*\*|__|`|\[[^\]]*\]\([^)]*\)|!\[|\|")

# 朗读时长估算用的两类「字」：中日韩字符按字读，拉丁词按词读
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*")

# 语速经验值：中文约 4.5 字/秒（≈270 字/分），英文约 2.5 词/秒（≈150 词/分）
_CJK_PER_SEC = 4.5
_WORD_PER_SEC = 2.5


def markdown_left(text: str) -> int:
    """数一数这段文字里还剩多少 **Markdown 标记**（确定性规则，不用问模型）。

    ⭐ 这是本阶段的「可验证约束」：「口语稿必须无 Markdown」这条要求，
    **不需要 LLM 去判定**——一条正则就能把它变成可断言的数字。
    """
    return len(_MD_LINE_RE.findall(text)) + len(_MD_INLINE_RE.findall(text))


def spoken_profile(text: str) -> dict:
    """朗读稿体检：有效字数（中文字符 + 拉丁词）与估算朗读时长。"""
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(text))
    return {
        "cjk": cjk,
        "words": words,
        "plainChars": cjk + words,
        "estSeconds": round(cjk / _CJK_PER_SEC + words / _WORD_PER_SEC, 1),
    }


def audit_terms(answer: str, speak: str, terms: list[dict]) -> dict:
    """**确定性地核对**模型自报的对照表：每条 `from` 应真的出现在口语稿里、`to` 应真的出现在朗读稿里。

    ⭐ 为什么值得多写这 10 行：`terms` 是**模型的自述，不是 diff**。
    实测（见 docs/stages/17-MultimodalVoice.md §5）就抓到过这种情况——
    改写阶段**自己**就把 `2026-09-17` 写成了汉字，于是规范化阶段「没什么可改的」，
    对照表里只剩 `BX → B X`；而对照表的读者很可能以为「数字没被改过」。
    一句 `in` 判断就能把「自述」降级成「可核对」，这正是本课程一贯的分工：

        阶段 14  `grounded`   模型**声称**引用了吗        （答案 vs 来源编号）
        阶段 15  `faithful`   那些声称**被资料支持**吗     （逐句 vs 资料）
        阶段 17  `termCheck`  那些自述的改写**真的发生了**吗（from/to vs 两稿文本）

    返回 `{total, verified, suspect}`；`suspect` 是核对不上的条目（原样带回，便于人看）。
    """
    suspect = [
        t
        for t in terms
        if (t.get("from") or "") not in answer or (t.get("to") or "") not in speak
    ]
    return {"total": len(terms), "verified": len(terms) - len(suspect), "suspect": suspect}



# ---------- ① 口语化改写 ----------

VOICE_SYSTEM = (
    "你是一个语音助手的「口语化改写」模块。用户的问题来自语音转写，"
    "而你写出的内容会被 **直接朗读出来**（送进 TTS），所以必须「念得顺、听得懂」。\n"
    "硬性要求：\n"
    "1. **不要任何 Markdown**：不要井号标题、不要减号或星号列表、不要加粗、"
    "不要反引号代码、不要链接、不要表格、不要 emoji；\n"
    "2. **用短句**：一句话尽量不超过 25 个字，用句号断句，方便朗读时换气；\n"
    "3. **不要括号注释**，也不要「1. 2. 3.」这种编号（要分点就说「第一…第二…」）；\n"
    "4. 直接给结论，不要复述用户的问题，不要「作为一个 AI」这类客套；\n"
    "5. 总长度控制在目标字数以内；信息不够就说到哪算哪，**不要编造**。\n"
    "只输出要朗读的正文，不要任何额外说明。"
)

# 三种口语风格：同一件事，说的方式不同
STYLE_SPEC = {
    "brief": "简洁作答：只讲最关键的结论，能用一句话说清就别用两句。",
    "explain": "展开讲解：先给结论，再补一到两个必要的理由或例子，仍然保持短句。",
    "step": "分步口述：按「第一步…第二步…」的顺序讲清做法，每步一句话。",
}


def _build_rewrite_messages(transcript: str, style: str, max_chars: int) -> list[dict]:
    spec = STYLE_SPEC.get(style, STYLE_SPEC["brief"])
    return [
        {"role": "system", "content": VOICE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"【用户说】{transcript}\n\n"
                f"【风格要求】{spec}\n"
                f"【长度限制】不超过 {max_chars} 字。\n"
                f"请直接给出要朗读的正文。"
            ),
        },
    ]


# ---------- ② 朗读稿规范化（Text Normalization） ----------

TN_SYSTEM = (
    "你是 TTS（语音合成）之前的「文本规范化」模块。给定一段口语稿，"
    "它会被原样送进语音合成器朗读。请把所有**符号化的写法**改写成**读出来的样子**：\n"
    "1. 日期：2026-09-21 → 二零二六年九月二十一日；\n"
    "2. 金额：¥2,158.50 → 两千一百五十八元五角；158.50元 → 一百五十八元五角；\n"
    "3. 数字：0.85 → 零点八五；3.5% → 百分之三点五；1200 → 一千二百；\n"
    "4. 缩写与型号：USB-C → U S B C；AI → A I；RAG → R A G；HTTP → H T T P；\n"
    "5. 单位与符号：km → 公里；℃ → 摄氏度；& → 和；\n"
    "6. 其余保持**原意、语序、风格完全不变**，不要增删内容、不要改写得更好听。\n"
    "另外，把所有做过改写的项**逐条**列进 terms（from = 原文写法，to = 朗读写法）。\n"
    "只输出 JSON，不要解释。"
)

TN_HINT = '{"speak": "规范化之后的朗读稿", "terms": [{"from": "原文写法", "to": "朗读写法"}]}'


class SpeakTerm(BaseModel):
    from_: str = Field(alias="from")
    to: str = ""

    model_config = {"populate_by_name": True}


class SpeakPlan(BaseModel):
    speak: str = ""
    terms: list[SpeakTerm] = Field(default_factory=list)


def _strip_fence(text: str) -> str:
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
    """JSON 模式 + Pydantic 校验 + 一次自纠重试，返回 (对象 | None, 原文)。

    与阶段 11/13/14/15/16 同源，本文件自带一份紧凑实现（见下方 TECH_DEBT）。
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


TECH_DEBT = (
    "阶段 11/13/14/15/16/17 各自持有一份 _json_call（JSON 模式 + Pydantic 校验 + 自纠）。"
    "按阶段 12 规矩应在第三次抽框架，现已第六处——下次若再出现，应下沉到 llm.py "
    "并同步更新各 _t_*_static.py 的 ScriptedClient 分流。"
)


def _normalize(
    client: LLMClient, answer: str, model: str | None, temperature: float
) -> tuple[str, list[dict]]:
    """做一次文本规范化，返回 (朗读稿, terms)。

    失败时**保守降级**：朗读稿退回口语稿原文、terms 留空——**绝不让规范化把内容弄丢**。
    """
    messages = [
        {"role": "system", "content": TN_SYSTEM},
        {
            "role": "user",
            "content": f"【口语稿】\n{answer}\n\n请严格按下面的 JSON 输出：\n{TN_HINT}",
        },
    ]
    plan, _ = _json_call(client, messages, model, temperature, SpeakPlan, TN_HINT)
    if plan is None or not plan.speak.strip():
        return answer, []
    terms = [t.model_dump(by_alias=True) for t in plan.terms if t.from_.strip()]
    return plan.speak.strip(), terms


# ---------- 主流程 ----------


def run_voice(
    client: LLMClient,
    transcript: str,
    *,
    model: str | None = None,
    style: str = "brief",
    max_chars: int = 220,
    temperature: float = 0.3,
) -> Iterator[dict]:
    """跑一次语音问答的文本侧编排，**逐事件 yield**。

    事件类型（延续阶段 10–16 的"一帧一行"协议，新增 `rewrite` / `speak` 两类）：

        start        转写文本、风格、模型
        answer_delta 口语化改写的流式增量（前端边收边显示）
        rewrite      改写完成：字数 + **残留 Markdown 计数**（确定性体检）
        speak        朗读稿 + 对照表 terms + **termCheck**（确定性核对对照表本身）
        finish       全部结果 + 耗时
        error        失败原因
    """
    started = time.perf_counter()
    buckets = {"rewrite": 0.0, "normalize": 0.0}

    text = _check_transcript(transcript)
    yield {
        "type": "start",
        "transcript": text,
        "style": style,
        "model": model or client.model,
        "maxChars": max_chars,
    }

    # 失败不抛穿生成器：生成器里抛异常只会断流，前端**连半截结果都看不到**。
    # 所以两个 LLM 阶段各自兜住异常 → 发一帧 error，再照常发 finish（带着已有的部分结果）。
    failure: str | None = None
    answer = ""
    speak = ""
    terms: list[dict] = []
    profile = spoken_profile("")
    check: dict = {"total": 0, "verified": 0, "suspect": []}

    # ---------- ① 口语化改写（流式：边想边念） ----------
    t0 = time.perf_counter()
    try:
        for piece in client.stream(
            _build_rewrite_messages(text, style, max_chars), model=model, temperature=temperature
        ):
            answer += piece
            yield {"type": "answer_delta", "text": piece}
    except Exception as e:  # noqa: BLE001
        failure = f"口语化改写失败：{e}"
    buckets["rewrite"] = round((time.perf_counter() - t0) * 1000, 2)

    answer = answer.strip()
    md = markdown_left(answer)
    yield {
        "type": "rewrite",
        "chars": len(answer),
        "markdownLeft": md,
        "ms": buckets["rewrite"],
    }

    # ---------- ② 朗读稿规范化 ----------
    if failure is None:
        t0 = time.perf_counter()
        try:
            speak, terms = _normalize(client, answer, model, temperature)
        except Exception as e:  # noqa: BLE001  保守降级：直接用口语稿念，绝不把内容弄丢
            failure = f"文本规范化失败：{e}"
            speak, terms = answer, []
        buckets["normalize"] = round((time.perf_counter() - t0) * 1000, 2)
        profile = spoken_profile(speak)
        check = audit_terms(answer, speak, terms)
        yield {
            "type": "speak",
            "speak": speak,
            "terms": terms,
            "termCheck": check,
            "plainChars": profile["plainChars"],
            "estSeconds": profile["estSeconds"],
            "ms": buckets["normalize"],
        }

    if failure is not None:
        yield {"type": "error", "message": failure}

    yield {
        "type": "finish",
        "transcript": text,
        "answer": answer,
        "speak": speak,
        "terms": terms,
        "termCheck": check,
        "style": style,
        "model": model or client.model,
        "markdownLeft": md,
        "plainChars": profile["plainChars"],
        "estSeconds": profile["estSeconds"],
        "times": dict(buckets),
        "totalMs": round((time.perf_counter() - started) * 1000, 2),
        "llmMs": round(buckets["rewrite"] + buckets["normalize"], 2),
    }


def run_voice_blocking(client: LLMClient, transcript: str, **kwargs: Any) -> dict:
    """把生成器跑完，收成一份完整结果（给非流式调用方用）。

    ⚠️ `error` 与部分结果**可以同时存在**：改写失败时 answer 可能是半截的，
    规范化失败时 speak 退回口语稿原文——这时 `error` 有值、其余字段仍有内容可看。
    """
    events = list(run_voice(client, transcript, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)
    err = next((e for e in events if e["type"] == "error"), None)

    base: dict[str, Any] = {
        "transcript": start.get("transcript") or (transcript or "").strip(),
        "style": (finish or {}).get("style") or start.get("style") or kwargs.get("style") or "brief",
        "model": (finish or {}).get("model") or start.get("model") or kwargs.get("model") or client.model,
    }
    if finish is None:
        return {
            **base,
            "answer": None,
            "speak": None,
            "terms": [],
            "termCheck": None,
            "markdownLeft": 0,
            "plainChars": 0,
            "estSeconds": 0.0,
            "times": {},
            "totalMs": 0.0,
            "llmMs": 0.0,
            "error": (err or {}).get("message", "未知错误"),
        }
    return {
        **base,
        "answer": finish.get("answer"),
        "speak": finish.get("speak"),
        "terms": finish.get("terms", []),
        "termCheck": finish.get("termCheck"),
        "markdownLeft": finish.get("markdownLeft", 0),
        "plainChars": finish.get("plainChars", 0),
        "estSeconds": finish.get("estSeconds", 0.0),
        "times": finish.get("times", {}),
        "totalMs": finish.get("totalMs", 0.0),
        "llmMs": finish.get("llmMs", 0.0),
        "error": (err or {}).get("message"),
    }
