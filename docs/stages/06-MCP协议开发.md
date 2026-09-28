# 阶段 06 · MCP 协议开发

> **主线大项目起点**｜预计 2–3 小时
> 配套代码：`backend/mcpkit/server.py`、`backend/mcpkit/client.py`、`backend/mcpkit/host.py`、
> `backend/main.py`（新增 4 个 `/api/mcp/*` 路由）、
> `frontend/src/stages.ts`、`frontend/src/router/index.ts`、
> `frontend/src/composables/useMcp.ts`、`frontend/src/components/McpConsole.vue`、`frontend/src/pages/*`

## 1. 阶段目标

前五个阶段是**能力积木**：会聊天、会流式、会抽取、会调工具。从这一阶段起，我们把这些积木拼成**一个真正的应用**——「全栈 AI 研究助手」。

这一阶段要解决的核心问题是：**工具从哪来？**

阶段 05 里，四个工具是我们自己 `import` 进同一个进程的 Python 函数。这在小 demo 里没问题，但你很快会撞上三堵墙：

- 你在 Cursor 里写好了一个数据库工具，在 Claude Desktop 里又写了一遍，在这个项目里还得写第三遍——**同一份能力被复制粘贴 M 次**；
- 你想接一个别人写好的 GitHub 工具，但它是 Node 写的、跑在另一个进程里——**跨语言、跨进程**；
- 用户凭什么信任你提供的"删除文件"工具？**权限边界在代码里是隐式的**，没法声明、没法审查。

MCP（Model Context Protocol）就是为解决这三件事而生的协议。学完这一阶段，你会：

- 明白 MCP 到底标准化了什么（**答案很朴素：一个函数调用的协议**）；
- 能手写一个跑在 stdio 上的 JSON-RPC 2.0 server，并解释每一行报文；
- 掌握三大原语 **Tools / Resources / Prompts** 的分工与区别；
- 把 MCP server 的能力接进阶段 05 的 tool loop——**而且循环代码一行都不用改**；
- 在前端把 JSON-RPC 报文摊开给用户看，让"能力是外挂的"这件事变得**可见**；
- 顺手把前端从"单页实验台"升级为**多阶段应用壳**（路由化），这是主线项目的第一次重构。

> **为什么这一阶段要手写协议，而不用官方 SDK？**
> 因为 MCP 的全部"魔法"就是 `server.py` 里的 200 行：**读一行 JSON → 看 method 分发 → 写一行 JSON 回去**。看完这 200 行你再用官方 SDK，就知道它在替你做什么、以及它可能在哪儿出问题。先懂原理，再用轮子。

## 2. 前置知识 / 环境

| 项 | 要求 |
|---|---|
| 已完成 | 阶段 05（函数调用与工具集成）——本阶段直接复用它的 `TOOLS`、`execute_tool`、`run_tool_loop` |
| 后端 | `cd backend && uv run fastapi dev main.py` |
| 前端 | `cd frontend && pnpm dev` |
| 密钥 | `backend/.env` 里的 `DEEPSEEK_API_KEY`（**只有"跑一轮对话"需要**；看协议目录不需要） |
| 新增依赖 | **无**。整个 MCP 实现只用标准库：`subprocess` / `json` / `threading` / `queue` |

最后一行值得强调：MCP 是**协议**，不是框架。任何语言、任何进程，只要能读写 stdin/stdout 就能当 server——这正是它比"又一个 Python 库"更有价值的地方。

## 3. 核心概念

### 3.1 从 M×N 到 M+N

没有协议的世界是这样的：

```
M 个应用（Claude Desktop / Cursor / 你的助手）× N 个能力（数据库 / GitHub / 文件系统）
= M × N 份适配代码
```

每接一个新应用，就要为它重写一遍所有能力；每加一个新能力，就要给所有应用各写一遍。这是经典的**集成爆炸**。

MCP 的做法是插一层协议，让两边都只对接协议：

```
M 个应用 ──┐                    ┌── N 个能力
           ├── MCP 协议 ────────┤
           ┘                    └──
= M + N 份适配代码
```

**应用侧**只需要实现一次"MCP 客户端"，**能力侧**只需要实现一次"MCP 服务端"。之后任意组合都能直接工作——**同一个 server 能被 Claude Desktop、Cursor 和本课程的应用同时复用**。

一句话总结 MCP 的定位：**它是"函数调用"这件事的跨进程、跨语言标准化封装。**

### 3.2 三层架构：Host / Client / Server

MCP 规范里把参与者分成三层，名字很像但职责完全不同，**这是最容易混淆的地方**：

| 角色 | 是什么 | 在本项目里是谁 |
|---|---|---|
| **Host** | 应用本体。它**拥有 LLM**，也**拥有用户**，负责决定"给模型看哪些工具" | `backend/mcpkit/host.py`（跑在 FastAPI 进程里） |
| **Client** | Host 内部**与某一个 Server 通信**的连接器。一个 Host 可以有很多 Client | `backend/mcpkit/client.py`（`MCPClient` 实例） |
| **Server** | 提供能力的**独立进程/服务**。它**完全不认识 LLM** | `backend/mcpkit/server.py`（`python server.py` 子进程） |

关键点：**Server 不知道 DeepSeek 是什么，也不知道什么是 prompt。** 它只认 `method + params`。这种"无知"恰恰是可复用性的来源——正因为它不关心谁在调用，所以谁都能调用。

数据流向：

```
用户问题
   ↓
Host（FastAPI）── 把 MCP 工具翻译成 OpenAI 的 tools 参数 ──→ LLM
   ↑                                                          ↓
   └── Client 写 JSON-RPC 到 Server 的 stdin ←── tool_calls ──┘
                    ↓
              Server 读 stdin → 执行 → 写 stdout
```

### 3.3 传输层：一行一个 JSON

MCP 支持多种传输方式，最基础的是 **stdio**——Server 作为子进程启动，双方通过管道通信。规则简单到令人意外：

> **一行 = 一条完整的 JSON-RPC 报文。写完必须 flush。**

```
→ {"jsonrpc":"2.0","id":1,"method":"ping"}
← {"jsonrpc":"2.0","id":1,"result":{}}
```

没有长度前缀，没有分隔符，就是**换行**。这带来一个必须刻进脑子的后果：

> **stdout 是协议通道，一个字节都不能污染。**

新手最常踩的坑就是随手 `print("debug")`——那一行会被客户端当成报文去解析，整个协议当场崩掉。所以本项目的日志一律走 **stderr**：

```python
def log(*parts: Any) -> None:
    """调试日志走 stderr——stdout 是协议通道，绝不能污染。"""
    print("[mcp-server]", *parts, file=sys.stderr, flush=True)
```

顺带说一句，HTTP 传输（Streamable HTTP / SSE）也支持，适合远程 server。但 stdio 是"本地能力接入"的主流选择：无需端口、无需鉴权、生命周期跟着 Host 走。

### 3.4 JSON-RPC 2.0：请求、响应、通知

MCP 的报文格式完全遵循 JSON-RPC 2.0。只有三种形态，**靠有没有 `id` 来区分**：

| 形态 | 有 `id`？ | 对方要回应吗 | 例子 |
|---|---|---|---|
| **请求 Request** | 有 | **要** | `tools/list`、`tools/call` |
| **响应 Response** | 有（与请求同 id） | — | `{"id":1,"result":{...}}` |
| **通知 Notification** | **没有** | **绝对不要** | `notifications/initialized` |

这是**第二条铁律**：通知没有 `id`，所以**必须不回应**。如果你给通知回了一条 `result`，对方会收到一条它没请求过的消息——协议直接乱套。

代码里体现得非常直白：

```python
def handle(message: dict) -> None:
    method = message.get("method")
    mid = message.get("id")

    # 通知（没有 id）：不回应。典型的是 initialize 之后的 notifications/initialized
    if mid is None:
        log("notification", method)
        return
    ...
```

错误码也用标准值，别自己发明：

| 码 | 含义 | 本项目触发场景 |
|---|---|---|
| `-32700` | Parse error | 喂进去一行不是 JSON |
| `-32601` | Method not found | 调用未实现的方法（如 `tools/nonexistent`） |
| `-32603` | Internal error | handler 内部抛异常 |

### 3.5 握手与能力协商

连上之后不能立刻问"你有什么工具"——**必须先握手**。`initialize` 是唯一一个必须在其他所有请求之前完成的调用：

```python
def _initialize(_params: dict) -> dict:
    """握手：双方交换协议版本与能力清单，之后才知道"能问对方什么"。"""
    return {
        "protocolVersion": PROTOCOL_VERSION,   # "2025-06-18"
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        },
        "serverInfo": SERVER_INFO,             # {"name": "research-notes", "version": "0.1.0"}
    }
```

两个方向都要报自己的 `capabilities`，这叫**能力协商**：

- Server 说"我支持 tools / resources / prompts"；
- Client 说"我支持 roots / sampling"（后者是"反过来让模型帮忙生成内容"的能力，本课程暂不展开）。

握手完还没结束——**Client 必须再发一条 `notifications/initialized` 通知**，握手才算真正完成。这条通知没有 `id`，Server 收到后什么也不回：

```python
def initialize(self) -> dict:
    result = self._request("initialize", {...})
    self.protocol_version = result.get("protocolVersion", "")
    self.capabilities = result.get("capabilities", {})
    self.server_info = result.get("serverInfo", {})
    # 规范要求：initialize 之后必须发一条 initialized 通知，握手才算完成
    self._notify("notifications/initialized")
    return result
```

为什么这么设计？因为双方要**先对齐版本与能力，再开始对话**。如果 Server 不支持 `prompts`，Client 就不该去调 `prompts/list`——这是协议层的"先看菜单再点菜"。

### 3.6 三大原语：Tools / Resources / Prompts

MCP 把"外部能力"分成三类。它们的区别不是技术上的，而是**语义上的**，理解这一点比记 API 重要得多：

| 原语 | 一句话 | 谁主动 | 类比 | 本项目的例子 |
|---|---|---|---|---|
| **Tools** | **可调用**的函数，会产生效果 | **模型**主动调 | 动词 | `calculator`、`get_weather` |
| **Resources** | **可读取**的数据，只读、无副作用 | **应用**主动取 | 名词 | `research://notes`、`research://glossary` |
| **Prompts** | **可复用**的提示模板 | **用户**主动选 | 短语手册 | `research/explain`、`research/compare` |

为什么要把 Tools 和 Resources 分开？因为它们的**信任模型不同**：

- Tools 会**改变世界**（发邮件、删文件、下单），所以调用前可以拦截、可以让用户确认；
- Resources 只**读取世界**，读一百次也没有副作用，所以可以自由订阅、自由缓存。

把它们混成一个 `functions` 列表，就没法做这种区分了。这是 MCP 设计上最值得学习的一处——**用类型系统表达权限意图**。

还有一处细节：**Prompts 取回来的是 `messages`，不是一段纯文本**：

```python
return {
    "description": f"MCP prompt: {name}",
    "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
}
```

因为提示模板天然是"对话结构"（可能带 system、可能带 few-shot 示例），用 `messages` 表达才不会丢信息。

### 3.7 第三条铁律：`isError` 不是协议错误

阶段 05 我们学过"工具失败要当数据回传"。MCP 把这件事做成了**协议的一部分**：

```python
result, error = execute_tool(name, json.dumps(arguments, ensure_ascii=False))
if error:
    # 注意：工具失败不是协议错误，而是"成功的响应 + isError: true"
    return {"content": [{"type": "text", "text": error}], "isError": True}
```

对比一下两种"失败"，这是本阶段最精妙的一处设计：

| 情况 | 报文形态 | 谁来处理 |
|---|---|---|
| 工具**业务失败**（查火星天气） | **成功的响应**（`result`）+ `isError: true` | **模型**——它读得懂错误文本，会改口解释 |
| 方法不存在 / 参数结构错 | **失败的响应**（`error` + 错误码） | **Host 代码**——这是编程错误，模型无能为力 |

区别在于**谁能修复**：模型能修复的（换成"深圳"重试、告诉用户没数据），就走 `isError` 让模型看见；模型不能修复的（协议用错了），就走 `error` 让程序处理。

至此三条铁律齐了：**① stdout 是协议通道；② 通知不回应；③ server 不认识 LLM。** 这三条能挡掉新手 90% 的坑。

## 4. 动手实现

### 4.1 后端 · Server：`mcpkit/server.py`

**先说一个命名上的坑**：目录叫 `mcpkit` 而不是 `mcp`。因为 `backend/` 排在 `sys.path` 首位，如果目录叫 `mcp`，那么将来 `pip install mcp`（官方 SDK）之后，`import mcp` 会解析到我们自己的本地目录，排查起来极其痛苦。**给你的包起名时，避开要用的第三方包名。**

Server 的骨架就是一个"读-分发-写"循环：

```python
HANDLERS: dict[str, Callable[[dict], dict]] = {
    "tools/list": _list_tools,
    "tools/call": _call_tool,
    "resources/list": _list_resources,
    "resources/read": _read_resource,
    "prompts/list": _list_prompts,
    "prompts/get": _get_prompt,
    "ping": lambda _params: {},
}

def main() -> None:
    log("started, protocol", PROTOCOL_VERSION)
    for line in sys.stdin:                 # 一行一条报文，读到 EOF 就退出
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": f"Parse error: {e}"}})
            continue
        handle(message)
```

`tools/list` 的返回里，**`inputSchema` 就是 JSON Schema**——和阶段 05 给 OpenAI 的 `parameters` 同源：

```python
def _list_tools(_params: dict) -> dict:
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                # MCP 的 inputSchema 就是 JSON Schema——和 OpenAI 的 parameters 同源
                "inputSchema": t.args_model.model_json_schema(),
            }
            for t in TOOLS.values()
        ]
    }
```

这句话是整个阶段 06 能"几乎不写新代码"的根本原因：**MCP 和 OpenAI 用的是同一套 JSON Schema**。所谓"翻译"，只是把键名从 `parameters` 改成 `inputSchema` 而已。

`tools/call` 直接复用阶段 05 的执行器（参数校验、错误捕获都现成）：

```python
def _call_tool(params: dict) -> dict:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    result, error = execute_tool(name, json.dumps(arguments, ensure_ascii=False))
    ...
```

Resources 用 URI 寻址。注意 `resources/read` 里未知 URI 是**抛异常**（→ `-32603`），因为那是**调用方的编程错误**：

```python
_NOTE_RESOURCES = {
    "research://notes": {"name": "课程笔记", "description": "...", "mimeType": "text/markdown"},
    "research://glossary": {"name": "术语表", "description": "...", "mimeType": "text/markdown"},
}
```

### 4.2 后端 · Client：`mcpkit/client.py`

Client 的活儿看起来很少：**拉起子进程 → 往 stdin 写 → 从 stdout 读**。但有两个工程细节必须处理对，否则在 Windows 上一定翻车。

**细节一：读 stdout 必须放在独立线程。**

主线程要能"带超时地等响应"，而管道读是阻塞的。如果主线程直接 `readline()`，server 一旦不回话，整个应用就卡死了。做法是开一个后台线程把每一行丢进队列：

```python
def _pump_stdout(self) -> None:
    assert self._proc.stdout is not None
    for line in self._proc.stdout:
        self._out.put(line)
    self._out.put(None)  # 进程结束标记
```

然后主线程带超时地从队列取：

```python
while True:
    try:
        line = self._out.get(timeout=self.timeout)   # 20s
    except queue.Empty as e:
        raise MCPError(f"等待 {message['method']} 响应超时（{self.timeout}s）") from e
    if line is None:
        raise MCPError("MCP server 已退出（stdout 关闭）")
    ...
```

> **Windows 专属提醒**：不要用 `select` 去等管道——**Windows 上 `select` 不支持管道**，只支持 socket。用"线程 + 队列"是跨平台最稳的写法。

**细节二：server 的 stderr 要单独收走。**

否则 server 写日志写多了会把 stderr 管道缓冲写满，然后**卡死在写日志上**——一个非常隐蔽的死锁：

```python
def _pump_stderr(self) -> None:
    assert self._proc.stderr is not None
    for line in self._proc.stderr:
        self.server_log.append(line.rstrip("\n"))
```

**细节三：只接受与自己 id 配对的响应。**

```python
# 只接受与自己 id 配对的响应
if response.get("id") != message["id"]:
    self._record("←(stale)", response)
    continue
```

单线程一问一答时这条看似多余，但它保证了**乱序响应、迟到响应**不会串台。协议实现的严谨性就体现在这种地方。

最后，Client 把**每一条进出报文都记进 `self.log`**：

```python
def _record(self, direction: str, payload: Any) -> None:
    self.log.append({"dir": direction, "payload": payload})
```

这块日志不是调试用的——**它是本阶段的教学主角**，前端要把它摊给用户看。

### 4.3 后端 · Host：`mcpkit/host.py`——翻译层

Host 的职责只有两件"翻译"：

```python
def openai_tools(self) -> list[dict]:
    """MCP tools → OpenAI tools 参数。inputSchema 就是 JSON Schema，直接搬。"""
    ...
    out.append({
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        },
    })
```

和反方向——把模型的 `tool_calls` 转成 MCP 的 `tools/call`。关键是**让执行器的签名与阶段 05 的 `execute_tool` 完全一致**：

```python
def make_executor(self) -> Callable[[str, str], tuple[Any, str | None]]:
    """返回一个"执行器"，签名与阶段 05 的 execute_tool 一致，可直接注入 run_tool_loop。"""
    def executor(name: str, raw_arguments: str) -> tuple[Any, str | None]:
        owner = self.tool_owner.get(name)
        if owner is None:
            return None, f"没有任何 MCP server 提供工具 {name}"
        ...
        result = self.clients[owner].call_tool(name, arguments)
        text = "\n".join(part.get("text", "") for part in result.get("content", [])
                         if part.get("type") == "text")
        # MCP 用 isError 表达"工具业务失败"，同样当数据回传给模型
        if result.get("isError"):
            return None, text
        return text, None
    return executor
```

于是阶段 05 的循环**一行都不用改**，只是换了注入进去的工具与执行器：

```python
outcome = run_tool_loop(
    llm, question,
    model=model, max_steps=max_steps,
    tools=self.openai_tools(),      # ← 来自 MCP server
    executor=self.make_executor(),  # ← 走 JSON-RPC
    system=HOST_SYSTEM_PROMPT,
)
```

> **这就是"协议"的价值**：工具来自本进程的函数，还是来自另一个进程，**对模型来说没有区别**。阶段 05 刻意把 `tools` / `executor` 做成可注入参数，到这一阶段终于收回了回报。

`tool_owner` 这张路由表为多 server 预留：工具名 → 提供它的 server。现在只有一个 server，但结构上已经支持"接十个 server、按名字路由"。

还有一处细节值得学：`run()` 用**游标**只回传本次新增的报文，而不是把全部日志倒给前端：

```python
cursor = {name: len(c.log) for name, c in self.clients.items()}
...
protocol = []
for name, client in self.clients.items():
    for entry in client.log[cursor[name]:]:
        protocol.append({"server": name, **entry})
outcome["protocol"] = protocol
```

**长连接的日志会无限增长**，每次请求只回增量，才不会让响应体越来越胖。

### 4.4 后端 · 路由：`main.py`

```python
@app.get("/api/mcp/servers", response_model=MCPCatalog)     # 目录：协议版本 / 能力 / 三原语清单
@app.post("/api/mcp/run", response_model=MCPRunResponse)    # 跑一轮，附带 JSON-RPC 报文
@app.get("/api/mcp/resource", response_model=MCPResourceContent)  # resources/read
@app.post("/api/mcp/prompt")                                # prompts/get
```

其中 `/api/mcp/servers` 有个值得注意的性质：**它不依赖 LLM，所以没有 Key 也能看**。

```python
@app.get("/api/mcp/servers", response_model=MCPCatalog)
def mcp_servers() -> MCPCatalog:
    """不依赖 LLM，所以没有 Key 也能看——这正是 MCP 的意义：能力与模型解耦。"""
```

这不是巧合，而是 MCP 架构的直接推论：**Server 不认识 LLM**，所以列出它的能力当然不需要 LLM。能力与模型解耦，是这套设计最实用的红利。

### 4.5 前端 · 从单页到应用壳

前五个阶段的前端是一个"单页实验台"：换阶段就改 `App.vue`。到了阶段 06 必须停手——主线项目要**同时保留**聊天、抽取、工具、MCP 四个页面。这就是路由化的时机。

**第一步：把阶段清单抽成唯一事实来源**（`src/stages.ts`）。之前 `StageRail.vue` 里硬编码了 19 项数组，现在抽出来共享：

```ts
export const STAGES: StageMeta[] = [ /* id / label / path */ ]
export function stageById(id: number): StageMeta | undefined
export function implementedStages(): StageMeta[]
```

**第二步：建路由**（`src/router/index.ts`）。三个设计决策：

```ts
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  { path: '/chat',    name: 'chat',    component: () => import('../pages/ChatPage.vue'),    meta: { stage: 3 } },
  { path: '/extract', name: 'extract', component: () => import('../pages/ExtractPage.vue'), meta: { stage: 4 } },
  { path: '/tools',   name: 'tools',   component: () => import('../pages/ToolsPage.vue'),   meta: { stage: 5 } },
  { path: '/mcp',     name: 'mcp',     component: () => import('../pages/McpPage.vue'),     meta: { stage: 6 } },
  { path: '/stage/:id(\\d+)', name: 'stage-placeholder',
    component: () => import('../pages/StagePlaceholder.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]
```

1. **组件一律懒加载**（`() => import(...)`）——`vue-best-practices` 的建议，顺带解决了"所有阶段组件打进一个 chunk"的体积问题。构建产物里每个阶段是独立文件，这就是证据。
2. **`meta: { stage: n }` 携带阶段号**——这样"当前阶段"有唯一事实来源，进度轨、页面标题都从它推导，不会出现"路由在 4、标题写着 3"的不一致。
3. **未实现的阶段走 `/stage/:id` 占位页**——让进度轨上任何一格都**点得动**，用户不会点了没反应。

`afterEach` 统一维护标题，顺便把 `meta.stage` 与 `stages.ts` 对齐：

```ts
router.afterEach((to) => {
  const id = Number(to.meta.stage ?? to.params.id ?? 0)
  const meta = stageById(id)
  document.title = meta
    ? `阶段 ${String(id).padStart(2, '0')} · ${meta.label} — 全栈 AI 研究助手`
    : '全栈 AI 研究助手'
})

/** 按阶段号找路由（StageRail 点击时用） */
export function pathForStage(id: number): string {
  return stageById(id)?.path ?? `/stage/${id}`
}
```

**第三步：`App.vue` 变成应用壳**。它不再知道任何具体业务，只负责"外壳 + 插槽"：

```vue
<template>
  <StageRail />                       <!-- 左侧进度轨，点击 → router.push(pathForStage(id)) -->
  <AppHeader :stage="currentStage" /> <!-- 顶部标题，读 route.meta -->
  <RouterView />                      <!-- 业务页面在这里 -->
</template>
```

阶段号从路由推导，而不是自己维护一个 `ref`：

```ts
const currentStage = computed(() => Number(route.meta.stage ?? route.params.id ?? 3))
```

这是**"路由是状态的唯一来源"**的实践——刷新页面、直接粘贴 URL、浏览器前进后退，阶段号永远是对的。

### 4.6 前端 · `useMcp.ts` + `McpConsole.vue`

`useMcp.ts` 与阶段 05 的 `useToolRunner.ts` 结构相似（拉目录 + 跑一轮），但多了两个原语的操作：

```ts
async function readResource(uri: string)   // resources/read：资源是"可读取的数据"
async function getPrompt(name: string)     // prompts/get：取回来是 messages，不是纯文本
```

四个预设问题各对应一种调用形态：

| 预设 | 观察点 |
|---|---|
| 检索笔记 | `search_notes` 的参数来自用户问题（一次问两个关键词 → 同一步两次调用） |
| 多工具 | 一次请求里 `calculator` + `get_weather` |
| 工具报错 | `isError: true` → 模型解释而非崩溃 |
| 时间与算数 | 真实 IO（时区）+ 计算 |

> **关于「工具报错」预设为什么问广州，而不是火星**
> 这是个真机踩出来的细节。最早我们用的是「火星现在天气怎么样？」，结果模型**直接拒绝调用工具**——它知道火星没有城市、也没有气象站，于是用背景知识回答，压根没触发 `isError`。演示效果为零。
> 换成**广州**就不一样了：它是真实城市，模型一定会调用 `get_weather`，只是我们的模拟数据里没有它——于是必然走到 `isError: true` 这条路上。
> **教训**：设计演示用例时，要让"错误"由**系统**产生，而不是由**模型的判断**产生。前者稳定复现，后者看运气。

`McpConsole.vue` 是三栏控制台，**右栏是本阶段的核心新增**：

- **左 · Server 目录**：协议版本、能力清单，下面按三大原语分组——Tools（可展开看参数）、Resources（点一下直接读内容）、Prompts（点一下看它生成的 messages）。
- **中 · 提问与调用**：预设问题 + 最终回答 + `trace` 时间线（复用阶段 05 的视觉语言）。
- **右 · JSON-RPC 报文**：把 `protocol` 数组逐条渲染，`→` 是发出的请求、`←` 是收到的响应，每条都能展开看完整 JSON。

**为什么右栏必须存在？** 因为"工具是外挂的、通过进程间协议调用"这件事，**在界面上原本是不可见的**。用户看到的只是"问了一句、答了一句"。把报文摊开，用户才第一次真正看见：**模型产出的 `tool_calls` 是怎样变成一行 JSON、写进另一个进程的 stdin、再读回结果的。**

这就是本阶段的产品价值：**把"黑盒"变成"可验证的证据"**——和阶段 05 的 trace 是同一个思路，只是这次证据下探到了协议层。

## 5. 运行验证

```bash
# 终端 1
cd backend && uv run fastapi dev main.py
# 终端 2
cd frontend && pnpm dev
```

### 5.1 不用客户端：手工喂 JSON-RPC

这是**最推荐先做的验证**——它证明 server 真的只认协议，不认客户端：

```bash
cd backend
printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"ping"}' \
'{"jsonrpc":"2.0","id":3,"method":"tools/list"}' \
'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"calculator","arguments":{"expression":"(1234+876)*3/7"}}}' \
'{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_weather","arguments":{"city":"火星"}}}' \
'{"jsonrpc":"2.0","id":6,"method":"resources/list"}' \
'{"jsonrpc":"2.0","id":7,"method":"resources/read","params":{"uri":"research://glossary"}}' \
'{"jsonrpc":"2.0","id":8,"method":"prompts/list"}' \
'{"jsonrpc":"2.0","id":9,"method":"prompts/get","params":{"name":"research/explain","arguments":{"topic":"MCP"}}}' \
'{"jsonrpc":"2.0","id":10,"method":"resources/read","params":{"uri":"research://nope"}}' \
'{"jsonrpc":"2.0","id":11,"method":"tools/nonexistent"}' \
'this is not json' \
| uv run python mcpkit/server.py 2>/tmp/mcp_stderr.txt
```

> **注意**：用 `uv run`（即后端的虚拟环境）启动，不要用系统 Python——`tools.py` 依赖 pydantic。顺手把 stderr 重定向到文件，正好可以对照验证"日志走 stderr、协议走 stdout"。

**这个命令能一次性验证六件事：**

| # | 验证点 | 期望 |
|---|---|---|
| 1 | 通知不回应 | 12 条输入里，`notifications/initialized` **没有**对应输出 |
| 2 | 握手 | `id=1` 返回 `protocolVersion` + `capabilities` + `serverInfo` |
| 3 | 三大原语 | `tools/list`、`resources/list`、`prompts/list` 各返回自己的清单 |
| 4 | 工具业务失败 | `id=5` 是 **`result` + `isError:true`**（不是 `error`） |
| 5 | 协议错误 | `id=10` / `id=11` 返回 `-32603` / `-32601`；最后一行返回 `-32700` |
| 6 | stdout/stderr 分离 | stdout 只有 JSON；`/tmp/mcp_stderr.txt` 里有 `[mcp-server]` 日志 |

### 5.2 HTTP 层验证

```bash
# 目录（不需要 Key）
curl -s http://localhost:8000/api/mcp/servers | python -m json.tool

# 读一个资源
curl -s "http://localhost:8000/api/mcp/resource?uri=research://glossary"

# 取一个提示模板（注意返回的是 messages）
curl -s -X POST http://localhost:8000/api/mcp/prompt \
  -H "Content-Type: application/json" \
  -d '{"name":"research/explain","arguments":{"topic":"MCP"}}'

# 跑一轮真实对话（需要 Key）
curl -s -X POST http://localhost:8000/api/mcp/run \
  -H "Content-Type: application/json" \
  -d '{"question":"课程笔记里关于 MCP 和 RAG 分别讲了什么？"}' | python -m json.tool
```

### 5.3 前端验证清单（实测结果）

**路由化改造**（浏览器逐条访问，读 `document.title` 与 DOM）：

| 访问 | 页面标题 | 结果 |
|---|---|---|
| `/chat` | 阶段 03 · 多轮对话与流式 | ✅ |
| `/extract` | 阶段 04 · 结构化输出与信息抽取 | ✅ |
| `/tools` | 阶段 05 · 函数调用与工具集成 | ✅ |
| `/mcp` | 阶段 06 · MCP 协议开发 | ✅ |
| `/stage/07` | 阶段 07 · RAG 基础（**占位页**，不是白屏） | ✅ |
| 刷新 `/mcp` | 仍停留在 `/mcp` | ✅（`createWebHistory` 生效） |

**MCP 控制台**（真实点击预设「多工具」并运行）：

- [x] 左栏 server 卡片显示 `research-notes`、`connected`、`2025-06-18 · research-notes v0.1.0`，能力徽章 `tools / resources / prompts` 齐全；
- [x] TOOLS 列出 4 个工具，每个都能展开看描述（就是模型看到的"说明书"）；
- [x] RESOURCES 列出 `research://notes`（课程笔记）与 `research://glossary`（术语表）；
- [x] PROMPTS 列出 `research/explain` 与 `research/compare`；
- [x] 4 个预设 chip 各自带提示（如「多工具 · calculator + get_weather」）；
- [x] 点预设 → 输入框自动填入「帮我算 (1234+876)*3/7，再告诉我深圳现在多少度。」；
- [x] 点「运行」→ 右上角显示「**2 次工具调用 · 2 轮**」，最终回答同时给出 `904.285714…` 和「深圳 32℃ 阵雨」；
- [x] 中栏 trace 时间线出现**同一 STEP 1 下的两个节点**（`calculator` 0.7ms、`get_weather` 0.4ms），各带 `arguments` 与 `result`；
- [x] **右栏「JSON-RPC 报文」显示 4 条**，每条可展开完整 JSON：

```
→ tools/call  id=13  {"name":"calculator","arguments":{"expression":"(1234+876)*3/7"}}
← result      id=13  {"content":[{"type":"text","text":"{\"expression\":…,\"value\":904.2857142857143}"}],"isError":false}
→ tools/call  id=14  {"name":"get_weather","arguments":{"city":"深圳"}}
← result      id=14  {"content":[{"type":"text","text":"{\"city\":\"深圳\",\"temperature\":32,…}"}],"isError":false}
```

最后一栏是本阶段的"成绩单"：**报文条数（4）= 工具调用次数（2）× 2**，每次调用都是一来一回。id 连续递增（13、14），说明它们出自同一个 Client 会话。

> 顺带记一个前端配置上的小坑：`vite.config.ts` 的代理目标写的是 `http://127.0.0.1:8000` 而不是 `http://localhost:8000`。uvicorn 默认只监听 IPv4，而 Windows 上 `localhost` 有可能先被解析成 IPv6 的 `::1`，代理就会连不上后端。**本地开发时把 IP 写死，能省掉一类"玄学连不上"的问题。**

### 5.4 实测记录

**① 协议层（手工喂 13 条输入，不含 LLM）**

**输入 13 条 → 输出 12 条**，差的正好是那条通知。stdout 里 **0 行非 JSON**（用脚本逐行 `json.loads` 校验过），stderr 里有 5 行 `[mcp-server]` 日志：

```
[mcp-server] started, protocol 2025-06-18
[mcp-server] notification notifications/initialized      ← 通知被记录，但没有回应
[mcp-server] tools/call calculator {'expression': '(1234+876)*3/7'}
[mcp-server] tools/call get_weather {'city': '火星'}
[mcp-server] stdin closed, exiting
```

逐条响应：

| 输入 | 输出 | 结论 |
|---|---|---|
| `initialize` | `proto=2025-06-18`，`caps=[tools, resources, prompts]`，`serverInfo={name: research-notes, version: 0.1.0}` | 握手成功 |
| `notifications/initialized` | **无输出**（只在 stderr 留了一行日志） | **通知不回应** ✅ |
| `ping` | `result: {}`（空对象） | 心跳正常 |
| `tools/list` | `tools=4` → `calculator / search_notes / get_weather / get_current_time` | 与阶段 05 的 schema 同源 |
| `tools/call` calculator | `isError=False`，text=`{"expression": ..., "value": 904.2857142857143}` | 复用阶段 05 执行器 |
| `tools/call` get_weather 火星 | `isError=True`，text=`ValueError: 没有 火星 的天气数据，目前仅支持：北京、上海、深圳、杭州` | **业务失败 ≠ 协议错误** ✅ |
| `resources/list` | `research://notes`、`research://glossary` | 资源用 URI 寻址 |
| `resources/read` glossary | `text` 以 `# 术语表` 开头，357 字符 | 只读数据 |
| `prompts/list` | `research/explain`、`research/compare` | 提示模板清单 |
| `prompts/get` | `messages=1`，text=`你是一位耐心的技术讲师。请面向零基础初学者解释「MCP」…` | **返回 messages 而非纯文本** |
| `resources/read` 未知 URI | `error -32603 ValueError: 未知资源：research://nope` | 编程错误走 `error` |
| `tools/nonexistent` | `error -32601 Method not found` | 未实现方法 |
| 非 JSON 行 | `error -32700 Parse error` | 解析错误 |

**② Host 层（真实 DeepSeek 调用，4 个预设各跑一轮）**

| 预设问题 | steps | trace | protocol | 最终回答 |
|---|---|---|---|---|
| 课程笔记里关于 MCP 和 RAG 分别讲了什么？ | 2 | **同一步两个** `search_notes`（MCP / RAG） | 4 条（→2 / ←2） | 分别概括两条笔记 |
| 帮我算 (1234+876)*3/7，再告诉我深圳现在多少度。 | 2 | `calculator` + `get_weather(深圳)` | 4 条（→2 / ←2） | `904.286` / 32℃ 阵雨 |
| 广州现在多少度？ | 2 | `get_weather(广州)` → **`isError: true`** | 2 条（→1 / ←1） | 解释"只支持北京/上海/深圳/杭州" |
| 现在几点了？顺便帮我算 25 的平方根乘以 4。 | 2 | `get_current_time` + `calculator` | 4 条（→2 / ←2） | `2026-09-28T21:55:18+08:00` / 20 |

三个可直接观察到的结论：

1. **协议报文数与工具调用数严格成对**——每次 `tools/call` 必有一条 `→` 和一条 `←`。这就是"工具真的跑在另一个进程里"的铁证；
2. **`isError` 路径完全走通**——`get_weather(广州)` 的响应是 `{"content": [...], "isError": true}`，**不是** `error` 对象；模型读到它之后改口解释，而不是崩给你看；
3. **单次请求耗时 1.5～2.8 秒**（含 1～2 轮模型往返 + 进程间通信），和阶段 05 的本地工具在同一量级——**协议开销几乎可以忽略**。

**③ 一个必须知道的 Windows 坑：`tzdata`**

第一次真机跑「时间与算数」时，`get_current_time` 报了这个错：

```
ZoneInfoNotFoundError: 'No time zone found with key Asia/Shanghai'
```

看起来像"时区名写错了"，于是模型很聪明地换成 `UTC` 重试——**结果还是同一个错**。这暴露了真正的原因：

> **Windows 上 Python 没有系统时区数据库**，`zoneinfo` 找不到任何时区（连 `UTC` 都找不到）。必须额外安装 `tzdata` 包。

所以 `pyproject.toml` 的依赖里多了这一行：

```toml
"tzdata>=2024.1",
```

同时我们把错误信息改成**可操作**的，而不是让人去猜：

```python
try:
    tz = ZoneInfo(timezone)
except ZoneInfoNotFoundError as e:
    raise ValueError(
        f"取不到时区 {timezone}：运行环境缺少时区数据库。"
        "Windows 上需安装 tzdata（uv add tzdata）后重启服务。"
    ) from e
```

装上 `tzdata` 后，工具立刻返回 `2026-09-28T21:55:18+08:00`。

> 这一幕很值得回味：**模型的表现是对的**（它换了个时区重试），**错的是环境**。如果没有把错误信息写清楚，你可能会花半小时去怀疑"是不是模型不会用这个工具"。**把环境错误和业务错误分开、并把原因写进错误文本**，是让 Agent 可调试的关键一步。

**④ 前端构建**

```
✓ 1645 modules transformed
✓ built in 16.38s
dist/assets/McpPage-jyS-5uW9.js      9.75 kB │ gzip: 4.39 kB
dist/assets/ToolsPage-DT0UCgIe.js    6.41 kB │ gzip: 3.49 kB
dist/assets/ExtractPage-U4EZ5sF_.js  7.43 kB │ gzip: 4.02 kB
dist/assets/ChatPage-CcUsLdM2.js     4.67 kB │ gzip: 2.80 kB
dist/assets/StagePlaceholder-*.js    1.01 kB │ gzip: 0.83 kB
```

每个阶段一个独立 chunk——**路由懒加载生效的直接证据**。`vue-tsc -b` 0 错误。

**④ 一次值得写进教程的"意外"**

真机验证「检索笔记」时，模型回答"课程笔记里**没有**关于 MCP 的条目"。这**不是 bug**——`_NOTES` 里当时确实只到阶段 05 为止。模型没有编造，而是**如实说了"没找到"**。

这正是阶段 05 反复强调的那件事在真实场景里的兑现：**工具返回的内容就是模型的"事实边界"**。它不会凭空知道你的笔记里有什么。我们随后往 `_NOTES` 里补了 MCP 条目，再问就答对了。

> 如果你的 RAG（阶段 11 起）检索不到内容时模型开始胡编，回头想想这一幕：**问题往往不在模型，而在你给它的上下文里没有答案。**

## 6. 小结

- **MCP 是"函数调用"的跨进程、跨语言标准化封装**，解决的是 M×N 集成爆炸问题，把适配成本降到 M+N。
- 三层角色别混：**Host 拥有 LLM 与用户，Client 负责与某个 Server 通信，Server 完全不认识 LLM**。
- 传输层朴素得令人意外：**一行一个 JSON，stdout 是协议通道**。日志必须走 stderr。
- JSON-RPC 2.0 只有三种形态，**靠有没有 `id` 区分**：有 id 要回应，没 id（通知）**绝对不回应**。
- 连接后**必须先握手**（`initialize` + `notifications/initialized`），双方交换协议版本与能力清单。
- 三大原语按**信任模型**分工：Tools 会改变世界（模型调）、Resources 只读（应用取）、Prompts 是模板（用户选）。
- **工具业务失败是"成功的响应 + `isError:true`"，不是协议错误**——判断依据是"谁能修复"。
- 翻译层只做键名搬运（`inputSchema` ↔ `parameters`），**阶段 05 的循环一行都不用改**——这就是协议的价值。
- 能力与模型解耦的红利：**列出 MCP 目录不需要 LLM，也就不需要 Key**。
- 前端的价值是把协议**变可见**：右栏的 JSON-RPC 报文，让"工具是外挂的"从口号变成可验证的证据。

## 7. 练习与验收

**改造题**：给 MCP Server 增加**第二个 server**，实现一个「文档库」能力，并让 Host 能同时路由两个 server 的工具。

提示：

1. 复制 `server.py` 为 `server_docs.py`，`SERVER_INFO` 改成 `{"name": "doc-library", ...}`，把 `tools/list` 换成两个新工具（如 `list_documents` / `read_document`）；
2. 在 `MCPHost.connect()` 里再连一个（`connect("doc-library", command=[sys.executable, str(DOCS_SERVER_PATH)])`）——注意 `tool_owner` 路由表已经预留好了，**理论上不用改 `make_executor`**；
3. 观察 `openai_tools()` 是否把两个 server 的工具合并成了一个列表（模型看到的是一份"合并菜单"）。

**验收标准**

1. `GET /api/mcp/servers` 返回 **2 个** server，各有自己的 `protocolVersion` 与工具清单；
2. 提问"文档库里有哪些文档？"→ 模型调用 `list_documents`，且 `protocol` 报文里 `server` 字段是 `doc-library`；
3. 提问一个需要跨 server 协作的问题（如"先查文档库里有哪些文档，再算一下数量乘以 3"）→ trace 里出现**两个不同 server 的工具**；
4. 故意让两个 server 提供**同名工具** → 观察 `tool_owner` 的后写覆盖行为，并说明这在工程上应该如何避免（提示：工具名前缀命名空间，如 `docs/list`——MCP 官方 server 就是这么做的）。

**思考题（不写代码）**

1. 为什么 MCP 要把 Tools 和 Resources 分成两类，而不是统一成 `functions`？（提示：想想"读一百次"和"执行一百次"的差别）
2. 如果工具是"转账"这类**有副作用**的操作，你会把确认环节放在哪一层——Host、Client 还是 Server？（提示：阶段 05 末尾留过这个问题，MCP 的答案在 Host，因为**只有 Host 认识用户**）
3. `isError: true` 和 JSON-RPC 的 `error` 各该用在什么场景？举一个"模型能修复"和一个"模型不能修复"的例子。
