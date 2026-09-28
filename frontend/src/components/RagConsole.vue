<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRag, PRESET_QUERIES, type RagHit } from '../composables/useRag'

defineProps<{
  stage: number
  title: string
}>()

const {
  status,
  documents,
  ingest,
  query,
  topK,
  hits,
  searchedQuery,
  question,
  askResult,
  busy,
  error,
  loadStatus,
  addDocument,
  seed,
  removeDocument,
  runSearch,
  runAsk,
  reset,
  applyQuery,
  applyQuestion,
} = useRag()

onMounted(loadStatus)

const newTitle = ref('')
const newText = ref('')
const showChunks = ref(true)
const showPrompt = ref(false)

/** 余弦相似度一般在 0.3~0.9 之间，直接当百分比画条会全都"很短" */
function barWidth(score: number) {
  return Math.max(4, Math.min(100, Math.round(score * 100)))
}

/** 三档配色：≥0.6 很相关 / ≥0.45 沾边 / 其余基本是噪声 */
function tone(score: number) {
  if (score >= 0.6) return 'high'
  if (score >= 0.45) return 'mid'
  return 'low'
}

async function submitDoc() {
  await addDocument(newTitle.value, newText.value)
  newTitle.value = ''
  newText.value = ''
}

function hitLabel(h: RagHit) {
  return `${h.title} · 第 ${h.index} 段`
}
</script>

<template>
  <section class="rag-console" aria-label="RAG 检索控制台">
    <p class="principle">
      <span class="principle-mark">原理</span>
      RAG 是五步流水线：<b>切块 → 嵌入 → 入库 → 检索 → 生成</b>。
      前四步<b>完全不需要 LLM</b>——所以中栏的「检索」可以单独跑。
      调 RAG 的第一条纪律就是<b>先看检索</b>：检索捞错了，生成再强也救不回来。
      右栏会把「检索到的内容是怎么拼进 prompt 的」原样摊开。
    </p>

    <div class="cols">
      <!-- 左：知识库 -->
      <aside class="col col-kb">
        <header class="col-head">
          <span class="field-label">知识库</span>
          <span class="meta">ChromaDB · 本地</span>
        </header>

        <div v-if="status" class="status-card">
          <div class="status-grid">
            <div class="stat">
              <span class="stat-num">{{ status.documents }}</span>
              <span class="stat-key">文档</span>
            </div>
            <div class="stat">
              <span class="stat-num">{{ status.chunks }}</span>
              <span class="stat-key">块</span>
            </div>
          </div>
          <dl class="kv">
            <dt>嵌入模型</dt>
            <dd class="mono">{{ status.embedModel }}</dd>
            <dt>切块</dt>
            <dd class="mono">{{ status.chunkSize }} 字 / 重叠 {{ status.chunkOverlap }}</dd>
            <dt>集合</dt>
            <dd class="mono">{{ status.collection }}</dd>
          </dl>
        </div>

        <div class="btn-row">
          <el-button size="small" :loading="busy === 'seed'" @click="seed">导入示例文档</el-button>
          <el-button size="small" text :loading="busy === 'reset'" @click="reset">清空</el-button>
        </div>

        <div class="doc-list">
          <p v-if="!documents.length" class="empty">还没有文档。点「导入示例文档」，或自己粘一段进来。</p>
          <article v-for="d in documents" :key="d.title" class="doc-row">
            <div class="doc-main">
              <span class="doc-title">{{ d.title }}</span>
              <span class="doc-meta mono">{{ d.chunks }} 块 · {{ d.chars }} 字</span>
            </div>
            <button type="button" class="doc-del" title="删除" @click="removeDocument(d.title)">×</button>
          </article>
        </div>

        <details class="add-box">
          <summary>手动添加文档</summary>
          <el-input v-model="newTitle" size="small" placeholder="文档标题，例如：我的读书笔记" />
          <el-input
            v-model="newText"
            type="textarea"
            :rows="4"
            resize="none"
            placeholder="粘贴一段长文本（越接近真实文档，越能看出切块效果）"
          />
          <el-button
            size="small"
            type="primary"
            :disabled="!newTitle.trim() || !newText.trim()"
            :loading="busy === 'ingest'"
            @click="submitDoc"
          >
            切块并入库
          </el-button>
        </details>

        <!-- 切块预览：这是本阶段最"解谜"的一块——文档被切成了什么样 -->
        <div v-if="ingest" class="chunk-box">
          <button type="button" class="chunk-head" @click="showChunks = !showChunks">
            <span class="field-label">切块结果 · {{ ingest.title }}</span>
            <span class="meta">{{ ingest.chars }} 字 → {{ ingest.chunks }} 块 {{ showChunks ? '▾' : '▸' }}</span>
          </button>
          <ol v-if="showChunks" class="chunk-list">
            <li v-for="(c, i) in ingest.preview" :key="i" class="chunk-item">
              <span class="chunk-no mono">#{{ i }}</span>
              <p class="chunk-text">{{ c }}</p>
            </li>
          </ol>
        </div>
      </aside>

      <!-- 中：检索（不调 LLM） -->
      <div class="col col-search">
        <header class="col-head">
          <span class="field-label">检索 · 不调 LLM</span>
          <span class="meta">POST /api/rag/search</span>
        </header>

        <div class="presets">
          <button
            v-for="p in PRESET_QUERIES"
            :key="p.label"
            type="button"
            class="preset-chip"
            :title="p.query"
            @click="applyQuery(p.query)"
          >
            {{ p.label }}
            <span class="chip-hint">{{ p.hint }}</span>
          </button>
        </div>

        <div class="search-row">
          <el-input v-model="query" size="small" placeholder="输入一个问题或关键词" @keyup.enter="runSearch" />
          <el-input-number v-model="topK" size="small" :min="1" :max="10" controls-position="right" />
          <el-button size="small" type="primary" :loading="busy === 'search'" :disabled="!query.trim()" @click="runSearch">
            检索
          </el-button>
        </div>

        <!-- 相似度是余弦值，绝对高低没有意义，只看相对排序和这三档 -->
        <div class="legend">
          <span class="lg"><i class="lg-dot high"></i>≥ 0.60 高度相关</span>
          <span class="lg"><i class="lg-dot mid"></i>0.45 – 0.60 沾边</span>
          <span class="lg"><i class="lg-dot low"></i>&lt; 0.45 基本是噪声</span>
        </div>

        <div class="hit-list">
          <p v-if="!hits.length" class="empty">
            检索结果会出现在这里。<b>它只做一件事</b>：把问题的向量和库里每个块的向量算余弦相似度，取最高的 top-k。
          </p>
          <article v-for="(h, i) in hits" :key="`${h.title}-${h.index}`" class="hit-card" :class="tone(h.score)">
            <header class="hit-head">
              <span class="hit-rank mono">TOP {{ i + 1 }}</span>
              <span class="hit-src">{{ hitLabel(h) }}</span>
              <span class="hit-score mono">{{ h.score.toFixed(4) }}</span>
            </header>
            <div class="score-bar">
              <div class="score-fill" :style="{ width: barWidth(h.score) + '%' }"></div>
            </div>
            <p class="hit-text">{{ h.text }}</p>
          </article>
        </div>
        <p v-if="searchedQuery && hits.length" class="meta foot-note">查询：{{ searchedQuery }}</p>
      </div>

      <!-- 右：RAG 问答 -->
      <div class="col col-ask">
        <header class="col-head">
          <span class="field-label">RAG 问答</span>
          <span class="meta">POST /api/rag/ask</span>
        </header>

        <div class="presets">
          <button
            v-for="p in PRESET_QUERIES"
            :key="p.label"
            type="button"
            class="preset-chip"
            :title="p.query"
            @click="applyQuestion(p.query)"
          >
            {{ p.label }}
          </button>
        </div>

        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          resize="none"
          placeholder="例如：这门课第 07 到第 12 阶段分别讲什么？"
        />
        <div class="run-row">
          <el-button
            size="small"
            type="primary"
            :loading="busy === 'ask'"
            :disabled="!question.trim()"
            @click="runAsk"
          >
            {{ busy === 'ask' ? '检索并生成' : '提问' }}
          </el-button>
          <span v-if="askResult" class="meta">{{ askResult.hits.length }} 条资料 · {{ askResult.model }}</span>
        </div>

        <div v-if="askResult" class="answer">
          <span class="field-label">最终回答</span>
          <p class="answer-text">{{ askResult.answer }}<span class="caret-done"></span></p>
        </div>

        <div v-if="askResult" class="sources">
          <span class="field-label">引用的资料（{{ askResult.hits.length }}）</span>
          <ul class="source-list">
            <li v-for="(h, i) in askResult.hits" :key="`s-${i}`" class="source-item">
              <span class="source-no mono">[{{ i + 1 }}]</span>
              <span class="source-src">{{ hitLabel(h) }}</span>
              <span class="source-score mono">{{ h.score.toFixed(4) }}</span>
            </li>
          </ul>
        </div>

        <div v-if="askResult" class="prompt-box">
          <button type="button" class="prompt-head" @click="showPrompt = !showPrompt">
            <span class="field-label">拼给模型的 prompt</span>
            <span class="meta">{{ showPrompt ? '收起 ▾' : '展开 ▸' }}</span>
          </button>
          <pre v-if="showPrompt" class="prompt-body">{{ askResult.prompt }}</pre>
        </div>
      </div>
    </div>

    <p v-if="error" class="console-error" role="alert">
      <span class="err-mark">!</span><span>{{ error }}</span>
    </p>
  </section>
</template>

<style scoped>
.rag-console {
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
.principle-mark {
  display: inline-block;
  margin-right: 8px;
  padding: 1px 7px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink);
  background: var(--data);
  border-radius: 3px;
}

.cols {
  display: grid;
  grid-template-columns: 1fr 1.1fr 1fr;
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
.empty {
  margin: 0;
  padding: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
  border: 1px dashed var(--line);
  border-radius: 6px;
}
.empty b { color: var(--paper); }

/* 左栏 */
.status-card {
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.stat { display: flex; flex-direction: column; gap: 2px; }
.stat-num { font-family: var(--font-display); font-size: 24px; line-height: 1; color: var(--signal); }
.stat-key { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.kv {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 10px;
  margin: 12px 0 0;
  font-size: 11px;
}
.kv dt { color: var(--muted); }
.kv dd { margin: 0; color: var(--paper); text-align: right; word-break: break-all; }

.btn-row { display: flex; gap: 8px; }

.doc-list {
  flex: 0 1 auto;
  max-height: 26%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.doc-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.doc-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 auto; }
.doc-title { font-size: 12px; color: var(--paper); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-meta { font-size: 10px; color: var(--muted); }
.doc-del {
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  font-size: 15px;
  line-height: 1;
  color: var(--muted);
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 4px;
  cursor: pointer;
}
.doc-del:hover { color: var(--signal); border-color: var(--signal); }

.add-box {
  padding: 8px 10px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.add-box summary {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
}
.add-box :deep(.el-input),
.add-box :deep(.el-textarea) { margin-top: 8px; }
.add-box :deep(.el-button) { margin-top: 8px; width: 100%; }

.chunk-box {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.chunk-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 10px;
  text-align: left;
  background: var(--panel-2);
  border: 0;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.chunk-list {
  flex: 1 1 auto;
  min-height: 0;
  margin: 0;
  padding: 8px 10px;
  list-style: none;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chunk-item { display: flex; gap: 8px; }
.chunk-no { flex: 0 0 auto; font-size: 10px; color: var(--signal); padding-top: 2px; }
.chunk-text {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
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

.search-row { display: flex; align-items: center; gap: 8px; }
.search-row :deep(.el-input-number) { width: 96px; flex: 0 0 auto; }

.legend { display: flex; flex-wrap: wrap; gap: 12px; }
.lg { display: inline-flex; align-items: center; gap: 5px; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.lg-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.lg-dot.high { background: var(--data); }
.lg-dot.mid { background: var(--signal); }

.hit-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hit-card {
  padding: 10px 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--muted);
  border-radius: 0 8px 8px 0;
}
.hit-card.high { border-left-color: var(--data); }
.hit-card.mid { border-left-color: var(--signal); }
.hit-card.low { border-left-color: var(--muted); }
.hit-head { display: flex; align-items: baseline; gap: 8px; }
.hit-rank { font-size: 10px; color: var(--muted); }
.hit-src { flex: 1 1 auto; font-size: 11.5px; color: var(--data); }
.hit-score { font-size: 11px; color: var(--signal); }
.score-bar {
  height: 3px;
  margin: 7px 0;
  background: var(--line);
  border-radius: 2px;
  overflow: hidden;
}
.score-fill { height: 100%; background: var(--data); }
.hit-card.mid .score-fill { background: var(--signal); }
.hit-card.low .score-fill { background: var(--muted); }
.hit-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 9.9em;
  overflow: hidden;
}
.foot-note { margin: 0; }

/* 右栏 */
.run-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

.answer {
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--data);
  border-radius: 0 8px 8px 0;
}
.answer-text {
  margin: 8px 0 0;
  font-size: 14px;
  line-height: 1.75;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
}
.caret-done {
  display: inline-block;
  width: 0.5ch;
  height: 1.05em;
  margin-left: 1px;
  vertical-align: text-bottom;
  background: var(--data);
  border-radius: 50%;
}

.sources { display: flex; flex-direction: column; gap: 6px; }
.source-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.source-item { display: flex; align-items: baseline; gap: 8px; font-size: 11.5px; }
.source-no { color: var(--signal); }
.source-src { flex: 1 1 auto; color: var(--paper); }
.source-score { color: var(--muted); }

.prompt-box {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.prompt-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 7px 10px;
  text-align: left;
  background: var(--panel-2);
  border: 0;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.prompt-body {
  flex: 1 1 auto;
  min-height: 0;
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

.console-error {
  display: flex;
  gap: 8px;
  margin: 0;
  padding: 10px 12px;
  font-size: 12.5px;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent);
  border-radius: 6px;
}
.err-mark { font-family: var(--font-mono); font-weight: 600; }

@media (max-width: 1180px) {
  .cols { grid-template-columns: 1fr; }
  .doc-list { max-height: none; }
  .chunk-box { min-height: 200px; }
  .hit-list { min-height: 240px; }
  .prompt-box { min-height: 180px; }
}
</style>
