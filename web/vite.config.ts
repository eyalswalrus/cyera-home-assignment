import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In dev the UI runs on :5173 and proxies API calls to FastAPI, so the browser sees a single
// origin - same as production, where FastAPI serves the built app. No CORS needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
