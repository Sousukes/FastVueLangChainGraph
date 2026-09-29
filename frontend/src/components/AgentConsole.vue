<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AGENT_PRESETS, REASON_TEXT, useAgent } from '../composables/useAgent'

defineProps<{
  stage: number
  title: string
}>()

const {
  catalog,
  groups,
  question,
  maxSteps,
  observationLimit,
  maxRepeat,
  running,
  steps,
  live,
  answer,
  reason,
  runTools,
  runGroups,
  model,
  totalMs,
  llmMs,
  contextChars,
  trace,
  busy,
  error,
  shadowed,
  usedTools,
  repeatCount,
  expectedTools,
  toolCount,
  loadCatalog,
  run,
  resetRun,
  toggleGroup,
  applyPreset,
} = useAgent()

onMounted(loadCatalog)

const showTools = ref(false)
const showTrace = ref(false)

/** 工具名 → 它来自哪些组（overlaps 里查得到就说明有重叠） */
const overlapOf = computed(() => {
  const m = new Map<string, { groups: string[]; winner: string }>()
  for (const o of catalog.value?.overlaps ?? []) m.set(o.name, o)
  return m
})

function groupLabel(g: string) {
  return catalog.value?.groups.find((x) => x.group === g)?.label ?? g
}

function fmtArgs(a: unknown): string {
  if (typeof a === 'string') return a
  try {
    return JSON.stringify(a)
  } catch {
    return String(a)
  }
}

function fmtChars(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

const finished = computed(() => reason.value !== null)
const reasonText = computed(() => (reason.value ? REASON_TEXT[reason.value] : null))

/** 时间线上那几张卡（= 工具轮）各自 LLM 耗时之和。 */
const toolLlmMs = computed(() => steps.value.reduce((n, s) => n + s.llmMs, 0))

/**
 * 「收尾作答」那一轮的 LLM 耗时 = 总 LLM − 工具轮之和。
 *
 * ⚠️ `react.py` **只把「有工具调用」的轮次写进 trace**（`steps.append(step_record)`
 *   在 660 行；「模型直接作答」那一轮在 589 行就 `return` 了）。所以卡片的数量
 *   **天然比 `finish.steps` 少一张**、分步求和也**必然小于**总 LLM 耗时。
 *   把差额单独摊出来，读数才自洽（否则看起来像"每步加起来不到总数"的错账）。
 *
 * 不去改后端把末轮也塞进 trace，是因为那会推翻 `react.py:552-563` 的明确设计：
 * 末轮流出的文本是**答案**而非 Thought，前端要丢掉缓冲区改用 `finish.answer`。
 */
const closingLlmMs = computed(() => Math.max(0, llmMs.value - toolLlmMs.value))
</script>

<template>
  <section class="agent-console" aria-label="ReAct 智能体控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 05 已经能调工具了，但那个循环里模型的输出只有两种可能：给答案，或给
      <code>tool_calls</code>——<b>推理过程完全不可见，也不进入上下文</b>。于是它下一步只能盯着
      "上一步的原始结果"做决定，而不是"上一步的结论"。
      <b>ReAct = Reason + Act</b>（Yao et al., 2022）的贡献只有一句话：
      <b>让模型在每一步动手之前先把推理写出来，并让这段推理留在上下文里。</b>
      本阶段把它做成三件事：显式的 <b>Thought</b>、<b>统一的工具面</b>（本地 + RAG + GraphRAG + MCP
      收进同一张清单）、以及<b>停止条件的纪律</b>（死循环 / 上下文膨胀 / 预算耗尽）。
      为什么这样能减少幻觉？因为<b>每一次断言都有机会被 Observation 打脸</b>。
    </p>

    <!-- 状态条 -->
    <div class="stats-bar">
      <div class="stat">
        <span class="stat-num">{{ toolCount || '—' }}</span>
        <span class="stat-label">
          工具面
          <template v-if="runTools.length">（已发送）</template>
          <template v-else-if="expectedTools.length">（按勾选预算）</template>
        </span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ steps.length }}<i class="of">/{{ maxSteps }}</i></span>
        <span class="stat-label">已用步数</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ fmtChars(contextChars) }}</span>
        <span class="stat-label">上下文（字符）</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ (totalMs / 1000).toFixed(1) }}<i class="of">s</i></span>
        <span class="stat-label">总耗时 · LLM {{ (llmMs / 1000).toFixed(1) }}s</span>
      </div>
      <div class="stat" :class="{ warn: repeatCount > 0 }">
        <span class="stat-num">{{ repeatCount }}</span>
        <span class="stat-label">重复调用</span>
      </div>
      <div v-if="model" class="stat wide">
        <span class="stat-types">
          <span v-for="t in usedTools" :key="t" class="tchip">{{ t }}</span>
          <span v-if="!usedTools.length" class="tchip muted">还没调用过工具</span>
        </span>
        <span class="stat-label">
          本次实际用到的工具 · 模型 <code>{{ model }}</code>
        </span>
      </div>
    </div>

    <!-- ① 工具面 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">① 工具面（勾掉几组再问同一个问题，对比最直观）</span>
        <button type="button" class="link-btn" @click="showTools = !showTools">
          {{ showTools ? '收起说明书' : '展开工具说明书' }}
        </button>
      </div>

      <div class="groups">
        <button
          v-for="g in catalog?.groups ?? []"
          :key="g.group"
          type="button"
          class="group-chip"
          :class="{ on: groups.includes(g.group), broken: !!g.error }"
          :title="g.error ? `加载失败：${g.error}` : `${g.tools.length} 个工具`"
          @click="toggleGroup(g.group)"
        >
          <i class="tick" :class="{ on: groups.includes(g.group) }"></i>
          {{ g.label }}
          <span class="chip-hint">{{ g.tools.length }}</span>
        </button>
      </div>

      <p v-if="groups.length === 0" class="warn note">
        <span class="warn-mark">!</span>
        一组工具都没勾。模型将<b>没有任何外部能力</b>，只能凭记忆回答——
        这也是个值得看的对照实验。
      </p>

      <!-- 重叠：工具面是需要设计的 -->
      <p v-if="(catalog?.overlaps.length ?? 0) > 0" class="warn note">
        <span class="warn-mark">!</span>
        工具面<b>有重叠</b>：
        <template v-for="(o, i) in catalog?.overlaps ?? []" :key="o.name">
          <code>{{ o.name }}</code>
          <span v-if="i < (catalog?.overlaps.length ?? 0) - 1">、</span>
        </template>
        同时由
        <b>{{ [...new Set((catalog?.overlaps ?? []).flatMap((o) => o.groups))].map(groupLabel).join(' 和 ') }}</b>
        提供。OpenAI 协议<b>不允许重名工具</b>，所以运行时按优先级
        <code>{{ catalog?.priority.join(' &gt; ') }}</code> 去重，本地版本生效
        （进程内一次函数调用，比走 MCP 的 IPC 快一个数量级）。
        这正是阶段 06 的 MCP server 把阶段 05 那批工具又包了一遍的直接后果——
        <b>真实 MCP host 用命名空间（<code>mcp__server__tool</code>）隔离，本项目用了更朴素的做法。</b>
      </p>

      <p v-if="shadowed.length" class="hint-line">
        本次工具面被去重挤掉的名字：<code>{{ shadowed.join('、') }}</code>
      </p>

      <div v-if="showTools" class="tool-list">
        <div v-for="g in catalog?.groups ?? []" :key="g.group" class="tool-group">
          <p class="tool-group-head">
            {{ g.label }}
            <span v-if="g.error" class="bad-mark">加载失败 · {{ g.error }}</span>
          </p>
          <div v-for="t in g.tools" :key="g.group + t.name" class="tool-item">
            <p class="tool-name">
              <code>{{ t.name }}</code>
              <span v-if="t.server" class="srv">{{ t.server }}</span>
              <span v-if="overlapOf.has(t.name)" class="dup">与 {{ overlapOf.get(t.name)?.winner }} 重叠</span>
            </p>
            <p class="tool-desc">{{ t.description }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- ② 提问与预算 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">② 提问与预算（每个旋钮都能单独做一次实验）</span>
        <span class="meta">Thought 会随生成实时流出，不用等整轮跑完</span>
      </div>

      <div class="presets">
        <button
          v-for="p in AGENT_PRESETS"
          :key="p.label"
          type="button"
          class="preset-chip"
          :class="{ active: question === p.question, danger: p.kind === 'loop' }"
          :title="p.question"
          @click="applyPreset(p.question)"
        >
          {{ p.label }}
          <span class="chip-hint">{{ p.hint }}</span>
        </button>
      </div>

      <div class="ctrl-row">
        <el-input
          v-model="question"
          size="small"
          placeholder="问一个需要查证的问题，比如「重排是哪个阶段引入的」"
          class="q-input"
          :disabled="running"
          @keyup.enter="run"
        />
        <el-button size="small" type="primary" :loading="running" :disabled="!question.trim()" @click="run">
          {{ running ? '运行中…' : '运行' }}
        </el-button>
        <el-button size="small" :disabled="running" @click="resetRun">清空</el-button>
      </div>

      <div class="ctrl-row">
        <label class="num">
          步数预算
          <el-input-number v-model="maxSteps" size="small" :min="1" :max="12" controls-position="right" />
        </label>
        <label class="num">
          观测上限
          <el-input-number v-model="observationLimit" size="small" :min="200" :max="4000" :step="100" controls-position="right" />
          字符
        </label>
        <label class="num">
          重复上限
          <el-input-number v-model="maxRepeat" size="small" :min="1" :max="5" controls-position="right" />
        </label>
        <span class="meta">
          调到 <b class="est">1</b> 步 → 看它信息不足时怎么收口 ·
          <b class="est">200</b> 字符 → 看截断会不会让它漏信息 ·
          重复上限 <b class="est">1</b> → 死循环检测立刻触发（但模型通常先一步拒绝病态指令——
          <b class="est">护栏是兜底，不是日常</b>）
        </span>
      </div>
    </div>

    <p v-if="error" class="warn note">
      <span class="warn-mark">!</span>
      {{ error }}
    </p>

    <!-- ③ 时间线 -->
    <div class="panel timeline-panel">
      <div class="panel-head">
        <span class="field-label">③ 时间线（Thought → Action → Observation）</span>
        <span v-if="runGroups.length" class="meta">本次启用 {{ runGroups.map(groupLabel).join(' · ') }}</span>
      </div>

      <p v-if="!steps.length && !live && !answer && !running" class="hint-line">
        点「运行」开始。<b>看着它想</b>是这一阶段唯一的看点——
        尤其是它第一次拿到 Observation 之后，下一步的 Thought 会明显变得更具体。
      </p>

      <article v-for="s in steps" :key="s.step" class="step-card">
        <header class="step-head">
          <span class="step-no">STEP {{ s.step }}</span>
          <span class="meta">LLM {{ s.llmMs }}ms</span>
          <span v-if="s.contextChars !== null" class="meta">上下文 {{ fmtChars(s.contextChars) }} 字符</span>
        </header>

        <div v-if="s.thought" class="node thought">
          <span class="node-tag">Thought</span>
          <p class="node-body">{{ s.thought }}</p>
        </div>
        <div v-else class="node thought empty">
          <span class="node-tag">Thought</span>
          <p class="node-body">（这一轮没写想法，直接动手了——ReAct 里这属于没按纪律来）</p>
        </div>

        <template v-for="a in s.actions" :key="a.callId">
          <div class="node action">
            <span class="node-tag">Action</span>
            <p class="node-body mono">{{ a.tool }}({{ fmtArgs(a.args) }})</p>
            <span v-if="a.repeat > 0" class="repeat-flag">
              第 {{ a.repeat + 1 }} 次同参调用
            </span>
          </div>
          <div class="node observation" :class="{ bad: a.ok === false, waiting: a.ok === null }">
            <span class="node-tag">Observation</span>
            <p class="node-body mono">{{ a.content || '…' }}</p>
            <p class="node-foot">
              <span v-if="a.ok === false" class="bad-mark">工具失败</span>
              <span v-else-if="a.ok" class="ok-mark">成功</span>
              <span v-if="a.ms">{{ a.ms }}ms</span>
              <span v-if="a.truncated" class="trunc-mark">
                已截断 {{ fmtChars(a.rawChars) }} → {{ fmtChars(a.content.length) }}
              </span>
            </p>
          </div>
        </template>
      </article>

      <!-- 单一 live 区域：定性之前，谁也不知道它是什么 -->
      <article v-if="live" class="step-card live-card">
        <header class="step-head">
          <span class="step-no">STEP {{ steps.length + 1 }}</span>
          <span class="meta live-mark">正在生成…</span>
        </header>
        <div class="node pending">
          <span class="node-tag">?</span>
          <p class="node-body">{{ live }}<span class="caret"></span></p>
        </div>
        <p class="live-note">
          流式下<b>没法提前知道</b>这段文本是 Thought 还是最终答案——唯一的分界线是这一步结束时
          <b>有没有 <code>action</code></b>。所以只能先流出来、再定性：
          收到 <code>action</code> 就落成想法，收到 <code>finish</code> 就是答案本身。
        </p>
      </article>

      <p v-if="running && !live && steps.length === 0" class="hint-line">
        已发出请求，等第一帧回来…
      </p>
    </div>

    <!-- ④ 最终答案 -->
    <div v-if="answer !== null" class="answer-box" :class="reason ?? ''">
      <div class="ans-head">
        <span class="field-label">④ 最终答案</span>
        <span v-if="reasonText" class="reason-tag" :class="reason ?? ''">{{ reasonText.label }}</span>
      </div>
      <p class="ans-text">{{ answer }}<span class="caret-done"></span></p>
      <p v-if="reasonText" class="ans-hint">{{ reasonText.hint }}</p>

      <div class="ans-foot">
        <span class="meta">
          工具轮 <b>{{ steps.length }}</b> 轮 ·
          工具调用 <b>{{ steps.reduce((n, s) => n + s.actions.length, 0) }}</b> 次 ·
          总耗时 <b>{{ (totalMs / 1000).toFixed(2) }}s</b>（LLM 占 {{ (llmMs / 1000).toFixed(2) }}s
          = 工具轮 {{ (toolLlmMs / 1000).toFixed(2) }}s + 收尾作答 {{ (closingLlmMs / 1000).toFixed(2) }}s，
          其余是检索/图谱/工具本身）
        </span>
        <button v-if="trace.length" type="button" class="link-btn" @click="showTrace = !showTrace">
          {{ showTrace ? '收起 trace' : '查看结构化 trace' }}
        </button>
      </div>

      <pre v-if="showTrace" class="trace">{{ JSON.stringify(trace, null, 2) }}</pre>
    </div>

    <p v-if="finished && !steps.length && !answer" class="warn note">
      <span class="warn-mark">!</span>
      这次运行没有产生任何步骤。多半是模型直接回答了（它认为不需要工具），
      或者链路在第一步就出错了。
    </p>
  </section>
</template>

<style scoped>
/* 三色：想 / 做 / 看。直接复用 design-system 的既有色，
   这样三个阶段（思考-行动-观察）在整门课里是同一套视觉语言。 */
.agent-console {
  --c-thought: #6fa8ff;
  --c-action: #ffb454;
  --c-observation: #5bc8b0;

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
  border-left: 3px solid var(--signal);
  border-radius: 0 6px 6px 0;
}
.principle b { color: var(--paper); }
.principle code,
.note code,
.hint-line code,
.ans-hint code,
.live-note code {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--data);
}
.principle-mark {
  display: inline-block;
  margin-right: 8px;
  padding: 1px 7px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink);
  background: var(--signal);
  border-radius: 3px;
}

.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--muted);
  text-transform: uppercase;
}
.meta { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
.mono { font-family: var(--font-mono); }
.link-btn {
  padding: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--data);
  background: none;
  border: 0;
  cursor: pointer;
  text-decoration: underline dotted;
}

/* 状态条 */
.stats-bar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-right: 16px;
  border-right: 1px solid var(--line);
}
.stat:last-child { border-right: 0; }
.stat.wide { flex: 1 1 240px; }
.stat-num { font-family: var(--font-display); font-size: 20px; font-weight: 700; color: var(--paper); }
.stat-num .of { font-size: 12px; font-style: normal; color: var(--muted); }
.stat.warn .stat-num { color: var(--signal); }
.stat-label { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.stat-types { display: flex; flex-wrap: wrap; gap: 6px; }
.tchip {
  padding: 1px 7px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--data);
  background: color-mix(in srgb, var(--data) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--data) 30%, transparent);
  border-radius: 3px;
}
.tchip.muted { color: var(--muted); background: none; border-color: var(--line); }

.warn.note,
.hint-line {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.6;
  border-radius: 6px;
}
.warn.note {
  display: block;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent);
}
.warn-mark { font-family: var(--font-mono); font-weight: 600; margin-right: 6px; }
.warn.note b { color: var(--signal); }
.hint-line { color: var(--muted); background: var(--panel-2); border: 1px solid var(--line); }
.hint-line b { color: var(--signal); }

/* 面板 */
.panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  /* ⚠️ 必须 `flex-shrink: 0`。
     `.agent-console` 是个 `overflow-y: auto` 的 flex 列，面板是它的 flex item。
     flex item 默认的 `min-height: auto`（= 内容高度）正是"不许被压扁"的保护——
     一旦有人顺手写成 `min-height: 0`（这看起来很"专业"），面板就会被压到比内容矮，
     卡片直接从边框里溢出去。这里显式钉死，让面板保持内容高度、由外层负责滚动。 */
  flex-shrink: 0;
}
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.timeline-panel { flex-shrink: 0; }

/* 控制件 */
.ctrl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.q-input { flex: 1 1 280px; min-width: 200px; }
.ctrl-row :deep(.el-input-number) { width: 100px; }
.num { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.est { color: var(--signal); font-family: var(--font-mono); }

.groups { display: flex; flex-wrap: wrap; gap: 8px; }
.group-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  font-size: 12px;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 9999px;
  cursor: pointer;
}
.group-chip:hover { border-color: var(--data); }
.group-chip.on { color: var(--paper); border-color: var(--data); background: color-mix(in srgb, var(--data) 14%, var(--panel-2)); }
.group-chip.broken { border-style: dashed; border-color: var(--signal); }
.tick {
  display: inline-block;
  width: 11px;
  height: 11px;
  border: 1px solid var(--muted);
  border-radius: 3px;
}
.tick.on { background: var(--data); border-color: var(--data); }
.chip-hint { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.presets { display: flex; flex-wrap: wrap; gap: 8px; }
.preset-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  font-size: 12px;
  color: var(--paper);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 9999px;
  cursor: pointer;
}
.preset-chip:hover { border-color: var(--signal); }
.preset-chip.active { border-color: var(--signal); background: color-mix(in srgb, var(--signal) 14%, var(--panel-2)); }
.preset-chip.danger { border-style: dashed; }

/* 工具说明书 */
.tool-list { display: flex; flex-direction: column; gap: 10px; max-height: 260px; overflow-y: auto; }
.tool-group { display: flex; flex-direction: column; gap: 6px; }
.tool-group-head { margin: 0; font-family: var(--font-mono); font-size: 11px; color: var(--signal); }
.tool-item {
  padding: 6px 10px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 5px;
}
.tool-name { margin: 0 0 2px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.tool-name code { font-family: var(--font-mono); font-size: 11.5px; color: var(--data); }
.srv {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: 0 4px;
}
.dup { font-family: var(--font-mono); font-size: 10px; color: var(--signal); }
.tool-desc { margin: 0; font-size: 11.5px; line-height: 1.55; color: var(--muted); }

/* 时间线 */
.step-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.step-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.step-no {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--paper);
}
.live-card { border-style: dashed; border-color: color-mix(in srgb, var(--data) 50%, var(--line)); }
.live-mark { color: var(--data); }

.node {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 4px 10px;
  padding: 8px 10px;
  border-left: 3px solid var(--line);
  border-radius: 0 5px 5px 0;
  background: var(--ink);
}
.node-tag {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  padding-top: 1px;
}
.node-body {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.62;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
}
.node-body.mono { font-family: var(--font-mono); font-size: 11.5px; color: var(--muted); }
.node-foot {
  grid-column: 2;
  margin: 0;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--muted);
}

.node.thought { border-left-color: var(--c-thought); }
.node.thought .node-tag { color: var(--c-thought); }
.node.thought.empty .node-body { color: var(--muted); font-style: italic; }

.node.action { border-left-color: var(--c-action); }
.node.action .node-tag { color: var(--c-action); }
.node.action .node-body { color: var(--paper); }

.node.observation { border-left-color: var(--c-observation); }
.node.observation .node-tag { color: var(--c-observation); }
.node.observation.bad { border-left-color: #ff8fa3; }
.node.observation.bad .node-tag { color: #ff8fa3; }
.node.observation.waiting { opacity: 0.6; }

.node.pending { border-left-color: var(--data); }
.node.pending .node-tag { color: var(--data); }

.repeat-flag {
  grid-column: 2;
  justify-self: start;
  padding: 1px 7px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent);
  border-radius: 3px;
}
.ok-mark { color: var(--c-observation); }
.bad-mark { color: #ff8fa3; }
.trunc-mark { color: var(--signal); }

.live-note {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--muted);
}
.live-note b { color: var(--paper); }

/* 答案 */
.answer-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--data);
  border-radius: 0 8px 8px 0;
}
.answer-box.exhausted { border-left-color: var(--signal); }
.answer-box.loop { border-left-color: #ff8fa3; }
.answer-box.error { border-left-color: #ff8fa3; }
.ans-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.reason-tag {
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--ink);
  background: var(--data);
  border-radius: 3px;
}
.reason-tag.exhausted { background: var(--signal); }
.reason-tag.loop,
.reason-tag.error { background: #ff8fa3; }
.ans-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.72;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
}
.ans-hint { margin: 0; font-size: 11.5px; line-height: 1.6; color: var(--muted); }
.caret-done {
  display: inline-block;
  width: 0.5ch;
  height: 1.05em;
  margin-left: 1px;
  vertical-align: text-bottom;
  background: var(--data);
  border-radius: 50%;
}
.ans-foot { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.ans-foot b { color: var(--data); font-family: var(--font-mono); }
.trace {
  margin: 0;
  max-height: 280px;
  overflow: auto;
  padding: 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.55;
  color: var(--muted);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
}
</style>
