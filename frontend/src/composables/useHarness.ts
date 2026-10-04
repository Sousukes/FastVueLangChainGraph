import { computed, ref } from 'vue'
import { assertOk, readSseStream, useSseAbort } from './useSseStream'
import type { AgentColumn, AgentStep } from './useTeam'

/**
 * 阶段 12 · Agent Harness 框架。
 *
 * 多智能体（阶段 11）是「一组按 agent 分列的时间线」；单智能体（阶段 10）是
 * 「一条时间线」。本阶段的 harness 控制台把这两者**统一**了——它跑的就是
 * 阶段 11 里那个 `harness.Agent`，只是一开始只选一个角色。所以这里的视图模型
 * 直接复用阶段 11 的 `AgentColumn`：选一个角色单独跑，渲染出来和它在多智能体
 * 里那一栏**一模一样**。这就是「框架复用」最直观的证据。
 */

export interface HarnessRoleInfo {
  name: string
  label: string
  groups: string[]
  tools: string[]
  maxSteps: number
  temperature: number
  description: string
}

export interface HarnessRolesResponse {
  roles: HarnessRoleInfo[]
  groups: string[]
}

/** SSE 一帧的载荷，与 backend/main.py 的 harness_stream 一一对应 */
type HarnessFrame = {
  type?: string
  agent?: string
  question?: string
  tools?: string[]
  maxSteps?: number
  model?: string
  // delta / action / observation / step_end
  step?: number
  text?: string
  callId?: string
  tool?: string
  arguments?: unknown
  repeat?: number
  ok?: boolean | null
  content?: string
  rawChars?: number
  truncated?: boolean
  ms?: number
  // finish
  answer?: string | null
  totalMs?: number
  llmMs?: number
  steps?: number
  reason?: string | null
  /**
   * finish 帧独有：后端把整条 trace 回传，每步都带**真实** llmMs。
   * 流式过程中的 step_end / action / observation 帧都**不带** llmMs，
   * 所以每步只能在这里回填（详见 finish 分支的注释）。
   */
  trace?: Array<{ step?: number; llmMs?: number }>
  // error
  message?: string
}

/** 预设：每个角色挑一个最能显出它工具面的问题 */
export const HARNESS_PRESETS: Array<{ role: string; label: string; question: string; hint: string }> = [
  {
    role: 'researcher',
    label: '检索员',
    question: 'RAG 的默认切块大小是多少？它是在哪个阶段讲的？',
    hint: '会真实调用检索 + 知识图谱，证据来自课程文档',
  },
  {
    role: 'analyst',
    label: '分析员',
    question: '把 (400 + 200) 乘 2，再除以 12，结果保留两位小数是多少？',
    hint: '只会调计算器，不碰任何资料',
  },
  {
    role: 'pure',
    label: '纯净助手',
    question: '用一句话解释什么是 ReAct。',
    hint: '无工具，框架退化为纯对话',
  },
]

export function useHarness(options: { apiBase?: string } = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  // ---------- 角色目录 ----------
  const roles = ref<HarnessRoleInfo[]>([])
  const groups = ref<string[]>([])

  // ---------- 运行参数 ----------
  const role = ref('researcher')
  const question = ref(HARNESS_PRESETS[0].question)
  const maxSteps = ref(6)
  const temperature = ref(0.2)
  const observationLimit = ref(1200)

  // ---------- 运行状态 ----------
  const running = ref(false)
  const column = ref<AgentColumn | null>(null)
  const answer = ref<string | null>(null)
  const model = ref('')
  const totalMs = ref(0)
  const llmMs = ref(0)
  const steps = ref(0)
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
  const busy = ref(false)

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${apiBase}${path}`, init)
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      throw new Error(`HTTP ${resp.status}${detail ? ' · ' + detail.slice(0, 240) : ''}`)
    }
    return (await resp.json()) as T
  }

  async function loadRoles() {
    busy.value = true
    try {
      const data = await call<HarnessRolesResponse>('/harness/roles')
      roles.value = data.roles
      groups.value = data.groups
      // 默认选中第一个角色、并填充它的预设问题
      if (data.roles.length && !roles.value.some((r) => r.name === role.value)) {
        role.value = data.roles[0].name
      }
      error.value = null
    } catch (e) {
      error.value = `角色目录加载失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      busy.value = false
    }
  }

  function resetRun() {
    column.value = null
    answer.value = null
    totalMs.value = 0
    llmMs.value = 0
    steps.value = 0
    elapsedMs.value = 0
    error.value = null
  }

  function ensureColumn(roleName: string): AgentColumn {
    if (!column.value || column.value.id !== roleName) {
      const info = roles.value.find((r) => r.name === roleName)
      column.value = {
        id: roleName,
        role: roleName,
        label: info?.label ?? roleName,
        kind: '',
        groups: info?.groups ?? [],
        tools: info?.tools ?? [],
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
    }
    return column.value
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
    const r = role.value
    if (!q || !r || running.value) return
    running.value = true
    resetRun()
    const c = ensureColumn(r)
    c.started = true
    startedAt.value = Date.now()
    if (timer.value) clearInterval(timer.value)
    timer.value = setInterval(() => {
      elapsedMs.value = Date.now() - startedAt.value
    }, 120)

    const signal = beginSse()

    try {
      const resp = await fetch(`${apiBase}/harness/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          role: r,
          question: q,
          maxSteps: maxSteps.value,
          temperature: temperature.value,
          observationLimit: observationLimit.value,
        }),
        signal,
      })
      await assertOk(resp)
      await readSseStream<HarnessFrame>(resp.body!, apply, signal)
    } catch (e) {
      // 主动停止不是错误：保留已生成的结果
      if (aborted.value) return
      error.value = `harness 运行失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      endSse()
      running.value = false
      if (timer.value) clearInterval(timer.value)
      elapsedMs.value = 0
      if (column.value) column.value.live = ''
    }
  }


  function apply(p: HarnessFrame) {
    const id = p.agent ?? role.value
    switch (p.type) {
      case 'start': {
        const c = ensureColumn(id)
        c.tools = p.tools ?? c.tools
        c.label = roles.value.find((r) => r.name === id)?.label ?? c.label
        model.value = p.model ?? model.value
        break
      }
      case 'delta': {
        const c = ensureColumn(id)
        c.live += p.text ?? ''
        break
      }
      case 'action': {
        const c = ensureColumn(id)
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
        const c = ensureColumn(p.agent ?? id)
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
      case 'step_end': {
        const c = ensureColumn(p.agent ?? id)
        const s = ensureStep(c, p.step ?? 0)
        if (c.live) {
          // 这一步没有工具调用：live 里攒的就是最终答案的增量
          s.thought = c.live
          c.live = ''
        }
        break
      }
      case 'finish': {
        const c = ensureColumn(id)
        c.ended = true
        c.live = ''
        answer.value = p.answer ?? null
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        steps.value = p.steps ?? 0
        c.totalMs = p.totalMs ?? 0
        c.llmMs = p.llmMs ?? 0
        c.stepsCount = p.steps ?? 0
        c.answer = p.answer ?? null
        c.reason = p.reason ?? null
        model.value = p.model ?? model.value
        // ⚠️ 每步的 llmMs 只能在这里回填：流式帧（step_end / action / observation）
        //    **都不带** llmMs，后端只在 finish 的 trace 里给。
        //    不回填的话 HarnessConsole 里每步永远显示 "LLM 0ms"（而总耗时是 8s 级），
        //    看起来像"模型思考不花时间"，与页面同时给出的总耗时自相矛盾。
        if (Array.isArray(p.trace)) {
          for (const t of p.trace) {
            ensureStep(c, t.step ?? 0).llmMs = t.llmMs ?? 0
          }
        }
        break
      }
      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  const finished = computed(() => answer.value !== null || error.value !== null)
  const wallMs = computed(() => totalMs.value || elapsedMs.value)

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
    roles,
    groups,
    role,
    question,
    maxSteps,
    temperature,
    observationLimit,
    running,
    column,
    answer,
    model,
    totalMs,
    llmMs,
    steps,
    elapsedMs,
    wallMs,
    error,
    finished,
    busy,
    HARNESS_PRESETS,
    loadRoles,
    run,
    stop,
    resetRun,
    fmtArgs,
    fmtChars,
  }
}
