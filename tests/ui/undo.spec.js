import { test, expect } from '@playwright/test';

test('undo last reverses previous mutation and is visible in journal feed', async ({ page }) => {
  const sessionId = `undo${Date.now()}`;
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  const submitIntent = async (text) => {
    const resp = await page.request.post('/api/turn', {
      data: { sessionId, intent: text, onConflict: 'merge' },
      timeout: 60_000,
    });
    expect(resp.ok()).toBeTruthy();
  };

  await submitIntent('add task undo me');
  await submitIntent('undo last');

  const journalCard = page.locator('.feed-card', {
    has: page.locator('.surface-label', { hasText: 'Journal' })
  }).first();
  await expect(journalCard).toBeVisible();
  await expect(journalCard).toContainText(/ok undo_last/i);
});
