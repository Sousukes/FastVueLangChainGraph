<script setup lang="ts">
import AppHeader from './components/AppHeader.vue'
import StageRail from './components/StageRail.vue'
import ChatPanel from './components/ChatPanel.vue'

// 当前阶段配置（与 docs/DESIGN.md 三·阶段映射总表 对齐）
const CURRENT_STAGE = 1
const STAGE_TITLE = '大模型 API 编程'

// 阶段导航：阶段 01 仅作 UI 占位，真实路由在阶段 06 主线接入
function onNavigate(stage: number) {
  console.info(`[stage-rail] 请求跳转阶段 ${stage}（阶段 01 暂未接入路由）`)
}
</script>

<template>
  <div class="app-shell">
    <StageRail :current="CURRENT_STAGE" class="app-rail" @navigate="onNavigate" />
    <div class="app-main">
      <AppHeader :stage="CURRENT_STAGE" :title="STAGE_TITLE" />
      <main class="app-console">
        <ChatPanel :stage="CURRENT_STAGE" :title="STAGE_TITLE" />
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns: 232px 1fr;
  height: 100vh;
  background: var(--ink);
}

.app-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100vh;
}

.app-console {
  flex: 1 1 auto;
  min-height: 0;
  padding: 22px;
  overflow-y: auto;
}

@media (max-width: 820px) {
  .app-shell {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
    height: auto;
    min-height: 100vh;
  }
  .app-main {
    height: auto;
  }
}
</style>
