import { computed, ref } from 'vue'

/**
 * 阶段 18 · Computer Use（仿制）。
 *
 * 与阶段 16/17 的 composable 不同，这里跑的是一个**闭环**：
 * 每一轮都要「看截图 → 决定动作 → 执行 → 再看新截图」，所以状态是**累积**的
 * （一串截图 + 一串动作），而不是一份最终结果。
 *
 * 于是这一层的活有两件：
 *   1. 把 SSE 帧还原成「第 N 步的屏幕」和「第 N 步做了什么」；
 *   2. ⭐ 把**动作坐标**和**元素真值**两路数据摆到一起——屏幕图上那些落点标记，
 *      就靠 `clicksOnView`。这是本阶段的签名元素：模型点在哪 vs 按钮真的在哪。
 *
 * 帧协议与后端 computer.py 一一对应：start / screen / delta / action / finish / error。
 *
 * ⚠️ finish **一定会到**（失败时也到，带部分结果），所以判断成败要看 `success` / `error`，
 *    不要靠"流断了没有"。
 */

/** 虚拟屏幕上的一个元素 —— 后端给的就是**真值**（我们渲染的屏幕，坐标精确已知） */
export interface ComputerElement {
  name: string
  kind: string
  x: number
  y: number
  w: number
  h: number
  label: string
  dangerous: boolean
}

export interface ComputerScreen {
  width: number
  height: number
  elements: ComputerElement[]
}

/** 一次被执行的（或被拒绝的）动作 + 确定性评分 */
export interface ComputerAction {
  step: number
  action: string
  x?: number | null
  y?: number | null
  text?: string | null
  keys?: string | null
  ok: boolean
  error?: string | null
  /** 落点所在元素；None 表示点在空白处 */
  target?: string | null
  hit?: boolean
  /** 点空时：落点到最近元素矩形的像素距离 */
  errorPx?: number | null
  /** 命中时：落点到**目标元素中心**的像素距离 —— 命中率高≠点得准 */
  centerOffsetPx?: number | null
  /** 这次 type 的按键被丢弃了（前一次点击没点中输入框） */
  lostKeys?: boolean
  /** 后端回传给模型的那句观察结果 —— 直接显示，不自己编话术 */
  note?: string | null
}

/** 一帧屏幕截图 */
export interface ComputerShot {
  step: number
  image: string
  state: Record<string, string>
  revision?: number
}

/** 任务级 + 定位级读数（全部确定性，无一来自模型自述） */
export interface ComputerScore {
  fieldScore: number
  fieldTotal: number
  submitted: boolean
  success: boolean
  clicks: number
  hitClicks: number
  clickHitRate: number
  avgClickErrorPx: number
  /** 命中时离目标中心的平均偏差 —— 全部点中时仍有值，可横向比较 */
  avgCenterOffsetPx: number
  lostKeystrokes: number
}

/** 一个「大脑」的自述 —— 阶段 18B：同一个循环，第②步可换 */
export interface ComputerProviderInfo {
  provider: string
  label: string
  protocol: string
  model: string
  /** 现在能不能用（缺 Key 时为 false） */
  available: boolean
  /** 不可用的原因，直接展示给用户 */
  reason?: string | null
  /** 需要哪个环境变量 —— 前端据此给出配置指引 */
  keyEnv: string
  endpoint: string
  toolVersion?: string | null
  betaHeader?: string | null
}

export interface ComputerProvidersResponse {
  providers: ComputerProviderInfo[]
  /** provider=auto 时后端实际会选中哪个 */
  default: string
  note: string
}

/** SSE 一帧的载荷，与 backend/main.py 的 computer_stream 一一对应 */
type ComputerFrame = {
  type?: string
  // start
  task?: string
  model?: string
  provider?: string
  protocol?: string | null
  brain?: Record<string, unknown>
  screen?: ComputerScreen
  expected?: Record<string, string>
  maxSteps?: number
  allowDangerous?: boolean
  // screen
  step?: number
  image?: string
  state?: Record<string, string>
  revision?: number
  // delta
  text?: string
  // action
  action?: string
  x?: number | null
  y?: number | null
  ok?: boolean
  error?: string | null
  target?: string | null
  hit?: boolean
  errorPx?: number | null
  centerOffsetPx?: number | null
  lostKeys?: boolean
  note?: string | null
  keys?: string | null
  // finish
  steps?: number
  transcript?: string[]
  fieldScore?: number
  fieldTotal?: number
  submitted?: boolean
  success?: boolean
  clicks?: number
  hitClicks?: number
  clickHitRate?: number
  avgClickErrorPx?: number
  avgCenterOffsetPx?: number
  lostKeystrokes?: number
  rejectedActions?: number
  times?: Record<string, number>
  totalMs?: number
  llmMs?: number
  // error
  message?: string
}

export interface UseComputerOptions {
  apiBase?: string
}

export function useComputer(options: UseComputerOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  // ---- 输入 ----
  const orderId = ref('ORDER-2026-0917')
  const amount = ref('2158.50')
  const date = ref('2026-09-21')
  const task = ref('')
  const maxSteps = ref(8)
  const allowDangerous = ref(false)
  const showBoxes = ref(true)
  /**
   * 用哪个大脑（阶段 18B）。'auto' = 后端按「有没有 CLAUDE_API_KEY」自己选。
   * ⚠️ 这个选项只影响第②步；屏幕、沙箱、评分对它是无感知的。
   */
  const provider = ref<'auto' | 'deepseek' | 'claude'>('auto')

  // ---- 运行状态 ----
  const running = ref(false)
  const error = ref<string | null>(null)

  // ---- 结果：屏幕规格 + 每步截图 + 动作流水 ----
  const model = ref('')
  /** 这一轮**实际**用了哪个大脑（由 start / finish 帧回报，不是前端猜的） */
  const activeProvider = ref('')
  const protocol = ref('')
  const screen = ref<ComputerScreen | null>(null)
  const expected = ref<Record<string, string>>({})
  const shots = ref<ComputerShot[]>([])
  const actions = ref<ComputerAction[]>([])
  const narration = ref<Record<number, string>>({})
  const finalState = ref<Record<string, string>>({})

  // ---- 评分 ----
  const score = ref<ComputerScore>({
    fieldScore: 0,
    fieldTotal: 3,
    submitted: false,
    success: false,
    clicks: 0,
    hitClicks: 0,
    clickHitRate: 0,
    avgClickErrorPx: 0,
    avgCenterOffsetPx: 0,
    lostKeystrokes: 0,
  })
  const stepsUsed = ref(0)
  const times = ref<Record<string, number>>({})
  const totalMs = ref(0)
  const llmMs = ref(0)
  /** 被沙箱/白名单拒绝的动作数 —— 走 Claude 时通常 > 0（它的动作词表比宿主宽） */
  const rejectedActions = ref(0)
  const finished = ref(false)

  /** 用户可以在步进条上回看任意一步；null 表示跟随最新 */
  const viewStep = ref<number | null>(null)

  const latestStep = computed(() => (shots.value.length ? shots.value[shots.value.length - 1].step : 0))

  const viewShot = computed<ComputerShot | null>(() => {
    if (!shots.value.length) return null
    const want = viewStep.value ?? latestStep.value
    return shots.value.find((s) => s.step === want) ?? shots.value[shots.value.length - 1]
  })

  const viewStepNo = computed(() => (viewShot.value ? viewShot.value.step : 0))

  /** ⭐ 签名元素的数据来源：当前显示的那一步，模型点在哪 */
  const clicksOnView = computed(() =>
    actions.value.filter((a) => a.step === viewStepNo.value && a.action === 'left_click' && a.ok),
  )

  /** 当前显示那一步的动作（时间线高亮用） */
  const actionsOnView = computed(() => actions.value.filter((a) => a.step === viewStepNo.value))

  // ---- 可用的「大脑」（阶段 18B）----
  const providers = ref<ComputerProviderInfo[]>([])
  const providersNote = ref('')
  const providersError = ref<string | null>(null)
  const defaultProvider = ref('deepseek')

  /**
   * ⭐ 为什么要单独拉一次名单：没有 Key 时最糟的体验是**点了才发现不能用**。
   * 进页面就问一次，于是可以把按钮直接置灰并给出配置指引。
   */
  async function fetchProviders() {
    try {
      const resp = await fetch(`${apiBase}/computer/providers`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = (await resp.json()) as ComputerProvidersResponse
      providers.value = data.providers ?? []
      providersNote.value = data.note ?? ''
      defaultProvider.value = data.default ?? 'deepseek'
      providersError.value = null
    } catch (e) {
      providersError.value = e instanceof Error ? e.message : String(e)
    }
  }

  /** 'auto' 在后端实际等于谁 */
  const effectiveProviderId = computed(() =>
    provider.value === 'auto' ? defaultProvider.value : provider.value,
  )

  const selectedProviderInfo = computed<ComputerProviderInfo | null>(
    () => providers.value.find((p) => p.provider === effectiveProviderId.value) ?? null,
  )

  /** 选中的大脑现在不可用 → 按钮置灰（找不到名单时不过度拦截） */
  const providerBlocked = computed(
    () => providers.value.length > 0 && selectedProviderInfo.value?.available === false,
  )

  const canRun = computed(() => !running.value && !providerBlocked.value)

  /** 屏幕容器的宽高比，覆盖层才不会被拉变形 */
  const aspectStyle = computed(() => ({
    aspectRatio: screen.value ? `${screen.value.width} / ${screen.value.height}` : '4 / 3',
  }))

  function clearResult() {
    screen.value = null
    expected.value = {}
    shots.value = []
    actions.value = []
    narration.value = {}
    finalState.value = {}
    score.value = {
      fieldScore: 0,
      fieldTotal: 3,
      submitted: false,
      success: false,
      clicks: 0,
      hitClicks: 0,
      clickHitRate: 0,
      avgClickErrorPx: 0,
      avgCenterOffsetPx: 0,
      lostKeystrokes: 0,
    }
    activeProvider.value = ''
    protocol.value = ''
    rejectedActions.value = 0
    stepsUsed.value = 0
    times.value = {}
    totalMs.value = 0
    llmMs.value = 0
    finished.value = false
    viewStep.value = null
  }

  async function run() {
    if (!canRun.value) return
    running.value = true
    clearResult()
    error.value = null

    try {
      const resp = await fetch(`${apiBase}/computer/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task: task.value.trim() || null,
          orderId: orderId.value,
          amount: amount.value,
          date: date.value,
          maxSteps: maxSteps.value,
          allowDangerous: allowDangerous.value,
          provider: provider.value,
        }),
      })
      if (!resp.ok || !resp.body) {
        const detailText = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detailText.slice(0, 200)}`)
      }
      await readStream(resp.body)
    } catch (e) {
      error.value = `Computer Use 运行失败：${e instanceof Error ? e.message : String(e)}`
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
          let payload: ComputerFrame
          try {
            payload = JSON.parse(line.slice(5).trim()) as ComputerFrame
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

  function apply(p: ComputerFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? model.value
        activeProvider.value = p.provider ?? activeProvider.value
        protocol.value = p.protocol ?? protocol.value
        screen.value = p.screen ?? screen.value
        expected.value = p.expected ?? {}
        break
      case 'screen': {
        const step = p.step ?? 0
        shots.value.push({
          step,
          image: p.image ?? '',
          state: p.state ?? {},
          revision: p.revision,
        })
        // 新的一步到了，就自动跟到最新（用户手动回看时不打扰）
        if (viewStep.value === null) viewStep.value = null
        finalState.value = p.state ?? finalState.value
        break
      }
      case 'delta': {
        const step = p.step ?? 0
        narration.value[step] = (narration.value[step] ?? '') + (p.text ?? '')
        break
      }
      case 'action':
        actions.value.push({
          step: p.step ?? 0,
          action: p.action ?? '?',
          x: p.x,
          y: p.y,
          text: p.text,
          keys: p.keys,
          ok: p.ok ?? false,
          error: p.error,
          target: p.target,
          hit: p.hit,
          errorPx: p.errorPx,
          centerOffsetPx: p.centerOffsetPx,
          lostKeys: p.lostKeys,
          note: p.note,
        })
        break
      case 'finish':
        score.value = {
          fieldScore: p.fieldScore ?? 0,
          fieldTotal: p.fieldTotal ?? 3,
          submitted: !!p.submitted,
          success: !!p.success,
          clicks: p.clicks ?? 0,
          hitClicks: p.hitClicks ?? 0,
          clickHitRate: p.clickHitRate ?? 0,
          avgClickErrorPx: p.avgClickErrorPx ?? 0,
          avgCenterOffsetPx: p.avgCenterOffsetPx ?? 0,
          lostKeystrokes: p.lostKeystrokes ?? 0,
        }
        rejectedActions.value = p.rejectedActions ?? 0
        activeProvider.value = p.provider ?? activeProvider.value
        protocol.value = p.protocol ?? protocol.value
        stepsUsed.value = p.steps ?? 0
        times.value = p.times ?? {}
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        finalState.value = p.state ?? finalState.value
        finished.value = true
        // finish 的 transcript 是权威版本（服务端存的是每轮完整说明）
        if (p.transcript && p.transcript.length) {
          const next: Record<number, string> = {}
          p.transcript.forEach((t, i) => {
            next[i + 1] = t
          })
          narration.value = next
        }
        if (p.error) error.value = p.error
        break
      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  /** 该步模型说了什么（空则不显示） */
  function stepNarration(step: number): string {
    return (narration.value[step] ?? '').trim()
  }

  /** 字段值是否与期望一致（前端只做展示；判分在后端，两边规则都是"忽略空白与大小写"） */
  function fieldOk(name: string): boolean {
    const want = expected.value[name]
    if (!want) return false
    return (finalState.value[name] ?? '').trim().toUpperCase() === want.trim().toUpperCase()
  }

  /** 元素中心点（画覆盖层时标出"真值"位置） */
  function centerOf(el: ComputerElement): { cx: number; cy: number } {
    return { cx: el.x + el.w / 2, cy: el.y + el.h / 2 }
  }

  /** 动作的一行摘要（模板里别写带逗号的复合表达式） */
  /** 像素数字统一格式（模板里不写 toFixed） */
  function px(v?: number | null): string {
    return v === null || v === undefined ? '' : `${v.toFixed(1)}px`
  }

  function actionSummary(a: ComputerAction): string {
    if (a.action === 'left_click') return `(${a.x ?? '?'}, ${a.y ?? '?'})`
    if (a.action === 'type') return a.text ?? ''
    if (a.action === 'key') return a.keys ?? ''
    return ''
  }

  return {
    // 输入
    orderId,
    amount,
    date,
    task,
    maxSteps,
    allowDangerous,
    showBoxes,
    provider,
    // 状态
    running,
    error,
    canRun,
    finished,
    // 屏幕与动作
    model,
    activeProvider,
    protocol,
    screen,
    expected,
    shots,
    actions,
    actionsOnView,
    clicksOnView,
    finalState,
    narration,
    latestStep,
    viewShot,
    viewStepNo,
    viewStep,
    aspectStyle,
    // 评分
    score,
    rejectedActions,
    stepsUsed,
    times,
    totalMs,
    llmMs,
    // 动作
    run,
    clearResult,
    fetchProviders,
    providers,
    providersNote,
    providersError,
    defaultProvider,
    effectiveProviderId,
    selectedProviderInfo,
    providerBlocked,
    stepNarration,
    fieldOk,
    centerOf,
    actionSummary,
    px,
  }
}
