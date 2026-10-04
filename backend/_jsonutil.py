"""结构化输出的公共件：剥 fence、错误提示、JSON 自纠、工具参数解析。

## 这段代码就是阶段 04 讲的原理

阶段 04 第一次要「让模型输出结构化 JSON」时，只能手写三件事：
剥掉模型爱加的 ```` ```json ```` 围栏、把 Pydantic 的报错压成一句模型看得懂的话、
校验失败把报错递回去让它自纠一次。当时刻意没抽公共函数（**先把原理讲明白**）。

后续 11/13/14/15/16/17 每个阶段都要这套招式，于是同一段逻辑被复制了 6 份。
副本之间**确实漂移出真实差异**（`validation_hint` 三种文案、`safe_args` 两种语义），
也有**只是写法不同但行为等价**的（`strip_fence`），所以阶段 18B 收尾时下沉到本文件。

## 为什么早先不抽（保留这段历史，因为它是个真实的工程判断）

阶段 12 立过一条规矩：**「第三次才抽框架」** —— 前两次手写是为了讲清原理，
第三次才说明确实该抽了。但 14/15/16/17 各自推进时都判断「收益不抵风险」，
于是 `research.py` / `voice.py` 的文件头把这件事记成了 `TECH_DEBT` 账。

为什么不早抽？两个真实成本，不是矫情：
1. `_json_call` 被 6 个阶段各自的**静态测试**（`_t_*_static.py` 的 `ScriptedClient`）间接覆盖，
   改签名会波及它们。
2. 各阶段的**自纠提示语不一样**（见下），不是纯副本。

现在这些成本都解决了：差异被显式参数化而不是抹平，静态测试只走公开 API。

## 四个函数的真实差异（合并时**故意保留**，不是遗漏）

| 函数 | 差异 |
|---|---|
| `strip_fence` | 阶段 11（team）用 `rsplit`，其余 5 份用切片 `[:-3]`。\n**实测穷举后确认：两版在所有常见输入上结果相同，唯一差异是失败方式不同**，\n不是真 bug。统一取 team 版只为消除写法分叉。 |
| `validation_hint` | 3 种风格：标准版（4 份）/ 阶段 11 取前 4 条且附原值 / 阶段 04 用 `;` 连接且标类型。\n风格会改变**给模型看的提示词**，故用 `style` 参数保留。 |
| `json_call` | 自纠提示语 2 种（阶段 11 一套、其余一套），另有阶段 11 会**吞掉首次异常**的差异。\n故用 `retry_prompt` 参数保留。 |
| `safe_args` | **两种语义，不能合并**：阶段 10/12 解析失败**返回原文**（仅用于 trace 展示）；\n阶段 18 解析失败**返回 `{}`** 且捕获所有异常（沙箱安全 —— 失败结果要拿去执行）。\n故拆成 `safe_args`（宽松/展示用）与 `safe_args_dict`（严格/执行用）。 |

⚠️ 改这四个函数前先读上面那张表；`_safe_args` 那条尤其别想当然合并成一份。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from llm import LLMClient

__all__ = [
    "strip_fence",
    "validation_hint",
    "json_call",
    "safe_args",
    "safe_args_dict",
    "HintStyle",
    "RetryStyle",
]


# ---------- 1. 剥模型爱加的 ``` 围栏 ----------


def strip_fence(text: str) -> str:
    """剥掉模型爱加的 ```` ```json ... ``` ```` 围栏。

    ⚠️ 这里原本有一处 5 份拷贝 vs 1 份的写法差异（`team.py` 用 `rsplit`，
    其余用切片 `[:-3]`）。**已实测穷举比对，结论是：两版在所有常见输入上结果完全相同**
    （正常围栏 / 尾随换行 / 闭合后有字 / 两个闭合围栏 / 无围栏 / 只有开围栏 —— 全部 same），
    唯一差异是「带前言」场景下失败方式不同，**两版都解析失败**。
    所以这不是一处真 bug，`rsplit` 也不比切片更健壮。统一取 team 版只为消除写法分叉。

    **已知边界（别指望这个函数解决）**：模型在闭合围栏之后补说明文字
    （"```\\n以上是 JSON"）时，本函数无能为力 —— 那需要正则截取首个完整 JSON 对象，
    属于另一个问题，本阶段各版本都没处理，故此处也不假装能处理。
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    return t.strip()


# ---------- 2. 把 Pydantic 报错压成模型看得懂的话 ----------

HintStyle = str
"""三种提示风格（都指向 `_hint_impl` 的分支）：

- ``"std"``   —— 标准版：`字段 a.b：msg`（阶段 11/13/14/15/16/17 通用）
- ``"team"``  —— 阶段 11 版：只取前 4 条，并附上模型给错的原值
- ``"extract"`` —— 阶段 04 版：用 ``;`` 连接，附期望类型
"""

_RETRY_FALLBACK = "- 输出不是合法 JSON：{err}"


def _hint_impl(err: ValidationError, style: HintStyle = "std") -> str:
    """把 Pydantic 的报错压成一句模型看得懂的话。

    直接把 ValidationError 原样塞回去有两大问题：一是很长（几十行），
    二是全是 Python 术语（"field required" 模型不一定当回事）。
    """
    if style == "team":
        # 阶段 11：只给前 4 条（免得提示词过长），并附上你给的值
        lines = []
        for e in err.errors()[:4]:
            loc = ".".join(str(x) for x in e["loc"])
            lines.append(f"- 字段 {loc}：{e['msg']}（你给的值是 {e.get('input')!r}）")
        return "\n".join(lines)

    if style == "extract":
        # 阶段 04：用分号连接成一行，并标出期望类型
        parts: list[str] = []
        for e in err.errors():
            loc = ".".join(str(x) for x in e["loc"])
            parts.append(f"{loc}: {e['msg']}（期望类型 {e['type']}）")
        return "; ".join(parts)

    # "std"：标准版
    return "\n".join(
        f"- 字段 {'.'.join(str(x) for x in e['loc'])}：{e['msg']}" for e in err.errors()
    )


def validation_hint(err: ValidationError, style: HintStyle = "std") -> str:
    """对外入口。`style` 见 `HintStyle` 说明。"""
    return _hint_impl(err, style)


# ---------- 3. JSON 模式 + 校验 + 一次自纠 ----------

RetryStyle = str
"""两种自纠提示语：

- ``"std"``   —— 「你的输出没能通过校验…请严格按下面的 JSON 重来一次」（阶段 13/14/15/16/17）
- ``"team"``  —— 「你上一次的输出没能通过校验…请按下面的结构重新输出」（阶段 11）
"""


def _retry_prompt(hint: str, schema_hint: str, style: RetryStyle) -> str:
    if style == "team":
        return (
            f"你上一次的输出没能通过校验：\n{hint}\n\n"
            f"请按下面的结构重新输出（只输出 JSON）：\n{schema_hint}"
        )
    return (
        f"你的输出没能通过校验：\n{hint}\n\n"
        f"请严格按下面的 JSON 重来一次，不要加解释：\n{schema_hint}"
    )


def json_call(
    client: LLMClient,
    messages: list[dict],
    model: str | None,
    temperature: float,
    validator: type[BaseModel],
    schema_hint: str,
    *,
    hint_style: HintStyle = "std",
    retry_style: RetryStyle = "std",
) -> tuple[BaseModel | None, str]:
    """JSON 模式 + Pydantic 校验 + 一次自纠重试，返回 (对象 | None, 原文)。

    失败（两次都没通过校验）时返回 `(None, 原文)` —— 调用方据此决定降级还是报错。
    """
    try:
        raw = client.chat(
            messages, model=model, temperature=temperature, response_format={"type": "json_object"}
        )
    except Exception:  # noqa: BLE001  某些厂商不认 response_format，直接降级
        raw = client.chat(messages, model=model, temperature=temperature)

    for attempt in (1, 2):
        try:
            return validator.model_validate_json(strip_fence(raw)), raw
        except (ValidationError, json.JSONDecodeError) as e:
            # 实测（Pydantic 2）：非法 JSON 也抛 **ValidationError**（type=json_invalid），
            # 不是 json.JSONDecodeError。所以下面那个 JSON 分支在 Pydantic 2 下几乎走不到
            # —— 保留它只是为了兼容「strip_fence 后 json 能过、但 schema 不过」等边角情况，
            # 与重构前各阶段的行为完全一致（没趁机改语义）。
            if attempt == 2:
                return None, raw
            hint = (
                _hint_impl(e, hint_style)
                if isinstance(e, ValidationError)
                else _RETRY_FALLBACK.format(err=e)
            )
            # 自纠：把报错和原文一起递回去，让它照着改
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _retry_prompt(hint, schema_hint, retry_style)},
            ]
            raw = client.chat(messages, model=model, temperature=temperature)
    return None, raw  # pragma: no cover


# ---------- 4. 工具调用参数解析（两种语义，别合并） ----------


def safe_args(raw: str) -> Any:
    """宽松版：解析失败**返回原文**。

    仅用于 trace / 可观测性面板展示 —— 那里宁可显示模型吐的原始坏 JSON，
    也不要悄悄变成空参数（丢了"模型本来想说什么"这个线索）。
    阶段 10（agent）与阶段 12（react）用它。
    """
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw


def safe_args_dict(raw: str) -> dict:
    """严格版：解析失败**返回 `{}`**，且捕获所有异常。

    用于**要拿去执行**的参数（阶段 18 的沙箱工具调用）：坏参数绝不能当字符串
    往沙箱里塞，必须退化成空参数（顶多少做一步，绝不能崩或执行错的东西）。
    """
    try:
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
