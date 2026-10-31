import fs from 'node:fs';
import { expect, libraryAlbums, test } from '../fixtures';

// There is no separate dashboard route: "/" lands on the Library overview,
// and the System page carries version and readiness.
test('landing page summarises the synthetic library and its next repair', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/\/library$/);
  const main = page.getByRole('main');
  await expect(main.getByText('3 artists', { exact: true })).toBeVisible();
  await expect(main.getByText('4 albums', { exact: true })).toBeVisible();
  await expect(main.getByText('8 tracks', { exact: true })).toBeVisible();
  // The one album without MusicBrainz IDs is what needs repair next.
  await expect(main.getByRole('heading', { name: 'Untagged Sessions', level: 2 })).toBeVisible();
  await expect(main.getByRole('button', { name: /MusicBrainz Needs IDs 1/ })).toBeVisible();

  const albums = await libraryAlbums(page);
  expect(albums).toHaveLength(4);
  expect(albums.reduce((sum, album) => sum + album.track_count, 0)).toBe(8);
});

test('issue filter narrows the browser to the album that has the issue', async ({ page }) => {
  await page.goto('/library');
  await page.getByRole('button', { name: /MusicBrainz Needs IDs 1/ }).click();
  const main = page.getByRole('main');
  await expect(main.getByText('Active filter: Needs IDs')).toBeVisible();
  await expect(main.getByRole('article')).toHaveCount(1);
  await expect(main.getByRole('article')).toContainText('Untagged Sessions');
});

test('nav tabs and the system menu reach every section, and System reports the real version', async ({ page }) => {
  await page.goto('/library');
  const nav = page.getByRole('banner');
  for (const [tab, url, heading] of [
    ['Import', /\/import/, 'Import music'],
    ['Playlists', /\/playlists/, 'Playlists'],
    ['Jobs', /\/jobs/, 'Job History'],
    ['Library', /\/library/, 'Browse music library'],
  ] as const) {
    await nav.getByRole('link', { name: new RegExp(`^${tab}`) }).click();
    await expect(page).toHaveURL(url);
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
  }

  await nav.getByRole('button', { name: 'System' }).click();
  await page.getByRole('menuitem', { name: 'System' }).click();
  await expect(page).toHaveURL(/\/system/);
  const version = fs.readFileSync('../VERSION', 'utf8').trim();
  await expect(page.getByRole('main').getByText(`Version ${version}`)).toBeVisible();
  await expect(page.getByRole('main').getByText('Beets 2.14.1')).toBeVisible();
});

test('signing out ends the session; a deep link then asks for login', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ storageState: 'e2e/test-results/.auth.json', baseURL });
  const page = await context.newPage();
  await page.goto('/library');
  await page.getByRole('banner').getByRole('button', { name: 'System' }).click();
  await page.getByRole('menuitem', { name: 'Sign Out' }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto('/jobs');
  await expect(page).toHaveURL(/\/login/);
  expect((await page.request.get('/api/library')).status()).toBe(401);
  await context.close();
});
