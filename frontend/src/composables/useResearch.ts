import { computed, ref } from 'vue'

/**
 * 阶段 15 · Deep Research（把检索交还给规划 + 忠实性校验）。
 *
 * 与阶段 14 的 useSearch 不同，这里渲染的不是"一次搜索的结果页"，而是一份
 * **逐步成形的研究报告**：先看到大纲（规划），再看到每节各自的检索与写作进度，
 * 最后拼成一篇带全局 [n] 引用的长报告，并附上"忠实性核查"结论。
 *
 * 帧协议与后端 research.py 一一对应：start / plan / section_start / section_retrieve /
 * section_answer_delta / answer_delta / section_finish / synthesize / faithfulness / finish。
 * 两边的"答案带 [n]"逻辑与阶段 14 同源，故 answerParts 的拆 chip 规则一致。
 */

export type SectionStatus = 'pending' | 'researching' | 'writing' | 'done'

export interface ResearchSection {
  index: number
  title: string
  subQuestion: string
  status: SectionStatus
  rounds: number
  basedOn: string
  hitCount: number
  channel: string | null
  citations: number[]
  grounded: boolean
  draft: string
}

/** finish 里的一条全局来源（跨小节去重合并后） */
export interface ResearchSource {
  rank: number
  index: number
  title: string
  snippet: string
  score?: number | null
  rerankScore?: number | null
  channel?: string | null
  section?: number | null
  cited: boolean
}

/** SSE 一帧的载荷，与 backend/main.py 的 research_stream 一一对应 */
type ResearchFrame = {
  type?: string
  // start
  question?: string
  model?: string
  maxSections?: number
  // section_* 通用
  index?: number
  // plan
  sections?: { title: string; question: string }[]
  // section_start
  subQuestion?: string
  total?: number
  // section_retrieve
  rounds?: number
  basedOn?: string
  hitCount?: number
  channel?: string | null
  query?: string
  fallback?: boolean
  ms?: number
  // section_answer_delta
  text?: string
  // synthesize
  status?: string
  sectionCount?: number
  sourceCount?: number
  // faithfulness
  score?: number
  faithful?: boolean
  unsupported?: string[]
  // finish
  answer?: string | null
  sources?: ResearchSource[]
  citations?: number[]
  grounded?: boolean
  coverage?: number
  faithfulness?: number
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

export interface UseResearchOptions {
  apiBase?: string
}

export function useResearch(options: UseResearchOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const question = ref('')
  const model = ref('')
  const topK = ref(4)
  const rerank = ref(true)
  const maxSections = ref(4)

  const running = ref(false)
  const error = ref<string | null>(null)

  // 大纲 / 分段进度
  const outline = ref<ResearchSection[]>([])

  // 结果状态
  const answer = ref('')
  const sources = ref<ResearchSource[]>([])
  const citations = ref<number[]>([])
  const grounded = ref(false)
  const coverage = ref(0)
  const faithful = ref(false)
  const faithfulness = ref(0)
  const unsupported = ref<string[]>([])
  const times = ref<Record<string, number>>({})
  const totalMs = ref(0)
  const llmMs = ref(0)

  /** 当前被高亮的来源（点引用 chip 时设置，用于滚动定位） */
  const activeSource = ref<number | null>(null)

  /** 答案拆成可点击片段：[1] 变成 chip，其余是文本（越界下标兜底为纯文本） */
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
      if (sources.value.length > 0 && n >= 1 && n <= sources.value.length) {
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
    outline.value = []
    answer.value = ''
    sources.value = []
    citations.value = []
    grounded.value = false
    coverage.value = 0
    faithful.value = false
    faithfulness.value = 0
    unsupported.value = []
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
      const resp = await fetch(`${apiBase}/research/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: question.value,
          topK: topK.value,
          rerank: rerank.value,
          maxSections: maxSections.value,
        }),
      })
      if (!resp.ok || !resp.body) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 200)}`)
      }
      await readStream(resp.body)
    } catch (e) {
      error.value = `深度研究运行失败：${e instanceof Error ? e.message : String(e)}`
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
          let payload: ResearchFrame
          try {
            payload = JSON.parse(line.slice(5).trim()) as ResearchFrame
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

  function findSection(i: number): ResearchSection | undefined {
    return outline.value.find((s) => s.index === i)
  }

  function apply(p: ResearchFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? model.value
        break
      case 'plan':
        outline.value = (p.sections ?? []).map((s, idx) => ({
          index: idx + 1,
          title: s.title,
          subQuestion: s.question,
          status: 'pending',
          rounds: 0,
          basedOn: '',
          hitCount: 0,
          channel: null,
          citations: [],
          grounded: false,
          draft: '',
        }))
        break
      case 'section_start': {
        const sec = findSection(p.index ?? 0)
        if (sec) sec.status = 'researching'
        break
      }
      case 'section_retrieve': {
        const sec = findSection(p.index ?? 0)
        if (sec) {
          sec.rounds = p.rounds ?? 0
          sec.basedOn = p.basedOn ?? ''
          sec.hitCount = p.hitCount ?? 0
          sec.channel = p.channel ?? null
          sec.status = 'writing'
        }
        break
      }
      case 'section_answer_delta': {
        const sec = findSection(p.index ?? 0)
        if (sec) sec.draft += p.text ?? ''
        break
      }
      case 'section_finish': {
        const sec = findSection(p.index ?? 0)
        if (sec) {
          sec.citations = p.citations ?? []
          sec.grounded = !!p.grounded
          sec.status = 'done'
        }
        break
      }
      case 'answer_delta':
        answer.value += p.text ?? ''
        break
      case 'faithfulness':
        faithful.value = !!p.faithful
        faithfulness.value = p.score ?? 0
        unsupported.value = p.unsupported ?? []
        break
      case 'finish':
        answer.value = p.answer ?? answer.value
        sources.value = p.sources ?? []
        citations.value = p.citations ?? []
        grounded.value = !!p.grounded
        coverage.value = p.coverage ?? 0
        faithful.value = !!p.faithful
        faithfulness.value = p.faithfulness ?? 0
        unsupported.value = p.unsupported ?? []
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
    maxSections,
    // 状态
    running,
    error,
    model,
    // 大纲 / 分段
    outline,
    // 结果
    answer,
    sources,
    citations,
    grounded,
    coverage,
    faithful,
    faithfulness,
    unsupported,
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
