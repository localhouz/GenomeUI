import { test, expect } from '@playwright/test';

test('undo last reverses previous mutation and is visible in journal feed', async ({ page }) => {
  const sessionId = `undo${Date.now()}`;
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  const status = page.locator('#status');
  await expect(input).toBeVisible();

  const submitIntent = async (text) => {
    await input.fill(text);
    await input.press('Enter');
    await expect(status).toContainText('INTERPRETING', { timeout: 10_000 });
    await expect(status).not.toContainText('INTERPRETING', { timeout: 25_000 });
  };

  await submitIntent('add task undo me');
  await submitIntent('undo last');

  const journalCard = page.locator('.feed-card', {
    has: page.locator('.surface-label', { hasText: 'Journal' })
  }).first();
  await expect(journalCard).toBeVisible();
  await expect(journalCard).toContainText(/ok undo_last/i);
});

