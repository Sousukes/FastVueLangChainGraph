import { ref } from 'vue'

export interface MCPToolInfo {
  name: string
  description: string
  inputSchema: {
    properties?: Record<string, { type?: string; description?: string; enum?: unknown[] }>
    required?: string[]
  }
}

export interface MCPResourceInfo {
  uri: string
  name: string
  description: string
  mimeType: string | null
}

export interface MCPPromptInfo {
  name: string
  description: string
  arguments: Array<{ name: string; description?: string; required?: boolean }>
}

export interface MCPServerInfo {
  name: string
  protocolVersion: string
  serverInfo: Record<string, unknown>
  capabilities: Record<string, unknown>
  tools: MCPToolInfo[]
  resources: MCPResourceInfo[]
  prompts: MCPPromptInfo[]
}

export interface MCPLogEntry {
  server: string
  dir: '→' | '←' | string
  payload: Record<string, any>
}

export interface MCPTraceStep {
  step: number
  tool: string
  arguments: unknown
  result: unknown
  error: string | null
  ms: number
}

export interface MCPRunResult {
  answer: string | null
  trace: MCPTraceStep[]
  steps: number
  model: string
  exhausted: boolean
  protocol: MCPLogEntry[]
}

export const PRESET_QUESTIONS: Array<{ label: string; question: string; hint: string }> = [
  {
    label: '检索笔记',
    question: '课程笔记里关于 MCP 和 RAG 分别讲了什么？',
    hint: 'search_notes ×2',
  },
  {
    label: '多工具',
    question: '帮我算 (1234+876)*3/7，再告诉我深圳现在多少度。',
    hint: 'calculator + get_weather',
  },
  {
    label: '工具报错',
    // 用「广州」而不是「火星」：火星是真实城市之外的存在，模型会直接拒绝调用工具，
    // 演示不出 isError；广州是真实城市、只是不在我们的模拟数据里，
    // 模型一定会调用 get_weather，然后收到 isError 并改口解释。
    question: '广州现在多少度？',
    hint: 'isError → 模型解释',
  },
  {
    label: '时间与算数',
    question: '现在几点了？顺便帮我算 25 的平方根乘以 4 是多少。',
    hint: 'get_current_time + calculator',
  },
]

/**
 * 阶段 06：MCP 控制台。
 *
 * 与阶段 05 的关键差别：工具不再来自本进程的注册表，而是来自一个**独立子进程**
 * （通过 JSON-RPC 调用）。所以这里多了一块 `protocol`——把报文摊开，
 * 让"工具是外挂的"这件事变得可见。
 */
export function useMcp(options: { apiBase?: string } = {}) {
  const apiBase =
    options.apiBase ?? ((import.meta.env.VITE_API_BASE as string | undefined) || '/api')

  const catalog = ref<MCPServerInfo[]>([])
  const question = ref('')
  const loading = ref(false)
  const result = ref<MCPRunResult | null>(null)
  const error = ref<string | null>(null)

  // 左栏的"资源 / 提示模板"预览
  const resourcePreview = ref<{ uri: string; text: string; mimeType: string | null } | null>(null)
  const promptPreview = ref<{ name: string; text: string } | null>(null)

  async function loadCatalog() {
    try {
      const resp = await fetch(`${apiBase}/mcp/servers`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = (await resp.json()) as { servers: MCPServerInfo[] }
      catalog.value = data.servers ?? []
    } catch (e) {
      error.value = `MCP Server 目录加载失败：${e instanceof Error ? e.message : String(e)}`
    }
  }

  async function run() {
    const q = question.value.trim()
    if (!q || loading.value) return
    loading.value = true
    error.value = null
    result.value = null
    try {
      const resp = await fetch(`${apiBase}/mcp/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q }),
      })
      if (!resp.ok) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 240)}`)
      }
      result.value = (await resp.json()) as MCPRunResult
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** resources/read：资源是"可读取的数据"，读出来给你看 */
  async function readResource(uri: string) {
    promptPreview.value = null
    try {
      const resp = await fetch(`${apiBase}/mcp/resource?uri=${encodeURIComponent(uri)}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = (await resp.json()) as { uri: string; text: string; mimeType: string | null }
      resourcePreview.value = data
    } catch (e) {
      error.value = `读取资源失败：${e instanceof Error ? e.message : String(e)}`
    }
  }

  /** prompts/get：提示模板取回来是 messages，不是一段纯文本 */
  async function getPrompt(name: string) {
    resourcePreview.value = null
    try {
      const resp = await fetch(`${apiBase}/mcp/prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, arguments: {} }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = (await resp.json()) as { messages?: Array<{ content?: { text?: string } }> }
      promptPreview.value = {
        name,
        text: data.messages?.[0]?.content?.text ?? '(空)',
      }
    } catch (e) {
      error.value = `取提示模板失败：${e instanceof Error ? e.message : String(e)}`
    }
  }

  function applyQuestion(q: string) {
    question.value = q
    result.value = null
  }

  function reset() {
    question.value = ''
    result.value = null
    error.value = null
  }

  return {
    catalog,
    question,
    loading,
    result,
    error,
    resourcePreview,
    promptPreview,
    loadCatalog,
    run,
    readResource,
    getPrompt,
    applyQuestion,
    reset,
  }
}
