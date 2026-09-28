<script setup lang="ts">
import { useExtractor, PRESETS } from '../composables/useExtractor'

defineProps<{
  stage: number
  title: string
}>()

const {
  text,
  fields,
  loading,
  result,
  error,
  canRun,
  applyPreset,
  addField,
  removeField,
  setEnumText,
  run,
} = useExtractor()

function pretty(v: unknown) {
  if (v === null || v === undefined) return '—'
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
</script>

<template>
  <section class="extractor" aria-label="结构化抽取实验台">
    <p class="principle">
      <span class="principle-mark">原理</span>
      <code>response_format=&lcub;&quot;type&quot;: &quot;json_object&quot;&rcub;</code>
      只能让模型倾向输出 JSON，<b>不能</b>保证字段名、类型、枚举值符合 schema。
      后端用 Pydantic 校验 + 自纠重试，校验失败会把原因喂回模型让它改一次。
    </p>

    <!-- 预设 -->
    <div class="presets">
      <button
        v-for="p in PRESETS"
        :key="p.name"
        type="button"
        class="preset-chip"
        @click="applyPreset(p)"
      >
        {{ p.name }} 预设
      </button>
      <button type="button" class="preset-chip ghost" @click="addField">＋ 加字段</button>
    </div>

    <div class="cols">
      <!-- 左：待抽取文本 -->
      <div class="col">
        <label class="field">
          <span class="field-label">待抽取文本</span>
          <el-input
            v-model="text"
            type="textarea"
            :rows="14"
            resize="none"
            placeholder="粘贴一段文本（新闻 / 简历 / 产品介绍…），点击上方预设快速开始"
            class="mono"
          />
        </label>
      </div>

      <!-- 中：字段定义 + 抽取 -->
      <div class="col">
        <header class="col-head">
          <span class="field-label">字段定义（{{ fields.length }}）</span>
          <span class="meta">POST /api/extract</span>
        </header>

        <div class="field-list">
          <article v-for="f in fields" :key="f.id" class="field-row">
            <el-input v-model="f.name" size="small" placeholder="字段名 (snake_case)" />
            <el-select v-model="f.type" size="small" class="type-select">
              <el-option label="string" value="string" />
              <el-option label="int" value="int" />
              <el-option label="number" value="number" />
              <el-option label="bool" value="bool" />
              <el-option label="array" value="array" />
            </el-select>
            <el-input
              v-model="f.description"
              size="small"
              placeholder="描述（会喂给模型）"
              class="desc-input"
            />
            <label class="req-toggle">
              <el-checkbox v-model="f.required" size="small">必填</el-checkbox>
            </label>
            <el-input
              v-if="f.type === 'string'"
              :model-value="f.enum.join(',')"
              size="small"
              placeholder="enum: a,b,c"
              class="enum-input"
              @update:model-value="(v: string | number) => setEnumText(f, String(v ?? ''))"
            />
            <span v-else class="enum-spacer" />
            <button
              type="button"
              class="row-del"
              title="删除字段"
              @click="removeField(f.id)"
            >
              ×
            </button>
          </article>
          <p v-if="!fields.length" class="empty">还没有字段，先选预设或「＋ 加字段」</p>
        </div>

        <div class="run-row">
          <el-button
            type="primary"
            :loading="loading"
            :disabled="!canRun"
            @click="run"
          >
            {{ loading ? '抽取中' : '抽取并校验' }}
          </el-button>
          <span v-if="result" class="badge" :class="result.valid ? 'ok' : 'fail'">
            {{ result.valid ? `✓ 通过 · 第 ${result.attempts} 次` : `✗ 失败 · ${result.attempts} 次尝试` }}
          </span>
        </div>
      </div>

      <!-- 右：结果 -->
      <div class="col">
        <header class="col-head">
          <span class="field-label">结果</span>
          <span v-if="result" class="meta">model: {{ result.model }}</span>
        </header>

        <div v-if="error" class="console-error" role="alert">
          <span class="err-mark">!</span><span>{{ error }}</span>
        </div>
        <div v-else-if="!result" class="result-empty">
          结果将在此处展示。点击「抽取并校验」开始。
        </div>
        <template v-else>
          <p v-if="result.warning" class="warning">
            ⚠ {{ result.warning }}
          </p>

          <div v-if="result.valid && result.data" class="data-table">
            <header class="data-head">
              <span>字段</span><span>值</span>
            </header>
            <div
              v-for="(value, key) in result.data"
              :key="String(key)"
              class="data-row"
            >
              <span class="data-key">{{ key }}</span>
              <span class="data-value">
                <span v-if="Array.isArray(value)" class="chip" v-for="(v, i) in value" :key="i">{{ v }}</span>
                <span v-else>{{ pretty(value) }}</span>
              </span>
            </div>
          </div>

          <details v-if="result.raw" class="raw-block">
            <summary>原始输出（模型最后一答）</summary>
            <pre class="raw">{{ result.raw }}</pre>
          </details>

          <p v-if="!result.valid && result.error" class="error-msg">
            校验错误：{{ result.error }}
          </p>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.extractor {
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

.presets { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.preset-chip {
  padding: 6px 14px;
  font-size: 13px;
  color: var(--paper);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 9999px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.preset-chip:hover { border-color: var(--data); }
.preset-chip.ghost { color: var(--muted); }

.cols {
  display: grid;
  grid-template-columns: 1fr 1.15fr 1.15fr;
  gap: 14px;
  flex: 1 1 auto;
  min-height: 0;
}
.col {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
.col-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}

.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.meta {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
}
.mono :deep(textarea) {
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.65;
}

/* 字段定义 */
.field-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.field-row {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr 1.4fr auto 1.2fr auto;
  gap: 8px;
  align-items: center;
}
.type-select { width: 100%; }
.desc-input { width: 100%; }
.enum-input { width: 100%; }
.enum-spacer { display: block; }
.req-toggle { display: inline-flex; align-items: center; }
.row-del {
  width: 28px;
  height: 28px;
  line-height: 1;
  font-size: 18px;
  color: var(--muted);
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
}
.row-del:hover { color: var(--signal); border-color: var(--signal); }
.empty {
  margin: auto;
  font-size: 12px;
  color: var(--muted);
}

.run-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.badge {
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 9999px;
}
.badge.ok { color: var(--ink); background: var(--data); }
.badge.fail { color: var(--ink); background: var(--signal); }

/* 结果 */
.result-empty {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--muted);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.warning {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid var(--signal);
  border-radius: 6px;
}
.data-table {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.data-head, .data-row {
  display: grid;
  grid-template-columns: 1fr 2fr;
  padding: 10px 14px;
  font-size: 13px;
}
.data-head {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
  background: var(--panel-2);
}
.data-row + .data-row { border-top: 1px solid var(--line); }
.data-key { font-family: var(--font-mono); color: var(--signal); }
.data-value { color: var(--paper); display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 2px 8px;
  background: color-mix(in srgb, var(--data) 18%, transparent);
  border: 1px solid color-mix(in srgb, var(--data) 50%, transparent);
  border-radius: 9999px;
  color: var(--data);
}

.raw-block {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--muted);
}
.raw-block summary { cursor: pointer; padding: 4px 0; }
.raw {
  margin: 6px 0 0;
  padding: 12px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--data);
  max-height: 240px;
  overflow: auto;
}
.error-msg {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid var(--signal);
  border-radius: 6px;
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

@media (max-width: 1100px) {
  .cols { grid-template-columns: 1fr; }
  .field-row { grid-template-columns: 1fr 0.7fr 1.2fr auto 1.2fr auto; }
}
</style>