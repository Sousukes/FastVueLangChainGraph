<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useAgentic, AGENTIC_PRESETS } from '../composables/useAgentic'

defineProps<{ stage: number; title: string }>()

const a = useAgentic()

onMounted(() => a.loadChannels())

const canRun = computed(() => !!a.question.value.trim() && !a.running.value)

function applyPreset(q: string) {
  a.question.value = q
}

const CHANNEL_TAG: Record<string, string> = {
  vector: '阶段 07',
  hybrid: '阶段 08',
  graph: '阶段 09',
}
</script>

<template>
  <section class="agentic-console" aria-label="Agentic RAG 控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      阶段 07/08 的 RAG 是一条<b>固定管道</b>：无论如何都检索 top-k、拼进 prompt、生成。它有三个
      <b>静默失败</b>的缺口——不该查的也查、检索失手没人知道、没人问过"这几段够不够"。
      本阶段把这三处硬编码换成三个可被审视的决策：<b>①要不要检索 ②够不够用 ③不够怎么办</b>。
      看下面这条链路，重点不是"它用了智能体"，而是<b>检索从管道变成了带反馈回路的控制系统</b>。
    </p>

    <!-- 状态条 -->
    <div class="stats-bar">
      <div class="stat">
        <span class="stat-num">
          <template v-if="!a.routeDecision.value">—</template>
          <template v-else-if="a.retrievalSkipped.value">跳过</template>
          <template v-else>执行</template>
        </span>
        <span class="stat-label">是否检索</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ a.usedRounds.value }}<i class="of">/ {{ a.maxRounds.value }}</i></span>
        <span class="stat-label">检索轮次</span>
      </div>
      <div class="stat">
        <span class="stat-num mono">{{ a.basedOn.value }}</span>
        <span class="stat-label">最终依靠的通道</span>
      </div>
      <div class="stat">
        <span class="stat-num">{{ (a.totalMs.value / 1000).toFixed(1) }}<i class="of">s</i></span>
        <span class="stat-label">总耗时 · LLM {{ (a.llmMs.value / 1000).toFixed(1) }}s</span>
      </div>
      <div v-if="a.model.value" class="stat wide">
        <span class="stat-types">{{ a.model.value }}</span>
        <span class="stat-label">模型</span>
      </div>
    </div>

    <!-- ① 检索计划 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">① 检索计划：每次重试会换一种检索方式</span>
      </div>
      <div class="channels">
        <div v-for="c in a.channels.value" :key="c.name" class="ch-card">
          <header class="ch-head">
            <span class="ch-round">第 {{ c.round }} 轮</span>
            <b>{{ c.label }}</b>
            <span class="ch-tag">{{ CHANNEL_TAG[c.name] ?? '' }}</span>
          </header>
          <p class="ch-why">{{ c.why }}</p>
        </div>
        <div v-if="!a.channels.value.length" class="ch-empty">尚未读取到通道清单</div>
      </div>
      <p class="hint-line">
        ⭐ 这是本阶段最值得记住的一点：<b>被反思的不只是检索词，还可以是"检索方式"本身</b>。
        前三阶段的检索资产因此各归其位，而不是被一个"更强的模型"替代掉。
      </p>
    </div>

    <!-- ② 提问 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">② 提问 · 运行</span>
      </div>

      <div class="presets">
        <button
          v-for="p in AGENTIC_PRESETS"
          :key="p.label"
          type="button"
          class="preset-btn"
          :title="p.hint"
          @click="applyPreset(p.question)"
        >
          {{ p.label }}
        </button>
      </div>

      <div class="ask-row">
        <el-input
          v-model="a.question.value"
          size="default"
          placeholder="问一个可能需要查资料、也可能不需要的问题"
          @keyup.enter="canRun && a.run()"
        />
        <el-button size="default" type="primary" :loading="a.running.value" :disabled="!canRun" @click="a.run()">
          {{ a.running.value ? '决策中…' : '运行' }}
        </el-button>
        <el-button size="default" :disabled="a.running.value" @click="a.reset()">清空</el-button>
      </div>

      <div class="params">
        <label class="param">
          <span>最多轮次</span>
          <el-input-number v-model="a.maxRounds.value" size="small" :min="1" :max="3" controls-position="right" />
        </label>
        <label class="param">
          <span>每轮 topK</span>
          <el-input-number v-model="a.topK.value" size="small" :min="1" :max="10" controls-position="right" />
        </label>
        <label class="param">
          <span>图谱跳数</span>
          <el-input-number v-model="a.hops.value" size="small" :min="1" :max="3" controls-position="right" />
        </label>
      </div>
    </div>

    <p v-if="a.error.value" class="err-line">⚠️ {{ a.error.value }}</p>

    <!-- ③ 决策链路 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">③ 决策链路</span>
      </div>

      <!-- 出口 ① -->
      <div v-if="a.routeDecision.value" class="decision" :class="{ skip: a.retrievalSkipped.value }">
        <span class="d-badge">① 路由</span>
        <span class="d-verdict">{{ a.retrievalSkipped.value ? '无需检索 → 直接生成' : '需要检索 → 进入回路' }}</span>
        <span class="d-ms">{{ a.routeDecision.value.ms }}ms</span>
        <p class="d-reason">{{ a.routeDecision.value.reason }}</p>
        <p v-if="a.routeDecision.value.degraded" class="d-degraded">
          降级：路由结果没能解析成结构化对象，已保守地按「照常检索」处理。
        </p>
      </div>

      <p v-if="a.retrievalSkipped.value" class="skip-note">
        ⭐ 这就是省下来的那一刀：这一步的白屏等于过去偷偷花掉的 token 与延迟。
        <b>「多查总没错」是错的</b>——不必要的检索会引入噪声，还会把模型带偏。
      </p>

      <!-- 每一轮 -->
      <div v-for="r in a.rounds.value" :key="r.round" class="round">
        <header class="r-head">
          <span class="r-no">第 {{ r.round }} 轮</span>
          <span class="r-ch">{{ r.channelLabel || r.channel }}</span>
          <span class="r-ms">检索 {{ r.retrieveMs }}ms · 评级 {{ r.gradeMs }}ms</span>
        </header>

        <p class="r-query">
          <span class="r-k">检索词</span><code>{{ r.query }}</code>
        </p>

        <!-- 出口 ② -->
        <div v-if="r.useful !== null" class="verdict" :class="r.useful ? 'ok' : 'bad'">
          <span class="v-badge">② 评级</span>
          <span class="v-text">
            {{ r.useful ? `够用，留下 ${r.kept.length} 段` : `不够（留下 ${r.kept.length} 段）` }}
          </span>
          <span v-if="!r.useful && r.missing" class="v-missing">缺：{{ r.missing }}</span>
        </div>

        <!-- 命中片段 -->
        <ul class="hits">
          <li
            v-for="(h, i) in r.hits"
            :key="`${h.title}-${h.index}`"
            class="hit"
            :class="{ kept: r.kept.includes(i), dropped: r.useful === false && !r.kept.includes(i) }"
          >
            <span class="h-src">《{{ h.title }}》第 {{ h.index }} 段</span>
            <span v-if="typeof h.score === 'number'" class="h-score">{{ h.score.toFixed(3) }}</span>
            <span v-else-if="h.edgeCount" class="h-score">边 {{ h.edgeCount }}</span>
            <span class="h-mark">{{ r.kept.includes(i) ? '留下' : '丢弃' }}</span>
            <p class="h-text">{{ h.text }}</p>
          </li>
        </ul>

        <!-- 出口 ③ -->
        <div v-if="r.rewriteTo" class="rewrite">
          <span class="rw-badge">③ 改写</span>
          <span class="rw-flow"><code>{{ r.query }}</code> → <code class="to">{{ r.rewriteTo }}</code></span>
          <p v-if="r.rewriteWhy" class="rw-why">为什么：{{ r.rewriteWhy }}</p>
          <p class="rw-next">
            下一轮改用：<b>{{ a.channels.value.find((c) => c.round === r.round + 1)?.label ?? '—' }}</b>
          </p>
        </div>
      </div>

      <p v-if="!a.routeDecision.value && !a.rounds.value.length" class="empty">
        运行之后，三个决策点会依次出现在这里。
      </p>
    </div>

    <!-- ④ 答案 -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">④ 最终答案</span>
        <span v-if="a.finalQuery.value && a.finalQuery.value !== a.question.value" class="mini">
          最后用的检索词：{{ a.finalQuery.value }}
        </span>
      </div>
      <div class="answer">
        <template v-if="a.answer.value">
          <span class="ans-text">{{ a.answer.value }}</span>
          <i v-if="a.running.value" class="caret"></i>
        </template>
        <span v-else class="ans-empty">尚无输出</span>
      </div>
      <div v-if="a.keptHits.value.length" class="cites">
        <span class="cite-label">引用依据</span>
        <span v-for="(h, i) in a.keptHits.value" :key="`c-${h.title}-${h.index}`" class="cite">
          《{{ h.title }}》第 {{ h.index }} 段
        </span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.agentic-console {
  --c-ok: #5bc8b0;
  --c-bad: #ff7a72;
  --c-skip: #8b95a7;
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
.stat-num { font-family: var(--font-display); font-size: 17px; color: var(--data); }
.stat-num.mono { font-family: var(--font-mono); font-size: 13px; }
.stat-num .of { font-style: normal; font-size: 11px; color: var(--muted); }
.stat-label { font-size: 10px; color: var(--muted); }
.stat-types { font-family: var(--font-mono); font-size: 11px; color: var(--paper); }

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
.mini { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.hint-line { margin: 8px 0 0; font-size: 11.5px; line-height: 1.6; color: var(--muted); }
.hint-line b { color: var(--paper); }

.channels { display: flex; flex-wrap: wrap; gap: 8px; }
.ch-card {
  flex: 1 1 220px;
  padding: 9px 11px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.ch-head { display: flex; align-items: center; gap: 7px; margin-bottom: 5px; }
.ch-round {
  padding: 1px 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--data);
  border-radius: 3px;
}
.ch-head b { font-size: 12.5px; color: var(--paper); }
.ch-tag { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.ch-why { margin: 0; font-size: 11.5px; line-height: 1.55; color: var(--muted); }
.ch-empty { font-size: 11.5px; color: var(--muted); }

.presets { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.preset-btn {
  padding: 4px 9px;
  font-family: var(--font-body);
  font-size: 11.5px;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 4px;
  cursor: pointer;
}
.preset-btn:hover { color: var(--paper); border-color: var(--signal); }

.ask-row { display: flex; gap: 8px; }
.ask-row :deep(.el-input) { flex: 1 1 auto; }

.params { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 9px; }
.param { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }
.param :deep(.el-input-number) { width: 92px; }

.err-line {
  margin: 0;
  padding: 8px 11px;
  font-size: 12px;
  color: var(--c-bad);
  background: var(--panel);
  border: 1px solid var(--c-bad);
  border-radius: 6px;
}

/* ---------- 决策链路 ---------- */
.decision,
.round,
.verdict,
.rewrite {
  border-radius: 6px;
}
.decision {
  position: relative;
  margin-bottom: 10px;
  padding: 9px 11px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-left: 3px solid var(--signal);
}
.decision.skip { border-left-color: var(--c-skip); }
.d-badge,
.v-badge,
.rw-badge {
  display: inline-block;
  margin-right: 7px;
  padding: 1px 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--signal);
  border-radius: 3px;
  vertical-align: 1px;
}
.d-verdict { font-size: 12.5px; font-weight: 600; color: var(--paper); }
.decision.skip .d-verdict { color: var(--muted); }
.decision.skip .d-badge { background: var(--c-skip); }
.d-ms { margin-left: 8px; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.d-reason { margin: 6px 0 0; font-size: 11.5px; line-height: 1.6; color: var(--muted); }
.d-degraded { margin: 5px 0 0; font-size: 11px; color: var(--signal); }

.skip-note {
  margin: 0 0 10px;
  padding: 8px 11px;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--muted);
  background: var(--panel-2);
  border: 1px dashed var(--line);
  border-radius: 6px;
}
.skip-note b { color: var(--paper); }

.round {
  margin-bottom: 10px;
  padding: 10px 11px;
  background: var(--panel-2);
  border: 1px solid var(--line);
}
.r-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 7px; }
.r-no {
  padding: 1px 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink);
  background: var(--data);
  border-radius: 3px;
}
.r-ch { font-size: 12.5px; font-weight: 600; color: var(--paper); }
.r-ms { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }

.r-query { display: flex; gap: 6px; margin: 0 0 8px; font-size: 11.5px; color: var(--muted); }
.r-k { flex: 0 0 auto; }
.r-query code {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--data);
  word-break: break-all;
}

.verdict { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-bottom: 8px; }
.verdict.ok .v-badge { background: var(--c-ok); }
.verdict.bad .v-badge { background: var(--c-bad); }
.v-text { font-size: 12px; font-weight: 600; }
.verdict.ok .v-text { color: var(--c-ok); }
.verdict.bad .v-text { color: var(--c-bad); }
.v-missing { font-size: 11px; color: var(--muted); }

.hits { margin: 0 0 8px; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.hit {
  padding: 7px 9px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 5px;
  opacity: 0.55;
}
.hit.kept { opacity: 1; border-color: var(--c-ok); }
.hit.dropped { border-color: var(--c-bad); }
.h-src { font-family: var(--font-mono); font-size: 10.5px; color: var(--data); }
.h-score { margin-left: 7px; font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
.h-mark {
  float: right;
  padding: 0 6px;
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 3px;
}
.hit.kept .h-mark { color: var(--c-ok); border-color: var(--c-ok); }
.hit.dropped .h-mark { color: var(--c-bad); border-color: var(--c-bad); }
.h-text {
  margin: 5px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--muted);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.rewrite {
  padding: 8px 10px;
  background: var(--ink);
  border: 1px dashed var(--signal);
}
.rw-badge { background: var(--signal); }
.rw-flow { font-size: 11.5px; color: var(--muted); }
.rw-flow code { font-family: var(--font-mono); font-size: 11px; color: var(--muted); }
.rw-flow code.to { color: var(--data); }
.rw-why { margin: 5px 0 0; font-size: 11.5px; color: var(--muted); }
.rw-next { margin: 4px 0 0; font-size: 11.5px; color: var(--muted); }
.rw-next b { color: var(--signal); }

.empty { margin: 0; font-size: 11.5px; color: var(--muted); }

.answer {
  min-height: 64px;
  padding: 11px 13px;
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--paper);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.ans-text { white-space: pre-wrap; }
.ans-empty { color: var(--muted); font-size: 12px; }

.cites { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 8px; }
.cite-label { font-size: 10px; color: var(--muted); }
.cite {
  padding: 1px 7px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--data);
  border: 1px solid var(--line);
  border-radius: 3px;
}
</style>
