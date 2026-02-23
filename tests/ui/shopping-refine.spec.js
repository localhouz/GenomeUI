import { test, expect } from '@playwright/test';

test('shopping refine chips trigger follow-up intents', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `shoprefine${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('show me size 8 1/2 pumas, for men');
  await input.press('Enter');

  await expect(page.locator('.scene-shopping')).toBeVisible();
  await expect.poll(async () => await page.locator('.shop-refine-chip').count()).toBeGreaterThan(0);
  const historyBefore = await page.locator('.history-node').count();

  await page.locator('.shop-refine-chip').first().click();
  await expect.poll(async () => await page.locator('.history-node').count()).toBeGreaterThan(historyBefore);
});

