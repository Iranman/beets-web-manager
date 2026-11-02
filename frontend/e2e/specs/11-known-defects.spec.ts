import {
  cleanAllPanel, expect, holdCleanAll, libraryAlbums, releaseCleanAll, startFreshCleanAll, test,
} from '../fixtures';

test.afterAll(releaseCleanAll);

// Reproduces a real defect. When it is fixed the signature check below fails: remove it and test.fail then.
// backend/import_reconciliation_service.py passes apply_artist_folder_reconcile keyword arguments it
// does not accept. The TypeError is logged as a lost response, then a never-applied transaction is polled.
test('Clean All finishes when an imported artist folder needs its MBID stamp', async ({ page }) => {
  test.setTimeout(150_000);
  // Needs the album 03-review imported. Failures up to test.fail() below are real failures.
  expect((await libraryAlbums(page)).map((album) => album.album)).toContain('Review Album');
  holdCleanAll();
  await page.goto('/jobs');
  await startFreshCleanAll(page);
  releaseCleanAll();

  // The hang below only counts as this defect if the job log shows this defect's signature.
  const { jobs } = (await (await page.request.get('/api/jobs')).json()) as { jobs: { job_id: string; label: string; status: string }[] };
  const job = jobs.find((row) => row.label === 'Clean All' && row.status === 'running');
  expect(job, 'running Clean All job').toBeTruthy();
  await expect
    .poll(async () => ((await (await page.request.get(`/api/jobs/${job?.job_id}`)).json()) as { log: string[] }).log.join('\n'), { timeout: 60_000 })
    .toContain('Apply response was lost (unexpected error)');

  test.fail();
  await expect(cleanAllPanel(page).getByRole('button', { name: 'Stop' })).toHaveCount(0, { timeout: 60_000 });
});
