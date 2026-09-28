import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'FastAPI + Vue3 全栈 LLM 实战',
  description: '0 基础全栈 AI 实战教程（18 阶段）',
  themeConfig: {
    nav: [{ text: '首页', link: '/' }],
    sidebar: [
      {
        text: '课程设计',
        items: [{ text: '设计树 DESIGN', link: '/DESIGN' }]
      },
      {
        text: '阶段 00 · 环境准备',
        items: [{ text: '环境准备', link: '/stages/00-环境准备' }]
      }
      // 后续阶段（01–18 + 进阶篇）随撰写补充
    ]
  }
})
