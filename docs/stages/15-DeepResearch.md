# 阶段 15 · Deep Research

> 把「一次搜索」升级成「一次研究」：先规划大纲，再逐节自适应检索与撰写，
> 合并成一篇带全局引用的长报告，最后用一次额外 LLM 调用做**忠实性核查**。
> 本阶段的核心不是「又多加一层检索」，而是回答一个更尖锐的问题：
> **答案不仅要有出处，还要经得起「逐句核对」。**

---

## 1. 阶段目标

- 理解 **Deep Research（深度研究代理）** 与「单轮 AI 搜索」的差异：搜索回答**一个**问题，深度研究**拆解并回答一组**问题，再综合成一篇报告。
- 掌握深度研究的四个环节：① **规划**（把问题拆成子问题大纲）；② **逐节自适应检索**（复用阶段 13）；③ **合并 + 全局引用重编号**（复用阶段 14 的引用工程）；④ **忠实性校验**（阶段 15 新增）。
- 打通一个关键能力：**把多节各自的本地 `[n]` 引用，正确合并成一套全局编号**——即跨小节的来源去重与重编号。
- 明确**忠实性（faithfulness）** 的含义：报告里的每一句事实性结论，是否真的被检索到的资料支持。这是阶段 14 遗留的「引用了 ≠ 可信」的补齐。
- 产出一个完整的前端研究页：研究问题输入 → 大纲与分段进度 → 带全局引用的报告 → 可核对的来源面板 + 忠实性结论。

---

## 2. 前置知识 / 环境

- **已完成**：阶段 07（向量检索）、08（混合检索 + 重排）、09（图谱）、13（Agentic RAG）、14（AI 搜索）。
- **复用模块**：`agentic.run_agentic_blocking`（阶段 13 的多轮自适应检索）、`search.SEARCH_SYSTEM` / `search._parse_citations`（阶段 14 的引用合成与解析）、`rag.hybrid_search`（兜底单轮检索）、`llm.LLMClient`（chat + stream）。
- **环境**：后端依赖与阶段 07 一致（`uvicorn` + ChromaDB + 本地嵌入）；前端为 `vue-tsc` 通过的 Vue3 组件。`.env` 里需有 `DEEPSEEK_API_KEY`，且阶段 07 的知识库已灌入（本机 412 块）。

---

## 3. 核心概念

### 3.1 为什么「一次搜索」不够

阶段 14 的 AI 搜索已经能「问一句、给答案、每段挂来源」。但它仍有一个天花板：**它只检索一次**。遇到一个宽泛或复合的研究性问题（例如「如何系统评估一份 RAG 系统的检索质量？」），单轮 top-k 只会捞回一小撮片段，答案要么以偏概全，要么根本没覆盖问题的全部面向。

深度研究的思路是把问题**先拆开**：

```
用户问题（宽泛 / 复合）
        │  ① 规划 plan
        ▼
研究大纲：[子问题1, 子问题2, 子问题3, ...]
        │  ② 逐节自适应检索（每节各跑一遍阶段 13 的 route→grade→rewrite）
        ▼
每节：命中片段 + 本地 [n] 小结
        │  ③ 合并 + 全局重编号
        ▼
一篇带全局 [n] 引用的长报告
        │  ④ 忠实性校验
        ▼
finish：report + sources + faithful / faithfulness / unsupported
```

### 3.2 四个环节，各自复用谁

| 环节 | 做什么 | 复用资产 |
|---|---|---|
| ① 规划 | 把问题拆成 N 个子问题（大纲） | 阶段 04 的「结构化输出 + 自纠」（本文件自带 `_json_call`） |
| ② 逐节检索 | 每个子问题各跑一遍**自适应检索** | 阶段 13 `agentic.run_agentic_blocking`（route→grade→rewrite + 通道升级） |
| ③ 合并 | 各节小结的本地 `[n]` **合并去重 + 全局重编号** | 阶段 14 `search._parse_citations` + 本文件的来源表 |
| ④ 忠实性 | 逐句核对报告是否被资料支持 | 本文件新增 `_faithfulness` |

**为什么把阶段 13 与阶段 14 组合起来？** 因为它们恰好互补：阶段 13 擅长「怎么把资料查全」（自适应、多轮、换通道），阶段 14 擅长「把查回来的资料变成可核对的话」（带 `[n]` 的合成）。深度研究 = **用 13 去查，用 14 去写**，再补上一道 14 没做的核查。

### 3.3 「引用了」≠「可信」：阶段 15 补的那一刀

阶段 14 反复强调过：`grounded=true` 只代表「模型在答案里写了 `[n]`，且 `n` 落在合法来源上」。它**不保证**那句结论真的被资料支持。阶段 15 用一次**独立的 LLM 调用**把这件事挑明：

```
你把「报告 + 全部资料片段」交给一个事实核查员：
  → 逐句检查事实性结论能否被资料支持；
  → 不被支持的，原样摘录进 unsupported；
  → 给出 faithful（是否整体可信）与 faithfulness（被支持结论占比）。
```

这一步的价值在于**把「看起来可信」变成「可被问责」**：报告里若出现模型凭记忆补的、或断章取义的一句话，`unsupported` 会把它点出来。前端据此把结论如实标出，而不是假装「已验证」。

### 3.4 和阶段 13 / 14 的关系

| | 阶段 13 Agentic RAG | 阶段 14 AI 搜索 | 阶段 15 Deep Research |
|---|---|---|---|
| 回答几个问题 | 1 个 | 1 个 | **一组**（大纲） |
| 检索 | 多轮自适应 | 单轮混合 | **每节各跑一遍多轮自适应** |
| 输出 | 决策链路时间线 | 带 `[n]` 的答案 + 来源面板 | **带全局 `[n]` 的长报告** + 忠实性核查 |
| 一句话 | 检索引擎 | 引擎之上的产品外壳 | **引擎 + 外壳之上的研究员** |

---

## 4. 动手实现

### 4.1 后端 `research.py`：四件事收口在一个生成器里

事件协议延续阶段 10–14 的「一帧一行」，并新增研究特有的几类：

```
start             问题、模型、小节数
plan              研究大纲（各子问题）              ← ① 规划
section_start     第 i 节开始：标题 + 子问题
section_retrieve  该节自适应检索结果（轮次/通道/命中数） ← ② 逐节检索
section_answer_delta 该节小结的流式增量（本地 [n]）
answer_delta      合并后、全局重新编号的报告增量      ← ③ 合并
section_finish    该节小结收尾（本地引用 + grounded）
synthesize        合并状态（小节数 / 全局来源数）
faithfulness      忠实性核查结果（是否可信 / 分值 / 存疑结论） ← ④ 校验
finish            完整报告 + 逐节小结 + 来源面板 + 忠实性
error             失败原因
```

规划用「结构化输出 + 一次自纠」（与阶段 11/13/14 同源，本文件自带一份紧凑实现）：

```python
PLAN_HINT = '{"sections": [{"title": "小节标题", "question": "要查的具体子问题"}]}'

def _plan(client, question, model, max_sections, temperature):
    outline, raw = _json_call(client, messages, model, temperature, PlanOutline, PLAN_HINT)
    if outline is None or not outline.sections:
        return [{"title": "研究概述", "question": question}]   # 解析失败：整问题即唯一一节
    return [...][:6]
```

逐节检索直接复用阶段 13，若智能体判定「无需检索」或落空，则兜底一次单轮混合检索：

```python
def _section_retrieve(client, sub_q, *, model, top_k, rerank, hops, max_rounds, temperature):
    res = agentic.run_agentic_blocking(client, sub_q, model=model, top_k=top_k, hops=hops,
                                       max_rounds=max_rounds, temperature=temperature)
    hits = res.get("hits") or []
    if not hits:                                   # 兜底：确保每节都有资料可引
        found = rag.hybrid_search(sub_q, top_k=top_k, mode="hybrid", rerank=rerank)
        hits = [...]
    return {"hits": hits, "rounds": res.get("rounds", 0), "basedOn": res.get("basedOn", "hybrid"), ...}
```

⭐ **合并 + 全局重编号**是本阶段最容易写错、也最值得写对的一段。每节的小结是**本地编号**（`[1]` 指本节第 1 段），合并时必须映射到**全局来源表**（按 `title + index + text` 去重），再把小结文字里的 `[n]` 整体重写：

```python
mapping = {}
for local_i in range(1, len(hits) + 1):
    key = (hits[local_i-1]["title"], hits[local_i-1]["index"], hits[local_i-1]["text"])
    if key in seen_keys:
        mapping[local_i] = seen_keys[key]        # 跨节重复 → 复用同一全局编号
    else:
        rank = len(global_sources) + 1
        global_sources.append({**hits[local_i-1], "rank": rank, "section": i})
        seen_keys[key] = rank
        mapping[local_i] = rank

renumbered = _CITE_RE.sub(lambda m: f"[{mapping.get(int(m.group(1)), int(m.group(1)))}]", sub_answer)
```

忠实性核查是阶段 15 的签名能力（一次额外的 `chat` 调用，JSON + 校验）：

```python
FAITH_HINT = '{"faithful": true/false, "score": 0.0~1.0, "unsupported": ["不被资料支持的具体结论原文"]}'
```

### 4.2 端点 `main.py`：复用阶段 14 的接法

```python
@app.post("/api/research/run", response_model=ResearchResponse)   # 非流式
@app.post("/api/research/stream")                                 # 流式（前端用这个）
```

`ResearchRequest` / `ResearchResponse` 是新增契约（`schemas.py`），其中 `ResearchResponse.faithful` / `faithfulness` / `unsupported` 是阶段 15 相对阶段 14 新增的字段。

### 4.3 前端：研究大纲是签名元素

`useResearch.ts` 把 SSE 帧还原成三样东西：**大纲（含每节状态）**、**全局重新编号的报告**、**来源面板 + 忠实性**。`ResearchConsole.vue` 的三块：

1. **研究问题输入框（签名元素）**：多行 textarea + 紫色强调边框，`Ctrl/⌘ + Enter` 触发；
2. **研究大纲 / 分段进度**：每节一条，带 `待研究 → 检索中 → 撰写中 → 已完成` 状态徽章，并显示命中数、自适应轮次、走的通道；
3. **报告卡 + 来源面板**：报告里的 `[n]` 是可点击 chip，点一下定位到来源面板；卡头挂一枚**忠实性徽章**（已通过 / N 处存疑），存疑结论单列在下方。

```ts
// 报告按行渲染：以 #/## 开头的当小标题，其余按 [n] 拆 chip
const renderedAnswer = computed(() => s.answer.value.split('\n').map((line) => {
  const h = /^#{1,3}\s+(.*)$/.exec(line)
  return h ? { heading: h[1] } : { parts: splitParts(line) }
}))
```

> 视觉区分：阶段 13 用青绿标「决策链路」，阶段 14 用琥珀标「搜索框 + 引用 chip」，
> 阶段 15 用**紫**标「研究大纲 + 忠实性核查」。同一套设计系统，三种记忆点。

---

## 5. 运行验证

### ① 静态冒烟（不需要 Key）

```bash
cd backend && uv run python _t_research_static.py
```

验证七件事，全部通过才算合格：

| # | 验证点 | 期望 |
|---|---|---|
| 1 | 规划产出大纲，条数 == `max_sections` | 大纲条数正确 |
| 2 | 两节重叠片段**全局去重**，本地 `[n]` 重编号 | 3 条来源、`citations=[1,2,3]` |
| 3 | 越界引用 `[9]`（只有 2 段）被安全丢弃 | 该节 `grounded=false`，不崩溃 |
| 4 | 某节检索为空时合成被跳过、如实交代 | 报告含「未检索到」，流程正常收尾 |
| 5 | 忠实性核查结果进入 `finish` | `faithful=true`、`faithfulness=0.95` |
| 6 | 耗时分项自洽 | `llmMs == Σ分项`，`totalMs >= llmMs` |
| 7 | blocking 包装结构正确 | `sources/grounded/faithful/error` 齐全 |

### ② 真实调用（需要 Key）

启动前后端后打开 `/research`，输入问题即可。下面的实测来自本机 DeepSeek 实跑
（知识库 11 篇 / 412 块，`top_k=4`、`max_sections=3`、含重排）：

问题：**「RAG 系统的检索质量该从哪些环节评估？切块、混合检索、重排各要注意什么？」**

规划出的 3 个小节，各自的检索与引用情况：

| 研究小节 | 自适应轮次 | 命中 | 引用 | grounded |
|---|---|---|---|---|
| 检索质量评估的整体框架与指标体系 | 0（判为无需检索 → 兜底混合） | 4 | `[1][2][3][4]` | ✅ |
| 切块（Chunking）策略的评估要点 | 3 | 2 | `[5][6]` | ✅ |
| 混合检索与重排环节的评估要点 | 3 | 2 | `[7][8]` | ✅ |

合并后的整体结果：

| 指标 | 值 |
|---|---|
| 小节数 | 3 |
| 全局来源（已跨节去重） | 8 条 |
| 全局引用编号 | `[1]`–`[8]`（全覆盖） |
| grounded | ✅ |
| 引用覆盖率 coverage | 1.00 |
| **忠实性 faithful / faithfulness** | **❌ / 0.85** |
| 存疑结论 unsupported | 9 处 |
| 报告长度 | 约 4130 字 |
| 总耗时 / LLM | 175.0s / 175.0s |

耗时结构：`plan 2.2s · retrieve 101.3s · synthesize 42.5s · faithfulness 29.0s`。

观察四点：

1. **覆盖率 1.00**：8 条全局来源**全部**被报告引用，说明各节小结没有浪费检索到的资料。
2. **忠实性核查真的「开火」了**：`faithful=false`、`faithfulness=0.85`，并点出 9 处存疑。逐条看，绝大多数是模型写的**元话语**（如「资料提出思考题……」「资料提到……」），而非事实性结论——说明核查偏严，会把「转述资料」的句子也标出来。这恰好印证了阶段 14 那句「引用了 ≠ 可信」：**即使每句都挂了 `[n]`，仍有句子被判定「资料并未直接支持」**。这正是本阶段存在的意义。
3. **第 1 节 `rounds=0`**：智能体判定「该子问题无需检索」，系统**兜底**了一次单轮混合检索、拿到 4 段——兜底路径在真实环境里确实生效了。
4. **瓶颈全在 LLM**：`totalMs ≈ llmMs`（175.0s），检索本身（ChromaDB + 重排）几乎不占时间；深度研究「贵」在多出来的这一串 LLM 调用（每节多轮 route/grade/rewrite + 合成 + 核查）。

> ⚠️ 以上数字来自本机实跑，**没有伪造任何测量数据**。你的环境耗时会有差异，但结构
> （规划 → 逐节检索 → 合并 → 忠实性）应一致。

---

## 6. 小结

- 深度研究 = **规划 + 逐节自适应检索 + 合并重编号 + 忠实性校验**。阶段 13 负责「查全」，阶段 14 负责「写得可核对」，本阶段把两者组合并补上核查。
- **合并 + 全局重编号**是本阶段真正的技术难点：多节各自的本地 `[n]`，要靠一张按内容去重的全局来源表映射成一套统一编号。
- **忠实性（faithfulness）** 是阶段 14 那句「引用了 ≠ 可信」的补齐：用一次独立 LLM 调用逐句核对，把「看起来可信」推进到「可被问责」。
- 深度研究贵在**多花几次 LLM 调用换覆盖度与可核查性**——这正是它区别于「快而准」的单轮搜索的地方。

---

## 7. 练习与验收

**练习 1（扩展题）**：给忠实性核查加一道「**自动返修**」——若某节小结里出现 `unsupported` 的结论，
回到该节把「不被支持的那句」剔除后重写小结，再重新合并。思考：返修会不会引入新的不忠实？

**练习 2（思考题）**：目前各节是**串行**研究的。改成**并发**（`concurrent.futures` / `asyncio`）
需要解决什么问题？提示：全局来源表的编号是**顺序敏感**的——并发下如何保证「全局编号稳定」？

**练习 3（改造题）**：把规划输出从「扁平小节」升级为「**两级大纲**」（章 + 节），
并让前端渲染出可折叠的目录树。

**验收标准**：

1. `uv run python _t_research_static.py` 全绿；`import main` 成功、路由数增加、`vue-tsc -b` 零错误、`vite build` 通过、`vitepress build` 通过。
2. 前端 `/research` 能跑通：能看到大纲与分段进度，报告里的 `[n]` 渲染成可点击 chip，点 chip 能定位到来源面板对应条目，忠实性徽章如实显示。
3. ⭐ **新增阶段必须实际 `import main` 一次**（阶段 13 踩过的 P0：漏 import 模型会让整个后端起不来）。
4. 能讲清：`grounded=true` 与 `faithful=true` 分别证明了什么、没证明什么。
