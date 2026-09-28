import { computed, ref } from 'vue'

/** 节点类型 → 颜色。和 docs/design-system.md 的调色板同源：
 *  琥珀（Signal）留给"当前焦点"，青绿（Data）留给"数据/成功"，
 *  其余类型走冷色系——这样一眼就能看出图上哪一块是"这次检索命中的"。 */
export const TYPE_COLOR: Record<string, string> = {
  阶段: '#FFB454', // Signal
  技术: '#5BC8B0', // Data
  概念: '#8B95A7', // Muted
  参数: '#C9A2FF',
  文档: '#6FA8FF',
  工具: '#FF8FA3',
}

export function typeColor(t: string) {
  return TYPE_COLOR[t] ?? '#8B95A7'
}

export interface GraphStats {
  entities: number
  edges: number
  extractedChunks: number
  totalChunks: number
  coverage: number
  types: Array<{ type: string; count: number }>
  relations: Array<{ relation: string; count: number }>
  entityTypes: string[]
  relationHints: string[]
  lastBuiltAt: string | null
}

export interface GraphNode {
  name: string
  type: string
  mentions: number
  hop: number
  isSeed: boolean
  degree: number
}

export interface GraphEdge {
  id: number
  head: string
  relation: string
  tail: string
  sourceTitle: string
  sourceIndex: number
  hop: number
}

export interface GraphChunk {
  id: string
  title: string
  index: number
  text: string
  edgeCount: number
}

export interface GraphSearchResult {
  query: string
  seeds: string[]
  nodes: GraphNode[]
  edges: GraphEdge[]
  chunks: GraphChunk[]
  hops: number
  timings: Record<string, number>
}

export interface GraphAskResult extends Omit<GraphSearchResult, 'query'> {
  question: string
  answer: string
  prompt: string
  model: string
}

export interface GraphDocument {
  title: string
  chunks: number
  extracted: number
}

export interface GraphEntityItem {
  name: string
  type: string
  mentions: number
  degree: number
}

/** 构建进度（来自 SSE 的 progress 帧） */
export interface BuildProgress {
  done: number
  total: number
  chunk: string
  entities: number
  edges: number
}

/**
 * 预设问题。分三类，因为**这三类的结果差别本身就是本阶段最该看的东西**：
 *
 * - `multi-hop`：答案**不在任何一段文本里**，必须靠图上的关系链推出来。
 *   这是向量检索永远做不到的事——阶段 07/08 对这类问题只能给出"最像的几段"。
 * - `direct`：一跳就够，用来看"图能做什么"的下界。
 * - `miss`：图上**锚不到实体**，于是整条链路空转。
 *   它演示的是 GraphRAG 的真实边界：图的覆盖率只有百分之几时，
 *   "图里没有"和"语料里没有"是**两件必须分清的事**。
 */
export const GRAPH_PRESETS: Array<{
  label: string
  query: string
  kind: 'multi-hop' | 'direct' | 'miss'
  hint: string
}> = [
  {
    label: '跨文档两跳',
    query: '设计计划里定义了哪些颜色？',
    kind: 'multi-hop',
    hint: 'DESIGN → design-system',
  },
  {
    label: '两跳关系',
    query: '重排是哪个阶段引入的？',
    kind: 'multi-hop',
    hint: '重排 → RAG 进阶 → 主线',
  },
  {
    label: '一跳直连',
    query: 'MCP 协议开发用了什么协议？',
    kind: 'direct',
    hint: 'MCP 协议开发 → MCP 协议',
  },
  {
    label: '锚定失败',
    query: '阶段 08 依赖哪些阶段？',
    kind: 'miss',
    hint: '图上没有「阶段 08」这个节点',
  },
]

export function nodeKey(n: { name: string }) {
  return n.name
}

export function chunkKey(c: { title: string; index: number }) {
  return `${c.title}#${c.index}`
}

/** SSE 一帧的载荷，与 backend/main.py 的 graph_build_stream 一一对应 */
type BuildFrame = {
  phase?: 'start' | 'progress' | 'done'
  total?: number
  done?: number
  chunk?: string
  entities?: number
  edges?: number
  processed?: number
  seconds?: number
  chunks?: number
  error?: string
}

/**
 * 阶段 09：GraphRAG 知识图谱。
 *
 * 这个 composable 和前面几个有一个**结构性的不同**：它同时管着"读"和"写"两条链路。
 *
 * - 读（search / ask）：几十毫秒，普通 POST 就够。
 * - 写（build）：每个块一次 LLM 调用，动辄几分钟 → 必须走 SSE 把进度推出来。
 *
 * 阶段 03 用 SSE 推的是"token"，这里推的是"块"——**同一个协议，两种用途**。
 */
export function useGraph(options: { apiBase?: string } = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const stats = ref<GraphStats | null>(null)
  const documents = ref<GraphDocument[]>([])
  const overview = ref<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null)
  const entities = ref<GraphEntityItem[]>([])
  const entitiesTotal = ref(0)

  /** 构建范围：勾选哪些文档。空数组 = 全语料（不推荐，会跑到天亮） */
  const scope = ref<string[]>(['DESIGN', 'design-system'])
  const limit = ref(12)
  const workers = ref(6)
  const progress = ref<BuildProgress | null>(null)
  const building = ref(false)
  const lastBuild = ref<{ processed: number; entities: number; edges: number; seconds: number } | null>(
    null,
  )

  const query = ref(GRAPH_PRESETS[0].query)
  const hops = ref(2)
  const topK = ref(3)
  const result = ref<GraphSearchResult | null>(null)
  const askResult = ref<GraphAskResult | null>(null)

  /**
   * 图上看什么：检索命中了就画子图，否则画截断后的全图。
   *
   * ⚠️ 这个状态**必须放在 composable 里**，而且每次检索完要自动切回 `result`。
   * 浏览器实测踩到的坑：先点了「全图」再检索，图**还是那张全图**——
   * 用户点了「检索」，看到图没变，会直接以为功能坏了。
   * 结果出来了就该显示结果，这是默认预期，不该靠用户再点一次。
   */
  const viewMode = ref<'result' | 'overview'>('result')

  const busy = ref<'' | 'status' | 'search' | 'ask' | 'overview'>('')
  const error = ref<string | null>(null)

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(`${apiBase}${path}`, init)
    if (!resp.ok) {
      const detail = await resp.text().catch(() => '')
      throw new Error(`HTTP ${resp.status}${detail ? ' · ' + detail.slice(0, 240) : ''}`)
    }
    return (await resp.json()) as T
  }

  function fail(prefix: string, e: unknown) {
    error.value = `${prefix}：${e instanceof Error ? e.message : String(e)}`
  }

  async function loadStats() {
    try {
      stats.value = await call<GraphStats>('/graph/stats')
      error.value = null
    } catch (e) {
      fail('图谱状态加载失败', e)
    }
  }

  async function loadDocuments() {
    try {
      documents.value = await call<GraphDocument[]>('/graph/documents')
    } catch (e) {
      fail('文档列表加载失败', e)
    }
  }

  async function loadOverview(nodeLimit = 40) {
    busy.value = 'overview'
    try {
      overview.value = await call<{ nodes: GraphNode[]; edges: GraphEdge[] }>(
        `/graph/overview?limit=${nodeLimit}`,
      )
    } catch (e) {
      fail('全图加载失败', e)
    } finally {
      busy.value = ''
    }
  }

  async function loadEntities(q = '', entityLimit = 300) {
    try {
      const data = await call<{ total: number; items: GraphEntityItem[] }>(
        `/graph/entities?limit=${entityLimit}&q=${encodeURIComponent(q)}`,
      )
      entities.value = data.items
      entitiesTotal.value = data.total
    } catch (e) {
      fail('实体清单加载失败', e)
    }
  }

  /** 首次进入页面要拉的全部东西 */
  async function loadAll() {
    busy.value = 'status'
    await Promise.all([loadStats(), loadDocuments(), loadOverview(), loadEntities()])
    busy.value = ''
  }

  /**
   * 构建图谱（SSE 流式）。
   *
   * 这里刻意**没有**用 EventSource：它只支持 GET，而构建要 POST 一个请求体。
   * 所以和阶段 03 一样，用 fetch + ReadableStream 手动切帧。
   */
  async function runBuild() {
    if (building.value) return
    building.value = true
    error.value = null
    progress.value = null
    lastBuild.value = null

    const body = {
      limit: limit.value,
      workers: workers.value,
      titles: scope.value.length ? scope.value : null,
    }

    try {
      const resp = await fetch(`${apiBase}/graph/build/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!resp.ok || !resp.body) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 200)}`)
      }
      await readBuildStream(resp.body)
    } catch (e) {
      fail('图谱构建失败', e)
    } finally {
      building.value = false
      // 构建完（或失败）都要刷新一次：实体、边、覆盖率全变了
      await loadAll()
    }
  }

  async function readBuildStream(body: ReadableStream<Uint8Array>) {
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
          let payload: BuildFrame
          try {
            payload = JSON.parse(line.slice(5).trim()) as BuildFrame
          } catch {
            continue
          }
          if (payload.error) {
            error.value = payload.error
            return
          }
          if (payload.phase === 'start') {
            progress.value = { done: 0, total: payload.total ?? 0, chunk: '', entities: 0, edges: 0 }
          } else if (payload.phase === 'progress') {
            progress.value = {
              done: payload.done ?? 0,
              total: payload.total ?? 0,
              chunk: payload.chunk ?? '',
              entities: payload.entities ?? 0,
              edges: payload.edges ?? 0,
            }
          } else if (payload.phase === 'done') {
            lastBuild.value = {
              processed: payload.processed ?? 0,
              entities: payload.entities ?? 0,
              edges: payload.edges ?? 0,
              seconds: payload.seconds ?? 0,
            }
          }
        }
      }
    } finally {
      reader.releaseLock()
    }
  }

  async function runSearch() {
    const q = query.value.trim()
    if (!q) return
    busy.value = 'search'
    error.value = null
    askResult.value = null
    try {
      result.value = await call<GraphSearchResult>('/graph/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, hops: hops.value, top_k: topK.value }),
      })
      viewMode.value = 'result' // 检索完就切回子图，别让用户对着全图发愣
    } catch (e) {
      fail('图谱检索失败', e)
    } finally {
      busy.value = ''
    }
  }

  async function runAsk() {
    const q = query.value.trim()
    if (!q) return
    busy.value = 'ask'
    error.value = null
    try {
      askResult.value = await call<GraphAskResult>('/graph/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, hops: hops.value, top_k: topK.value }),
      })
      // 问答自带完整检索结果，顺手把它也显示在图里——这样"模型依据了什么"是可见的
      result.value = { ...askResult.value, query: askResult.value.question }
      viewMode.value = 'result'
    } catch (e) {
      fail('图谱问答失败', e)
    } finally {
      busy.value = ''
    }
  }

  async function runReset() {
    try {
      await call('/graph/reset', { method: 'POST' })
      result.value = null
      askResult.value = null
      progress.value = null
      lastBuild.value = null
      await loadAll()
    } catch (e) {
      fail('清空图谱失败', e)
    }
  }

  function applyPreset(q: string) {
    query.value = q
    result.value = null
    askResult.value = null
  }

  /** 构建预计耗时：实测单块约 19s，并发后按 workers 摊——**给用户一个数，别让他干等** */
  const estimateSeconds = computed(() => {
    const remaining = documents.value
      .filter((d) => !scope.value.length || scope.value.includes(d.title))
      .reduce((sum, d) => sum + (d.chunks - d.extracted), 0)
    const todo = Math.min(remaining, limit.value)
    return Math.round((todo / Math.max(1, workers.value)) * 19)
  })

  /** 当前检索命中的边（hop 越小越相关）——图上要按这个决定高亮顺序 */
  const hitEdges = computed(() => result.value?.edges ?? [])

  /** 检索涉及的节点名集合，用来在全图模式下也能标出来 */
  const hitNames = computed(() => new Set((result.value?.nodes ?? []).map((n) => n.name)))

  return {
    stats,
    documents,
    overview,
    entities,
    entitiesTotal,
    scope,
    limit,
    workers,
    progress,
    building,
    lastBuild,
    query,
    hops,
    topK,
    viewMode,
    result,
    askResult,
    busy,
    error,
    estimateSeconds,
    hitEdges,
    hitNames,
    loadStats,
    loadDocuments,
    loadOverview,
    loadEntities,
    loadAll,
    runBuild,
    runSearch,
    runAsk,
    runReset,
    applyPreset,
  }
}
