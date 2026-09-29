"""阶段 11 的静态冒烟测试：用**桩 client**跑通完整多智能体流程，不调真实模型。

覆盖的不变量：
  1. 角色工具面互异（researcher 能检索、analyst 能计算、critic/planner/writer 无工具）
  2. 拓扑分层正确（无依赖 → 同一层 → 并行）
  3. Planner 的 JSON 输出能被 Pydantic 校验（坏 JSON 自纠一次）
  4. 并行段确实并发（两个 worker 的 agent_start 都在任一 agent_result 之前）
  5. 证据（evidence）被收集并随 handoff 传下，评审员能看到，不是静默空
  6. 收口事件带分角色计时（planMs/reviewMs/writeMs/totalMs）

桩 client 的路由：
  - chat()      只看 system：含"规划员"→返回合法 plan；含"评审员"→返回 pass 报告
  - stream_message()  worker：首轮发一个工具调用；当上一轮是 tool 结果→收口成答案
  - stream()     writer：吐最终答案文本
"""
from __future__ import annotations

import json
import threading

import react
import team
from team import _layers, role_catalog


class ScriptedClient:
    model = "stub"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.log: list[str] = []

    def _rec(self, tag: str) -> None:
        with self.lock:
            self.log.append(tag)

    # ---- planner / critic：非流式 JSON ----
    def chat(self, messages, model=None, temperature=0.2, response_format=None):
        sys_text = messages[0]["content"]
        if "规划员" in sys_text:
            self._rec("chat:planner")
            return json.dumps(
                {
                    "tasks": [
                        {"id": "t1", "kind": "research", "description": "查 RAG 默认切块大小", "dependsOn": []},
                        {"id": "t2", "kind": "compute", "description": "算 (400+200)*2", "dependsOn": []},
                    ]
                }
            )
        if "评审员" in sys_text:
            self._rec("chat:critic")
            return json.dumps({"verdict": "pass", "issues": []})
        self._rec("chat:other")
        return "{}"

    # ---- worker：流式 message（含 tool_calls 拼装） ----
    def stream_message(self, messages, model=None, temperature=0.2, tools=None, tool_choice=None):
        last = messages[-1]
        self._rec("stream_message")
        if last.get("role") == "tool":
            # 工具结果已回来 → 收口
            yield {"delta": "综合检索结果，结论是：RAG 切块 400，计算结果为 1200。"}
            yield {"message": {"content": "结论：RAG 切块 400，计算结果为 1200。", "tool_calls": None}}
            return
        # 首轮：按角色系统决定调哪个工具（验证工具面确实生效）
        sys_text = messages[0]["content"]
        if "研究员" in sys_text:
            tool = "query_knowledge_graph"  # 知识图谱：空库也优雅返回，不依赖种子数据
            args = {"query": "RAG 默认切块大小", "hops": 2}
        else:  # 分析员
            tool = "calculator"
            args = {"expression": "(400+200)*2"}
        yield {"delta": "我先调一个工具查证。"}
        yield {
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}
                ],
            }
        }

    # ---- writer：流式纯文本 ----
    def stream(self, messages, model=None, temperature=0.3):
        for w in ["最终答案：", "RAG 切块大小为 400，", "乘算结果为 1200。"]:
            yield w


def test_roles_distinct():
    cat = role_catalog()
    faces = {r["name"]: set(r["tools"]) for r in cat["roles"]}
    local_tools = set(s["function"]["name"] for s in react.build_schemas((react.GROUP_LOCAL,)))
    rag_graph_tools = set(s["function"]["name"] for s in react.build_schemas((react.GROUP_RAG, react.GROUP_GRAPH)))
    assert faces["researcher"] == rag_graph_tools, faces["researcher"]
    assert faces["analyst"] == local_tools, faces["analyst"]
    assert faces["planner"] == set(), faces["planner"]
    assert faces["critic"] == set(), faces["critic"]
    # 关键不变量：**有工具**的角色之间不能完全相同（否则退化成伪多智能体）。
    # planner/critic/writer 都没工具是正常的（职责是规划/评审/汇总，不调工具）。
    tooled = [v for v in faces.values() if v]
    for i in range(len(tooled)):
        for j in range(i + 1, len(tooled)):
            assert tooled[i] != tooled[j], "有工具的角色工具面相同（伪多智能体）"
    print(f"✅ 1. 角色工具面互异：researcher={faces['researcher']} analyst={faces['analyst']}")


def test_layers():
    tasks = [
        team.TaskItem(id="t1", kind="research", description="a", dependsOn=[]),
        team.TaskItem(id="t2", kind="compute", description="b", dependsOn=[]),
        team.TaskItem(id="t3", kind="research", description="c", dependsOn=["t1"]),
        # 循环依赖应被丢弃
        team.TaskItem(id="t4", kind="compute", description="d", dependsOn=["t4"]),
    ]
    layers = _layers(tasks)
    flat = [t.id for layer in layers for t in layer]
    assert set(flat) == {"t1", "t2", "t3"}, flat  # t4 因自环被丢弃
    assert {"t1", "t2"} <= set(l.id for l in layers[0]), "无依赖的应在第 0 层"
    assert layers[-1][0].id == "t3", "依赖 t1 的应在最后一层"
    print(f"✅ 2. 拓扑分层正确：{[[t.id for t in l] for l in layers]}")


def test_run_team():
    client = ScriptedClient()
    events = list(team.run_team(client, "RAG 切块多少？算一下 (400+200)*2", parallel=True, max_tasks=4))

    types = [e["type"] for e in events]
    assert "start" in types and "plan" in types and "review" in types and "finish" in types

    # 并行：两个 worker 的 agent_start 都早于任一 agent_result
    starts = [events.index(e) for e in events if e["type"] == "agent_start"]
    results = [events.index(e) for e in events if e["type"] == "agent_result"]
    assert starts and results, (starts, results)
    assert max(starts) < min(results), "并行段：所有 agent_start 应在 agent_result 之前"

    # 分角色计时齐全
    finish = next(e for e in events if e["type"] == "finish")
    for k in ("totalMs", "planMs", "reviewMs", "writeMs", "llmMs"):
        assert k in finish and isinstance(finish[k], (int, float)), k

    # evidence 随 handoff 传出，且非全空
    handoffs = finish["handoffs"]
    assert len(handoffs) == 2, len(handoffs)
    total_evidence = sum(len(h["evidence"]) for h in handoffs)
    assert total_evidence > 0, "evidence 应为空被静默丢弃（评审员会误判）——这里必须 > 0"
    print(f"✅ 3. run_team 跑通：并行={finish['totalMs']}ms，证据条数={total_evidence}")
    print(f"   角色计时 plan={finish['planMs']} review={finish['reviewMs']} write={finish['writeMs']} total={finish['totalMs']}")


def test_planner_self_correct():
    """故意让 planner 第一次返回坏 JSON，验证 _chat_json 会自纠一次。"""
    cat = role_catalog()

    class FlakyClient(ScriptedClient):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        def chat(self, messages, model=None, temperature=0.2, response_format=None):
            if "规划员" in messages[0]["content"]:
                self.n += 1
                if self.n == 1:
                    return "这不是合法 JSON {"  # 触发自纠
                return json.dumps({"tasks": [{"id": "t1", "kind": "research", "description": "x", "dependsOn": []}]})
            return super().chat(messages, model, temperature, response_format)

    client = FlakyClient()
    events = list(team.run_team(client, "一个问题", parallel=False))
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["tasks"], "坏 JSON 自纠后仍应有任务"
    print("✅ 4. Planner 坏 JSON 自纠一次后恢复")


if __name__ == "__main__":
    test_roles_distinct()
    test_layers()
    test_run_team()
    test_planner_self_correct()
    print("\n全部静态冒烟通过 ✅")
