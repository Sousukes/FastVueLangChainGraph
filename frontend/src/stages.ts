/**
 * 阶段清单：全课程唯一的"阶段 → 路由"映射表。
 *
 * 阶段 06 起项目并入主线大项目（docs/DESIGN.md 一·结构），前端从"每阶段换一个组件"
 * 升级为"一个应用壳 + 多路由"。这张表同时被 StageRail（进度轨）与 router（路由表）使用，
 * 避免两处各写一份而逐渐漂移。
 *
 * `path` 为 null 表示该阶段尚无页面（点进去会看到占位页）。
 *
 * ⚠️ id=1（ChatPanel.vue）/ id=2（PromptLab.vue）是**刻意不接路由**，不是漏做了：
 *    阶段 01–05 的前端是"单组件模式"（App.vue 硬编码 CURRENT_STAGE，渲染那一个组件），
 *    阶段 06 才升级为多路由；而这两个组件是【浏览器直连】DeepSeek 的教学实现
 *    （UI 上带"粘贴 API Key"输入框），与阶段 03 起"一律走后端代理、密钥只存 .env"
 *    的架构冲突，接入反而会让主线长出一个被刻意淘汰的反模式。
 *    故保持 null，组件与其 composable 作为阶段快照留在 components/ 与 composables/ 下，
 *    **请勿当作未使用代码删除**；历史快照在 `stage-01` / `stage-02` 分支各自保留。
 */

export interface StageMeta {
  id: number
  label: string
  path: string | null
}

export const STAGES: StageMeta[] = [
  { id: 0, label: '环境准备', path: null },
  { id: 1, label: '大模型 API 编程', path: null },
  { id: 2, label: 'Prompt 工程', path: null },
  { id: 3, label: '多轮对话与流式', path: '/chat' },
  { id: 4, label: '结构化输出与信息抽取', path: '/extract' },
  { id: 5, label: '函数调用与工具集成', path: '/tools' },
  { id: 6, label: 'MCP 协议开发', path: '/mcp' },
  { id: 7, label: 'RAG 基础', path: '/rag' },
  { id: 8, label: 'RAG 进阶', path: '/hybrid' },
  { id: 9, label: 'GraphRAG 知识图谱', path: '/graph' },
  { id: 10, label: '单智能体', path: '/agent' },
  { id: 11, label: '多智能体', path: '/team' },
  { id: 12, label: 'Agent Harness', path: '/harness' },
  { id: 13, label: 'Agentic RAG', path: '/agentic' },
  { id: 14, label: 'AI 搜索应用', path: '/search' },
  { id: 15, label: 'Deep Research', path: null },
  { id: 16, label: '多模态·图像', path: null },
  { id: 17, label: '多模态·语音', path: null },
  { id: 18, label: 'Computer Use', path: null },
]

export function stageById(id: number): StageMeta | undefined {
  return STAGES.find((s) => s.id === id)
}

/** 已实现（有页面）的阶段，用于占位页给出去处 */
export function implementedStages(): StageMeta[] {
  return STAGES.filter((s) => s.path !== null)
}
