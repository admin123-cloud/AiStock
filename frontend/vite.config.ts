import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    host: '0.0.0.0',
    port: 3100,
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
          if (id.includes('/lightweight-charts/')) return 'kline'
          if (id.includes('/react/') || id.includes('/react-dom/') || id.includes('/scheduler/')) return 'react-runtime'
          if (id.includes('/@headlessui/') || id.includes('/motion/') || id.includes('/clsx/')) return 'ui-runtime'
        }
      }
    }
  }
})
