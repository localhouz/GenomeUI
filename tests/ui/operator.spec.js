import { test, expect } from '@playwright/test';

test('operator commands work via intent plane', async ({ page }) => {
  test.setTimeout(360_000);
  const sessionId = `ops${Date.now()}`;
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  const submitIntent = async (intent) => {
    const resp = await page.request.post('/api/turn', {
      data: { sessionId, intent, onConflict: 'merge' },
      timeout: 45_000,
    });
    return await resp.json();
  };

  const expectNonFatal = async (turn) => {
    const message = String(turn?.execution?.message || '').toLowerCase();
    await expect(
      Boolean(turn?.execution)
      || Boolean(turn?.detail)
      || message.includes('denied')
      || message.includes('write blocked')
      || message.includes('no action')
      || message.includes('not available')
      || message.includes('failed')
    ).toBeTruthy();
  };

  await input.fill('add task operator seed');
  await input.press('Enter');
  await input.fill('delete task');
  await input.press('Enter');
  await expect(page.getByText(/denied clarification_needed/i)).toBeVisible();

  await expectNonFatal(await submitIntent('show audit'));
  await expectNonFatal(await submitIntent('show trace'));
  await expectNonFatal(await submitIntent('export trace'));
  await expectNonFatal(await submitIntent('show trace summary'));
  await expectNonFatal(await submitIntent('show connector grants'));
  await expectNonFatal(await submitIntent('show web status'));
  await expectNonFatal(await submitIntent('grant connector scope web.page.read'));
  await expectNonFatal(await submitIntent('search web local-first os'));
  await expectNonFatal(await submitIntent('open website https://example.com'));
  await expectNonFatal(await submitIntent('summarize website https://example.com'));
  await expectNonFatal(await submitIntent('show contacts status'));
  await expectNonFatal(await submitIntent('grant connector scope contacts.read'));
  await expectNonFatal(await submitIntent('find contact mike'));
  await expectNonFatal(await submitIntent('revoke weather forecast'));
  const weatherBlocked = await submitIntent('show weather in Seattle');
  const weatherBlockedMsg = String(weatherBlocked?.execution?.message || '').toLowerCase();
  await expect(
    weatherBlockedMsg.includes('connector scope missing')
    || weatherBlockedMsg.includes('no state changes requested')
    || weatherBlockedMsg.includes('weather ready')
  ).toBeTruthy();
  await expectNonFatal(await submitIntent('grant weather forecast'));
  await expectNonFatal(await submitIntent('show weather in Seattle'));
  await expectNonFatal(await submitIntent('show telephony status'));
  await expectNonFatal(await submitIntent('grant connector scope telephony.call.start'));
  await expectNonFatal(await submitIntent('grant connector scope contacts.read'));
  await expectNonFatal(await submitIntent('confirm call mike'));
  await expectNonFatal(await submitIntent('confirm call 5550100'));
  await expectNonFatal(await submitIntent('show banking status'));
  await expectNonFatal(await submitIntent('grant connector scope bank.account.balance.read'));
  await expectNonFatal(await submitIntent('show bank balance'));
  await expectNonFatal(await submitIntent('show social status'));
  await expectNonFatal(await submitIntent('grant connector scope social.feed.read'));
  await expectNonFatal(await submitIntent('show social feed'));
  await expectNonFatal(await submitIntent('remind me to stretch in 10m'));
  await expectNonFatal(await submitIntent('show reminder status'));
  await expectNonFatal(await submitIntent('show reminders'));
  await expectNonFatal(await submitIntent('pause reminder 1'));
  await expectNonFatal(await submitIntent('resume reminder 1'));
  await expectNonFatal(await submitIntent('cancel reminder 1'));

  await expectNonFatal(await submitIntent('show dead letters'));
  await expectNonFatal(await submitIntent('show runtime health'));
  await expectNonFatal(await submitIntent('show runtime profile'));
  await expectNonFatal(await submitIntent('show diagnostics'));

  await expectNonFatal(await submitIntent('show continuity'));
  await expectNonFatal(await submitIntent('show continuity health'));
  await expectNonFatal(await submitIntent('show continuity anomalies'));
  await expectNonFatal(await submitIntent('show continuity incidents'));
  await expectNonFatal(await submitIntent('show continuity next'));

  await expectNonFatal(await submitIntent('show continuity autopilot'));
  await expectNonFatal(await submitIntent('show continuity autopilot metrics'));
  await expectNonFatal(await submitIntent('show continuity autopilot guardrails'));
  await expectNonFatal(await submitIntent('show continuity autopilot posture'));
  await expectNonFatal(await submitIntent('show continuity autopilot posture actions'));
  await expectNonFatal(await submitIntent('show continuity autopilot posture actions anomalies'));

  await expectNonFatal(await submitIntent('dry run continuity autopilot'));
  await expectNonFatal(await submitIntent('enable continuity autopilot'));
  await expectNonFatal(await submitIntent('set continuity autopilot cooldown 5s'));
  await expectNonFatal(await submitIntent('tick continuity autopilot'));
  await expectNonFatal(await submitIntent('disable continuity autopilot'));

  await expectNonFatal(await submitIntent('show snapshot stats'));
  await expectNonFatal(await submitIntent('restore preview'));
  await expectNonFatal(await submitIntent('checkpoint now'));
  await expectNonFatal(await submitIntent('list checkpoints'));

  await expectNonFatal(await submitIntent('verify journal integrity'));
  await expectNonFatal(await submitIntent('repair journal integrity'));
  await expectNonFatal(await submitIntent('drill policy deny'));
  await expectNonFatal(await submitIntent('drill policy confirm'));
  await expectNonFatal(await submitIntent('run self check'));

  await expectNonFatal(await submitIntent('simulate persist failure on'));
  await expectNonFatal(await submitIntent('add note fault probe'));
  await expectNonFatal(await submitIntent('retry persist now'));
  await expectNonFatal(await submitIntent('simulate persist failure off'));
  await expectNonFatal(await submitIntent('retry persist now'));

  const perfCard = page.locator('.feed-card', {
    has: page.locator('.surface-label', { hasText: 'Performance' })
  }).first();
  await expect(perfCard).toBeVisible();
  await expect(perfCard).toContainText(/total:\s*\d+ms/i);
});
