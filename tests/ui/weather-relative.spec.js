import { test, expect } from '@playwright/test';

test('weather connector actions stay relative to current context', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `weatherrel${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('where am i');
  await input.press('Enter');

  const railText = await page.locator('.workspace-side').innerText();
  const normalized = railText.toLowerCase();
  expect(normalized).toContain("what's the weather where i am");
  expect(normalized).toContain('weather tomorrow where i am');
  expect(normalized).not.toContain('weather in seattle');
  expect(normalized).not.toContain('weather in new york');
});
