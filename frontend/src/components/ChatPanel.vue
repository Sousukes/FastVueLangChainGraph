<script setup lang="ts">
import { ref, computed } from 'vue'
import { useDeepSeek } from '../composables/useDeepSeek'

defineProps<{
  stage: number
  title: string
}>()

const baseUrl = (import.meta.env.VITE_DEEPSEEK_BASE_URL as string) || 'https://api.deepseek.com'
const model = (import.meta.env.VITE_DEEPSEEK_MODEL as string) || 'deepseek-flash'

// 阶段 01：直接消费 composable 的只读状态，密钥只存内存、不落盘。
const { apiKey, response, loading, error, send } = useDeepSeek({ baseUrl, model })

const systemPrompt = ref('你是一个严谨的研究助手，用中文简洁、有结构地作答。')
const userInput = ref('请用一句话解释什么是大模型 API，并给出一个最小调用示例。')

const canSend = computed(
  () => !!apiKey.value.trim() && !!userInput.value.trim() && !loading.value,
)

async function onSubmit() {
  if (!canSend.value) return
  await send({ system: systemPrompt.value, user: userInput.value.trim() })
}

function onClear() {
  userInput.value = ''
}
</script>

<template>
  <section class="chat-panel" aria-label="大模型 API 调用台">
    <!-- 原理提示：阶段 01 故意前端直连，讲清 REST 调用本质 -->
    <p class="principle">
      <span class="principle-mark">原理</span>
      本阶段由浏览器<strong>直接</strong>向 <code>{{ baseUrl }}/chat/completions</code> 发请求，
      不经由后端，只为看清一次 LLM 调用的真实模样。密钥仅存于内存，刷新即清空。
    </p>

    <div class="fields">
      <label class="field">
        <span class="field-label">DeepSeek API Key</span>
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          placeholder="sk-...（在 DeepSeek 控制台获取）"
          size="large"
        />
      </label>

      <label class="field">
        <span class="field-label">System Prompt（可选）</span>
        <el-input
          v-model="systemPrompt"
          type="textarea"
          :rows="2"
          resize="none"
          placeholder="设定模型角色与约束"
        />
      </label>

      <label class="field">
        <span class="field-label">User Message</span>
        <el-input
          v-model="userInput"
          type="textarea"
          :rows="4"
          resize="none"
          placeholder="想问模型的问题"
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
          {{ loading ? '生成中' : '发送请求' }}
        </el-button>
        <button type="button" class="link-btn" :disabled="!userInput" @click="onClear">
          清空输入
        </button>
        <span class="model-tag">model: {{ model }}</span>
      </div>
    </div>

    <!-- 输出控制台：签名元素「流式光标」 -->
    <div class="console" :class="{ 'is-empty': !response && !loading && !error }">
      <div v-if="error" class="console-error" role="alert">
        <span class="err-mark">!</span>
        <span>{{ error }}</span>
      </div>
      <pre v-else-if="response" class="console-text">{{ response }}<span class="caret-done" aria-hidden="true" /></pre>
      <div v-else-if="loading" class="console-loading">
        <span class="caret" aria-hidden="true" /> 正在生成…
      </div>
      <div v-else class="console-placeholder">
        回复将在此处输出。发出请求后，这里会以流式光标逐字呈现模型回答。
      </div>
    </div>
  </section>
</template>

<style scoped>
.chat-panel {
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
.principle code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--data);
}
.principle strong {
  color: var(--paper);
}

.fields {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}

.actions {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.link-btn {
  background: none;
  border: 0;
  color: var(--data);
  font-family: var(--font-body);
  font-size: 13px;
  cursor: pointer;
  padding: 0;
}
.link-btn:disabled {
  color: var(--muted);
  cursor: not-allowed;
}
.model-tag {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
}

.console {
  flex: 1 1 auto;
  min-height: 200px;
  padding: 18px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow-y: auto;
}
.console.is-empty {
  display: flex;
  align-items: center;
  justify-content: center;
}
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
.console-placeholder {
  text-align: center;
}
.console-loading {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 完成时 caret 收为实心句点（Data 青绿） */
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
  font-family: var(--font-body);
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
</style>
