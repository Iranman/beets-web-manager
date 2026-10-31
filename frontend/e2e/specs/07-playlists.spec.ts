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
  expect((await parsed).ok()).toBeTruthy();
  await expect(page.getByRole('main')).toContainText('First Light');
  await expect(page.getByRole('main')).toContainText('Not In Library');
});
