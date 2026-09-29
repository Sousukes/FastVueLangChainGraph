# 阶段 12 · Agent Harness 框架

> 阶段 10 把「一个全才」写成了 ReAct；阶段 11 把它拆成几个专职角色。
> 本阶段做一件事：**把三处重复的「心跳循环」抽成一个薄、可复用的框架**——
> 这样单智能体、多智能体的每个角色、甚至单独调试某个角色，都是同一段代码。

---

## 1. 阶段目标

完成本阶段后，你会得到：

- 一个 **`harness.Agent`**：把「客户端 / 系统提示 / 工具面 / 步数 / 温度」打包成一个对象，
  `Agent(...).run(question)` 就能跑一次带工具的对话。
- **多智能体（阶段 11）的 worker 不再自己调 `run_react`**，而是 `Agent(...).run(...)`——
  分工的实质（工具面）完全没变，只是循环有了唯一出处。
- 一个 **harness 控制台**：选一个角色（检索员 / 分析员 / 纯净助手）单独跑，
  你会直观看到它和「多智能体里同名的那一栏」是同一段代码。

本阶段的真正价值不是「又多了一个功能」，而是**把前面散落的代码收口**——
  这正是工程里「从能跑到可维护」的那一步。

---

## 2. 前置知识 / 环境

- 阶段 05（工具循环）、阶段 10（ReAct）、阶段 11（多智能体）都已实现。
- 后端依赖已就绪（`uv sync` 过）；前端 `pnpm install` 过。
- 一个真实可用的 `DEEPSEEK_API_KEY`（`.env`，本地、不入库）。

---

## 3. 核心概念

### 3.1 三个用例，同一段循环

阶段 05 / 10 / 11 的「心跳」其实是同一段伪代码：

```
model 决策 → 要么给答案，要么给 tool_calls
          → 执行工具，把结果回填进 messages
          → 再来一轮，直到答案 or 步数上限
```

区别只在**参数**：用什么工具面、什么系统提示、流式还是阻塞、谁在调。
当一份逻辑出现第三次，就该抽出来了——这是「该不该写框架」最朴素的一条判据：
**在有三个以上用例之前，先别急着抽象**（前两次复制粘贴通常是更便宜的）。我们现在正好有三个。

### 3.2 统一的事件协议 = 前端零改动

`react.run_react` 逐事件 yield：`start / delta / action / observation / step_end / finish`。
harness 的 `Agent.run` **直接委托**给它，所以事件流一字不差。
这意味着：多智能体（阶段 11）的前端时间线、单智能体（阶段 10）的前端时间线、
本阶段 harness 控制台的单栏时间线，**渲染逻辑完全相同**——多智能体里每个角色的栏，
就是这个 `AgentColumn` 直接画出来的。框架复用，连 UI 都跟着复用。

### 3.3 可插拔点：工具来源与执行器（呼应阶段 05 / 06）

```python
class ToolSource:   # 从哪拿工具说明书：本地注册表 or MCP server
    def schemas(self): return react.build_schemas(self.groups)
    def executor(self): return react.make_executor(self.groups)

class ToolExecutor: # 怎么执行：(name, arguments) -> (result, error)
    def __call__(self, name, arguments): return self._fn(name, arguments)
```

阶段 05 的 `run_tool_loop` 早就留了 `tools` / `executor` / `system` 三个注入点；
这里把它们命名成 `ToolSource` / `ToolExecutor`，**循环本身一行不用动**就能从本地切到 MCP。

### 3.4 ⚠️ 反模式：别把框架写成「大而全的 base class」

阶段 11 已经证明：**分工的实质是工具面，不是类名**。所以本阶段刻意只做薄封装——

- 不发明新的事件协议（复用 `react.run_react` 的）；
- 不重写执行器（复用 `react.make_executor`）；
- 不引入新的配置树（角色就是一组 `system + groups + max_steps`）。

`harness.py` 里**没有任何第二份循环**。这是有意为之：框架的价值在「收敛出处」，
不在「功能多」。多写一层抽象，就多一份要同步、要测试、要理解的东西。

---

## 4. 动手实现（后端 + 前端分步）

### 4.1 后端：`backend/harness.py`

`Agent` 只持有参数，`run` / `run_blocking` 委托给已经测过的 `react`：

```python
class Agent:
    def __init__(self, client, *, system=None, groups=DEFAULT_GROUPS,
                 max_steps=6, temperature=0.2, observation_limit=1200):
        self.client = client
        self.system = system
        self.groups = list(groups)
        self.max_steps = max_steps
        self.temperature = temperature
        self.observation_limit = observation_limit

    def run(self, question, *, model=None):
        yield from react.run_react(self.client, question, model=model,
                                   max_steps=self.max_steps, temperature=self.temperature,
                                   groups=self.groups, observation_limit=self.observation_limit,
                                   system=self.system)

    def run_blocking(self, question, *, model=None):
        return react.run_react_blocking(self.client, question, model=model,
                                        max_steps=self.max_steps, temperature=self.temperature,
                                        groups=self.groups, observation_limit=self.observation_limit,
                                        system=self.system)

    @property
    def tools(self):  # 这个 Agent 实际能用的工具——做「伪多智能体」自检
        return [s["function"]["name"] for s in react.build_schemas(self.groups)]
```

`PRESETS` 是「在框架之上配置角色」的示范：每个角色只是一组构造参数。
多智能体的角色和这里单独跑的角色，**背后是同一个 `Agent`**，区别只在 `groups` / `system`。

### 4.2 重构 `team.py`：worker 交给 Agent（单一真相源）

阶段 11 的 worker 原来直接 `react.run_react(client, ...)`。改成：

```python
for ev in Agent(
    client,
    system=role.system,
    groups=role.groups,
    max_steps=role.max_steps,
    temperature=role.temperature,
).run(_worker_prompt(task, deps), model=model, observation_limit=observation_limit):
    ...
```

**行为逐字段不变**：传入 `react.run_react` 的 kwarg 一个没少。`team.py` 的
`plan / layers / handoff / verdict` 全部照旧——这就是「重构安全」的判据：
框架抽出来之后，上层产物的形状不能变。

### 4.3 路由与 schema

- `GET /api/harness/roles` → 返回角色目录（含每个角色的工具面，和 `/api/team/roles` 同构）。
- `POST /api/harness/run` → 非流式跑一个角色，返回结果 + 结构化 trace。
- `POST /api/harness/stream` → SSE 逐事件推，每帧带 `agent` 字段（角色名）。

新增 schema：`HarnessRoleInfo / HarnessRolesResponse / HarnessRunRequest / HarnessRunResponse`
（与阶段 11 的同名结构同构，便于前端复用渲染）。

### 4.4 前端：`useHarness.ts` + `HarnessConsole.vue` + `/harness`

`useHarness` 直接复用阶段 11 的 `AgentColumn` / `AgentStep` 类型——单角色时间线
就是「只有一个栏的多智能体时间线」。控制台让你**点一张角色卡切换角色**，
看检索员怎么调检索、分析员怎么只算数、纯净助手怎么退化成纯对话。

---

## 5. 运行验证

> 以下数值来自本机真实调用（DeepSeek-Flash）。你的环境里数字会有出入，但**结构应当一致**。

**① 重构无回归**：`team.py` 改用 `Agent` 后，跑同一个问题，
`plan / layers / handoff / verdict` 与重构前逐字段相同（桩测试 `_t_team_static.py` 断言）。
框架抽出来，多智能体的行为一点没变。

**② 单角色跑通**（harness 控制台）：

| 角色 | 工具面 | 典型表现 |
|---|---|---|
| researcher | `search_course_docs` + `query_knowledge_graph` | 真实检索，结论带 `source` 出处 |
| analyst | `calculator` 等本地工具 | 只算数，不碰资料 |
| pure | 空 | 框架退化为纯对话，无 `action` 事件 |

**③ harness 端点**：`GET /api/harness/roles` 返回 3 个角色各自不同的 `tools`——
这本身就是「分工真实生效」的活证据；`POST /api/harness/stream` 推 190 类事件，
帧协议与阶段 10/11 一致。

**④ 关键对照**：在 harness 控制台跑「检索员」，和在阶段 11 多智能体里跑「研究员」，
看到的 Thought / Action / Observation 形状完全相同——**这就是框架复用**。

---

## 6. 小结

- 一份逻辑出现第三次，就该抽框架；但框架要**薄**：复用已有循环，不重写第二份。
- 统一事件协议让「单 / 多 / 单角色调试」共用一套前端渲染。
- 分工的实质永远是**工具面**，框架只是把「构造参数」收口成 `Agent`。
- `harness.py` 里没有新循环——这是有意为之，多一层抽象就多一份维护成本。

---

## 7. 练习与验收

**练习（改造题）**：给 `harness.PRESETS` 加一个 `critic` 角色（工具面为空，
系统提示要求「只核查传入的证据、不自己编」），在控制台单独跑它，
验证「无工具的角色」也能被同一个 `Agent` 驱动。

**验收标准**：
1. `python -m pytest` 或桩测试 `_t_team_static.py` 仍全绿（重构无回归）。
2. `GET /api/harness/roles` 返回的 3 个角色 `tools` 互不相同。
3. harness 控制台选「检索员」跑一个检索问题，能看到 `action` 真实调用 `search_course_docs`、
   `observation` 带回带 `source` 的原文。
4. `vue-tsc --noEmit` 0 错误，`vite build` 通过，`pnpm docs:build` 通过。

**调试题（15/18 加）**：若 `Agent.run` 后前端时间线空白——先确认
`react.run_react` 的 `start` 事件是否被正确透传（harness 的 `start` 帧少了 `groups`/`role`，
前端 `apply` 里要用选中的 `role` 兜底，否则 `column.tools` 为空、卡片不显示工具面）。
