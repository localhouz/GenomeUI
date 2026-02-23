import { test, expect } from '@playwright/test';

test('phone layout renders mobile webdeck mode', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `phoneweb${Date.now()}`;

  await page.setViewportSize({ width: 390, height: 844 });
  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();
  await input.fill('search web local-first operating system');
  await input.press('Enter');

  await expect(page.locator('.scene-webdeck')).toBeVisible();
  await expect(page.locator('.webdeck.webdeck-mobile')).toBeVisible();
  await expect(page.locator('.webdeck-results')).toBeVisible();
});
