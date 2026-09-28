import { ref } from 'vue'

export interface RagStatus {
  collection: string
  documents: number
  chunks: number
  embedModel: string
  chunkSize: number
  chunkOverlap: number
  persistDir: string
}

export interface RagDocument {
  title: string
  chunks: number
  chars: number
}

export interface RagIngestResult {
  title: string
  chunks: number
  chars: number
  preview: string[]
}

export interface RagHit {
  title: string
  index: number
  score: number
  text: string
}

export interface RagAskResult {
  answer: string
  hits: RagHit[]
  prompt: string
  model: string
}

/**
 * 预设问题：每个都对应一种**该被看见**的检索结果，而不是随便找几个问题。
 *
 * 括号里是导入示例文档后的实测分数（语料：docs/ 下 10 个文档 / 333 块）。
 * 「排序不稳」那条是故意留的——它演示的是 RAG 的真实局限，也是阶段 08 的入口。
 */
export const PRESET_QUERIES: Array<{ label: string; query: string; hint: string }> = [
  { label: '跨阶段', query: 'MCP 有哪三条铁律？', hint: '只捞到 1 条，标准答案块排第 25' },
  { label: '跨文档', query: '前端设计系统的调色板有哪些颜色？', hint: '命中 design-system · 0.68' },
  { label: '高分', query: 'RAG 的切块大小和重叠是怎么设置的？', hint: '命中 07 · 0.81' },
  { label: '噪声对照', query: '请问今天北京的天气怎么样？', hint: '0.43 · 低于 0.45' },
  { label: '排序不稳', query: '第 09 到第 15 阶段是主线还是能力插件？', hint: '表格被切碎，看它排错' },
]

/**
 * 阶段 07：RAG 检索台。
 *
 * 和前面阶段最大的不同：这里有三条**互相独立**的链路。
 *   入库（切块+嵌入）→ 检索（只算相似度，不调 LLM）→ 问答（检索 + 生成）
 * 把「检索」单独拆出来是本阶段的教学重点：**调 RAG 要先调检索**。
 */
export function useRag(options: { apiBase?: string } = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const status = ref<RagStatus | null>(null)
  const documents = ref<RagDocument[]>([])
  const ingest = ref<RagIngestResult | null>(null)

  const query = ref('')
  const topK = ref(3)
  const hits = ref<RagHit[]>([])
  const searchedQuery = ref('')

  const question = ref('')
  const askResult = ref<RagAskResult | null>(null)

  /** 哪个操作正在跑——用来精确显示 loading，而不是全局灰掉 */
  const busy = ref<'' | 'status' | 'ingest' | 'seed' | 'search' | 'ask' | 'reset'>('')
  const error = ref<string | null>(null)

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${apiBase}${path}`, init)
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      throw new Error(`HTTP ${resp.status}${detail ? ' · ' + detail.slice(0, 220) : ''}`)
    }
    return (await resp.json()) as T
  }

  function fail(prefix: string, e: unknown) {
    error.value = `${prefix}：${e instanceof Error ? e.message : String(e)}`
  }

  async function loadStatus() {
    busy.value = 'status'
    try {
      status.value = await call<RagStatus>('/rag/status')
      documents.value = await call<RagDocument[]>('/rag/documents')
      error.value = null
    } catch (e) {
      fail('知识库状态加载失败', e)
    } finally {
      busy.value = ''
    }
  }

  /** 入库：后端会把文档切成块再嵌入，返回每块的真实文本 */
  async function addDocument(title: string, text: string) {
    if (!title.trim() || !text.trim()) return
    busy.value = 'ingest'
    error.value = null
    try {
      ingest.value = await call<RagIngestResult>('/rag/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim(), text }),
      })
      await loadStatus()
    } catch (e) {
      fail('入库失败', e)
    } finally {
      busy.value = ''
    }
  }

  /** 一键导入本仓库的课程文档（真实长文档，能看出切块效果） */
  async function seed() {
    busy.value = 'seed'
    error.value = null
    try {
      const data = await call<{ imported: RagIngestResult[] }>('/rag/seed', { method: 'POST' })
      const last = data.imported[data.imported.length - 1]
      ingest.value = last ?? null
      await loadStatus()
    } catch (e) {
      fail('导入示例文档失败', e)
    } finally {
      busy.value = ''
    }
  }

  async function removeDocument(title: string) {
    busy.value = 'ingest'
    try {
      await call(`/rag/documents/${encodeURIComponent(title)}`, { method: 'DELETE' })
      await loadStatus()
    } catch (e) {
      fail('删除失败', e)
    } finally {
      busy.value = ''
    }
  }

  /** 纯检索：不调用 LLM */
  async function runSearch() {
    const q = query.value.trim()
    if (!q) return
    busy.value = 'search'
    error.value = null
    try {
      const data = await call<{ query: string; hits: RagHit[] }>('/rag/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, top_k: topK.value }),
      })
      hits.value = data.hits
      searchedQuery.value = data.query
    } catch (e) {
      fail('检索失败', e)
    } finally {
      busy.value = ''
    }
  }

  /** 完整 RAG：检索 + 生成 */
  async function runAsk() {
    const q = question.value.trim()
    if (!q) return
    busy.value = 'ask'
    error.value = null
    askResult.value = null
    try {
      askResult.value = await call<RagAskResult>('/rag/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, top_k: topK.value }),
      })
    } catch (e) {
      fail('RAG 问答失败', e)
    } finally {
      busy.value = ''
    }
  }

  async function reset() {
    busy.value = 'reset'
    try {
      await call('/rag/reset', { method: 'POST' })
      hits.value = []
      askResult.value = null
      ingest.value = null
      await loadStatus()
    } catch (e) {
      fail('清空失败', e)
    } finally {
      busy.value = ''
    }
  }

  /** 把检索预设填进检索框（同时清掉上一次结果，避免误读） */
  function applyQuery(q: string) {
    query.value = q
    hits.value = []
    searchedQuery.value = ''
  }

  function applyQuestion(q: string) {
    question.value = q
    askResult.value = null
  }

  return {
    status,
    documents,
    ingest,
    query,
    topK,
    hits,
    searchedQuery,
    question,
    askResult,
    busy,
    error,
    loadStatus,
    addDocument,
    seed,
    removeDocument,
    runSearch,
    runAsk,
    reset,
    applyQuery,
    applyQuestion,
  }
}
