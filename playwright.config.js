import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/ui',
  timeout: 120_000,
  retries: 1,
  workers: 1,
  expect: {
    timeout: 12_000
  },
  use: {
    headless: true,
    baseURL: 'http://127.0.0.1:5173',
    viewport: { width: 1440, height: 900 }
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://127.0.0.1:5173',
    timeout: 120_000,
    reuseExistingServer: true
  }
});
