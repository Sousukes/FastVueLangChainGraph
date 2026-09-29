import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { STAGES, stageById } from '../stages'

/**
 * 路由即"能力清单"：每完成一个阶段，就多一个页面。
 *
 * 组件一律懒加载（`() => import(...)`）——这既是 vue-best-practices 的建议，
 * 也顺手解决了"所有阶段组件打进一个 chunk"的体积问题。
 *
 * 未实现的阶段统一走 `/stage/:id` 占位页，让进度轨上任何一格都点得动。
 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('../pages/ChatPage.vue'),
    meta: { stage: 3 },
  },
  {
    path: '/extract',
    name: 'extract',
    component: () => import('../pages/ExtractPage.vue'),
    meta: { stage: 4 },
  },
  {
    path: '/tools',
    name: 'tools',
    component: () => import('../pages/ToolsPage.vue'),
    meta: { stage: 5 },
  },
  {
    path: '/mcp',
    name: 'mcp',
    component: () => import('../pages/McpPage.vue'),
    meta: { stage: 6 },
  },
  {
    path: '/rag',
    name: 'rag',
    component: () => import('../pages/RagPage.vue'),
    meta: { stage: 7 },
  },
  {
    path: '/hybrid',
    name: 'hybrid',
    component: () => import('../pages/HybridPage.vue'),
    meta: { stage: 8 },
  },
  {
    path: '/graph',
    name: 'graph',
    component: () => import('../pages/GraphPage.vue'),
    meta: { stage: 9 },
  },
  {
    path: '/agent',
    name: 'agent',
    component: () => import('../pages/AgentPage.vue'),
    meta: { stage: 10 },
  },
  {
    path: '/team',
    name: 'team',
    component: () => import('../pages/TeamPage.vue'),
    meta: { stage: 11 },
  },
  {
    path: '/harness',
    name: 'harness',
    component: () => import('../pages/HarnessPage.vue'),
    meta: { stage: 12 },
  },
  {
    path: '/agentic',
    name: 'agentic',
    component: () => import('../pages/AgenticPage.vue'),
    meta: { stage: 13 },
  },
  {
    path: '/search',
    name: 'search',
    component: () => import('../pages/SearchPage.vue'),
    meta: { stage: 14 },
  },
  {
    path: '/research',
    name: 'research',
    component: () => import('../pages/ResearchPage.vue'),
    meta: { stage: 15 },
  },
  {
    path: '/vision',
    name: 'vision',
    component: () => import('../pages/VisionPage.vue'),
    meta: { stage: 16 },
  },
  {
    path: '/stage/:id(\\d+)',
    name: 'stage-placeholder',
    component: () => import('../pages/StagePlaceholder.vue'),
  },
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 统一维护页面标题，顺便让"当前阶段"有唯一事实来源
router.afterEach((to) => {
  const id = Number(to.meta.stage ?? to.params.id ?? 0)
  const meta = stageById(id)
  document.title = meta
    ? `阶段 ${String(id).padStart(2, '0')} · ${meta.label} — 全栈 AI 研究助手`
    : '全栈 AI 研究助手'
})

/** 按阶段号找路由（StageRail 点击时用） */
export function pathForStage(id: number): string {
  const meta = stageById(id)
  return meta?.path ?? `/stage/${id}`
}

export { STAGES }
