import { ref, shallowRef, readonly } from 'vue'

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface UseDeepSeekOptions {
  baseUrl?: string
  model?: string
}

export interface SendPayload {
  system?: string
  user: string
}

/**
 * 阶段 01：前端直连 DeepSeek，示范最原始的 REST 调用（讲原理）。
 * 注意：密钥仅在浏览器内存中暂存，不写入代码；阶段 03 将改为后端代理。
 */
export function useDeepSeek(options: UseDeepSeekOptions = {}) {
  const baseUrl = options.baseUrl ?? 'https://api.deepseek.com'
  const model = options.model ?? 'deepseek-flash'

  const apiKey = ref('')
  const response = ref('')
  const loading = shallowRef(false)
  const error = shallowRef<string | null>(null)

  async function send(payload: SendPayload) {
    if (!apiKey.value) {
      error.value = '请先填写 DeepSeek API Key'
      return
    }
    loading.value = true
    error.value = null
    response.value = ''
    try {
      const messages: ChatMessage[] = []
      if (payload.system?.trim()) {
        messages.push({ role: 'system', content: payload.system.trim() })
      }
      messages.push({ role: 'user', content: payload.user })

      const resp = await fetch(`${baseUrl}/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${apiKey.value}`,
        },
        body: JSON.stringify({ model, messages, stream: false }),
      })

      if (!resp.ok) {
        const detail = await resp.text()
        throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 200)}`)
      }
      const data = await resp.json()
      response.value = data?.choices?.[0]?.message?.content ?? ''
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  return {
    apiKey,
    response: readonly(response),
    loading: readonly(loading),
    error: readonly(error),
    send,
  }
}
