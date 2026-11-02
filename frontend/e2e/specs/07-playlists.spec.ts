import { expect, test } from '../fixtures';

test('empty state, and actions stay disabled until there is input', async ({ page }) => {
  await page.goto('/playlists');
  await expect(page.getByRole('main').getByRole('alert')).toHaveText('No playlists have been saved yet.');
  await expect(page.getByRole('button', { name: 'Run Pipeline' })).toBeDisabled();
  await page.getByRole('button', { name: /Add or Import Playlist/ }).click();
  await expect(page.getByRole('button', { name: 'Preview Matches' })).toBeDisabled();
});

test('a track list previews against the library and marks what is already owned', async ({ page }) => {
  await page.goto('/playlists');
  await page.getByRole('button', { name: /Add or Import Playlist/ }).click();
  await page.getByRole('textbox', { name: 'Playlist name' }).fill('E2E Mix');
  await page.getByRole('combobox', { name: 'Source', exact: true }).selectOption('text');
  await page.getByRole('textbox', { name: 'Track list' }).fill('Synthetic Aurora - First Light\nNobody Real - Not In Library');
  const parsed = page.waitForResponse((r) => r.url().includes('/api/playlist/parse'));
  await page.getByRole('button', { name: 'Preview Matches' }).click();
  const response = await parsed;
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { matched: { title: string; album: string }[]; missing: { title: string }[] };
  expect(body.matched.map((track) => [track.title, track.album])).toEqual([['First Light', 'Glass Horizons']]);
  expect(body.missing.map((track) => track.title)).toEqual(['Not In Library']);

  const main = page.getByRole('main');
  await main.getByRole('button', { name: 'Available 1' }).click();
  // Exact: the track-list textarea above holds both lines.
  const owned = main.getByText('Synthetic Aurora - First Light', { exact: true });
  const absent = main.getByText('Nobody Real - Not In Library', { exact: true });
  await expect(owned).toBeVisible();
  await expect(absent).toHaveCount(0);
  await main.getByRole('button', { name: 'Missing 1' }).click();
  await expect(absent).toBeVisible();
  await expect(owned).toHaveCount(0);
});
