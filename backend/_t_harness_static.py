"""阶段 12 静态冒烟测试（不需要 Key，用桩替换 react 的循环）。

验证三件事：
  1. role_catalog 返回 3 个角色，工具面互异（researcher=检索+图谱 / analyst=本地 / pure=空）。
  2. Agent.tools 与 react.build_schemas 严格一致（工具面查询没自己另写一份）。
  3. Agent.run / run_blocking 确实**委托**给 react.run_react / run_react_blocking
     （框架没有偷偷写第二份循环）——用桩替换 react 的循环，看事件是否透传。
"""

from __future__ import annotations

import harness
import react


def _fake_run(
    client,
    question,
    *,
    model=None,
    max_steps=6,
    temperature=0.2,
    groups=(),
    observation_limit=1200,
    system=None,
):
    yield {"type": "start", "tools": [s["function"]["name"] for s in react.build_schemas(groups)], "groups": list(groups)}
    yield {"type": "finish", "answer": "ok", "reason": None, "steps": 1, "totalMs": 1.0, "llmMs": 1.0}


def _fake_run_blocking(client, question, **kwargs):
    return {
        "question": question,
        "model": kwargs.get("model") or client.model,
        "groups": list(kwargs.get("groups", ())),
        "tools": [s["function"]["name"] for s in react.build_schemas(kwargs.get("groups", ()))],
        "events": [],
        "answer": "ok",
        "reason": None,
        "steps": 1,
        "totalMs": 1.0,
        "llmMs": 1.0,
        "trace": [],
        "error": None,
    }


def test_role_catalog():
    cat = harness.role_catalog()
    names = [r["name"] for r in cat["roles"]]
    assert names == ["researcher", "analyst", "pure"], names
    tools = {r["name"]: set(r["tools"]) for r in cat["roles"]}
    assert tools["researcher"], "researcher 应有检索工具"
    assert tools["analyst"] and "search_course_docs" not in tools["analyst"], "analyst 不应有检索工具"
    assert tools["pure"] == set(), "pure 应为空工具面"
    # 互异：任意两个角色工具面不同
    for a in names:
        for b in names:
            if a != b:
                assert tools[a] != tools[b], f"{a} 与 {b} 工具面相同（伪多智能体）"
    print("✅ 1. role_catalog：3 角色，工具面互异（researcher 检索 / analyst 本地 / pure 空）")


def test_agent_tools_matches_build_schemas():
    for groups in [("rag", "graph"), ("local",), ()]:
        agent = harness.Agent(client=None, groups=groups)  # type: ignore[arg-type]
        expected = [s["function"]["name"] for s in react.build_schemas(groups)]
        assert agent.tools == expected, (groups, agent.tools, expected)
    print("✅ 2. Agent.tools 与 react.build_schemas 严格一致")


def test_agent_delegates_to_react():
    orig_run, orig_block = react.run_react, react.run_react_blocking
    react.run_react = _fake_run  # type: ignore[assignment]
    react.run_react_blocking = _fake_run_blocking  # type: ignore[assignment]
    try:
        class _C:
            model = "stub"

        # run：事件透传
        evs = list(harness.Agent(_C(), groups=("rag", "graph")).run("q"))
        assert evs[0]["type"] == "start" and set(evs[0]["tools"]) == set(
            s["function"]["name"] for s in react.build_schemas(("rag", "graph"))
        ), evs[0]
        assert evs[1]["type"] == "finish" and evs[1]["answer"] == "ok"
        # run_blocking：委托到 blocking 变体
        res = harness.Agent(_C(), groups=("local",)).run_blocking("q")
        assert res["answer"] == "ok" and res["tools"] == [
            s["function"]["name"] for s in react.build_schemas(("local",))
        ], res
    finally:
        react.run_react, react.run_react_blocking = orig_run, orig_block
    print("✅ 3. Agent.run / run_blocking 委托给 react（框架无第二份循环）")


def test_team_refactor_no_regression():
    # team 的 worker 现在走 Agent；确认 team 模块能导入且引用了 Agent
    import team

    assert hasattr(team, "Agent"), "team 应引用 harness.Agent"
    print("✅ 4. team 模块已改为复用 harness.Agent（单一真相源）")


if __name__ == "__main__":
    test_role_catalog()
    test_agent_tools_matches_build_schemas()
    test_agent_delegates_to_react()
    test_team_refactor_no_regression()
    print("\n🎉 阶段 12 静态冒烟全部通过")
