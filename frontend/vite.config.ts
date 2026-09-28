import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 阶段 03 起前端统一经后端代理访问 LLM，避免 Key 暴露在前端
    proxy: {
      '/api': 'http://localhost:8000'
    }
  }
})
