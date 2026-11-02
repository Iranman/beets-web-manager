import { expect, test } from '../fixtures';
import { NO_FRONTEND_PORT, PASSWORD } from '../env';

const DEEP_LINKS: [string, string][] = [
  ['/library', 'Browse music library'],
  ['/library/missing-ids', 'Missing MusicBrainz IDs'],
  ['/import', 'Import music'],
  ['/import?tab=review', 'Import music'],
  ['/playlists', 'Playlists'],
  ['/jobs', 'Job History'],
  ['/config', 'Settings'],
  ['/system', 'System'],
  ['/changes', 'Library Changes'],
  ['/submissions', 'Submission'],
];

for (const [link, heading] of DEEP_LINKS) {
  test(`deep link and refresh: ${link}`, async ({ page }) => {
    const response = await page.goto(link);
    expect(response?.status()).toBe(200);
    await expect(page.getByRole('heading', { name: new RegExp(heading) }).first()).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: new RegExp(heading) }).first()).toBeVisible();
    expect(new URL(page.url()).pathname + new URL(page.url()).search).toBe(link);
  });
}

test('tab survives a refresh; old and unknown routes redirect', async ({ page }) => {
  await page.goto('/import?tab=review');
  await page.reload();
  await expect(page.getByRole('tab', { name: /Review Queue/ })).toHaveAttribute('aria-selected', 'true');
  await page.goto('/clean');
  await expect(page).toHaveURL(/\/jobs$/);
  await page.goto('/no/such/page');
  await expect(page).toHaveURL(/\/library$/);
});

test('with the build present, unknown API and asset paths stay plain 404s', async ({ page }) => {
  expect((await page.request.get('/api/no-such-endpoint')).status()).toBe(404);
  expect((await page.request.get('/assets/no-such-file.js')).status()).toBe(404);
  expect((await page.request.get('/_next/static/no-such-file.js')).status()).toBe(404);
});

test.describe('frontend build missing', () => {
  const base = `http://127.0.0.1:${NO_FRONTEND_PORT}`;
  test.use({ storageState: { cookies: [], origins: [] } });
  const MESSAGE = 'The web frontend build is missing. Rebuild or reinstall the application.';

  for (const link of ['/', '/library', '/import?tab=review']) {
    test(`${link} answers 503 with the explanation`, async ({ page }) => {
      const response = await page.goto(base + link);
      expect(response?.status()).toBe(503);
      await expect(page.locator('body')).toHaveText(MESSAGE);
      await expect(page).toHaveTitle('Web frontend build is missing');
    });
  }

  test('real API routes keep working, unknown ones explain, assets 404', async ({ request }) => {
    expect((await request.get(`${base}/api/health`)).status()).toBe(200);
    const headers = { Authorization: `Basic ${Buffer.from(`admin:${PASSWORD}`).toString('base64')}` };
    expect((await request.get(`${base}/api/library`, { headers })).status()).toBe(200);
    const unknown = await request.get(`${base}/api/no-such-endpoint`, { headers });
    expect(unknown.status()).toBe(503);
    expect(await unknown.json()).toMatchObject({ error: 'frontend_missing', message: MESSAGE });
    expect((await request.get(`${base}/assets/app.js`)).status()).toBe(404);
  });
});
