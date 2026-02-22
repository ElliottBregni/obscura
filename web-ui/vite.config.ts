import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8080',
        ws: true,
      },
      '/mcp/': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      '/a2a/': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      '/.well-known/agent.json': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
