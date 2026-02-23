import { test, expect } from '@playwright/test';

test('manifest and service worker are wired for PWA shell', async ({ page, request }) => {
  test.setTimeout(180_000);

  const manifestResp = await request.get('/manifest.json');
  expect(manifestResp.ok()).toBeTruthy();
  const manifest = await manifestResp.json();
  expect(manifest.short_name).toBe('Genome');
  expect(Array.isArray(manifest.icons)).toBeTruthy();
  expect(manifest.icons.length).toBeGreaterThanOrEqual(2);

  await page.goto('/');
  await expect(page.locator('link[rel=\"manifest\"]')).toHaveCount(1);

  const swCount = await page.evaluate(async () => {
    if (!('serviceWorker' in navigator)) return 0;
    const regs = await navigator.serviceWorker.getRegistrations();
    return regs.length;
  });
  expect(swCount).toBeGreaterThanOrEqual(1);
});
