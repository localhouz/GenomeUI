import { test, expect } from '@playwright/test';

test('renders composite multi-step plan scene with step status cards', async ({ page }) => {
  test.setTimeout(120_000);
  const sessionId = `planscene${Date.now()}`;

  await page.goto(`/?session=${sessionId}`);
  await expect(page.locator('#intent-input')).toBeVisible();

  await page.evaluate(() => {
    const engine = window.__GENOME_UI_ENGINE__;
    engine._bootGuardUntil = 0;
    engine.state.session.lastExecution = {
      ok: false,
      isPlan: true,
      toolResults: [
        { op: 'travel_flight_search', ok: true, message: 'Flight options ready.' },
        { op: 'calendar_create', ok: true, message: 'Calendar hold created.' },
        { op: 'messaging_send', ok: false, message: 'Message send requires approval.' },
      ],
    };
    const envelope = {
      taskIntent: { goal: 'mutate', operation: 'write', targetDomains: ['travel', 'calendar', 'messaging'] },
      stateIntent: { readDomains: ['travel', 'calendar', 'messaging'], writeOperations: [] },
    };
    const kernelTrace = {
      route: { target: 'deterministic', reason: 'test', intentClass: 'mutate', confidence: 0.99 },
      policy: { allAllowed: false, codes: ['ok', 'ok', 'confirmation_required'] },
      diff: { tasks: 0, expenses: 0, notes: 0 },
      runtime: { presence: { activeCount: 1, count: 1 } },
      graph: { entities: 0, relations: 0, events: 0, byKind: {}, relationKinds: {}, recentRelationEvents: [] },
      journalTail: [],
    };
    engine.render(
      { version: '1.0.0', layout: { columns: 2, density: 'normal' }, suggestions: [], blocks: [], trace: {} },
      envelope,
      kernelTrace,
    );
  });

  await expect(page.locator('.plan-scene')).toBeVisible();
  await expect(page.locator('.plan-step')).toHaveCount(3);
  await expect(page.locator('.plan-summary')).toContainText('1 step(s) failed');
  await expect(page.locator('.plan-step-ok')).toHaveCount(2);
  await expect(page.locator('.plan-step-fail')).toHaveCount(1);
});
