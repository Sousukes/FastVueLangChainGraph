"""阶段 09 · GraphRAG：把语料抽成一张知识图谱，用**关系**来检索。

## 为什么还需要一张图

阶段 07/08 把「找相似的文本」这件事做到了相当好，但它们回答的是同一个问题：
**哪一段文字和我的问题最像？**

有一类问题它天然答不好——**关系型 / 多跳问题**：

    问：「阶段 08 依赖哪些阶段？」
    语料里没有任何一块同时写着"阶段 08 依赖阶段 07"。
    阶段 08 的块讲 RRF 与重排，阶段 07 的块讲切块与嵌入，两者**各自都对**，
    但它们之间的**那条边**从来没有人写过。

向量检索只会返回"最像"的那一块，永远拼不出这条边。
**因为那条边不在任何一段文本里——它需要被"建"出来。**

## 三个环节

    ① 抽取  chunk ──LLM──> (head, relation, tail) 三元组
    ② 建图  三元组落进 SQLite，边上记 source_chunk  →  **可溯源**
    ③ 检索  问题 ──锚定实体──> 沿边扩 N 跳 ──> 拿回「边 + 源块」──> 拼 prompt

## 三条工程约束（教程 §3 会展开）

1. **抽取是"入库时"的一次性成本**。每个块都要跑一次 LLM，333 个块就是 333 次调用。
   但查询时只是一次 SQLite 查询——**贵在前，便宜在后**，和阶段 07 的嵌入是同一个模式。
2. **边上必须带 source_chunk**。没有出处的三元组就是"没有引用的断言"，
   模型会把它当成事实写进答案。带上源块，才能把图上的边**还原成原文**。
3. **跳数是 trade-off**。1 跳精确但覆盖窄；2 跳能跨块连出新关系，但噪声也成倍增长。
   这不是"越大越好"的参数。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from pydantic import BaseModel, Field, ValidationError

import rag
from llm import LLMClient

GRAPH_DB = Path(__file__).resolve().parent / ".graph.db"

# 允许的实体类型与关系词表。**给模型一个封闭词表**是让图"可读"的关键——
# 否则同一件事会抽出「依赖 / 基于 / 使用了 / 需要」四种边，图会变成一团毛线。
ENTITY_TYPES = ["阶段", "技术", "概念", "参数", "文档", "工具"]
RELATION_HINTS = ["依赖", "使用", "属于", "包含", "对比", "导致", "产出", "是"]


# ---------- 抽取结果的数据契约（复用阶段 04 的思路：JSON 模式 + Pydantic 校验） ----------


class Entity(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    type: str = "概念"


class Relation(BaseModel):
    head: str = Field(min_length=1, max_length=40)
    relation: str = Field(min_length=1, max_length=20)
    tail: str = Field(min_length=1, max_length=40)


class TripleOut(BaseModel):
    """一次抽取的完整产出。Pydantic 在这里是**运行时护栏**，不是文档。"""

    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


EXTRACT_SYSTEM = (
    "你是知识图谱抽取引擎。从给定文本中抽取**实体**与**关系**，只输出 JSON。\n"
    "规则：\n"
    "1. 只抽文本里**明确写到**的事实，绝不推测、绝不补充背景知识；\n"
    "2. 实体是短名词（2–12 字），不要整句、不要代词（「它」「这个」不算实体）；\n"
    f"3. 实体 type 从这些里选：{'/'.join(ENTITY_TYPES)}；\n"
    f"4. 关系 relation 优先用这些词：{'/'.join(RELATION_HINTS)}，"
    "确实不合适时才自造一个（2–6 字）；\n"
    "5. head / tail 必须是**文本里出现过的**实体名；\n"
    "6. 文本没有可抽的内容时，返回空数组，不要硬凑。\n"
    '输出格式：{"entities":[{"name":"...","type":"..."}],'
    '"relations":[{"head":"...","relation":"...","tail":"..."}]}'
)


# ---------- SQLite 存储 ----------


_ready = False
_init_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    """开一个连接。**建表与 WAL 只在进程内第一次做，而且要加锁。**

    原先每个函数都是 `_connect()` + `_init(conn)`，而 `_init` 一次要 `executescript`
    六条语句（4 张表 + 2 个索引），每条 `CREATE ... IF NOT EXISTS` 都要先查一遍 schema——
    实测冷启动那一次要 69ms，纯属白花。加个进程级开关，这笔开销就只付一次。
    `PRAGMA journal_mode=WAL` 同理：它是**写进数据库文件**的持久属性，不必每次重设。

    ⚠️ 但"只做一次"这件事**必须加锁**，否则会引入一个很难查的并发 bug。
    浏览器实测抓到的现场：页面加载时前端并发发了 4 个请求（stats / documents /
    overview / entities），4 个线程同时看到 `_ready == False`，同时去执行
    `PRAGMA journal_mode=WAL`——**切换日志模式需要排他锁**，
    于是其中一个抛 `database is locked`，`/api/graph/stats` 直接 500。
    前端那边只表现为"状态条全是 0"，不报错也看不出原因，非常隐蔽。
    双检锁（先无锁判一次、加锁再判一次）是这里最省事的正确写法。

    ⚠️ 还要诚实说清楚：**去掉建表之后，单次检索并没有明显变快**（实测仍在 140ms 量级）。
    拆开测过之后原因很清楚——

        sqlite3.connect()      约 70ms   ← 真正的大头
        SELECT name FROM entities  0.33ms
        20 次按端点查边            0.93ms
        两跳 expand               6.8ms

    **查询本身快到可以忽略，成本全在"开连接"上**（本机是 Windows + 非系统盘，
    每次打开文件都要过一遍安全扫描）。所以正确的下一步不是优化 SQL，
    而是**复用连接**——按线程各持一个（`threading.local()`），
    或者干脆换 PostgreSQL 走连接池。教学版故意留着这一层不抽象，
    是为了让"连接开销 > 查询开销"这件事被看见；真实项目里它就该被池化掉。
    """
    global _ready
    GRAPH_DB.parent.mkdir(parents=True, exist_ok=True)

    if not _ready:
        with _init_lock:
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


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entities (
            name     TEXT PRIMARY KEY,
            type     TEXT NOT NULL DEFAULT '概念',
            mentions INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS edges (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            head         TEXT NOT NULL,
            relation     TEXT NOT NULL,
            tail         TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_index INTEGER NOT NULL,
            UNIQUE(head, relation, tail, source_title, source_index)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_head ON edges(head);
        CREATE INDEX IF NOT EXISTS idx_edges_tail ON edges(tail);
        -- 记录"哪些块已经抽过了"。有了它，构建可以**中断后续跑**，
        -- 不会因为一次网络抖动就要从头再来（333 次调用重来一遍是很痛的）。
        CREATE TABLE IF NOT EXISTS extracted (
            chunk_key TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()


def _set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def chunk_key(title: str, index: int) -> str:
    """块的稳定标识。用 `标题#段号` 而不是语料下标——
    下标会随语料增删而漂移，标题+段号不会。"""
    return f"{title}#{index}"


# ---------- ① 抽取 ----------


def extract_triples(
    client: LLMClient, text: str, model: str | None = None
) -> tuple[TripleOut, str]:
    """对一段文本抽三元组。返回 (校验后的结果, 原始响应)。

    沿用阶段 04 的结论：`response_format={"type":"json_object"}` **只是倾向**，
    不是保证。所以这里照样做 Pydantic 校验 + 一次自纠重试。
    """
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": "文本：\n" + text.strip()},
    ]
    last_raw = ""
    for _attempt in (1, 2):
        raw = client.chat(
            messages,
            model=model,
            temperature=0.1,  # 抽取是事实任务，温度压到最低
            response_format={"type": "json_object"},
        )
        last_raw = raw
        try:
            return TripleOut.model_validate_json(raw), raw
        except ValidationError as ve:
            # 把校验错误喂回去，让它自己改（阶段 04 的 self-correction）
            hint = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in ve.errors()
            )
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"上次响应未通过校验：{hint}\n请修正并只输出合法 JSON。",
                },
            ]
    return TripleOut(), last_raw


def _normalize(name: str) -> str:
    """实体名归一化。教学版只做最朴素的两件事：去空白、压掉首尾标点。

    真实项目这里会是一整套别名表 / 同义词归并——**图的可用性几乎全押在这一步**：
    "阶段 07" 和 "07 阶段" 如果被当成两个节点，图就散了。
    """
    return name.strip().strip("`*·。，、：:（）()「」《》\"'").strip()


def _save(conn: sqlite3.Connection, triples: TripleOut, title: str, index: int) -> tuple[int, int]:
    """落库。返回 (新增实体数, 新增边数)。

    注意：SQLite 的 `INSERT ... ON CONFLICT DO UPDATE` 无论走插入还是更新，
    `rowcount` 都是 1 —— 靠它判断"是不是新实体"会**全部算成新增**。
    所以这里显式先 SELECT 一次。多一次查询，换一个可信的计数。
    """
    new_e = new_r = 0
    known = {_normalize(e.name) for e in triples.entities}

    def upsert_entity(name: str, etype: str) -> None:
        nonlocal new_e
        exists = conn.execute("SELECT 1 FROM entities WHERE name=?", (name,)).fetchone()
        if exists:
            conn.execute("UPDATE entities SET mentions=mentions+1 WHERE name=?", (name,))
        else:
            conn.execute(
                "INSERT INTO entities(name, type, mentions) VALUES(?,?,1)", (name, etype)
            )
            new_e += 1

    for e in triples.entities:
        name = _normalize(e.name)
        if not name:
            continue
        upsert_entity(name, e.type if e.type in ENTITY_TYPES else "概念")

    for r in triples.relations:
        head, tail = _normalize(r.head), _normalize(r.tail)
        rel = _normalize(r.relation)
        # 只保留两端都有效的边——否则图上会出现一堆悬空的孤立点
        if not head or not tail or not rel or head == tail:
            continue
        # 关系里出现但 entities 里没声明的端点，补一个占位实体（mentions 不加）
        for endpoint in (head, tail):
            if endpoint not in known:
                conn.execute(
                    "INSERT OR IGNORE INTO entities(name, type, mentions) VALUES(?,?,0)",
                    (endpoint, "概念"),
                )
        cur = conn.execute(
            "INSERT OR IGNORE INTO edges(head, relation, tail, source_title, source_index) "
            "VALUES(?,?,?,?,?)",
            (head, rel, tail, title, index),
        )
        if cur.rowcount == 1:
            new_r += 1

    conn.execute(
        "INSERT OR IGNORE INTO extracted(chunk_key) VALUES(?)", (chunk_key(title, index),)
    )
    return new_e, new_r


# ---------- ② 构建 ----------


def build(
    client: LLMClient,
    limit: int = 24,
    model: str | None = None,
    workers: int = 6,
    titles: list[str] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    """遍历语料块抽三元组。

    `limit` 是**必须存在的**参数，不是偷懒：全量 333 个块意味着 333 次 LLM 调用。
    教学演示必须让人能控制这笔开销，而且要让人**亲眼看到**它有多大。

    `titles` 用来把范围收窄到指定文档——**这是让 GraphRAG 可用的关键一招**。
    真实项目里没人会为了回答"这门课的结构"去抽完整套文档；
    先抽你真正关心的那几篇，图小、跑得快、噪声也少。

    `workers` 是并发数。**这是本阶段最实用的一条工程经验**：
    抽取是典型的 IO 密集任务（等网络），块与块之间完全独立，
    串行跑 333 块要将近两小时；开 6 个并发就能压到二十分钟量级。
    实测单块耗时约 19s，所以串行跑 24 块 ≈ 7.6 分钟，并发后 ≈ 1.5 分钟。

    并发的边界划在这里：**抽取并行、落库串行**。
    SQLite 的写操作不该被多线程同时打（会锁库），而且 `as_completed`
    这个循环本身就跑在主线程里，顺手把结果写进去最省事也最安全。
    """
    rows = rag.corpus()
    if not rows:
        return {
            "processed": 0,
            "alreadyExtracted": 0,
            "remaining": 0,
            "entities": 0,
            "edges": 0,
            "chunks": 0,
        }

    conn = _connect()
    done = {r["chunk_key"] for r in conn.execute("SELECT chunk_key FROM extracted")}

    scope = [r for r in rows if not titles or r["title"] in set(titles)]
    todo = [r for r in scope if chunk_key(r["title"], r["index"]) not in done]
    pending = todo[:limit]
    remaining = len(todo) - len(pending)
    # 注意口径：alreadyExtracted 是**当前勾选范围内**已抽的块数，
    # 而 chunks 是**全局**已抽块数（覆盖率要用它）。混用这两个口径，
    # 前端会显示出自相矛盾的数字（"已抽 20 块，剩余 0 块，但范围里只有 9 块"）。
    scope_done = len(scope) - len(todo)

    new_e = new_r = 0
    processed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(extract_triples, client, row["text"], model): row for row in pending
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            row = futures[fut]
            if on_progress:
                on_progress(i, len(pending), chunk_key(row["title"], row["index"]))
            try:
                triples, _raw = fut.result()
            except Exception:  # noqa: BLE001
                # 单块失败不该毁掉整次构建——跳过它，继续下一个。
                # 这是"长跑任务"的通用纪律：部分失败要能被容忍。
                continue
            e, r = _save(conn, triples, row["title"], row["index"])
            new_e += e
            new_r += r
            processed += 1
            conn.commit()

    _set_meta(conn, "lastBuiltAt", datetime.now().isoformat(timespec="seconds"))
    conn.commit()
    result = {
        "processed": processed,
        "alreadyExtracted": scope_done,
        "remaining": remaining,
        "entities": new_e,
        "edges": new_r,
        "chunks": conn.execute("SELECT COUNT(*) c FROM extracted").fetchone()["c"],
    }
    conn.close()
    return result


# ---------- ③ 检索 ----------


def anchor_entities(query: str, limit: int = 6) -> list[str]:
    """在问题里锚定图上已有的实体。

    实现故意选最朴素的**最长优先子串匹配**，而不是再上向量检索：
    这一阶段要讲的是"图能做什么"，锚定方式不该抢戏。
    （真实项目这一步常常也是向量检索——用嵌入找"问题最像哪个实体"。）

    最长优先是为了避免"阶段 07"被拆成"阶段"+"07"两个无意义的锚点。
    """
    conn = _connect()
    names = [r["name"] for r in conn.execute("SELECT name FROM entities")]
    conn.close()

    names.sort(key=len, reverse=True)
    q = query.lower()
    found: list[str] = []
    taken: list[tuple[int, int]] = []  # 已被占用的字符区间

    for name in names:
        if len(name) < 2:  # 单字实体几乎全是噪声
            continue
        # ⚠️ 类型词和关系词都不当锚点。
        # 这是实测踩出来的：「阶段 08 依赖哪些阶段？」会被锚到「依赖」+「阶段」，
        # 而这两个都不是提问者想要的东西——「依赖」是关系词（图上它凑巧也是个名词实体），
        # 「阶段」是类型词（度数很高但什么都连）。
        # 教训是：**词表里的词是"结构"，不是"内容"**，锚定只该认内容。
        if name in RELATION_HINTS or name in ENTITY_TYPES:
            continue
        start = q.find(name.lower())
        while start != -1:
            end = start + len(name)
            if not any(s < end and start < e for s, e in taken):
                found.append(name)
                taken.append((start, end))
                break
            start = q.find(name.lower(), start + 1)
        if len(found) >= limit:
            break
    return found


def expand(seeds: list[str], hops: int = 2) -> dict:
    """从种子实体出发做 BFS，把 `hops` 跳内的边和节点都收回来。

    **注意这里是有向扩展**：既走 head→tail，也走 tail→head。
    因为"谁依赖我"和"我依赖谁"都是有效的关系，只走一个方向会漏掉一半。
    """
    conn = _connect()

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[int] = set()
    frontier = list(seeds)
    visited = set(seeds)
    hop_of: dict[str, int] = {s: 0 for s in seeds}

    for hop in range(1, hops + 1):
        if not frontier:
            break
        next_frontier: list[str] = []
        for name in frontier:
            rows = conn.execute(
                "SELECT * FROM edges WHERE head=? OR tail=? ORDER BY id", (name, name)
            ).fetchall()
            for row in rows:
                if row["id"] not in seen_edges:
                    seen_edges.add(row["id"])
                    edges.append(
                        {
                            "id": row["id"],
                            "head": row["head"],
                            "relation": row["relation"],
                            "tail": row["tail"],
                            "sourceTitle": row["source_title"],
                            "sourceIndex": row["source_index"],
                            "hop": hop,
                        }
                    )
                for endpoint in (row["head"], row["tail"]):
                    if endpoint not in visited:
                        visited.add(endpoint)
                        hop_of[endpoint] = hop
                        next_frontier.append(endpoint)
        frontier = next_frontier

    if visited:
        placeholders = ",".join("?" * len(visited))
        for row in conn.execute(
            f"SELECT name, type, mentions FROM entities WHERE name IN ({placeholders})",
            tuple(visited),
        ):
            nodes[row["name"]] = {
                "name": row["name"],
                "type": row["type"],
                "mentions": row["mentions"],
                "hop": hop_of.get(row["name"], 0),
                "isSeed": row["name"] in seeds,
            }
    conn.close()
    return {"nodes": list(nodes.values()), "edges": edges, "seeds": seeds}


def _chunk_lookup() -> dict[str, dict]:
    """(标题#段号) → 语料块。用来把图上的边**还原成原文**。"""
    return {chunk_key(r["title"], r["index"]): r for r in rag.corpus()}


def source_chunks(edges: Iterable[dict], top_k: int = 3) -> list[dict]:
    """把边回溯到源块，按"被多少条边引用"排序——**出现得越频繁的块越可能是枢纽**。"""
    lookup = _chunk_lookup()
    weight: dict[str, int] = {}
    for e in edges:
        key = chunk_key(e["sourceTitle"], e["sourceIndex"])
        if key in lookup:
            weight[key] = weight.get(key, 0) + 1
    ranked = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    return [{**lookup[k], "edgeCount": w} for k, w in ranked]


def graph_search(query: str, hops: int = 2, top_k: int = 3) -> dict:
    """GraphRAG 的检索：锚定 → 扩展 → 回溯源块。"""
    import time as _time

    t0 = _time.perf_counter()
    seeds = anchor_entities(query)
    t1 = _time.perf_counter()
    sub = expand(seeds, hops=hops)
    t2 = _time.perf_counter()
    chunks = source_chunks(sub["edges"], top_k=top_k)
    t3 = _time.perf_counter()

    return {
        "seeds": seeds,
        "nodes": sub["nodes"],
        "edges": sub["edges"],
        "chunks": chunks,
        "timings": {
            "anchor": round((t1 - t0) * 1000, 2),
            "expand": round((t2 - t1) * 1000, 2),
            "trace": round((t3 - t2) * 1000, 2),
        },
    }


def build_prompt(question: str, found: dict) -> str:
    """把「图上的边」+「边的出处原文」拼成 prompt。

    两个都要给：**边让模型看清关系，原文让它有据可依**。
    只给边，模型会顺着关系链自由发挥；只给原文，那又退回成阶段 07 的向量 RAG 了。
    """
    lines = [f"用户问题：{question}", ""]

    if found["edges"]:
        lines.append("【知识图谱中的关系】（格式：头实体 —关系→ 尾实体 ｜ 出处）")
        for e in found["edges"][:40]:
            mark = "★" if e["hop"] == 1 else f"{e['hop']}跳"
            lines.append(
                f"- {e['head']} —{e['relation']}→ {e['tail']}"
                f"　｜ 出处：{e['sourceTitle']}#{e['sourceIndex']}　[{mark}]"
            )
        lines.append("")
    else:
        lines.append("【知识图谱】没有找到与问题相关的实体或关系。")
        lines.append("")

    if found["chunks"]:
        lines.append("【相关原文】（用于核实上面的关系，不要编造图谱里没有的关系）")
        for i, c in enumerate(found["chunks"], start=1):
            lines.append(f"[资料{i}] {c['title']}#{c['index']}")
            lines.append(c["text"].strip())
            lines.append("")

    return "\n".join(lines)


GRAPH_SYSTEM_PROMPT = (
    "你是一个严谨的研究助手。请**只依据**下面提供的「知识图谱中的关系」和「相关原文」回答问题。\n"
    "规则：\n"
    "1. 优先利用图谱中的关系来回答**关系型问题**（谁依赖谁、谁属于谁、谁和谁对比）；\n"
    "2. 每条结论都要注明依据（是图谱里的哪条边，或哪段原文）；\n"
    "3. 图谱和原文里都没有的，直接说「资料里没有相关内容」，不要凭记忆补充；\n"
    "4. 如果图谱里的关系链**不足以**回答问题，就明说缺哪一环，不要硬凑。"
)


def ask(
    client: LLMClient,
    question: str,
    hops: int = 2,
    top_k: int = 3,
    model: str | None = None,
) -> dict:
    found = graph_search(question, hops=hops, top_k=top_k)
    prompt = build_prompt(question, found)
    answer = client.chat(
        [
            {"role": "system", "content": GRAPH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=0.2,
    )
    return {
        "answer": answer,
        "prompt": prompt,
        "model": model or client.model,
        "seeds": found["seeds"],
        "nodes": found["nodes"],
        "edges": found["edges"],
        "chunks": found["chunks"],
        "timings": found["timings"],
        "hops": hops,
    }


# ---------- 统计 / 重置 / 全图 ----------


def stats() -> dict:
    conn = _connect()
    entities = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    edges = conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    chunks = conn.execute("SELECT COUNT(*) c FROM extracted").fetchone()["c"]
    types = [
        {"type": r["type"], "count": r["c"]}
        for r in conn.execute(
            "SELECT type, COUNT(*) c FROM entities GROUP BY type ORDER BY c DESC"
        )
    ]
    relations = [
        {"relation": r["relation"], "count": r["c"]}
        for r in conn.execute(
            "SELECT relation, COUNT(*) c FROM edges GROUP BY relation ORDER BY c DESC LIMIT 10"
        )
    ]
    total_chunks = len(rag.corpus())
    last_built = _get_meta(conn, "lastBuiltAt")
    conn.close()
    return {
        "entities": entities,
        "edges": edges,
        "extractedChunks": chunks,
        "totalChunks": total_chunks,
        "coverage": round(chunks / total_chunks, 4) if total_chunks else 0.0,
        "types": types,
        "relations": relations,
        # 把封闭词表也回传：前端要拿它给节点配色、给关系词做筛选项，
        # 而且"允许抽哪几种类型"本身就是本阶段的核心知识点。
        "entityTypes": ENTITY_TYPES,
        "relationHints": RELATION_HINTS,
        "lastBuiltAt": last_built,
    }


def counts() -> dict:
    """只要两个数：实体数、边数。构建过程中推进度用它——

    比 `stats()` 轻得多（不碰 ChromaDB，不算分布），
    进度回调每完成一块就要问一次，不能顺带把整个语料读一遍。
    """
    conn = _connect()
    result = {
        "entities": conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"],
        "edges": conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"],
    }
    conn.close()
    return result


def list_entities(limit: int = 200, q: str = "") -> dict:
    """实体清单（按提及次数排序）。`q` 做名称过滤。

    `degree` 是连接度——它比 `mentions` 更能说明一个实体在图上有多"枢纽"：
    mentions 高只说明它被反复提到，degree 高说明它真的**连**着很多东西。
    """
    conn = _connect()
    degree = {
        r["name"]: r["deg"]
        for r in conn.execute(
            "SELECT name, COUNT(*) deg FROM ("
            "  SELECT head AS name FROM edges UNION ALL SELECT tail AS name FROM edges"
            ") GROUP BY name"
        )
    }
    where, params = ("WHERE name LIKE ?", (f"%{q}%",)) if q else ("", ())
    rows = conn.execute(
        f"SELECT name, type, mentions FROM entities {where} "
        "ORDER BY mentions DESC, name LIMIT ?",
        params + (limit,),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    conn.close()
    return {
        "total": total,
        "items": [
            {
                "name": r["name"],
                "type": r["type"],
                "mentions": r["mentions"],
                "degree": degree.get(r["name"], 0),
            }
            for r in rows
        ],
    }


def corpus_documents() -> list[dict]:
    """语料文档清单 + 每篇已抽了多少块。

    前端拿它渲染"构建范围"的勾选框：**先抽你真正关心的那几篇**，
    是让 GraphRAG 从"跑一夜"变成"跑两分钟"的关键操作。
    """
    conn = _connect()
    done = {r["chunk_key"] for r in conn.execute("SELECT chunk_key FROM extracted")}
    conn.close()

    grouped: dict[str, dict] = {}
    for r in rag.corpus():
        item = grouped.setdefault(r["title"], {"title": r["title"], "chunks": 0, "extracted": 0})
        item["chunks"] += 1
        if chunk_key(r["title"], r["index"]) in done:
            item["extracted"] += 1
    return sorted(grouped.values(), key=lambda d: d["title"])


def top_nodes(limit: int = 40) -> dict:
    """给前端画"全图"用：取连接度最高的若干节点及其之间的边。

    为什么要截断：一张 300 节点的图在屏幕上只是一团毛线，**信息量为零**。
    先按度数取枢纽，才是"能看懂"的图。
    """
    conn = _connect()
    rows = conn.execute(
        """
        SELECT name, COUNT(*) AS deg FROM (
            SELECT head AS name FROM edges UNION ALL SELECT tail AS name FROM edges
        ) GROUP BY name ORDER BY deg DESC, name LIMIT ?
        """,
        (limit,),
    ).fetchall()
    keep = [r["name"] for r in rows]
    if not keep:
        conn.close()
        return {"nodes": [], "edges": []}
    ph = ",".join("?" * len(keep))
    nodes = [
        {"name": r["name"], "type": r["type"], "mentions": r["mentions"], "hop": 0, "isSeed": False}
        for r in conn.execute(
            f"SELECT name, type, mentions FROM entities WHERE name IN ({ph})", tuple(keep)
        )
    ]
    deg = {r["name"]: r["deg"] for r in rows}
    for n in nodes:
        n["degree"] = deg.get(n["name"], 0)
    edges = [
        {
            "id": r["id"],
            "head": r["head"],
            "relation": r["relation"],
            "tail": r["tail"],
            "sourceTitle": r["source_title"],
            "sourceIndex": r["source_index"],
            "hop": 0,
        }
        for r in conn.execute(
            f"SELECT * FROM edges WHERE head IN ({ph}) AND tail IN ({ph}) ORDER BY id",
            tuple(keep) + tuple(keep),
        )
    ]
    conn.close()
    return {"nodes": nodes, "edges": edges}


def reset() -> None:
    conn = _connect()
    conn.executescript("DELETE FROM edges; DELETE FROM entities; DELETE FROM extracted;")
    conn.commit()
    conn.close()
