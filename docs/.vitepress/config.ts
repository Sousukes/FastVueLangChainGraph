import { defineConfig } from 'vitepress'
import { readdirSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

// 自动收录 docs/stages/ 下的阶段教程（文件名前缀 00–18 决定顺序），
// 之后新增阶段无需再改本文件。
const stagesDir = resolve(dirname(fileURLToPath(import.meta.url)), '../stages')
const stageItems = readdirSync(stagesDir)
  .filter((f) => f.endsWith('.md'))
  .sort()
  .map((f) => {
    const slug = f.replace(/\.md$/, '')
    const [no, ...rest] = slug.split('-')
    return { text: `${no} · ${rest.join('-')}`, link: `/stages/${encodeURIComponent(slug)}` }
  })

export default defineConfig({
  title: 'FastAPI + Vue3 全栈 LLM 实战',
  description: '0 基础全栈 AI 实战教程（18 阶段）',
  themeConfig: {
    nav: [{ text: '首页', link: '/' }],
    sidebar: [
      {
        text: '课程设计',
        items: [
          { text: '设计树 DESIGN', link: '/DESIGN' },
          { text: '前端设计系统', link: '/design-system' },
        ],
      },
      {
        text: '阶段教程',
        items: stageItems,
      },
    ],
  },
})
