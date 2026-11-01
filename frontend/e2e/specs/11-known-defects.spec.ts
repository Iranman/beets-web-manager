import {
  cleanAllPanel, expect, holdCleanAll, libraryAlbums, releaseCleanAll, startFreshCleanAll, test,
} from '../fixtures';

// Regression: the stamping step used to stall here, polling a never-applied transaction for up to 600 s.
test('Clean All finishes when an imported artist folder needs its MBID stamp', async ({ page }) => {
  test.setTimeout(120_000);
  // Needs the album 03-review imported; an unexpected pass without it would be meaningless.
  expect((await libraryAlbums(page)).map((album) => album.album)).toContain('Review Album');

  holdCleanAll();
  await page.goto('/jobs');
  await startFreshCleanAll(page);
  releaseCleanAll();
  await expect(cleanAllPanel(page).getByRole('button', { name: 'Stop' })).toHaveCount(0, { timeout: 60_000 });
  // Stamping has no implementation yet: the step must say so, not report success.
  await expect(cleanAllPanel(page).getByText(/Next phase: Artist Folder Merge/).first()).toBeVisible();
});
