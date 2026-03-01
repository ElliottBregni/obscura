import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const apiProxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://localhost:8080'
const wsProxyTarget = process.env.VITE_WS_PROXY_TARGET ?? 'ws://localhost:8080'

export default defineConfig({
  plugins: [react()],
  resolve: {
    dedupe: ['react', 'react-dom'],
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: wsProxyTarget,
        ws: true,
      },
      '/mcp/': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      '/a2a/': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      '/.well-known/agent.json': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
})
