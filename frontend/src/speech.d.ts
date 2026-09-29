/**
 * 阶段 17 · 语音识别（ASR）的最小类型补丁。
 *
 * 为什么需要这个文件（实测过，不是猜的）：TypeScript 5.9.3 的 `lib.dom.d.ts` 里
 *
 *   ✅ 有  SpeechSynthesis / SpeechSynthesisUtterance / SpeechSynthesisVoice   → TTS 侧不用补
 *   ✅ 有  SpeechRecognitionResult / SpeechRecognitionResultList /
 *          SpeechRecognitionAlternative                                        → 结果类型不用补
 *   ❌ 没有 SpeechRecognition 本体
 *   ❌ 没有 SpeechRecognitionEvent / SpeechRecognitionErrorEvent
 *   ❌ 没有 webkitSpeechRecognition（Chrome/Edge 的实际挂载名）
 *
 * 于是 `new SpeechRecognition()` 会直接 `Cannot find name`。这不是我们写错了，
 * 是标准库的空白——标准只把 Web Speech API 的「结果类型」收了进去，
 * 没把识别器本体收进去。**如实补上缺的那几块即可，不要重复声明已有的。**
 *
 * ⚠️ 刻意只声明真正用到的成员：接口开得越宽，越容易把拼写错误吞掉。
 *    比如 `onresult` 的签名写成 `unknown` 返回值——我们只关心「有没有回调」，
 *    不关心回调返回什么，就没必要假装知道。
 */

interface SpeechRecognitionEvent extends Event {
  /** 本次事件里第一个「新结果」的下标（前面的都是上一轮已给的） */
  readonly resultIndex: number
  readonly results: SpeechRecognitionResultList
}

interface SpeechRecognitionErrorEvent extends Event {
  /** 机器可读的错误码：`no-speech` / `not-allowed` / `network` / `aborted` … */
  readonly error: string
  readonly message: string
}

interface SpeechRecognition extends EventTarget {
  lang: string
  continuous: boolean
  /** true 时 `onresult` 会在用户还没说完时先给「临时结果」（用于边说边显示） */
  interimResults: boolean
  maxAlternatives: number
  start(): void
  stop(): void
  abort(): void
  onstart: ((this: SpeechRecognition, ev: Event) => unknown) | null
  onend: ((this: SpeechRecognition, ev: Event) => unknown) | null
  onresult: ((this: SpeechRecognition, ev: SpeechRecognitionEvent) => unknown) | null
  onerror: ((this: SpeechRecognition, ev: SpeechRecognitionErrorEvent) => unknown) | null
}

declare var SpeechRecognition: {
  prototype: SpeechRecognition
  new (): SpeechRecognition
}

/** Chrome / Edge 至今仍以 `webkit` 前缀挂载，实测两个都要探。 */
declare var webkitSpeechRecognition: {
  prototype: SpeechRecognition
  new (): SpeechRecognition
}
