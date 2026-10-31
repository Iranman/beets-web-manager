import { expect, filesUnder, libraryAlbums, root, test } from '../fixtures';

const GROUP = 'cccccccc-1111-4111-8111-111111111111';
const RELEASE = 'bbbbbbbb-1111-4111-8111-111111111111';
const FOLDER = 'Review Artist - Review Album';

test.describe.configure({ mode: 'serial' });

test('review queue shows the suggestion with full, correct MusicBrainz links', async ({ page }) => {
  await page.goto('/import?tab=review');
  const panel = page.getByRole('tabpanel', { name: /Review Queue/ });
  await expect(panel.getByRole('heading', { name: FOLDER, level: 2 })).toBeVisible();

  const group = panel.getByRole('link', { name: `Open Release group ${GROUP} on MusicBrainz in a new tab` });
  await expect(group).toHaveAttribute('href', `https://musicbrainz.org/release-group/${GROUP}`);
  await expect(group).toHaveAttribute('target', '_blank');
  await expect(group).toHaveText(GROUP);
  const release = panel.getByRole('link', { name: `Open Representative Release ID ${RELEASE} on MusicBrainz in a new tab` });
  await expect(release).toHaveAttribute('href', `https://musicbrainz.org/release/${RELEASE}`);
});

test('a manual ID that is not a UUID is refused', async ({ page }) => {
  await page.goto('/import?tab=review');
  const panel = page.getByRole('tabpanel', { name: /Review Queue/ });
  const field = panel.getByRole('textbox', { name: /MusicBrainz Release or Release Group ID/ });
  await expect(field).toBeVisible();
  await field.fill('not-a-musicbrainz-id');
  await panel.getByRole('button', { name: 'Validate ID' }).click();
  await expect(panel.getByText(/valid MusicBrainz|UUID/i).first()).toBeVisible();
  await expect(panel.getByText('Valid MusicBrainz ID')).toHaveCount(0);
});

test('nothing imports without approval: viewing and cancelling leave the folder alone', async ({ page }) => {
  const albumsBefore = (await libraryAlbums(page)).length;
  await page.goto('/import?tab=review');
  const panel = page.getByRole('tabpanel', { name: /Review Queue/ });
  await panel.getByRole('button', { name: 'Complete Verified Import' }).click();
  const dialog = page.getByRole('dialog', { name: 'Start Tagging Job' });
  await expect(dialog).toContainText(GROUP);
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toHaveCount(0);

  expect(filesUnder(root('downloads', FOLDER))).toHaveLength(2);
  expect(await libraryAlbums(page)).toHaveLength(albumsBefore);
});

test('approving imports the album into Beets under its release group and clears the queue', async ({ page }) => {
  await page.goto('/import?tab=review');
  const panel = page.getByRole('tabpanel', { name: /Review Queue/ });
  await panel.getByRole('button', { name: 'Complete Verified Import' }).click();
  await page.getByRole('dialog', { name: 'Start Tagging Job' }).getByRole('button', { name: 'Confirm' }).click();

  await expect
    .poll(async () => (await libraryAlbums(page)).find((album) => album.album === 'Review Album')?.mb_releasegroupid, {
      timeout: 60_000,
    })
    .toBe(GROUP);
  const imported = filesUnder(root('music')).filter((file) => file.includes('Review One') || file.includes('Review Two'));
  expect(imported).toHaveLength(2);

  await page.goto('/import?tab=review');
  await expect(page.getByRole('tab', { name: 'Review Queue', exact: true })).toBeVisible();
  await expect(page.getByRole('tabpanel', { name: /Review Queue/ }).getByRole('heading', { name: FOLDER, level: 2 })).toHaveCount(0);
});
