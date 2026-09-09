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
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (id.includes('/zrender/')) return 'chart-renderer'
          if (id.includes('/echarts/')) return 'charts'
          if (id.includes('/element-plus/') || id.includes('/@element-plus/')) return 'ui-components'
          if (id.includes('/@vue/') || id.includes('/vue/') || id.includes('/vue-router/') || id.includes('/pinia/')) return 'vue-runtime'
        }
      }
    }
  }
})
