import { computed, ref } from 'vue'
import { assertOk, readSseStream, useSseAbort } from './useSseStream'

/**
 * 阶段 13 · Agentic RAG（把检索交还给智能体决定）。
 *
 * 与前面各阶段的前端不同，这里要渲染的不是"一段流式文本"，而是**一条决策链路**：
 *
 *     要不要检索？(route) → 检索 → 够不够？(grade) → 不够就改写 + 换通道 → 再检索 → 生成
 *
 * 所以本 composable 的核心是把 SSE 帧还原成「回合（Round）」的嵌套结构：
 * 每一轮含一次 retrieve / 一次 grade / 可能一次 rewrite。视图只要 v-for 渲染
 * rounds 数组，用户就能看懂"它是怎么想的"。
 *
 * ⭐ 为什么**不**复用 useHarness 的 AgentColumn：那边的时间线是"一个角色的 Thought/Action"，
 *    这里的时间线是"三次决策导致的回合制结构"，形状不同，硬套会让视图层写出一堆分支。
 *    复用要复用在同一个抽象层级上——**错层复用比重复代码更贵**。
 */

export interface AgenticChannelInfo {
  name: string
  label: string
  why: string
  round: number
}

export interface AgenticChannelsResponse {
  channels: AgenticChannelInfo[]
}

/** 一段被检索命中的原文 */
export interface AgenticHit {
  title: string
  index: number
  text: string
  score?: number | null
  rerankScore?: number | null
  edgeCount?: number
  channel?: string
}

/** 一轮「检索 → 评级 →（改写）」 */
export interface AgenticRound {
  round: number
  channel: string
  channelLabel: string
  query: string
  hits: AgenticHit[]
  useful: boolean | null
  kept: number[]
  missing: string
  rewriteTo: string
  rewriteWhy: string
  retrieveMs: number
  gradeMs: number
}

/** SSE 一帧的载荷，与 backend/main.py 的 agentic_stream 一一对应 */
type AgenticFrame = {
  type?: string
  // start
  question?: string
  model?: string
  maxRounds?: number
  topK?: number
  plan?: Array<{ round: number; channel: string }>
  // route
  needRetrieval?: boolean
  reason?: string
  degraded?: boolean
  // retrieve
  round?: number
  channel?: string
  channelLabel?: string
  query?: string
  hits?: AgenticHit[]
  detail?: unknown
  ms?: number
  // grade
  useful?: boolean
  kept?: number[]
  missing?: string
  // rewrite
  from?: string
  to?: string
  why?: string
  // answer_delta
  text?: string
  // finish
  answer?: string | null
  rounds?: number
  finalQuery?: string
  basedOn?: string
  times?: Record<string, number>
  totalMs?: number
  llmMs?: number
  // error
  message?: string
}

export interface UseAgenticOptions {
  apiBase?: string
}

/**
 * 预设：四个都来自**本机实跑**（DeepSeek-Flash），期望值是实测出来的，不是猜的。
 *
 * 顺序刻意按"决策复杂度"递增排列：跳过 → 一轮命中 → 改写后命中 → 通道用尽。
 */
export const AGENTIC_PRESETS: Array<{ label: string; question: string; hint: string }> = [
  {
    label: '① 不该检索',
    question: '1 加 1 等于几？顺便用一句话说明为什么。',
    hint: '实测：route 判为无需检索 → rounds=0，约 2.3s。对照下面几例，这就是省下的那一刀',
  },
  {
    label: '② 一轮命中',
    question: 'RAG 的切块大小一般取多少？为什么要留重叠？',
    hint: '实测：第 1 轮 vector 评级"够用"后直接收手，约 10.5s',
  },
  {
    label: '③ 改写 + 换通道',
    question: '课程里那个把两段 reorder 过一遍再取前三的做法叫什么？',
    hint: '实测：第 1 轮评级不足 → 改写术语 → 第 2 轮升到 hybrid+重排命中，约 29.2s',
  },
  {
    label: '④ 走到图谱通道',
    question: '知识图谱里 BM25 这个概念和重排、混合检索之间是什么关系？',
    hint: '实测：连输两轮 → 第 3 轮切到 graph 仍为空 → 如实回答"资料里没有"，约 31.3s',
  },
]

export function useAgentic(options: UseAgenticOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const channels = ref<AgenticChannelInfo[]>([])
  const question = ref('')
  const model = ref('')
  const maxRounds = ref(3)
  const topK = ref(3)
  const hops = ref(2)

  const running = ref(false)
  const error = ref<string | null>(null)

  /**
   * 中断本轮流式运行。控制台目前还没放「停止」按钮（各阶段 UI 分批加），
   * 但能力先备好 —— 组件卸载时也可以调它。
   */
  const { signal, begin: beginSse, end: endSse, stop: stopSse, aborted } = useSseAbort()

  /** 组件卸载时中断未完成的流，避免后台继续跑 */
  function stop() {
    stopSse()
  }

  /** 决策链路 */
  const routeDecision = ref<{ needRetrieval: boolean; reason: string; degraded: boolean; ms: number } | null>(
    null,
  )
  const rounds = ref<AgenticRound[]>([])
  const answer = ref('')
  const finalQuery = ref('')
  const basedOn = ref('none')
  const usedRounds = ref(0)
  const times = ref<Record<string, number>>({})
  const totalMs = ref(0)
  const llmMs = ref(0)
  const keptHits = ref<AgenticHit[]>([])

  /** 检索被跳过——这是本阶段最值得高亮的一屏 */
  const retrievalSkipped = computed(() => routeDecision.value?.needRetrieval === false)

  async function loadChannels() {
    try {
      const resp = await fetch(`${apiBase}/agentic/channels`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = (await resp.json()) as AgenticChannelsResponse
      channels.value = data.channels ?? []
    } catch (e) {
      error.value = `读取检索通道失败：${e instanceof Error ? e.message : String(e)}`
    }
  }

  function reset() {
    routeDecision.value = null
    rounds.value = []
    answer.value = ''
    finalQuery.value = ''
    basedOn.value = 'none'
    usedRounds.value = 0
    times.value = {}
    totalMs.value = 0
    llmMs.value = 0
    keptHits.value = []
    error.value = null
  }

  async function run() {
    if (running.value || !question.value.trim()) return
    running.value = true
    reset()

    const signal = beginSse()

    try {
      const resp = await fetch(`${apiBase}/agentic/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: question.value,
          maxRounds: maxRounds.value,
          topK: topK.value,
          hops: hops.value,
        }),
        signal,
      })
      await assertOk(resp)
      await readSseStream<AgenticFrame>(resp.body!, apply, signal)
    } catch (e) {
      // 主动停止不是错误：保留已生成的结果
      if (aborted.value) return
      error.value = `Agentic RAG 运行失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      endSse()
      running.value = false
    }
  }


  function ensureRound(n: number): AgenticRound {
    let r = rounds.value.find((x) => x.round === n)
    if (!r) {
      r = {
        round: n,
        channel: '',
        channelLabel: '',
        query: '',
        hits: [],
        useful: null,
        kept: [],
        missing: '',
        rewriteTo: '',
        rewriteWhy: '',
        retrieveMs: 0,
        gradeMs: 0,
      }
      rounds.value.push(r)
      rounds.value.sort((a, b) => a.round - b.round)
    }
    return r
  }

  function apply(p: AgenticFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? model.value
        break
      case 'route':
        routeDecision.value = {
          needRetrieval: !!p.needRetrieval,
          reason: p.reason ?? '',
          degraded: !!p.degraded,
          ms: p.ms ?? 0,
        }
        break
      case 'retrieve': {
        const r = ensureRound(p.round ?? 0)
        r.channel = p.channel ?? ''
        r.channelLabel = p.channelLabel ?? ''
        r.query = p.query ?? ''
        r.hits = p.hits ?? []
        r.retrieveMs = p.ms ?? 0
        break
      }
      case 'grade': {
        const r = ensureRound(p.round ?? 0)
        r.useful = !!p.useful
        r.kept = p.kept ?? []
        r.missing = p.missing ?? ''
        r.gradeMs = p.ms ?? 0
        break
      }
      case 'rewrite': {
        const r = ensureRound(p.round ?? 0)
        r.rewriteTo = p.to ?? ''
        r.rewriteWhy = p.why ?? ''
        break
      }
      case 'answer_delta':
        answer.value += p.text ?? ''
        break
      case 'finish':
        answer.value = p.answer ?? answer.value
        usedRounds.value = p.rounds ?? 0
        finalQuery.value = p.finalQuery ?? ''
        basedOn.value = p.basedOn ?? 'none'
        times.value = p.times ?? {}
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        keptHits.value = p.hits ?? []
        break
      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  return {
    // 配置
    channels,
    question,
    maxRounds,
    topK,
    hops,
    // 状态（只读语义交给组件，这里给 ref 便于 v-model）
    running,
    error,
    model,
    // 结果
    routeDecision,
    rounds,
    answer,
    finalQuery,
    basedOn,
    usedRounds,
    times,
    totalMs,
    llmMs,
    keptHits,
    retrievalSkipped,
    // 动作
    loadChannels,
    run,
    stop,
    reset,
  }
}
