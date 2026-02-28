import { defineConfig } from 'vite';

export default defineConfig({
  publicDir: 'public',
  build: {
    target: 'es2020'
  },
  server: {
    host: true,
    port: 5173,
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
