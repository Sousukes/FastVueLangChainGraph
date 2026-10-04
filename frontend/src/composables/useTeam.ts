import { computed, ref } from 'vue'
import { assertOk, readSseStream, useSseAbort } from './useSseStream'

/**
 * 阶段 11：多智能体（Multi-agent）。
 *
 * 这个 composable 在阶段 10 的基础上多了**一个关键维度：`agent`**。
 *
 * 阶段 10 是一条"单角色时间线"——Thought / Action / Observation 全都属于同一个智能体。
 * 阶段 11 里，同一时刻可能有三四个角色在并发跑，事件**交织着**从 SSE 流里涌出来，
 * 每一帧都带一个 `agent` 字段（任务编号 t1 / t2 …）。所以这里的状态不是一条时间线，
 * 而是**一组按 agent 分列的时间线**：每个角色一栏，各自的 Thought / Action / Observation 落进各自的栏。
 *
 * ⭐ 本阶段真正要讲的东西——**多智能体并不更聪明，只是更可控、更可观测，
 * 代价是耗时按角色数线性增长**——必须有数据支撑。所以这里额外跑一次**单智能体**
 * （同一个问题、同一个模型）做并排对照：左栏多智能体、右栏单智能体，
 * 直接把"多付的时间"和"买到的东西（证据 / 评审）"摆在一起看。
 */

// ---------- 类型（与 backend/schemas.py 的阶段 11 段一一对应） ----------

export interface TeamRoleInfo {
  name: string
  label: string
  groups: string[]
  tools: string[]
  maxSteps: number
  temperature: number
  kinds: string[]
}

export interface TeamRolesResponse {
  roles: TeamRoleInfo[]
  kinds: string[]
}

export interface TeamTask {
  id: string
  kind: string
  description: string
  dependsOn: string[]
}

export interface TeamIssue {
  taskId: string
  problem: string
}

/** 一个角色栏里的一条"步骤"（沿用阶段 10 的 StepView 形状，便于复用渲染） */
export interface AgentStep {
  step: number
  thought: string
  llmMs: number
  contextChars: number | null
  actions: Array<{
    callId: string
    tool: string
    args: unknown
    repeat: number
    ok: boolean | null
    content: string
    rawChars: number
    truncated: boolean
    ms: number
  }>
}

/** 一个角色栏的视图模型 */
export interface AgentColumn {
  id: string // 任务编号，如 t1
  role: string
  label: string
  kind: string
  groups: string[]
  tools: string[]
  started: boolean
  ended: boolean
  steps: AgentStep[]
  live: string
  /** 该角色收口后的结果（来自 agent_result 事件） */
  answer: string | null
  reason: string | null
  totalMs: number
  llmMs: number
  stepsCount: number
  /** 它实际调用过的工具（来自 handoff 里的 evidence），评审员据此核查 */
  evidence: Array<{ tool: string; arguments: unknown; observation: string }>
}

/** 单智能体对照结果（来自 /api/agent/run，阻塞接口） */
export interface SingleResult {
  answer: string | null
  reason: string | null
  steps: number
  totalMs: number
  llmMs: number
  tools: string[]
  error: string | null
}

/** SSE 一帧的载荷，与 backend/main.py 的 team_stream 一一对应 */
type TeamFrame = {
  type?: string
  // start
  roles?: string[]
  parallel?: boolean
  model?: string
  maxTasks?: number
  question?: string
  // phase
  phase?: string
  label?: string
  // plan
  tasks?: TeamTask[]
  layers?: string[][]
  ms?: number
  // layer
  agents?: Array<{ task: string; role: string; kind: string }>
  // agent_start
  agent?: string
  task?: string
  role?: string
  kind?: string
  tools?: string[]
  groups?: string[]
  // delta / action / observation
  step?: number
  text?: string
  callId?: string
  tool?: string
  arguments?: unknown
  repeat?: number
  ok?: boolean
  content?: string
  rawChars?: number
  truncated?: boolean
  // agent_result
  result?: {
    answer: string | null
    reason: string | null
    steps: number
    totalMs: number
    llmMs: number
    trace: AgentStep[]
    tools: string[]
  }
  // handoff
  handoff?: { id: string; role: string; conclusion: string; evidence: Array<{ tool: string; arguments: unknown; observation: string }> }
  // review
  verdict?: string
  issues?: TeamIssue[]
  // write
  // finish
  answer?: string | null
  totalMs?: number
  llmMs?: number
  planMs?: number
  reviewMs?: number
  writeMs?: number
  // error
  message?: string
}

/** 预设问题。刻意选"能拆成几个独立子任务"的，否则多智能体的并行红利出不来。 */
export const TEAM_PRESETS: Array<{ label: string; question: string; hint: string }> = [
  {
    label: '一个能拆的问题',
    question: 'RAG 的默认切块大小是多少？重排是哪个阶段引入的？把这两个数加起来再乘 2 是多少？',
    hint: 'research + research + compute，可并行',
  },
  {
    label: '纯检索对比',
    question: '课程里 GraphRAG 和纯向量检索各自的定位是什么？哪个更适合"连不连"类的问题？',
    hint: '两个 research 并行',
  },
  {
    label: '计算为主',
    question: '阶段 07 到阶段 10 一共有几个阶段？这些阶段编号之和乘以 3 是多少？',
    hint: 'research + compute',
  },
]

export function useTeam(options: { apiBase?: string } = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  // ---------- 角色目录 ----------
  const roles = ref<TeamRoleInfo[]>([])
  const kinds = ref<string[]>([])

  // ---------- 运行参数 ----------
  const question = ref(TEAM_PRESETS[0].question)
  const parallel = ref(true)
  const maxTasks = ref(4)
  const observationLimit = ref(1200)

  // ---------- 运行状态 ----------
  const running = ref(false)
  const columns = ref<AgentColumn[]>([])
  const rolesSeen = ref<string[]>([])
  const plan = ref<TeamTask[]>([])
  const layers = ref<string[][]>([])
  const phase = ref<string>('')
  const phaseLabel = ref<string>('')
  const writeText = ref('')
  const answer = ref<string | null>(null)
  const verdict = ref<string | null>(null)
  const issues = ref<TeamIssue[]>([])
  const model = ref('')

  // 分角色计时（本阶段要算的就是这笔账）
  const totalMs = ref(0)
  const llmMs = ref(0)
  const planMs = ref(0)
  const reviewMs = ref(0)
  const writeMs = ref(0)

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
  const startedAt = ref(0)
  const elapsedMs = ref(0)
  const timer = ref<ReturnType<typeof setInterval> | null>(null)

  // ---------- 单智能体对照 ----------
  const single = ref<SingleResult | null>(null)
  const singleRunning = ref(false)
  const showCompare = ref(false)

  const busy = ref<'roles' | ''>('')

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${apiBase}${path}`, init)
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      throw new Error(`HTTP ${resp.status}${detail ? ' · ' + detail.slice(0, 240) : ''}`)
    }
    return (await resp.json()) as T
  }

  async function loadRoles() {
    busy.value = 'roles'
    try {
      const data = await call<TeamRolesResponse>('/team/roles')
      roles.value = data.roles
      kinds.value = data.kinds
      error.value = null
    } catch (e) {
      error.value = `角色目录加载失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      busy.value = ''
    }
  }

  function resetRun() {
    columns.value = []
    rolesSeen.value = []
    plan.value = []
    layers.value = []
    phase.value = ''
    phaseLabel.value = ''
    writeText.value = ''
    answer.value = null
    verdict.value = null
    issues.value = []
    totalMs.value = 0
    llmMs.value = 0
    planMs.value = 0
    reviewMs.value = 0
    writeMs.value = 0
    elapsedMs.value = 0
    error.value = null
  }

  function ensureColumn(id: string): AgentColumn {
    let c = columns.value.find((x) => x.id === id)
    if (!c) {
      c = {
        id,
        role: '',
        label: '',
        kind: '',
        groups: [],
        tools: [],
        started: false,
        ended: false,
        steps: [],
        live: '',
        answer: null,
        reason: null,
        totalMs: 0,
        llmMs: 0,
        stepsCount: 0,
        evidence: [],
      }
      columns.value.push(c)
    }
    return c
  }

  function ensureStep(c: AgentColumn, n: number): AgentStep {
    let s = c.steps.find((x) => x.step === n)
    if (!s) {
      s = { step: n, thought: '', llmMs: 0, contextChars: null, actions: [] }
      c.steps.push(s)
      c.steps.sort((a, b) => a.step - b.step)
    }
    return s
  }

  async function run() {
    const q = question.value.trim()
    if (!q || running.value) return
    running.value = true
    resetRun()
    single.value = null
    startedAt.value = Date.now()
    if (timer.value) clearInterval(timer.value)
    timer.value = setInterval(() => {
      elapsedMs.value = Date.now() - startedAt.value
    }, 120)

    const signal = beginSse()

    try {
      const resp = await fetch(`${apiBase}/team/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: q,
          parallel: parallel.value,
          max_tasks: maxTasks.value,
          observation_limit: observationLimit.value,
        }),
        signal,
      })
      await assertOk(resp)
      await readSseStream<TeamFrame>(resp.body!, apply, signal)
    } catch (e) {
      // 主动停止不是错误：保留已生成的结果
      if (aborted.value) return
      error.value = `多智能体运行失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      endSse()
      running.value = false
      if (timer.value) clearInterval(timer.value)
      elapsedMs.value = 0
      live.value = ''
    }
  }

  const live = ref('') // 顶层 live：仅给"还没归属任何角色的瞬间"占位，正常不会用到


  function apply(p: TeamFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.roles ? '' : model.value
        break

      case 'phase':
        phase.value = p.phase ?? ''
        phaseLabel.value = p.label ?? ''
        break

      case 'plan':
        plan.value = p.tasks ?? []
        layers.value = p.layers ?? []
        planMs.value = p.ms ?? 0
        break

      case 'layer':
        // 仅标记进度，真正的列在 agent_start 时建立
        break

      case 'agent_start': {
        const id = p.agent ?? p.task ?? ''
        const c = ensureColumn(id)
        c.started = true
        c.role = p.role ?? c.role
        c.label = (roles.value.find((r) => r.name === c.role)?.label ?? c.label) || c.role
        c.kind = p.kind ?? c.kind
        c.groups = p.groups ?? c.groups
        c.tools = p.tools ?? c.tools
        if (!rolesSeen.value.includes(c.role)) rolesSeen.value.push(c.role)
        break
      }

      case 'delta': {
        const c = ensureColumn(p.agent ?? '')
        c.live += p.text ?? ''
        break
      }

      case 'action': {
        const c = ensureColumn(p.agent ?? '')
        const s = ensureStep(c, p.step ?? 0)
        if (c.live) {
          s.thought = c.live
          c.live = ''
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
        const c = ensureColumn(p.agent ?? '')
        const s = ensureStep(c, p.step ?? 0)
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

      case 'agent_result': {
        const c = ensureColumn(p.agent ?? '')
        c.live = ''
        const r = p.result
        if (r) {
          c.answer = r.answer
          c.reason = r.reason
          c.totalMs = r.totalMs
          c.llmMs = r.llmMs
          c.stepsCount = r.steps
          // ⚠️ 每个角色「每步的 llmMs」只能在这里回填：worker 的流式帧
          //    （step_end / action / observation）**都不带** llmMs，
          //    只有收口的 `agent_result`（= worker 的 finish，被 team.py 的
          //    `_normalize_worker_event` 改了名，`trace` 由 `_result_of` 带上）才有。
          //    不回填的话 TeamConsole 里每步永远显示 "LLM 0ms"（而总耗时是秒级），
          //    与该角色那一栏给出的总耗时自相矛盾。
          for (const t of r.trace ?? []) {
            ensureStep(c, t.step ?? 0).llmMs = t.llmMs ?? 0
          }
        }
        break
      }

      case 'agent_end': {
        // ⚠️ 必须和 `agent_start` 一样回落到 `p.task`：后端 team.py 的 agent_end
        //    只带 `task` 字段（`{"type": "agent_end", "task": tid}`，见 team.py 603/632 行），
        //    **从不带 `agent`**。这里若只读 `p.agent`，`ensureColumn('')` 会建出一个
        //    id 为空、标签为「·」的幽灵列并把它标成 ✓，而**真正的角色列永远停在「运行中」**。
        const c = ensureColumn(p.agent ?? p.task ?? '')
        c.ended = true
        c.live = ''
        break
      }

      case 'handoff': {
        // 把 evidence 登记到对应角色栏，供评审面板展示"它到底用了哪些工具"
        const h = p.handoff
        if (h) {
          const c = ensureColumn(h.id)
          c.evidence = h.evidence ?? []
        }
        break
      }

      case 'review':
        verdict.value = p.verdict ?? null
        issues.value = p.issues ?? []
        reviewMs.value = p.ms ?? 0
        break

      case 'write':
        writeText.value += p.text ?? ''
        break

      case 'finish':
        answer.value = p.answer ?? null
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        planMs.value = p.planMs ?? 0
        reviewMs.value = p.reviewMs ?? 0
        writeMs.value = p.writeMs ?? 0
        verdict.value = p.verdict ?? verdict.value
        issues.value = p.issues ?? issues.value
        model.value = p.model ?? model.value
        break

      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  /** 并排对照：同一个问题跑一次单智能体（阻塞接口，直接拿最终答案 + 耗时） */
  async function runCompare() {
    const q = question.value.trim()
    if (!q || singleRunning.value) return
    singleRunning.value = true
    showCompare.value = true
    try {
      single.value = await call<SingleResult>('/agent/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: q,
          groups: ['local', 'rag', 'graph'],
          max_steps: 6,
          observation_limit: observationLimit.value,
        }),
      })
    } catch (e) {
      single.value = { answer: null, reason: null, steps: 0, totalMs: 0, llmMs: 0, tools: [], error: `单智能体对照失败：${e instanceof Error ? e.message : String(e)}` }
    } finally {
      singleRunning.value = false
    }
  }

  // ---------- 派生 ----------

  const finished = computed(() => answer.value !== null || error.value !== null)

  /** 角色目录里工具面完全相同的"伪多智能体"自检：任意两个角色的 tools 一致即报警 */
  const shadowRoles = computed(() => {
    const seen = new Map<string, string>()
    const dups: string[] = []
    for (const r of roles.value) {
      const key = r.tools.join(',')
      if (seen.has(key) && !dups.includes(seen.get(key)!)) dups.push(seen.get(key)!)
      if (seen.has(key)) dups.push(r.label)
      seen.set(key, r.label)
    }
    return [...new Set(dups)]
  })

  /** 顶层计时（墙钟） */
  const wallMs = computed(() => totalMs.value || elapsedMs.value)

  /** 多智能体 vs 单智能体的耗时倍数（对照用） */
  const speedRatio = computed(() => {
    if (!single.value || !single.value.totalMs || !totalMs.value) return null
    return totalMs.value / single.value.totalMs
  })

  function fmtArgs(a: unknown): string {
    if (typeof a === 'string') return a
    try {
      return JSON.stringify(a)
    } catch {
      return String(a)
    }
  }
  function fmtChars(n: number): string {
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
    return String(n)
  }

  return {
    // 目录
    roles,
    kinds,
    // 参数
    question,
    parallel,
    maxTasks,
    observationLimit,
    // 状态
    running,
    columns,
    rolesSeen,
    plan,
    layers,
    phase,
    phaseLabel,
    writeText,
    answer,
    verdict,
    issues,
    model,
    totalMs,
    llmMs,
    planMs,
    reviewMs,
    writeMs,
    elapsedMs,
    wallMs,
    error,
    finished,
    busy,
    // 对照
    single,
    singleRunning,
    showCompare,
    runCompare,
    speedRatio,
    shadowRoles,
    // 派生
    fmtArgs,
    fmtChars,
    // 动作
    loadRoles,
    run,
    stop,
    resetRun,
  }
}
