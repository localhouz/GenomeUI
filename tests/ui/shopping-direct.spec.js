import { test, expect } from '@playwright/test';

test('shopping direct intent renders brand stage with canonical source link', async ({ page, request }) => {
  test.setTimeout(180_000);
  const sessionId = `shopdirect${Date.now()}`;

  await request.post('/api/session/init', { data: { sessionId } });
  await page.goto(`/?session=${sessionId}`);

  const input = page.locator('#intent-input');
  await expect(input).toBeVisible();

  await input.fill('show me jordan shoes for men size 8.5');
  await input.press('Enter');

  await expect(page.locator('.scene-shopping')).toBeVisible();
  const stageStripCount = await page.locator('.shop-source-strip').count();
  const galleryCount = await page.locator('.shop-gallery').count();
  expect(stageStripCount + galleryCount).toBeGreaterThan(0);
  if (stageStripCount > 0) {
    await expect(page.locator('.shop-stage-live-frame').first()).toBeVisible();
  }
  const cta = page.locator('.shop-brand-cta').first();
  const fallbackCta = page.locator('.scene-shopping .scene-chip-link').first();
  const target = (await cta.count()) > 0 ? cta : fallbackCta;
  await expect(target).toBeVisible();
  const href = await target.getAttribute('href');
  const hrefStr = String(href || '').toLowerCase();
  expect(
    hrefStr.includes('puma.com')
    || hrefStr.includes('nike.com')
    || hrefStr.includes('adidas.com')
    || hrefStr.includes('newbalance.com')
  ).toBeTruthy();
  expect(
    hrefStr.includes('?q=')
    || hrefStr.includes('/search?')
    || hrefStr.includes('/w?')
    || hrefStr.includes('/men/shoes')
    || hrefStr.includes('/mens-shoes')
  ).toBeTruthy();
});
