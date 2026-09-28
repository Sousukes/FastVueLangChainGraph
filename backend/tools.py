"""阶段 05 · 工具注册表。

函数调用（Function Calling）最容易误解的一点：**模型从不执行任何代码**。
它只做两件事——
    1. 读我们给出的"工具说明书"（name + description + parameters JSON Schema）
    2. 需要时产出一个"调用意图"：`{"name": "...", "arguments": "{...}"}`
真正执行的是本文件里的 Python 函数，执行结果再由 agent 循环喂回模型。

因此本文件承担三件事：
    - 用 Pydantic 定义每个工具的参数（顺带自动生成 JSON Schema，见 tools_schema）
    - 执行工具（参数先过 Pydantic 校验，非法参数不进入函数体）
    - 任何异常都转成"可读的错误文本"返回，而不是让请求崩掉——让模型自己决定怎么处理
"""

from __future__ import annotations

import ast
import json
import operator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError


# ---------- 工具参数：Pydantic 模型即 JSON Schema 的唯一来源 ----------


class CalculatorArgs(BaseModel):
    """安全的四则运算。"""

    expression: str = Field(
        description="要计算的算术表达式，只支持数字与 + - * / // % ** 和括号，例如 (12+8)*3/4"
    )


class SearchNotesArgs(BaseModel):
    """在课程笔记里做关键词检索。"""

    query: str = Field(description="检索关键词，例如 RAG、流式、函数调用")
    limit: int = Field(default=3, ge=1, le=5, description="返回条数上限")


class WeatherArgs(BaseModel):
    """查询城市天气（演示用的静态数据）。"""

    city: str = Field(description="城市名，例如 北京、上海、深圳")
    unit: Literal["celsius", "fahrenheit"] = Field(
        default="celsius", description="温度单位：celsius 或 fahrenheit"
    )


class TimeArgs(BaseModel):
    """查询某个时区的当前时间。"""

    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区名，例如 Asia/Shanghai")


# ---------- 工具实现 ----------


# 只允许这些 AST 节点 → 杜绝 eval("__import__('os').system(...)") 这类注入
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的字面量：{node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"不支持的表达式结构：{type(node).__name__}")


def tool_calculator(expression: str) -> dict:
    """注意：这里刻意不用 eval()——模型给的参数永远当作"不可信输入"。"""
    tree = ast.parse(expression, mode="eval")
    return {"expression": expression, "value": _eval_node(tree)}


_NOTES = [
    ("流式", "SSE 用 `data: ...\\n\\n` 分帧；前端用 fetch + ReadableStream 手动切帧，"
             "TextDecoder 必须带 {stream:true}，否则跨块中文会乱码。"),
    ("函数调用", "模型不执行代码，只产出 tool_calls（name + arguments）；"
                 "执行、参数校验、错误回传都由我们自己的代码完成。"),
    ("RAG", "检索增强：先把文档切块、向量化入库，提问时召回最相关的若干块塞进 prompt。"
            "ChromaDB 常作本地向量库。"),
    ("Prompt", "角色设定 / 少样本 / 结构化约束是三种最常用的 Prompt 技法。"),
    ("结构化输出", "response_format=json_object 只保证 JSON 合法，字段与类型合规要靠 Pydantic 校验。"),
    ("多轮对话", "HTTP 无状态：多轮 = 每轮把整段 messages 历史重新发一遍。"),
    ("MCP", "Model Context Protocol：用 JSON-RPC 2.0 把外部能力标准化接入 LLM 应用。"
            "三大原语 Tools（可调用）/ Resources（可读取）/ Prompts（可复用）；"
            "传输可用 stdio 或 Streamable HTTP；Host 是应用本体、Client 负责连接、Server 提供能力。"),
]


def tool_search_notes(query: str, limit: int = 3) -> dict:
    hits = [(t, s) for t, s in _NOTES if query in t or query in s]
    if not hits:  # 退化为宽匹配，避免"检索不到就什么也不给"
        hits = [(t, s) for t, s in _NOTES if any(ch in t or ch in s for ch in query)]
    return {
        "query": query,
        "total": len(hits),
        "items": [{"title": t, "snippet": s} for t, s in hits[:limit]],
    }


def all_notes() -> list[tuple[str, str]]:
    """只读访问器：阶段 06 的 MCP server 把笔记整包作为 Resource 暴露出去。"""
    return list(_NOTES)


_WEATHER = {
    "北京": (26, "晴"),
    "上海": (29, "多云"),
    "深圳": (32, "阵雨"),
    "杭州": (28, "阴"),
}


def tool_get_weather(city: str, unit: str = "celsius") -> dict:
    if city not in _WEATHER:
        # 业务错误：不是异常，而是"工具回答了但它答不了"——交给模型向用户解释
        raise ValueError(f"没有 {city} 的天气数据，目前仅支持：{'、'.join(_WEATHER)}")
    temp, desc = _WEATHER[city]
    if unit == "fahrenheit":
        temp = round(temp * 9 / 5 + 32)
    return {"city": city, "temperature": temp, "unit": unit, "description": desc}


def tool_get_current_time(timezone: str = "Asia/Shanghai") -> dict:
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as e:
        # 真机踩坑：Windows 没有系统时区库，zoneinfo 会找不到任何时区
        # （连 "UTC" 都找不到，报错却长得像"时区名写错了"，极易误导）。
        # 依赖 tzdata 包（见 pyproject.toml），这里把原因说清楚。
        raise ValueError(
            f"取不到时区 {timezone}：运行环境缺少时区数据库。"
            "Windows 上需安装 tzdata（uv add tzdata）后重启服务。"
        ) from e
    now = datetime.now(tz)
    return {
        "timezone": timezone,
        "iso": now.isoformat(timespec="seconds"),
        "weekday": now.strftime("%A"),
    }


# ---------- 注册表 ----------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., Any]


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("calculator", "计算一个算术表达式，返回精确数值。适合做数学运算。", CalculatorArgs, tool_calculator),
        Tool("search_notes", "在课程笔记里检索关键词，返回相关片段。适合回答课程内容相关问题。", SearchNotesArgs, tool_search_notes),
        Tool("get_weather", "查询城市当前天气（演示用静态数据）。", WeatherArgs, tool_get_weather),
        Tool("get_current_time", "查询指定时区的当前时间。", TimeArgs, tool_get_current_time),
    ]
}


def tools_schema() -> list[dict]:
    """给模型的"工具说明书"：Pydantic 模型直接编译成 JSON Schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.args_model.model_json_schema(),
            },
        }
        for t in TOOLS.values()
    ]


def execute_tool(name: str, raw_arguments: str) -> tuple[Any, str | None]:
    """执行一次工具调用。

    返回 `(result, error)`：
      - 成功：result 是工具返回值，error 为 None
      - 失败：result 为 None，error 是可读文本（会被原样作为 tool 消息回给模型）

    失败绝不抛异常到上层：让模型看到"参数错了 / 城市不支持"，它才有机会自纠或向用户解释。
    """
    tool = TOOLS.get(name)
    if tool is None:
        return None, f"未知工具 {name}。可用工具：{', '.join(TOOLS)}"

    try:
        raw = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as e:
        return None, f"arguments 不是合法 JSON：{e}（原始内容：{raw_arguments!r}）"

    try:
        args = tool.args_model.model_validate(raw)  # 参数校验：非法参数不进入函数体
    except ValidationError as e:
        hints = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        return None, f"参数不符合 schema：{hints}"

    try:
        return tool.fn(**args.model_dump()), None
    except Exception as e:  # 工具自身的业务错误
        return None, f"{type(e).__name__}: {e}"
