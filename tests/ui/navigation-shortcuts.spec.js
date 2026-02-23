import { test, expect } from '@playwright/test';

test('history and quick-command keyboard shortcuts work', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `nav${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('add task keyboard flow one');
  await input.press('Enter');
  await input.fill('add task keyboard flow two');
  await input.press('Enter');

  await expect.poll(async () => await page.locator('.history-node').count()).toBeGreaterThan(1);
  const activeBefore = await page.locator('.history-node.active').getAttribute('data-history-index');

  await page.keyboard.press('Alt+ArrowLeft');
  await expect.poll(async () => await page.locator('.history-node.active').getAttribute('data-history-index')).not.toEqual(activeBefore);

  await page.keyboard.press('Alt+ArrowRight');
  await expect.poll(async () => await page.locator('.history-node.active').getAttribute('data-history-index')).toEqual(activeBefore);

  await expect(page.locator('.tasks-row-action').first()).toBeVisible();
  await expect(page.locator('[data-command][data-hotkey-index="1"]').first()).toBeVisible();
  await page.keyboard.press('Alt+Digit1');
  await expect.poll(async () => await page.locator('.tasks-done-pill').count()).toBeGreaterThan(0);

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+KeyK' : 'Control+KeyK');
  await expect(input).toBeFocused();
});
