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
      '/api': {
        target: 'http://localhost:7700',
        changeOrigin: true
      }
    }
  }
});
