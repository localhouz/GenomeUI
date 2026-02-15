import { test, expect } from '@playwright/test';

test('shows graph context after relation intent flow', async ({ page, request }) => {
  const sessionId = `ui${Date.now()}`;
  const postTurn = async (intent) => {
    const resp = await request.post('/api/turn', { data: { sessionId, intent } });
    expect(resp.ok()).toBeTruthy();
  };

  const init = await request.post('/api/session/init', { data: { sessionId } });
  expect(init.ok()).toBeTruthy();
  await postTurn('add task ui graph trace');
  await postTurn('add note relation target');
  await postTurn('link task 1 references note 1');
  await postTurn('add task dependency mid');
  await postTurn('add task dependency leaf');
  const graphResp = await request.get(`/api/session/${encodeURIComponent(sessionId)}/graph?limit=200`);
  expect(graphResp.ok()).toBeTruthy();
  const graphPayload = await graphResp.json();
  const entities = Array.isArray(graphPayload.entities) ? graphPayload.entities : [];
  const rootTask = entities.find((e) => String(e.kind || '').toLowerCase() === 'task' && String(e.title || '') === 'ui graph trace');
  const midTask = entities.find((e) => String(e.kind || '').toLowerCase() === 'task' && String(e.title || '') === 'dependency mid');
  const leafTask = entities.find((e) => String(e.kind || '').toLowerCase() === 'task' && String(e.title || '') === 'dependency leaf');
  expect(rootTask).toBeTruthy();
  expect(midTask).toBeTruthy();
  expect(leafTask).toBeTruthy();
  await postTurn(`link task ${String(rootTask.id).slice(0, 8)} depends_on task ${String(midTask.id).slice(0, 8)}`);
  await postTurn(`link task ${String(midTask.id).slice(0, 8)} depends_on task ${String(leafTask.id).slice(0, 8)}`);

  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  const status = page.locator('#status');
  await expect(input).toBeVisible();
  await input.click();

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

  await submitIntent('show references for note 1');
  await submitIntent(`show dependency chain for task ${String(rootTask.id).slice(0, 8)}`);
  await submitIntent(`show blockers for task ${String(rootTask.id).slice(0, 8)}`);
  await submitIntent(`show impact for task ${String(leafTask.id).slice(0, 8)}`);

  await expect(page.getByText('Graph Context')).toBeVisible();
  await expect(page.getByText(/relations:\s*[1-9]/i)).toBeVisible();
  const relationKindSignal = page.getByText(/(references|depends_on):\s*[1-9]/i);
  await expect(relationKindSignal.first()).toBeVisible();
});
