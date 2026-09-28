<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useToolRunner, PRESET_QUESTIONS, type ToolInfo } from '../composables/useToolRunner'

defineProps<{
  stage: number
  title: string
}>()

const {
  tools,
  question,
  loading,
  result,
  error,
  paramsOf,
  loadTools,
  run,
  reset,
} = useToolRunner()

onMounted(loadTools)

const openTool = ref<string | null>(null)
function toggleTool(name: string) {
  openTool.value = openTool.value === name ? null : name
}

function applyQuestion(q: string) {
  question.value = q
  result.value = null
}

function json(v: unknown) {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function toolName(t: ToolInfo) {
  return t.name
}
</script>

<template>
  <section class="tool-console" aria-label="函数调用控制台">
    <p class="principle">
      <span class="principle-mark">原理</span>
      模型<b>从不执行代码</b>。它只读我们给的「工具说明书」（name + description + JSON Schema），
      需要时产出 <code>tool_calls</code>（调用意图）。执行工具、校验参数、把结果与错误回喂，
      全部由后端的循环完成——右侧时间线就是这条链路的心跳。
    </p>

    <div class="cols">
      <!-- 左：工具清单 -->
      <aside class="col col-tools">
        <header class="col-head">
          <span class="field-label">可用工具（{{ tools.length }}）</span>
          <span class="meta">GET /api/tools</span>
        </header>
        <div class="tool-list">
          <article
            v-for="t in tools"
            :key="toolName(t)"
            class="tool-card"
            :class="{ 'is-open': openTool === toolName(t) }"
          >
            <button type="button" class="tool-head" @click="toggleTool(toolName(t))">
              <span class="tool-name">{{ t.name }}</span>
              <span class="tool-caret">{{ openTool === toolName(t) ? '−' : '+' }}</span>
            </button>
            <p class="tool-desc">{{ t.description }}</p>
            <ul v-if="openTool === toolName(t)" class="param-list">
              <li v-for="p in paramsOf(t)" :key="p.name" class="param-row">
                <span class="param-name">{{ p.name }}</span>
                <span class="param-type">{{ p.type }}</span>
                <span v-if="p.required" class="param-req">必填</span>
                <span v-else class="param-opt">可选</span>
                <span class="param-desc">{{ p.description }}</span>
              </li>
            </ul>
          </article>
          <p v-if="!tools.length" class="empty">工具清单加载中…</p>
        </div>
      </aside>

      <!-- 中：提问 -->
      <div class="col col-ask">
        <header class="col-head">
          <span class="field-label">提问</span>
          <span class="meta">POST /api/tools/run</span>
        </header>

        <div class="presets">
          <button
            v-for="p in PRESET_QUESTIONS"
            :key="p.label"
            type="button"
            class="preset-chip"
            :title="p.question"
            @click="applyQuestion(p.question)"
          >
            {{ p.label }}
            <span class="chip-hint">{{ p.hint }}</span>
          </button>
        </div>

        <el-input
          v-model="question"
          type="textarea"
          :rows="6"
          resize="none"
          placeholder="例如：北京现在多少度？如果超过 28 度，帮我换算成华氏度"
        />

        <div class="run-row">
          <el-button type="primary" :loading="loading" :disabled="!question.trim()" @click="run">
            {{ loading ? '调用中' : '运行' }}
          </el-button>
          <el-button text @click="reset">清空</el-button>
          <span v-if="result" class="meta">
            {{ result.trace.length }} 次工具调用 · {{ result.steps }} 轮 · model: {{ result.model }}
          </span>
        </div>

        <div v-if="error" class="console-error" role="alert">
          <span class="err-mark">!</span><span>{{ error }}</span>
        </div>

        <!-- 最终回答 -->
        <div v-if="result" class="answer" :class="{ 'is-exhausted': result.exhausted }">
          <span class="field-label">最终回答</span>
          <p v-if="result.answer" class="answer-text">
            {{ result.answer }}<span class="caret-done" aria-hidden="true" />
          </p>
          <p v-else class="answer-warn">
            ⚠ 达到 max_steps 上限仍未收敛（模型一直在调工具）。这本身就是个重要信号：
            要么工具描述不清，要么该把上限调高。
          </p>
        </div>
      </div>

      <!-- 右：trace 时间线 -->
      <div class="col col-trace">
        <header class="col-head">
          <span class="field-label">调用时间线</span>
          <span v-if="result" class="meta">{{ result.trace.length }} steps</span>
        </header>

        <div v-if="!result" class="trace-empty">
          运行一次提问，这里会逐步展开：模型要调哪个工具、参数是什么、工具返回了什么。
        </div>
        <ol v-else class="trace">
          <li v-for="(s, i) in result.trace" :key="i" class="trace-item" :class="{ 'is-error': !!s.error }">
            <header class="trace-head">
              <span class="trace-step">STEP {{ s.step }}</span>
              <span class="trace-tool">{{ s.tool }}</span>
              <span class="trace-ms">{{ s.ms }}ms</span>
            </header>
            <div class="trace-block">
              <span class="trace-key">arguments</span>
              <pre class="trace-pre">{{ json(s.arguments) }}</pre>
            </div>
            <div class="trace-block">
              <span class="trace-key" :class="{ err: !!s.error }">
                {{ s.error ? 'error' : 'result' }}
              </span>
              <pre class="trace-pre" :class="{ err: !!s.error }">{{
                s.error ? s.error : json(s.result)
              }}</pre>
            </div>
          </li>
        </ol>
      </div>
    </div>
  </section>
</template>

<style scoped>
.tool-console {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  min-height: 0;
}

.principle {
  margin: 0;
  padding: 12px 14px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--muted);
  background: var(--panel-2);
  border-left: 3px solid var(--data);
  border-radius: 0 6px 6px 0;
}
.principle b { color: var(--paper); }
.principle code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--signal);
}

.cols {
  display: grid;
  grid-template-columns: 0.95fr 1.1fr 1.15fr;
  gap: 14px;
  flex: 1 1 auto;
  min-height: 0;
}
.col { display: flex; flex-direction: column; gap: 10px; min-height: 0; }
.col-head { display: flex; align-items: baseline; justify-content: space-between; }

.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.meta { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }

/* 工具清单 */
.tool-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tool-card {
  padding: 10px 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.tool-card.is-open { border-color: color-mix(in srgb, var(--data) 55%, var(--line)); }
.tool-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 0;
  background: transparent;
  border: 0;
  cursor: pointer;
}
.tool-name { font-family: var(--font-mono); font-size: 13px; color: var(--data); }
.tool-caret { font-family: var(--font-mono); color: var(--muted); }
.tool-desc { margin: 6px 0 0; font-size: 12px; line-height: 1.6; color: var(--muted); }

.param-list { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.param-row {
  display: grid;
  grid-template-columns: auto auto auto 1fr;
  gap: 6px;
  align-items: baseline;
  font-size: 11px;
}
.param-name { font-family: var(--font-mono); color: var(--signal); }
.param-type { font-family: var(--font-mono); color: var(--muted); }
.param-req { color: var(--data); font-size: 10px; }
.param-opt { color: var(--muted); font-size: 10px; }
.param-desc { color: var(--muted); grid-column: 1 / -1; line-height: 1.5; }
.empty { margin: auto; font-size: 12px; color: var(--muted); }

/* 提问 */
.presets { display: flex; flex-wrap: wrap; gap: 8px; }
.preset-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  font-size: 12px;
  color: var(--paper);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 9999px;
  cursor: pointer;
}
.preset-chip:hover { border-color: var(--data); }
.chip-hint { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.run-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

.answer {
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--data);
  border-radius: 0 8px 8px 0;
}
.answer.is-exhausted { border-left-color: var(--signal); }
.answer-text {
  margin: 8px 0 0;
  font-size: 14px;
  line-height: 1.75;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
}
.answer-warn { margin: 8px 0 0; font-size: 12.5px; line-height: 1.7; color: var(--signal); }
.caret-done {
  display: inline-block;
  width: 0.5ch;
  height: 1.05em;
  margin-left: 1px;
  vertical-align: text-bottom;
  background: var(--data);
  border-radius: 50%;
}

/* trace */
.trace-empty,
.trace {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.trace-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--muted);
  text-align: center;
  padding: 24px;
  line-height: 1.7;
}
.trace { list-style: none; margin: 0; display: flex; flex-direction: column; gap: 14px; }
.trace-item {
  position: relative;
  padding-left: 16px;
  border-left: 2px solid color-mix(in srgb, var(--data) 55%, transparent);
}
.trace-item.is-error { border-left-color: color-mix(in srgb, var(--signal) 70%, transparent); }
.trace-item::before {
  content: '';
  position: absolute;
  left: -6px;
  top: 4px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--data);
}
.trace-item.is-error::before { background: var(--signal); }
.trace-head { display: flex; align-items: baseline; gap: 10px; }
.trace-step {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--muted);
}
.trace-tool { font-family: var(--font-mono); font-size: 13px; color: var(--paper); }
.trace-ms { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.trace-block { margin-top: 6px; }
.trace-key {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.trace-key.err { color: var(--signal); }
.trace-pre {
  margin: 4px 0 0;
  padding: 8px 10px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--data);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
}
.trace-pre.err { color: var(--signal); border-color: color-mix(in srgb, var(--signal) 45%, var(--line)); }

.console-error {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid var(--signal);
  border-radius: 6px;
  padding: 12px;
}
.err-mark {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  border-radius: 50%;
  background: var(--signal);
  color: var(--ink);
  font-weight: 700;
  font-size: 12px;
}

@media (max-width: 1100px) {
  .cols { grid-template-columns: 1fr; }
}
</style>
