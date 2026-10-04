import { computed, onBeforeUnmount, ref } from 'vue'
import { assertOk, readSseStream, useSseAbort } from './useSseStream'

/**
 * 阶段 17 · 多模态·语音。
 *
 * 本阶段最该先想清楚的不是代码，而是**边界**：语音能力归谁？
 *
 * 实测结论：DeepSeek **没有**音频接口（`/audio/transcriptions`、`/audio/speech` 都是 404）。
 * 所以这个 composable 把一次语音问答劈成两半：
 *
 *   浏览器（Web Speech API，零依赖零密钥）  后端（只做文本侧，因为只有它值得 LLM）
 *   ────────────────────────────────────  ────────────────────────────────────────
 *   ① SpeechRecognition  语音 → 转写文本   ③ 口语化改写    书面语 → 适读短句（流式）
 *   ② speechSynthesis    朗读稿 → 声音     ④ 文本规范化    符号写法 → 读出来的样子
 *                                          ⑤ 确定性体检    残留 Markdown 数 / 朗读时长
 *
 * 「能用平台能力解决的事就别自己造」——把 ASR/TTS 硬搬到服务端，就得引入一套语音服务与密钥，
 * 收益为零、复杂度暴涨。
 *
 * 帧协议与后端 voice.py 一一对应：start / answer_delta / rewrite / speak / finish / error。
 */

export type VoiceStyle = 'brief' | 'explain' | 'step'

/** 朗读稿规范化的一条对照：`USB-C` → `U S B C` */
export interface VoiceTerm {
  /** ⚠️ 字段名就是 `from`（Python 侧用 `from_` + 别名，响应里仍是 `from`） */
  from: string
  to: string
}

/**
 * 对 `terms` 的**确定性核对**结果。
 *
 * `terms` 是模型自述，不是 diff——模型可能漏报（比如它顺手把阿拉伯数字改成了汉字却没列进来），
 * 也可能多报。服务端用一句 `in` 判断把「自述」降级成「可核对」，前端据此打徽章。
 */
export interface VoiceTermCheck {
  total: number
  verified: number
  suspect: VoiceTerm[]
}

/** SSE 一帧的载荷，与 backend/main.py 的 voice_stream 一一对应 */
type VoiceFrame = {
  type?: string
  // start
  transcript?: string
  style?: string
  model?: string
  maxChars?: number
  // answer_delta
  text?: string
  // rewrite
  chars?: number
  markdownLeft?: number
  ms?: number
  // speak
  speak?: string
  terms?: VoiceTerm[]
  termCheck?: VoiceTermCheck
  plainChars?: number
  estSeconds?: number
  // finish
  answer?: string | null
  times?: Record<string, number>
  totalMs?: number
  llmMs?: number
  // error
  message?: string
}

export interface UseVoiceOptions {
  apiBase?: string
}

/** 与后端 voice._MAX_TRANSCRIPT 对齐，避免白跑一趟网络 */
const MAX_TRANSCRIPT = 2000

/** 探测两个全局：标准名与 webkit 前缀名（见 src/speech.d.ts 的说明） */
function pickRecognitionCtor(): (new () => SpeechRecognition) | null {
  if (typeof window === 'undefined') return null
  if (typeof SpeechRecognition !== 'undefined') return SpeechRecognition
  if (typeof webkitSpeechRecognition !== 'undefined') return webkitSpeechRecognition
  return null
}

export function useVoice(options: UseVoiceOptions = {}) {
  const envBase = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
  const apiBase = options.apiBase ?? (envBase || '/api')

  // ---------- 输入 ----------
  const transcript = ref('')
  const style = ref<VoiceStyle>('brief')
  const maxChars = ref(220)

  // ---------- ASR（浏览器侧） ----------
  const asrSupported = ref(false)
  const listening = ref(false)
  const interim = ref('') // 临时结果：用户还没说完时的"预览"，不进最终文本
  const asrError = ref<string | null>(null)
  let recognition: SpeechRecognition | null = null

  // ---------- TTS（浏览器侧） ----------
  const ttsSupported = ref(false)
  const speaking = ref(false)
  const paused = ref(false)
  const autoSpeak = ref(true) // 规范化一落地就朗读（省掉一次点击）
  const rate = ref(1)
  const voices = ref<SpeechSynthesisVoice[]>([])
  const voiceURI = ref('')

  // ---------- 后端 ----------
  const running = ref(false)
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

  // ---------- 结果 ----------
  const answer = ref('') // 口语化改写稿（流式累积）
  const speak = ref('') // 朗读稿（已规范化）
  const terms = ref<VoiceTerm[]>([])
  const termCheck = ref<VoiceTermCheck | null>(null)
  const rewriteChars = ref(0)
  const markdownLeft = ref(0)
  const plainChars = ref(0)
  const estSeconds = ref(0)
  const model = ref('')
  const times = ref<Record<string, number>>({})
  const totalMs = ref(0)
  const llmMs = ref(0)

  const text = computed(() => transcript.value.trim())
  const canRun = computed(
    () => !!text.value && text.value.length <= MAX_TRANSCRIPT && !running.value,
  )
  const textTooLong = computed(() => text.value.length > MAX_TRANSCRIPT)

  /** 朗读稿体检：无 Markdown 才算"干净稿"（确定性数字，不是模型自述） */
  const cleanSpeak = computed(() => !!speak.value && markdownLeft.value === 0)

  /** 对照表核对结果：自述的改写是否真的发生在文本里 */
  const termsSuspect = computed(() => termCheck.value?.suspect.length ?? 0)

  // ---------- 初始化：探测能力 + 预热语音列表 ----------
  if (typeof window !== 'undefined') {
    asrSupported.value = pickRecognitionCtor() !== null
    ttsSupported.value = 'speechSynthesis' in window
    if (ttsSupported.value) {
      const load = () => {
        voices.value = window.speechSynthesis.getVoices()
        if (!voiceURI.value) voiceURI.value = pickVoice(voices.value)?.voiceURI ?? ''
      }
      load()
      // Chrome 首次 getVoices() 常常是空数组，要等 voiceschanged
      window.speechSynthesis.addEventListener?.('voiceschanged', load)
    }
  }

  /** 优先选中文音色：先把 `lang` 以 zh 开头的挑出来，再往 zh-CN 收窄 */
  function pickVoice(list: SpeechSynthesisVoice[]): SpeechSynthesisVoice | undefined {
    if (!list.length) return undefined
    return (
      list.find((v) => v.lang?.toLowerCase().startsWith('zh-cn')) ??
      list.find((v) => v.lang?.toLowerCase().startsWith('zh')) ??
      list[0]
    )
  }

  // ---------- ASR ----------
  function startListening() {
    const Ctor = pickRecognitionCtor()
    if (!Ctor) {
      asrError.value = '这个浏览器不支持语音识别（Chrome / Edge 可用）。可以直接打字，效果一样。'
      return
    }
    stopSpeaking()
    asrError.value = null
    interim.value = ''

    const rec = new Ctor()
    rec.lang = 'zh-CN'
    rec.continuous = true
    rec.interimResults = true
    rec.maxAlternatives = 1

    rec.onstart = () => {
      listening.value = true
    }
    rec.onresult = (ev) => {
      let live = ''
      for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
        const alt = ev.results[i][0]
        if (ev.results[i].isFinal) {
          // 终态结果：并进正文
          transcript.value = (transcript.value + alt.transcript).slice(0, MAX_TRANSCRIPT)
        } else {
          live += alt.transcript
        }
      }
      interim.value = live
    }
    rec.onerror = (ev) => {
      // `no-speech` 是最常见的"什么都没说"，不该吓唬用户
      asrError.value =
        ev.error === 'no-speech'
          ? '没听到声音，再试一次？'
          : ev.error === 'not-allowed'
            ? '麦克风权限被拒绝：请点地址栏的权限图标允许麦克风'
            : `语音识别出错：${ev.error}`
      listening.value = false
    }
    rec.onend = () => {
      listening.value = false
      interim.value = ''
    }

    recognition = rec
    try {
      rec.start()
    } catch (e) {
      // 连点两次 start() 会抛 InvalidStateError，这里不该炸掉页面
      listening.value = false
      asrError.value = `无法启动识别：${e instanceof Error ? e.message : String(e)}`
    }
  }

  function stopListening() {
    recognition?.stop()
    recognition = null
    listening.value = false
    interim.value = ''
  }

  // ---------- TTS ----------
  function speakOut(source?: string) {
    const body = (source ?? speak.value ?? '').trim()
    if (!('speechSynthesis' in window)) {
      error.value = '这个浏览器不支持语音合成（Chrome / Edge 可用）'
      return
    }
    if (!body) return
    window.speechSynthesis.cancel()
    const u = new SpeechSynthesisUtterance(body)
    u.lang = 'zh-CN'
    u.rate = rate.value
    const v = voices.value.find((x) => x.voiceURI === voiceURI.value) ?? pickVoice(voices.value)
    if (v) u.voice = v
    u.onstart = () => {
      speaking.value = true
      paused.value = false
    }
    u.onend = () => {
      speaking.value = false
      paused.value = false
    }
    u.onerror = () => {
      speaking.value = false
      paused.value = false
    }
    window.speechSynthesis.speak(u)
  }

  function togglePause() {
    if (!('speechSynthesis' in window)) return
    if (paused.value) {
      window.speechSynthesis.resume()
      paused.value = false
    } else {
      window.speechSynthesis.pause()
      paused.value = true
    }
  }

  function stopSpeaking() {
    if (!('speechSynthesis' in window)) return
    window.speechSynthesis.cancel()
    speaking.value = false
    paused.value = false
  }

  // ---------- 主流程 ----------
  function clearResult() {
    answer.value = ''
    speak.value = ''
    terms.value = []
    termCheck.value = null
    rewriteChars.value = 0
    markdownLeft.value = 0
    plainChars.value = 0
    estSeconds.value = 0
    model.value = ''
    times.value = {}
    totalMs.value = 0
    llmMs.value = 0
  }

  function reset() {
    stopListening()
    stopSpeaking()
    transcript.value = ''
    interim.value = ''
    asrError.value = null
    clearResult()
    error.value = null
  }

  async function run() {
    if (!canRun.value) return
    stopListening()
    stopSpeaking()
    running.value = true
    clearResult()
    error.value = null

    const signal = beginSse()

    try {
      const resp = await fetch(`${apiBase}/voice/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          transcript: text.value,
          style: style.value,
          maxChars: maxChars.value,
        }),
        signal,
      })
      await assertOk(resp)
      await readSseStream<VoiceFrame>(resp.body!, apply, signal)
    } catch (e) {
      // 主动停止不是错误：保留已生成的结果
      if (aborted.value) return
      error.value = `语音管线失败：${e instanceof Error ? e.message : String(e)}`
    } finally {
      endSse()
      running.value = false
    }
  }


  function apply(p: VoiceFrame) {
    switch (p.type) {
      case 'start':
        model.value = p.model ?? model.value
        break
      case 'answer_delta':
        answer.value += p.text ?? ''
        break
      case 'rewrite':
        rewriteChars.value = p.chars ?? rewriteChars.value
        markdownLeft.value = p.markdownLeft ?? 0
        break
      case 'speak':
        speak.value = p.speak ?? ''
        terms.value = p.terms ?? []
        termCheck.value = p.termCheck ?? null
        plainChars.value = p.plainChars ?? 0
        estSeconds.value = p.estSeconds ?? 0
        // 规范化一到手就朗读：这是"流式"在本阶段的真实收益
        if (autoSpeak.value) speakOut(speak.value)
        break
      case 'finish':
        answer.value = p.answer ?? answer.value
        speak.value = p.speak ?? speak.value
        terms.value = p.terms ?? terms.value
        termCheck.value = p.termCheck ?? termCheck.value
        markdownLeft.value = p.markdownLeft ?? markdownLeft.value
        plainChars.value = p.plainChars ?? plainChars.value
        estSeconds.value = p.estSeconds ?? estSeconds.value
        times.value = p.times ?? {}
        totalMs.value = p.totalMs ?? 0
        llmMs.value = p.llmMs ?? 0
        break
      case 'error':
        error.value = p.message ?? '未知错误'
        break
    }
  }

  /** 人读的时长：24.4 秒 → `约 24 秒`；超过 1 分钟就用分秒 */
  function humanSeconds(s: number): string {
    if (!s) return '—'
    if (s < 60) return `约 ${Math.round(s)} 秒`
    return `约 ${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`
  }

  onBeforeUnmount(() => {
    stopListening()
    stopSpeaking()
  })

  return {
    // 输入
    transcript,
    style,
    maxChars,
    // ASR
    asrSupported,
    listening,
    interim,
    asrError,
    startListening,
    stopListening,
    // TTS
    ttsSupported,
    speaking,
    paused,
    autoSpeak,
    rate,
    voices,
    voiceURI,
    speakOut,
    togglePause,
    stopSpeaking,
    // 后端
    running,
    error,
    canRun,
    textTooLong,
    // 结果
    answer,
    speak,
    terms,
    termCheck,
    termsSuspect,
    rewriteChars,
    markdownLeft,
    plainChars,
    estSeconds,
    cleanSpeak,
    model,
    times,
    totalMs,
    llmMs,
    // 动作
    run,
    stop,
    reset,
    humanSeconds,
  }
}
