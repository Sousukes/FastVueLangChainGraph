import { computed, ref } from 'vue'

/**
 * 阶段 14 · AI 搜索应用（把检索结果变成可被验证的答案）。
 *
 * 与阶段 13 的 useAgentic 不同，这里渲染的不是"决策链路"，而是**搜索结果页**：
 *
 *     搜索框 → 一次混合检索 → 带引用 [n] 的合成答案 + 可点击核对的来源面板
 *
 * 所以本 composable 的核心是：把 SSE 帧还原成「答案文本（带 [n] 标记）+ 来源列表」，
 * 并额外把答案拆成「文本段 / 引用 chip」两段序列，方便视图层把 [n] 渲染成可点击的引用。
 *
 * ⭐ 为什么不复用 useAgentic 的读流逻辑：两边的帧协议虽然同源（start/retrieve/
 *   answer_delta/finish），但 agentic 多了一整条 route→grade→rewrite 决策链，
 *   而 search 的 retrieve 只有一次、且多了 finish 里的 sources/citations/grounded。
 *   错层复用比重复代码更贵，所以这里各自读流、各自管自己的状态。
 */

export interface SearchHit {
  title: string
  index: number
  text: string
  score?: number | null
  rerankScore?: number | null
  channel?: string
}

/** finish 里的一条来源（已带 cited 标记） */
export interface SearchSource {
  rank: number
  index: number
  title: string
  snippet: string
  score?: number | null
  rerankScore?: number | null
  cited: boolean
}

/** SSE 一帧的载荷，与 backend/main.py 的 search_stream 一一对应 */
type SearchFrame = {
  type?: string
  // start
  question?: string
  model?: string
  topK?: number
  // retrieve
  query?: string
  hits?: SearchHit[]
  count?: number
  ms?: number
  // synthesize
  status?: string
  // answer_delta
  text?: string
  // finish
  answer?: string | null
  sources?: SearchSource[]
  citations?: number[]
  grounded?: boolean
  coverage?: number
  times?: Record<string, number>
  totalMs?: number
  llmMs?: number
  // error
  message?: string
}

/** 把答案拆成「文本 / 引用」交替的片段，供模板渲染可点击的引用 chip */
export interface AnswerPart {
  kind: 'text' | 'cite'
  value: string | number
}

export interface UseSearchOptions {
  apiBase?: string
}

export function useSearch(options: UseSearchOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const question = ref('')
  const model = ref('')
  const topK = ref(6)
  const rerank = ref(true)

  const running = ref(false)
  const error = ref<string | null>(null)

  // 结果状态
  const retrievedCount = ref(0)
  const hits = ref<SearchHit[]>([])
  const answer = ref('')
  const sources = ref<SearchSource[]>([])
  const citations = ref<number[]>([])
  const grounded = ref(false)
  const coverage = ref(0)
  const times = ref<Record<string, number>>({})
  const totalMs = ref(0)
  const llmMs = ref(0)

  /** 当前被高亮的来源（点引用 chip 时设置，用于滚动定位） */
  const activeSource = ref<number | null>(null)

  /** 答案拆成可点击片段：[1] 变成 chip，其余是文本 */
  const answerParts = computed<AnswerPart[]>(() => {
    const text = answer.value
    if (!text) return []
    const parts: AnswerPart[] = []
    const re = /\[(\d+)\]/g
    let last = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parts.push({ kind: 'text', value: text.slice(last, m.index) })
      const n = Number(m[1])
      // 只把"确实指向某个来源"的 [n] 渲染成可点击 chip。越界下标（如来源只有 3 条却出现 [9]）
      // 退回纯文本，避免点击 focusSource(n) 因找不到 src-n 元素而静默无反应。
      // 后端 _parse_citations 只过滤 citations 列表里的越界项，原始答案文本里的越界标记这里兜底。
      if (retrievedCount.value > 0 && n >= 1 && n <= retrievedCount.value) {
        parts.push({ kind: 'cite', value: n })
      } else {
        parts.push({ kind: 'text', value: m[0] })
      }
      last = m.index + m[0].length
    }
    if (last < text.length) parts.push({ kind: 'text', value: text.slice(last) })
    return parts
  })

  const canRun = computed(() => !!question.value.trim() && !running.value)

  function reset() {
    retrievedCount.value = 0
    hits.value = []
    answer.value = ''
    sources.value = []
    citations.value = []
    grounded.value = false
    coverage.value = 0
    times.value = {}
    totalMs.value = 0
    llmMs.value = 0
    activeSource.value = null
    error.value = null
  }

  async function run() {
    if (running.value || !question.value.trim()) return
    running.value = true
    reset()

    try {
      const resp = await fetch(`${apiBase}/search/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: question.value,
          topK: topK.value,
          rerank: rerank.value,
        }),
      })
      if (!resp.ok || !resp.body) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 200)}`)
      }
      await readStream(resp.body)
    } catch (e) {
      error.value = `AI 搜索运行失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      running.value = false
    }
  }

  async function readStream(body: ReadableStream<Uint8Array>) {
    const reader = body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          let payload: SearchFrame
          try {
            payload = JSON.parse(line.slice(5).trim()) as SearchFrame
          } catch {
            continue
          }
          apply(payload)
        }
      }
    } finally {
      reader.releaseLock()
    }
  }

  function apply(p: SearchFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? model.value
        break
      case 'retrieve':
        hits.value = p.hits ?? []
        retrievedCount.value = p.count ?? (p.hits?.length ?? 0)
        break
      case 'answer_delta':
        answer.value += p.text ?? ''
        break
      case 'finish':
        answer.value = p.answer ?? answer.value
        sources.value = p.sources ?? []
        citations.value = p.citations ?? []
        grounded.value = !!p.grounded
        coverage.value = p.coverage ?? 0
        times.value = p.times ?? {}
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        break
      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  /** 点引用 chip：高亮对应来源，并滚动到它 */
  function focusSource(n: number) {
    activeSource.value = n
    const el = document.getElementById(`src-${n}`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  return {
    // 配置
    question,
    topK,
    rerank,
    // 状态
    running,
    error,
    model,
    // 结果
    retrievedCount,
    hits,
    answer,
    sources,
    citations,
    grounded,
    coverage,
    times,
    totalMs,
    llmMs,
    activeSource,
    answerParts,
    // 动作
    run,
    reset,
    focusSource,
    canRun,
  }
}
