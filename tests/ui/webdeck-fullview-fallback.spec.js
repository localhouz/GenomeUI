import { test, expect } from '@playwright/test';

test('window controls stay hidden outside Electron runtime', async ({ page, request }) => {
  const sessionId = `webfallback${Date.now()}`;
  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  await expect(page.locator('#window-controls')).toBeHidden();
  await expect(page.locator('body')).not.toHaveClass(/electron-mode/);
});
