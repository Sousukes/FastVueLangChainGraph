import { ref, shallowRef, readonly, computed } from 'vue'

export interface ChatTurn {
  id: string
  role: 'system' | 'user' | 'assistant'
  content: string
  /** 本条是否正在流式生成中（驱动签名光标闪烁） */
  streaming?: boolean
}

export interface UseChatStreamOptions {
  /** 后端地址，默认走 Vite 代理 /api -> http://localhost:8000 */
  apiBase?: string
  system?: string
  model?: string
  temperature?: number
}

/** SSE 一帧里可能出现的三种载荷，与 backend/main.py 的 sse() 一一对应 */
type StreamFrame = { delta?: string; done?: boolean; model?: string; error?: string }

const newId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`

/**
 * 阶段 03：多轮对话 + SSE 流式。
 *
 * 两个职责分离得很清楚：
 * - **多轮**：messages 数组就是全部真相。每轮把完整历史 POST 给后端（HTTP 无状态，
 *   服务端不替你记；阶段 07 才会引入 SQLite 做服务端持久化）。
 * - **流式**：用原生 fetch + ReadableStream 手动切分 SSE 帧。没用 EventSource，
 *   因为 EventSource 只支持 GET，而我们要 POST 一整段对话历史。
 */
export function useChatStream(options: UseChatStreamOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  const system = ref(options.system ?? '你是一个简洁、严谨的 AI 研究助手。')
  const messages = ref<ChatTurn[]>([])
  const loading = shallowRef(false)
  const error = shallowRef<string | null>(null)

  let controller: AbortController | null = null

  /** 发给后端的完整上下文：system + 历史 + 本轮（本轮由 send 追加） */
  const context = computed(() => [
    ...(system.value.trim() ? [{ role: 'system' as const, content: system.value.trim() }] : []),
    ...messages.value.map((m) => ({ role: m.role, content: m.content })),
  ])

  /** 参与计费/上下文的轮数（不含 system） */
  const turnCount = computed(() => messages.value.length)

  async function send(text: string) {
    const content = text.trim()
    if (!content || loading.value) return

    error.value = null
    messages.value.push({ id: newId(), role: 'user', content })
    const assistant: ChatTurn = {
      id: newId(),
      role: 'assistant',
      content: '',
      streaming: true,
    }
    messages.value.push(assistant)
    loading.value = true

    controller = new AbortController()
    try {
      const resp = await fetch(`${apiBase}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: context.value,
          model: options.model,
          temperature: options.temperature ?? 0.7,
        }),
        signal: controller.signal,
      })

      if (!resp.ok || !resp.body) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 200)}`)
      }

      await readSse(resp.body, assistant)
    } catch (e) {
      const err = e as Error
      // 用户主动停止不是错误：保留已生成的部分
      if (err.name !== 'AbortError') {
        error.value = err.message || String(err)
      }
    } finally {
      assistant.streaming = false
      loading.value = false
      controller = null
    }
  }

  /** 逐块读取响应体，按 `\n\n` 切帧，把 delta 累加到 assistant 上 */
  async function readSse(body: ReadableStream<Uint8Array>, target: ChatTurn) {
    const reader = body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        // stream: true —— 让跨 chunk 的中文等多字节字符不被截断成乱码
        buffer += decoder.decode(value, { stream: true })

        // SSE 以空行分帧。最后一段可能是不完整的半帧，留在 buffer 里等下一块
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''

        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          let payload: StreamFrame
          try {
            payload = JSON.parse(line.slice(5).trim()) as StreamFrame
          } catch {
            continue // 心跳注释等非 JSON 行，忽略
          }
          if (typeof payload.delta === 'string') {
            target.content += payload.delta
          } else if (payload.error) {
            error.value = payload.error
          } else if (payload.done) {
            return // 收尾帧，正常结束
          }
        }
      }
    } finally {
      reader.releaseLock()
    }
  }

  /** 立即中断：abort 会让 fetch 抛出 AbortError，已生成内容保留 */
  function stop() {
    controller?.abort()
  }

  function reset() {
    stop()
    messages.value = []
    error.value = null
  }

  return {
    system,
    // 对外只读：改对话请走 send / stop / reset，避免组件直接 mutate 历史
    messages: readonly(messages),
    loading: readonly(loading),
    error: readonly(error),
    turnCount: readonly(turnCount),
    send,
    stop,
    reset,
  }
}
