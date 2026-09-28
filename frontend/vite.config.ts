import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 阶段 03 起前端统一经后端代理访问 LLM，避免 Key 暴露在前端。
    // 代理目标显式写成 127.0.0.1 而不是 localhost：uvicorn 默认只监听 IPv4，
    // 而 Windows 上 localhost 可能先被解析成 IPv6 的 ::1，导致代理连不上。
    proxy: {
      '/api': 'http://127.0.0.1:8000'
    }
  }
})
