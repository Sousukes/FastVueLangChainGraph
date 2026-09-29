# 阶段 09 · GraphRAG 知识图谱

> 本阶段把语料**抽成一张图**，用**关系**而不是"相似度"来检索。
> 这是 RAG 三部曲（07 向量 / 08 混合与重排 / 09 图谱）的最后一环，
> 也是第一次触及一个前面两个阶段根本做不到的能力：**回答"谁依赖谁"**。

---

## 1. 阶段目标

阶段 07 和阶段 08 把"**找相似的文本**"这件事做到了相当好：

| | 召回方式 | 回答的问题 |
|---|---|---|
| 07 向量检索 | 余弦相似度 | 哪一段文字**最像**我的问题？ |
| 08 BM25 + RRF + 重排 | 关键词 + 名次融合 + Cross-encoder 精排 | 哪一段文字**最相关**？ |

但有一类问题它们**天然答不好**——**关系型 / 多跳问题**：

```
问：重排是哪个阶段引入的？
语料里有讲 RRF 的块，有讲重排的块，两者各自都对。
但"重排属于 RAG 进阶"这条关系，从来没有任何一段文字写过。
```

向量检索只会返回"最像"的那几块。它永远拼不出这条边——
**因为那条边不在任何一段文本里，它需要被"建"出来。**

本阶段要做的三件事：

```
① 抽取   chunk ──LLM──> (头实体, 关系, 尾实体) 三元组
② 建图   三元组落进 SQLite，边上记 source_chunk   → 可溯源
③ 检索   问题 ──锚定实体──> 沿边扩 N 跳 ──> 拿回「边 + 源块」──> 拼 prompt
```

学完你应该能回答：

- 为什么"关系型问题"是向量检索的盲区？举个自己语料里的例子。
- 抽取（入库时跑 LLM）和检索（查询时查 SQLite）的成本分别在什么时候付？
- 跳数 1 / 2 / 3 各自的代价是什么？怎么选？
- 为什么每条边都必须带出处？不带会出什么问题？
- 图的覆盖率只有 5% 时，"图里没有"和"语料里没有"有什么区别？

---

## 2. 前置知识 / 环境

**必须已完成**：阶段 04（结构化输出 + Pydantic 校验 + 自纠重试）、
阶段 07（切块 / 嵌入 / ChromaDB）、阶段 08（BM25 / RRF）。

阶段 09 **不需要新依赖**。图谱落在 SQLite 上，而 SQLite 是 Python 标准库自带的：

```python
import sqlite3   # 就这一行，没有 pip install
```

> **为什么用 SQLite 而不是 Neo4j / 图数据库？**
>
> 因为这一阶段要讲的是"图能做什么"，不是"图数据库怎么运维"。
> 我们这张图只有几百个节点、几百条边——SQLite 建两个索引（`head` / `tail`）
> 之后，一次两跳扩展只要几毫秒。
> 换成 Neo4j 你得先起一个服务、配好连接、学一套 Cypher 语法，
> 然后发现它跑得还没 SQLite 快。
>
> **规模没到之前，图数据库是纯负担。** 这句话对绝大多数"我们也要上 GraphRAG"的场景都成立。

**环境检查**（沿用阶段 07/08 的 `.env`）：

```bash
cd backend
# 确认这三样都还在
cat .env | grep -E "DEEPSEEK|HF_ENDPOINT"
ls .chroma          # 阶段 07 的向量库
```

图谱数据落在 `backend/.graph.db`（SQLite 文件），第一次构建时自动创建。

> ⚠️ **`.graph.db` 要加进 `.gitignore`**。它是"跑出来的产物"，
> 不是源代码——和 `.chroma/` 一样，提交它只会让仓库变大且充满冲突。

---

## 3. 核心概念

### 3.1 三元组：把句子拆成"头—关系—尾"

一张知识图谱的最小单位是**三元组**（triple）：

```
(RAG 进阶, 包含, 重排)
(重排, 属于, RAG 进阶)
(MCP 协议开发, 使用, MCP 协议)
```

每个三元组就是图上的一条**有向边**。整张图 = 一堆三元组的集合。

注意这里有个容易被忽略的事实：**三元组不是从文本里"读"出来的，是"抽"出来的。**
原文可能写的是：

> 本阶段在阶段 07 的基础上引入 RRF 融合与 Cross-encoder 重排。

抽成三元组就是：

```
(RAG 进阶, 包含, RRF 融合)
(RAG 进阶, 包含, Cross-encoder 重排)
(RAG 进阶, 依赖, RAG 基础)
```

**这是一次有损压缩。** 原文的语气、条件、限定词全丢了，只剩下骨架。
但正是这个骨架让"跨块关系"变得可查询——这是这个 trade 的收益。

### 3.2 为什么需要封闭词表

如果让模型自由发挥，同一件事会被抽出四种边：

```
RAG 进阶 —依赖→ RAG 基础
RAG 进阶 —基于→ RAG 基础
RAG 进阶 —使用了→ RAG 基础
RAG 进阶 —需要→ RAG 基础
```

四个节点、四条边，说的是同一件事。图会立刻变成一团毛线——
**你想查"谁依赖 RAG 基础"，只能查到四分之一。**

所以给模型一个**封闭词表**：

```python
ENTITY_TYPES = ["阶段", "技术", "概念", "参数", "文档", "工具"]
RELATION_HINTS = ["依赖", "使用", "属于", "包含", "对比", "导致", "产出", "是"]
```

并要求它"优先用这些词，确实不合适时才自造"。

> 这本质上和阶段 04 的 `enum` 字段是同一招：
> **给模型一个有限的选项集，输出就变得可聚合。**
> 自由的字符串没法 `GROUP BY`，受控的词表可以。

实测下来词表覆盖得相当好，自造的关系词只有少数几个：

```
包含 152 · 使用 57 · 是 32 · 属于 27 · 用于 19 · 遵循 4 · 对比 4 · 面向 3 · 依赖 3 · 作为 3
```

### 3.3 抽取是"入库时"的一次性成本

这是本阶段最重要的工程判断：

```
抽取   每个块一次 LLM 调用     ← 贵，但只付一次
检索   一次 SQLite 查询        ← 便宜，每次查询都付
```

和阶段 07 的嵌入是**完全同一个模式**：

| | 入库时（贵，一次性） | 查询时（便宜，每次） |
|---|---|---|
| 阶段 07 | 嵌入：每块算一次向量 | 向量近邻搜索 |
| 阶段 09 | 抽取：每块跑一次 LLM | 按端点查边 |

**为什么这件事必须被看见？** 因为它的成本量级和嵌入完全不是一个概念：

```
嵌入     本地 ONNX 模型，一块几毫秒，不要钱
抽取     一次 LLM 调用，一块约 19 秒，要钱
```

实测单块抽取耗时 **约 19 秒**。我们这份语料有 **412 个块**：

```
412 块 × 19s ≈ 2.2 小时（串行）
```

这不是"跑一下试试"的量级。所以本阶段的设计里，
**`limit` 是一个必须存在的参数，而不是可选项**——
你必须能控制这笔开销，而且必须**亲眼看到**它有多大。

### 3.4 并发：抽取是 IO 密集，块与块完全独立

好消息是抽取这件事**天然可并行**：每个块独立抽，互不依赖，绝大部分时间在等网络。

```python
with ThreadPoolExecutor(max_workers=6) as pool:
    futures = {pool.submit(extract_triples, client, row["text"]): row for row in pending}
```

实测（12 块）：

```
串行      约 19.0s / 块
并发 6    约  4.08s / 块      ← 约 4.7 倍加速
```

（没到 6 倍是因为主线程还要串行落库，以及尾部几个块凑不满并发。）

**并发的边界划在这里：抽取并行、落库串行。**

```python
for fut in as_completed(futures):
    row = futures[fut]
    triples, _raw = fut.result()          # 网络部分：并行
    _save(conn, triples, row["title"], row["index"])   # SQLite 写入：串行
    conn.commit()
```

两个理由：SQLite 的写操作不该被多线程同时打（会锁库），
而且 `as_completed` 这个循环本身就跑在主线程里，顺手写进去最省事也最安全。

> **单块失败不该毁掉整次构建。** 一次长跑任务里，网络抖动、模型返回超时都是常态：
>
> ```python
> try:
>     triples, _raw = fut.result()
> except Exception:
>     continue      # 跳过它，继续下一个
> ```
>
> 这是"长跑任务"的通用纪律：**部分失败要能被容忍**，
> 否则 412 个块跑到第 400 个挂掉，你会想把电脑砸了。

### 3.5 每条边都必须带出处

```sql
CREATE TABLE edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    head         TEXT NOT NULL,
    relation     TEXT NOT NULL,
    tail         TEXT NOT NULL,
    source_title TEXT NOT NULL,      -- ⚠️ 这两列不能省
    source_index INTEGER NOT NULL,
    UNIQUE(head, relation, tail, source_title, source_index)
);
```

**没有出处的三元组就是"没有引用的断言"。** 模型看到 `(RAG 进阶, 包含, 重排)`
会直接把它当事实写进答案，而它可能抽错了——比如把"阶段 08 **不**包含重排"
里的否定漏掉了。

带上 `source_title` + `source_index`，就能在检索时把边**还原成原文**：

```
【知识图谱中的关系】
- RAG 进阶 —包含→ 重排　｜ 出处：DESIGN#7　[★]

【相关原文】（用于核实上面的关系，不要编造图谱里没有的关系）
[资料1] DESIGN#7
| 07 | RAG 基础（向量检索） | 主线 |
| 08 | RAG 进阶（混合检索/重排） | 主线 |
```

**边让模型看清关系，原文让它有据可依——两个都要给。**
只给边，模型会顺着关系链自由发挥；只给原文，那就退回成阶段 07 的向量 RAG 了。

> 注意 `UNIQUE` 里包含 `source_title, source_index`：
> 同一个三元组从不同块抽出来是**两条边**（两条证据），不是重复。

### 3.6 检索：锚定 → 扩展 → 回溯源块

```
问题："重排是哪个阶段引入的？"
  ↓ ① 锚定   在问题里找到图上已有的实体  → ["重排"]
  ↓ ② 扩展   从"重排"出发走 2 跳
             RAG 进阶 —包含→ 重排      (1 跳)
             RAG 进阶 —属于→ 主线      (2 跳)
  ↓ ③ 回溯   把边还原成源块            → [DESIGN#7, DESIGN#6]
  ↓ ④ 拼 prompt → 生成
```

**① 锚定用最长优先的子串匹配**，而不是再上一次向量检索：

```python
names.sort(key=len, reverse=True)          # 最长优先
for name in names:
    start = q.find(name.lower())
```

最长优先是为了避免"阶段 07"被拆成"阶段" + "07"两个无意义的锚点。

**② 扩展是"有向双向"的**——既走 `head→tail`，也走 `tail→head`：

```sql
SELECT * FROM edges WHERE head=? OR tail=? ORDER BY id
```

因为"谁依赖我"和"我依赖谁"都是有效的关系。只走一个方向会漏掉一半。

**③ 回溯按"被多少条边引用"排序**——出现得越频繁的块越可能是枢纽：

```python
weight[key] = weight.get(key, 0) + 1
ranked = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
```

### 3.7 跳数是一个 trade-off，不是"越大越好"

这是本阶段最容易被调错的参数。同一个问题，只改 `hops`：

```
问：MCP 协议开发用了什么协议？

hops=1    3 节点 /  3 边    精确，覆盖窄
hops=2    4 节点 /  6 边    能跨块连出新关系
hops=3   22 节点 / 32 边    ← 噪声暴涨 5 倍
```

3 跳那一次，子图里多出来的 19 个节点全是"××—属于→ 主线"——
因为"主线"是个超级枢纽（度数 18），一跳过去就把**所有阶段**都拉进来了。

```
1 跳   精确但覆盖窄
2 跳   能跨块连出新关系，噪声开始出现
3 跳   关系链很长，但信噪比已经崩了
```

**默认值取 2。** 这不是"越大越全"——**多出来的边会稀释真正的证据**，
模型在 32 条边里找那 1 条相关的，比在 3 条里找要难得多。

> 顺带说：这也是为什么**全图必须截断**。
> 320 个节点画在屏幕上只是一团毛线，**信息量为零**。
> 前端只按连接度取最枢纽的 40 个节点（及其之间的 47 条边），图才"能看懂"。

---

## 4. 动手实现

### 4.1 后端 · 抽取：`graph.py`

沿用阶段 04 的结论：`response_format={"type":"json_object"}` **只是倾向**，不是保证。
所以照样做 Pydantic 校验 + 一次自纠重试：

```python
class Entity(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    type: str = "概念"

class Relation(BaseModel):
    head: str = Field(min_length=1, max_length=40)
    relation: str = Field(min_length=1, max_length=20)
    tail: str = Field(min_length=1, max_length=40)

class TripleOut(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


def extract_triples(client, text, model=None) -> tuple[TripleOut, str]:
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": "文本：\n" + text.strip()},
    ]
    for _attempt in (1, 2):
        raw = client.chat(
            messages,
            model=model,
            temperature=0.1,        # 抽取是事实任务，温度压到最低
            response_format={"type": "json_object"},
        )
        try:
            return TripleOut.model_validate_json(raw), raw
        except ValidationError as ve:
            hint = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in ve.errors()
            )
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"上次响应未通过校验：{hint}\n请修正并只输出合法 JSON。"},
            ]
    return TripleOut(), raw     # 两次都不行就放弃这一块，不要让它毁掉整次构建
```

### 4.2 后端 · 落库：一个 `rowcount` 陷阱

```python
def _save(conn, triples, title, index) -> tuple[int, int]:
    """落库。返回 (新增实体数, 新增边数)。"""
    new_e = new_r = 0

    def upsert_entity(name: str, etype: str) -> None:
        nonlocal new_e
        # ⚠️ SQLite 的 `INSERT ... ON CONFLICT DO UPDATE` 无论走插入还是更新，
        # `rowcount` 都是 1 —— 靠它判断"是不是新实体"会**全部算成新增**。
        # 所以这里显式先 SELECT 一次。多一次查询，换一个可信的计数。
        exists = conn.execute("SELECT 1 FROM entities WHERE name=?", (name,)).fetchone()
        if exists:
            conn.execute("UPDATE entities SET mentions=mentions+1 WHERE name=?", (name,))
        else:
            conn.execute("INSERT INTO entities(name, type, mentions) VALUES(?,?,1)", (name, etype))
            new_e += 1
    ...
```

**为什么要在意这个计数？** 因为它出现在前端上，而错误的数字会直接误导人：
"本轮抽了 12 块，新增 211 个实体"——如果实际只有 40 个是新的，
你会对图的规模产生完全错误的印象。

### 4.3 后端 · 实体名归一化

```python
def _normalize(name: str) -> str:
    """教学版只做最朴素的两件事：去空白、压掉首尾标点。"""
    return name.strip().strip("`*·。，、：:（）()「」《》\"'").strip()
```

真实项目这里会是一整套别名表 / 同义词归并——**图的可用性几乎全押在这一步**：
"阶段 07" 和 "07 阶段" 如果被当成两个节点，图就散了。

> **⚠️ 一个真实的语法坑**：注释里如果写 `（"它""这个"不算实体）`，
> 那两个双引号会**截断字符串字面量**。中文文档里写代码注释时，
> 内部引号一律用「」或单引号。

### 4.4 后端 · 建表：惰性初始化必须加锁

`_init()` 建 4 张表 + 2 个索引，`_connect()` 里只在进程内第一次跑：

```python
_ready = False
_init_lock = threading.Lock()

def _connect() -> sqlite3.Connection:
    global _ready
    GRAPH_DB.parent.mkdir(parents=True, exist_ok=True)

    if not _ready:
        with _init_lock:              # ⚠️ 双检锁
            if not _ready:
                boot = sqlite3.connect(GRAPH_DB)
                try:
                    boot.execute("PRAGMA journal_mode=WAL")
                    _init(boot)
                finally:
                    boot.close()
                _ready = True

    conn = sqlite3.connect(GRAPH_DB)
    conn.row_factory = sqlite3.Row
    return conn
```

**为什么必须加锁？** 这不是理论担忧，是浏览器实测抓到的现场：

> 前端首屏会并发发出 4 个请求（stats / documents / overview / entities），
> FastAPI 把它们丢进线程池，于是 4 个线程**同时**看到 `_ready == False`，
> 同时去执行 `PRAGMA journal_mode=WAL`。
> 而**切换日志模式需要排他锁**，于是其中一个抛 `database is locked`，
> `/api/graph/stats` 直接 500。
>
> 前端那边的表现是：**状态条全是 0，而且不报错。** 最难查的那类 bug。

### 4.5 后端 · 锚定：类型词和关系词都不当锚点

第一版实现直接把所有实体名拿去做子串匹配，结果：

```
问：阶段 08 依赖哪些阶段？
锚定结果：["依赖", "阶段"]        ← 两个都是废话
```

- `依赖` 是**关系词**（图上它凑巧也是个名词实体："uv —管理→ 依赖"）
- `阶段` 是**类型词**（度数 18，什么都连，但什么都没说）

修法很直白：

```python
# ⚠️ 类型词和关系词都不当锚点。
# 教训是：**词表里的词是"结构"，不是"内容"**，锚定只该认内容。
if name in RELATION_HINTS or name in ENTITY_TYPES:
    continue
```

改完之后：

```
问：设计计划里定义了哪些颜色？
锚定结果：["设计计划"]        ← 干净
```

### 4.6 后端 · 一个"连接比查询贵"的实测

`graph_search` 返回三段耗时，方便定位：

```python
return {
    "seeds": seeds, "nodes": ..., "edges": ..., "chunks": chunks,
    "timings": {"anchor": ..., "expand": ..., "trace": ...},
}
```

实测下来，`anchor` 一直是 80–145ms，看起来像是"锚定算法慢"。但拆开测之后：

```
sqlite3.connect()           约 70ms   ← 真正的大头
SELECT name FROM entities    0.33ms
20 次按端点查边               0.93ms
两跳 expand                  6.8ms
```

**查询本身快到可以忽略，成本全在"开连接"上**（本机是 Windows + 非系统盘，
每次打开文件都要过一遍安全扫描）。

所以正确的下一步**不是优化 SQL，而是复用连接**——按线程各持一个
（`threading.local()`），或者干脆换 PostgreSQL 走连接池。

教学版故意留着这一层不抽象，是为了让"**连接开销 > 查询开销**"这件事被看见。
真实项目里它就该被池化掉。

### 4.7 后端 · 路由：读用 POST，写用 SSE

```python
GET  /api/graph/stats       实体数 / 边数 / 覆盖率 / 类型分布 / 关系词分布
GET  /api/graph/entities    实体清单（按提及次数排序，带 degree）
GET  /api/graph/documents   语料文档 + 每篇已抽多少块（前端用来勾选构建范围）
GET  /api/graph/overview    全图快照（按度数截断）
POST /api/graph/build       同步构建（小批量）
POST /api/graph/build/stream  **流式构建**（边抽边推进度）
POST /api/graph/search      纯图谱检索（不调 LLM）
POST /api/graph/ask         图谱问答（检索 → 拼 prompt → 生成）
POST /api/graph/reset       清空图谱
```

这一组接口和 RAG 那组的**结构差异**本身就是知识点：

```
检索（search / ask）  读   几十毫秒   普通 POST 就够
构建（build）        写   几分钟     必须走 SSE 把进度推出来
```

**SSE 这里有个必须绕开的坑。** 阶段 03 的 SSE 之所以能直接 `yield`，
是因为 `client.stream()` 本身就是个生成器。而 `graph.build()` 是**阻塞函数**：

```python
def event_gen():
    yield sse({"phase": "start"})        # ← 这一帧是立即发出去的
    result = graph.build(...)            # ← 但这里会卡住几分钟
    yield sse({"phase": "done"})         # ← 所以进度会"在结束的瞬间一起涌出来"
```

**等于没有进度。** 正确做法是把 `build` 丢进后台线程，用 `queue.Queue` 把进度搬回生成器：

```python
frames: queue.Queue = queue.Queue()

def event_gen():
    outcome: dict = {}

    def worker():
        try:
            outcome["result"] = graph.build(
                client, limit=req.limit, model=req.model, workers=req.workers,
                titles=req.titles,
                on_progress=lambda done, total, key: frames.put({
                    "phase": "progress", "done": done, "total": total,
                    "chunk": key, **graph.counts(),
                }),
            )
        except Exception as e:
            outcome["error"] = str(e)
        finally:
            frames.put(None)          # 哨兵：告诉消费端"没有更多了"

    yield sse({"phase": "start", "total": req.limit})
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while True:
        item = frames.get()
        if item is None:
            break
        yield sse(item)
    thread.join()
    ...
```

实测的帧到达时间（8 块）：

```
[   4.8s] {"phase": "start", "total": 8}
[   4.8s]  1/ 8 块  当前 design-system#5   累计 211 实体 / 217 边
[  12.0s]  2/ 8 块  当前 design-system#3   累计 218 实体 / 224 边
[  15.4s]  3/ 8 块  当前 design-system#2   累计 239 实体 / 244 边
[  23.2s]  4/ 8 块  当前 design-system#6   累计 257 实体 / 265 边
[  23.7s]  5/ 8 块  当前 design-system#7   累计 268 实体 / 280 边
[  23.8s]  6/ 8 块  当前 design-system#1   累计 283 实体 / 296 边
[  29.5s]  7/ 8 块  当前 design-system#8   累计 289 实体 / 304 边
[  41.5s]  8/ 8 块  当前 design-system#4   累计 308 实体 / 312 边
[  41.8s] {"phase": "done", "processed": 8, "entities": 109, "edges": 118, "seconds": 41.59}
```

**时间戳是逐条推进的**——这才叫进度条。它也是
"抽取是入库时的一次性成本"这句话**唯一的证据**。

> 用 curl 单独测首帧延迟：`TTFB=0.168s`。服务端确实是立即推第一帧的。

### 4.8 后端 · ⚠️ 一个跨阶段的并发 bug（真被浏览器抓出来了）

浏览器实测时发现：**首屏状态条全是 0，但不报错。**

一路查下去，根因不在本阶段的代码里，而在阶段 07 的 `rag.py`：

```python
# 问题代码（阶段 07 版本）
_client: chromadb.ClientAPI | None = None

def get_collection():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))   # ← 没有锁
    return _client.get_or_create_collection(...)
```

`chromadb.PersistentClient` 构造时会去操作一个**进程级共享注册表**
（`SharedSystemClient._identifier_to_system`），而这个注册表**不是线程安全的**。
首屏 4 个并发请求同时第一次调用它，实测抛的是：

```
KeyError: 'E:\...\backend\.chroma'
AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'
```

于是 `graph.stats()` 里的 `rag.corpus()` 炸了 → `/api/graph/stats` 500 →
前端 `stats.value` 保持 `null` → 模板里的 `stats?.entities ?? 0` 渲染出 `0`。

**修法是给所有惰性单例加双检锁**（`get_collection` / `get_embedder` / `get_reranker`
三处都有同样的问题）：

```python
_collection_lock = threading.Lock()

def get_collection() -> chromadb.Collection:
    global _client, _collection
    if _collection is None:
        with _collection_lock:
            if _collection is None:
                CHROMA_DIR.mkdir(parents=True, exist_ok=True)
                if _client is None:
                    _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
                _collection = _client.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"},
                )
    return _collection
```

> **这一条要记牢：凡是"第一次调用会构造全局单例"的惰性初始化，都要当成并发入口来处理。**
>
> 为什么阶段 07/08 没暴露？因为那两个页面的首屏请求恰好是串行的
> （`useRag` 先 `loadStatus`，再等用户点检索）。
> 阶段 09 的页面为了"一进来就能看到图的规模"，用了 `Promise.all` 并发拉 4 个接口，
> **于是把一个潜伏了两阶段的 bug 踩了出来。**

回归测试（6 个并发首屏请求，全新进程）：

```
结果: {'stats': 320, 'documents': 11, 'overview': 40, 'entities': 320,
       'rag_corpus': 412, 'rag_search': 4}
异常: 无 ✓
```

### 4.9 前端 · 力导向图：手写，不引 d3

**为什么不引 d3-force？** 引一个只要几十 KB，但这一阶段要讲的是"图长什么样"，
而"节点为什么摆在这个位置"恰恰是理解图的关键。六十行能讲清楚的事，
不值得藏进一个黑盒依赖里。

三个力就够了：

```ts
// 1. 斥力（所有节点两两）：O(n²)，节点少的时候完全够用
const f = 8000 / d2
pts[i].vx -= fx; pts[i].vy -= fy
pts[j].vx += fx; pts[j].vy += fy

// 2. 弹簧（每条边）：把有关系的节点拉到目标长度附近
const f = (d - L) * 0.045        // L = 148（边少）/ 82（边多）

// 3. 向心力：防止孤立节点被斥力推到画布外面去
p.vx += (W / 2 - p.x) * 0.010
p.vy += (H / 2 - p.y) * 0.020
```

**初始位置用"按名字排序后的圆环"，不是随机撒点：**

```ts
const ordered = [...nodes].sort((a, b) => a.name.localeCompare(b.name, 'zh'))
const ang = (i / ordered.length) * Math.PI * 2
```

**同一份数据每次渲染出来的图必须一模一样**，否则没法截图对比、没法写文档。
同理，节点完全重合时给的"微推"也必须确定：

```ts
// 不要用 Math.random —— 那会破坏可复现性
const ang = ((i * 12.9898 + j * 78.233) % 6.2832) as number
```

**视觉编码：**

| 元素 | 含义 |
|---|---|
| 圆的**颜色** | 实体类型（阶段 / 技术 / 概念 / 参数 / 文档 / 工具） |
| 圆的**大小** | 连接度（degree）——一眼看出谁是枢纽 |
| **琥珀色**边 | 1 跳直接命中 |
| **青绿**边 | 多跳连出来的 |
| 虚线**光环** | 锚定到的种子实体 |

**布局调参也是实测出来的：** 第一版用 `800×430` 的 viewBox，
但面板实际约 830×420（比例 2.0），`preserveAspectRatio="meet"` 把图缩到 0.82 倍，
**左右各空 88px**，图看起来"被挤在中间"。改成 `900×420` 就基本贴合了。

> 教训：**画布比例要和容器接近**。SVG 的 `viewBox` 不是随便定的数字，
> 它决定了图形在容器里被缩放多少。

### 4.10 前端 · 视图模式必须自动切回结果

浏览器实测踩到的第二个坑：

```
先点「全图（截断）」→ 再点「检索」
→ 图**还是那张全图**，结果子图没出来
```

原因很蠢：`viewMode` 停在 `'overview'` 没动。

```ts
// ❌ 第一版：viewMode 由用户手动切，检索完不切回来
const shown = computed(() => {
  if (viewMode.value === 'result' && result.value) return result.value
  return overview.value ?? { nodes: [], edges: [] }
})
```

**用户点了"检索"，看到图没变，会直接以为功能坏了。**
结果出来了就该显示结果，这是默认预期，不该靠用户再点一次：

```ts
result.value = await call<GraphSearchResult>('/graph/search', {...})
viewMode.value = 'result'    // ← 检索完就切回子图
```

> 这是"**默认行为要匹配用户预期**"的典型例子。
> 交互上每一个需要用户"再补一步"的地方，都是一个潜在的"功能坏了"的误判。

### 4.11 前端 · 构建范围：让 GraphRAG 变得可用的一招

全量抽 412 块要两个多小时。**但没人真的需要抽完整套文档。**

前端把语料按文档列成可勾选的 chip，并显示每篇的进度：

```
00-环境准备 0/13    01-大模型API编程基础 0/18   ...   DESIGN 11/11   design-system 9/9
```

勾上 `DESIGN` + `design-system`（共 20 块）→ 1~2 分钟建出一张能用的图。

**先抽你真正关心的那几篇，图小、跑得快、噪声也少。**
这是把 GraphRAG 从"跑一夜"变成"跑两分钟"的关键操作。

---

## 5. 运行验证

```bash
# 终端 1
cd backend
uv run uvicorn main:app --reload --port 8000

# 终端 2
cd frontend
pnpm dev
```

打开 `http://localhost:5173/graph`。

### 5.1 不需要 Key 就能看的部分

图谱的**读取**和模型是解耦的（和 MCP 目录、知识库状态一样）：

```bash
curl -s http://127.0.0.1:8000/api/graph/stats
```

```json
{"entities":320,"edges":335,"extractedChunks":20,"totalChunks":412,"coverage":0.0485,
 "types":[{"type":"概念","count":151},{"type":"技术","count":41},{"type":"工具","count":37},
          {"type":"文档","count":35},{"type":"阶段","count":28},{"type":"参数","count":28}],
 "relations":[{"relation":"包含","count":152},{"relation":"使用","count":57},
              {"relation":"是","count":32},{"relation":"属于","count":27},{"relation":"用于","count":19}],
 "entityTypes":["阶段","技术","概念","参数","文档","工具"],
 "relationHints":["依赖","使用","属于","包含","对比","导致","产出","是"],
 "lastBuiltAt":"2026-09-29T02:25:45"}
```

> **`coverage` 是这里最该看的一个数：4.85%。**
> 它直接把本阶段的成本模型摆出来——覆盖率不是 100% 时，
> **"图里没有"和"语料里没有"是两件事**，回答时必须分清楚。

### 5.2 流式构建：亲眼看一次"一次性成本"

```bash
curl -N -X POST http://127.0.0.1:8000/api/graph/build/stream \
  -H 'Content-Type: application/json' \
  -d '{"limit":8,"workers":6,"titles":["design-system"]}'
```

实测输出见 §4.7：8 块耗时 **41.59s**，新增 **109 实体 / 118 边**。

平均 **5.2s/块**（含尾部凑不满并发的损耗）——
对照 §3.4 的"串行 19s/块"，这就是并发的价值。

### 5.3 跳数对比：同一个问题，三种 hops

```bash
for H in 1 2 3; do
  curl -s -X POST http://127.0.0.1:8000/api/graph/search \
    -H 'Content-Type: application/json' \
    -d "{\"query\":\"MCP 协议开发用了什么协议？\",\"hops\":$H,\"top_k\":3}"
  echo
done
```

| hops | 节点 | 边 | 子图里多出来的东西 |
|---|---|---|---|
| 1 | 3 | 3 | —— |
| 2 | 4 | 6 | `RAG 基础 —属于→ 主线` |
| 3 | **22** | **32** | 全是"××—属于→ 主线"，**噪声涨了 5 倍** |

### 5.4 前端验证清单（逐项实测）

打开 `http://localhost:5173/graph`：

- [x] 顶栏标题变成「阶段 09 · GraphRAG 知识图谱」，左侧进度轨第 09 格高亮
- [x] 状态条：**320 实体 / 335 关系边 / 4.9% 覆盖率 20 / 412 块** + 六类实体分布
- [x] 琥珀色警示条："覆盖率只有 4.9%……「图里没有」不等于「语料里没有」"
- [x] 构建面板列出 11 篇文档的可勾选 chip，每篇带 `已抽/总数`；DESIGN 与 design-system 显示满进度
- [x] 点「全图（截断）」→ 图上出现 **40 节点 / 47 边**，枢纽节点（主线 / 独立练习 / 技术栈基线）圆明显更大
- [x] 点预设「跨文档两跳」+ 2 跳 + 检索 → 锚定实体 `["设计计划"]`；图变成 **15 节点 / 15 边**，
      其中 **4 条琥珀（1 跳）+ 11 条青绿（多跳）**，边上标着关系词「包含」
- [x] 同一问题切到 **1 跳** → 图缩到 **5 节点 / 4 边**，
      **颜色节点全部消失**——直观演示"1 跳够不到"
- [x] 点预设「锚定失败」（阶段 08 依赖哪些阶段？）→ 锚定为空，提示"整条链路空转"，
      **图变成 0 节点 / 0 边**
- [x] 关系链面板把边分成两组：`1 跳 · 直接命中（4）` 与 `多跳 · 跨块连出来的（11）`，
      **每条边都标着出处**（前者全是 `DESIGN#9`，后者跨到了 `design-system#2`）
- [x] 源块面板列出 3 段回溯到的原文，标着"6 条边引用 / 4 条边引用 / 3 条边引用"
- [x] 点「问模型」→ 得到带依据的回答；点「展开实际发给模型的 prompt」能看到边 + 原文的完整拼接
- [x] 实体清单（`<details>`）可展开，300 个实体带类型色点、提及次数、度数；回车可过滤
- [x] 控制台**没有任何报错**

### 5.5 实测记录：旗舰问题「设计计划里定义了哪些颜色？」

这个问题是**故意挑的**——它的答案**不在任何一段文本里**：

```
DESIGN#9        只写"设计计划包含 调色板/字体/布局/签名元素"
design-system#2 只写"调色板包含 Ink/Panel/Paper/Muted/Signal/Data"
```

**没有任何一块同时写着"设计计划"和"Ink"。** 但图上有一条两跳路径：

```
设计计划 —包含→ 调色板   ｜ 出处 DESIGN#9         [1 跳]
调色板 —包含→ Ink        ｜ 出处 design-system#2   [2 跳]
调色板 —包含→ Panel      ｜ 出处 design-system#2   [2 跳]
调色板 —包含→ Paper      ｜ 出处 design-system#2   [2 跳]
调色板 —包含→ Muted      ｜ 出处 design-system#2   [2 跳]
调色板 —包含→ Signal     ｜ 出处 design-system#2   [2 跳]
调色板 —包含→ Data       ｜ 出处 design-system#2   [2 跳]
```

模型拿到的 prompt：

```
【知识图谱中的关系】（格式：头实体 —关系→ 尾实体 ｜ 出处）
- 设计计划 —包含→ 调色板　｜ 出处：DESIGN#9　[★]
- 设计计划 —包含→ 字体　｜ 出处：DESIGN#9　[★]
- 设计计划 —包含→ 布局　｜ 出处：DESIGN#9　[★]
- 设计计划 —包含→ 签名元素　｜ 出处：DESIGN#9　[★]
- 设计语言 —包含→ 调色板　｜ 出处：DESIGN#10　[2跳]
- 调色板 —包含→ Ink　｜ 出处：design-system#2　[2跳]
- 调色板 —包含→ Panel　｜ 出处：design-system#2　[2跳]
- 调色板 —包含→ Paper　｜ 出处：design-system#2　[2跳]
- 调色板 —包含→ Muted　｜ 出处：design-system#2　[2跳]
- 调色板 —包含→ Signal　｜ 出处：design-system#2　[2跳]
- 调色板 —包含→ Data　｜ 出处：design-system#2　[2跳]
...

【相关原文】（用于核实上面的关系，不要编造图谱里没有的关系）
[资料1] design-system#2
## 3. 调色板（4–6 命名 hex）
| 名称 | Hex | 用途 |
|---|---|---|
| Ink | `#0E1116` | 背景，带蓝调的墨黑（非纯黑） |
...
```

模型回答（实测原文）：

> 根据图谱关系链：
>
> - **设计计划 —包含→ 调色板**（出处：DESIGN#9　[★]）
> - **调色板 —包含→ Ink、Panel、Paper、Muted、Signal、Data**（出处：design-system#2　[2跳]）
>
> 因此，设计计划通过其包含的「调色板」定义了以下颜色；具体名称和 Hex 由原文
> `design-system#2` 核实：
>
> | 颜色名称 | Hex | 用途 |
> |---|---|---|
> | Ink | `#0E1116` | 背景，带蓝调的墨黑（非纯黑） |
> | Panel | `#161B22` | 卡片 / 面板 |
> | Paper | `#E6E9EF` | 正文文字 |
> | Muted | `#8B95A7` | 次要文字 / 边框 |
> | Signal | `#FFB454` | 琥珀高亮，唯一主强调色 |
> | Data | `#5BC8B0` | 青绿，用于链接 / 数据 / 流式光点（次级） |
>
> 补充说明：
> - `DESIGN#9` 原文只写到设计计划包含"调色板/字体/布局/签名元素"，**没有直接列出具体颜色**。
> - 具体颜色清单和 Hex 值依据的是 `design-system#2` 原文。

**注意模型最后那段"补充说明"**——它自己指出了：
`DESIGN#9` 里没有颜色清单。这正是"答案不在任何一段文本里"的证明，
而模型能意识到这一点，是因为 prompt 里**同时给了边和原文**，两者可以对账。

对照实验：同一个问题切到 **1 跳**，图上只剩 `设计计划 —包含→ 调色板/字体/布局/签名元素`
四条边，**一个颜色都够不到**。

### 5.6 实测记录：锚定失败长什么样

```
问：阶段 08 依赖哪些阶段？
锚定实体：（空）
子图：0 节点 / 0 边
```

这不是 bug，是**覆盖率 4.85% 的直接后果**——`阶段 08` 这个实体还没被抽进图里。

前端会显示：

> 图上没有这个问题的入口实体 —— **整条链路空转**。
> 这不是 bug，是图谱覆盖率（4.9%）的直接后果：
> 要么换个说法，要么先把相关文档抽进图里。

**"锚定失败"是 GraphRAG 最该被展示的一种结果**，因为它最容易被误读成
"语料里没有这个信息"。这两件事必须分清。

### 5.7 耗时明细

| 环节 | 实测 | 说明 |
|---|---|---|
| 实体锚定 | 80–145ms | 大头是 `sqlite3.connect()` 的 ~70ms，不是算法 |
| 多跳扩展 | 5–150ms | 同上；纯查询只要 6.8ms |
| 回溯源块 | **0.2–0.7ms** | 命中缓存后基本为零 |

### 5.8 构建验证

```bash
cd frontend
npx vue-tsc -b --force     # 0 错误
npx vite build
```

```
dist/assets/GraphPage-*.js    21.47 kB │ gzip: 10.08 kB
dist/assets/GraphPage-*.css   10.90 kB │ gzip:  2.16 kB
```

`GraphPage` 独立分包（懒加载），不进主 chunk。

---

## 6. 小结

**五个必须记住的结论：**

1. **图补的是"关系"，不是"相似"。**
   向量 / BM25 都在回答"哪段文字最像我"，而关系型问题的答案
   **不在任何一段文本里**——那条边需要被"建"出来。

2. **抽取是入库时的一次性成本，贵在前、便宜在后。**
   每块一次 LLM 调用（实测约 19s），但查询只是毫秒级 SQLite。
   **和阶段 07 的嵌入是同一个模式**，只是贵了几个数量级——
   所以 `limit` 和"按文档收窄范围"必须是可用的旋钮。

3. **跳数是 trade-off，不是"越大越好"。**
   1 跳精确但窄，2 跳能跨块，**3 跳噪声涨 5 倍**（3 条边 → 32 条边）。
   默认取 2。

4. **每条边都必须带出处。**
   没有出处的三元组是"没有引用的断言"。带上源块，才能把边还原成原文，
   让模型**对账**——§5.5 里模型主动指出"DESIGN#9 没有颜色清单"就是靠这个。

5. **封闭词表让图可读。**
   自由的关系词会把同一件事拆成四条边，"谁依赖 RAG 基础"就只能查到四分之一。
   给模型有限选项，输出才可聚合。

**本阶段踩到的三个真坑（都值得记）：**

| 坑 | 现象 | 教训 |
|---|---|---|
| `chromadb` 惰性单例无锁 | 首屏状态条全是 0，**不报错** | 凡"第一次调用构造全局单例"的初始化，都要当并发入口 |
| `PRAGMA journal_mode=WAL` 并发执行 | `database is locked` → 500 | 切换日志模式需要排他锁，双检锁是最省事的正确写法 |
| `viewMode` 不自动切回 | 检索后图不变，用户以为功能坏了 | 默认行为要匹配用户预期，别让用户"再补一步" |

**还有两条实测出来的工程经验：**

- **连接开销 > 查询开销**（70ms vs 0.33ms）——
  这时候优化 SQL 是白费力气，该做的是复用连接 / 上连接池。
- **SSE + 阻塞函数必须用"线程 + 队列"**，
  否则进度会"在结束的瞬间一起涌出来"，等于没有进度。

**本阶段的边界（必须说清楚）：**

- **锚定用的是子串匹配，不是语义匹配。** "技术栈"匹配不到"技术栈基线"（方向反了）。
  真实项目这一步通常也是向量检索——用嵌入找"问题最像哪个实体"。
- **实体归一化只做了最朴素的两件事**（去空白、压首尾标点）。
  真实项目这里是一整套别名表，而**图的可用性几乎全押在这一步**。
- **覆盖率只有 4.85%。** 这张图是"够用来讲清楚原理"的规模，不是"够用来问答"的规模。

---

## 7. 练习与验收

### 练习 A（必做）· 观察跳数

把同一个问题（比如"混合检索属于哪个阶段？"）在 `hops = 1 / 2 / 3` 下各跑一遍，
把 **节点数 / 边数 / 真正相关的边数** 记下来。

要求：写出一句话结论，说明你会把默认值定成几，为什么。

### 练习 B（必做）· 制造一次锚定失败

故意问一个图上没有实体的问题（比如"这门课用什么构建工具？"），
记录锚定结果和子图规模。

要求：解释为什么"锚定失败"和"语料里没有"是两件不同的事，
并给出两种不同的处理方式。

### 练习 C（必做）· 把 `limit` 拉大，观察成本

把 `limit` 从 8 改成 24，`workers` 保持 6，跑一次 `/api/graph/build/stream`，
记录总耗时与 `processed` 块数。

要求：算出实际的平均 `秒/块`，并与串行（19s/块）对比，写出加速比。
思考：如果 `workers` 从 6 提到 12，加速比会翻倍吗？为什么不会？

### 练习 D（选做）· 改词表

往 `ENTITY_TYPES` 里加一个类型（比如"服务"），或往 `RELATION_HINTS` 里加一个关系词
（比如"替代"），重新抽一两篇文档。

要求：观察新词表是否真的被用上了，以及**旧的边有没有被重新抽**（提示：想想 `extracted` 表）。

### 练习 E（选做）· 给图谱加一个"最短路径"查询

现在的检索是 BFS 扩展，返回的是"N 跳内的所有边"。
试着实现一个 `shortest_path(a, b)`，回答"两个实体之间隔着几跳、经过哪些边"。

要求：用双向 BFS（从两端同时扩），并说明为什么它比单向 BFS 快。

### 验收标准

- [ ] `uv run uvicorn main:app --reload --port 8000` 能起，`/api/graph/stats` 返回非空
- [ ] `/api/graph/build/stream` 的进度帧是**逐条推进**的，不是结束时一次性涌出
- [ ] `/api/graph/search` 在 `hops=1` 与 `hops=2` 下返回**不同的子图规模**
- [ ] `/graph` 页面上：全图（截断）能画出来，枢纽节点明显更大
- [ ] 检索后**图会自动切回子图**（不用手动点"检索子图"）
- [ ] 关系链面板里**每条边都有出处**
- [ ] 「问模型」的回答里**每一条结论都注明了依据**
- [ ] 浏览器控制台**无报错**，首屏状态条数字**不是 0**
- [ ] `npx vue-tsc -b --force` 零错误，`npx vite build` 通过
