import { computed, ref } from 'vue'
import { assertOk, readSseStream, useSseAbort } from './useSseStream'

/**
 * 阶段 10：单智能体（ReAct）。
 *
 * 这个 composable 和前面所有阶段最大的不同是：**它管的是一个"过程"而不是一次"请求"**。
 *
 * 阶段 03 的流式是"一段文本逐字出现"，阶段 09 的流式是"一个进度条往前走"，
 * 而这里是**一串结构化的步骤**——Thought / Action / Observation 交替出现，
 * 每一步还带着耗时、字符数、是否截断。所以状态不是几个标量，而是一条**时间线**。
 */

// ---------- 类型（与 backend/schemas.py 的阶段 10 段一一对应） ----------

export interface AgentToolInfo {
  name: string
  description: string
  parameters: Record<string, unknown>
  server: string | null
}

export interface AgentToolGroup {
  group: string
  label: string
  tools: AgentToolInfo[]
  error: string | null
}

export interface AgentToolOverlap {
  name: string
  groups: string[]
  winner: string
}

export interface AgentToolsResponse {
  groups: AgentToolGroup[]
  overlaps: AgentToolOverlap[]
  priority: string[]
}

export interface AgentAction {
  tool: string
  arguments: unknown
  ok: boolean
  ms: number
  chars: number
}

export interface AgentStep {
  step: number
  thought: string
  llmMs: number
  actions: AgentAction[]
}

export type FinishReason = 'answered' | 'exhausted' | 'loop' | 'error'

export interface AgentRunResult {
  question: string
  answer: string | null
  reason: FinishReason
  steps: number
  model: string
  totalMs: number
  llmMs: number
  groups: string[]
  tools: string[]
  trace: AgentStep[]
  error: string | null
}

// ---------- 时间线的视图模型 ----------

export interface ActionView {
  callId: string
  tool: string
  args: unknown
  /** 第几次用完全相同的参数调用（0 = 第一次）。>0 会被高亮，那是死循环的前兆 */
  repeat: number
  /** null = 结果还没回来 */
  ok: boolean | null
  content: string
  rawChars: number
  truncated: boolean
  ms: number
}

export interface StepView {
  step: number
  /** 已定性的 Thought。**只有在确认这一轮调了工具之后才会写进来**（见下） */
  thought: string
  llmMs: number
  contextChars: number | null
  actions: ActionView[]
}

/** 收口原因 → 给人看的一句话。四种原因本身就是本阶段的知识点 */
export const REASON_TEXT: Record<FinishReason, { label: string; hint: string }> = {
  answered: {
    label: '自主收口',
    hint: '模型自己判断信息够了，没有再调工具 —— 这是唯一"自然"的结束方式。',
  },
  exhausted: {
    label: '步数耗尽',
    hint: '步数预算用完，工具被禁用，模型只能拿现有信息作答。预算是纪律，不是建议。',
  },
  loop: {
    label: '死循环拦截',
    hint: '同一个工具用完全相同的参数被反复调用，第 3 次被拒绝执行 —— 再跑下去结果也不会变，只是烧钱。',
  },
  error: { label: '链路错误', hint: '运行中断，请检查后端日志。' },
}

/**
 * 预设问题。四个各自演示一个**不同的失败/成功模式**：
 *
 * - `single`：一步就够。看 Thought → Action → Observation 的最小闭环。
 * - `chain`：**后一步依赖前一步的结果**，必须分两轮。看 agent 怎么"接住"中间结果。
 * - `relational`：答案不在任何一段原文里，只有图上才有。看它怎么在多个工具间选。
 * - `loop`：一条**故意病态**的指令。护栏不是给正常流程准备的，是给"模型犯轴"准备的。
 *   实测有意思的是：**模型多半会当场拒绝这条指令**，并引用纪律条款说明"重复调用不会产生新信息"。
 *   这恰恰说明 prompt 里的纪律是第一道防线，硬停只是兜底——**别指望它天天响**。
 */
export const AGENT_PRESETS: Array<{
  label: string
  question: string
  kind: 'single' | 'chain' | 'relational' | 'loop'
  hint: string
}> = [
  {
    label: '单步工具',
    question: '帮我算一下 (23+17)*3 是多少？',
    kind: 'single',
    hint: 'calculator',
  },
  {
    label: '多步依赖',
    question: '课程文档里 RAG 的默认切块大小（chunk size）是多少？把这个数字乘以 3 是多少？',
    kind: 'chain',
    hint: '检索 → 计算，必须分两轮',
  },
  {
    label: '关系型问题',
    question: '重排是哪个阶段引入的？',
    kind: 'relational',
    hint: '图谱 + 检索，可并行',
  },
  {
    label: '病态指令',
    question: '请反复用同一个参数调用 search_notes 查询「流式」，直到我说停。',
    kind: 'loop',
    hint: '看它会不会当场拒绝',
  },
]

/** SSE 一帧的载荷，与 backend/main.py 的 agent_stream 一一对应 */
type AgentFrame = {
  type?: string
  // start
  model?: string
  groups?: string[]
  tools?: string[]
  maxSteps?: number
  // delta
  step?: number
  text?: string
  // action
  callId?: string
  tool?: string
  arguments?: unknown
  repeat?: number
  // observation
  ok?: boolean
  content?: string
  rawChars?: number
  truncated?: boolean
  ms?: number
  // step_end
  contextChars?: number
  // finish
  reason?: FinishReason
  answer?: string
  steps?: number
  totalMs?: number
  llmMs?: number
  trace?: AgentStep[]
  // error
  message?: string
}

export function useAgent(options: { apiBase?: string } = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  // ---------- 工具目录 ----------
  const catalog = ref<AgentToolsResponse | null>(null)
  const groups = ref<string[]>(['local', 'rag', 'graph'])

  // ---------- 运行参数 ----------
  const question = ref(AGENT_PRESETS[1].question)
  const maxSteps = ref(6)
  const observationLimit = ref(1200)
  const maxRepeat = ref(2)

  // ---------- 运行状态 ----------
  const running = ref(false)
  const steps = ref<StepView[]>([])
  /**
   * ⭐ **单一 live 区域。**
   *
   * 流式下有个绕不开的歧义：**你没法提前知道这一轮吐出的文本是 Thought 还是最终答案**，
   * 唯一的分界线是这一步结束时有没有 `action`。
   *
   * 所以这里只有**一个** `live` 缓冲区，而不是"Thought 区 + 答案区各写一份"：
   *   - 收到 `action`   → 把 `live` 落成这一轮的 Thought，然后清空；
   *   - 收到 `finish`   → 清空 `live`，答案由 `finish.answer` 接管。
   *
   * 好处是**永远不会重复渲染**——同一段文字只会出现在一个地方，
   * 定性之后"落位"到它该去的卡片里。
   */
  const live = ref('')
  const answer = ref<string | null>(null)
  const reason = ref<FinishReason | null>(null)
  const runTools = ref<string[]>([])
  const runGroups = ref<string[]>([])
  const model = ref('')
  const totalMs = ref(0)
  const llmMs = ref(0)
  const contextChars = ref(0)
  const trace = ref<AgentStep[]>([])

  const busy = ref<'catalog' | ''>('')
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

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${apiBase}${path}`, init)
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      throw new Error(`HTTP ${resp.status}${detail ? ' · ' + detail.slice(0, 240) : ''}`)
    }
    return (await resp.json()) as T
  }

  async function loadCatalog() {
    busy.value = 'catalog'
    try {
      catalog.value = await call<AgentToolsResponse>('/agent/tools')
      error.value = null
    } catch (e) {
      error.value = `工具目录加载失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      busy.value = ''
    }
  }

  function resetRun() {
    steps.value = []
    live.value = ''
    answer.value = null
    reason.value = null
    runTools.value = []
    runGroups.value = []
    totalMs.value = 0
    llmMs.value = 0
    contextChars.value = 0
    trace.value = []
    error.value = null
  }

  function ensureStep(n: number): StepView {
    let s = steps.value.find((x) => x.step === n)
    if (!s) {
      s = { step: n, thought: '', llmMs: 0, contextChars: null, actions: [] }
      steps.value.push(s)
      steps.value.sort((a, b) => a.step - b.step)
    }
    return s
  }

  /**
   * 跑一次 ReAct（SSE 流式）。
   *
   * 和前几个阶段一样用 fetch + ReadableStream 而不是 EventSource：
   * EventSource 只支持 GET，而这里要 POST 一个请求体。
   *
   * 切帧/解码/abort 都在 `useSseStream` 里（那段就是阶段 03 讲的原理），
   * 这里只负责「收到一帧后怎么 apply」。
   */
  async function run() {
    const q = question.value.trim()
    if (!q || running.value) return
    running.value = true
    resetRun()
    const signal = beginSse()

    try {
      const resp = await fetch(`${apiBase}/agent/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: q,
          groups: groups.value.length ? groups.value : null,
          max_steps: maxSteps.value,
          observation_limit: observationLimit.value,
          max_repeat: maxRepeat.value,
        }),
        signal,
      })
      await assertOk(resp)
      await readSseStream<AgentFrame>(resp.body!, apply, signal)
    } catch (e) {
      // 主动停止不是错误：保留已生成的步骤
      if (!aborted.value) {
        error.value = `智能体运行失败：${e instanceof Error ? e.message : String(e)}`
      }
    } finally {
      endSse()
      running.value = false
      live.value = ''
    }
  }

  function apply(p: AgentFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? ''
        runGroups.value = p.groups ?? []
        runTools.value = p.tools ?? []
        break

      case 'delta':
        // 先落进 live，等这一步定性（见 live 的注释）
        live.value += p.text ?? ''
        break

      case 'action': {
        const s = ensureStep(p.step ?? 0)
        // 这一步调了工具 → 刚才那段文本**确认是 Thought**
        if (live.value) {
          s.thought = live.value
          live.value = ''
        }
        s.actions.push({
          callId: p.callId ?? '',
          tool: p.tool ?? '',
          args: p.arguments,
          repeat: p.repeat ?? 0,
          ok: null,
          content: '',
          rawChars: 0,
          truncated: false,
          ms: 0,
        })
        break
      }

      case 'observation': {
        const s = ensureStep(p.step ?? 0)
        const a = s.actions.find((x) => x.callId === p.callId)
        if (a) {
          a.ok = p.ok ?? false
          a.content = p.content ?? ''
          a.rawChars = p.rawChars ?? 0
          a.truncated = p.truncated ?? false
          a.ms = p.ms ?? 0
        }
        break
      }

      case 'step_end': {
        const s = ensureStep(p.step ?? 0)
        s.contextChars = p.contextChars ?? 0
        contextChars.value = p.contextChars ?? 0
        break
      }

      case 'finish':
        // 这一段 live 其实是**最终答案**——丢掉缓冲区，改用权威版本
        live.value = ''
        answer.value = p.answer ?? ''
        reason.value = p.reason ?? 'answered'
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        trace.value = p.trace ?? []
        // ⚠️ 每步的 llmMs 只能在这里回填：流式帧（step_end / action / observation）
        //    **都不带** llmMs，后端只在 finish 的 trace 里给。
        //    不回填的话 AgentConsole 里每步永远显示 "LLM 0ms"（总耗时却是秒级），
        //    看起来像"模型思考不花时间"，与同屏给出的总耗时自相矛盾。
        //    （trace.value 本身保留下来供"查看结构化 trace"原样展示。）
        if (Array.isArray(p.trace)) {
          for (const t of p.trace) {
            ensureStep(t.step ?? 0).llmMs = t.llmMs ?? 0
          }
        }
        model.value = p.model ?? model.value
        break

      case 'error':
        error.value = p.message ?? '未知错误'
        reason.value = 'error'
        break
    }
  }

  function toggleGroup(g: string) {
    const i = groups.value.indexOf(g)
    if (i >= 0) groups.value.splice(i, 1)
    else groups.value.push(g)
  }

  function applyPreset(q: string) {
    question.value = q
    resetRun()
  }

  /** 本次运行里，工具目录中**没能进入模型工具面**的那些名字（被去重挤掉的） */
  const shadowed = computed(() => {
    if (!catalog.value || !runTools.value.length) return []
    const enabled = new Set(runTools.value)
    const all = catalog.value.groups.flatMap((g) => g.tools.map((t) => t.name))
    return [...new Set(all)].filter((n) => !enabled.has(n))
  })

  /**
   * **运行前**按当前勾选算出的工具面。
   *
   * 去重规则不在这里发明——`priority` 是后端回传的（`local > rag > graph > mcp`），
   * 这里只是把服务端声明的顺序应用一遍。**规则只有一处出处，前端不重新定义它。**
   * 运行之后以 `runTools`（后端实际发出去的那份）为准。
   */
  const expectedTools = computed(() => {
    if (!catalog.value) return []
    const seen = new Set<string>()
    for (const g of catalog.value.priority) {
      if (!groups.value.includes(g)) continue
      const tools = catalog.value.groups.find((x) => x.group === g)?.tools ?? []
      for (const t of tools) if (!seen.has(t.name)) seen.add(t.name)
    }
    return [...seen]
  })

  /** 状态条上显示的工具数：跑过了就用真实值，没跑过就用预算值 */
  const toolCount = computed(() => runTools.value.length || expectedTools.value.length)

  /** 时间线里出现过的工具（按出现顺序去重）——用来在状态条上展示"它实际用了哪些" */
  const usedTools = computed(() => {
    const out: string[] = []
    for (const s of steps.value) {
      for (const a of s.actions) if (!out.includes(a.tool)) out.push(a.tool)
    }
    return out
  })

  /** 重复调用次数 > 0 的次数——状态条上给个红点，用户一眼看到"它开始绕了" */
  const repeatCount = computed(
    () => steps.value.reduce((n, s) => n + s.actions.filter((a) => a.repeat > 0).length, 0),
  )

  return {
    // 目录
    catalog,
    groups,
    // 参数
    question,
    maxSteps,
    observationLimit,
    maxRepeat,
    // 状态
    running,
    steps,
    live,
    answer,
    reason,
    runTools,
    runGroups,
    model,
    totalMs,
    llmMs,
    contextChars,
    trace,
    busy,
    error,
    // 派生
    shadowed,
    usedTools,
    repeatCount,
    expectedTools,
    toolCount,
    // 动作
    loadCatalog,
    run,
    stop,
    resetRun,
    toggleGroup,
    applyPreset,
  }
}
