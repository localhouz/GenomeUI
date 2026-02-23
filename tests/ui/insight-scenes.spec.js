import { test, expect } from '@playwright/test';

test('renders immersive expenses and notes scenes', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `insights${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('add expense 24.5 food lunch');
  await input.press('Enter');
  await input.fill('add expense 56 transport train');
  await input.press('Enter');
  await expect(page.locator('.scene-expenses')).toBeVisible();
  await expect(page.locator('.expenses-shell')).toBeVisible();

  await input.fill('add note summarize launch blockers');
  await input.press('Enter');
  await input.fill('show notes');
  await input.press('Enter');
  await expect.poll(async () => await page.locator('.notes-shell').count(), { timeout: 20_000 }).toBeGreaterThan(0);
  await expect(page.locator('.notes-wall')).toBeVisible();
});
