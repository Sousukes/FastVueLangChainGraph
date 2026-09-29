"""阶段 07 · RAG 基础：切块 → 嵌入 → 入库 → 检索 → 生成。

RAG（Retrieval-Augmented Generation）的全部内容就是这条流水线，五个环节各解决一个问题：

    切块 chunking    长文档塞不进上下文，切成小块
    嵌入 embedding   把文本变成向量，让"语义相近"变成"向量距离近"
    入库 indexing    把向量存进向量库，并建索引以便快速查找
    检索 retrieval   把问题的向量拿去库里找最相近的 top-k 块
    生成 generation  把检索到的块拼进 prompt，让模型"看着材料回答"

为什么嵌入模型是**本地**的？
    DeepSeek 不提供 embedding 接口。而且嵌入是"高频、低价值"的调用——
    一次入库要算几十上百次，放本地既省钱又快，还完全离线。
    本项目用 FastEmbed（ONNX 实现），**不依赖 torch**，装完约 100MB。

为什么单独一个模块？
    RAG 的三件事（切块 / 嵌入 / 检索）都跟 LLM 无关，
    只有最后的"生成"才需要 LLM。把它们分开，你才能单独测试检索质量——
    这恰恰是 RAG 调优时最该做的事：**先看检索，再看生成**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

# ⚠️ 这两行必须在 `from fastembed import ...` 之前执行：
# huggingface_hub 是在**导入时**读取 HF_ENDPOINT 的，晚了就不生效。
# 这样国内用户只要在 backend/.env 里写一行
#     HF_ENDPOINT=https://hf-mirror.com
# 就能走镜像下载嵌入模型，不必去改系统环境变量。
load_dotenv()

import chromadb  # noqa: E402
from fastembed import TextEmbedding  # noqa: E402
from fastembed.common.model_description import ModelSource  # noqa: E402
from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: E402

import bm25 as bm25kit  # noqa: E402
from llm import LLMClient  # noqa: E402

# ---------- 配置 ----------

# 中文文档按**字符**切块：400 字约等于一段完整论述，80 字重叠防止句子被切断
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
COLLECTION_NAME = "course_knowledge"

# 阶段 08 起：重排模型。默认用 int8 量化版（266MB），见 get_reranker() 里的说明。
RERANK_MODEL = "Xenova/bge-reranker-base"
RERANK_MODEL_FILE = "onnx/model_quantized.onnx"

# 向量库落在 backend/.chroma（已 gitignore）；想重来一遍就删掉它
CHROMA_DIR = Path(__file__).resolve().parent / ".chroma"


# ---------- 切块 ----------


def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把长文本切成带重叠的小块。

    策略（由粗到细）：
        1. 先按空行切成段落——段落是作者给的天然语义边界，优先尊重它；
        2. 段落本身超长，再按中文句末标点切句；
        3. 仍超长（比如整段没有标点），才硬切。
    然后按 chunk_size 把句子拼成块，块与块之间保留 overlap 个字符的重叠。

    为什么要重叠？因为答案可能正好跨在两块的边界上。不重叠的话，
    "深圳的天气是" 和 "32 度" 会被分到两块，谁都答不全。
    """
    # 1) 归一化 + 段落切分
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    paragraphs = [p for p in paragraphs if p]

    # 2) 段落 → 句子
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            units.append(para)
            continue
        buf = ""
        for ch in para:
            buf += ch
            if ch in "。！？；\n" and len(buf) >= 40:
                units.append(buf.strip())
                buf = ""
        if buf.strip():
            units.append(buf.strip())

    # 3) 句子 → 块（带重叠）
    #    拼接时保留换行：块是要喂给模型、也要展示给人看的，
    #    把 Markdown 段落压成一行会丢掉结构（标题和正文糊在一起）。
    chunks: list[str] = []
    cur = ""
    for unit in units:
        if not cur:
            cur = unit
            continue
        if len(cur) + len(unit) + 1 <= chunk_size:
            cur += "\n" + unit
        else:
            chunks.append(cur)
            # 用上一块的尾巴做重叠，避免答案落在接缝上
            tail = cur[-overlap:] if overlap > 0 else ""
            cur = tail + "\n" + unit
    if cur:
        chunks.append(cur)

    return [c.strip() for c in chunks if c.strip()]


# ---------- 嵌入 ----------


_embedder: TextEmbedding | None = None


def get_embedder() -> TextEmbedding:
    """懒加载嵌入模型（首次调用会下载约 90MB 权重到本地缓存）。

    下载失败通常是因为访问不了 HuggingFace。国内可走镜像——在 backend/.env 里加一行
        HF_ENDPOINT=https://hf-mirror.com
    然后重启服务即可（rag.py 会在导入 fastembed 之前加载 .env）。
    """
    global _embedder
    if _embedder is None:
        try:
            _embedder = TextEmbedding(model_name=EMBED_MODEL)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"加载嵌入模型 {EMBED_MODEL} 失败：{e}。"
                "首次使用需从 HuggingFace 下载权重；若网络不通，"
                "可在 backend/.env 里加 HF_ENDPOINT=https://hf-mirror.com 后重启服务。"
            ) from e
    return _embedder


def embed_documents(texts: list[str]) -> list[list[float]]:
    """把「待检索的文档」变成向量。"""
    return [v.tolist() for v in get_embedder().embed(texts)]


def embed_query(text: str) -> list[float]:
    """把「问题」变成向量。

    注意这里用的是 query_embed 而不是 embed：BGE 这类检索模型对
    「文档」和「查询」使用不同的前缀（查询要加"为这个句子生成表示…"），
    混用会让检索质量明显下降。FastEmbed 帮你处理了这个细节。
    """
    return next(iter(get_embedder().query_embed([text]))).tolist()


# ---------- 向量库 ----------


_client: chromadb.ClientAPI | None = None


def get_collection() -> chromadb.Collection:
    """拿到（必要时创建）持久化集合。

    metadata 里的 hnsw:space=cosine 很关键：BGE 系列是按**余弦相似度**训练的，
    用默认的 L2 距离会得到不一样的排序。距离度量必须和模型匹配。
    """
    global _client
    if _client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        # ⚠️ 只用**嵌入式**客户端（PersistentClient），不要换成 HttpClient。
        #
        # chromadb 1.5.x 目前带着 5 个未修复的 CVE（CVE-2026-45829/45830/45831/45833），
        # 但它们**全部是服务端模式（HTTP API + 多租户 + 鉴权）的漏洞**：
        # 比如"往 /api/v2/tenants/{t}/databases/{db}/collections 发一个恶意模型仓库
        # 并把 trust_remote_code 设为 true 就能 RCE"。
        # 本项目跑的是**本地嵌入式**库——没有 HTTP 服务、没有租户、没有鉴权，
        # 这些攻击面一个都不存在。所以：**别为了"修漏洞"去升级或改用 HttpClient。**
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


# ---------- 统一语料视图（阶段 08） ----------

# 向量检索和 BM25 是两套完全不同的打分机制，要融合就必须先让两边指向同一批"块"。
# 这里把库里的块整体取出来，用**列表下标**当块的内部编号。
_corpus_cache: list[dict] | None = None
_bm25_cache: "bm25kit.BM25Index | None" = None


def invalidate_cache() -> None:
    """语料变了（入库 / 删除 / 清空）就调它，下次检索时重建。

    这是缓存最容易出错的地方：**忘了失效，检索结果就会"慢半拍"**——
    明明刚导入的文档，却怎么搜都搜不到。所以增删改三处都要记得调。
    """
    global _corpus_cache, _bm25_cache
    _corpus_cache = None
    _bm25_cache = None


def corpus() -> list[dict]:
    """取出全部块（id / title / index / text）。"""
    global _corpus_cache
    if _corpus_cache is None:
        collection = get_collection()
        data = collection.get(include=["documents", "metadatas"])
        _corpus_cache = [
            {
                "id": cid,
                "title": str(meta.get("title", "")),
                "index": int(meta.get("index", 0)),
                "text": doc or "",
            }
            for cid, doc, meta in zip(
                data.get("ids") or [],
                data.get("documents") or [],
                data.get("metadatas") or [],
            )
        ]
    return _corpus_cache


def bm25_index() -> bm25kit.BM25Index:
    """按当前语料建 BM25 索引（带缓存，几百块瞬间建好）。"""
    global _bm25_cache
    if _bm25_cache is None:
        index = bm25kit.BM25Index()
        index.build([row["text"] for row in corpus()])
        _bm25_cache = index
    return _bm25_cache


# ---------- 入库 / 检索 ----------


def add_document(title: str, text: str) -> dict:
    """把一个文档切块、嵌入、入库。返回切块结果供前端展示。"""
    chunks = split_text(text)
    if not chunks:
        raise ValueError("文档内容为空，没有可入库的块")

    collection = get_collection()
    # 用标题做前缀，重复导入同一标题时先清掉旧块（幂等）
    delete_document(title)

    ids = [f"{title}#{i}" for i in range(len(chunks))]
    # 把**原文总字数**一起存进 metadata：块与块之间有 overlap，
    # 直接拿块的字数求和会比原文多出一截，两个地方显示的数字就对不上了。
    metadatas = [
        {"title": title, "index": i, "chars": len(text)} for i in range(len(chunks))
    ]
    collection.add(
        ids=ids,
        documents=chunks,
        metadatas=metadatas,
        embeddings=embed_documents(chunks),
    )
    invalidate_cache()  # 语料变了，BM25 索引必须重建
    return {
        "title": title,
        "chunks": len(chunks),
        "chars": len(text),
        "preview": chunks,
    }


def delete_document(title: str) -> int:
    collection = get_collection()
    existing = collection.get(where={"title": title}, include=[])
    ids = existing.get("ids") or []
    if ids:
        collection.delete(ids=ids)
        invalidate_cache()
    return len(ids)


def list_documents() -> list[dict]:
    """按标题聚合，列出已入库的文档。

    chars 取的是原文总字数（入库时写进 metadata），不是块字数之和——
    块之间有 overlap，求和会比原文多出一截。
    """
    collection = get_collection()
    data = collection.get(include=["metadatas", "documents"])
    grouped: dict[str, dict] = {}
    for meta, doc in zip(data.get("metadatas") or [], data.get("documents") or []):
        title = str(meta.get("title", "(未命名)"))
        item = grouped.setdefault(title, {"title": title, "chunks": 0, "chars": 0, "_sum": 0})
        item["chunks"] += 1
        item["_sum"] += len(doc or "")
        item["chars"] = max(item["chars"], int(meta.get("chars", 0)))

    out: list[dict] = []
    for item in grouped.values():
        # 兼容没有 chars 字段的旧数据：退回块字数之和
        if not item["chars"]:
            item["chars"] = item["_sum"]
        del item["_sum"]
        out.append(item)
    return sorted(out, key=lambda d: d["title"])


def stats() -> dict:
    collection = get_collection()
    data = collection.get(include=["metadatas"])
    titles = {str(m.get("title", "")) for m in (data.get("metadatas") or [])}
    titles.discard("")
    return {
        "collection": COLLECTION_NAME,
        "documents": len(titles),
        "chunks": collection.count(),
        "embedModel": EMBED_MODEL,
        "chunkSize": CHUNK_SIZE,
        "chunkOverlap": CHUNK_OVERLAP,
        "persistDir": str(CHROMA_DIR),
        # 阶段 08 起：重排模型。rerankReady 只查缓存，不触发下载——
        # "要不要下 266MB"这个决定必须由用户来做，不能由一次状态查询偷偷替他做。
        "rerankModel": RERANK_MODEL,
        "rerankReady": rerank_ready(),
    }


def reset() -> None:
    """清空知识库（教学用：改完切块参数可以重来一遍）。"""
    collection = get_collection()
    existing = collection.get(include=[])
    ids = existing.get("ids") or []
    if ids:
        collection.delete(ids=ids)
    invalidate_cache()


def search(query: str, top_k: int = 3) -> list[dict]:
    """纯检索：不调用 LLM，只回答"库里的哪些块跟这个问题最像"。"""
    collection = get_collection()
    if collection.count() == 0:
        return []

    result = collection.query(
        query_embeddings=[embed_query(query)],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    hits: list[dict] = []
    for doc, meta, dist in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        hits.append(
            {
                "title": str(meta.get("title", "")),
                "index": int(meta.get("index", 0)),
                # cosine 空间下 distance = 1 - 相似度，转成"越大越像"更直观
                "score": round(1.0 - float(dist), 4),
                "text": doc,
            }
        )
    return hits


# ---------- 阶段 08 · 混合检索与重排 ----------


def vector_ranking(query: str, top_k: int) -> list[tuple[int, float]]:
    """向量召回：返回 [(块下标, 余弦相似度)]。"""
    collection = get_collection()
    rows = corpus()
    if not rows or collection.count() == 0:
        return []
    position = {row["id"]: i for i, row in enumerate(rows)}
    result = collection.query(
        query_embeddings=[embed_query(query)],
        n_results=min(top_k, collection.count()),
        include=["distances"],
    )
    out: list[tuple[int, float]] = []
    for cid, dist in zip(result["ids"][0], result["distances"][0]):
        if cid in position:
            out.append((position[cid], round(1.0 - float(dist), 4)))
    return out


def bm25_ranking(query: str, top_k: int) -> list[tuple[int, float]]:
    """关键词召回：返回 [(块下标, BM25 分数)]。"""
    return [(i, round(s, 4)) for i, s in bm25_index().search(query, top_k=top_k)]


_reranker: TextCrossEncoder | None = None


def rerank_ready() -> bool:
    """重排模型是否已在本地（只查缓存，不触发下载）。

    用一个 boolean 把这个信息暴露出去，是因为**下载 266MB 不该是意外**——
    用户点"重排"之前就应该知道会发生什么。
    """
    try:
        from fastembed.common.utils import define_cache_dir
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(
            RERANK_MODEL, RERANK_MODEL_FILE, cache_dir=str(define_cache_dir())
        )
        return isinstance(cached, str) and Path(cached).exists()
    except Exception:  # noqa: BLE001
        return False


def get_reranker() -> TextCrossEncoder:
    """懒加载 Cross-encoder 重排模型。

    **为什么不是内置的 `BAAI/bge-reranker-base`？**
    内置源指向 fp32 的 `onnx/model.onnx`，**1.06 GB**——对学员太重了。
    Xenova 提供了同一个模型的 **int8 量化版，只要 266 MB**（小 4 倍），
    排序效果几乎无损。FastEmbed 支持注册自定义模型，把 `model_file`
    指到量化版即可。

    顺便记住这个思路：**"模型太大"的第一反应应该是量化，而不是换模型**。

    **Cross-encoder 和嵌入模型（bi-encoder）的区别是本阶段的核心概念：**
      - bi-encoder：问题和文档**各自**编码成向量，最后算距离。
        快（文档向量可以预先算好），但**问题和文档从没见过面**。
      - cross-encoder：把 **(问题, 文档) 拼成一条序列**一起送进模型，
        让注意力在两者之间自由流动。慢（每对都要跑一次前向），但准得多。

    所以标准做法是：**bi-encoder 粗筛出几十条 → cross-encoder 精排**。
    这也解释了为什么重排**必须**放在召回之后，而且候选不能太多。
    """
    global _reranker
    if _reranker is None:
        try:
            registered = {m["model"] for m in TextCrossEncoder.list_supported_models()}
            if RERANK_MODEL not in registered:
                TextCrossEncoder.add_custom_model(
                    model=RERANK_MODEL,
                    sources=ModelSource(hf=RERANK_MODEL),
                    model_file=RERANK_MODEL_FILE,
                    description="bge-reranker-base int8 量化版（多语言，约 266MB）",
                    license="MIT",
                    size_in_gb=0.26,
                )
            _reranker = TextCrossEncoder(model_name=RERANK_MODEL)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"加载重排模型 {RERANK_MODEL} 失败：{e}。"
                "首次使用需下载约 266MB 权重；若网络不通，"
                "可在 backend/.env 里加 HF_ENDPOINT=https://hf-mirror.com 后重启服务。"
            ) from e
    return _reranker


def rerank_scores(query: str, texts: list[str]) -> list[float]:
    """给每条候选打分——注意这里是**逐条**和问题一起过模型，所以慢。"""
    return [float(s) for s in get_reranker().rerank(query, texts)]


def hybrid_search(
    query: str,
    top_k: int = 3,
    mode: str = "hybrid",
    rerank: bool = False,
    candidates: int = 10,
) -> dict:
    """阶段 08 的完整检索管道：

        向量召回 ┐
                 ├─ RRF 融合 ─（可选）Cross-encoder 重排 ─ 取 top_k
        BM25 召回┘

    返回的不只是结果，还有**一张排名对照表**——每个候选块在各阶段的
    名次都摊开。这是本阶段最有说服力的一屏：你能亲眼看到阶段 07
    排第 25 名的那个块，是怎么一路爬到第 1 名的。
    """
    import time as _time

    rows = corpus()
    timings: dict[str, float] = {}
    if not rows:
        return {"hits": [], "rows": [], "timings": timings}

    # ① 两路召回（各自取 candidates 条，给融合留出余量）
    t0 = _time.perf_counter()
    vec = vector_ranking(query, candidates) if mode in ("vector", "hybrid") else []
    timings["vector"] = round((_time.perf_counter() - t0) * 1000, 2)

    t0 = _time.perf_counter()
    bm = bm25_ranking(query, candidates) if mode in ("bm25", "hybrid") else []
    timings["bm25"] = round((_time.perf_counter() - t0) * 1000, 2)

    # ② 融合
    t0 = _time.perf_counter()
    if mode == "vector":
        fused = vec
    elif mode == "bm25":
        fused = bm
    else:
        fused = bm25kit.rrf_fuse([[i for i, _ in vec], [i for i, _ in bm]])
    timings["fuse"] = round((_time.perf_counter() - t0) * 1000, 2)

    # ③ 重排（候选池取融合结果的前 candidates 条）
    reranked: list[tuple[int, float]] = []
    if rerank and fused:
        pool = [i for i, _ in fused][: max(candidates, top_k)]
        t0 = _time.perf_counter()
        scores = rerank_scores(query, [rows[i]["text"] for i in pool])
        reranked = sorted(zip(pool, scores), key=lambda p: (-p[1], p[0]))
        timings["rerank"] = round((_time.perf_counter() - t0) * 1000, 2)

    final = reranked or fused
    final_ids = [i for i, _ in final[:top_k]]

    # ④ 拼排名对照表
    def ranks(pairs: list[tuple[int, float]]) -> dict[int, int]:
        return {i: r for r, (i, _) in enumerate(pairs, 1)}

    def scores_of(pairs: list[tuple[int, float]]) -> dict[int, float]:
        return {i: s for i, s in pairs}

    vr, br, fr, rr = ranks(vec), ranks(bm), ranks(fused), ranks(reranked)
    vs, bs, fs, rs = scores_of(vec), scores_of(bm), scores_of(fused), scores_of(reranked)

    pool_ids = sorted(
        set(vr) | set(br) | set(fr) | set(rr),
        key=lambda i: (fr.get(i, 10**6), vr.get(i, 10**6), br.get(i, 10**6)),
    )
    table = [
        {
            "title": rows[i]["title"],
            "index": rows[i]["index"],
            "text": rows[i]["text"],
            "vector_rank": vr.get(i),
            "bm25_rank": br.get(i),
            "fused_rank": fr.get(i),
            "rerank_rank": rr.get(i),
            "vector_score": vs.get(i),
            "bm25_score": bs.get(i),
            "fused_score": fs.get(i),
            "rerank_score": rs.get(i),
            "in_final": i in final_ids,
        }
        for i in pool_ids
    ]

    hits = [
        {
            "title": rows[i]["title"],
            "index": rows[i]["index"],
            "text": rows[i]["text"],
            # score 沿用阶段 07 的语义（余弦相似度），方便两个阶段对照；
            # BM25 召回的块没有余弦分，就用 0 表示"不是靠语义进来的"。
            "score": vs.get(i, 0.0),
            "vector_score": vs.get(i),
            "bm25_score": bs.get(i),
            "rerank_score": rs.get(i),
        }
        for i in final_ids
    ]

    return {"hits": hits, "rows": table, "timings": timings, "final_ids": final_ids}


# ---------- 生成 ----------

RAG_SYSTEM_PROMPT = (
    "你是一个严谨的研究助手。请**只依据**下面提供的「参考资料」回答用户问题。\n"
    "规则：\n"
    "1. 资料里有的，就依据资料回答，并注明是第几段资料；\n"
    "2. 资料里没有的，直接说「资料里没有相关内容」，不要凭记忆补充；\n"
    "3. 不要编造资料里不存在的细节。"
)


def build_prompt(question: str, hits: list[dict]) -> str:
    """把检索结果拼成 prompt——这一步是 RAG 的"增强"（Augmented）。"""
    if not hits:
        return f"（知识库为空，没有任何参考资料）\n\n用户问题：{question}"
    blocks = [
        f"[资料 {i}] 来源《{h['title']}》第 {h['index']} 段\n{h['text']}"
        for i, h in enumerate(hits, 1)
    ]
    return "参考资料：\n\n" + "\n\n".join(blocks) + f"\n\n用户问题：{question}"


def ask(
    client: LLMClient,
    question: str,
    top_k: int = 3,
    model: str | None = None,
    # 阶段 08 起新增（都有默认值，阶段 07 的调用方式不受影响）
    mode: str = "vector",
    rerank: bool = False,
    candidates: int = 10,
) -> dict[str, Any]:
    """完整 RAG：检索 → 拼 prompt → 生成。

    返回里带上 `prompt` 和 `hits` 是有意的——**让用户看见模型到底看到了什么**。
    这是阶段 06 那条经验的延续：可解释性来自"把中间产物摊开"。

    阶段 08 起，检索这一步换成了可配置的管道（纯向量 / BM25 / 混合 + 重排）。
    默认仍是 `mode="vector"`，所以阶段 07 的行为**一字不变**。
    """
    found = hybrid_search(
        question, top_k=top_k, mode=mode, rerank=rerank, candidates=candidates
    )
    hits = found["hits"]
    prompt = build_prompt(question, hits)
    answer = client.chat(
        [
            {"role": "system", "content": RAG_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        model=model,
        temperature=0.2,
    )
    return {
        "answer": answer,
        "hits": hits,
        "prompt": prompt,
        "model": model or client.model,
        "mode": mode,
        "rerank": rerank,
        "rows": found["rows"],
        "timings": found["timings"],
    }


# ---------- 便捷入口（给教学/脚本用） ----------


def ingest_many(items: list[tuple[str, str]], on_progress: Callable[[str, int], None] | None = None) -> list[dict]:
    out = []
    for title, text in items:
        info = add_document(title, text)
        if on_progress:
            on_progress(title, info["chunks"])
        out.append(info)
    return out
