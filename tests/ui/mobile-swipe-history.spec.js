import { test, expect } from '@playwright/test';

test('mobile history navigation restores prior surface', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `swipe${Date.now()}`;

  await page.setViewportSize({ width: 390, height: 844 });
  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('add task swipe test one');
  await input.press('Enter');
  await input.fill('add note swipe test two');
  await input.press('Enter');

  const activeBefore = await page.locator('.history-node.active').getAttribute('data-history-index');
  await page.evaluate(() => {
    const engine = window.__GENOME_UI_ENGINE__;
    if (!engine || typeof engine.navigateIntentHistory !== 'function') return;
    engine.navigateIntentHistory(-1);
  });

  await expect.poll(async () => await page.locator('.history-node.active').getAttribute('data-history-index')).not.toEqual(activeBefore);
});
