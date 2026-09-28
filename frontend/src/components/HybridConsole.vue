<script setup lang="ts">
import { computed, onMounted } from 'vue'
import {
  useHybrid,
  HYBRID_PRESETS,
  rowKey,
  type RankRow,
} from '../composables/useHybrid'

defineProps<{
  stage: number
  title: string
}>()

const {
  status,
  query,
  mode,
  rerank,
  candidates,
  topK,
  result,
  askResult,
  busy,
  error,
  loadStatus,
  runSearch,
  runAsk,
  applyPreset,
} = useHybrid()

onMounted(loadStatus)

/** 当前预设标出的"正确答案块"——对照表里给它打星 */
const target = computed(() => {
  const hit = HYBRID_PRESETS.find((p) => p.query === query.value)
  return hit?.target ?? null
})

const targetRow = computed<RankRow | null>(() => {
  if (!result.value || !target.value) return null
  return result.value.rows.find((r) => rowKey(r) === target.value) ?? null
})

/** 名次是否受当前模式影响（比如纯向量模式下 BM25 列不该显示） */
const showBm25 = computed(() => mode.value !== 'vector')
const showVector = computed(() => mode.value !== 'bm25')
const showRerank = computed(() => rerank.value)

type Move = 'up' | 'down' | 'flat' | 'new' | 'gone'

/** 与「阶段 07 的纯向量」相比，这一格是升了还是降了 */
function move(row: RankRow, stage: 'bm25' | 'fused' | 'rerank'): Move {
  const cur = row[`${stage}_rank` as const]
  if (cur == null) return row.vector_rank == null ? 'flat' : 'gone'
  if (row.vector_rank == null) return 'new'
  if (cur < row.vector_rank) return 'up'
  if (cur > row.vector_rank) return 'down'
  return 'flat'
}

const MOVE_GLYPH: Record<Move, string> = {
  up: '↑',
  down: '↓',
  flat: '',
  new: '★',
  gone: '—',
}

const MOVE_TITLE: Record<Move, string> = {
  up: '比纯向量靠前',
  down: '比纯向量靠后',
  flat: '与纯向量相同',
  new: '纯向量根本没捞到它',
  gone: '这一路没有它',
}

function rankText(v: number | null) {
  return v == null ? '—' : String(v)
}

function fmtScore(v: number | null | undefined, digits = 3) {
  return v == null ? '—' : v.toFixed(digits)
}

function snippet(text: string, n = 88) {
  return text.replace(/\s+/g, ' ').slice(0, n)
}

/** 耗时明细，按管道顺序排 */
const timingOrder = ['vector', 'bm25', 'fuse', 'rerank']
const timings = computed(() => {
  const t = result.value?.timings ?? {}
  return timingOrder.filter((k) => t[k] != null).map((k) => ({ key: k, ms: t[k] }))
})
const TIMING_LABEL: Record<string, string> = {
  vector: '向量召回',
  bm25: 'BM25 召回',
  fuse: 'RRF 融合',
  rerank: '重排',
}
</script>

<template>
  <section class="hybrid-console" aria-label="混合检索对照台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 07 只用了<b>向量检索</b>，它暴露了两个真实的失败：<b>精确术语捞不到</b>
      （「三条铁律」的标准答案块排到第 25 名）、<b>表格被切碎后排错</b>。
      本阶段用三招修：<b>BM25 关键词召回</b>补上"字面命中"，
      <b>RRF 融合</b>把两路排名合成一份，
      <b>Cross-encoder 重排</b>让模型真正读一遍内容再决定顺序。
      下面的表格会把每个块在各阶段的名次<b>摊开</b>——变化比解释更有说服力。
    </p>

    <!-- 控制条 -->
    <div class="controls">
      <div class="presets">
        <button
          v-for="p in HYBRID_PRESETS"
          :key="p.label"
          type="button"
          class="preset-chip"
          :class="{ active: query === p.query }"
          :title="p.query"
          @click="applyPreset(p.query)"
        >
          {{ p.label }}
          <span class="chip-hint">{{ p.hint }}</span>
        </button>
      </div>

      <div class="ctrl-row">
        <el-input
          v-model="query"
          size="small"
          placeholder="输入一个问题"
          class="q-input"
          @keyup.enter="runSearch"
        />

        <div class="seg" role="radiogroup" aria-label="检索模式">
          <button
            v-for="m in (['vector', 'bm25', 'hybrid'] as const)"
            :key="m"
            type="button"
            class="seg-btn"
            :class="{ on: mode === m }"
            :title="
              m === 'vector'
                ? '只看向量（阶段 07 的基线）'
                : m === 'bm25'
                  ? '只看关键词'
                  : '两路召回 + RRF 融合'
            "
            @click="mode = m"
          >
            {{ m === 'vector' ? '向量' : m === 'bm25' ? 'BM25' : '混合' }}
          </button>
        </div>

        <label class="chk" title="用 Cross-encoder 对候选重排（首次会下载约 266MB）">
          <input v-model="rerank" type="checkbox" />
          <span>重排</span>
        </label>

        <label class="num">
          候选
          <el-input-number
            v-model="candidates"
            size="small"
            :min="1"
            :max="50"
            controls-position="right"
          />
        </label>
        <label class="num">
          取
          <el-input-number
            v-model="topK"
            size="small"
            :min="1"
            :max="10"
            controls-position="right"
          />
        </label>

        <el-button
          size="small"
          type="primary"
          :loading="busy === 'search'"
          :disabled="!query.trim()"
          @click="runSearch"
        >
          检索
        </el-button>
        <el-button
          size="small"
          :loading="busy === 'ask'"
          :disabled="!query.trim()"
          @click="runAsk"
        >
          问模型
        </el-button>
      </div>

      <!-- 重排模型状态：把"要下 266MB"这件事提前说清楚，而不是让用户等一个意外 -->
      <p v-if="status && rerank && !status.rerankReady" class="warn">
        <span class="warn-mark">!</span>
        重排模型 <code>{{ status.rerankModel }}</code> 还没下载（约 266MB）。
        第一次点「检索」会先下载，请耐心等待；也可以先去
        <code>backend/.env</code> 里配 <code>HF_ENDPOINT=https://hf-mirror.com</code>。
      </p>
      <p v-else-if="status && rerank" class="ok-note">
        重排模型已就绪：<code>{{ status.rerankModel }}</code>（int8 量化版）
      </p>
    </div>

    <!-- 目标块的"爬升轨迹" -->
    <div v-if="targetRow" class="journey">
      <span class="journey-label">★ 答案块</span>
      <span class="journey-src mono">{{ rowKey(targetRow) }}</span>
      <span class="journey-steps">
        <template v-if="showVector">
          <span class="step">
            <em>向量</em>{{ rankText(targetRow.vector_rank) }}
          </span>
          <span class="arrow">→</span>
        </template>
        <template v-if="showBm25">
          <span class="step">
            <em>BM25</em>{{ rankText(targetRow.bm25_rank) }}
          </span>
          <span class="arrow">→</span>
        </template>
        <span class="step">
          <em>融合</em>{{ rankText(targetRow.fused_rank) }}
        </span>
        <template v-if="showRerank">
          <span class="arrow">→</span>
          <span class="step hot">
            <em>重排</em>{{ rankText(targetRow.rerank_rank) }}
          </span>
        </template>
        <span class="journey-final" :class="{ win: targetRow.in_final }">
          {{ targetRow.in_final ? '✓ 进了最终结果' : '✗ 没进最终结果' }}
        </span>
      </span>
    </div>

    <!-- 排名对照表 -->
    <div class="table-wrap">
      <p v-if="!result" class="empty">
        点一个预设或直接检索。结果会以<b>排名对照表</b>的形式出现：
        每一行是一个块，每一列是它在某个检索阶段的名次。
        <b>同一个块在不同列之间的跳变，就是这一阶段要讲的全部内容。</b>
      </p>

      <table v-else class="rank-table">
        <thead>
          <tr>
            <th class="col-src">块</th>
            <th v-if="showVector" class="col-rank">向量</th>
            <th v-if="showBm25" class="col-rank">BM25</th>
            <th class="col-rank">融合</th>
            <th v-if="showRerank" class="col-rank">重排</th>
            <th class="col-score">分数</th>
            <th class="col-final">最终</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="r in result.rows"
            :key="rowKey(r)"
            :class="{
              'row-final': r.in_final,
              'row-target': rowKey(r) === target,
            }"
          >
            <td class="col-src">
              <div class="src-line">
                <span v-if="rowKey(r) === target" class="star" title="预设标出的答案块">★</span>
                <span class="src mono">{{ rowKey(r) }}</span>
              </div>
              <p class="src-text">{{ snippet(r.text) }}</p>
            </td>

            <td v-if="showVector" class="col-rank">
              <span class="rank" :class="{ dim: r.vector_rank == null }">
                {{ rankText(r.vector_rank) }}
              </span>
            </td>

            <td v-if="showBm25" class="col-rank">
              <span class="rank">{{ rankText(r.bm25_rank) }}</span>
              <span
                v-if="MOVE_GLYPH[move(r, 'bm25')]"
                class="mv"
                :class="move(r, 'bm25')"
                :title="MOVE_TITLE[move(r, 'bm25')]"
                >{{ MOVE_GLYPH[move(r, 'bm25')] }}</span
              >
            </td>

            <td class="col-rank">
              <span class="rank">{{ rankText(r.fused_rank) }}</span>
              <span
                v-if="MOVE_GLYPH[move(r, 'fused')]"
                class="mv"
                :class="move(r, 'fused')"
                :title="MOVE_TITLE[move(r, 'fused')]"
                >{{ MOVE_GLYPH[move(r, 'fused')] }}</span
              >
            </td>

            <td v-if="showRerank" class="col-rank">
              <span class="rank">{{ rankText(r.rerank_rank) }}</span>
              <span
                v-if="MOVE_GLYPH[move(r, 'rerank')]"
                class="mv"
                :class="move(r, 'rerank')"
                :title="MOVE_TITLE[move(r, 'rerank')]"
                >{{ MOVE_GLYPH[move(r, 'rerank')] }}</span
              >
            </td>

            <td class="col-score mono">
              <span class="sc" title="余弦相似度">
                {{ fmtScore(r.vector_score, 3) }}
              </span>
              <span class="sc bm" title="BM25 分数">{{ fmtScore(r.bm25_score, 1) }}</span>
              <span v-if="showRerank" class="sc rr" title="重排分数">
                {{ fmtScore(r.rerank_score, 3) }}
              </span>
            </td>

            <td class="col-final">
              <span v-if="r.in_final" class="badge">TOP</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="result" class="meta foot">
      查询：{{ result.query }} ·
      mode=<code>{{ result.mode }}</code> · rerank=<code>{{ result.rerank }}</code> ·
      候选 <code>{{ result.candidates }}</code> ·
      候选池共 <code>{{ result.rows.length }}</code> 块 ·
      耗时
      <span v-for="t in timings" :key="t.key" class="tm">
        {{ TIMING_LABEL[t.key] }} {{ t.ms }}ms
      </span>
    </p>

    <!-- 问答：把"检索修好了，答案就对了"这个闭环补上 -->
    <div v-if="askResult" class="answer-box">
      <div class="ans-head">
        <span class="field-label">模型回答</span>
        <span class="meta">
          {{ askResult.hits.length }} 条资料 ·
          <code>{{ askResult.model }}</code> ·
          <code>{{ askResult.mode }}</code>{{ askResult.rerank ? ' + 重排' : '' }}
        </span>
      </div>
      <p class="ans-text">{{ askResult.answer }}<span class="caret-done"></span></p>

      <ul class="src-list">
        <li v-for="(h, i) in askResult.hits" :key="`a-${i}`" class="src-item">
          <span class="src-no mono">[{{ i + 1 }}]</span>
          <span class="src-name">{{ h.title }} · 第 {{ h.index }} 段</span>
          <span class="src-scores mono">
            向量 {{ fmtScore(h.vector_score, 3) }} / BM25 {{ fmtScore(h.bm25_score, 1) }} /
            重排 {{ fmtScore(h.rerank_score, 3) }}
          </span>
        </li>
      </ul>
    </div>

    <p v-if="error" class="console-error" role="alert">
      <span class="err-mark">!</span><span>{{ error }}</span>
    </p>
  </section>
</template>

<style scoped>
.hybrid-console {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}

.principle {
  margin: 0;
  padding: 11px 14px;
  font-size: 12.5px;
  line-height: 1.65;
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

.field-label {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: uppercase;
}
.meta { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
.mono { font-family: var(--font-mono); }

/* 控制条 */
.controls { display: flex; flex-direction: column; gap: 8px; flex: 0 0 auto; }
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
.preset-chip.active { border-color: var(--data); background: color-mix(in srgb, var(--data) 14%, var(--panel)); }
.chip-hint { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.ctrl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.q-input { flex: 1 1 300px; min-width: 220px; }
.ctrl-row :deep(.el-input-number) { width: 88px; }
.num { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }

.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg-btn {
  padding: 5px 12px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--muted);
  background: var(--panel);
  border: 0;
  cursor: pointer;
}
.seg-btn + .seg-btn { border-left: 1px solid var(--line); }
.seg-btn.on { color: var(--ink); background: var(--data); }

.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); cursor: pointer; }
.chk input { accent-color: var(--data); }

.warn, .ok-note {
  display: flex;
  gap: 8px;
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.6;
  border-radius: 6px;
}
.warn {
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent);
}
.warn-mark { font-family: var(--font-mono); font-weight: 600; }
.ok-note {
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--line);
}
.warn code, .ok-note code, .foot code { font-family: var(--font-mono); font-size: 11px; }

/* 爬升轨迹 */
.journey {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 9px 12px;
  background: color-mix(in srgb, var(--data) 8%, var(--panel));
  border: 1px solid color-mix(in srgb, var(--data) 32%, var(--line));
  border-radius: 6px;
}
.journey-label { font-size: 12px; color: var(--data); }
.journey-src { font-size: 11.5px; color: var(--paper); }
.journey-steps { display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.step { color: var(--paper); }
.step em { margin-right: 4px; font-style: normal; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.step.hot { color: var(--data); font-weight: 600; }
.arrow { color: var(--muted); }
.journey-final { padding: 2px 8px; font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 9999px; }
.journey-final.win { color: var(--ink); background: var(--data); border-color: var(--data); }

/* 对照表 */
.table-wrap { flex: 1 1 auto; min-height: 0; overflow: auto; }
.empty {
  margin: 0;
  padding: 14px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
  border: 1px dashed var(--line);
  border-radius: 6px;
}
.empty b { color: var(--paper); }

.rank-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.rank-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 7px 8px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 500;
  color: var(--muted);
  text-align: left;
  text-transform: uppercase;
  background: var(--ink);
  border-bottom: 1px solid var(--line);
}
.rank-table td { padding: 8px; vertical-align: top; border-bottom: 1px solid var(--line); }
.col-rank, .col-score, .col-final { width: 1%; white-space: nowrap; }
.col-src { width: auto; }

.row-final { background: color-mix(in srgb, var(--data) 7%, transparent); }
.row-target { background: color-mix(in srgb, var(--signal) 9%, transparent); }

.src-line { display: flex; align-items: baseline; gap: 5px; }
.star { color: var(--signal); font-size: 11px; }
.src { font-size: 11px; color: var(--data); }
.src-text {
  margin: 3px 0 0;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--muted);
  word-break: break-word;
}

.rank { font-family: var(--font-mono); font-size: 13px; color: var(--paper); }
.rank.dim { color: var(--muted); opacity: 0.55; }
.mv { margin-left: 3px; font-size: 10px; }
.mv.up { color: var(--data); }
.mv.new { color: var(--signal); }
.mv.down, .mv.gone { color: var(--muted); }

.col-score { display: flex; flex-direction: column; gap: 2px; }
.sc { font-size: 10.5px; color: var(--muted); }
.sc.bm { color: color-mix(in srgb, var(--data) 70%, var(--muted)); }
.sc.rr { color: var(--signal); }

.badge {
  padding: 1px 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--data);
  border-radius: 3px;
}

.foot { margin: 0; }
.tm { margin-left: 8px; }

/* 问答 */
.answer-box {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--data);
  border-radius: 0 8px 8px 0;
}
.ans-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.ans-text {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.75;
  color: var(--paper);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 12em;
  overflow-y: auto;
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
.src-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.src-item { display: flex; align-items: baseline; gap: 8px; font-size: 11.5px; flex-wrap: wrap; }
.src-no { color: var(--signal); }
.src-name { color: var(--paper); }
.src-scores { font-size: 10.5px; color: var(--muted); }

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
  .table-wrap { min-height: 320px; }
  .ans-text { max-height: none; }
}
</style>
