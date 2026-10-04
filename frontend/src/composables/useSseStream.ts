import { shallowRef } from 'vue'

/**
 * 共享 SSE 读取器 —— 把「手写 SSE 解析」这段收敛到一处。
 *
 * ## 这段代码就是阶段 03 讲的那段
 *
 * 阶段 03（`/chat`）第一次要接流式输出时，只能手写：`fetch` → `getReader()` →
 * 按 `\n\n` 切帧 → `JSON.parse`。当时刻意没抽公共函数，因为**先把原理讲明白**。
 * 后续 10 个阶段（agent / agentic / graph / harness / research / search / team /
 * vision / voice / computer）每一个都要接 SSE，于是同一段解析逻辑被复制了 10 遍。
 *
 * 现在阶段 18B 收尾，把原理沉淀到这里，各阶段只保留**自己的帧语义**（`apply`）。
 *
 * ## 为什么要手写而不用 EventSource
 *
 * `EventSource` 只支持 GET，而我们要 POST 一整段对话历史 / 任务参数过去。
 * 这是阶段 03 的结论，不是本文件的取舍。
 *
 * ## 三个必须保留的细节（各阶段原本分散写了 10 遍，漏一条就出 bug）
 *
 * 1. **`decoder.decode(value, { stream: true })`** —— 必须带 `stream: true`。
 *    否则跨 chunk 的中文等多字节字符会被截断成乱码（阶段 03 踩过的坑）。
 * 2. **`frames.pop()` 留半帧** —— SSE 以空行分帧，最后一段可能是不完整的，
 *    留在 buffer 里等下一块，而不是直接丢弃。
 * 3. **`reader.releaseLock()` 放在 finally** —— 正常结束、异常、abort 都要释放。
 */

/** 一帧 SSE 的最小形状：具体阶段各自再声明字段 */
export interface SseFrame {
  [key: string]: unknown
}

/**
 * 读取 SSE 响应体，逐帧回调。
 *
 * @param body    `fetch` 拿到的 `resp.body`
 * @param onFrame 每收到一个**能成功 JSON.parse 的 data 帧**就调一次。
 *                返回 `true` 表示「到此为止」，读取会立刻结束（如收到 `done` 收尾帧）。
 * @param signal  `AbortSignal`。传入后 `signal.aborted` 为真时**立即停止读取**
 *                并释放 reader —— 这就是「停止生成」的底层。
 * @returns 正常读完 / 被 abort / onFrame 要求停止，都会 resolve（**不 reject**）；
 *          真正的网络/解析错误仍由 fetch 侧抛出。
 */
export async function readSseStream<F extends SseFrame = SseFrame>(
  body: ReadableStream<Uint8Array>,
  onFrame: (payload: F) => void | boolean,
  signal?: AbortSignal,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      // 中断检查放在每次取块之前：abort 后不再消费 body，服务端也能尽快收到断开
      if (signal?.aborted) return

      const { value, done } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? '' // 留半帧，等下一块

      for (const frame of frames) {
        // 心跳等注释行没有 data: 前缀，跳过
        const line = frame.split('\n').find((l) => l.startsWith('data:'))
        if (!line) continue
        let payload: F
        try {
          payload = JSON.parse(line.slice(5).trim()) as F
        } catch {
          continue // 非 JSON 行（心跳注释等）忽略，不中断整条流
        }
        if (onFrame(payload) === true) return // onFrame 要求收尾
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/**
 * abort 生命周期：让每个 composable 都能「发请求 → 中途可停 → 清理」而不必各写一遍。
 *
 * 用法（以阶段 10 为例）：
 * ```ts
 * const { signal, begin, end, stop, aborted } = useSseAbort()
 *
 * async function run() {
 *   begin()                                  // 新一轮：复位 aborted
 *   try {
 *     const resp = await fetch(url, { ..., signal })
 *     if (!resp.ok || !resp.body) throw new Error(...)
 *     await readSseStream<AgentFrame>(resp.body, apply, signal)
 *   } catch (e) {
 *     // 主动停止不是错误：保留已生成的部分
 *     if (!aborted.value) error.value = (e as Error).message
 *   } finally {
 *     end()                                  // 无论成败都复位 controller
 *   }
 * }
 * ```
 */
export function useSseAbort() {
  const aborted = shallowRef(false)
  let controller: AbortController | null = null

  /** 取当前轮的 signal；同时复位 `aborted` 并新建 controller */
  function begin(): AbortSignal {
    controller = new AbortController()
    aborted.value = false
    return controller.signal
  }

  /** 主动中断本轮（组件卸载时也该调） */
  function stop(): void {
    controller?.abort()
  }

  /** 本轮结束，无论成功/失败都调用，释放 controller 引用 */
  function end(): void {
    controller = null
  }

  // begin() 前拿不到 controller，故 signal 惰性代理到当前 controller
  return {
    aborted,
    begin,
    end,
    stop,
    get signal(): AbortSignal | undefined {
      return controller?.signal
    },
  }
}

/**
 * 抽帧后判断「HTTP 是否失败」—— 11 处都写着一模一样的 3 行。
 *
 * 非 2xx 时读出前 200 字响应体一并抛出，省掉「报错信息里啥都没有」。
 */
export async function assertOk(resp: Response): Promise<void> {
  if (resp.ok && resp.body) return
  const detail = await resp.text().catch(() => '')
  throw new Error(`HTTP ${resp.status} · ${detail.slice(0, 200)}`)
}
