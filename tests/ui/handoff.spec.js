import { test, expect } from '@playwright/test';

test('handoff start and claim are visible in feed', async ({ page, request }) => {
  const sessionId = `handoff${Date.now()}`;
  const deviceId = `dev-${Math.random().toString(36).slice(2, 8)}`;
  const initResp = await request.post('/api/session/init', { data: { sessionId } });
  expect(initResp.ok()).toBeTruthy();
  const startResp = await request.post(`/api/session/${encodeURIComponent(sessionId)}/handoff/start`, {
    data: { deviceId },
  });
  expect(startResp.ok()).toBeTruthy();
  const startPayload = await startResp.json();
  const token = String(startPayload?.handoff?.pending?.token || '');
  expect(token.length).toBeGreaterThanOrEqual(8);

  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  const handoffCard = page.locator('.feed-card', {
    has: page.locator('.surface-label', { hasText: 'Handoff' })
  }).first();
  await expect(handoffCard).toBeVisible();
  await expect(handoffCard).toContainText(/pending:\s*(?!none\b)[a-z0-9-]{8,}/i);

  await input.fill(`claim handoff ${token}`);
  await input.press('Enter');

  await expect(handoffCard).toContainText(/pending:\s*none/i);
  await expect(handoffCard).toContainText(/active:\s*dev-[a-z0-9]+/i);

  const presenceResp = await request.get(`/api/session/${encodeURIComponent(sessionId)}/presence`);
  expect(presenceResp.ok()).toBeTruthy();
  const presence = await presenceResp.json();
  expect(Number(presence.activeCount || 0)).toBeGreaterThanOrEqual(1);
});
