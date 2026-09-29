<script setup lang="ts">
import { computed } from 'vue'
import { useResearch, type AnswerPart } from '../composables/useResearch'

defineProps<{ stage: number; title: string }>()

const s = useResearch()

const CHANNEL_LABELS: Record<string, string> = {
  vector: '向量检索（阶段 07）',
  hybrid: '混合检索 + 重排（阶段 08）',
  graph: '知识图谱（阶段 09）',
  none: '未检索',
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待研究',
  researching: '检索中',
  writing: '撰写中',
  done: '已完成',
}

/** 把一行文本拆成「文本 / 引用 chip」交替片段（与 useSearch 同规则） */
function splitParts(line: string): AnswerPart[] {
  const parts: AnswerPart[] = []
  const re = /\[(\d+)\]/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) parts.push({ kind: 'text', value: line.slice(last, m.index) })
    const n = Number(m[1])
    if (s.sources.value.length > 0 && n >= 1 && n <= s.sources.value.length) {
      parts.push({ kind: 'cite', value: n })
    } else {
      parts.push({ kind: 'text', value: m[0] })
    }
    last = m.index + m[0].length
  }
  if (last < line.length) parts.push({ kind: 'text', value: line.slice(last) })
  return parts
}

/** 报告按行渲染：以 #/## 开头的当作小标题，其余按 [n] 拆 chip。 */
interface RenderedLine {
  heading?: string
  parts?: AnswerPart[]
}
const renderedAnswer = computed<RenderedLine[]>(() => {
  const text = s.answer.value
  if (!text) return []
  return text.split('\n').map((line) => {
    const h = /^#{1,3}\s+(.*)$/.exec(line)
    if (h) return { heading: h[1] }
    return { parts: splitParts(line) }
  })
})

const faithLabel = computed(() => {
  if (!s.answer.value) return ''
  if (s.faithful.value) return '已通过忠实性核查'
  if (s.unsupported.value.length) return `⚠ ${s.unsupported.value.length} 处结论资料无法支持`
  return '忠实性未确认'
})

const sortedSources = computed(() =>
  [...s.sources.value].sort((a, b) => Number(b.cited) - Number(a.cited) || a.rank - b.rank),
)

/** 把一组编号渲染成「[1] [3] [5]」样式，供大纲里的「引用」一行展示。 */
function citeText(list: number[]): string {
  return list.length ? list.map((n) => `[${n}]`).join(' ') : ''
}
</script>

<template>
  <section class="research-console" aria-label="深度研究控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 14 让答案「带出处、可核对」——但它只能验证「模型<span class="ul">声称</span>引用了」。
      本阶段再往前一步：先把问题<span class="ul">规划成研究大纲</span>，每节各自跑
      <span class="ul">自适应检索</span>（复用阶段 13），再合并成一篇带全局引用的长报告，
      最后用一次额外 LLM 调用做<span class="ul">忠实性核查</span>——逐句确认报告真的被资料支持。
    </p>

    <!-- ⭐ 签名元素：研究问题输入框 -->
    <div class="research-hero">
      <div class="research-box" :class="{ active: s.running.value }">
        <span class="research-glyph" aria-hidden="true">⌕</span>
        <textarea
          v-model="s.question.value"
          class="research-input"
          rows="2"
          placeholder="输入一个需要深入研究的问题，比如「如何系统评估一份 RAG 系统的检索质量？」"
          @keydown.ctrl.enter="s.canRun.value && s.run()"
          @keydown.meta.enter="s.canRun.value && s.run()"
        />
      </div>
      <div class="research-actions">
        <el-button
          type="primary"
          size="default"
          :loading="s.running.value"
          :disabled="!s.canRun.value"
          @click="s.run()"
        >
          {{ s.running.value ? '研究中…' : '开始深度研究' }}
        </el-button>
        <el-button size="small" :disabled="s.running.value" text @click="s.reset()">清空</el-button>
      </div>
      <div class="research-params">
        <label class="param">
          <span>每节检索条数</span>
          <el-input-number v-model="s.topK.value" size="small" :min="1" :max="10" controls-position="right" />
        </label>
        <label class="param">
          <span>研究小节数</span>
          <el-input-number v-model="s.maxSections.value" size="small" :min="1" :max="6" controls-position="right" />
        </label>
        <label class="param toggle">
          <el-switch v-model="s.rerank.value" size="small" />
          <span>Cross-encoder 重排</span>
        </label>
        <span class="hint">Ctrl/⌘ + Enter 开始</span>
      </div>
    </div>

    <p v-if="s.error.value" class="err-line">⚠️ {{ s.error.value }}</p>

    <!-- 状态条 -->
    <div v-if="s.answer.value || s.running.value" class="stats-bar">
      <div class="stat">
        <span class="stat-num" :class="{ bad: !s.grounded.value }">{{ s.grounded.value ? '已挂出处' : '未挂出处' }}</span>
        <span class="stat-label">答案可验证</span>
      </div>
      <div class="stat">
        <span class="stat-num" :class="{ bad: !s.faithful.value }">
          {{ (s.faithfulness.value * 100).toFixed(0) }}<i class="of">%</i>
        </span>
        <span class="stat-label">忠实性核查</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ s.citations.value.length }}<i class="of">/ {{ s.sources.value.length }}</i></span>
        <span class="stat-label">被引用 / 来源数</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ s.outline.value.length }}</span>
        <span class="stat-label">研究小节</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ (s.coverage.value * 100).toFixed(0) }}<i class="of">%</i></span>
        <span class="stat-label">引用覆盖率</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ (s.totalMs.value / 1000).toFixed(1) }}<i class="of">s</i></span>
        <span class="stat-label">总耗时 · LLM {{ (s.llmMs.value / 1000).toFixed(1) }}s</span>
      </div>
      <div v-if="s.model.value" class="stat wide">
        <span class="stat-types">{{ s.model.value }}</span>
        <span class="stat-label">模型</span>
      </div>
    </div>

    <template v-if="s.outline.value.length">
      <!-- 研究大纲 / 分段进度 -->
      <div class="panel outline-panel">
        <div class="panel-head">
          <span class="field-label">研究大纲 · 分段进度</span>
        </div>
        <ol class="outline">
          <li
            v-for="sec in s.outline.value"
            :key="sec.index"
            class="outline-item"
            :class="sec.status"
          >
            <header class="outline-head">
              <span class="sec-index">{{ sec.index }}</span>
              <b class="sec-title">{{ sec.title }}</b>
              <span class="sec-status" :class="sec.status">{{ STATUS_LABELS[sec.status] }}</span>
            </header>
            <p class="sec-q">↳ {{ sec.subQuestion }}</p>
            <p v-if="sec.status !== 'pending'" class="sec-meta">
              <span v-if="sec.hitCount">命中 {{ sec.hitCount }} 段</span>
              <span v-if="sec.rounds">· 自适应 {{ sec.rounds }} 轮</span>
              <span v-if="sec.basedOn">· {{ CHANNEL_LABELS[sec.basedOn] ?? sec.basedOn }}</span>
              <span v-if="sec.citations.length">· 引用 {{ citeText(sec.citations) }}</span>
            </p>
            <p v-if="sec.draft" class="sec-draft">{{ sec.draft }}</p>
          </li>
        </ol>
      </div>
    </template>

    <template v-if="s.answer.value || s.running.value">
      <!-- 报告卡 -->
      <div class="panel answer-panel">
        <div class="panel-head">
          <span class="field-label">研究报告 · 带全局引用</span>
          <span class="faith-badge" :class="{ ok: s.faithful.value, bad: !s.faithful.value && s.answer.value }">
            {{ faithLabel }}
          </span>
        </div>
        <div class="answer">
          <template v-for="(line, li) in renderedAnswer" :key="li">
            <h3 v-if="line.heading" class="ans-h">{{ line.heading }}</h3>
            <span v-else class="ans-line">
              <template v-for="(p, i) in (line.parts || [])" :key="i">
                <span v-if="p.kind === 'text'" class="ans-text">{{ p.value }}</span>
                <button
                  v-else
                  type="button"
                  class="cite-chip"
                  :class="{ active: s.activeSource.value === p.value }"
                  :title="`查看来源 [${p.value}]`"
                  @click="s.focusSource(p.value as number)"
                >
                  [{{ p.value }}]
                </button>
              </template>
            </span>
          </template>
          <i v-if="s.running.value" class="caret"></i>
        </div>
        <ul v-if="s.unsupported.value.length" class="unsupported">
          <li v-for="(u, i) in s.unsupported.value" :key="i">⚠ 资料无法支持：{{ u }}</li>
        </ul>
      </div>

      <!-- 来源面板 -->
      <div class="panel sources-panel">
        <div class="panel-head">
          <span class="field-label">来源 · 可核对（点报告里的 [n] 定位）</span>
        </div>
        <ul class="sources">
          <li
            v-for="src in sortedSources"
            :id="`src-${src.rank}`"
            :key="src.rank"
            class="source"
            :class="{ cited: src.cited, active: s.activeSource.value === src.rank }"
          >
            <header class="src-head">
              <span class="src-rank">{{ src.rank }}</span>
              <b class="src-title">《{{ src.title }}》第 {{ src.index }} 段</b>
              <span v-if="src.cited" class="src-cited">已引用</span>
              <span v-if="src.channel" class="src-channel">{{ CHANNEL_LABELS[src.channel] ?? src.channel }}</span>
              <span v-if="typeof src.rerankScore === 'number'" class="src-score">重排 {{ src.rerankScore.toFixed(3) }}</span>
              <span v-else-if="typeof src.score === 'number'" class="src-score">{{ src.score.toFixed(3) }}</span>
            </header>
            <p class="src-snippet">{{ src.snippet }}</p>
          </li>
        </ul>
      </div>
    </template>

    <p v-else class="empty">
      在上方输入一个研究性问题并点击「开始深度研究」。系统会先生成研究大纲，逐节检索与撰写，
      最后产出一份带出处、且经过忠实性核查的长报告。
    </p>
  </section>
</template>

<style scoped>
.research-console {
  --accent: #8b7cf6; /* 紫：本阶段「深度研究」的主调，区别于阶段 14 的琥珀 */
  --cited: var(--data); /* 青绿：被引用的来源 */
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

/* ---------- 签名元素：研究问题输入框 ---------- */
.research-hero { display: flex; flex-direction: column; gap: 8px; }
.research-box {
  display: flex;
  align-items: stretch;
  gap: 10px;
  padding: 8px 10px 8px 14px;
  background: var(--panel);
  border: 1.5px solid var(--line);
  border-radius: 10px;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.research-box.active,
.research-box:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
}
.research-glyph {
  font-size: 20px;
  color: var(--accent);
  font-family: var(--font-mono);
  align-self: flex-start;
  margin-top: 4px;
}
.research-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 6px 0;
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.6;
  color: var(--paper);
  background: transparent;
  border: none;
  outline: none;
  resize: vertical;
}
.research-input::placeholder { color: var(--muted); }
.research-actions { display: flex; align-items: center; gap: 8px; }
.research-params { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.param { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }
.param :deep(.el-input-number) { width: 92px; }
.param.toggle { gap: 7px; }
.hint { font-size: 10.5px; color: var(--muted); }

.err-line {
  margin: 0;
  padding: 8px 11px;
  font-size: 12px;
  color: var(--c-bad, #ff7a72);
  background: var(--panel);
  border: 1px solid var(--c-bad, #ff7a72);
  border-radius: 6px;
}

/* ---------- 状态条 ---------- */
.stats-bar { display: flex; flex-wrap: wrap; gap: 8px; }
.stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 96px;
  padding: 8px 11px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.stat.wide { flex: 1 1 200px; min-width: 0; }
.stat-num { font-family: var(--font-display); font-size: 16px; color: var(--data); }
.stat-num.bad { color: var(--muted); }
.stat-num .of { font-style: normal; font-size: 11px; color: var(--muted); }
.stat-label { font-size: 10px; color: var(--muted); }
.stat-types { font-family: var(--font-mono); font-size: 11px; color: var(--paper); }

/* ---------- 面板通用 ---------- */
.panel {
  padding: 12px 13px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.field-label { font-size: 11px; font-weight: 600; letter-spacing: 0.04em; color: var(--muted); }

/* ---------- 研究大纲 ---------- */
.outline { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.outline-item {
  padding: 9px 11px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-left: 3px solid var(--line);
  border-radius: 6px;
  transition: border-color 0.15s ease, opacity 0.15s ease;
}
.outline-item.researching { border-left-color: var(--accent); }
.outline-item.writing { border-left-color: var(--data); }
.outline-item.done { border-left-color: var(--data); opacity: 1; }
.outline-item.pending { opacity: 0.6; }
.outline-head { display: flex; align-items: center; gap: 8px; }
.sec-index {
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink);
  background: var(--accent);
  border-radius: 4px;
}
.sec-title { font-size: 13px; color: var(--paper); }
.sec-status {
  margin-left: auto;
  padding: 1px 8px;
  font-size: 10px;
  font-family: var(--font-mono);
  border-radius: 3px;
  color: var(--muted);
  background: var(--panel-2);
}
.sec-status.researching { color: var(--ink); background: var(--accent); }
.sec-status.writing { color: var(--ink); background: var(--data); }
.sec-status.done { color: var(--ink); background: var(--data); }
.sec-q { margin: 5px 0 0; font-size: 11.5px; color: var(--muted); }
.sec-meta { margin: 4px 0 0; font-size: 10.5px; color: var(--muted); font-family: var(--font-mono); }
.sec-draft {
  margin: 6px 0 0;
  padding: 7px 9px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--muted);
  background: var(--panel-2);
  border-radius: 5px;
  max-height: 88px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  line-clamp: 4;
  -webkit-box-orient: vertical;
}

/* ---------- 答案卡 ---------- */
.answer {
  min-height: 80px;
  padding: 11px 13px;
  font-size: 13px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--paper);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.ans-h {
  margin: 14px 0 6px;
  font-family: var(--font-display);
  font-size: 14px;
  color: var(--accent);
}
.ans-h:first-child { margin-top: 0; }
.ans-line { white-space: pre-wrap; }
.ans-text { white-space: pre-wrap; }
.cite-chip {
  display: inline-block;
  margin: 0 1px;
  padding: 0 5px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.5;
  color: var(--ink);
  background: var(--accent);
  border: none;
  border-radius: 3px;
  cursor: pointer;
  vertical-align: 1px;
  transition: transform 0.12s ease, box-shadow 0.12s ease;
}
.cite-chip:hover { transform: translateY(-1px); }
.cite-chip.active { box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 45%, transparent); }
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

.faith-badge {
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 3px;
  font-family: var(--font-mono);
}
.faith-badge.ok { color: var(--ink); background: var(--data); }
.faith-badge.bad { color: #ffd9d6; background: var(--c-bad, #ff7a72); }

.unsupported {
  margin: 10px 0 0;
  padding: 8px 11px;
  list-style: none;
  font-size: 11.5px;
  line-height: 1.6;
  color: #ffd9d6;
  background: color-mix(in srgb, var(--c-bad, #ff7a72) 14%, var(--ink));
  border: 1px solid var(--c-bad, #ff7a72);
  border-radius: 6px;
}

/* ---------- 来源面板 ---------- */
.sources { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 7px; }
.source {
  padding: 8px 10px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
  opacity: 0.6;
  transition: border-color 0.15s ease, opacity 0.15s ease, box-shadow 0.15s ease;
}
.source.cited { opacity: 1; border-color: var(--cited); }
.source.active { box-shadow: 0 0 0 2px color-mix(in srgb, var(--cited) 40%, transparent); }
.src-head { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-bottom: 5px; }
.src-rank {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink);
  background: var(--accent);
  border-radius: 4px;
}
.source.cited .src-rank { background: var(--cited); }
.src-title { font-size: 12px; color: var(--paper); }
.src-cited {
  padding: 0 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--cited);
  border-radius: 3px;
}
.src-channel { font-size: 10px; color: var(--accent); }
.src-score { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.src-snippet {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--muted);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.empty { margin: 4px 0 0; font-size: 12px; line-height: 1.7; color: var(--muted); }
</style>
