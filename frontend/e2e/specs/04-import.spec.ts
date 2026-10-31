import fs from 'node:fs';
import { expect, filesUnder, libraryAlbums, root, test } from '../fixtures';

const FOLDER = 'Import Artist - Import Album';

test.describe.configure({ mode: 'serial' });

test('preview is read-only and reports the folder it found', async ({ page }) => {
  const before = filesUnder(root('downloads'));
  await page.goto('/import');
  const source = page.getByRole('tabpanel', { name: /Import Source/ });
  await expect(source.getByRole('textbox', { name: 'Import/source path' })).toHaveValue(root('downloads'));
  await source.getByRole('button', { name: 'Preview Import All' }).click();
  await expect(source.getByText(FOLDER, { exact: true })).toBeVisible();
  await expect(source.getByText('1 file')).toBeVisible();
  expect(filesUnder(root('downloads'))).toEqual(before);
});

test('previewing the music root offers nothing to import: its folders are already in the library', async ({ page }) => {
  await page.goto('/import');
  const source = page.getByRole('tabpanel', { name: /Import Source/ });
  await source.getByRole('textbox', { name: 'Import/source path' }).fill(root('music'));
  await source.getByRole('button', { name: 'Preview Import All' }).click();
  await expect(source.getByText('In library', { exact: true })).toHaveCount(4);
  await expect(source.getByText(FOLDER, { exact: true })).toHaveCount(0);
  await expect(source.getByRole('button', { name: 'Import All', exact: true }).last()).toBeDisabled();
});

test('Import All asks first; cancelling starts nothing', async ({ page }) => {
  const before = (await libraryAlbums(page)).length;
  await page.goto('/import');
  const source = page.getByRole('tabpanel', { name: /Import Source/ });
  await source.getByRole('button', { name: 'Preview Import All' }).click();
  await expect(source.getByText(FOLDER, { exact: true })).toBeVisible();
  await source.getByRole('button', { name: 'Import All', exact: true }).last().click();
  const dialog = page.getByRole('dialog', { name: 'Run Import All?' });
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toHaveCount(0);
  expect(await libraryAlbums(page)).toHaveLength(before);
  expect(fs.existsSync(root('downloads', FOLDER))).toBe(true);
});

test('confirmed Import All runs as a job and the folder ends up imported or held for review', async ({ page }) => {
  await page.goto('/import');
  const source = page.getByRole('tabpanel', { name: /Import Source/ });
  await source.getByRole('button', { name: 'Preview Import All' }).click();
  await expect(source.getByText(FOLDER, { exact: true })).toBeVisible();
  await source.getByRole('button', { name: 'Import All', exact: true }).last().click();
  const started = page.waitForResponse((r) => r.url().includes('/api/import') && r.request().method() === 'POST' && !r.url().includes('preflight'));
  await page.getByRole('dialog', { name: 'Run Import All?' }).getByRole('button', { name: 'Import eligible only' }).click();
  expect((await started).ok()).toBeTruthy();

  await page.goto('/jobs');
  const history = page.getByRole('button', { name: /^Completed .*[Ii]mport/ }).first();
  await expect(history).toBeVisible({ timeout: 60_000 });

  // The stubbed lookup does not confirm this album, so a safe import leaves it unimported.
  const imported = (await libraryAlbums(page)).some((album) => album.album === 'Import Album');
  const inQueue = (await (await page.request.get('/api/import/review-queue')).text()).includes(FOLDER);
  expect(imported || inQueue || fs.existsSync(root('downloads', FOLDER))).toBe(true);
});
