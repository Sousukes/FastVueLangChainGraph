<script setup lang="ts">
import AppHeader from './components/AppHeader.vue'
import StageRail from './components/StageRail.vue'
import Extractor from './components/Extractor.vue'

// 当前阶段配置（与 docs/DESIGN.md 三·阶段映射总表 对齐）
const CURRENT_STAGE = 4
const STAGE_TITLE = '结构化输出与信息抽取'

// 阶段导航：阶段 04 仅作 UI 占位，真实路由在阶段 06 主线接入
function onNavigate(stage: number) {
  console.info(`[stage-rail] 请求跳转阶段 ${stage}（阶段 04 暂未接入路由）`)
}
</script>

<template>
  <div class="app-shell">
    <StageRail :current="CURRENT_STAGE" class="app-rail" @navigate="onNavigate" />
    <div class="app-main">
      <AppHeader :stage="CURRENT_STAGE" :title="STAGE_TITLE" />
      <main class="app-console">
        <Extractor :stage="CURRENT_STAGE" :title="STAGE_TITLE" />
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
  overflow: hidden;
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
  .app-console {
    overflow-y: auto;
  }
}
</style>
