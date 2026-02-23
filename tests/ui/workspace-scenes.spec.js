import { test, expect } from '@playwright/test';

test('renders immersive task and file scenes', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `workspace${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('add task ship launcher');
  await input.press('Enter');
  await input.fill('add task verify visual scene');
  await input.press('Enter');
  await expect(page.locator('.scene-tasks')).toBeVisible();
  await expect(page.locator('.tasks-shell')).toBeVisible();
  await expect(page.locator('.tasks-row-action').first()).toBeVisible();

  await input.fill('list files .');
  await input.press('Enter');
  await expect(page.locator('.scene-files')).toBeVisible();
  await expect(page.locator('.files-body')).toBeVisible();
  await expect(page.locator('.files-tree')).toBeVisible();
  const commandNodes = await page.locator('.files-node[data-command]').count();
  const emptyNodes = await page.getByText('No entries').count();
  expect(commandNodes + emptyNodes).toBeGreaterThan(0);
});
