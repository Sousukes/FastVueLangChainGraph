<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { TEAM_PRESETS, useTeam, type AgentColumn } from '../composables/useTeam'

defineProps<{
  stage: number
  title: string
}>()

const {
  roles,
  kinds,
  question,
  parallel,
  maxTasks,
  observationLimit,
  running,
  columns,
  rolesSeen,
  plan,
  layers,
  phase,
  phaseLabel,
  writeText,
  answer,
  verdict,
  issues,
  model,
  totalMs,
  llmMs,
  planMs,
  reviewMs,
  writeMs,
  wallMs,
  error,
  finished,
  busy,
  single,
  singleRunning,
  showCompare,
  runCompare,
  speedRatio,
  shadowRoles,
  fmtArgs,
  fmtChars,
  loadRoles,
  run,
  resetRun,
} = useTeam()

onMounted(loadRoles)

const showRoles = ref(false)

/** 角色名 → 标签 */
function roleLabel(name: string): string {
  return roles.value.find((r) => r.name === name)?.label ?? name
}
/** 任务编号 → 颜色（同一角色用同色，便于分列） */
const roleColors: Record<string, string> = {
  planner: '#9d8cff',
  researcher: '#5bc8b0',
  analyst: '#6fa8ff',
  critic: '#ffb454',
  writer: '#ff8fa3',
}
function colorOf(role: string): string {
  return roleColors[role] ?? '#8b95a7'
}

/** 该角色「时间线卡片」（= 工具轮）各自 LLM 耗时之和。 */
function toolMsOf(c: AgentColumn): number {
  return c.steps.reduce((n, s) => n + s.llmMs, 0)
}

/**
 * 该角色「收尾作答」那一轮的 LLM 耗时 = 角色总 LLM − 工具轮之和。
 *
 * ⚠️ worker 跑的就是 `react.py` 那套循环，而它**只把「有工具调用」的轮次写进 trace**
 *   （`steps.append(step_record)` 在 660 行；「模型直接作答」那轮在 589 行就 `return`）。
 *   所以每个角色的卡片数**天然比它的 `steps` 少一张**、分步求和也**必然小于**总 LLM。
 *   差额单列出来，栏里的读数才自洽。
 */
function closingMsOf(c: AgentColumn): number {
  return Math.max(0, c.llmMs - toolMsOf(c))
}
const verdictText = computed(() => {
  if (verdict.value === 'pass') return { label: '证据充分 · 放行', cls: 'pass' }
  if (verdict.value === 'revise') return { label: '证据不足 · 需修订', cls: 'revise' }
  if (verdict.value === 'unknown') return { label: '评审未决', cls: 'unknown' }
  return null
})
</script>

<template>
  <section class="team-console" aria-label="多智能体控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 10 的 ReAct 是「一个全才」：一个智能体、一套工具、一个循环。本阶段把它拆成
      <b>几个专职角色</b>——规划员把问题拆成可并行的子任务，研究员只能检索、分析员只能计算、
      评审员只查证据、主笔汇总。**多智能体并不比单智能体"更聪明"，它只是更可控、更可观测，
      代价是耗时按角色数线性增长。** 所以本页右侧永远放着一个<b>单智能体对照</b>：
      同一个问题、同一个模型，把"多付的时间"和"买到的东西"摆在一起看。
    </p>

    <!-- 状态条 -->
    <div class="stats-bar">
      <div class="stat">
        <span class="stat-num">{{ columns.length }}<i class="of">个角色</i></span>
        <span class="stat-label">已派出</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ layers.length }}<i class="of">层</i></span>
        <span class="stat-label">拓扑分层（同层并行）</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ (wallMs / 1000).toFixed(1) }}<i class="of">s</i></span>
        <span class="stat-label">总耗时 · LLM {{ (llmMs / 1000).toFixed(1) }}s</span>
      </div>
      <div class="stat" :class="{ warn: shadowRoles.length > 0 }">
        <span class="stat-num">{{ roles.length }}</span>
        <span class="stat-label">角色工具面 {{ shadowRoles.length ? '· 有重复!' : '· 互异' }}</span>
      </div>
      <div v-if="model" class="stat wide">
        <span class="stat-types">{{ model }}</span>
        <span class="stat-label">模型 · 并行 {{ parallel ? '开' : '关' }}</span>
      </div>
    </div>

    <!-- ① 角色工具面 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">① 角色工具面（分工成不成立，看这里）</span>
        <button type="button" class="link-btn" @click="showRoles = !showRoles">
          {{ showRoles ? '收起' : '展开角色清单' }}
        </button>
      </div>

      <div class="roles">
        <div v-for="r in roles" :key="r.name" class="role-card" :style="{ '--rc': colorOf(r.name) }">
          <header class="role-head">
            <span class="role-dot"></span>
            <b>{{ r.label }}</b>
            <span class="role-kinds">{{ r.kinds.join(' / ') || '无' }}</span>
          </header>
          <p class="role-tools">
            <template v-if="r.tools.length">
              <span v-for="t in r.tools" :key="t" class="tchip">{{ t }}</span>
            </template>
            <span v-else class="tchip muted">无工具</span>
          </p>
        </div>
      </div>

      <p v-if="shadowRoles.length" class="warn note">
        <span class="warn-mark">!</span>
        有角色的工具面<b>完全相同</b>（{{ shadowRoles.join(' / ') }}）——这是"伪多智能体"：
        它们其实是同一个智能体换了两个名字，分工没有落地。正常情况每个角色的
        <code>tools</code> 应当互不相同。
      </p>

      <p v-if="!roles.length && !busy" class="hint-line">
        点「运行」后会拉起角色目录。这里能提前看到：研究员只能检索、分析员只能计算、评审员没有工具——
        <b>工具面不同，分工才真实生效</b>。
      </p>
    </div>

    <!-- ② 提问与调度 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">② 提问与调度（并行开关是核心实验）</span>
        <span class="meta">同一个问题，关掉并行耗时立刻数倍</span>
      </div>

      <div class="presets">
        <button
          v-for="p in TEAM_PRESETS"
          :key="p.label"
          type="button"
          class="preset-chip"
          :class="{ active: question === p.question }"
          :title="p.question"
          @click="question = p.question"
        >
          {{ p.label }}
          <span class="chip-hint">{{ p.hint }}</span>
        </button>
      </div>

      <div class="ctrl-row">
        <el-input
          v-model="question"
          size="small"
          placeholder="问一个能拆成几个子任务的问题"
          class="q-input"
          :disabled="running"
          @keyup.enter="run"
        />
        <el-button size="small" type="primary" :loading="running" :disabled="!question.trim()" @click="run">
          {{ running ? '运行中…' : '运行多智能体' }}
        </el-button>
        <el-button size="small" :disabled="running" @click="runCompare">
          {{ singleRunning ? '单智能体对照中…' : '并排对照单智能体' }}
        </el-button>
        <el-button size="small" :disabled="running" @click="resetRun">清空</el-button>
      </div>

      <div class="ctrl-row">
        <label class="toggle">
          <input type="checkbox" v-model="parallel" :disabled="running" />
          并行执行（关掉 → 所有任务串行）
        </label>
        <label class="num">
          最多子任务
          <el-input-number v-model="maxTasks" size="small" :min="1" :max="6" controls-position="right" />
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

    <!-- ③ 规划 -->
    <div v-if="plan.length" class="panel">
      <div class="panel-head">
        <span class="field-label">③ 规划员拆解（JSON · 拓扑分层）</span>
        <span class="meta">规划耗时 {{ (planMs / 1000).toFixed(2) }}s</span>
      </div>
      <div class="layers">
        <div v-for="(layer, i) in layers" :key="i" class="layer">
          <span class="layer-no">层 {{ i + 1 }} · {{ layer.length > 1 ? '并行' : '串行' }}</span>
          <span v-for="tid in layer" :key="tid" class="layer-task">{{ tid }}</span>
        </div>
      </div>
      <div class="tasks">
        <div v-for="t in plan" :key="t.id" class="task">
          <b>{{ t.id }}</b>
          <span class="task-kind">{{ t.kind }}</span>
          <span class="task-desc">{{ t.description }}</span>
          <span v-if="t.dependsOn.length" class="task-dep">依赖 {{ t.dependsOn.join(',') }}</span>
        </div>
      </div>
    </div>

    <!-- ④ 分角色时间线 -->
    <div class="panel timeline-panel">
      <div class="panel-head">
        <span class="field-label">④ 分角色时间线（每个角色一栏）</span>
        <span v-if="phase" class="meta">{{ phaseLabel }}</span>
      </div>

      <p v-if="!columns.length && !running" class="hint-line">
        运行后，规划出的每个子任务会在自己的栏里跑 Thought → Action → Observation。
        <b>并行时各栏交替刷新</b>——这就是多智能体相对单智能体的视觉区别。
      </p>

      <div v-for="c in columns" :key="c.id" class="col" :style="{ '--rc': colorOf(c.role) }">
        <header class="col-head">
          <span class="col-dot"></span>
          <b>{{ c.label }}</b>
          <span class="col-id">{{ c.id }} · {{ c.kind }}</span>
          <span v-if="c.tools.length" class="col-tools">{{ c.tools.join(' · ') }}</span>
          <span class="col-state">{{ c.ended ? '✓' : c.started ? '运行中' : '待命' }}</span>
        </header>

        <article v-for="s in c.steps" :key="s.step" class="step-card">
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
              <span v-if="a.repeat > 0" class="repeat-flag">第 {{ a.repeat + 1 }} 次同参调用</span>
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

        <div v-if="c.live" class="node pending">
          <span class="node-tag">?</span>
          <p class="node-body">{{ c.live }}<span class="caret"></span></p>
        </div>

        <p v-if="c.answer !== null" class="col-answer">
          <span class="ans-label">结论</span>{{ c.answer }}
          <span class="col-ms">
            {{ (c.totalMs / 1000).toFixed(2) }}s ·
            LLM {{ (c.llmMs / 1000).toFixed(2) }}s
            （工具轮 {{ c.steps.length }} 卡 {{ (toolMsOf(c) / 1000).toFixed(2) }}s
            + 收尾作答 {{ (closingMsOf(c) / 1000).toFixed(2) }}s）
          </span>
        </p>
        <p v-else-if="c.ended" class="col-answer muted">（该角色未给出结论）</p>
      </div>
    </div>

    <!-- ⑤ 评审 -->
    <div v-if="verdict || issues.length" class="panel review-panel" :class="verdictText?.cls">
      <div class="panel-head">
        <span class="field-label">⑤ 评审员核查证据</span>
        <span v-if="verdictText" class="reason-tag" :class="verdictText.cls">{{ verdictText.label }}</span>
        <span class="meta">耗时 {{ (reviewMs / 1000).toFixed(2) }}s</span>
      </div>
      <ul v-if="issues.length" class="issues">
        <li v-for="(it, i) in issues" :key="i">
          <b>{{ it.taskId }}</b>：{{ it.problem }}
        </li>
      </ul>
      <p v-else class="hint-line">证据充分，无问题。</p>
    </div>

    <!-- ⑥ 汇总 + 对照 -->
    <div v-if="answer !== null || writeText" class="answer-wrap">
      <div class="answer-box">
        <div class="ans-head">
          <span class="field-label">⑥ 主笔汇总（流式）</span>
          <span class="meta">写作用时 {{ (writeMs / 1000).toFixed(2) }}s</span>
        </div>
        <p class="ans-text">{{ writeText || answer }}<span class="caret-done"></span></p>
        <p class="ans-foot">
          <span class="meta">
            总耗时 <b>{{ (totalMs / 1000).toFixed(2) }}s</b>（规划 {{ (planMs / 1000).toFixed(2) }}s ·
            评审 {{ (reviewMs / 1000).toFixed(2) }}s · 汇总 {{ (writeMs / 1000).toFixed(2) }}s）·
            LLM 占 {{ (llmMs / 1000).toFixed(2) }}s
          </span>
        </p>
      </div>

      <div v-if="showCompare" class="compare-box">
        <div class="ans-head">
          <span class="field-label">单智能体对照（同问题同模型）</span>
          <span v-if="singleRunning" class="meta">运行中…</span>
        </div>
        <p v-if="single" class="ans-text single">
          <template v-if="single.error">{{ single.error }}</template>
          <template v-else>{{ single.answer }}</template>
        </p>
        <p v-else-if="!singleRunning" class="hint-line">点上方「并排对照单智能体」。</p>
        <p class="ans-foot">
          <span class="meta" v-if="single && !single.error">
            单智能体耗时 <b>{{ (single.totalMs / 1000).toFixed(2) }}s</b> ·
            <template v-if="speedRatio">
              多智能体是单智能体的 <b class="ratio">{{ speedRatio.toFixed(2) }}×</b>
            </template>
            <template v-else>（多智能体尚未完成）</template>
          </span>
        </p>
      </div>
    </div>

    <p v-if="finished && !columns.length && !answer" class="warn note">
      <span class="warn-mark">!</span>
      这次运行没有产生任何角色。多半是规划员没能输出合法任务列表，或链路在第一步就出错了。
    </p>
  </section>
</template>

<style scoped>
.team-console {
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
.stat.wide { flex: 1 1 200px; }
.stat-num { font-family: var(--font-display); font-size: 20px; font-weight: 700; color: var(--paper); }
.stat-num .of { font-size: 12px; font-style: normal; color: var(--muted); }
.stat.warn .stat-num { color: var(--signal); }
.stat-label { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.stat-types { font-family: var(--font-mono); font-size: 11px; color: var(--data); }

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

.panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  flex-shrink: 0;
}
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }

/* 角色卡 */
.roles { display: flex; flex-wrap: wrap; gap: 10px; }
.role-card {
  flex: 1 1 160px;
  padding: 9px 11px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 3px solid var(--rc);
  border-radius: 6px;
}
.role-head { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--paper); }
.role-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--rc); }
.role-kinds { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.role-tools { display: flex; flex-wrap: wrap; gap: 5px; margin: 6px 0 0; }
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

/* 控制件 */
.ctrl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.q-input { flex: 1 1 280px; min-width: 200px; }
.num { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
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
.chip-hint { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

/* 规划 */
.layers { display: flex; flex-direction: column; gap: 6px; }
.layer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.layer-no { font-family: var(--font-mono); font-size: 11px; color: var(--signal); }
.layer-task { font-family: var(--font-mono); font-size: 11px; color: var(--data); padding: 0 4px; border: 1px solid var(--line); border-radius: 3px; }
.tasks { display: flex; flex-direction: column; gap: 5px; }
.task { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.task-kind { font-family: var(--font-mono); font-size: 10px; color: var(--paper); background: var(--panel-2); padding: 0 5px; border-radius: 3px; }
.task-desc { color: var(--muted); flex: 1 1 200px; }
.task-dep { font-family: var(--font-mono); font-size: 10px; color: var(--signal); }

/* 分角色时间线 */
.timeline-panel { flex-shrink: 0; }
.col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 3px solid var(--rc);
  border-radius: 8px;
}
.col-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.col-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--rc); }
.col-id { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.col-tools { font-family: var(--font-mono); font-size: 10px; color: var(--data); flex: 1 1 120px; }
.col-state { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.step-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.step-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.step-no { font-family: var(--font-mono); font-size: 11px; font-weight: 600; letter-spacing: 0.08em; color: var(--paper); }

.node {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 4px 10px;
  padding: 8px 10px;
  border-left: 3px solid var(--line);
  border-radius: 0 5px 5px 0;
  background: var(--ink);
}
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
.repeat-flag { grid-column: 2; justify-self: start; padding: 1px 7px; font-family: var(--font-mono); font-size: 10px; color: var(--signal); background: color-mix(in srgb, var(--signal) 14%, transparent); border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent); border-radius: 3px; }
.ok-mark { color: var(--c-observation); }
.bad-mark { color: #ff8fa3; }
.trunc-mark { color: var(--signal); }
.col-answer { margin: 0; padding: 7px 10px; font-size: 12.5px; line-height: 1.6; color: var(--paper); background: color-mix(in srgb, var(--rc) 12%, var(--ink)); border-radius: 5px; }
.col-answer.muted { color: var(--muted); font-style: italic; }
.ans-label { font-family: var(--font-mono); font-size: 10px; color: var(--rc); margin-right: 8px; }
.col-ms { float: right; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

/* 评审 */
.review-panel { border-left: 3px solid var(--signal); }
.reason-tag { padding: 1px 8px; font-family: var(--font-mono); font-size: 10.5px; color: var(--ink); background: var(--signal); border-radius: 3px; }
.reason-tag.pass { background: var(--data); }
.reason-tag.revise { background: #ff8fa3; }
.reason-tag.unknown { background: var(--muted); }
.issues { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--paper); }
.issues b { font-family: var(--font-mono); color: var(--signal); }

/* 汇总 + 对照 */
.answer-wrap { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 860px) { .answer-wrap { grid-template-columns: 1fr; } }
.answer-box,
.compare-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--data);
  border-radius: 0 8px 8px 0;
  flex-shrink: 0;
}
.compare-box { border-left-color: var(--signal); }
.ans-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.ans-text { margin: 0; font-size: 13px; line-height: 1.72; color: var(--paper); white-space: pre-wrap; word-break: break-word; }
.ans-text.single { color: var(--muted); }
.ans-foot { margin: 0; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.ans-foot b { color: var(--data); font-family: var(--font-mono); }
.ans-foot .ratio { color: var(--signal); }
.caret-done { display: inline-block; width: 0.5ch; height: 1.05em; margin-left: 1px; vertical-align: text-bottom; background: var(--data); border-radius: 50%; }
</style>
