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
  // One label per library folder, plus the summary tile's.
  const albums = (await libraryAlbums(page)).length;
  await expect(source.getByText('In library', { exact: true })).toHaveCount(albums + 1);
  await expect(source.getByText(FOLDER, { exact: true })).toHaveCount(0);
  await expect(source.getByRole('button', { name: 'Import All', exact: true }).last()).toBeDisabled();
});

test('Import All asks first; cancelling starts nothing', async ({ page }) => {
  const before = (await libraryAlbums(page)).length;
  await page.goto('/import');
  const source = page.getByRole('tabpanel', { name: /Import Source/ });
  await source.getByRole('button', { name: 'Preview Import All' }).click();
  await expect(source.getByText(FOLDER, { exact: true })).toBeVisible();
  const importPosts: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().includes('/api/ai-batch-import')) importPosts.push(request.url());
  });
  await source.getByRole('button', { name: 'Import All', exact: true }).last().click();
  const dialog = page.getByRole('dialog', { name: 'Run Import All?' });
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(1_000); // a wrongly fired request needs a moment to appear; there is no event for "none"
  expect(importPosts).toEqual([]);
  expect(await libraryAlbums(page)).toHaveLength(before);
  expect(fs.existsSync(root('downloads', FOLDER))).toBe(true);
});

test('confirmed Import All runs as a job; an unverifiable folder goes to review, not the library', async ({ page }) => {
  await page.goto('/import');
  const source = page.getByRole('tabpanel', { name: /Import Source/ });
  await source.getByRole('button', { name: 'Preview Import All' }).click();
  await expect(source.getByText(FOLDER, { exact: true })).toBeVisible();
  await source.getByRole('button', { name: 'Import All', exact: true }).last().click();
  const started = page.waitForResponse((r) => r.url().includes('/api/ai-batch-import') && r.request().method() === 'POST');
  await page.getByRole('dialog', { name: 'Run Import All?' }).getByRole('button', { name: 'Import eligible only' }).click();
  const response = await started;
  expect(response.ok()).toBeTruthy();
  const { job_id: jobId } = (await response.json()) as { job_id: string };

  const job = async () => (await (await page.request.get(`/api/jobs/${jobId}`)).json()) as { status: string; log: string[] };
  await expect.poll(async () => (await job()).status, { timeout: 60_000 }).toBe('success');
  expect((await job()).log.join('\n')).toContain(`review item created: ${root('downloads', FOLDER)}`);

  // The stubbed lookup does not confirm this album, so a safe import leaves it unimported.
  const imported = (await libraryAlbums(page)).some((album) => album.album === 'Import Album');
  expect(imported).toBe(false);
  expect(filesUnder(root('downloads', FOLDER))).toHaveLength(1);
  await expect
    .poll(async () => (await (await page.request.get('/api/import/review-queue')).text()).includes(FOLDER))
    .toBe(true);
});
