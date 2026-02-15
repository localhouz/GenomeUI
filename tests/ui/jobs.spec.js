import { test, expect } from '@playwright/test';

test('scheduler lifecycle is visible in jobs feed', async ({ page }) => {
  const sessionId = `jobs${Date.now()}`;
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  const status = page.locator('#status');
  await expect(input).toBeVisible();

  const submitIntent = async (text) => {
    await input.fill(text);
    await input.press('Enter');
    await expect
      .poll(async () => {
        const current = String((await status.textContent()) || '');
        return current.includes('INTERPRETING') || current.includes('MODE:');
      }, { timeout: 10_000 })
      .toBeTruthy();
    await expect(status).not.toContainText('INTERPRETING', { timeout: 25_000 });
  };

  await submitIntent('add task scheduler target');
  await submitIntent('watch task 1 every 10m');
  await expect(page.locator('.feed-card .surface-label', { hasText: 'Jobs' }).first()).toBeVisible();
  await expect(page.getByText(/active watch_task/i)).toBeVisible();

  await submitIntent('pause job 1');
  await expect(page.getByText(/paused watch_task/i)).toBeVisible();

  await submitIntent('resume job 1');
  await expect(page.getByText(/active watch_task/i)).toBeVisible();

  await submitIntent('cancel job 1');
  await expect(page.getByText(/No active jobs/i)).toBeVisible();
});
