import { test, expect } from '@playwright/test';

test('renders live graph scene and domain canvases', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `graphviz${Date.now()}`;

  const postTurn = async (intent) => {
    const resp = await request.post('/api/turn', {
      data: { sessionId, intent, onConflict: 'merge' },
      timeout: 45_000,
    });
    expect(resp.ok()).toBeTruthy();
    return resp.json();
  };

  await request.post('/api/session/init', { data: { sessionId } });
  await postTurn('add task graph render root');
  await postTurn('add task graph render child');
  await postTurn('link task 1 depends_on task 2');
  await postTurn('show graph summary relation depends_on limit 10');

  await page.goto(`/?session=${sessionId}`);
  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('show graph summary relation depends_on limit 10');
  await input.press('Enter');
  await expect(page.locator('.scene-canvas[data-scene="graph"]')).toBeVisible();
  await expect(page.locator('.scene-graph .graph-live')).toBeVisible();
  await expect.poll(async () => await page.locator('.scene-graph .graph-node').count()).toBeGreaterThan(1);
  await expect.poll(async () => await page.locator('.scene-graph .graph-link').count()).toBeGreaterThan(0);
});
