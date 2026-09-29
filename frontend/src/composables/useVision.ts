import { computed, ref } from 'vue'

/**
 * 阶段 16 · 多模态·图像（把"看图"变成一次可控的输入编码）。
 *
 * 与阶段 14/15 的 composable 不同，这里的状态多了一层**图片**：
 *   1. 选择/粘贴/拖入图片 → 读成 data URL（浏览器 FileReader，不经过后端）；
 *   2. 点"理解" → POST /api/vision/stream，读 SSE；
 *   3. 按 mode 还原两种结果：`qa` 是一段流式解读，`extract` 是一张字段表。
 *
 * 帧协议与后端 vision.py 一一对应：start / answer_delta / structured / finish / error。
 * `start.image` 是服务端**嗅探魔数**得到的元数据（格式/尺寸/字节数/声明是否不符），
 * 前端把它当"体检单"展示——这是本阶段"确定性管线先于模型"的可见证据。
 */

export type VisionMode = 'qa' | 'extract'
export type VisionDetail = 'auto' | 'low' | 'high'

/** 服务端解析出的图像元数据 */
export interface VisionImageMeta {
  mime: string
  format: string
  bytes: number
  width: number | null
  height: number | null
  declared: string | null
  mismatch: boolean
}

/** extract 模式的一条字段 */
export interface VisionField {
  label: string
  value: string
}

/** SSE 一帧的载荷，与 backend/main.py 的 vision_stream 一一对应 */
type VisionFrame = {
  type?: string
  // start
  mode?: string
  model?: string
  image?: VisionImageMeta
  // answer_delta
  text?: string
  // structured
  summary?: string | null
  fields?: VisionField[]
  raw?: string
  // finish
  answer?: string | null
  extracted?: boolean
  times?: Record<string, number>
  totalMs?: number
  llmMs?: number
  // error
  message?: string
}

export interface UseVisionOptions {
  apiBase?: string
}

/** 单张图上限 8 MB，与后端 vision._MAX_BYTES 对齐，避免白跑一趟网络 */
const MAX_BYTES = 8 * 1024 * 1024

export function useVision(options: UseVisionOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  // 输入
  const image = ref('') // data URL
  const fileName = ref('')
  const question = ref('')
  const mode = ref<VisionMode>('qa')
  const detail = ref<VisionDetail>('auto')
  const schemaHint = ref('')

  // 状态
  const running = ref(false)
  const error = ref<string | null>(null)

  // 结果
  const imageMeta = ref<VisionImageMeta | null>(null)
  const answer = ref('')
  const summary = ref<string | null>(null)
  const fields = ref<VisionField[]>([])
  const extracted = ref(false)
  const model = ref('')
  const times = ref<Record<string, number>>({})
  const totalMs = ref(0)
  const llmMs = ref(0)

  const hasImage = computed(() => !!image.value)
  const canRun = computed(
    () => hasImage.value && !running.value && (mode.value === 'extract' || !!question.value.trim()),
  )

  /** 把 File 读成 data URL（本地完成，不上传） */
  function loadFile(file: File) {
    if (!file.type.startsWith('image/')) {
      error.value = `不是图片文件：${file.name || file.type || '未知类型'}`
      return
    }
    if (file.size > MAX_BYTES) {
      error.value = `图片过大：${(file.size / 1024 / 1024).toFixed(1)} MB，上限 ${MAX_BYTES / 1024 / 1024} MB`
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      image.value = String(reader.result || '')
      fileName.value = file.name
      error.value = null
      // 换图后清掉上一轮结果，避免"旧答案配新图"
      clearResult()
    }
    reader.onerror = () => {
      error.value = '读取图片失败，请重试'
    }
    reader.readAsDataURL(file)
  }

  function clearImage() {
    image.value = ''
    fileName.value = ''
    clearResult()
    error.value = null
  }

  function clearResult() {
    imageMeta.value = null
    answer.value = ''
    summary.value = null
    fields.value = []
    extracted.value = false
    times.value = {}
    totalMs.value = 0
    llmMs.value = 0
  }

  function reset() {
    clearImage()
    model.value = ''
  }

  async function run() {
    if (!canRun.value) return
    running.value = true
    clearResult()
    error.value = null

    try {
      const resp = await fetch(`${apiBase}/vision/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image: image.value,
          question: question.value,
          mode: mode.value,
          schemaHint: schemaHint.value || null,
          detail: detail.value,
        }),
      })
      if (!resp.ok || !resp.body) {
        const detailText = await resp.text().catch(() => '')
        throw new Error(`HTTP ${resp.status} · ${detailText.slice(0, 200)}`)
      }
      await readStream(resp.body)
    } catch (e) {
      error.value = `图像理解失败：${e instanceof Error ? e.message : String(e)}`
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
          let payload: VisionFrame
          try {
            payload = JSON.parse(line.slice(5).trim()) as VisionFrame
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

  function apply(p: VisionFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? model.value
        imageMeta.value = p.image ?? null
        break
      case 'answer_delta':
        answer.value += p.text ?? ''
        break
      case 'structured':
        summary.value = p.summary ?? null
        fields.value = p.fields ?? []
        extracted.value = true
        break
      case 'finish':
        answer.value = p.answer ?? answer.value
        imageMeta.value = p.image ?? imageMeta.value
        summary.value = p.summary ?? summary.value
        fields.value = p.fields ?? fields.value
        extracted.value = !!p.extracted
        times.value = p.times ?? {}
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        break
      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  /** 人读的字节数 */
  function humanBytes(n: number): string {
    if (n < 1024) return `${n} B`
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
    return `${(n / 1024 / 1024).toFixed(2)} MB`
  }

  return {
    // 输入
    image,
    fileName,
    question,
    mode,
    detail,
    schemaHint,
    // 状态
    running,
    error,
    canRun,
    hasImage,
    // 结果
    imageMeta,
    answer,
    summary,
    fields,
    extracted,
    model,
    times,
    totalMs,
    llmMs,
    // 动作
    loadFile,
    clearImage,
    run,
    reset,
    humanBytes,
  }
}
