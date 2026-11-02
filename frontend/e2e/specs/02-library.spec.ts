import { expect, libraryAlbums, test } from '../fixtures';

test('search narrows artists; clearing brings them back', async ({ page }) => {
  await page.goto('/library');
  const search = page.getByRole('textbox', { name: 'Search' });
  await search.fill('Flight Recorder');
  await expect(page.getByRole('button', { name: /The Test Pilots artwork/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Synthetic Aurora artwork/ })).toHaveCount(0);
  await search.fill('');
  await expect(page.getByRole('button', { name: /Synthetic Aurora artwork/ })).toBeVisible();
});

test('letter index filters artists and disables letters with none', async ({ page }) => {
  await page.goto('/library');
  await expect(page.getByRole('button', { name: 'Q', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'T', exact: true }).click();
  await expect(page.getByRole('button', { name: /The Test Pilots artwork/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Unknown Quantity artwork/ })).toHaveCount(0);
});

test('artist page lists that artist\'s albums with the right counts', async ({ page }) => {
  await page.goto('/library');
  await page.getByRole('button', { name: /Synthetic Aurora artwork/ }).click();
  const main = page.getByRole('main');
  await expect(main.getByRole('heading', { name: 'Synthetic Aurora', level: 2 })).toBeVisible();
  const cards = main.getByRole('article');
  await expect(cards).toHaveCount(2);
  await expect(cards.filter({ hasText: 'Glass Horizons' })).toContainText('3 tracks');
  await expect(cards.filter({ hasText: 'Static Bloom' })).toContainText('2 tracks');

  await main.getByRole('button', { name: 'Artists', exact: true }).click();
  await expect(page.getByRole('button', { name: /The Test Pilots artwork/ })).toBeVisible();
});

test('album details show the tracks stored in Beets, with canonical release-group identity', async ({ page }) => {
  await page.goto('/library');
  await page.getByRole('button', { name: /Synthetic Aurora artwork/ }).click();
  await page.getByRole('article').filter({ hasText: 'Glass Horizons' }).getByRole('button', { name: 'View details' }).click();
  const dialog = page.getByRole('dialog', { name: 'Glass Horizons' });
  await expect(dialog.getByText('3 tracks', { exact: true })).toBeVisible();
  for (const title of ['First Light', 'Paper Moons', 'Long Exposure']) {
    await expect(dialog.getByText(title)).toBeVisible();
  }
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(dialog).toHaveCount(0);

  const album = (await libraryAlbums(page)).find((candidate) => candidate.album === 'Glass Horizons');
  expect(album?.mb_releasegroupid).toBe('11111111-1111-4111-8111-111111111111');
});

test('album without IDs offers the repair path and appears under Missing IDs', async ({ page }) => {
  await page.goto('/library');
  await page.getByRole('button', { name: /Unknown Quantity artwork/ }).click();
  await expect(page.getByRole('article').filter({ hasText: 'Untagged Sessions' })).toContainText(/Needs IDs/);
  await page.goto('/library/missing-ids');
  await expect(page.getByRole('main')).toContainText('Untagged Sessions');
  await expect(page.getByRole('main')).not.toContainText('Glass Horizons');
});
