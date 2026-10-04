"""阶段 04 · 结构化抽取引擎。

三条核心约定，对应教程「核心概念」一节：

1. **JSON 模式 ≠ 合规保证**：`response_format={"type":"json_object"}` 只是让模型倾向
   输出合法 JSON，并不能保证字段、类型、枚举值符合我们的 schema。所以必须做运行时校验。
2. **校验不过就让它自己改**：把校验失败的具体原因（哪个字段、什么类型错了）作为 user
   消息追加回去，让模型带着错误信息再答一次。这是 LLM 工程里常说的 *self-correction*。
3. **若 JSON 模式不被支持，降级为自由生成**：厂商差异是真实的工程问题，宁可拿到一段
   普通文本再让 Pydantic 兜底校验，也不要直接把请求挂掉。
"""

from __future__ import annotations

from typing import Any, Literal, Optional, get_args

from openai import BadRequestError  # noqa: F401  # 仅用于 isinstance 兜底检测
from pydantic import BaseModel, ValidationError, create_model

from _jsonutil import validation_hint as _validation_hint
from llm import LLMClient
from schemas import ExtractRequest, ExtractResponse, FieldSpec


# ---------- 1. 字段定义 → 动态 Pydantic 模型 ----------

_PRIMITIVE = {
    "string": str,
    "int": int,
    "number": float,
    "bool": bool,
    "array": list[str],  # 教学版：数组元素统一按字符串处理
}


def _annotation(spec: FieldSpec):
    if spec.type == "string" and spec.enum:
        # Python 3.11+ 支持 Literal[*values]；不可解包时退回元组形式
        return Literal[tuple(spec.enum)]  # type: ignore[valid-type]
    return _PRIMITIVE.get(spec.type, str)


def build_model(specs: list[FieldSpec]) -> type[BaseModel]:
    """把前端给的字段定义动态编译成一个 BaseModel。

    Pydantic v2 默认 `extra='ignore'`，模型偶尔塞冗余键也不会报错——这正是我们
    想要的"宽容输入、严格校验声明字段"。
    """
    fields: dict[str, Any] = {}
    for spec in specs:
        ann = _annotation(spec)
        if spec.required:
            # Ellipsis 是 Pydantic v2 的"必填"标记
            fields[spec.name] = (ann, ...)
        else:
            fields[spec.name] = (Optional[ann], None)
    return create_model("Extraction", **fields)


# ---------- 2. 抽取 + 校验 + 自纠 ----------


def _build_messages(text: str, specs: list[FieldSpec]) -> list[dict]:
    lines = ["你是信息抽取引擎。严格按下方 schema 从文本里抽取字段，并只输出 JSON（不要任何解释、不要 markdown 代码块）。\n字段："]
    for s in specs:
        opt = "必填" if s.required else "可选"
        type_str = s.type + (f"(enum: {','.join(s.enum)})" if s.enum and s.type == "string" else "")
        lines.append(f"- {s.name} ({type_str}, {opt}): {s.description or '—'}")
    lines.append("\n输出 JSON 键名严格等于字段名；缺值填 null。\n")
    lines.append("----- 文本开始 -----\n" + text.strip() + "\n----- 文本结束 -----")
    return [{"role": "user", "content": "\n".join(lines)}]


def extract_one(client: LLMClient, req: ExtractRequest) -> ExtractResponse:
    model_cls = build_model(req.fields)
    messages = _build_messages(req.text, req.fields)
    warning: str | None = None
    last_raw = ""
    error_msg: str | None = None

    for attempt in (1, 2):
        try:
            raw = client.chat(
                messages,
                model=req.model,
                temperature=0.2,  # 结构化任务降低温度
                response_format={"type": "json_object"},
            )
        except BadRequestError as e:
            # 厂商差异：有的厂商不支持 json_object 模式。降级为自由生成，
            # 但要在响应里留下 warning，让用户知道"合规保障降了一档"。
            msg = str(e) + " " + str(getattr(e, "body", "") or "")
            if "response_format" not in msg and "json_object" not in msg:
                raise  # 真不是格式问题，让上层 502
            raw = client.chat(messages, model=req.model, temperature=0.2)
            warning = "当前模型不支持 JSON 模式，已降级为自由生成（校验仍会执行）"

        last_raw = raw
        try:
            parsed = model_cls.model_validate_json(raw)
        except ValidationError as ve:
            error_msg = _validation_hint(ve, style="extract")
            # 第二次再答时，把"上次原文 + 错误原因"塞回去，让模型自纠
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "你的上一次响应未通过 schema 校验：\n"
                        f"{error_msg}\n请修正并只输出合法 JSON（仍然不要任何解释或代码块）。"
                    ),
                },
            ]
            continue

        return ExtractResponse(
            data=parsed.model_dump(),
            raw=raw,
            attempts=attempt,
            valid=True,
            model=req.model or client.model,
            warning=warning,
        )

    return ExtractResponse(
        data=None,
        raw=last_raw,
        attempts=2,
        valid=False,
        model=req.model or client.model,
        error=error_msg,
        warning=warning,
    )