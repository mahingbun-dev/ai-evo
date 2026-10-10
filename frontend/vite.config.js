import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import basicSsl from '@vitejs/plugin-basic-ssl'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue(), basicSsl()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    host: 'localhost',
    port: 3000,
    https: true,
    proxy: {
      '/api': {
        // 本地多实例联调可用 VITE_PROXY_TARGET 覆盖（如 worktree 后端起在 8101）
        target: process.env.VITE_PROXY_TARGET || 'http://localhost:8100',
        changeOrigin: true,
        ws: true
      }
    }
  }
})
