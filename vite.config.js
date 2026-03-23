import { defineConfig } from 'vite';

export default defineConfig({
  publicDir: 'public',
  build: {
    target: 'es2020'
  },
  server: {
    host: true,
    port: 5173,
    hmr: { host: 'localhost', port: 5173 },
    headers: {
      // Content-Security-Policy for dev server.
      // 'unsafe-inline' for styles is required because GenomeUI renders dynamic
      // CSS (scene colours, gradients) via inline style attributes at runtime.
      'Content-Security-Policy': [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        // ESPN CDN for team logos / venue photos; Simple Icons CDN for brand logos; blob: for canvas exports
        "img-src 'self' data: blob: https://a.espncdn.com https://a1.espncdn.com https://a2.espncdn.com https://a3.espncdn.com https://a4.espncdn.com https://cdn.simpleicons.org",
        // WebSocket + backend ports used in dev
        "connect-src 'self' ws://localhost:5173 ws://localhost:8787 http://localhost:8787 https://fonts.googleapis.com https://fonts.gstatic.com",
        "font-src 'self' https://fonts.gstatic.com",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'"
      ].join('; ')
    },
    proxy: {
      '/ws': {
        target: 'ws://localhost:8787',
        ws: true
      },
      // Turn requests go direct to backend — rule-based classifier handles intent,
      // no Nous round-trip needed for latency-critical path
      '/api/turn': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      // Auth endpoints bypass Nous — credentials never touch the gateway
      '/api/auth': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      // OAuth callbacks bypass Nous — token exchange goes direct to backend
      '/api/connectors/oauth': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      // Session, status, connectors all live on backend
      '/api/session': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      '/api/status': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      '/api/connectors': {
        target: 'http://localhost:8787',
        changeOrigin: true
      },
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: true
      }
    }
  }
});
