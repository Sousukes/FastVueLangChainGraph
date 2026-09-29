<script setup lang="ts">
/*
 * ⚠️ 刻意保留的教学快照 —— 主线零引用是预期的，请勿当死代码删除。
 *
 * 这是【阶段 02】的控制台本体。当时 App.vue 硬编码 CURRENT_STAGE=2，直接渲染本组件
 * （阶段 01–05 均为"单组件模式"，每阶段换一个 console 组件，旧的留在这里不动）。
 *
 * 为何阶段 06 路由化后没给它配路由（stages.ts 里 id=2 是 path: null）：
 *   本组件同样【浏览器直连】DeepSeek；阶段 03 起全站改为 FastAPI 代理 + SSE，
 *   密钥只存后端 .env。给它开路由会让主线产品重新出现"把密钥填进浏览器"的反模式。
 *
 * stage-02 分支里永久保留着它的历史快照。
 */
import { ref, computed } from 'vue'
import { useDeepSeek } from '../composables/useDeepSeek'
import { usePromptTemplate, PRESETS } from '../composables/usePromptTemplate'

defineProps<{
  stage: number
  title: string
}>()

const baseUrl = (import.meta.env.VITE_DEEPSEEK_BASE_URL as string) || 'https://api.deepseek.com'
const model = (import.meta.env.VITE_DEEPSEEK_MODEL as string) || 'deepseek-flash'

// 阶段 02：仍前端直连，只把"调优输入"这件事做到极致
const { apiKey, response, loading, error, send } = useDeepSeek({ baseUrl, model })
const { template, values, variables, rendered } = usePromptTemplate(PRESETS[0].body)

function applyPreset(body: string) {
  template.value = body
}

const unfilled = computed(() =>
  variables.value.filter((v) => !values[v] || !values[v].trim()),
)

const canSend = computed(
  () => !!apiKey.value.trim() && !!rendered.value.trim() && !loading.value,
)

async function onSubmit() {
  if (!canSend.value) return
  await send({ user: rendered.value })
}
</script>

<template>
  <section class="prompt-lab" aria-label="Prompt 模板实验台">
    <p class="principle">
      <span class="principle-mark">原理</span>
      Prompt 工程不发明新 API——它只研究<b>怎么把输入写对</b>。本实验台让你用变量拼模板、
      实时预览、一键发给模型对比效果。密钥仍仅存内存。
    </p>

    <!-- 技法预设 -->
    <div class="presets">
      <button
        v-for="p in PRESETS"
        :key="p.name"
        type="button"
        class="preset-chip"
        :class="{ 'is-active': template === p.body }"
        :title="p.hint"
        @click="applyPreset(p.body)"
      >
        {{ p.name }}
      </button>
      <span class="preset-hint">{{ PRESETS.find((p) => p.body === template)?.hint }}</span>
    </div>

    <div class="lab-cols">
      <!-- 左：模板编辑 -->
      <div class="col">
        <label class="field">
          <span class="field-label">Prompt 模板（用 &#123;&#123;变量&#125;&#125; 占位）</span>
          <el-input
            v-model="template"
            type="textarea"
            :rows="10"
            resize="none"
            class="mono"
          />
        </label>
      </div>

      <!-- 右：变量 + 预览 -->
      <div class="col">
        <label class="field">
          <span class="field-label">
            变量填充
            <span v-if="unfilled.length" class="warn">（缺 {{ unfilled.length }} 项）</span>
          </span>
          <div v-if="variables.length" class="vars">
            <div v-for="v in variables" :key="v" class="var-row">
              <span class="var-name">{{ v }}</span>
              <el-input v-model="values[v]" size="default" placeholder="填入值" />
            </div>
          </div>
          <p v-else class="var-empty">模板中暂无变量占位。</p>
        </label>

        <label class="field">
          <span class="field-label">渲染预览</span>
          <pre class="preview">{{ rendered }}</pre>
        </label>
      </div>
    </div>

    <!-- Key + 发送 -->
    <div class="fields">
      <label class="field key-field">
        <span class="field-label">DeepSeek API Key</span>
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          placeholder="sk-...（在 DeepSeek 控制台获取）"
          size="large"
        />
      </label>
      <div class="actions">
        <el-button
          type="primary"
          size="large"
          :loading="loading"
          :disabled="!canSend"
          @click="onSubmit"
        >
          {{ loading ? '生成中' : '用此 Prompt 发送' }}
        </el-button>
        <span class="model-tag">model: {{ model }}</span>
      </div>
    </div>

    <!-- 输出控制台：签名元素「流式光标」 -->
    <div class="console" :class="{ 'is-empty': !response && !loading && !error }">
      <div v-if="error" class="console-error" role="alert">
        <span class="err-mark">!</span><span>{{ error }}</span>
      </div>
      <pre v-else-if="response" class="console-text">{{ response }}<span class="caret-done" aria-hidden="true" /></pre>
      <div v-else-if="loading" class="console-loading">
        <span class="caret" aria-hidden="true" /> 正在生成…
      </div>
      <div v-else class="console-placeholder">
        回复将在此处输出。换不同预设 / 变量值，观察同一模型因 Prompt 不同而产生的差异。
      </div>
    </div>
  </section>
</template>

<style scoped>
.prompt-lab {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
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

.presets {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.preset-chip {
  padding: 6px 14px;
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--paper);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 9999px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.preset-chip:hover { border-color: var(--data); }
.preset-chip.is-active {
  color: var(--ink);
  background: var(--data);
  border-color: var(--data);
  font-weight: 600;
}
.preset-hint {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
}

.lab-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  align-items: start;
}
.col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }

.field { display: flex; flex-direction: column; gap: 6px; }
.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.warn { color: var(--signal); text-transform: none; letter-spacing: 0; }

.mono :deep(textarea) { font-family: var(--font-mono); font-size: 13px; line-height: 1.6; }

.vars { display: flex; flex-direction: column; gap: 8px; }
.var-row { display: flex; align-items: center; gap: 10px; }
.var-name {
  flex: 0 0 96px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--signal);
}
.var-empty { font-size: 12px; color: var(--muted); margin: 0; }

.preview {
  margin: 0;
  padding: 14px;
  min-height: 120px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--data);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  white-space: pre-wrap;
  word-break: break-word;
}

.fields {
  display: flex;
  align-items: flex-end;
  gap: 16px;
  flex-wrap: wrap;
}
.key-field { flex: 1 1 320px; max-width: 460px; }
.actions { display: flex; align-items: center; gap: 14px; }
.model-tag {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
}

.console {
  flex: 1 1 auto;
  min-height: 180px;
  padding: 18px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow-y: auto;
}
.console.is-empty { display: flex; align-items: center; justify-content: center; }
.console-text {
  margin: 0;
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.7;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
}
.console-placeholder,
.console-loading {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--muted);
}
.console-loading { display: flex; align-items: center; gap: 8px; }
.caret-done {
  display: inline-block;
  width: 0.5ch;
  height: 1.05em;
  margin-left: 1px;
  vertical-align: text-bottom;
  background: var(--data);
  border-radius: 50%;
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

@media (max-width: 820px) {
  .lab-cols { grid-template-columns: 1fr; }
}
</style>
