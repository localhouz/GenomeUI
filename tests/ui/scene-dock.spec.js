import { test, expect } from '@playwright/test';

test('scene dock restores semantic surfaces and keyboard cycle works', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `scenedock${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('add expense 34.5 transport train');
  await input.press('Enter');
  await expect(page.locator('.scene-expenses')).toBeVisible();
  await expect(page.locator('.surface-core')).toHaveAttribute('data-scene-to', 'expenses');

  await input.fill('add note scene dock verification');
  await input.press('Enter');
  await expect(page.locator('.scene-notes')).toBeVisible();
  await expect(page.locator('.surface-core')).toHaveAttribute('data-scene-to', 'notes');

  await expect(page.locator('.scene-dock')).toBeVisible();
  await page.locator('.scene-dock-node[data-scene-domain="expenses"]').click();
  await expect(page.locator('.scene-expenses')).toBeVisible();
  await expect(page.locator('.surface-core')).toHaveAttribute('data-scene-to', 'expenses');

  await page.keyboard.press('Alt+Shift+ArrowRight');
  await expect(page.locator('.scene-notes')).toBeVisible();
  await expect(page.locator('.surface-core')).toHaveAttribute('data-scene-to', 'notes');
});
