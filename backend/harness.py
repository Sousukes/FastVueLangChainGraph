"""阶段 12 · Agent Harness 框架。

阶段 05 的 `agent.run_tool_loop`、阶段 10 的 `react.run_react`、阶段 11 的
`team.run_team`（每个 worker 又调一次 run_react）——这三处的**心跳循环**是同一段代码：

    模型决策 → 要么给答案，要么给 tool_calls
            → 执行工具，把结果回填进 messages
            → 再来一轮，直到答案或步数上限

本阶段把它抽成一个**薄、可复用**的 `Agent`：把「客户端 / 系统提示 / 工具面 /
步数 / 温度」打成一个对象。单智能体、多智能体的每个角色、甚至独立调试某个角色，
都只是 `Agent(...).run(question)`。

⭐ 为什么是「薄」而不是「大而全」
    阶段 11 已经证明：分工的实质是**工具面**，不是类名。所以框架只做一件事——
    把「一次带工具的对话」跑完并 emit 统一事件流；**不**引入新的配置树、不发明
    新的事件协议、不重写执行器。`run` 直接委托给 `react.run_react`（流式）、
    `run_blocking` 委托给 `react.run_react_blocking`（收成结果）——本身不写第二份循环。

可插拔点（呼应前面的阶段）：
    - 工具来源 ToolSource：本地注册表 vs MCP server（阶段 06）
    - 工具执行器 ToolExecutor：本地 vs 远程（阶段 05 的 executor 注入点演化版）
"""

from __future__ import annotations

from typing import Iterable

from llm import LLMClient

import react
from react import DEFAULT_GROUPS


class Agent:
    """一个可复用的智能体。

    它不重新实现循环——`run` 委托给 `react.run_react`（流式）、
    `run_blocking` 委托给 `react.run_react_blocking`（收成结果）。

    三种用法只是不同的构造参数：

        Agent(client)                                         # 单智能体（默认工具面）
        Agent(client, system=role.system, groups=role.groups) # 多智能体的一个角色
        Agent(client, groups=("rag", "graph"))                # 只想看检索角色怎么跑
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        system: str | None = None,
        groups: Iterable[str] = DEFAULT_GROUPS,
        max_steps: int = 6,
        temperature: float = 0.2,
        observation_limit: int = 1200,
    ) -> None:
        self.client = client
        self.system = system
        self.groups: list[str] = list(groups)
        self.max_steps = max_steps
        self.temperature = temperature
        self.observation_limit = observation_limit

    def run(self, question: str, *, model: str | None = None) -> Iterable[dict]:
        """流式跑，逐事件 yield（与 `react.run_react` 同协议，前端零改动）。"""
        yield from react.run_react(
            self.client,
            question,
            model=model,
            max_steps=self.max_steps,
            temperature=self.temperature,
            groups=self.groups,
            observation_limit=self.observation_limit,
            system=self.system,
        )

    def run_blocking(self, question: str, *, model: str | None = None) -> dict:
        """跑完收成一份结果（给 Planner / Critic / Writer 这类不需要流式的角色用）。"""
        return react.run_react_blocking(
            self.client,
            question,
            model=model,
            max_steps=self.max_steps,
            temperature=self.temperature,
            groups=self.groups,
            observation_limit=self.observation_limit,
            system=self.system,
        )

    @property
    def tools(self) -> list[str]:
        """这个 Agent 实际能用的工具——用来做「伪多智能体」自检（阶段 11）。"""
        return [s["function"]["name"] for s in react.build_schemas(self.groups)]


class ToolSource:
    """工具来源：本地注册表 vs MCP server（呼应阶段 06）。

    阶段 05 的 `tools` 注入点演化版——把「从哪拿工具说明书」和「怎么执行」分开。
    """

    def __init__(self, groups: Iterable[str] = DEFAULT_GROUPS) -> None:
        self.groups: list[str] = list(groups)

    def schemas(self) -> list[dict]:
        return react.build_schemas(self.groups)

    def executor(self):
        return react.make_executor(self.groups)


class ToolExecutor:
    """工具执行器：本地 vs 远程（MCP）。

    `(name, arguments) -> (result, error)`，和阶段 05 的 executor 签名一致。
    换 MCP 时只需把这里换成远程调用，循环本身一行不用动。
    """

    def __init__(self, groups: Iterable[str] = DEFAULT_GROUPS) -> None:
        self._fn = react.make_executor(groups)

    def __call__(self, name: str, arguments: str) -> tuple[object, str | None]:
        return self._fn(name, arguments)


# ---------- 角色预设（应用层配置，框架本身不绑定具体角色） ----------

# ⭐ 这一处就是「在框架之上配置角色」的示范：每个角色只是一组
#    (system, groups, max_steps, temperature)。多智能体（阶段 11）的角色
#    和这里单独跑的角色，是**同一段 Agent 代码**——区别只在构造参数。
PRESETS: dict[str, dict] = {
    "researcher": {
        "label": "检索员",
        "groups": ("rag", "graph"),
        "system": (
            "你是检索员，只负责查资料，不负责计算或下结论。\n"
            "你能用的工具只有检索与知识图谱。查到之后把**原文片段和出处**交出来，\n"
            "不要自己总结成最终答案——结论交给主笔。\n"
            "引用时务必写出 source（如 07-RAG基础#25）。"
        ),
        "maxSteps": 4,
        "temperature": 0.2,
        "description": "只能检索 + 查图谱，适合「某概念是什么 / 属于哪个阶段」",
    },
    "analyst": {
        "label": "分析员",
        "groups": ("local",),
        "system": (
            "你是分析员，只负责本地计算与推理，不负责查资料。\n"
            "你能用的工具只有计算器和几个本地小工具。需要外部事实时，\n"
            "直接说明「这需要查资料，我算不了」——不要编数字。\n"
            "给出计算过程和结果即可，不要写长篇结论。"
        ),
        "maxSteps": 4,
        "temperature": 0.1,
        "description": "只能本地计算，适合「算一下 / 单位换算」",
    },
    "pure": {
        "label": "纯净助手",
        "groups": (),
        "system": "你是一个直接回答问题的助手，不使用任何工具，凭常识简洁作答。",
        "maxSteps": 1,
        "temperature": 0.3,
        "description": "无工具，用来演示「工具面为空时框架退化为纯对话」",
    },
}


def role_catalog() -> dict:
    """给 `/api/harness/roles` 用的目录：每个预设角色 + 它实际能用的工具面。"""
    roles = []
    for name, cfg in PRESETS.items():
        roles.append(
            {
                "name": name,
                "label": cfg["label"],
                "groups": list(cfg["groups"]),
                "tools": [s["function"]["name"] for s in react.build_schemas(cfg["groups"])],
                "maxSteps": cfg["maxSteps"],
                "temperature": cfg["temperature"],
                "description": cfg["description"],
            }
        )
    return {"roles": roles, "groups": list(react.ALL_GROUPS)}
