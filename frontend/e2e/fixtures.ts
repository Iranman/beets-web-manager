import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { expect, test as base, type Page } from '@playwright/test';
import { harnessRoot } from './env';

/** Every spec also fails on an uncaught exception in the page. */
export const test = base.extend<{ noPageErrors: void }>({
  noPageErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await use();
      expect(errors, 'uncaught page errors').toEqual([]);
    },
    { auto: true },
  ],
});
export { expect };

export const root = (...parts: string[]) => path.join(harnessRoot(), ...parts);

interface LibraryAlbum { album: string; track_count: number; mb_releasegroupid: string }
interface LibraryArtist { name: string; albums: LibraryAlbum[] }

/** Out-of-band view of the library: the API, not what the page rendered. */
export async function libraryAlbums(page: Page): Promise<LibraryAlbum[]> {
  const response = await page.request.get('/api/library');
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { artists: LibraryArtist[] };
  return body.artists.flatMap((artist) => artist.albums);
}

/** Every file under a directory, relative and sorted: proves media did or did not move. */
export function filesUnder(dir: string): string[] {
  return (fs.readdirSync(dir, { recursive: true, withFileTypes: true }) as fs.Dirent[])
    .filter((entry) => entry.isFile())
    .map((entry) => path.relative(dir, path.join(entry.parentPath, entry.name)))
    .sort();
}

/** Each file under a directory with the sha256 of its bytes: proves media kept its name and content. */
export const fileHashes = (dir: string) =>
  filesUnder(dir).map((file) => [file, createHash('sha256').update(fs.readFileSync(path.join(dir, file))).digest('hex')]);

const HOLD = root('hold-clean-all');
/** While held, Clean All waits at Root Folder Repair (harness/serve.py). */
export const holdCleanAll = () => fs.writeFileSync(HOLD, '');
export const releaseCleanAll = () => fs.rmSync(HOLD, { force: true });

/** The innermost section holding the "Clean All" heading on the Jobs page. */
export const cleanAllPanel = (page: Page) =>
  page.locator('section').filter({ has: page.getByRole('heading', { name: 'Clean All', exact: true }) }).last();

/** Start Clean All from a known state: a leftover checkpoint is discarded through the UI. Returns the run's job ID. */
export async function startFreshCleanAll(page: Page): Promise<string> {
  const panel = cleanAllPanel(page);
  const started = page.waitForResponse(
    (response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/jobs/maintenance-runner',
  );
  const discard = panel.getByRole('button', { name: /Discard checkpoint/ });
  const start = panel.getByRole('button', { name: 'Clean All', exact: true });
  await expect(discard.or(start).first()).toBeVisible();
  if (await discard.isVisible()) {
    page.once('dialog', (dialog) => dialog.accept());
    await discard.click();
  } else {
    await start.click();
  }
  const { job_id: jobId } = (await (await started).json()) as { job_id: string };
  await expect(panel.getByText('Root Folder Repair')).toBeVisible();
  return jobId;
}
