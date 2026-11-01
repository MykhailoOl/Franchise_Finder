import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), ['API_', 'DEV_'])
  const target = process.env.API_PROXY_TARGET ?? env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000'
  const usePolling = (process.env.DEV_USE_POLLING ?? env.DEV_USE_POLLING) === 'true'
  const proxy = Object.fromEntries(
    ['/api', '/docs', '/openapi.json'].map((path) => [path, { target, changeOrigin: true }]),
  )

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      proxy,
      watch: usePolling ? { usePolling: true, interval: 500, binaryInterval: 1000, pollInterval: 500 } : undefined,
    },
    preview: { host: '0.0.0.0', port: 4173, strictPort: true, proxy },
  }
})
