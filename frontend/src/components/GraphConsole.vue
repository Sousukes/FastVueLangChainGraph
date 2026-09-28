<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import GraphView from './GraphView.vue'
import {
  useGraph,
  GRAPH_PRESETS,
  typeColor,
  chunkKey,
  type GraphNode,
  type GraphEdge,
} from '../composables/useGraph'

defineProps<{
  stage: number
  title: string
}>()

const {
  stats,
  documents,
  overview,
  entities,
  entitiesTotal,
  scope,
  limit,
  workers,
  progress,
  building,
  lastBuild,
  query,
  hops,
  topK,
  viewMode,
  result,
  askResult,
  busy,
  error,
  estimateSeconds,
  loadAll,
  loadOverview,
  loadEntities,
  runBuild,
  runSearch,
  runAsk,
  runReset,
  applyPreset,
} = useGraph()

onMounted(loadAll)

const shown = computed<{ nodes: GraphNode[]; edges: GraphEdge[] }>(() => {
  if (viewMode.value === 'result' && result.value) {
    return { nodes: result.value.nodes, edges: result.value.edges }
  }
  return overview.value ?? { nodes: [], edges: [] }
})

const showingResult = computed(
  () => viewMode.value === 'result' && !!result.value && result.value.nodes.length > 0,
)

const presetKind = computed(
  () => GRAPH_PRESETS.find((p) => p.query === query.value)?.kind ?? 'direct',
)

/** 锚定失败是最该被看见的一种结果——它意味着"图上根本没有这个入口" */
const anchorMissed = computed(
  () => !!result.value && result.value.seeds.length === 0,
)

/** 按跳数分组：1 跳是直接命中，2 跳是"跨块连出来的" */
const edgesByHop = computed(() => {
  const edges = result.value?.edges ?? []
  const one = edges.filter((e) => e.hop === 1)
  const two = edges.filter((e) => e.hop > 1)
  return { one, two }
})

const progressPct = computed(() => {
  const p = progress.value
  if (!p || !p.total) return 0
  return Math.round((p.done / p.total) * 100)
})

function toggleScope(title: string) {
  const i = scope.value.indexOf(title)
  if (i === -1) scope.value = [...scope.value, title]
  else scope.value = scope.value.filter((t) => t !== title)
}

const timingOrder = ['anchor', 'expand', 'trace']
const TIMING_LABEL: Record<string, string> = {
  anchor: '实体锚定',
  expand: '多跳扩展',
  trace: '回溯源块',
}
const timings = computed(() => {
  const t = result.value?.timings ?? {}
  return timingOrder.filter((k) => t[k] != null).map((k) => ({ key: k, ms: t[k] }))
})

function snippet(text: string, n = 110) {
  return text.replace(/\s+/g, ' ').slice(0, n)
}

const showPrompt = ref(false)
const entityFilter = ref('')

const nodeTypes = computed(() => stats.value?.entityTypes ?? [])
</script>

<template>
  <section class="graph-console" aria-label="知识图谱控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 07/08 把"找相似的文本"做到了相当好，但它们都在回答同一个问题：
      <b>哪一段文字和我的问题最像？</b>
      有一类问题它天然答不好——<b>关系型 / 多跳问题</b>。比如「重排是哪个阶段引入的」，
      语料里<b>没有任何一块</b>同时写着"重排"和"RAG 进阶"：
      一块讲 RRF，一块讲重排，各自都对，但它们之间<b>那条边从来没人写过</b>。
      本阶段做三件事：把块抽成 <code>(头, 关系, 尾)</code> 三元组、落成一张图、
      检索时沿边扩 N 跳。<b>那条边不在任何文本里，它需要被"建"出来。</b>
    </p>

    <!-- 图谱状态 -->
    <div class="stats-bar">
      <div class="stat">
        <span class="stat-num">{{ stats?.entities ?? 0 }}</span>
        <span class="stat-label">实体</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ stats?.edges ?? 0 }}</span>
        <span class="stat-label">关系边</span>
      </div>
      <div class="stat" :class="{ warn: (stats?.coverage ?? 0) < 0.2 }">
        <span class="stat-num">{{ ((stats?.coverage ?? 0) * 100).toFixed(1) }}%</span>
        <span class="stat-label">
          覆盖率 {{ stats?.extractedChunks ?? 0 }} / {{ stats?.totalChunks ?? 0 }} 块
        </span>
      </div>
      <div class="stat wide">
        <span class="stat-types">
          <span v-for="t in stats?.types ?? []" :key="t.type" class="tchip">
            <i class="dot" :style="{ background: typeColor(t.type) }"></i>
            {{ t.type }} {{ t.count }}
          </span>
        </span>
        <span class="stat-label">
          实体类型分布 ·
          <template v-if="stats?.lastBuiltAt">上次构建 {{ stats.lastBuiltAt }}</template>
          <template v-else>还没构建过</template>
        </span>
      </div>
    </div>

    <p v-if="stats && stats.coverage < 0.2" class="warn note">
      <span class="warn-mark">!</span>
      覆盖率只有 <b>{{ ((stats.coverage ?? 0) * 100).toFixed(1) }}%</b>。
      这意味着大量问题会<b>锚不到实体</b>——但请记住：<b>「图里没有」不等于「语料里没有」</b>，
      它们只说明"还没抽"。这正是图谱最容易被误读的地方。
    </p>

    <!-- 构建面板 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">① 构建图谱（抽取 = 入库时的一次性成本）</span>
        <span class="meta">
          每个块一次 LLM 调用 · 实测约 19s/块 · 并发后按 workers 摊
        </span>
      </div>

      <div class="scope">
        <button
          v-for="d in documents"
          :key="d.title"
          type="button"
          class="scope-chip"
          :class="{ on: scope.includes(d.title), full: d.extracted >= d.chunks }"
          :title="`${d.title}：${d.chunks} 块，已抽 ${d.extracted}`"
          @click="toggleScope(d.title)"
        >
          {{ d.title }}
          <span class="chip-hint">{{ d.extracted }}/{{ d.chunks }}</span>
        </button>
      </div>

      <div class="ctrl-row">
        <label class="num">
          本轮最多抽
          <el-input-number v-model="limit" size="small" :min="1" :max="200" controls-position="right" />
          块
        </label>
        <label class="num">
          并发
          <el-input-number v-model="workers" size="small" :min="1" :max="12" controls-position="right" />
        </label>
        <span class="meta">
          按当前勾选范围预计 <b class="est">{{ estimateSeconds }}s</b>
        </span>

        <el-button
          size="small"
          type="primary"
          :loading="building"
          :disabled="building || scope.length === 0"
          @click="runBuild"
        >
          {{ building ? '抽取中…' : '构建' }}
        </el-button>
        <el-button size="small" :disabled="building" @click="runReset">清空图谱</el-button>
      </div>

      <p v-if="scope.length === 0" class="hint-line">
        没勾选任何文档 = 抽<b>全语料</b>（{{ stats?.totalChunks ?? 0 }} 块）。这通常不是你想干的事。
      </p>

      <!-- 进度：这是"抽取是入库时的一次性成本"这句话唯一的证据 -->
      <div v-if="progress" class="progress">
        <div class="bar"><span class="fill" :style="{ width: `${progressPct}%` }"></span></div>
        <p class="progress-meta mono">
          {{ progress.done }} / {{ progress.total }} 块 · 当前
          <code>{{ progress.chunk || '—' }}</code>
          · 已累积 <b>{{ progress.entities }}</b> 实体 / <b>{{ progress.edges }}</b> 边
        </p>
      </div>
      <p v-if="lastBuild" class="ok-note">
        本轮完成：抽出 <b>{{ lastBuild.processed }}</b> 块 →
        新增 <b>{{ lastBuild.entities }}</b> 实体 / <b>{{ lastBuild.edges }}</b> 边，
        耗时 <b>{{ lastBuild.seconds }}s</b>（平均
        {{ (lastBuild.seconds / Math.max(1, lastBuild.processed)).toFixed(2) }}s/块）。
      </p>
    </div>

    <!-- 检索面板 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">② 多跳检索（锚定 → 扩展 → 回溯源块）</span>
        <span class="meta">查询时只查 SQLite，不调模型 —— 贵在前，便宜在后</span>
      </div>

      <div class="presets">
        <button
          v-for="p in GRAPH_PRESETS"
          :key="p.label"
          type="button"
          class="preset-chip"
          :class="{ active: query === p.query, miss: p.kind === 'miss' }"
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
          placeholder="问一个关系型问题，比如「重排是哪个阶段引入的」"
          class="q-input"
          @keyup.enter="runSearch"
        />

        <div class="seg" role="radiogroup" aria-label="扩展跳数">
          <button
            v-for="h in ([1, 2, 3] as const)"
            :key="h"
            type="button"
            class="seg-btn"
            :class="{ on: hops === h }"
            :title="
              h === 1
                ? '只看直接相连的实体：精确，但覆盖窄'
                : h === 3
                  ? '三跳：能连出很远的关系，但噪声成倍增长'
                  : '两跳：跨块连出新关系，教学上的默认值'
            "
            @click="hops = h"
          >
            {{ h }} 跳
          </button>
        </div>

        <label class="num">
          源块
          <el-input-number v-model="topK" size="small" :min="1" :max="10" controls-position="right" />
        </label>

        <el-button size="small" type="primary" :loading="busy === 'search'" :disabled="!query.trim()" @click="runSearch">
          检索
        </el-button>
        <el-button size="small" :loading="busy === 'ask'" :disabled="!query.trim()" @click="runAsk">
          问模型
        </el-button>
      </div>

      <!-- 锚定结果：整条链路的入口。入口错了，后面全错 -->
      <div v-if="result" class="anchors" :class="{ miss: anchorMissed }">
        <span class="field-label">锚定实体</span>
        <template v-if="result.seeds.length">
          <span v-for="s in result.seeds" :key="s" class="seed-chip">{{ s }}</span>
        </template>
        <template v-else>
          <span class="seed-empty">
            图上没有这个问题的入口实体 —— <b>整条链路空转</b>。
            这不是 bug，是图谱覆盖率（{{ ((stats?.coverage ?? 0) * 100).toFixed(1) }}%）的直接后果：
            要么换个说法，要么先把相关文档抽进图里。
          </span>
        </template>
      </div>
    </div>

    <!-- 图 -->
    <div class="panel graph-panel">
      <div class="panel-head">
        <span class="field-label">③ 图谱</span>
        <div class="seg">
          <button
            type="button"
            class="seg-btn"
            :class="{ on: viewMode === 'result' }"
            :disabled="!result"
            @click="viewMode = 'result'"
          >
            检索子图
          </button>
          <button
            type="button"
            class="seg-btn"
            :class="{ on: viewMode === 'overview' }"
            title="按连接度取最枢纽的若干节点，截断后画出来"
            @click="
              () => {
                viewMode = 'overview'
                loadOverview()
              }
            "
          >
            全图（截断）
          </button>
        </div>
        <span class="meta">
          {{ shown.nodes.length }} 节点 / {{ shown.edges.length }} 边 ·
          琥珀 = 1 跳直接命中 · 青绿 = 多跳 · 圆越大 = 连接度越高
        </span>
      </div>

      <GraphView :nodes="shown.nodes" :edges="shown.edges" :height="420" />

      <p class="meta legend-line">
        为什么全图要<b>截断</b>：{{ stats?.entities ?? 0 }} 个节点画在屏幕上只是一团毛线，
        <b>信息量为零</b>。所以只按连接度取最枢纽的若干节点 —— 一张能看懂的图才有价值。
      </p>
    </div>

    <!-- 关系链：边 + 出处 -->
    <div v-if="result && result.edges.length" class="panel">
      <div class="panel-head">
        <span class="field-label">④ 关系链（每条边都带出处）</span>
        <span class="meta">
          没有出处的三元组 = 没有引用的断言，模型会把它当事实写进答案
        </span>
      </div>

      <div v-for="grp in [{ label: '1 跳 · 直接命中', list: edgesByHop.one }, { label: '多跳 · 跨块连出来的', list: edgesByHop.two }]" :key="grp.label">
        <template v-if="grp.list.length">
          <p class="hop-label">{{ grp.label }}（{{ grp.list.length }}）</p>
          <ul class="edge-list">
            <li v-for="e in grp.list" :key="e.id" class="edge-item" :class="{ hot: e.hop === 1 }">
              <span class="triple">
                <span class="ent">{{ e.head }}</span>
                <span class="rel-word">—{{ e.relation }}→</span>
                <span class="ent">{{ e.tail }}</span>
              </span>
              <span class="src mono">出处 {{ e.sourceTitle }}#{{ e.sourceIndex }}</span>
            </li>
          </ul>
        </template>
      </div>
    </div>

    <!-- 回溯到的源块 -->
    <div v-if="result && result.chunks.length" class="panel">
      <div class="panel-head">
        <span class="field-label">⑤ 回溯到的源块</span>
        <span class="meta">按"被多少条边引用"排序 —— 出现越频繁的块越可能是枢纽</span>
      </div>
      <ul class="chunk-list">
        <li v-for="(c, i) in result.chunks" :key="chunkKey(c)" class="chunk-item">
          <div class="chunk-head">
            <span class="src-no mono">[{{ i + 1 }}]</span>
            <span class="src mono">{{ chunkKey(c) }}</span>
            <span class="badge">{{ c.edgeCount }} 条边引用</span>
          </div>
          <p class="chunk-text">{{ snippet(c.text, 220) }}</p>
        </li>
      </ul>
    </div>

    <p v-if="result" class="meta foot">
      查询：{{ result.query }} · 跳数 <code>{{ result.hops }}</code> ·
      节点 <code>{{ result.nodes.length }}</code> · 边 <code>{{ result.edges.length }}</code> ·
      耗时
      <span v-for="t in timings" :key="t.key" class="tm">
        {{ TIMING_LABEL[t.key] }} {{ t.ms }}ms
      </span>
    </p>

    <!-- 问答 -->
    <div v-if="askResult" class="answer-box">
      <div class="ans-head">
        <span class="field-label">模型回答</span>
        <span class="meta">
          {{ askResult.edges.length }} 条关系 · {{ askResult.chunks.length }} 段原文 ·
          <code>{{ askResult.model }}</code>
        </span>
      </div>
      <p class="ans-text">{{ askResult.answer }}<span class="caret-done"></span></p>

      <button type="button" class="prompt-toggle" @click="showPrompt = !showPrompt">
        {{ showPrompt ? '收起' : '展开' }}实际发给模型的 prompt
        <span class="meta">（边让模型看清关系，原文让它有据可依 —— 两个都要给）</span>
      </button>
      <pre v-if="showPrompt" class="prompt mono">{{ askResult.prompt }}</pre>
    </div>

    <!-- 实体清单（顺手给个"图里到底有什么"的入口） -->
    <details class="panel entity-panel">
      <summary class="field-label">
        实体清单（共 {{ entitiesTotal }} 个，按提及次数排序）
      </summary>
      <div class="ctrl-row">
        <el-input
          v-model="entityFilter"
          size="small"
          placeholder="按名字过滤"
          class="q-input"
          @keyup.enter="loadEntities(entityFilter)"
        />
        <span class="meta">回车过滤 · 实体类型词表：{{ nodeTypes.join(' / ') }}</span>
      </div>
      <ul class="entity-list">
        <li v-for="e in entities" :key="e.name" class="entity-item">
          <i class="dot" :style="{ background: typeColor(e.type) }"></i>
          <span class="ent-name">{{ e.name }}</span>
          <span class="meta">{{ e.type }}</span>
          <span class="meta mono">提及 {{ e.mentions }} · 度 {{ e.degree }}</span>
        </li>
      </ul>
    </details>

    <p v-if="error" class="console-error" role="alert">
      <span class="err-mark">!</span><span>{{ error }}</span>
    </p>
  </section>
</template>

<style scoped>
.graph-console {
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
.principle code, .note code, .foot code, .progress-meta code {
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

/* 状态条 */
.stats-bar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.stat { display: flex; flex-direction: column; gap: 2px; padding-right: 16px; border-right: 1px solid var(--line); }
.stat:last-child { border-right: 0; }
.stat.wide { flex: 1 1 260px; }
.stat-num { font-family: var(--font-display); font-size: 20px; font-weight: 700; color: var(--paper); }
.stat.warn .stat-num { color: var(--signal); }
.stat-label { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.stat-types { display: flex; flex-wrap: wrap; gap: 8px; }
.tchip { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--paper); }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; }

.warn.note, .ok-note, .hint-line {
  margin: 0;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.6;
  border-radius: 6px;
}
.warn.note {
  display: flex;
  gap: 8px;
  color: var(--signal);
  background: color-mix(in srgb, var(--signal) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--signal) 40%, transparent);
}
.warn-mark { font-family: var(--font-mono); font-weight: 600; }
.warn.note b { color: var(--signal); }
.ok-note {
  color: var(--muted);
  background: color-mix(in srgb, var(--data) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--data) 28%, transparent);
}
.ok-note b { color: var(--data); }
.hint-line { color: var(--muted); background: var(--panel-2); border: 1px solid var(--line); }
.hint-line b { color: var(--signal); }

/* 面板 */
.panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.graph-panel { padding-bottom: 8px; }

/* 控制件 */
.ctrl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.q-input { flex: 1 1 280px; min-width: 200px; }
.ctrl-row :deep(.el-input-number) { width: 92px; }
.num { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.est { color: var(--signal); font-family: var(--font-mono); }

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
.preset-chip.miss { border-style: dashed; }
.chip-hint { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.scope { display: flex; flex-wrap: wrap; gap: 6px; max-height: 104px; overflow-y: auto; }
.scope-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  font-size: 11.5px;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  cursor: pointer;
}
.scope-chip:hover { border-color: var(--data); }
.scope-chip.on { color: var(--paper); border-color: var(--data); background: color-mix(in srgb, var(--data) 14%, var(--panel-2)); }
.scope-chip.full { opacity: 0.55; }
.scope-chip.full .chip-hint { color: var(--data); }

.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg-btn {
  padding: 5px 12px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--muted);
  background: var(--panel-2);
  border: 0;
  cursor: pointer;
}
.seg-btn + .seg-btn { border-left: 1px solid var(--line); }
.seg-btn.on { color: var(--ink); background: var(--signal); }
.seg-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* 进度 */
.progress { display: flex; flex-direction: column; gap: 6px; }
.bar { height: 6px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 9999px; overflow: hidden; }
.fill { display: block; height: 100%; background: var(--signal); transition: width 0.25s ease; }
.progress-meta { margin: 0; font-size: 11px; color: var(--muted); }
.progress-meta b { color: var(--data); }

/* 锚定 */
.anchors {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 10px;
  background: color-mix(in srgb, var(--signal) 8%, var(--panel-2));
  border: 1px solid color-mix(in srgb, var(--signal) 30%, var(--line));
  border-radius: 6px;
}
.anchors.miss { background: var(--panel-2); border-style: dashed; border-color: var(--line); }
.seed-chip {
  padding: 2px 9px;
  font-size: 12px;
  color: var(--ink);
  background: var(--signal);
  border-radius: 9999px;
}
.seed-empty { font-size: 11.5px; line-height: 1.6; color: var(--muted); }
.seed-empty b { color: var(--signal); }

.legend-line { margin: 0; }
.legend-line b { color: var(--paper); }

/* 关系链 */
.hop-label { margin: 4px 0 4px; font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); }
.edge-list { list-style: none; margin: 0 0 6px; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.edge-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 4px 8px;
  font-size: 12px;
  border-left: 2px solid var(--line);
  background: var(--panel-2);
}
.edge-item.hot { border-left-color: var(--signal); }
.triple { display: inline-flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.ent { color: var(--paper); }
.rel-word { font-family: var(--font-mono); font-size: 11px; color: var(--data); }
.edge-item.hot .rel-word { color: var(--signal); }
.src { font-size: 10.5px; color: var(--muted); }

/* 源块 */
.chunk-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.chunk-item { padding: 8px 10px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; }
.chunk-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.src-no { color: var(--signal); font-size: 11px; }
.chunk-item .src { font-size: 11px; color: var(--data); }
.badge {
  padding: 1px 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--data);
  border-radius: 3px;
}
.chunk-text { margin: 5px 0 0; font-size: 11.5px; line-height: 1.6; color: var(--muted); }

.foot { margin: 0; }
.tm { margin-left: 8px; }

/* 问答 */
.answer-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-left: 3px solid var(--signal);
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
.prompt-toggle {
  align-self: flex-start;
  padding: 3px 0;
  font-size: 11.5px;
  color: var(--data);
  background: none;
  border: 0;
  cursor: pointer;
}
.prompt-toggle:hover { text-decoration: underline; }
.prompt {
  margin: 0;
  max-height: 260px;
  overflow: auto;
  padding: 10px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--paper);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 实体清单 */
.entity-panel summary { cursor: pointer; }
.entity-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 4px;
  max-height: 260px;
  overflow-y: auto;
}
.entity-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 3px 6px;
  font-size: 11.5px;
  background: var(--panel-2);
  border-radius: 4px;
}
.ent-name { color: var(--paper); }

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
</style>
