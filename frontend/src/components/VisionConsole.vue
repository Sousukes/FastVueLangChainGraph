<script setup lang="ts">
import { computed, ref } from 'vue'
import { useVision, type VisionMode } from '../composables/useVision'

defineProps<{ stage: number; title: string }>()

const s = useVision()

const fileInput = ref<HTMLInputElement | null>(null)

const MODES: { value: VisionMode; label: string; hint: string }[] = [
  { value: 'qa', label: '视觉问答', hint: '看图回答一个问题' },
  { value: 'extract', label: '结构化抽取', hint: '把图里的信息抽成字段表' },
]

const DETAILS: { value: 'auto' | 'low' | 'high'; label: string }[] = [
  { value: 'auto', label: 'auto（自动）' },
  { value: 'low', label: 'low（省 token）' },
  { value: 'high', label: 'high（更精细）' },
]

/** 尺寸文本：服务端读文件头拿到的宽高，读不到就如实说"未知" */
const dimText = computed(() => {
  const m = s.imageMeta.value
  if (!m) return '—'
  if (m.width && m.height) return `${m.width} × ${m.height} px`
  return '未知（文件头未给出）'
})

const formatLabel = computed(() => (s.imageMeta.value?.format ?? '').toUpperCase())
const modeHint = computed(() => MODES.find((m) => m.value === s.mode.value)?.hint ?? '')

function pick() {
  fileInput.value?.click()
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) s.loadFile(file)
  // 允许连续选择同一张图
  input.value = ''
}

function onDrop(e: DragEvent) {
  const file = e.dataTransfer?.files?.[0]
  if (file) s.loadFile(file)
}

function onPaste(e: ClipboardEvent) {
  const item = Array.from(e.clipboardData?.items ?? []).find((i) => i.type.startsWith('image/'))
  const file = item?.getAsFile()
  if (file) {
    s.loadFile(file)
  } else {
    s.error.value = '剪贴板里没有图片（试着先截图或复制一张图）'
  }
}

function onKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && s.canRun.value) s.run()
}
</script>

<template>
  <section class="vision-console" aria-label="图像理解控制台">
    <p class="principle">
      <span class="principle-mark">为什么</span>
      前面所有阶段，模型看到的都只有文字。本阶段把图片也变成一种输入——而秘密
      <b>不在模型调用，而在消息结构</b>：阶段 03 起 <span class="ul">messages</span> 里那条 user 消息的
      <span class="ul">content</span>，从「字符串」升级成了「文本块 + 图像块」的数组。协议层零改动。
      在此之前，服务端先做一道<span class="ul">确定性解析</span>：用<b>魔数</b>识破伪造的格式、量清体积、读出尺寸。
    </p>

    <!-- ⭐ 签名元素：图画布（拖入 / 粘贴 / 点选） -->
    <div class="vision-hero">
      <div
        v-if="!s.image.value"
        class="drop"
        tabindex="0"
        role="button"
        aria-label="选择图片"
        @click="pick"
        @keydown.enter.prevent="pick"
        @keydown.space.prevent="pick"
        @paste="onPaste"
        @dragover.prevent
        @drop.prevent="onDrop"
      >
        <span class="drop-glyph" aria-hidden="true">▣</span>
        <p class="drop-title">拖入 / 粘贴 / 点击，选一张图片</p>
        <p class="drop-sub">PNG · JPEG · GIF · WebP，≤ 8 MB（图片只在本地读取，随即交给后端解析）</p>
      </div>

      <div v-else class="stage">
        <div class="frame">
          <img class="preview" :src="s.image.value" alt="待理解的图片" />
          <span class="tick tl"></span><span class="tick tr"></span>
          <span class="tick bl"></span><span class="tick br"></span>
        </div>
        <div class="stage-side">
          <p class="stage-name" :title="s.fileName.value">
            {{ s.fileName.value || '剪贴板图片' }}
          </p>
          <div class="stage-actions">
            <el-button size="small" @click="pick">更换</el-button>
            <el-button size="small" text @click="s.clearImage()">移除</el-button>
          </div>
        </div>
      </div>

      <input
        ref="fileInput"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        class="hidden-input"
        @change="onPick"
      />

      <!-- 模式切换（自定义分段控件，配色即本阶段记忆点） -->
      <div class="mode-row">
        <div class="seg" role="tablist" aria-label="理解模式">
          <button
            v-for="m in MODES"
            :key="m.value"
            type="button"
            class="seg-item"
            :class="{ active: s.mode.value === m.value }"
            role="tab"
            :aria-selected="s.mode.value === m.value"
            @click="s.mode.value = m.value"
          >
            {{ m.label }}
          </button>
        </div>
        <label class="param">
          <span>精细度</span>
          <el-select v-model="s.detail.value" size="small" style="width: 132px">
            <el-option v-for="d in DETAILS" :key="d.value" :label="d.label" :value="d.value" />
          </el-select>
        </label>
        <span class="hint">{{ modeHint }}</span>
      </div>

      <!-- 提问 / 抽取要求 -->
      <div class="ask-row" @keydown="onKeydown">
        <textarea
          v-if="s.mode.value === 'qa'"
          v-model="s.question.value"
          class="ask-input"
          rows="2"
          placeholder="问一个关于这张图的问题，比如「这张图里有哪些数据，分别是什么？」"
        />
        <textarea
          v-else
          v-model="s.schemaHint.value"
          class="ask-input"
          rows="2"
          placeholder="（可选）说明要抽哪些字段，比如「抽取表格里的所有行列，以及标题和单位」"
        />
      </div>

      <div class="actions">
        <el-button
          type="primary"
          size="default"
          :loading="s.running.value"
          :disabled="!s.canRun.value"
          @click="s.run()"
        >
          {{ s.running.value ? '理解中…' : '理解这张图' }}
        </el-button>
        <el-button size="small" text :disabled="s.running.value" @click="s.reset()">清空</el-button>
        <span class="hint">Ctrl/⌘ + Enter 开始</span>
      </div>
    </div>

    <p v-if="s.error.value" class="err-line">⚠️ {{ s.error.value }}</p>

    <!-- 体检单：服务端嗅探出的客观元数据 -->
    <div v-if="s.imageMeta.value" class="panel meta-panel">
      <div class="panel-head">
        <span class="field-label">图像体检单 · 服务端解码得到</span>
        <span v-if="!s.imageMeta.value.mismatch" class="ok-badge">未发现异常</span>
      </div>
      <div class="meta-strip">
        <div class="meta">
          <span class="meta-k">真实格式</span>
          <span class="meta-v">{{ formatLabel }}</span>
        </div>
        <div class="meta">
          <span class="meta-k">尺寸</span>
          <span class="meta-v">{{ dimText }}</span>
        </div>
        <div class="meta">
          <span class="meta-k">体积</span>
          <span class="meta-v">{{ s.humanBytes(s.imageMeta.value.bytes) }}</span>
        </div>
        <div class="meta">
          <span class="meta-k">真实 mime</span>
          <span class="meta-v mono">{{ s.imageMeta.value.mime }}</span>
        </div>
      </div>
      <p v-if="s.imageMeta.value.mismatch" class="mismatch">
        ⚠ 声明的是 <code>{{ s.imageMeta.value.declared }}</code>，但魔数显示真实格式是
        <code>{{ s.imageMeta.value.mime }}</code> —— 已按<b>真实格式</b>处理（改扩展名骗不过魔数）。
      </p>
    </div>

    <template v-if="s.answer.value || s.running.value">
      <!-- qa：流式解读 -->
      <div v-if="s.mode.value === 'qa'" class="panel">
        <div class="panel-head">
          <span class="field-label">图像解读 · 流式</span>
          <span class="ok-badge">{{ s.model.value || '—' }}</span>
        </div>
        <div class="answer">{{ s.answer.value }}<i v-if="s.running.value" class="caret"></i></div>
      </div>

      <!-- extract：字段表 -->
      <div v-else class="panel">
        <div class="panel-head">
          <span class="field-label">结构化字段表</span>
          <span class="extract-badge" :class="{ ok: s.extracted.value }">
            {{ s.extracted.value ? `已解析 ${s.fields.value.length} 字段` : '未解析为 JSON' }}
          </span>
        </div>
        <p v-if="s.summary.value" class="summary">{{ s.summary.value }}</p>

        <table v-if="s.fields.value.length" class="fields">
          <thead>
            <tr>
              <th class="col-k">字段</th>
              <th>值（图中原文）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(f, i) in s.fields.value" :key="i">
              <td class="col-k">{{ f.label }}</td>
              <td class="col-v">{{ f.value }}</td>
            </tr>
          </tbody>
        </table>

        <pre v-else class="raw">{{ s.answer.value }}<i v-if="s.running.value" class="caret"></i></pre>

        <details v-if="s.fields.value.length" class="raw-details">
          <summary>查看模型原始输出（JSON）</summary>
          <pre class="raw">{{ s.answer.value }}</pre>
        </details>
      </div>

      <!-- 耗时 -->
      <div class="stats-bar">
        <div class="stat">
          <span class="stat-num">{{ s.humanBytes(s.imageMeta.value?.bytes ?? 0) }}</span>
          <span class="stat-label">图像体积</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ (s.times.value['encode'] ?? 0).toFixed(1) }}<i class="of">ms</i></span>
          <span class="stat-label">解码（确定性）</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ (s.llmMs.value / 1000).toFixed(1) }}<i class="of">s</i></span>
          <span class="stat-label">模型调用</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ (s.totalMs.value / 1000).toFixed(1) }}<i class="of">s</i></span>
          <span class="stat-label">总耗时</span>
        </div>
      </div>
    </template>

    <p v-else class="empty">
      选一张图片，切到「视觉问答」问个问题，或切到「结构化抽取」把图里的信息抽成字段表。
      提交后服务端会先做一遍确定性解码（格式 / 尺寸 / 体积），再交给模型。
    </p>
  </section>
</template>

<style scoped>
.vision-console {
  --accent: #ec6e9e; /* 品红：本阶段「图像理解」的主调，区别于阶段 15 的紫 */
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
.principle .ul { color: var(--accent); font-weight: 600; }
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

/* ---------- 签名元素：图画布 ---------- */
.vision-hero { display: flex; flex-direction: column; gap: 10px; }

.drop {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-height: 148px;
  padding: 20px;
  text-align: center;
  background: var(--panel);
  border: 1.5px dashed var(--line);
  border-radius: 10px;
  cursor: pointer;
  outline: none;
  transition: border-color 0.18s ease, background 0.18s ease;
}
.drop:hover,
.drop:focus-visible {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, var(--panel));
}
.drop-glyph { font-size: 30px; color: var(--accent); line-height: 1; }
.drop-title { margin: 4px 0 0; font-size: 13.5px; color: var(--paper); }
.drop-sub { margin: 0; font-size: 11px; color: var(--muted); }

.stage { display: flex; gap: 12px; align-items: flex-start; }
.frame { position: relative; flex: 0 0 auto; padding: 6px; background: var(--ink); border: 1px solid var(--line); border-radius: 8px; }
.preview {
  display: block;
  max-width: 260px;
  max-height: 200px;
  border-radius: 4px;
  image-rendering: auto;
}
/* 蓝图角标：呼应「实验工作台」的示意图气质 */
.tick { position: absolute; width: 11px; height: 11px; border: 2px solid var(--accent); }
.tick.tl { top: -4px; left: -4px; border-right: none; border-bottom: none; }
.tick.tr { top: -4px; right: -4px; border-left: none; border-bottom: none; }
.tick.bl { bottom: -4px; left: -4px; border-right: none; border-top: none; }
.tick.br { bottom: -4px; right: -4px; border-left: none; border-top: none; }
.stage-side { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.stage-name {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.stage-actions { display: flex; gap: 6px; }
.hidden-input { display: none; }

.mode-row { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.seg { display: inline-flex; padding: 2px; background: var(--panel); border: 1px solid var(--line); border-radius: 7px; }
.seg-item {
  padding: 5px 14px;
  font-family: var(--font-body);
  font-size: 12px;
  color: var(--muted);
  background: transparent;
  border: none;
  border-radius: 5px;
  cursor: pointer;
  transition: color 0.15s ease, background 0.15s ease;
}
.seg-item:hover { color: var(--paper); }
.seg-item.active { color: var(--ink); background: var(--accent); font-weight: 600; }
.seg-item:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

.param { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }
.hint { font-size: 10.5px; color: var(--muted); }

.ask-row { display: flex; }
.ask-input {
  flex: 1 1 auto;
  min-width: 0;
  padding: 9px 12px;
  font-family: var(--font-body);
  font-size: 13px;
  line-height: 1.6;
  color: var(--paper);
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  outline: none;
  resize: vertical;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.ask-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent);
}
.ask-input::placeholder { color: var(--muted); }

.actions { display: flex; align-items: center; gap: 8px; }

.err-line {
  margin: 0;
  padding: 8px 11px;
  font-size: 12px;
  color: var(--c-bad, #ff7a72);
  background: var(--panel);
  border: 1px solid var(--c-bad, #ff7a72);
  border-radius: 6px;
}

/* ---------- 面板通用 ---------- */
.panel { padding: 12px 13px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.field-label { font-size: 11px; font-weight: 600; letter-spacing: 0.04em; color: var(--muted); }
.ok-badge {
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--data);
  background: color-mix(in srgb, var(--data) 16%, var(--ink));
  border-radius: 3px;
}
.extract-badge {
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--muted);
  background: var(--panel-2);
  border-radius: 3px;
}
.extract-badge.ok { color: var(--ink); background: var(--accent); }

/* ---------- 体检单 ---------- */
.meta-strip { display: flex; flex-wrap: wrap; gap: 8px; }
.meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 92px;
  padding: 7px 10px;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.meta-k { font-size: 10px; color: var(--muted); }
.meta-v { font-family: var(--font-display); font-size: 14px; color: var(--accent); }
.meta-v.mono { font-family: var(--font-mono); font-size: 11px; color: var(--paper); }
.mismatch {
  margin: 10px 0 0;
  padding: 8px 11px;
  font-size: 11.5px;
  line-height: 1.6;
  color: #ffe0ec;
  background: color-mix(in srgb, var(--accent) 16%, var(--ink));
  border: 1px solid var(--accent);
  border-radius: 6px;
}
.mismatch code {
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 0 3px;
  background: var(--panel-2);
  border-radius: 3px;
}

/* ---------- qa 解读 ---------- */
.answer {
  min-height: 72px;
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

/* ---------- extract 字段表 ---------- */
.summary {
  margin: 0 0 10px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--paper);
}
.fields { width: 100%; border-collapse: collapse; font-size: 12px; }
.fields th {
  padding: 6px 9px;
  text-align: left;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--muted);
  background: var(--panel-2);
  border-bottom: 1px solid var(--line);
}
.fields td { padding: 7px 9px; vertical-align: top; border-bottom: 1px solid var(--line); }
.fields tr:last-child td { border-bottom: none; }
.col-k { width: 34%; color: var(--accent); font-weight: 600; }
.col-v { color: var(--paper); word-break: break-word; }
.raw {
  margin: 0;
  padding: 10px 12px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--paper);
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.raw-details { margin-top: 10px; }
.raw-details summary {
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
}
.raw-details summary:hover { color: var(--accent); }
.raw-details .raw { margin-top: 8px; }

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
@media (prefers-reduced-motion: reduce) {
  .caret { animation: none; }
}

/* ---------- 耗时 ---------- */
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
.stat-num { font-family: var(--font-display); font-size: 16px; color: var(--data); }
.stat-num .of { font-style: normal; font-size: 11px; color: var(--muted); }
.stat-label { font-size: 10px; color: var(--muted); }

.empty { margin: 4px 0 0; font-size: 12px; line-height: 1.7; color: var(--muted); }
</style>
