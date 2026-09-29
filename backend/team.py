"""阶段 11 · 多智能体（Multi-agent）。

阶段 10 的 ReAct 是「一个全才」：一个智能体、一套工具、一个循环、一个提示词。
本阶段把它拆成**几个专职角色**，由一个编排器（orchestrator）串起来：

    用户问题
       │
       ▼
    ┌─────────┐   JSON 任务列表        ┌────────────┐
    │ Planner │ ─────────────────────▶ │ 拓扑分层    │
    │（无工具）│                        │ 同层并行    │
    └─────────┘                        └─────┬──────┘
                                             │ 每个任务派给对应角色
                            ┌────────────────┼────────────────┐
                            ▼                ▼                ▼
                      ┌──────────┐    ┌──────────┐    ┌──────────┐
                      │Researcher│    │ Analyst  │    │   ...    │
                      │检索工具面 │    │计算工具面 │    └──────────┘
                      └─────┬────┘    └─────┬────┘
                            │ 结论 + 证据    │
                            └────────┬───────┘
                                     ▼
                              ┌──────────┐  JSON 评审
                              │  Critic  │ ────────────┐
                              │（无工具） │             │
                              └──────────┘             ▼
                                              ┌──────────────┐
                                              │ Writer（流式）│
                                              └──────┬───────┘
                                                     ▼
                                                  最终答案

⭐ 但请先接受一个反直觉的结论

    **多智能体并不比单智能体"更聪明"**，它只是**更可控、更可观测**。
    代价是**耗时按角色数线性增长**（实测见教程 §5：单智能体 6s 量级 →
    多智能体 20s 量级）。所以本阶段真正要回答的问题不是"怎么搭多智能体"，
    而是「**什么时候值得多付这几倍的时间**」。

⭐ 分工要真实生效，必须同时做到三件事（缺一件就是"伪多智能体"）

    1. **工具面不同**：researcher 只能检索、analyst 只能计算、critic 没有工具。
       如果所有角色拿的是同一套工具，它们会退化成"同一个智能体跑 N 遍"——
       角色名写得很漂亮，实际什么也没分。这是多智能体最常见的自欺。
    2. **上下文不共享**：角色之间只传「结论 + 证据」，不传彼此的完整对话。
       否则上下文按角色数线性膨胀（每多一个角色就多一份完整历史）。
    3. **交接是结构化的**：Planner 和 Critic 的输出走 JSON + Pydantic 校验，
       而不是让模型自由发挥一段话、我们再拿正则去抠。

⚠️ 关于线程

    阶段 10 说过"能自己拆开的流程就别用线程去绕"。本阶段**恰恰相反**：
    多个 worker 天然是同时跑的独立流程，谁也不知道谁什么时候产出一帧，
    所以必须回到"后台线程 + queue.Queue + 哨兵"（阶段 09 的那一套）。
    **判据是"这个流程本身是不是并发的"，不是"能不能用生成器拆开"。**
"""

from __future__ import annotations

import json
import logging
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterator

from llm import LLMClient
from pydantic import BaseModel, Field, ValidationError

import react
from react import GROUP_GRAPH, GROUP_LOCAL, GROUP_RAG

# 阶段 12：worker 的循环统一交给 harness.Agent 跑（单一真相源，见 harness.py）
from harness import Agent

logger = logging.getLogger(__name__)

# ---------- 角色 ----------

ROLE_PLANNER = "planner"
ROLE_RESEARCHER = "researcher"
ROLE_ANALYST = "analyst"
ROLE_CRITIC = "critic"
ROLE_WRITER = "writer"

# 任务种类 → 由哪个角色接。这就是"分工"落地的那一处。
KIND_ROLE: dict[str, str] = {"research": ROLE_RESEARCHER, "compute": ROLE_ANALYST}


@dataclass(frozen=True)
class Role:
    """一个专职角色。

    ⭐ `groups` 才是分工的**实质**，`system` 只是让它更愿意用对工具。
    把两个角色的 `groups` 写成一样，它们就是同一个智能体。
    """

    name: str
    label: str
    system: str
    groups: tuple[str, ...]
    max_steps: int = 1
    temperature: float = 0.2


PLANNER_SYSTEM = (
    "你是任务规划员。把用户的问题拆成若干个**可并行**的子任务。\n"
    "规则：\n"
    "- 每个子任务的 kind 只能是两种：\n"
    "    research —— 需要在课程文档或知识图谱里查证的事实；\n"
    "    compute  —— 纯计算（不需要查资料）。\n"
    "- 只有「必须用到另一个任务的结果」时才写 dependsOn，否则留空。\n"
    "  **留空的任务会被并行执行**——能并行的别写成串行，这是多智能体唯一的速度来源。\n"
    "- 最多 4 个子任务。**一个任务能解决的就不要拆**——每多一个角色就多一次完整调用。\n"
    "- 只输出 JSON，不要任何解释文字。"
)

RESEARCHER_SYSTEM = (
    "你是资料研究员，**只负责查证，不负责计算和最终行文**。\n"
    "你能用的工具只有课程检索和知识图谱。\n"
    "\n"
    "纪律：\n"
    "- 每一轮调用工具之前先用一两句话写清楚你要查什么、为什么。\n"
    "- **任何事实都必须先检索再下结论**，不要凭记忆回答。\n"
    "- 不要重复调用相同参数的工具。\n"
    "- 结论里必须带上出处（形如 `07-RAG基础#25`）。\n"
    "- 查不到就直说查不到，**不要用模糊的话糊过去**——下游的评审员会检查你。\n"
    "- 你只输出这一条任务的结论，不要替别的任务下判断。"
)

ANALYST_SYSTEM = (
    "你是数据分析员，**只用本地工具，不查课程知识库和知识图谱**。\n"
    "你能用的工具是计算器、本地笔记检索、时间、天气等——都是本地能力，\n"
    "没有课程检索（search_course_docs）和知识图谱（query_knowledge_graph）。\n"
    "\n"
    "纪律：\n"
    "- 每一轮调用工具之前先用一两句话写清楚你要算什么。\n"
    "- 需要课程资料时，直接说明「这需要查知识库，我查不了，交给 researcher」——不要编数字。\n"
    "- 前置任务给你的结论可以直接使用，不要重新推导。\n"
    "- 给出计算过程和最终结果。"
)

CRITIC_SYSTEM = (
    "你是评审员，**你没有工具，只能根据手上的材料做判断**。\n"
    "你会拿到：每个子任务的结论 + 该任务实际调用过的工具（即证据）。\n"
    "\n"
    "你的职责是挑出**没有证据支撑**的断言：\n"
    "- 结论里出现具体数字、参数名、阶段编号，但证据里没有任何一次检索 → 记一条问题。\n"
    "- 结论含糊其辞（「大概是」「应该是」）→ 记一条问题。\n"
    "- 结论与证据明显矛盾 → 记一条问题。\n"
    "\n"
    "⚠️ 不要为了显得严格而挑刺：**证据充分就判 pass**。\n"
    "⚠️ 也不要脑补证据：你没看到的就是没看到，不能因为“它大概对”就放过。\n"
    "只输出 JSON，不要任何解释文字。"
)

WRITER_SYSTEM = (
    "你是主笔，负责把各路结论汇总成一份给用户的最终答案。\n"
    "你会拿到：用户的原始问题 + 各子任务的结论 + 评审员的意见。\n"
    "\n"
    "纪律：\n"
    "- **评审员标记为无证据的结论，要么补一句“此点缺少检索支撑”，要么直接不用**——\n"
    "  不要原样写进答案，那等于把幻觉洗白成正文。\n"
    "- 答案要直接回应用户的问题，不要复述流程。\n"
    "- 引用出处（形如 `07-RAG基础#25`）。\n"
    "- 用中文，简洁。"
)

ROLES: dict[str, Role] = {
    ROLE_PLANNER: Role(
        name=ROLE_PLANNER,
        label="规划员",
        system=PLANNER_SYSTEM,
        groups=(),           # ⭐ 没有工具——它只做拆解，不该被工具分心
        max_steps=1,
        temperature=0.1,
    ),
    ROLE_RESEARCHER: Role(
        name=ROLE_RESEARCHER,
        label="研究员",
        system=RESEARCHER_SYSTEM,
        groups=(GROUP_RAG, GROUP_GRAPH),   # ⭐ 只能检索
        max_steps=3,
        temperature=0.2,
    ),
    ROLE_ANALYST: Role(
        name=ROLE_ANALYST,
        label="分析员",
        system=ANALYST_SYSTEM,
        groups=(GROUP_LOCAL,),             # ⭐ 只能计算
        max_steps=3,
        temperature=0.1,
    ),
    ROLE_CRITIC: Role(
        name=ROLE_CRITIC,
        label="评审员",
        system=CRITIC_SYSTEM,
        groups=(),           # ⭐ 没有工具——它只能就材料论材料
        max_steps=1,
        temperature=0.1,
    ),
    ROLE_WRITER: Role(
        name=ROLE_WRITER,
        label="主笔",
        system=WRITER_SYSTEM,
        groups=(),
        max_steps=1,
        temperature=0.3,
    ),
}

# 派工顺序（也用于前端展示角色清单）
ROLE_ORDER = (ROLE_PLANNER, ROLE_RESEARCHER, ROLE_ANALYST, ROLE_CRITIC, ROLE_WRITER)


def role_catalog() -> dict:
    """给 `GET /api/team/roles` 用：把每个角色的**工具面**摊开给前端看。

    ⭐ 工具面必须显示出来。分工成不成立，一眼就能从这个清单看出来——
    如果两个角色的 tools 是一模一样的，那就是伪多智能体。
    """
    out = []
    for name in ROLE_ORDER:
        role = ROLES[name]
        schemas = react.build_schemas(role.groups)
        out.append(
            {
                "name": role.name,
                "label": role.label,
                "groups": list(role.groups),
                "tools": [s["function"]["name"] for s in schemas],
                "maxSteps": role.max_steps,
                "temperature": role.temperature,
                "kinds": [k for k, r in KIND_ROLE.items() if r == role.name],
            }
        )
    return {"roles": out, "kinds": list(KIND_ROLE)}


# ---------- 结构化交接：Planner / Critic 的输出 ----------


class TaskItem(BaseModel):
    """一个子任务。

    `dependsOn` 是调度器唯一关心的字段：**它决定了哪些任务能并行**。
    """

    id: str = Field(description="任务编号，如 t1 / t2")
    kind: str = Field(description="research（查资料）或 compute（纯计算）")
    description: str = Field(description="这个任务要做什么，写给接手的人看")
    dependsOn: list[str] = Field(default_factory=list, description="依赖的任务编号；没有就留空")


class PlanModel(BaseModel):
    tasks: list[TaskItem]


class CriticIssue(BaseModel):
    taskId: str = Field(description="出问题的任务编号")
    problem: str = Field(description="具体是什么问题")


class CriticReport(BaseModel):
    verdict: str = Field(description="pass 或 revise")
    issues: list[CriticIssue] = Field(default_factory=list)


def _validation_hint(err: ValidationError) -> str:
    """把 Pydantic 的报错压成一句模型看得懂的话（沿用阶段 04 的做法）。

    直接把 ValidationError 原样塞回去有两大问题：一是很长（几十行），
    二是全是 Python 术语（"field required" 模型不一定当回事）。
    """
    lines = []
    for e in err.errors()[:4]:
        loc = ".".join(str(x) for x in e["loc"])
        lines.append(f"- 字段 {loc}：{e['msg']}（你给的值是 {e.get('input')!r}）")
    return "\n".join(lines)


def _chat_json(
    client: LLMClient,
    messages: list[dict],
    model: str | None,
    temperature: float,
    validator: type[BaseModel],
    schema_hint: str,
) -> tuple[BaseModel | None, str]:
    """JSON 模式 + Pydantic 校验 + **一次自纠重试**。

    返回 (校验通过的对象, 原始文本)。失败时返回 (None, 文本)。

    ⭐ 为什么必须自己收口成结构化对象：
    多智能体的交接点一多，任何一个"模型自由发挥了一段话"的环节
    都会变成下游的解析泥潭。**角色之间的接口要像函数签名一样硬。**
    """
    raw = ""
    # 第一次：带 response_format
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
            # 自纠：把报错和原文一起递回去，让它照着改
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"你上一次的输出没能通过校验：\n{hint}\n\n"
                    f"请按下面的结构重新输出（只输出 JSON）：\n{schema_hint}",
                },
            ]
            raw = client.chat(messages, model=model, temperature=temperature)
    return None, raw


def _strip_fence(text: str) -> str:
    """模型很喜欢用 ```json ... ``` 把 JSON 包起来，去掉它。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    return t.strip()


PLAN_SCHEMA_HINT = (
    '{"tasks": [{"id": "t1", "kind": "research", "description": "...", "dependsOn": []}]}'
)
CRITIC_SCHEMA_HINT = (
    '{"verdict": "pass", "issues": [{"taskId": "t1", "problem": "..."}]}'
)


# ---------- 拓扑分层：并行性的唯一来源 ----------


def _layers(tasks: list[TaskItem]) -> list[list[TaskItem]]:
    """按 `dependsOn` 把任务分成若干层，**同层之间没有依赖**。

    ⭐ 这是多智能体**唯一**的速度来源：同层并行、跨层串行。
    如果所有任务都写满了 dependsOn，分层就退化成一条链，
    多智能体的耗时 = 单智能体 × 角色数，而且一点并行红利都没有。

    ⚠️ 循环依赖（t1 依赖 t2、t2 依赖 t1）会被**直接丢弃**——
    这里不做环检测，因为 Planner 是我们自己的，与其写一套环检测，
    不如在提示词里约束它。真出现环时，这些任务会被静默跳过（见下方 done 集合）。
    """
    remaining = list(tasks)
    done: set[str] = set()
    out: list[list[TaskItem]] = []
    while remaining:
        layer = [t for t in remaining if all(d in done for d in t.dependsOn)]
        if not layer:
            # 剩下的全都依赖不到 —— 丢弃，避免死循环
            break
        out.append(layer)
        done.update(t.id for t in layer)
        remaining = [t for t in remaining if t.id not in done]
    return out


# ---------- 交接包：只传「结论 + 证据」，不传完整历史 ----------

_CONCLUSION_LIMIT = 700
_EVIDENCE_LIMIT = 300


def _handoff(task_id: str, result: dict) -> dict:
    """打包一个 worker 的产出，交给下游（别的 worker / 评审员 / 主笔）。

    ⭐ `evidence` 是**调用记录 + 实际拿到的内容**，不是结论文本。
    对评审员来说这两件事必须分开：**结论是主张，调用记录才是证据**。
    否则评审员等于听它自己说自己是对的。
    """
    return {
        "id": task_id,
        "role": result.get("role"),
        "conclusion": (result.get("answer") or "")[:_CONCLUSION_LIMIT],
        "evidence": result.get("evidence", []),
        "ms": result.get("totalMs", 0.0),
        "steps": result.get("steps", 0),
        "reason": result.get("reason"),
    }


def _feed_evidence(buf: list[dict], ev: dict) -> None:
    """从 worker 的 `action` / `observation` 事件里收集证据。

    ⚠️ **不能从 `trace` 里取**——阶段 10 的 trace 只记了
    `{tool, arguments, ok, ms, chars}`，**没有保存 observation 原文**
    （观测当年只作为 SSE 事件发出，没进 trace）。
    如果照着 trace 去读 `act["observation"]`，拿到的是空字符串，
    评审员会**看不到任何证据**，于是把每条结论都判成"无支撑"——
    一个静默失效的角色，比没有这个角色更糟（它还让你以为审查过了）。

    所以这里按 `callId` 把 action 和 observation 配成对。
    """
    if ev.get("type") == "action":
        buf.append(
            {
                "callId": ev.get("callId"),
                "tool": ev.get("tool"),
                "arguments": ev.get("arguments"),
                "observation": "",
            }
        )
    elif ev.get("type") == "observation":
        for item in buf:
            if item.get("callId") == ev.get("callId"):
                item["observation"] = (ev.get("content") or "")[:_EVIDENCE_LIMIT]
                return


def _worker_prompt(task: TaskItem, handoffs: list[dict]) -> str:
    """拼出 worker 的 prompt。

    ⭐ 只给「前置任务的结论 + 证据」，**不给它们的完整对话历史**。
    这是多智能体控制上下文的关键：每多一个角色，上下文只增加一份
    几百字的结论，而不是一整份 messages 列表。
    """
    head = f"你要完成的任务（类型 {task.kind}）：\n{task.description}"
    if not handoffs:
        return head
    lines = [
        head,
        "",
        "下面是前置任务已经得出的结论，**直接使用，不要重复查证**：",
    ]
    for h in handoffs:
        lines.append(f"\n[{h['id']}]（由 {h.get('role') or '?'} 完成）\n{h['conclusion']}")
        if h["evidence"]:
            brief = "；".join(
                f"{e['tool']}({json.dumps(e['arguments'], ensure_ascii=False)[:80]})"
                for e in h["evidence"][:3]
            )
            lines.append(f"  证据（调用记录）：{brief}")
    return "\n".join(lines)


# ---------- 编排主流程 ----------

_SENTINEL = None  # 队列哨兵：一个 worker 跑完了


def run_team(
    client: LLMClient,
    question: str,
    *,
    model: str | None = None,
    parallel: bool = True,
    max_tasks: int = 4,
    observation_limit: int = 1200,
) -> Iterator[dict]:
    """跑一整个多智能体流程，**逐事件 yield**。

    事件类型（在阶段 10 的基础上多了 `agent` 字段——并行时必须有它，
    否则前端无法把交织的事件分回各自的角色）：

        start / phase / plan / agent_start / delta / action / observation
        / agent_end / review / write / finish / error

    ⭐ 并行段用的是「后台线程 + queue.Queue + 哨兵」（阶段 09 那一套）。
    阶段 10 说"能自己拆开的流程就别用线程去绕"，但这里的流程**本身就是并发的**：
    三个 worker 同时跑，谁先产出一帧是不确定的。**判据是"流程是否并发"。**
    """
    t_start = time.perf_counter()
    llm_ms = 0.0

    yield {
        "type": "start",
        "question": question,
        "model": model or client.model,
        "parallel": parallel,
        "maxTasks": max_tasks,
        "roles": [r.name for r in (ROLES[k] for k in ROLE_ORDER)],
    }

    # ---------- ① 规划 ----------
    yield {"type": "phase", "phase": "plan", "label": "规划员拆解任务"}
    planner = ROLES[ROLE_PLANNER]
    # ⭐ Planner / Critic 走的是 `client.chat()`（非流式），**也要计时**。
    # 本阶段的核心论点是"多智能体的成本是角色数的线性倍数"——
    # 如果只统计 worker 的时间，这个账就算漏了。
    t_p = time.perf_counter()
    plan, raw = _chat_json(
        client,
        [
            {"role": "system", "content": planner.system},
            {"role": "user", "content": f"用户的问题是：{question}"},
        ],
        model,
        planner.temperature,
        PlanModel,
        PLAN_SCHEMA_HINT,
    )
    if plan is None:
        # 规划失败就没法分工 —— 如实说明，而不是退化成"一个角色干完"
        yield {"type": "error", "message": f"规划员没能输出合法的任务列表：{raw[:200]}"}
        return

    plan_ms = round((time.perf_counter() - t_p) * 1000, 1)
    llm_ms += plan_ms
    tasks = plan.tasks[:max_tasks]
    yield {
        "type": "plan",
        "tasks": [t.model_dump() for t in tasks],
        "layers": [[t.id for t in layer] for layer in _layers(tasks)],
        "ms": plan_ms,
        "raw": raw,
    }

    # ---------- ② 执行（拓扑分层，同层可并行） ----------
    yield {"type": "phase", "phase": "work", "label": "各角色并行执行"}
    handoffs: list[dict] = []
    results: dict[str, dict] = {}

    for layer_no, layer in enumerate(_layers(tasks), start=1):
        ready = [(t, [h for h in handoffs if h["id"] in t.dependsOn]) for t in layer]
        yield {
            "type": "layer",
            "layer": layer_no,
            "parallel": parallel and len(ready) > 1,
            "agents": [
                {"task": t.id, "role": KIND_ROLE.get(t.kind, ROLE_RESEARCHER), "kind": t.kind}
                for t, _ in ready
            ],
        }

        layer_results: dict[str, dict] = {}
        # 证据缓冲区：按任务收集「调用记录 + 实际拿到的内容」，供评审员核查
        evidence: dict[str, list[dict]] = {t.id: [] for t, _ in ready}

        # ⭐ 先把每个角色的**工具面**报出来。前端要靠它判断"分工是不是真的"——
        # 如果两个 agent 的 tools 一模一样，那就是伪多智能体。
        for task, _deps in ready:
            role = ROLES[KIND_ROLE.get(task.kind, ROLE_RESEARCHER)]
            yield {
                "type": "agent_start",
                "agent": task.id,
                "task": task.id,
                "role": role.name,
                "label": role.label,
                "groups": list(role.groups),
                "tools": [s["function"]["name"] for s in react.build_schemas(role.groups)],
                "kind": task.kind,
            }

        if parallel and len(ready) > 1:
            # ⭐ 并行：必须走线程 + 队列，否则事件会全部堆到最后才涌出
            q: "queue.Queue[tuple[str, dict | None]]" = queue.Queue()

            def worker_body(task: TaskItem, deps: list[dict]) -> None:
                try:
                    # worker 内部的 ReAct 事件也要转发出来（"把黑盒拆开"是全程原则）
                    role = ROLES[KIND_ROLE.get(task.kind, ROLE_RESEARCHER)]
                    for ev in Agent(
                        client,
                        system=role.system,
                        groups=role.groups,
                        max_steps=role.max_steps,
                        temperature=role.temperature,
                        observation_limit=observation_limit,
                    ).run(_worker_prompt(task, deps), model=model):
                        q.put((task.id, ev))
                except Exception as e:  # noqa: BLE001
                    # ⚠️ 这里必须是宽捕获：worker 跑在线程池里，异常不接住会让整个
                    # ThreadPoolExecutor 静默丢结果。但**宽捕获不等于静默**——
                    # 阶段 12 出过一次真实事故：`observation_limit` 误当 run() 的 kwarg，
                    # TypeError 被这里吞成一条普通 error 事件，worker 全部空转却看不出原因。
                    # 所以除了把消息转给用户，还要把堆栈打到日志，保证下次能一眼定位。
                    # 把异常本身也写进 msg：没有配置 handler 时 logging 走 lastResort，
                    # 只打印 msg、不带堆栈——至少保证原因不会丢。
                    logger.exception("worker %s 执行失败：%s", task.id, e)
                    q.put((task.id, {"type": "error", "message": f"{task.id} 执行失败：{e}"}))
                finally:
                    q.put((task.id, _SENTINEL))

            with ThreadPoolExecutor(max_workers=len(ready)) as ex:
                for task, deps in ready:
                    ex.submit(worker_body, task, deps)

                finished = 0
                while finished < len(ready):
                    tid, ev = q.get()
                    if ev is None:  # 哨兵：这个 worker 跑完了
                        finished += 1
                        yield {"type": "agent_end", "task": tid}
                        continue
                    _feed_evidence(evidence[tid], ev)
                    ev = _normalize_worker_event(ev)
                    if ev is None:
                        continue
                    yield {**ev, "agent": tid}
                    if ev.get("type") == "agent_result":
                        layer_results[tid] = ev["result"]
        else:
            # 串行：流程在我们手里，直接 for 就行，不需要线程
            # （agent_start 已经在上面统一发过了）
            for task, deps in ready:
                role = ROLES[KIND_ROLE.get(task.kind, ROLE_RESEARCHER)]
                for ev in Agent(
                    client,
                    system=role.system,
                    groups=role.groups,
                    max_steps=role.max_steps,
                    temperature=role.temperature,
                    observation_limit=observation_limit,
                ).run(_worker_prompt(task, deps), model=model):
                    _feed_evidence(evidence[task.id], ev)
                    ev = _normalize_worker_event(ev)
                    if ev is None:
                        continue
                    yield {**ev, "agent": task.id}
                    if ev.get("type") == "agent_result":
                        layer_results[task.id] = ev["result"]
                yield {"type": "agent_end", "task": task.id}

        for tid, res in layer_results.items():
            role_name = KIND_ROLE.get(
                next((t.kind for t in tasks if t.id == tid), "research"), ROLE_RESEARCHER
            )
            res["role"] = role_name
            res["label"] = ROLES[role_name].label
            res["tools"] = [
                s["function"]["name"] for s in react.build_schemas(ROLES[role_name].groups)
            ]
            # ⭐ 把这一层收集到的证据挂回结果。不挂的话，评审员永远拿到空 evidence，
            # 于是把每条结论都判成"无支撑"——一个静默失效的审查（见 _feed_evidence）。
            res["evidence"] = evidence.get(tid, [])
            results[tid] = res
            handoff = _handoff(tid, res)
            handoffs.append(handoff)
            llm_ms += res.get("llmMs", 0.0)
            yield {"type": "handoff", "agent": tid, "handoff": handoff}

    # ---------- ③ 评审 ----------
    yield {"type": "phase", "phase": "review", "label": "评审员核查证据"}
    critic = ROLES[ROLE_CRITIC]
    material = json.dumps(
        [{"task": h["id"], "conclusion": h["conclusion"], "evidence": h["evidence"]} for h in handoffs],
        ensure_ascii=False,
        indent=2,
    )
    t_c = time.perf_counter()
    report, raw_review = _chat_json(
        client,
        [
            {"role": "system", "content": critic.system},
            {
                "role": "user",
                "content": f"原始问题：{question}\n\n各任务产出如下：\n{material}",
            },
        ],
        model,
        critic.temperature,
        CriticReport,
        CRITIC_SCHEMA_HINT,
    )
    review_ms = round((time.perf_counter() - t_c) * 1000, 1)
    llm_ms += review_ms
    issues = [i.model_dump() for i in (report.issues if report else [])]
    verdict = report.verdict if report else "unknown"
    yield {
        "type": "review",
        "verdict": verdict,
        "issues": issues,
        "ms": review_ms,
        "raw": raw_review,
    }

    # ---------- ④ 汇总（流式） ----------
    yield {"type": "phase", "phase": "write", "label": "主笔汇总"}
    writer = ROLES[ROLE_WRITER]
    flagged = {i["taskId"] for i in issues}
    brief = [
        f"原始问题：{question}",
        "",
        "各任务结论：",
    ]
    for h in handoffs:
        mark = "【评审员标记：证据不足】" if h["id"] in flagged else ""
        brief.append(f"- [{h['id']}] {mark}\n  {h['conclusion']}")
    if issues:
        brief.append("\n评审意见：")
        for i in issues:
            brief.append(f"- {i['taskId']}：{i['problem']}")

    messages = [
        {"role": "system", "content": writer.system},
        {"role": "user", "content": "\n".join(brief)},
    ]
    answer = ""
    t_w = time.perf_counter()
    for chunk in client.stream(messages, model=model, temperature=writer.temperature):
        answer += chunk
        yield {"type": "write", "text": chunk}
    write_ms = round((time.perf_counter() - t_w) * 1000, 1)
    llm_ms += write_ms

    yield {
        "type": "finish",
        "answer": answer.strip(),
        "model": model or client.model,
        "totalMs": round((time.perf_counter() - t_start) * 1000, 1),
        "llmMs": round(llm_ms, 1),
        # 分角色计时：本阶段要算的就是这笔账——钱花在谁身上了
        "planMs": plan_ms,
        "reviewMs": review_ms,
        "writeMs": write_ms,
        "verdict": verdict,
        "issues": issues,
        "tasks": [
            {
                "id": t.id,
                "kind": t.kind,
                "description": t.description,
                "dependsOn": t.dependsOn,
                "role": KIND_ROLE.get(t.kind, ROLE_RESEARCHER),
                "answer": (results.get(t.id) or {}).get("answer"),
                "ms": (results.get(t.id) or {}).get("totalMs", 0.0),
                "steps": (results.get(t.id) or {}).get("steps", 0),
                "reason": (results.get(t.id) or {}).get("reason"),
                "tools": (results.get(t.id) or {}).get("tools", []),
            }
            for t in tasks
        ],
        "handoffs": handoffs,
    }


def _result_of(finish_event: dict) -> dict:
    """把 worker 的 `finish` 事件收成一份结果。"""
    return {
        "answer": finish_event.get("answer"),
        "reason": finish_event.get("reason"),
        "steps": finish_event.get("steps", 0),
        "totalMs": finish_event.get("totalMs", 0.0),
        "llmMs": finish_event.get("llmMs", 0.0),
        "trace": finish_event.get("trace", []),
        "tools": [],
    }


def _normalize_worker_event(ev: dict) -> dict | None:
    """把 worker（ReAct 循环）的事件收编进编排器的事件流。

    ⚠️ 两个事件必须改名，否则会和编排器自己的同名事件撞车：

      - worker 的 `start`  → **丢掉**。`agent_start` 已经把角色、工具面、kind 都报过了，
        再转发一次只是重复。
      - worker 的 `finish` → 改名 `agent_result`。不改名的话前端分不清
        "某个角色跑完了" 和 "整个团队跑完了"——两者现在都叫 finish。

    其余 `delta` / `action` / `observation` / `step_end` 原样转发，
    由调用方补上 `agent` 字段。
    """
    t = ev.get("type")
    if t == "start":
        return None
    if t == "finish":
        return {"type": "agent_result", "result": _result_of(ev)}
    return ev


def run_team_blocking(client: LLMClient, question: str, **kwargs) -> dict:
    """把生成器跑完，收成一份完整结果（给不需要流式的调用方用）。"""
    events = list(run_team(client, question, **kwargs))
    start = next((e for e in events if e["type"] == "start"), {})
    finish = next((e for e in events if e["type"] == "finish"), None)
    plan = next((e for e in events if e["type"] == "plan"), {})
    review = next((e for e in events if e["type"] == "review"), {})

    base = {
        "question": question,
        "model": (finish or {}).get("model") or kwargs.get("model") or client.model,
        "parallel": start.get("parallel", True),
        "tasks": plan.get("tasks", []),
        "layers": plan.get("layers", []),
        "verdict": review.get("verdict"),
        "issues": review.get("issues", []),
        "events": events,
    }
    if finish is None:
        err = next((e for e in events if e["type"] == "error"), None)
        return {
            **base,
            "answer": None,
            "results": [],
            "totalMs": 0.0,
            "llmMs": 0.0,
            "error": (err or {}).get("message", "未知错误"),
        }
    return {
        **base,
        "answer": finish["answer"],
        "results": finish["tasks"],
        "handoffs": finish.get("handoffs", []),
        "totalMs": finish.get("totalMs", 0.0),
        "llmMs": finish.get("llmMs", 0.0),
        "writeMs": finish.get("writeMs", 0.0),
        "error": None,
    }
