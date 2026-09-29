import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The FastAPI backend (dashboard/app.py) serves this build at root behind a strict CSP:
//   script-src 'self' 'unsafe-inline'; connect-src 'self'
// A production Vite build emits hashed ES modules referenced with `type="module" src="/assets/…"`,
// which satisfies script-src 'self' (no eval, no remote scripts). All API calls are same-origin
// /api/*, satisfying connect-src 'self'. base:'/' keeps asset URLs absolute from the app root.
export default defineConfig({
  base: '/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Small app — a single chunk keeps the CSP module-preload graph trivial.
    chunkSizeWarningLimit: 900,
  },
  // `npm test`: component tests in jsdom against the API fixtures in test/fixtures (their shape is
  // checked against the backend by tests/test_dashboard_frontend_fixtures.py). e2e/ is Playwright's.
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.{js,jsx}'],
    setupFiles: ['test/setup.js'],
    restoreMocks: true,
    unstubGlobals: true,
  },
  server: {
    // `npm run dev` proxies /api and /grafana to a locally running dashboard backend.
    proxy: {
      '/api': 'http://localhost:8080',
      '/grafana': 'http://localhost:8080',
    },
  },
})
