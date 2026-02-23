import { test, expect } from '@playwright/test';

test('offline overlay appears on disconnect and manual retry restores online state', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `offline${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();
  await expect(page.locator('#boot-sequence')).toBeHidden();
  await expect(page.locator('#offline-overlay.visible')).toHaveCount(0);

  await page.evaluate(() => {
    window.dispatchEvent(new Event('offline'));
  });

  await expect(page.locator('#offline-overlay.visible')).toHaveCount(1);
  await expect(page.locator('#status')).toContainText('NET: OFFLINE');

  await page.locator('[data-offline-retry]').click({ force: true });
  await expect(page.locator('#offline-overlay.visible')).toHaveCount(0);
  await expect.poll(async () => await page.locator('#status').innerText()).toContain('NET: ONLINE');
});
