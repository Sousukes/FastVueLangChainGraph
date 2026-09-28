<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useMcp, PRESET_QUESTIONS, type MCPToolInfo } from '../composables/useMcp'

defineProps<{
  stage: number
  title: string
}>()

const {
  catalog,
  question,
  loading,
  result,
  error,
  resourcePreview,
  promptPreview,
  loadCatalog,
  run,
  readResource,
  getPrompt,
  applyQuestion,
  reset,
} = useMcp()

onMounted(loadCatalog)

const openTool = ref<string | null>(null)
const showSchema = ref(false)

function toggleTool(name: string) {
  openTool.value = openTool.value === name ? null : name
}

function json(v: unknown, indent = 2) {
  try {
    return JSON.stringify(v, null, indent)
  } catch {
    return String(v)
  }
}

/** 从报文里抽一个"给人看的"标签 */
function label(entry: { dir: string; payload: Record<string, any> }) {
  const p = entry.payload ?? {}
  if (entry.dir === '→') return p.method ?? '(request)'
  if (p.error) return `error ${p.error.code}`
  return p.result !== undefined ? 'result' : '(response)'
}

function toolParams(t: MCPToolInfo) {
  const props = t.inputSchema?.properties ?? {}
  const required = new Set(t.inputSchema?.required ?? [])
  return Object.entries(props).map(([name, schema]) => ({
    name,
    type: schema?.type ?? 'any',
    required: required.has(name),
    enum: schema?.enum,
    description: schema?.description ?? '',
  }))
}
</script>

<template>
  <section class="mcp-console" aria-label="MCP 协议控制台">
    <p class="principle">
      <span class="principle-mark">原理</span>
      MCP Server 是<b>独立进程</b>，只认 JSON-RPC 2.0：我们写一行
      <code>{"method":"tools/call"}</code> 进它的 stdin，它写一行 result 回来。
      它<b>完全不认识 LLM</b>——正因如此，同一个 server 能被 Claude Desktop、Cursor 和本应用共用。
      右栏就是这条链路的原始报文。
    </p>

    <div class="cols">
      <!-- 左：Server 目录（能力视角） -->
      <aside class="col col-server">
        <header class="col-head">
          <span class="field-label">MCP Server（{{ catalog.length }}）</span>
          <span class="meta">GET /api/mcp/servers</span>
        </header>

        <div class="server-list">
          <article v-for="s in catalog" :key="s.name" class="server-card">
            <header class="server-head">
              <span class="server-name">{{ s.name }}</span>
              <span class="badge-ok">connected</span>
            </header>
            <p class="server-meta">
              <span class="mono">{{ s.protocolVersion }}</span>
              · {{ (s.serverInfo as any)?.name }} v{{ (s.serverInfo as any)?.version }}
            </p>
            <div class="caps">
              <span v-for="(_, cap) in s.capabilities" :key="cap" class="cap">{{ cap }}</span>
            </div>

            <!-- Tools -->
            <section class="group">
              <h4 class="group-title">Tools（{{ s.tools.length }}）</h4>
              <article v-for="t in s.tools" :key="t.name" class="tool-card">
                <button type="button" class="tool-head" @click="toggleTool(t.name)">
                  <span class="tool-name">{{ t.name }}</span>
                  <span class="tool-caret">{{ openTool === t.name ? '−' : '+' }}</span>
                </button>
                <p class="tool-desc">{{ t.description }}</p>
                <ul v-if="openTool === t.name" class="param-list">
                  <li v-for="p in toolParams(t)" :key="p.name" class="param-row">
                    <span class="param-name">{{ p.name }}</span>
                    <span class="param-type">{{ p.type }}</span>
                    <span :class="p.required ? 'param-req' : 'param-opt'">
                      {{ p.required ? '必填' : '可选' }}
                    </span>
                    <span v-if="p.enum" class="param-enum">enum: {{ (p.enum as string[]).join('/') }}</span>
                    <span class="param-desc">{{ p.description }}</span>
                  </li>
                </ul>
              </article>
            </section>

            <!-- Resources -->
            <section class="group">
              <h4 class="group-title">Resources（{{ s.resources.length }}）</h4>
              <button
                v-for="r in s.resources"
                :key="r.uri"
                type="button"
                class="res-row"
                @click="readResource(r.uri)"
              >
                <span class="res-uri">{{ r.uri }}</span>
                <span class="res-name">{{ r.name }}</span>
              </button>
            </section>

            <!-- Prompts -->
            <section class="group">
              <h4 class="group-title">Prompts（{{ s.prompts.length }}）</h4>
              <button
                v-for="p in s.prompts"
                :key="p.name"
                type="button"
                class="res-row"
                @click="getPrompt(p.name)"
              >
                <span class="res-uri">{{ p.name }}</span>
                <span class="res-name">{{ p.description }}</span>
              </button>
            </section>
          </article>
          <p v-if="!catalog.length" class="empty">正在连接 MCP Server…</p>
        </div>

        <!-- 资源 / 模板预览 -->
        <div v-if="resourcePreview || promptPreview" class="preview">
          <header class="preview-head">
            <span class="mono">{{ resourcePreview?.uri ?? promptPreview?.name }}</span>
            <button type="button" class="preview-close" @click="resourcePreview = null; promptPreview = null">
              ×
            </button>
          </header>
          <pre class="preview-body">{{ resourcePreview?.text ?? promptPreview?.text }}</pre>
        </div>
      </aside>

      <!-- 中：提问与回答（模型视角） -->
      <div class="col col-ask">
        <header class="col-head">
          <span class="field-label">提问</span>
          <span class="meta">POST /api/mcp/run</span>
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
          :rows="4"
          resize="none"
          placeholder="例如：课程笔记里关于 MCP 和 RAG 分别讲了什么？"
        />

        <div class="run-row">
          <el-button type="primary" :loading="loading" :disabled="!question.trim()" @click="run">
            {{ loading ? '调用中' : '运行' }}
          </el-button>
          <el-button text @click="reset">清空</el-button>
          <span v-if="result" class="meta">
            {{ result.trace.length }} 次工具调用 · {{ result.steps }} 轮
          </span>
        </div>

        <div v-if="error" class="console-error" role="alert">
          <span class="err-mark">!</span><span>{{ error }}</span>
        </div>

        <div v-if="result" class="answer" :class="{ 'is-exhausted': result.exhausted }">
          <span class="field-label">最终回答</span>
          <p v-if="result.answer" class="answer-text">
            {{ result.answer }}<span class="caret-done" aria-hidden="true" />
          </p>
          <p v-else class="answer-warn">⚠ 达到 max_steps 上限仍未收敛。</p>
        </div>

        <!-- trace：模型视角的调用链 -->
        <div v-if="result" class="trace-wrap">
          <span class="field-label">工具调用链（模型视角）</span>
          <ol class="trace">
            <li
              v-for="(s, i) in result.trace"
              :key="i"
              class="trace-item"
              :class="{ 'is-error': !!s.error }"
            >
              <header class="trace-head">
                <span class="trace-step">STEP {{ s.step }}</span>
                <span class="trace-tool">{{ s.tool }}</span>
                <span class="trace-ms">{{ s.ms }}ms</span>
              </header>
              <pre class="trace-pre">{{ json(s.arguments, 0) }}</pre>
              <pre class="trace-pre" :class="{ err: !!s.error }">{{
                s.error ? s.error : json(s.result, 0)
              }}</pre>
            </li>
          </ol>
        </div>
      </div>

      <!-- 右：JSON-RPC 报文（协议视角） -->
      <div class="col col-protocol">
        <header class="col-head">
          <span class="field-label">JSON-RPC 报文</span>
          <span v-if="result" class="meta">{{ result.protocol.length }} 条</span>
        </header>

        <div v-if="!result" class="protocol-empty">
          运行一次提问，这里会逐条列出我们与 MCP Server 之间的原始报文：
          <span class="mono">→</span> 是发出，<span class="mono">←</span> 是回应。
        </div>
        <ol v-else class="protocol">
          <li
            v-for="(entry, i) in result.protocol"
            :key="i"
            class="frame"
            :class="entry.dir === '→' ? 'is-out' : 'is-in'"
          >
            <header class="frame-head">
              <span class="frame-dir">{{ entry.dir }}</span>
              <span class="frame-label">{{ label(entry) }}</span>
              <span class="frame-id">id={{ entry.payload?.id ?? '—' }}</span>
            </header>
            <pre class="frame-body">{{ json(entry.payload, 1) }}</pre>
          </li>
        </ol>
      </div>
    </div>
  </section>
</template>

<style scoped>
.mcp-console {
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
  grid-template-columns: 1fr 1.05fr 1fr;
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
.mono { font-family: var(--font-mono); }

/* 左栏 */
.server-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.server-card {
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.server-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.server-name { font-family: var(--font-mono); font-size: 13px; color: var(--data); }
.badge-ok {
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 2px 8px;
  color: var(--ink);
  background: var(--data);
  border-radius: 9999px;
}
.server-meta { margin: 6px 0 0; font-size: 11px; color: var(--muted); }
.caps { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.cap {
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 2px 8px;
  color: var(--signal);
  border: 1px solid color-mix(in srgb, var(--signal) 45%, transparent);
  border-radius: 9999px;
}

.group { margin-top: 12px; }
.group-title {
  margin: 0 0 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.tool-card {
  padding: 8px 10px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.tool-card + .tool-card { margin-top: 6px; }
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
.tool-name { font-family: var(--font-mono); font-size: 12px; color: var(--paper); }
.tool-caret { font-family: var(--font-mono); color: var(--muted); }
.tool-desc { margin: 4px 0 0; font-size: 11px; line-height: 1.55; color: var(--muted); }
.param-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.param-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline; font-size: 10.5px; }
.param-name { font-family: var(--font-mono); color: var(--signal); }
.param-type { font-family: var(--font-mono); color: var(--muted); }
.param-req { color: var(--data); }
.param-opt { color: var(--muted); }
.param-enum { font-family: var(--font-mono); color: var(--data); }
.param-desc { flex: 1 1 100%; color: var(--muted); line-height: 1.5; }

.res-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 5px 8px;
  font-size: 11px;
  text-align: left;
  background: transparent;
  border: 1px dashed var(--line);
  border-radius: 6px;
  cursor: pointer;
}
.res-row + .res-row { margin-top: 5px; }
.res-row:hover { border-color: var(--data); }
.res-uri { font-family: var(--font-mono); color: var(--data); }
.res-name { color: var(--muted); }
.empty { margin: auto; font-size: 12px; color: var(--muted); }

.preview {
  max-height: 34%;
  display: flex;
  flex-direction: column;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  font-size: 11px;
  color: var(--data);
  background: var(--panel-2);
  border-bottom: 1px solid var(--line);
}
.preview-close {
  background: transparent;
  border: 0;
  color: var(--muted);
  font-size: 15px;
  cursor: pointer;
}
.preview-body {
  margin: 0;
  padding: 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-y: auto;
}

/* 中栏 */
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
.answer-warn { margin: 8px 0 0; font-size: 12.5px; color: var(--signal); }
.caret-done {
  display: inline-block;
  width: 0.5ch;
  height: 1.05em;
  margin-left: 1px;
  vertical-align: text-bottom;
  background: var(--data);
  border-radius: 50%;
}

.trace-wrap { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 6px; }
.trace {
  list-style: none;
  margin: 0;
  padding: 8px 10px;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.trace-item {
  position: relative;
  padding-left: 14px;
  border-left: 2px solid color-mix(in srgb, var(--data) 55%, transparent);
}
.trace-item.is-error { border-left-color: color-mix(in srgb, var(--signal) 70%, transparent); }
.trace-item::before {
  content: '';
  position: absolute;
  left: -6px;
  top: 3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--data);
}
.trace-item.is-error::before { background: var(--signal); }
.trace-head { display: flex; align-items: baseline; gap: 8px; }
.trace-step { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.trace-tool { font-family: var(--font-mono); font-size: 12px; color: var(--paper); }
.trace-ms { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.trace-pre {
  margin: 4px 0 0;
  padding: 6px 8px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.55;
  color: var(--data);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 5px;
  white-space: pre-wrap;
  word-break: break-word;
}
.trace-pre.err { color: var(--signal); }

/* 右栏：报文 */
.protocol,
.protocol-empty {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.protocol-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.8;
  color: var(--muted);
  text-align: center;
}
.protocol { list-style: none; margin: 0; display: flex; flex-direction: column; gap: 8px; }
.frame {
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--ink);
}
.frame.is-out { border-left: 3px solid var(--signal); }
.frame.is-in { border-left: 3px solid var(--data); }
.frame-head { display: flex; align-items: baseline; gap: 8px; }
.frame-dir { font-family: var(--font-mono); font-size: 13px; }
.frame.is-out .frame-dir { color: var(--signal); }
.frame.is-in .frame-dir { color: var(--data); }
.frame-label { font-family: var(--font-mono); font-size: 11px; color: var(--paper); }
.frame-id { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.frame-body {
  margin: 6px 0 0;
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1.55;
  color: var(--muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 160px;
  overflow-y: auto;
}

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

@media (max-width: 1200px) {
  .cols { grid-template-columns: 1fr; }
}
</style>
