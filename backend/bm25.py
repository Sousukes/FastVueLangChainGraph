"""阶段 08 · BM25 关键词检索。

为什么向量检索之外还要一个「关键词」检索？
    向量检索擅长"意思相近"，但有两类问题它答不好：
      ① **精确术语**——`isError`、`CHUNK_SIZE`、"三条铁律" 这种词，
         向量会把它和一堆近义表达混在一起，反而定位不到；
      ② **答案只占块的一小部分**——块的向量被占多数篇幅的其他内容主导，
         于是"含答案的那一块"排不上号（阶段 07 实测：标准答案块排第 25 名）。
    BM25 正好相反：它**完全不懂语义**，只会数词。但"字面命中"这件事它极其可靠。

BM25 是什么？
    一句话：**把「这个词在本块里出现得多不多」和「这个词在全库里稀不稀有」结合起来打分**。

        score(q, d) = Σ_{t∈q}  IDF(t) ·  f(t,d)·(k₁+1)
                                          ─────────────────────────────
                                          f(t,d) + k₁·(1-b+b·|d|/avgdl)

    逐项拆开看，每一块都在回答一个朴素的问题：

        f(t,d)          词 t 在文档 d 里出现了几次？（出现越多越相关）
        IDF(t)          词 t 在全库里有多稀有？（越稀有越有区分度——
                        "的"出现在每篇文档里，它什么也说明不了）
        |d| / avgdl     这篇文档比平均长多少？（长文档天然容易撞词，要压一压）
        k₁, b           两个旋钮：k₁ 控制"词频的饱和速度"，b 控制"长度惩罚的力度"

    为什么叫"饱和"？因为词频的作用不是线性的：出现 1 次到 2 次意义重大，
    出现 20 次到 21 次几乎没区别。所以公式里 f 在分母也出现了一次——
    出现次数再多，分数也会被压在一个上限附近。

为什么中文必须先分词？
    英文用空格天然分词，`split()` 就够了。中文没有空格：
    "三条铁律" 如果整段当成一个词，那用户搜"铁律"就永远匹配不上。
    所以中文检索的地基是**分词**，本项目用 jieba。
"""

from __future__ import annotations

import logging
import math
import warnings
from collections import Counter

# jieba 0.42.1 在 Python 3.12+ 上会抛一堆 SyntaxWarning（正则里的无效转义），
# 功能完全正常，但会把控制台刷满、盖住真正的告警。这里显式静音。
with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    import jieba

# jieba 首次加载会打印 "Building prefix dict…"，那是它的 INFO 日志。
# 词典要加载约 1 秒（之后走 %TEMP%\jieba.cache），刷在每次启动的日志里没意义。
jieba.setLogLevel(logging.WARNING)

# BM25 的两个经典默认值（Robertson 等人给出的经验值，几乎所有实现都用这两个数）
K1 = 1.5
B = 0.75

# 停用词：高频但不携带区分度的词。注意**不要**把否定词（不 / 没 / 无）放进来，
# 它们会改变语义（"不需要 Key" 和 "需要 Key" 是两回事）。
STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "也", "都", "就", "而", "及", "或",
    "一个", "我们", "你们", "他们", "这", "那", "有", "很", "会", "能",
    "上", "下", "中", "里", "对", "从", "到", "为", "以", "之", "其", "但",
    "又", "你", "我", "他", "她", "它", "吗", "呢", "吧", "啊", "把", "被",
    "让", "使", "给", "跟", "用", "做", "说", "个", "些", "该", "可以",
    "如果", "所以", "因为", "这样", "那么", "什么", "怎么", "还是", "就是",
}


def _is_content(token: str) -> bool:
    """只保留"有内容"的词：字母、数字、或中日韩汉字。"""
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in token)


def tokenize(text: str) -> list[str]:
    """中文分词 → 归一化 → 去停用词。

    这一步是关键词检索的地基：**分错了，后面全错**。
    可以自己试一下 `tokenize("MCP 的三条铁律是什么")` 的输出——
    你会看到 jieba 把"三条/铁律"切开了，所以搜"铁律"能命中。
    """
    out: list[str] = []
    for word in jieba.cut(text):
        w = word.strip().lower()
        if not w or w in STOPWORDS or not _is_content(w):
            continue
        out.append(w)
    return out


class BM25Index:
    """内存版 BM25 索引。

    本项目语料只有几百块，全量装进内存、每次查询线性打分完全够用
    （实测 < 10 ms）。真实生产环境会换成倒排索引（Elasticsearch / Lucene），
    但**打分公式是一模一样的**——先把公式搞懂，再去用现成的。
    """

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self.n = 0
        self.tf: list[Counter[str]] = []   # 每块的词频
        self.doc_len: list[int] = []       # 每块的分词后长度
        self.df: Counter[str] = Counter()  # 词 → 出现在多少块里（document frequency）
        self.avgdl = 0.0

    def build(self, texts: list[str]) -> None:
        self.tf = [Counter(tokenize(t)) for t in texts]
        self.n = len(self.tf)
        self.doc_len = [sum(c.values()) for c in self.tf]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        # df：每个词在多少块里出现过（同一块里出现多次只算一次）
        self.df = Counter()
        for counter in self.tf:
            self.df.update(counter.keys())

    def idf(self, term: str) -> float:
        """逆文档频率。

        加 1 是为了保证非负：一个词如果在**每一块**里都出现（n_t = n），
        朴素公式会得到负值，反而倒扣分——那显然不合理。
        """
        n_t = self.df.get(term, 0)
        return math.log(1 + (self.n - n_t + 0.5) / (n_t + 0.5))

    def score(self, query_tokens: list[str], doc_id: int) -> float:
        tf = self.tf[doc_id]
        dl = self.doc_len[doc_id]
        if not self.avgdl:
            return 0.0
        total = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if not f:  # 没出现的词直接跳过——这就是"字面匹配"的含义
                continue
            norm = 1 - self.b + self.b * dl / self.avgdl
            total += self.idf(term) * (f * (self.k1 + 1)) / (f + self.k1 * norm)
        return total

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """返回 [(块序号, BM25 分数)]，按分数降序。分数为 0 的块不返回。"""
        tokens = tokenize(query)
        if not tokens or not self.n:
            return []
        scored = [(i, self.score(tokens, i)) for i in range(self.n)]
        scored = [pair for pair in scored if pair[1] > 0]
        scored.sort(key=lambda p: (-p[1], p[0]))
        return scored[:top_k]


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """RRF（Reciprocal Rank Fusion）：把多份排名合成一份。

        score(d) = Σ_i  1 / (k + rank_i(d))

    为什么不能直接把分数相加？因为**量纲不同**：
    向量给的是余弦相似度（0~1，差距往往只有 0.0x），
    BM25 给的是分数（0~几十，差距可能很大）。
    直接相加，BM25 会彻底压死向量——这不是"融合"，是"覆盖"。

    RRF 绕开了量纲问题：**它只看名次，不看分数**。
    一个块在向量里排第 1、在 BM25 里排第 3，它的得分就是 1/61 + 1/63。
    两份排名都靠前的块自然浮上来——这正是"融合"想要的效果。

    k 的作用是**削弱头名的绝对优势**：k 越小，第 1 名越强势；
    k=60 是论文里的经验值，含义是"第 1 名和第 2 名的差距，不应该比
    第 20 名和第 21 名的差距大 20 倍"。
    """
    acc: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            acc[doc_id] = acc.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(acc.items(), key=lambda p: (-p[1], p[0]))
