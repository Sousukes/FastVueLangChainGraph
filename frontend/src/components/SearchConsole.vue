<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useSearch } from '../composables/useSearch'

defineProps<{ stage: number; title: string }>()

const s = useSearch()

onMounted(() => {
  s.question.value = 'RAG 的切块大小一般取多少？为什么要留重叠？'
})

/** 来源按"被引用优先、其次按检索排名"排序，让被引用的来源浮到前面 */
const sortedSources = computed(() =>
  [...s.sources.value].sort((a, b) => Number(b.cited) - Number(a.cited) || a.rank - b.rank),
)
</script>

<template>
  <section class="search-console" aria-label="AI 搜索控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 07–13 教会了"怎么把资料找回来、要不要查、查够了没"。但有一件事一直没解决：
      <b>找回来之后，怎么让用户相信答案？</b> 单纯把 top-k 拼进 prompt，用户拿到的是一段
      <b>无法核对</b>的文字。本阶段做"AI 搜索"——答案每句都挂上
      <b>[n] 出处</b>，点一下就能跳到原文。这正是 Perplexity 式产品的核心：<b>答案必须可被验证</b>。
    </p>

    <!-- ⭐ 签名元素：搜索框 -->
    <div class="search-hero">
      <div class="search-box" :class="{ active: s.running.value }">
        <span class="search-glyph" aria-hidden="true">⌕</span>
        <input
          v-model="s.question.value"
          class="search-input"
          type="text"
          placeholder="问一个需要查资料的问题，比如「重排是怎么做的」"
          @keyup.enter="s.canRun.value && s.run()"
        />
        <el-button
          class="search-go"
          size="default"
          type="primary"
          :loading="s.running.value"
          :disabled="!s.canRun.value"
          @click="s.run()"
        >
          {{ s.running.value ? '检索中…' : '搜索' }}
        </el-button>
      </div>
      <div class="search-params">
        <label class="param">
          <span>返回条数</span>
          <el-input-number v-model="s.topK.value" size="small" :min="1" :max="10" controls-position="right" />
        </label>
        <label class="param toggle">
          <el-switch v-model="s.rerank.value" size="small" />
          <span>Cross-encoder 重排</span>
        </label>
        <el-button size="small" :disabled="s.running.value" text @click="s.reset()">清空</el-button>
      </div>
    </div>

    <p v-if="s.error.value" class="err-line">⚠️ {{ s.error.value }}</p>

    <!-- 状态条 -->
    <div v-if="s.retrievedCount.value || s.running.value" class="stats-bar">
      <div class="stat">
        <span class="stat-num" :class="{ bad: !s.grounded.value }">{{ s.grounded.value ? '已挂出处' : '未挂出处' }}</span>
        <span class="stat-label">答案可验证</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ s.citations.value.length }}<i class="of">/ {{ s.retrievedCount.value }}</i></span>
        <span class="stat-label">被引用 / 检索条数</span>
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

    <template v-if="s.answer.value || s.running.value">
      <!-- 答案卡 -->
      <div class="panel answer-panel">
        <div class="panel-head">
          <span class="field-label">答案 · 带引用</span>
          <span v-if="!s.grounded.value && !s.running.value" class="warn-tag">⚠ 答案未引用任何资料，可能不基于检索结果</span>
        </div>
        <div class="answer">
          <template v-for="(p, i) in s.answerParts.value" :key="i">
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
          <i v-if="s.running.value" class="caret"></i>
        </div>
      </div>

      <!-- 来源面板 -->
      <div class="panel sources-panel">
        <div class="panel-head">
          <span class="field-label">来源 · 可核对（点答案里的 [n] 定位）</span>
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
              <span
                v-if="typeof src.rerankScore === 'number'"
                class="src-score"
              >重排 {{ src.rerankScore.toFixed(3) }}</span>
              <span v-else-if="typeof src.score === 'number'" class="src-score">{{ src.score.toFixed(3) }}</span>
            </header>
            <p class="src-snippet">{{ src.snippet }}</p>
          </li>
        </ul>
      </div>
    </template>

    <p v-else class="empty">
      在上方搜索框输入问题并点击「搜索」。检索到的资料会以 [n] 标在答案里，下方来源面板可逐条核对。
    </p>
  </section>
</template>

<style scoped>
.search-console {
  --accent: var(--signal); /* 琥珀：本阶段的搜索主调 */
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

/* ---------- 签名元素：搜索框 ---------- */
.search-hero { display: flex; flex-direction: column; gap: 8px; }
.search-box {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 6px 6px 14px;
  background: var(--panel);
  border: 1.5px solid var(--line);
  border-radius: 10px;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.search-box.active,
.search-box:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
}
.search-glyph {
  font-size: 20px;
  color: var(--accent);
  font-family: var(--font-mono);
}
.search-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 9px 0;
  font-family: var(--font-body);
  font-size: 15px;
  color: var(--paper);
  background: transparent;
  border: none;
  outline: none;
}
.search-input::placeholder { color: var(--muted); }
.search-go { flex: 0 0 auto; }

.search-params { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.param { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }
.param :deep(.el-input-number) { width: 92px; }
.param.toggle { gap: 7px; }

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
.warn-tag { font-size: 10.5px; color: var(--accent); }

/* ---------- 答案卡 ---------- */
.answer {
  min-height: 64px;
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
