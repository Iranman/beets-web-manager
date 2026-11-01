import {
  cleanAllPanel, expect, holdCleanAll, libraryAlbums, releaseCleanAll, startFreshCleanAll, test,
} from '../fixtures';

test.afterAll(releaseCleanAll);

// Reproduces a real defect. When it is fixed this reports an unexpected pass: remove test.fail then.
// backend/import_reconciliation_service.py passes apply_artist_folder_reconcile keyword arguments it
// does not accept. The TypeError is logged as a lost response, then a never-applied transaction is polled.
test('Clean All finishes when an imported artist folder needs its MBID stamp', async ({ page }) => {
  test.setTimeout(120_000);
  // Needs the album 03-review imported. Failures up to test.fail() below are real failures.
  expect((await libraryAlbums(page)).map((album) => album.album)).toContain('Review Album');
  holdCleanAll();
  await page.goto('/jobs');
  await startFreshCleanAll(page);
  releaseCleanAll();

  test.fail();
  await expect(cleanAllPanel(page).getByRole('button', { name: 'Stop' })).toHaveCount(0, { timeout: 60_000 });
});
