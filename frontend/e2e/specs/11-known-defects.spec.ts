import {
  cleanAllPanel, expect, fileHashes, holdCleanAll, libraryAlbums, releaseCleanAll, root, startFreshCleanAll, test,
} from '../fixtures';

test.afterAll(releaseCleanAll);

// Stamping and scan merge are a recorded gap (docs/TECHNICAL_DEBT.md), not a failure: Clean All finishes without them.
test('Clean All finishes and skips Artist Folder Merge, with the reason, when a folder needs its MBID stamp', async ({ page }) => {
  test.setTimeout(120_000);
  // Needs the album 03-review imported: the skip only happens when a folder needs stamping.
  expect((await libraryAlbums(page)).map((album) => album.album)).toContain('Review Album');
  const filesBefore = fileHashes(root('music'));
  holdCleanAll();
  await page.goto('/jobs');
  const jobId = await startFreshCleanAll(page);
  releaseCleanAll();

  const panel = cleanAllPanel(page);
  await expect(panel.getByRole('button', { name: 'Stop' })).toHaveCount(0, { timeout: 75_000 });
  await expect(panel.getByRole('button', { name: 'Resume Clean All' })).toHaveCount(0);
  await expect(panel).toContainText(/Complete/);
  await expect(panel).toContainText('Artist Folder Merge skipped: Artist folder stamping and merging are not available');

  // Out of band: the job record, not what the page rendered.
  const job = (await (await page.request.get(`/api/jobs/${jobId}`)).json()) as {
    status: string;
    log: string[];
    state: { maintenance_status: string; maintenance_tasks: { id: string; status: string; detail: string }[] };
  };
  expect(job.status).toBe('success');
  expect(job.state.maintenance_status).toBe('complete');
  const task = job.state.maintenance_tasks.find((row) => row.id === 'artist_folder_merge');
  expect(task?.status).toBe('skipped');
  expect(task?.detail).toContain('not available');
  expect(job.log.join('\n')).not.toContain('Delegated');
  expect(fileHashes(root('music'))).toEqual(filesBefore);
});
