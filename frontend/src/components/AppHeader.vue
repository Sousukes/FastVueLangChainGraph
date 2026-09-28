<script setup lang="ts">
// 顶栏：课程名 + 当前阶段标识。纯展示组件，状态由父级传入（props down）。
defineProps<{
  stage: number
  title: string
}>()
</script>

<template>
  <header class="app-header">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">◧</span>
      <span class="brand-name">全栈 AI 研究助手</span>
      <span class="brand-sub">FastAPI + Vue3 全栈 LLM 实战</span>
    </div>
    <div class="stage-tag" :aria-label="`当前阶段 ${stage}`">
      <span class="stage-no">阶段 {{ String(stage).padStart(2, '0') }}</span>
      <span class="stage-title">{{ title }}</span>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 22px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}

.brand {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.brand-mark {
  color: var(--signal);
  font-size: 18px;
  line-height: 1;
}
.brand-name {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 17px;
  color: var(--paper);
  white-space: nowrap;
}
.brand-sub {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.stage-tag {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex: 0 0 auto;
  padding: 6px 12px;
  border: 1px solid var(--signal);
  border-radius: 6px;
  background: color-mix(in srgb, var(--signal) 10%, transparent);
}
.stage-no {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--signal);
  letter-spacing: 0.05em;
}
.stage-title {
  font-family: var(--font-body);
  font-size: 13px;
  font-weight: 600;
  color: var(--paper);
}

@media (max-width: 820px) {
  .app-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .brand-sub {
    display: none;
  }
}
</style>
