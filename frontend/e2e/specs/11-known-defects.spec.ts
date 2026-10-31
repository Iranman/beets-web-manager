import {
  cleanAllPanel, expect, holdCleanAll, libraryAlbums, releaseCleanAll, startFreshCleanAll, test,
} from '../fixtures';

// Reproduces a real defect. When it is fixed this reports an unexpected pass: remove test.fail then.
// backend/import_reconciliation_service.py passes apply_artist_folder_reconcile keyword arguments it
// does not accept. The TypeError is logged as a lost response, then a never-applied transaction is polled.
test('Clean All finishes when an imported artist folder needs its MBID stamp', async ({ page }) => {
  test.fail();
  test.setTimeout(120_000);
  // Needs the album 03-review imported; an unexpected pass without it would be meaningless.
  expect((await libraryAlbums(page)).map((album) => album.album)).toContain('Review Album');

  holdCleanAll();
  await page.goto('/jobs');
  await startFreshCleanAll(page);
  releaseCleanAll();
  await expect(cleanAllPanel(page).getByRole('button', { name: 'Stop' })).toHaveCount(0, { timeout: 60_000 });
});
