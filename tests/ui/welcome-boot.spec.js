import { test, expect } from '@playwright/test';

test('empty boot shows welcome suggestions and tiles are tappable', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `welcome${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  await expect(page.locator('.welcome-surface')).toBeVisible();
  await expect.poll(async () => await page.locator('.welcome-tile').count()).toBeGreaterThanOrEqual(4);

  const first = page.locator('.welcome-tile[data-command]').first();
  await expect(first).toBeVisible();
  const command = await first.getAttribute('data-command');
  expect(command).toBeTruthy();
  await first.click();

  await expect(page.locator('.workspace')).toBeVisible();
});

test('reloading an active session boots back to latent surface instead of restoring last scene', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `cleanboot${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  const turn = await request.post('/api/turn', {
    data: {
      sessionId,
      intent: 'show weather in chicago',
      nousIntent: { op: 'weather_forecast', slots: { location: 'Chicago' }, _nousMs: 1 }
    }
  });
  expect(turn.ok()).toBeTruthy();

  await page.goto(`/?session=${sessionId}`);

  await expect(page.locator('.welcome-surface')).toBeVisible();
  await expect(page.locator('.scene')).toHaveCount(0);
  await expect.poll(async () => await page.locator('.history-node').count()).toBeGreaterThan(0);
});
