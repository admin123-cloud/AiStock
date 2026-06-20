import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
      host: '0.0.0.0',
      port: 3000,
      allowedHosts: ['.trycloudflare.com', 'stock.aistock3.fun'],
      proxy: {
        '/api': {
          target: process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000',
          changeOrigin: true
        }
      }
    },
  build: {
    outDir: 'dist',
    sourcemap: false
  }
})
