import { test, expect } from '@playwright/test';

test('continuity alert push marks status badge and shows warning toast', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `contalert${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);
  await expect(page.locator('#intent-input')).toBeVisible();
  await page.evaluate(() => {
    const engine = window.__GENOME_UI_ENGINE__;
    if (!engine || typeof engine.applyBackgroundEvents !== 'function') return;
    engine.applyBackgroundEvents([
      {
        type: 'continuity_alert',
        severity: 'warn',
        anomalyCount: 3,
        message: 'Continuity health degraded: 3 anomalies detected',
        createdAt: Date.now(),
      },
    ]);
  });

  await expect.poll(async () => await page.locator('#status').getAttribute('data-alert')).toBe('continuity');
  await expect.poll(async () => (await page.locator('#ux-toast').innerText()).toLowerCase()).toContain('continuity health');
});
