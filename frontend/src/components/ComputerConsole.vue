<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useComputer } from '../composables/useComputer'

defineProps<{ stage: number; title: string }>()

const s = useComputer()

// ⭐ 进页面就问一次「两个大脑现在能不能用」——而不是等用户点了才发现缺 Key。
//    这是那个 /providers 端点存在的全部理由。
onMounted(() => {
  void s.fetchProviders()
})

/** 三个选项；不可用的大脑直接置灰，并把「缺什么、去哪儿配」摊在界面上 */
const brainOptions = computed(() => {
  const find = (id: string) => s.providers.value.find((p) => p.provider === id)
  const claude = find('claude')
  const deepseek = find('deepseek')
  return [
    {
      id: 'auto' as const,
      label: 'auto',
      tip: s.providersNote.value || '有 CLAUDE_API_KEY 就用 Claude，否则用 DeepSeek',
      disabled: false,
    },
    {
      id: 'claude' as const,
      label: 'Claude（方案 A）',
      tip: claude?.available ? `原生 Computer Use · ${claude.model}` : (claude?.reason ?? '不可用'),
      disabled: claude?.available === false,
    },
    {
      id: 'deepseek' as const,
      label: 'DeepSeek（仿制）',
      tip: deepseek?.available
        ? `OpenAI 协议工具调用 · ${deepseek.model}`
        : (deepseek?.reason ?? '不可用'),
      disabled: deepseek?.available === false,
    },
  ]
})

const viewBox = computed(() => {
  const sc = s.screen.value
  return sc ? `0 0 ${sc.width} ${sc.height}` : '0 0 1024 768'
})

const hitRatePct = computed(() => `${(s.score.value.clickHitRate * 100).toFixed(0)}%`)

const stepsLabel = computed(() => {
  if (s.stepsUsed.value === 0) return '未开跑'
  return `${s.stepsUsed.value} 轮`
})

/** 有没有任何一步留下说明文字（模板里不写 Object.keys，省得踩模板作用域） */
const hasNarration = computed(() => {
  const last = s.shots.value.length ? s.shots.value[s.shots.value.length - 1].step : 0
  for (let i = 0; i <= last; i++) {
    if (s.stepNarration(i)) return true
  }
  return false
})
</script>

<template>
  <section class="console">
    <header class="banner">
      <div class="banner-kicker">阶段 {{ stage }} · {{ title }}</div>
      <h2 class="banner-title">Computer Use 的四步里，只有一步是模型厂商卖的</h2>
      <p class="banner-body">
        <strong>②「看屏幕 → 说出点哪里」</strong>才是厂商卖的能力。
        ① 截图、③ 执行动作、④ 回灌结果全是应用侧自己的代码 —— 本课已在
        <em>阶段 05（工具注册表）</em>、<em>阶段 10（ReAct 循环）</em>、<em>阶段 16（多模态消息）</em> 建好。
        本阶段用 DeepSeek 的视觉把 ② 接上：不需要 Claude、不需要 Docker、不需要新密钥。
      </p>
      <p class="banner-body">
        代价是<strong>精度</strong>，而精度差恰好是最好的教材：这台「电脑」是我们自己渲染的，
        所以每个按钮的坐标都是<strong>已知真值</strong> —— 模型的每一次点击都能被量出偏差，
        不必问它「你点准了吗」。
      </p>
      <ol class="loop">
        <li>① 截图</li>
        <li class="loop-hot">② 模型决策 · 绑定厂商</li>
        <li>③ 执行动作</li>
        <li>④ 回灌结果</li>
      </ol>
    </header>

    <div class="grid">
      <!-- ============ 左：虚拟屏幕 ============ -->
      <div class="panel screen-panel">
        <div class="panel-head">
          <span class="field-label">虚拟屏幕 · 1024×768 · 元素坐标即真值</span>
          <span class="step-badge">step {{ s.viewStepNo.value }}</span>
        </div>

        <div class="screen-wrap" :style="s.aspectStyle.value">
          <img v-if="s.viewShot.value" class="screen-img" :src="s.viewShot.value.image" alt="虚拟屏幕截图" />
          <div v-else class="screen-empty">点「开始操作」后，这里会显示模型看到的屏幕</div>

          <svg v-if="s.screen.value" class="overlay" :viewBox="viewBox">
            <!-- 元素真值：按钮/输入框真实所在的矩形 -->
            <g v-if="s.showBoxes.value">
              <rect
                v-for="el in s.screen.value.elements"
                :key="el.name"
                :x="el.x"
                :y="el.y"
                :width="el.w"
                :height="el.h"
                class="el-box"
                :class="{ danger: el.dangerous }"
              />
              <circle
                v-for="el in s.screen.value.elements"
                :key="el.name + '-c'"
                :cx="s.centerOf(el).cx"
                :cy="s.centerOf(el).cy"
                r="4"
                class="el-center"
              />
            </g>

            <!-- ⭐ 签名元素：模型的落点。绿=点中，红=点空 -->
            <g v-for="(a, i) in s.clicksOnView.value" :key="i" :class="{ miss: !a.hit }">
              <line :x1="(a.x ?? 0) - 26" :y1="a.y ?? 0" :x2="(a.x ?? 0) + 26" :y2="a.y ?? 0" class="cross" />
              <line :x1="a.x ?? 0" :y1="(a.y ?? 0) - 26" :x2="a.x ?? 0" :y2="(a.y ?? 0) + 26" class="cross" />
              <circle :cx="a.x ?? 0" :cy="a.y ?? 0" r="19" class="cross-ring" />
              <text :x="(a.x ?? 0) + 26" :y="(a.y ?? 0) - 22" class="cross-label">{{ i + 1 }}</text>
            </g>
          </svg>
        </div>

        <div class="strip">
          <span class="strip-label">回看</span>
          <button
            v-for="shot in s.shots.value"
            :key="shot.step"
            class="strip-btn"
            :class="{ active: shot.step === s.viewStepNo.value }"
            @click="s.viewStep.value = shot.step"
          >
            {{ shot.step }}
          </button>
          <button class="strip-btn live" :class="{ active: s.viewStep.value === null }" @click="s.viewStep.value = null">
            最新
          </button>
        </div>

        <div class="legend">
          <span><i class="dot hit"></i>落点（点中了元素）</span>
          <span><i class="dot miss"></i>落点（点空）</span>
          <span><i class="dot box"></i>元素真值矩形 + 中心点</span>
          <label class="legend-toggle">
            <el-switch v-model="s.showBoxes.value" size="small" />
            显示真值框
          </label>
        </div>
      </div>

      <!-- ============ 右：控制 + 评分 ============ -->
      <div class="col">
        <!-- 阶段 18B：只换第②步，其余三步不动 -->
        <div class="panel">
          <div class="panel-head">
            <span class="field-label">第②步「向模型要一个决策」用谁 · 其余三步与它无关</span>
          </div>
          <div class="brains">
            <button
              v-for="opt in brainOptions"
              :key="opt.id"
              class="brain-btn"
              :class="{ on: s.provider.value === opt.id }"
              :disabled="opt.disabled"
              :title="opt.tip"
              @click="s.provider.value = opt.id"
            >
              {{ opt.label }}
            </button>
          </div>
          <p class="brain-line">
            实际使用 <code>{{ s.activeProvider.value || s.effectiveProviderId.value }}</code>
            <span v-if="s.protocol.value">· {{ s.protocol.value }}</span>
          </p>
          <p v-if="s.providersError.value" class="brain-line">
            读不到大脑名单（{{ s.providersError.value }}）—— 不影响运行，点开始即可。
          </p>
          <p v-else-if="s.providerBlocked.value" class="brain-blocked">
            <strong>{{ s.selectedProviderInfo.value?.label }}</strong> 现在不可用：
            {{ s.selectedProviderInfo.value?.reason }}<br />
            配置：在 <code>backend/.env</code> 里填 <code>{{ s.selectedProviderInfo.value?.keyEnv }}</code> 后重启后端。
            端点 <code>{{ s.selectedProviderInfo.value?.endpoint }}</code>
            <template v-if="s.selectedProviderInfo.value?.toolVersion">
              <br />工具版本 <code>{{ s.selectedProviderInfo.value?.toolVersion }}</code>
              —— 必须配 <code>anthropic-beta: {{ s.selectedProviderInfo.value?.betaHeader }}</code>，配错会直接 400
            </template>
          </p>
          <p v-else class="brain-line">
            <strong>两条路共用同一块虚拟屏幕与同一套评分函数</strong> ——
            所以它们的命中率、定位偏差可以直接横向对比。
          </p>
        </div>

        <div class="panel">
          <div class="panel-head"><span class="field-label">任务 · 期望值（ASCII，键盘只能打 ASCII）</span></div>
          <div class="fields">
            <el-input v-model="s.orderId.value" size="small" placeholder="ORDER ID">
              <template #prepend>ORDER</template>
            </el-input>
            <el-input v-model="s.amount.value" size="small" placeholder="AMOUNT">
              <template #prepend>金额</template>
            </el-input>
            <el-input v-model="s.date.value" size="small" placeholder="DATE">
              <template #prepend>日期</template>
            </el-input>
          </div>
          <el-input
            v-model="s.task.value"
            class="task-input"
            type="textarea"
            :rows="3"
            :placeholder="'留空则用内置任务模板：填这个 Refund Request 表单并提交'"
          />

          <div class="opts">
            <label class="opt">
              最多轮数
              <el-input-number v-model="s.maxSteps.value" size="small" :min="1" :max="16" />
            </label>
            <label class="opt">
              <el-switch v-model="s.allowDangerous.value" size="small" />
              允许点危险按钮（RESET ALL）
            </label>
          </div>
          <p class="opt-hint">
            默认<strong>禁止</strong>危险动作 —— 沙箱拒绝而不是放行，这是 Computer Use 的第一原则。
            想演示"人工确认"这道闸门，就把上面打开对比一次。
          </p>

          <el-button
            type="primary"
            class="run"
            :disabled="!s.canRun.value"
            :loading="s.running.value"
            @click="s.run()"
          >
            {{ s.running.value ? '正在操作…' : '开始操作' }}
          </el-button>
          <p v-if="s.error.value" class="err">{{ s.error.value }}</p>
        </div>

        <div class="panel">
          <div class="panel-head">
            <span class="field-label">确定性读数 · 全部由后端数出来，无一来自模型自述</span>
          </div>
          <div class="score">
            <div class="score-item" :class="s.score.value.success ? 'good' : 'bad'">
              <span class="score-k">任务</span>
              <span class="score-v">{{ s.score.value.success ? '完成' : '未完成' }}</span>
            </div>
            <div class="score-item">
              <span class="score-k">字段</span>
              <span class="score-v">{{ s.score.value.fieldScore }} / {{ s.score.value.fieldTotal }}</span>
            </div>
            <div class="score-item" :class="s.score.value.clickHitRate >= 1 ? 'good' : 'warn'">
              <span class="score-k">点击命中率</span>
              <span class="score-v">{{ hitRatePct }}</span>
            </div>
            <div class="score-item" :class="s.score.value.avgCenterOffsetPx > 0 ? 'warn' : 'good'">
              <span class="score-k">平均定位偏差</span>
              <span class="score-v">{{ s.px(s.score.value.avgCenterOffsetPx) }}</span>
            </div>
            <div class="score-item" :class="s.score.value.avgClickErrorPx > 0 ? 'warn' : 'good'">
              <span class="score-k">点空偏差</span>
              <span class="score-v">{{ s.score.value.avgClickErrorPx }} px</span>
            </div>
            <div class="score-item" :class="s.score.value.lostKeystrokes > 0 ? 'bad' : 'good'">
              <span class="score-k">键盘被丢弃</span>
              <span class="score-v">{{ s.score.value.lostKeystrokes }} 次</span>
            </div>
            <div class="score-item" :class="s.rejectedActions.value > 0 ? 'warn' : 'good'">
              <span class="score-k">动作被沙箱拒绝</span>
              <span class="score-v">{{ s.rejectedActions.value }} 次</span>
            </div>
            <div class="score-item">
              <span class="score-k">轮数</span>
              <span class="score-v">{{ stepsLabel }}</span>
            </div>
          </div>
          <p class="score-hint">
            <strong>动作被沙箱拒绝</strong>在走 Claude 时通常大于 0：Claude 的动作词表比宿主宽
            （mouse_move / scroll / 双击…），而<em>愿意执行哪些动作由宿主决定，不由模型决定</em>。
            模型拿到拒绝理由后会改用 left_click —— 这不是故障，是沙箱在正常工作。
            两个大脑的动作词表差别（自写 schema vs 内置工具）正是本阶段的对照点。
          </p>
          <p class="score-hint">
            <strong>命中率</strong>回答「有没有点中」，<strong>定位偏差</strong>回答「点得多准」——
            两者都要看：一个 560×50 的大输入框，随便点哪儿都算命中，于是命中率满分也可能毫无精度。
            偏差是在<em>已知真值</em>下量出来的，所以连「全部点中」的那次运行也有读数，可横向比较。
          </p>
          <p class="score-hint">
            <strong>键盘被丢弃</strong>是本阶段最锋利的判据：模型想打字，但它前一次点击没落在任何输入框上，
            于是按键全丢 —— 这是<em>定位失败的硬证据</em>，纯观测、零成本，不需要模型承认。
          </p>

          <div v-if="s.finished.value" class="state-box">
            <div class="state-row" v-for="name in ['field_order', 'field_amount', 'field_date']" :key="name">
              <span class="state-k">{{ name }}</span>
              <span class="state-v" :class="{ ok: s.fieldOk(name) }">{{ s.finalState.value[name] || '（空）' }}</span>
            </div>
            <div class="state-row">
              <span class="state-k">submitted</span>
              <span class="state-v" :class="{ ok: s.score.value.submitted }">{{ s.score.value.submitted }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 动作时间线 ============ -->
    <div class="panel">
      <div class="panel-head">
        <span class="field-label">动作时间线 · 每一条都是模型发给「电脑」的指令</span>
        <span class="step-badge">{{ s.actions.value.length }} 个动作</span>
      </div>
      <p v-if="!s.actions.value.length" class="empty">还没有动作。</p>
      <ol v-else class="timeline">
        <li
          v-for="(a, i) in s.actions.value"
          :key="i"
          class="tl-item"
          :class="{ bad: !a.ok, focus: a.step === s.viewStepNo.value }"
        >
          <span class="tl-step">#{{ a.step }}</span>
          <span class="tl-action">{{ a.action }}</span>
          <span class="tl-summary">{{ s.actionSummary(a) }}</span>
          <span v-if="!a.ok" class="tl-tag bad">被拒</span>
          <span v-else-if="a.lostKeys" class="tl-tag bad">键盘丢弃</span>
          <span v-else-if="a.action === 'left_click' && a.hit" class="tl-tag good">
            命中 {{ a.target }} · 偏 {{ s.px(a.centerOffsetPx) }}
          </span>
          <span v-else-if="a.action === 'left_click'" class="tl-tag bad">点空 · 差 {{ a.errorPx }} px</span>
          <span v-if="a.note" class="tl-note">{{ a.note }}</span>
        </li>
      </ol>
    </div>

    <!-- ============ 模型自述 ============ -->
    <div v-if="hasNarration" class="panel">
      <div class="panel-head"><span class="field-label">模型每一轮说了什么（它的自述）</span></div>
      <div v-for="shot in s.shots.value" :key="shot.step" class="say">
        <template v-if="s.stepNarration(shot.step)">
          <span class="say-step">#{{ shot.step }}</span>
          <span class="say-text">{{ s.stepNarration(shot.step) }}</span>
        </template>
      </div>
    </div>

    <!-- ============ 耗时 ============ -->
    <div v-if="s.finished.value" class="panel times">
      <div class="times-bar">
        <span class="field-label">耗时</span>
        <span class="time-chip">渲染 {{ s.times.value.render }} ms</span>
        <span class="time-chip hot">模型 {{ s.llmMs.value }} ms</span>
        <span class="time-chip">合计 {{ s.totalMs.value }} ms</span>
      </div>
      <p class="times-note">
        渲染与模型调用差好几个数量级：截图是本地画一张 PNG，而决定"点哪里"必须过一趟视觉模型。
        <strong>能确定性算的就别问模型</strong> —— 本阶段所有评分都落在左边那一侧。
      </p>
    </div>
  </section>
</template>

<style scoped>
/* ---- 阶段 18B：两个「大脑」的切换 ---- */
.brains {
  display: flex;
  gap: 6px;
  margin-bottom: 4px;
}
.brain-btn {
  flex: 1;
  padding: 6px 8px;
  font-size: 12px;
  cursor: pointer;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: transparent;
  color: var(--muted);
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.brain-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.brain-btn.on {
  border-color: var(--accent);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  font-weight: 600;
}
.brain-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.brain-line {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--muted);
}
.brain-line strong {
  color: var(--paper);
}
.brain-line code,
.brain-blocked code {
  font-family: var(--font-mono);
  color: var(--accent);
}
.brain-blocked {
  margin: 10px 0 0;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.7;
  border-radius: 6px;
  border-left: 3px solid var(--c-bad, #ff7a72);
  background: color-mix(in srgb, var(--c-bad, #ff7a72) 10%, transparent);
  color: var(--paper);
}
.console {
  --accent: #a9e34b;
  --miss: var(--c-bad, #ff7a72);
  display: flex;
  flex-direction: column;
  gap: 18px;
  font-family: var(--font-body, system-ui);
  color: var(--ink);
}

.banner {
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: 12px;
  background: var(--panel);
  padding: 18px 20px;
}
.banner-kicker {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  letter-spacing: 0.06em;
  color: var(--accent);
  text-transform: uppercase;
}
.banner-title {
  margin: 6px 0 10px;
  font-family: var(--font-display, var(--font-body, system-ui));
  font-size: 19px;
  font-weight: 600;
}
.banner-body {
  margin: 0 0 8px;
  font-size: 13.5px;
  line-height: 1.75;
  color: var(--muted);
}
.banner-body strong {
  color: var(--ink);
}
.banner-body em {
  font-style: normal;
  color: var(--data, #5aa9ff);
}
.loop {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
  font-family: var(--font-mono, monospace);
  font-size: 12px;
}
.loop li {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 12px;
  color: var(--muted);
}
.loop-hot {
  border-color: var(--accent) !important;
  color: var(--accent) !important;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(300px, 1fr);
  gap: 18px;
  align-items: start;
}
@media (max-width: 980px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
.col {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}

.panel {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
  padding: 14px 16px;
  min-width: 0;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}
.field-label {
  font-size: 12.5px;
  color: var(--muted);
  font-family: var(--font-mono, monospace);
}
.step-badge {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  color: var(--accent);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 10px;
  white-space: nowrap;
}

.screen-wrap {
  position: relative;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: var(--paper);
}
.screen-img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.screen-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-size: 13px;
  color: var(--muted);
}
.overlay {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
.el-box {
  fill: none;
  stroke: var(--accent);
  stroke-width: 2;
  stroke-dasharray: 7 5;
  opacity: 0.75;
}
.el-box.danger {
  stroke: var(--miss);
}
.el-center {
  fill: var(--accent);
  opacity: 0.8;
}
.cross {
  stroke: var(--accent);
  stroke-width: 3;
}
.cross-ring {
  fill: none;
  stroke: var(--accent);
  stroke-width: 2;
}
.cross-label {
  fill: var(--accent);
  font-family: var(--font-mono, monospace);
  font-size: 20px;
}
.miss .cross,
.miss .cross-ring {
  stroke: var(--miss);
}
.miss .cross-label {
  fill: var(--miss);
}

.strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.strip-label {
  font-size: 12px;
  color: var(--muted);
  margin-right: 2px;
}
.strip-btn {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  min-width: 28px;
  padding: 3px 8px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.strip-btn.active {
  border-color: var(--accent);
  color: var(--accent);
}
.strip-btn.live {
  padding: 3px 10px;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--muted);
}
.legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}
.dot.hit {
  background: var(--accent);
}
.dot.miss {
  background: var(--miss);
}
.dot.box {
  border: 2px dashed var(--accent);
  background: transparent;
}
.legend-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.fields {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.task-input {
  margin-top: 10px;
}
.opts {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 12px;
}
.opt {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--muted);
}
.opt-hint {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted);
}
.opt-hint strong {
  color: var(--ink);
}
.run {
  width: 100%;
  margin-top: 14px;
}
.err {
  margin: 10px 0 0;
  font-size: 12.5px;
  color: var(--miss);
  line-height: 1.6;
}

.score {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.score-item {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.score-k {
  font-size: 11.5px;
  color: var(--muted);
}
.score-v {
  font-family: var(--font-mono, monospace);
  font-size: 16px;
  font-weight: 600;
}
.score-item.good .score-v {
  color: var(--accent);
}
.score-item.warn .score-v {
  color: var(--data, #5aa9ff);
}
.score-item.bad .score-v {
  color: var(--miss);
}
.score-hint {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted);
}
.score-hint strong {
  color: var(--ink);
}
.score-hint em {
  font-style: normal;
  color: var(--data, #5aa9ff);
}

.state-box {
  margin-top: 12px;
  border-top: 1px solid var(--line);
  padding-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.state-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-family: var(--font-mono, monospace);
  font-size: 12px;
}
.state-k {
  color: var(--muted);
}
.state-v.ok {
  color: var(--accent);
}

.timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 340px;
  overflow-y: auto;
}
.tl-item {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  border-left: 2px solid var(--line);
  padding: 5px 0 5px 10px;
  font-size: 12.5px;
}
.tl-item.focus {
  border-left-color: var(--accent);
}
.tl-item.bad {
  border-left-color: var(--miss);
}
.tl-step {
  font-family: var(--font-mono, monospace);
  font-size: 11.5px;
  color: var(--muted);
}
.tl-action {
  font-family: var(--font-mono, monospace);
  color: var(--data, #5aa9ff);
}
.tl-summary {
  font-family: var(--font-mono, monospace);
  color: var(--ink);
}
.tl-tag {
  font-size: 11.5px;
  border-radius: 999px;
  padding: 1px 8px;
  border: 1px solid var(--line);
}
.tl-tag.good {
  color: var(--accent);
  border-color: var(--accent);
}
.tl-tag.bad {
  color: var(--miss);
  border-color: var(--miss);
}
.tl-note {
  width: 100%;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
}
.empty {
  margin: 0;
  font-size: 12.5px;
  color: var(--muted);
}

.say {
  display: flex;
  gap: 8px;
  padding: 5px 0;
  font-size: 12.5px;
  line-height: 1.7;
}
.say-step {
  font-family: var(--font-mono, monospace);
  font-size: 11.5px;
  color: var(--muted);
  min-width: 26px;
}
.say-text {
  color: var(--ink);
  white-space: pre-wrap;
}

.times-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.time-chip {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 10px;
  color: var(--muted);
}
.time-chip.hot {
  color: var(--accent);
  border-color: var(--accent);
}
.times-note {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted);
}
.times-note strong {
  color: var(--ink);
}
</style>
