import { ref } from 'vue'

export interface HybridStatus {
  collection: string
  documents: number
  chunks: number
  embedModel: string
  chunkSize: number
  chunkOverlap: number
  rerankModel: string
  rerankReady: boolean
}

export type Mode = 'vector' | 'bm25' | 'hybrid'

/** 排名对照表的一行：同一个块在各阶段的名次 */
export interface RankRow {
  title: string
  index: number
  text: string
  vector_rank: number | null
  bm25_rank: number | null
  fused_rank: number | null
  rerank_rank: number | null
  vector_score: number | null
  bm25_score: number | null
  fused_score: number | null
  rerank_score: number | null
  in_final: boolean
}

export interface HybridHit {
  title: string
  index: number
  score: number
  text: string
  vector_score: number | null
  bm25_score: number | null
  rerank_score: number | null
}

export interface HybridSearchResult {
  query: string
  hits: HybridHit[]
  mode: string
  rerank: boolean
  candidates: number
  rows: RankRow[]
  timings: Record<string, number>
}

export interface HybridAskResult {
  answer: string
  hits: HybridHit[]
  prompt: string
  model: string
  mode: string
  rerank: boolean
  rows: RankRow[]
  timings: Record<string, number>
}

/**
 * 预设问题：每个都对应**阶段 07 真实踩过的坑**，并且标出了"正确答案是哪一块"。
 *
 * target 用 `标题#段号` 表示。前端会在对照表里给这一行打星，
 * 这样"它从第几名爬到第几名"就是一眼可见的，不需要解释。
 */
export const HYBRID_PRESETS: Array<{
  label: string
  query: string
  target: string | null
  hint: string
}> = [
  {
    label: 'MCP 三条铁律',
    query: 'MCP 有哪三条铁律？',
    target: '06-MCP协议开发#29',
    hint: '阶段 07：第 25 名',
  },
  {
    label: '阶段总表',
    query: '第 09 到第 15 阶段是主线还是能力插件？',
    target: 'DESIGN#7',
    hint: '阶段 07：排错到 #8',
  },
  {
    label: '切块参数',
    query: 'RAG 的切块大小和重叠是怎么设置的？',
    target: '07-RAG基础#25',
    hint: '正常对照：三路都第 1',
  },
  {
    label: '精确术语',
    query: 'isError 是什么意思？',
    target: null,
    hint: 'BM25 的强项',
  },
]

export function rowKey(r: { title: string; index: number }) {
  return `${r.title}#${r.index}`
}

/**
 * 阶段 08：混合检索对照台。
 *
 * 和阶段 07 的 useRag 最大的不同是：**这里一次请求要回一张排名对照表**。
 * 阶段 07 只告诉你"结果是什么"，阶段 08 要告诉你"结果是怎么来的"。
 */
export function useHybrid(options: { apiBase?: string } = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const status = ref<HybridStatus | null>(null)

  const query = ref(HYBRID_PRESETS[0].query)
  const mode = ref<Mode>('hybrid')
  const rerank = ref(true)
  const candidates = ref(10)
  const topK = ref(3)

  const result = ref<HybridSearchResult | null>(null)
  const askResult = ref<HybridAskResult | null>(null)

  const busy = ref<'' | 'status' | 'search' | 'ask' | 'warmup'>('')
  const error = ref<string | null>(null)

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${apiBase}${path}`, init)
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      throw new Error(`HTTP ${resp.status}${detail ? ' · ' + detail.slice(0, 240) : ''}`)
    }
    return (await resp.json()) as T
  }

  function fail(prefix: string, e: unknown) {
    error.value = `${prefix}：${e instanceof Error ? e.message : String(e)}`
  }

  async function loadStatus() {
    busy.value = 'status'
    try {
      status.value = await call<HybridStatus>('/rag/status')
      error.value = null
    } catch (e) {
      fail('知识库状态加载失败', e)
    } finally {
      busy.value = ''
    }
  }

  const body = () => ({
    mode: mode.value,
    rerank: rerank.value,
    candidates: candidates.value,
    top_k: topK.value,
  })

  async function runSearch() {
    const q = query.value.trim()
    if (!q) return
    busy.value = 'search'
    error.value = null
    askResult.value = null
    try {
      result.value = await call<HybridSearchResult>('/rag/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, ...body() }),
      })
      await loadStatus() // 重排模型可能刚下载完，刷新一下状态
    } catch (e) {
      fail('检索失败', e)
    } finally {
      busy.value = ''
    }
  }

  async function runAsk() {
    const q = query.value.trim()
    if (!q) return
    busy.value = 'ask'
    error.value = null
    try {
      askResult.value = await call<HybridAskResult>('/rag/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, ...body() }),
      })
    } catch (e) {
      fail('RAG 问答失败', e)
    } finally {
      busy.value = ''
    }
  }

  function applyPreset(q: string) {
    query.value = q
    result.value = null
    askResult.value = null
  }

  return {
    status,
    query,
    mode,
    rerank,
    candidates,
    topK,
    result,
    askResult,
    busy,
    error,
    loadStatus,
    runSearch,
    runAsk,
    applyPreset,
  }
}
