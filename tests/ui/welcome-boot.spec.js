import { test, expect } from '@playwright/test';

test('empty boot shows welcome suggestions and tiles are tappable', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `welcome${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  await expect(page.locator('.welcome-surface')).toBeVisible();
  await expect(page.locator('.welcome-tile')).toHaveCount(4);

  const first = page.locator('.welcome-tile').first();
  const command = await first.getAttribute('data-command');
  expect(command).toBeTruthy();
  await first.click();

  await expect.poll(async () => await page.locator('.history-node').count()).toBeGreaterThan(0);
});
