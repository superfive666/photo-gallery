import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
// 从 vitest/config 导入而非 vite：它扩展了 vite 的配置类型，使 `test` 字段合法
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 本地开发把 /api 代到 api 容器，避免跨域和 cookie 的 SameSite 问题
    proxy: {
      '/api': {
        target: process.env.VITE_DEV_API_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 产物由 nginx 托管，不需要 sourcemap 上生产
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
})
