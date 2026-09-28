<script setup lang="ts">
import { ref, nextTick, computed } from 'vue'
import { useChatStream } from '../composables/useChatStream'

defineProps<{
  stage: number
  title: string
}>()

const { system, messages, loading, error, turnCount, send, stop, reset } = useChatStream()

const draft = ref('')
const scroller = ref<HTMLElement | null>(null)

const canSend = computed(() => !!draft.value.trim() && !loading.value)

async function onSubmit() {
  if (!canSend.value) return
  const text = draft.value
  draft.value = ''
  await send(text)
  await nextTick()
  scrollToBottom()
}

function scrollToBottom() {
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行（聊天输入区的通用约定）
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    onSubmit()
  }
}
</script>

<template>
  <section class="stream-chat" aria-label="多轮对话与流式控制台">
    <p class="principle">
      <span class="principle-mark">原理</span>
      密钥已移到后端（<code>backend/.env</code>），前端不再接触它。
      多轮对话 = 每轮把<b>整段历史</b>发给后端；流式 = 后端用 SSE 逐块推送，前端边收边渲染。
    </p>

    <label class="field system-field">
      <span class="field-label">System 人设（每条请求都会带上）</span>
      <el-input v-model="system" placeholder="给模型一个稳定人设" size="default" />
    </label>

    <!-- 对话区 -->
    <div ref="scroller" class="thread">
      <p v-if="!messages.length" class="thread-empty">
        还没有对话。问一句「什么是 RAG」，然后追问「它和微调有什么区别」——
        看模型能不能记住上一轮在聊什么。
      </p>

      <article
        v-for="m in messages"
        :key="m.id"
        class="turn"
        :class="`turn-${m.role}`"
      >
        <header class="turn-head">
          <span class="turn-role">{{ m.role === 'user' ? 'YOU' : 'ASSISTANT' }}</span>
          <span v-if="m.streaming" class="turn-state">streaming</span>
        </header>
        <p class="turn-body">
          {{ m.content }}<span v-if="m.streaming" class="caret" aria-hidden="true" /><span
            v-else-if="m.role === 'assistant' && m.content"
            class="caret-done"
            aria-hidden="true"
          />
        </p>
      </article>

      <div v-if="error" class="console-error" role="alert">
        <span class="err-mark">!</span><span>{{ error }}</span>
      </div>
    </div>

    <!-- 输入区 -->
    <div class="composer">
      <el-input
        v-model="draft"
        type="textarea"
        :rows="3"
        resize="none"
        :disabled="loading"
        placeholder="输入消息，Enter 发送 / Shift+Enter 换行"
        @keydown="onKeydown"
      />
      <div class="composer-actions">
        <el-button type="primary" :disabled="!canSend" @click="onSubmit">发送</el-button>
        <el-button v-if="loading" @click="stop">停止生成</el-button>
        <el-button text @click="reset">清空会话</el-button>
        <span class="meta">上下文 {{ turnCount }} 条 · POST /api/chat/stream</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.stream-chat {
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

.field { display: flex; flex-direction: column; gap: 6px; }
.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}

/* 对话区 */
.thread {
  flex: 1 1 auto;
  min-height: 220px;
  padding: 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.thread-empty {
  margin: auto;
  max-width: 46ch;
  font-size: 13px;
  line-height: 1.7;
  color: var(--muted);
  text-align: center;
}

.turn {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 78%;
}
.turn-user { align-self: flex-end; align-items: flex-end; }
.turn-assistant { align-self: flex-start; }

.turn-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--muted);
}
.turn-user .turn-role { color: var(--signal); }
.turn-assistant .turn-role { color: var(--data); }
.turn-state { color: var(--data); text-transform: lowercase; }

.turn-body {
  margin: 0;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.75;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.turn-user .turn-body {
  background: color-mix(in srgb, var(--signal) 12%, var(--panel-2));
  border-color: color-mix(in srgb, var(--signal) 35%, var(--line));
}

/* 签名元素：流式光标 / 完成句点 */
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

/* 输入区 */
.composer { display: flex; flex-direction: column; gap: 10px; }
.composer-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.meta {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
}

@media (max-width: 820px) {
  .turn { max-width: 100%; }
  .meta { margin-left: 0; }
}
</style>
