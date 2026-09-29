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

from llm import LLMClient  # noqa: E402

# ---------- 配置 ----------

# 中文文档按**字符**切块：400 字约等于一段完整论述，80 字重叠防止句子被切断
CHUNK_SIZE = 400
CHUNK_OVERLAP = 80

EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
COLLECTION_NAME = "course_knowledge"

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
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


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
    }


def reset() -> None:
    """清空知识库（教学用：改完切块参数可以重来一遍）。"""
    collection = get_collection()
    existing = collection.get(include=[])
    ids = existing.get("ids") or []
    if ids:
        collection.delete(ids=ids)


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
) -> dict[str, Any]:
    """完整 RAG：检索 → 拼 prompt → 生成。

    返回里带上 `prompt` 和 `hits` 是有意的——**让用户看见模型到底看到了什么**。
    这是阶段 06 那条经验的延续：可解释性来自"把中间产物摊开"。
    """
    hits = search(question, top_k=top_k)
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
