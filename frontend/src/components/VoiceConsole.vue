<script setup lang="ts">
import { computed } from 'vue'
import { useVoice, type VoiceStyle } from '../composables/useVoice'

defineProps<{ stage: number; title: string }>()

const s = useVoice()

/** 与后端 voice._MAX_TRANSCRIPT 对齐，只用于计数器展示 */
const MAX_TRANSCRIPT = 2000

const STYLES: { value: VoiceStyle; label: string; hint: string }[] = [
  { value: 'brief', label: '简洁', hint: '只讲最关键的结论' },
  { value: 'explain', label: '展开', hint: '先结论，再补一两个理由' },
  { value: 'step', label: '分步', hint: '按「第一步…第二步…」讲清做法' },
]

const RATES: { value: number; label: string }[] = [
  { value: 0.8, label: '0.8× 慢' },
  { value: 1, label: '1.0× 正常' },
  { value: 1.2, label: '1.2× 快' },
  { value: 1.5, label: '1.5× 很快' },
]

/** 三个示例：口语提问，且**故意**带上日期 / 金额 / 型号，好让规范化有活干 */
const SAMPLES: { label: string; text: string }[] = [
  {
    label: '报销流程',
    text: '帮我讲一下差旅报销的流程，我九月二十一号交的单子，金额两千一百五十八块五，走的是 BX-2026-0917 这个单号。',
  },
  {
    label: '选型建议',
    text: '团队要用 RAG 做一个内部知识库，向量库该选哪个？我们大概有一百万条文档，预算有限。',
  },
  {
    label: '代码问题',
    text: 'FastAPI 里怎么把流式响应推给前端？我想用 SSE，但不知道要不要额外装库。',
  },
]

const styleHint = computed(() => STYLES.find((x) => x.value === s.style.value)?.hint ?? '')

const voiceOptions = computed(() =>
  s.voices.value.map((v) => ({ label: `${v.name}（${v.lang}）`, value: v.voiceURI })),
)

/** 关键点：ASR 与 TTS 都在浏览器，它们的能力差异要如实告诉用户 */
const capabilityNote = computed(() => {
  if (s.asrSupported.value && s.ttsSupported.value) return ''
  if (!s.asrSupported.value && !s.ttsSupported.value) {
    return '当前浏览器既不支持语音识别也不支持语音合成（Chrome / Edge 可用）。直接在下面打字即可，后端流程完全一样。'
  }
  if (!s.asrSupported.value) {
    return '当前浏览器不支持语音识别（Chrome / Edge 可用）。打字提问即可，后端流程完全一样。'
  }
  return '当前浏览器不支持语音合成，朗读稿仍会生成，但没法在这儿直接念出来。'
})

function useSample(t: string) {
  s.transcript.value = t
  s.error.value = null
}
</script>

<template>
  <section class="voice-console" aria-label="语音问答控制台">
    <p class="principle">
      <span class="principle-mark">边界</span>
      语音能力该归谁？先问这个，再写代码。实测 <b>DeepSeek 没有音频接口</b>（
      <span class="ul">/audio/transcriptions</span> 与 <span class="ul">/audio/speech</span> 都是 404），
      所以「听」和「说」交给浏览器（<span class="ul">Web Speech API</span>，零依赖零密钥），
      服务端只做两件<b>只有模型能做</b>的事：把书面语改写成<b>念得顺</b>的口语，再把
      <span class="ul">2026-09-21</span>、<span class="ul">¥2,158.50</span>、
      <span class="ul">USB-C</span> 这类符号改写成<b>读出来的样子</b>。
      最后，用一条正则把「口语稿里还剩多少 Markdown」变成<b>可断言的数字</b>。
    </p>

    <!-- ⭐ 签名元素：拾音台（麦克风 + 波形 + 实时转写） -->
    <div class="voice-hero">
      <div class="mic-stage">
        <button
          type="button"
          class="mic"
          :class="{ live: s.listening.value }"
          :disabled="!s.asrSupported.value"
          :aria-pressed="s.listening.value"
          :title="s.asrSupported.value ? '点击开始/停止拾音' : '此浏览器不支持语音识别'"
          @click="s.listening.value ? s.stopListening() : s.startListening()"
        >
          <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
            <rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor" />
            <path
              d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6"
              fill="none"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
            />
          </svg>
          <span class="mic-label">{{ s.listening.value ? '聆听中' : '按住说' }}</span>
        </button>

        <div class="wave" :class="{ live: s.listening.value }" aria-hidden="true">
          <span v-for="i in 9" :key="i" class="bar" :style="{ animationDelay: `${i * 70}ms` }"></span>
        </div>

        <div class="stage-info">
          <p v-if="s.listening.value" class="status live">
            正在识别…说完点一下麦克风停止
          </p>
          <p v-else-if="s.asrSupported.value" class="status">
            点麦克风开始拾音，或直接在下面打字 —— 后端拿到的都只是<b>文本</b>
          </p>
          <p v-else class="status">此浏览器不支持语音识别，打字即可</p>
          <p class="status-sub">
            转写在浏览器本地完成后才发给服务端；音频本身不会上传给任何语音服务
          </p>
        </div>
      </div>

      <div class="transcript-box">
        <div class="transcript-head">
          <span class="field-label">用户提问（ASR 转写 / 手输）</span>
          <span class="count" :class="{ over: s.textTooLong.value }">
            {{ s.transcript.value.length }} / {{ MAX_TRANSCRIPT }}
          </span>
        </div>
        <textarea
          v-model="s.transcript.value"
          class="transcript-input"
          rows="3"
          placeholder="点上面的麦克风说一句，或直接在这里打字……"
        />
        <p v-if="s.interim.value" class="interim">
          <span class="interim-k">临时</span>{{ s.interim.value }}
        </p>
        <div class="samples">
          <span class="hint">示例：</span>
          <button
            v-for="x in SAMPLES"
            :key="x.label"
            type="button"
            class="sample"
            @click="useSample(x.text)"
          >
            {{ x.label }}
          </button>
        </div>
      </div>
    </div>

    <p v-if="capabilityNote" class="capability">ⓘ {{ capabilityNote }}</p>
    <p v-if="s.asrError.value" class="err-line">⚠️ {{ s.asrError.value }}</p>

    <!-- 参数：口语风格 + 目标字数 -->
    <div class="mode-row">
      <div class="seg" role="tablist" aria-label="口语风格">
        <button
          v-for="m in STYLES"
          :key="m.value"
          type="button"
          class="seg-item"
          :class="{ active: s.style.value === m.value }"
          role="tab"
          :aria-selected="s.style.value === m.value"
          @click="s.style.value = m.value"
        >
          {{ m.label }}
        </button>
      </div>
      <label class="param">
        <span>目标字数</span>
        <el-input-number
          v-model="s.maxChars.value"
          size="small"
          :min="40"
          :max="600"
          :step="20"
          controls-position="right"
          style="width: 120px"
        />
      </label>
      <span class="hint">{{ styleHint }}</span>
    </div>

    <div class="actions">
      <el-button
        type="primary"
        size="default"
        :loading="s.running.value"
        :disabled="!s.canRun.value"
        @click="s.run()"
      >
        {{ s.running.value ? '生成中…' : '生成语音稿' }}
      </el-button>
      <el-button size="small" text :disabled="s.running.value" @click="s.reset()">清空</el-button>
      <span class="hint">口语化改写 → 朗读稿规范化 → 确定性体检</span>
    </div>

    <p v-if="s.error.value" class="err-line">⚠️ {{ s.error.value }}</p>

    <template v-if="s.answer.value || s.running.value">
      <!-- ① 口语化改写（流式） -->
      <div class="panel">
        <div class="panel-head">
          <span class="field-label">① 口语化改写 · 流式</span>
          <span class="badges">
            <span class="ok-badge">{{ s.model.value || '—' }}</span>
            <span class="md-badge" :class="{ ok: s.markdownLeft.value === 0 }">
              Markdown 残留 {{ s.markdownLeft.value }}
            </span>
          </span>
        </div>
        <div class="answer">{{ s.answer.value }}<i v-if="s.running.value" class="caret"></i></div>
        <p class="probe">
          「念得顺」这条要求不必问模型 —— 一条正则就能数出残留的 Markdown 标记数，
          所以 <b>Markdown 残留 {{ s.markdownLeft.value }}</b> 是客观读数，不是模型的自我评价。
        </p>
      </div>

      <!-- ② 朗读稿 + TTS 控制 -->
      <div v-if="s.speak.value" class="panel">
        <div class="panel-head">
          <span class="field-label">② 朗读稿 · 已做文本规范化</span>
          <span class="badges">
            <span class="ok-badge" :class="{ warn: !s.cleanSpeak.value }">
              {{ s.cleanSpeak.value ? '干净稿' : '含残留标记' }}
            </span>
          </span>
        </div>

        <div class="tts-bar">
          <el-button
            size="small"
            type="primary"
            :disabled="!s.ttsSupported.value"
            @click="s.speaking.value ? s.stopSpeaking() : s.speakOut()"
          >
            {{ s.speaking.value ? '停止' : '朗读' }}
          </el-button>
          <el-button
            size="small"
            :disabled="!s.speaking.value"
            @click="s.togglePause()"
          >
            {{ s.paused.value ? '继续' : '暂停' }}
          </el-button>
          <label class="param">
            <span>语速</span>
            <el-select v-model="s.rate.value" size="small" style="width: 118px">
              <el-option v-for="r in RATES" :key="r.value" :label="r.label" :value="r.value" />
            </el-select>
          </label>
          <label class="param">
            <span>自动朗读</span>
            <el-switch v-model="s.autoSpeak.value" size="small" />
          </label>
          <label v-if="voiceOptions.length" class="param grow">
            <span>音色</span>
            <el-select v-model="s.voiceURI.value" size="small" style="width: 200px">
              <el-option v-for="v in voiceOptions" :key="v.value" :label="v.label" :value="v.value" />
            </el-select>
          </label>
        </div>

        <div class="speak">{{ s.speak.value }}</div>
        <p class="probe">
          送进 TTS 的是这一份，<b>不是</b>上一份。差别就在下面这些符号改写里 ——
          直接把 <span class="mono">2026-09-21</span> 丢给合成器，读出来往往是错的。
        </p>
      </div>

      <!-- ③ 规范化对照表 -->
      <div v-if="s.terms.value.length" class="panel">
        <div class="panel-head">
          <span class="field-label">③ 文本规范化对照 · 逐条可核对</span>
          <span class="badges">
            <span class="ok-badge">{{ s.terms.value.length }} 处改写</span>
            <span
              v-if="s.termCheck.value"
              class="check-badge"
              :class="{ warn: s.termsSuspect.value > 0 }"
            >
              核对 {{ s.termCheck.value.verified }}/{{ s.termCheck.value.total }} 通过
            </span>
          </span>
        </div>
        <table class="terms">
          <thead>
            <tr>
              <th class="col-n">#</th>
              <th>原文写法</th>
              <th>读出来的样子</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(t, i) in s.terms.value" :key="i">
              <td class="col-n">{{ i + 1 }}</td>
              <td class="col-from">{{ t.from }}</td>
              <td class="col-to">{{ t.to }}</td>
            </tr>
          </tbody>
        </table>
        <p class="probe">
          这张表是<b>模型自述</b>，不是 diff —— 服务端逐条核对过：
          <span class="mono">from</span> 要真的出现在口语稿里、<span class="mono">to</span>
          要真的出现在朗读稿里。
          <template v-if="s.termsSuspect.value > 0">
            <b>{{ s.termsSuspect.value }} 条对不上</b>（已在服务端原样返回），
            所以「表里没提到」不等于「文字没被改过」。
          </template>
          <template v-else>本次全部对上。</template>
        </p>
      </div>

      <!-- 体检 + 耗时 -->
      <div class="stats-bar">
        <div class="stat">
          <span class="stat-num">{{ s.rewriteChars.value }}</span>
          <span class="stat-label">口语稿字数</span>
        </div>
        <div class="stat">
          <span class="stat-num" :class="{ bad: s.markdownLeft.value > 0 }">
            {{ s.markdownLeft.value }}
          </span>
          <span class="stat-label">Markdown 残留</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ s.plainChars.value }}</span>
          <span class="stat-label">朗读有效字</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ s.humanSeconds(s.estSeconds.value) }}</span>
          <span class="stat-label">估算朗读时长</span>
        </div>
      </div>

      <div class="stats-bar subtle">
        <div class="stat">
          <span class="stat-num">{{ ((s.times.value['rewrite'] ?? 0) / 1000).toFixed(1) }}<i class="of">s</i></span>
          <span class="stat-label">口语化改写</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ ((s.times.value['normalize'] ?? 0) / 1000).toFixed(1) }}<i class="of">s</i></span>
          <span class="stat-label">文本规范化</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ (s.llmMs.value / 1000).toFixed(1) }}<i class="of">s</i></span>
          <span class="stat-label">模型合计</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ (s.totalMs.value / 1000).toFixed(1) }}<i class="of">s</i></span>
          <span class="stat-label">总耗时</span>
        </div>
      </div>
    </template>

    <p v-else class="empty">
      点麦克风说一句话（或直接打字、或用示例），再点「生成语音稿」。
      服务端会先改写成适合朗读的短句，再把日期 / 金额 / 型号改写成读出来的样子，
      最后告诉你朗读大约要几秒 —— 生成的朗读稿可以直接交给浏览器念出来。
    </p>
  </section>
</template>

<style scoped>
.voice-console {
  --accent: #5aa9ff; /* 蔚蓝：本阶段「语音」的主调，区别于阶段 15 的紫、阶段 16 的品红 */
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}

.principle {
  margin: 0;
  padding: 11px 14px;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--muted);
  background: var(--panel-2);
  border-left: 3px solid var(--accent);
  border-radius: 0 6px 6px 0;
}
.principle b { color: var(--paper); }
.principle .ul { color: var(--accent); font-weight: 600; }
.principle-mark {
  display: inline-block;
  margin-right: 8px;
  padding: 1px 7px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink);
  background: var(--accent);
  border-radius: 3px;
}

/* ---------- 签名元素：拾音台 ---------- */
.voice-hero { display: flex; gap: 12px; align-items: stretch; }

.mic-stage {
  display: flex;
  flex: 0 0 208px;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 14px 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
}

.mic {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  width: 84px;
  height: 84px;
  justify-content: center;
  color: var(--accent);
  background: var(--ink);
  border: 1.5px solid var(--line);
  border-radius: 50%;
  cursor: pointer;
  transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.1s ease;
}
.mic:hover:not(:disabled) { border-color: var(--accent); }
.mic:active:not(:disabled) { transform: scale(0.96); }
.mic:disabled { color: var(--muted); cursor: not-allowed; }
.mic:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.mic.live {
  color: var(--ink);
  background: var(--accent);
  border-color: var(--accent);
  box-shadow: 0 0 0 6px color-mix(in srgb, var(--accent) 22%, transparent);
}
.mic-label { font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.04em; }

/* 波形：聆听时跳动，静止时是一条线 —— 状态一眼可辨 */
.wave { display: flex; align-items: center; gap: 3px; height: 26px; }
.bar {
  width: 3px;
  height: 5px;
  background: var(--line);
  border-radius: 2px;
  transition: background 0.2s ease;
}
.wave.live .bar {
  background: var(--accent);
  animation: pulse 0.9s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { height: 5px; }
  50% { height: 22px; }
}
@media (prefers-reduced-motion: reduce) {
  .wave.live .bar { animation: none; height: 14px; }
}

.stage-info { text-align: center; }
.status { margin: 0; font-size: 11px; line-height: 1.5; color: var(--muted); }
.status b { color: var(--paper); }
.status.live { color: var(--accent); font-weight: 600; }
.status-sub { margin: 4px 0 0; font-size: 10px; line-height: 1.5; color: var(--muted); opacity: 0.8; }

.transcript-box {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
  padding: 13px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
}
.transcript-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.field-label { font-size: 11px; font-weight: 600; letter-spacing: 0.04em; color: var(--muted); }
.count { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.count.over { color: var(--c-bad, #ff7a72); font-weight: 600; }

.transcript-input {
  flex: 1 1 auto;
  min-height: 76px;
  padding: 9px 12px;
  font-family: var(--font-body);
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--paper);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  outline: none;
  resize: vertical;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.transcript-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent);
}
.transcript-input::placeholder { color: var(--muted); }

.interim {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--muted);
  font-style: italic;
}
.interim-k {
  margin-right: 6px;
  padding: 0 5px;
  font-family: var(--font-mono);
  font-size: 10px;
  font-style: normal;
  color: var(--accent);
  border: 1px dashed var(--accent);
  border-radius: 3px;
}

.samples { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.sample {
  padding: 3px 9px;
  font-family: var(--font-body);
  font-size: 11px;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 20px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.sample:hover { color: var(--accent); border-color: var(--accent); }

.capability {
  margin: 0;
  padding: 8px 11px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--muted);
  background: var(--panel-2);
  border-radius: 6px;
}

.err-line {
  margin: 0;
  padding: 8px 11px;
  font-size: 12px;
  color: var(--c-bad, #ff7a72);
  background: var(--panel);
  border: 1px solid var(--c-bad, #ff7a72);
  border-radius: 6px;
}

/* ---------- 参数行 ---------- */
.mode-row { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.seg { display: inline-flex; padding: 2px; background: var(--panel); border: 1px solid var(--line); border-radius: 7px; }
.seg-item {
  padding: 5px 14px;
  font-family: var(--font-body);
  font-size: 12px;
  color: var(--muted);
  background: transparent;
  border: none;
  border-radius: 5px;
  cursor: pointer;
  transition: color 0.15s ease, background 0.15s ease;
}
.seg-item:hover { color: var(--paper); }
.seg-item.active { color: var(--ink); background: var(--accent); font-weight: 600; }
.seg-item:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

.param { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }
.param.grow { flex: 1 1 auto; }
.hint { font-size: 10.5px; color: var(--muted); }
.actions { display: flex; align-items: center; gap: 8px; }

/* ---------- 面板通用 ---------- */
.panel { padding: 12px 13px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.badges { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.ok-badge {
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--data);
  background: color-mix(in srgb, var(--data) 16%, var(--ink));
  border-radius: 3px;
}
.ok-badge.warn { color: var(--c-bad, #ff7a72); background: color-mix(in srgb, #ff7a72 16%, var(--ink)); }
.md-badge {
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--signal);
  border-radius: 3px;
}
.md-badge.ok { color: var(--data); background: color-mix(in srgb, var(--data) 16%, var(--ink)); }

/* 对照表核对徽章：全部对上用 --data，有对不上的用告警色 */
.check-badge {
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--data);
  background: color-mix(in srgb, var(--data) 16%, var(--ink));
  border-radius: 3px;
}
.check-badge.warn { color: var(--ink); background: var(--signal); }

.answer,
.speak {
  min-height: 68px;
  padding: 11px 13px;
  font-size: 13.5px;
  line-height: 1.85;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--paper);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.speak { border-left: 3px solid var(--accent); }

.probe { margin: 10px 0 0; font-size: 11px; line-height: 1.65; color: var(--muted); }
.probe b { color: var(--accent); }
.probe .mono { font-family: var(--font-mono); font-size: 11px; color: var(--paper); }

/* ---------- TTS 控制条 ---------- */
.tts-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--line);
}

/* ---------- 规范化对照表 ---------- */
.terms { width: 100%; border-collapse: collapse; font-size: 12px; }
.terms th {
  padding: 6px 9px;
  text-align: left;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--muted);
  background: var(--panel-2);
  border-bottom: 1px solid var(--line);
}
.terms td { padding: 7px 9px; vertical-align: top; border-bottom: 1px solid var(--line); }
.terms tr:last-child td { border-bottom: none; }
.col-n { width: 34px; color: var(--muted); font-family: var(--font-mono); font-size: 10.5px; }
.col-from { width: 40%; font-family: var(--font-mono); font-size: 11.5px; color: var(--paper); word-break: break-word; }
.col-to { font-size: 12.5px; color: var(--accent); word-break: break-word; }

.caret {
  display: inline-block;
  width: 7px;
  height: 15px;
  margin-left: 2px;
  background: var(--accent);
  vertical-align: text-bottom;
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink { to { visibility: hidden; } }
@media (prefers-reduced-motion: reduce) {
  .caret { animation: none; }
}

/* ---------- 体检 + 耗时 ---------- */
.stats-bar { display: flex; flex-wrap: wrap; gap: 8px; }
.stats-bar.subtle .stat { background: var(--panel-2); }
.stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 104px;
  padding: 8px 11px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.stat-num { font-family: var(--font-display); font-size: 16px; color: var(--data); }
.stat-num.bad { color: var(--c-bad, #ff7a72); }
.stat-num .of { font-style: normal; font-size: 11px; color: var(--muted); }
.stat-label { font-size: 10px; color: var(--muted); }

.empty { margin: 4px 0 0; font-size: 12px; line-height: 1.7; color: var(--muted); }
</style>
