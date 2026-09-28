import { ref } from 'vue'

export interface ToolParameter {
  name: string
  type: string
  description: string
  required: boolean
  default?: unknown
}

export interface ToolInfo {
  name: string
  description: string
  parameters: {
    properties?: Record<string, { type?: string; description?: string; default?: unknown }>
    required?: string[]
  }
}

export interface TraceStep {
  step: number
  tool: string
  arguments: unknown
  result: unknown
  error: string | null
  ms: number
}

export interface ToolRunResult {
  answer: string | null
  trace: TraceStep[]
  steps: number
  model: string
  exhausted: boolean
}

/** 示例问题：每个都刻意对应一种"工具调用形态" */
export const PRESET_QUESTIONS: Array<{ label: string; question: string; hint: string }> = [
  {
    label: '单工具',
    question: '帮我算一下 (1234 + 876) * 3 / 7 是多少？',
    hint: 'calculator 一次调用',
  },
  {
    label: '并行两工具',
    question: '北京和上海现在天气怎么样？',
    hint: 'get_weather ×2',
  },
  {
    label: '连续两轮',
    question: '北京现在多少度？如果超过 28 度，把它换算成华氏度告诉我。',
    hint: 'get_weather → calculator',
  },
  {
    label: '知识检索',
    question: '课程笔记里关于 RAG 和流式分别讲了什么？',
    hint: 'search_notes',
  },
  {
    label: '工具会报错',
    question: '火星现在的天气怎么样？',
    hint: '未知城市 → 模型解释',
  },
]

/**
 * 阶段 05：函数调用控制台。
 *
 * 前端在这里的角色很"薄"：只负责把问题送出去、把 trace 摊开给用户看。
 * 所有"该调哪个工具、参数对不对、错了怎么办"都发生在后端（agent.py + tools.py）。
 */
export function useToolRunner(options: { apiBase?: string } = {}) {
  const apiBase =
    options.apiBase ?? ((import.meta.env.VITE_API_BASE as string | undefined) || '/api')

  const tools = ref<ToolInfo[]>([])
  const question = ref('')
  const loading = ref(false)
  const result = ref<ToolRunResult | null>(null)
  const error = ref<string | null>(null)

  /** 把 JSON Schema 摊平成可展示的参数列表 */
  function paramsOf(tool: ToolInfo): ToolParameter[] {
    const props = tool.parameters?.properties ?? {}
    const required = new Set(tool.parameters?.required ?? [])
    return Object.entries(props).map(([name, schema]) => ({
      name,
      type: schema?.type ?? 'any',
      description: schema?.description ?? '',
      required: required.has(name),
      default: schema?.default,
    }))
  }

  async function loadTools() {
    try {
      const resp = await fetch(`${apiBase}/tools`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      tools.value = (await resp.json()) as ToolInfo[]
    } catch (e) {
      error.value = `工具清单加载失败：${e instanceof Error ? e.message : String(e)}`
    }
  }

  async function run() {
    const q = question.value.trim()
    if (!q || loading.value) return
    loading.value = true
    error.value = null
    result.value = null
    try {
      const resp = await fetch(`${apiBase}/tools/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      })
      if (!resp.ok) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 240)}`)
      }
      result.value = (await resp.json()) as ToolRunResult
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function reset() {
    question.value = ''
    result.value = null
    error.value = null
  }

  return { tools, question, loading, result, error, paramsOf, loadTools, run, reset }
}
