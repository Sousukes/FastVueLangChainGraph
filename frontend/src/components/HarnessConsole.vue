<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useHarness, type HarnessRoleInfo } from '../composables/useHarness'

defineProps<{
  stage: number
  title: string
}>()

const {
  roles,
  role,
  question,
  maxSteps,
  temperature,
  observationLimit,
  running,
  column,
  answer,
  model,
  totalMs,
  llmMs,
  steps,
  wallMs,
  error,
  finished,
  busy,
  HARNESS_PRESETS,
  loadRoles,
  run,
  resetRun,
  fmtArgs,
  fmtChars,
} = useHarness()

onMounted(loadRoles)

/** 时间线上那几张卡（= 工具轮）各自 LLM 耗时之和。 */
const toolLlmMs = computed(() =>
  (column.value?.steps ?? []).reduce((n, s) => n + s.llmMs, 0),
)

/**
 * 「收尾作答」那一轮的 LLM 耗时 = 总 LLM − 工具轮之和。
 *
 * ⚠️ 为什么要把这个差额**单独摊出来**，而不是让它悄悄藏在总数里：
 *   `react.py` **只把「有工具调用」的轮次写进 trace**（`steps.append(step_record)`
 *   在 660 行；而「模型直接作答」那一轮在 589 行就 `return` 了）。
 *   所以卡片数**天然比 `finish.steps` 少一张**，分步求和也**必然小于**总 LLM 耗时
 *   —— 这不是"读数对不上"，而是原先没人把差额说明白。
 *
 * 另一种改法是让后端把这轮也 append 进 trace，但那会**推翻** `react.py:552-563`
 * 明确写下的设计：末轮流出的文本是**答案**，不是 Thought，前端必须丢掉缓冲区、
 * 改用权威的 `finish.answer`。把答案再渲染成一张"Thought"卡是重复且自相矛盾的
 * —— 所以选择在前端把差额摊开，而不是去改那个刻意的设计。
 */
const closingLlmMs = computed(() => Math.max(0, llmMs.value - toolLlmMs.value))

/** 角色名 → 标签 */
function roleLabel(name: string): string {
  return roles.value.find((r) => r.name === name)?.label ?? name
}
const roleColors: Record<string, string> = {
  researcher: '#5bc8b0',
  analyst: '#6fa8ff',
  pure: '#9d8cff',
}
function colorOf(name: string): string {
  return roleColors[name] ?? '#8b95a7'
}
</script>

<template>
  <section class="harness-console" aria-label="Agent Harness 控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 05/10/11 三处都写了一遍「模型决策 → 调用工具 → 回填 → 再决策」的循环。本阶段把它抽成
      一个<b>薄、可复用</b>的 <code>harness.Agent</code>：单智能体、多智能体的每个角色、这里单独选一个角色跑，
      都只是 <code>Agent(...).run(question)</code>。下面这页跑的角色，和多智能体（阶段 11）里同名的那一栏
      <b>是同一段代码</b>——区别只在构造参数。选一个角色，看它怎么跑。
    </p>

    <!-- 状态条 -->
    <div class="stats-bar">
      <div class="stat">
        <span class="stat-num">{{ roleLabel(role) }}<i class="of">· {{ role }}</i></span>
        <span class="stat-label">当前角色</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ column?.tools.length ?? 0 }}</span>
        <span class="stat-label">该角色工具面</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ (wallMs / 1000).toFixed(1) }}<i class="of">s</i></span>
        <span class="stat-label">总耗时 · LLM {{ (llmMs / 1000).toFixed(1) }}s</span>
      </div>
      <div v-if="model" class="stat wide">
        <span class="stat-types">{{ model }}</span>
        <span class="stat-label">模型</span>
      </div>
    </div>

    <!-- ① 角色工具面 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">① 可选角色（每个都是 harness.Agent 的一个构造参数组合）</span>
      </div>
      <div class="roles">
        <div
          v-for="r in roles"
          :key="r.name"
          class="role-card"
          :class="{ on: r.name === role }"
          :style="{ '--rc': colorOf(r.name) }"
          @click="role = r.name"
        >
          <header class="role-head">
            <span class="role-dot"></span>
            <b>{{ r.label }}</b>
            <span class="role-name">{{ r.name }}</span>
          </header>
          <p class="role-desc">{{ r.description }}</p>
          <p class="role-tools">
            <template v-if="r.tools.length">
              <span v-for="t in r.tools" :key="t" class="tchip">{{ t }}</span>
            </template>
            <span v-else class="tchip muted">无工具</span>
          </p>
        </div>
      </div>
      <p class="hint-line">
        点一张卡切换角色。<b>工具面不同，分工才真实</b>——检索员只能检索、分析员只能计算、纯净助手没有工具。
        这和阶段 11 的「伪多智能体自检」是同一份逻辑。
      </p>
    </div>

    <!-- ② 角色选择 + 提问 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">② 选角色 · 提问 · 运行</span>
      </div>

      <div class="presets">
        <button
          v-for="p in HARNESS_PRESETS"
          :key="p.role"
          type="button"
          class="preset-chip"
          :class="{ active: role === p.role }"
          @click="role = p.role; question = p.question"
        >
          {{ p.label }}
          <span class="chip-hint">{{ p.hint }}</span>
        </button>
      </div>

      <div class="ctrl-row">
        <label class="num">
          角色
          <el-select v-model="role" size="small" :disabled="running" style="width: 120px">
            <el-option v-for="r in roles" :key="r.name" :label="r.label" :value="r.name" />
          </el-select>
        </label>
        <el-input
          v-model="question"
          size="small"
          placeholder="问这个角色一个适合它工具面的问题"
          class="q-input"
          :disabled="running"
          @keyup.enter="run"
        />
        <el-button size="small" type="primary" :loading="running" :disabled="!question.trim()" @click="run">
          {{ running ? '运行中…' : '运行 harness.Agent' }}
        </el-button>
        <el-button size="small" :disabled="running" @click="resetRun">清空</el-button>
      </div>

      <div class="ctrl-row">
        <label class="num">
          最大步数
          <el-input-number v-model="maxSteps" size="small" :min="1" :max="12" controls-position="right" />
        </label>
        <label class="num">
          温度
          <el-input-number v-model="temperature" size="small" :min="0" :max="1" :step="0.1" controls-position="right" />
        </label>
        <label class="num">
          观测上限
          <el-input-number v-model="observationLimit" size="small" :min="200" :max="4000" :step="100" controls-position="right" />
          字符
        </label>
      </div>
    </div>

    <p v-if="error" class="warn note">
      <span class="warn-mark">!</span>
      {{ error }}
    </p>

    <!-- ③ 单角色时间线 -->
    <div class="panel timeline-panel">
      <div class="panel-head">
        <span class="field-label">③ 单角色时间线（和它在多智能体里那一栏同构）</span>
      </div>

      <p v-if="!column && !running" class="hint-line">
        选一个角色、点「运行」。下面这一栏的渲染逻辑和多智能体（阶段 11）里每个角色的栏
        <b>完全一样</b>——这就是「框架复用」最直观的证据。
      </p>

      <div v-if="column" class="col" :style="{ '--rc': colorOf(column.role) }">
        <header class="col-head">
          <span class="col-dot"></span>
          <b>{{ column.label }}</b>
          <span class="col-id">{{ column.role }}</span>
          <span v-if="column.tools.length" class="col-tools">{{ column.tools.join(' · ') }}</span>
          <span class="col-state">{{ column.ended ? '✓' : column.started ? '运行中' : '待命' }}</span>
        </header>

        <article v-for="s in column.steps" :key="s.step" class="step-card">
          <header class="step-head">
            <span class="step-no">STEP {{ s.step }}</span>
            <span class="meta">LLM {{ s.llmMs }}ms</span>
          </header>
          <div v-if="s.thought" class="node thought">
            <span class="node-tag">Thought</span>
            <p class="node-body">{{ s.thought }}</p>
          </div>
          <template v-for="a in s.actions" :key="a.callId">
            <div class="node action">
              <span class="node-tag">Action</span>
              <p class="node-body mono">{{ a.tool }}({{ fmtArgs(a.args) }})</p>
            </div>
            <div class="node observation" :class="{ bad: a.ok === false, waiting: a.ok === null }">
              <span class="node-tag">Observation</span>
              <p class="node-body mono">{{ a.content || '…' }}</p>
              <p class="node-foot">
                <span v-if="a.ok === false" class="bad-mark">工具失败</span>
                <span v-else-if="a.ok" class="ok-mark">成功</span>
                <span v-if="a.ms">{{ a.ms }}ms</span>
                <span v-if="a.truncated" class="trunc-mark">已截断 {{ fmtChars(a.rawChars) }}</span>
              </p>
            </div>
          </template>
        </article>

        <div v-if="column.live" class="node pending">
          <span class="node-tag">?</span>
          <p class="node-body">{{ column.live }}<span class="caret"></span></p>
        </div>

        <p v-if="column.answer !== null" class="col-answer">
          <span class="ans-label">结论</span>{{ column.answer }}
          <span class="col-ms">{{ (column.totalMs / 1000).toFixed(2) }}s</span>
        </p>
        <p v-else-if="column.ended" class="col-answer muted">（该角色未给出结论）</p>
      </div>
    </div>

    <!-- ④ 结论 -->
    <div v-if="answer !== null" class="answer-wrap">
      <div class="answer-box">
        <div class="ans-head">
          <span class="field-label">④ 最终答案</span>
          <span class="meta">
            共 {{ steps }} 步（工具轮 {{ column?.steps.length ?? 0 }} + 收尾作答 1）
          </span>
        </div>
        <p class="ans-text">{{ answer }}<span class="caret-done"></span></p>
        <p class="ans-foot">
          <span class="meta">
            总耗时 <b>{{ (totalMs / 1000).toFixed(2) }}s</b> · LLM 占 {{ (llmMs / 1000).toFixed(2) }}s
            = 工具轮 <b>{{ (toolLlmMs / 1000).toFixed(2) }}s</b>
            + 收尾作答 <b>{{ (closingLlmMs / 1000).toFixed(2) }}s</b>
          </span>
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.harness-console {
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
.hint-line code {
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
.role-name { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.stats-bar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.stat { display: flex; flex-direction: column; gap: 2px; padding-right: 16px; border-right: 1px solid var(--line); }
.stat:last-child { border-right: 0; }
.stat.wide { flex: 1 1 200px; }
.stat-num { font-family: var(--font-display); font-size: 20px; font-weight: 700; color: var(--paper); }
.stat-num .of { font-size: 12px; font-style: normal; color: var(--muted); }
.stat-label { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.stat-types { font-family: var(--font-mono); font-size: 11px; color: var(--data); }

.warn.note,
.hint-line { margin: 0; padding: 8px 12px; font-size: 12px; line-height: 1.6; border-radius: 6px; }
.warn.note { display: block; color: var(--signal); background: color-mix(in srgb, var(--signal) 10%, transparent); border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent); }
.warn-mark { font-family: var(--font-mono); font-weight: 600; margin-right: 6px; }
.warn.note b { color: var(--signal); }
.hint-line { color: var(--muted); background: var(--panel-2); border: 1px solid var(--line); }
.hint-line b { color: var(--signal); }

.panel { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; flex-shrink: 0; }
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }

.roles { display: flex; flex-wrap: wrap; gap: 10px; }
.role-card {
  flex: 1 1 200px;
  padding: 9px 11px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 3px solid var(--rc);
  border-radius: 6px;
  cursor: pointer;
}
.role-card.on { background: color-mix(in srgb, var(--rc) 12%, var(--panel-2)); border-color: var(--rc); }
.role-head { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--paper); }
.role-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--rc); }
.role-desc { margin: 5px 0; font-size: 11.5px; line-height: 1.55; color: var(--muted); }
.role-tools { display: flex; flex-wrap: wrap; gap: 5px; margin: 6px 0 0; }
.tchip { padding: 1px 7px; font-family: var(--font-mono); font-size: 10.5px; color: var(--data); background: color-mix(in srgb, var(--data) 12%, transparent); border: 1px solid color-mix(in srgb, var(--data) 30%, transparent); border-radius: 3px; }
.tchip.muted { color: var(--muted); background: none; border-color: var(--line); }

.ctrl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.q-input { flex: 1 1 280px; min-width: 200px; }
.num { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.presets { display: flex; flex-wrap: wrap; gap: 8px; }
.preset-chip { display: inline-flex; align-items: center; gap: 8px; padding: 5px 12px; font-size: 12px; color: var(--paper); background: var(--panel-2); border: 1px solid var(--line); border-radius: 9999px; cursor: pointer; }
.preset-chip:hover { border-color: var(--signal); }
.preset-chip.active { border-color: var(--signal); background: color-mix(in srgb, var(--signal) 14%, var(--panel-2)); }
.chip-hint { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.timeline-panel { flex-shrink: 0; }
.col { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--line); border-left: 3px solid var(--rc); border-radius: 8px; }
.col-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.col-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--rc); }
.col-id { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.col-tools { font-family: var(--font-mono); font-size: 10px; color: var(--data); flex: 1 1 120px; }
.col-state { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.step-card { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; background: var(--ink); border: 1px solid var(--line); border-radius: 8px; }
.step-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.step-no { font-family: var(--font-mono); font-size: 11px; font-weight: 600; letter-spacing: 0.08em; color: var(--paper); }

.node { display: grid; grid-template-columns: 84px 1fr; gap: 4px 10px; padding: 8px 10px; border-left: 3px solid var(--line); border-radius: 0 5px 5px 0; background: var(--ink); }
.node-tag { font-family: var(--font-mono); font-size: 10.5px; font-weight: 600; letter-spacing: 0.04em; padding-top: 1px; }
.node-body { margin: 0; font-size: 12.5px; line-height: 1.62; color: var(--paper); white-space: pre-wrap; word-break: break-word; }
.node-body.mono { font-family: var(--font-mono); font-size: 11.5px; color: var(--muted); }
.node-foot { grid-column: 2; margin: 0; display: flex; gap: 10px; flex-wrap: wrap; font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.node.thought { border-left-color: var(--c-thought); }
.node.thought .node-tag { color: var(--c-thought); }
.node.action { border-left-color: var(--c-action); }
.node.action .node-tag { color: var(--c-action); }
.node.observation { border-left-color: var(--c-observation); }
.node.observation .node-tag { color: var(--c-observation); }
.node.observation.bad { border-left-color: #ff8fa3; }
.node.observation.bad .node-tag { color: #ff8fa3; }
.node.observation.waiting { opacity: 0.6; }
.node.pending { border-left-color: var(--data); }
.node.pending .node-tag { color: var(--data); }
.ok-mark { color: var(--c-observation); }
.bad-mark { color: #ff8fa3; }
.trunc-mark { color: var(--signal); }
.col-answer { margin: 0; padding: 7px 10px; font-size: 12.5px; line-height: 1.6; color: var(--paper); background: color-mix(in srgb, var(--rc) 12%, var(--ink)); border-radius: 5px; }
.col-answer.muted { color: var(--muted); font-style: italic; }
.ans-label { font-family: var(--font-mono); font-size: 10px; color: var(--rc); margin-right: 8px; }
.col-ms { float: right; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.answer-wrap { display: grid; grid-template-columns: 1fr; gap: 12px; }
.answer-box { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--data); border-radius: 0 8px 8px 0; flex-shrink: 0; }
.ans-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.ans-text { margin: 0; font-size: 13px; line-height: 1.72; color: var(--paper); white-space: pre-wrap; word-break: break-word; }
.ans-foot { margin: 0; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.ans-foot b { color: var(--data); font-family: var(--font-mono); }
.caret-done { display: inline-block; width: 0.5ch; height: 1.05em; margin-left: 1px; vertical-align: text-bottom; background: var(--data); border-radius: 50%; }
</style>
