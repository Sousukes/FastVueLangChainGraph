<script setup lang="ts">
// 阶段时间线：竖向 00→18 进度轨（桌面）；移动端收为顶部横向步骤条。
// 状态语义：done=已学(实心 Signal 点) / current=当前(▶ Data 箭头) / todo=未学(空心圈)。
const props = defineProps<{
  current: number
}>()

const emit = defineEmits<{
  (e: 'navigate', stage: number): void
}>()

// 与 docs/DESIGN.md 三·阶段映射总表 保持一致
const STAGES: { id: number; label: string }[] = [
  { id: 0, label: '环境准备' },
  { id: 1, label: '大模型 API 编程' },
  { id: 2, label: 'Prompt 工程' },
  { id: 3, label: '多轮对话与流式' },
  { id: 4, label: '结构化输出与信息抽取' },
  { id: 5, label: '函数调用与工具集成' },
  { id: 6, label: 'MCP 协议开发' },
  { id: 7, label: 'RAG 基础' },
  { id: 8, label: 'RAG 进阶' },
  { id: 9, label: 'GraphRAG 知识图谱' },
  { id: 10, label: '单智能体' },
  { id: 11, label: '多智能体' },
  { id: 12, label: 'Agent Harness' },
  { id: 13, label: 'Agentic RAG' },
  { id: 14, label: 'AI 搜索应用' },
  { id: 15, label: 'Deep Research' },
  { id: 16, label: '多模态·图像' },
  { id: 17, label: '多模态·语音' },
  { id: 18, label: 'Computer Use' },
]

function statusOf(id: number): 'done' | 'current' | 'todo' {
  if (id < props.current) return 'done'
  if (id === props.current) return 'current'
  return 'todo'
}

function onPick(id: number) {
  if (id === props.current) return
  emit('navigate', id)
}
</script>

<template>
  <nav class="stage-rail" aria-label="课程阶段进度">
    <div class="rail-head">
      <span class="rail-kicker">STAGES</span>
      <span class="rail-count">00 — 18</span>
    </div>
    <ol class="rail-list">
      <li
        v-for="s in STAGES"
        :key="s.id"
        class="rail-item"
        :class="[`is-${statusOf(s.id)}`]"
        :aria-current="statusOf(s.id) === 'current' ? 'step' : undefined"
      >
        <button type="button" class="rail-btn" @click="onPick(s.id)">
          <span class="rail-marker" aria-hidden="true">
            <span class="rail-dot" />
          </span>
          <span class="rail-id">{{ String(s.id).padStart(2, '0') }}</span>
          <span class="rail-label">{{ s.label }}</span>
        </button>
      </li>
    </ol>
  </nav>
</template>

<style scoped>
.stage-rail {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 18px 14px;
  background: var(--panel);
  border-right: 1px solid var(--line);
  overflow-y: auto;
}

.rail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 6px 14px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 12px;
}
.rail-kicker {
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.22em;
  color: var(--muted);
}
.rail-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--data);
}

.rail-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.rail-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 6px;
  background: transparent;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
  color: var(--paper);
  transition: background 0.15s ease;
}
.rail-btn:hover {
  background: var(--panel-2);
}

.rail-marker {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  flex: 0 0 auto;
}
.rail-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 1.5px solid var(--muted);
  background: transparent;
}

.rail-id {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--muted);
  flex: 0 0 auto;
  width: 18px;
}

.rail-label {
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--paper);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 已学：实心 Signal 点 */
.is-done .rail-dot {
  background: var(--signal);
  border-color: var(--signal);
}
.is-done .rail-id {
  color: var(--signal);
}

/* 当前：▶ Data 箭头 + 高亮行 */
.is-current .rail-btn {
  background: var(--panel-2);
}
.is-current .rail-marker::before {
  content: '▶';
  position: absolute;
  left: -2px;
  font-size: 9px;
  color: var(--data);
}
.is-current .rail-dot {
  border-color: var(--data);
  background: transparent;
}
.is-current .rail-id,
.is-current .rail-label {
  color: var(--data);
  font-weight: 600;
}

/* 未学：保持默认空心圈 */

/* 移动端：横向步骤条 */
@media (max-width: 820px) {
  .stage-rail {
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .rail-list {
    flex-direction: row;
    overflow-x: auto;
    gap: 4px;
  }
  .rail-btn {
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    min-width: 92px;
  }
  .rail-label {
    white-space: normal;
    font-size: 11px;
    line-height: 1.25;
  }
}
</style>
