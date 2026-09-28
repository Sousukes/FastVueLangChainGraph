<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppHeader from './components/AppHeader.vue'
import StageRail from './components/StageRail.vue'
import { stageById } from './stages'
import { pathForStage } from './router'

/**
 * 应用壳（阶段 06 起）：左进度轨 + 顶栏 + 路由出口。
 *
 * 壳本身不认识任何业务能力——它只回答两个问题：
 *   现在在哪一阶段（路由 meta / :id）？点进度轨要去哪个路径？
 * 具体能力（对话 / 抽取 / 工具 / MCP…）都在 pages/ 下，按路由懒加载。
 */
const route = useRoute()
const router = useRouter()

const currentStage = computed(() => Number(route.meta.stage ?? route.params.id ?? 3))
const stageTitle = computed(() => stageById(currentStage.value)?.label ?? '')

function onNavigate(stage: number) {
  router.push(pathForStage(stage))
}
</script>

<template>
  <div class="app-shell">
    <StageRail :current="currentStage" class="app-rail" @navigate="onNavigate" />
    <div class="app-main">
      <AppHeader :stage="currentStage" :title="stageTitle" />
      <main class="app-console">
        <RouterView />
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
