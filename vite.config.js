import { defineConfig } from 'vite';

export default defineConfig({
  publicDir: 'public',
  build: {
    target: 'es2020'
  },
  server: {
    host: true,
    port: 5173,
    headers: {
      // Content-Security-Policy for dev server.
      // 'unsafe-inline' for styles is required because GenomeUI renders dynamic
      // CSS (scene colours, gradients) via inline style attributes at runtime.
      'Content-Security-Policy': [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        // ESPN CDN for team logos / venue photos; blob: for canvas exports
        "img-src 'self' data: blob: https://a.espncdn.com https://a1.espncdn.com https://a2.espncdn.com https://a3.espncdn.com https://a4.espncdn.com",
        // WebSocket + backend ports used in dev
        "connect-src 'self' ws://localhost:5173 ws://localhost:7700 ws://localhost:8787 http://localhost:7700 http://localhost:8787",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'"
      ].join('; ')
    },
    proxy: {
      '/ws': {
        target: 'ws://localhost:7700',
        ws: true
      },
      // Auth endpoints bypass Nous — credentials never touch the gateway
      '/api/auth': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      '/api': {
        target: 'http://localhost:7700',
        changeOrigin: true
      }
    }
  }
});
