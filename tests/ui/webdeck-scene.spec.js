import { test, expect } from '@playwright/test';

test('routes web intents to webdeck scene', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `webdeck${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });

  await page.goto(`/?session=${sessionId}`);
  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('search web local-first operating system');
  await input.press('Enter');
  await expect(page.locator('.scene-webdeck')).toBeVisible();
  await expect(page.locator('.webdeck-bar')).toBeVisible();
  await expect(page.locator('.webdeck-results')).toBeVisible();
  await expect(page.locator('.webdeck-inspector')).toBeVisible();
  await expect.poll(async () => await page.locator('.webdeck-result-card').count()).toBeGreaterThan(0);
  await expect.poll(async () => await page.locator('.webdeck-result-media').count()).toBeGreaterThan(0);

  await input.fill('summarize website https://example.com');
  await input.press('Enter');
  await expect(page.locator('.scene-webdeck')).toBeVisible();
  await expect(page.locator('.webdeck-bar')).toBeVisible();
  const pagePreviewCount = await page.locator('.webdeck-page-preview').count();
  const emptyCount = await page.locator('.webdeck-empty').count();
  const resultCardsCount = await page.locator('.webdeck-result-card').count();
  expect(pagePreviewCount + emptyCount + resultCardsCount).toBeGreaterThan(0);
});
