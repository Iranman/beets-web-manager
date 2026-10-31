import {
  cleanAllPanel, expect, filesUnder, holdCleanAll, libraryAlbums, releaseCleanAll, root, startFreshCleanAll, test,
} from '../fixtures';

test.describe.configure({ mode: 'serial' });
test.afterAll(releaseCleanAll);

const resumeInfo = async (page: import('@playwright/test').Page) =>
  (await (await page.request.get('/api/jobs/maintenance-runner/report?summary=1')).json()) as {
    resumable: boolean;
    resume: { previous_status?: string; next_task?: string; completed_count?: number };
  };

test('start runs on the synthetic library; a second start is impossible while running', async ({ page }) => {
  holdCleanAll();
  await page.goto('/jobs');
  await startFreshCleanAll(page);
  const panel = cleanAllPanel(page);
  await expect(panel.getByText('Running', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Clean All', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Stop' })).toBeEnabled();
});

test('Stop cancels and leaves a resumable checkpoint at the next task', async ({ page }) => {
  await page.goto('/jobs');
  const panel = cleanAllPanel(page);
  await panel.getByRole('button', { name: 'Stop' }).click();
  await expect(panel.getByRole('button', { name: 'Resume Clean All' })).toBeVisible({ timeout: 30_000 });
  await expect(panel).toContainText('Resume checkpoint ready. Next phase: Root Folder Repair.');

  const report = await resumeInfo(page);
  expect(report.resumable).toBe(true);
  expect(report.resume).toMatchObject({ previous_status: 'cancelled', next_task: 'root_folder_repair', completed_count: 2 });
});

test('Resume skips the checkpointed tasks and runs to the end without losing data', async ({ page }) => {
  const filesBefore = filesUnder(root('music'));
  const albumsBefore = (await libraryAlbums(page)).map((album) => album.album).sort();

  await page.goto('/jobs');
  const panel = cleanAllPanel(page);
  await panel.getByRole('button', { name: 'Resume Clean All' }).click();
  const activity = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Current activity' }) });
  await expect(activity).toContainText('Resuming from checkpoint: 2 completed task(s); next: Root Folder Repair');
  await expect(activity).toContainText('Library DB Health Check already complete');

  releaseCleanAll();
  await expect(panel.getByRole('button', { name: 'Stop' })).toHaveCount(0, { timeout: 75_000 });
  await expect(panel.getByRole('button', { name: 'Resume Clean All' })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: 'Clean All', exact: true })).toBeEnabled();
  await expect(panel).toContainText(/Complete/);

  // The clean fixture has nothing to repair: Clean All must not have touched it.
  expect((await libraryAlbums(page)).map((album) => album.album).sort()).toEqual(albumsBefore);
  expect(filesUnder(root('music'))).toEqual(filesBefore);
  expect((await resumeInfo(page)).resumable).toBe(false);
});
