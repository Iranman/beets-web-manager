import type { Route } from '@playwright/test';
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
  await expect(panel.getByText('MusicBrainz ID rejected', { exact: true })).toBeVisible();
  await expect(panel.getByText('This is not a valid MusicBrainz UUID or URL.').first()).toBeVisible();
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

// "Use this release" on a ranked candidate must progress an ALBUM-target review
// to an importable state. The real fixture only seeds an already-eligible item
// (no needs-decision alt_candidates panel), so this test injects a needs-
// decision pending_ai album with alt_candidates at the browser boundary — the
// REAL built frontend still renders and runs the real handlers. It does no
// real mutation (never confirms the import), so it does not disturb the shared
// serial suite. It FAILS on main (clicking "Use this release" only filled the
// manual field; Import stayed disabled) and PASSES after the fix (the chosen
// release auto-validates and the Import action unblocks).
test('Use this release validates the chosen release and unblocks Import (album target)', async ({ page }) => {
  const RG_BEST = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
  const REL_BEST = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb';
  const RG_ALT = 'cccccccc-3333-4333-8333-cccccccccccc';
  const reviewFolder = `/downloads/${FOLDER}`;
  const item = {
    id: 'rq-use-this-release', type: 'pending_ai', status: 'Pending AI', target_kind: 'album',
    path: reviewFolder, folder_name: FOLDER, album: 'Review Album', artist: 'Review Artist',
    albumartist: 'Review Artist', year: 1998, tracks: 2, mb_valid: false,
    auto_import: { eligible: false, reasons: ['needs_human_confirmation'] },
    evidence: { deterministic_decision: { auto_import: { eligible: false, reasons: ['needs_human_confirmation'] } } },
    alt_candidates: {
      best_verified_release_group_id: RG_BEST,
      auto_import: { eligible: false, reasons: ['needs_human_confirmation'] },
      candidates: [
        {
          release_group_id: RG_BEST, representative_release_id: REL_BEST, deterministic_rank: 1, ai_rank: null,
          score: 0.97, action_allowed: true, review_reasons: [], conflicts: [],
          coverage: { local_tracks_verified: 2, local_tracks_total: 2 }, sources: ['musicbrainz'],
          edition_match: ['track_count'],
          mb_release_url: `https://musicbrainz.org/release/${REL_BEST}`,
          mb_release_group_url: `https://musicbrainz.org/release-group/${RG_BEST}`,
          is_best_verified: true, per_track: [],
        },
        {
          release_group_id: RG_ALT, representative_release_id: '', deterministic_rank: 2, ai_rank: null,
          score: 0.6, action_allowed: false, review_reasons: ['track_count_mismatch'], conflicts: [],
          coverage: { local_tracks_verified: 1, local_tracks_total: 2 }, sources: ['musicbrainz'],
          edition_match: [], mb_release_url: '', mb_release_group_url: `https://musicbrainz.org/release-group/${RG_ALT}`,
          is_best_verified: false, per_track: [],
        },
      ],
    },
  };
  const selectedMatch = {
    release_group_id: RG_BEST, representative_release_id: REL_BEST, artist: 'Review Artist', album: 'Review Album',
    year: '1998', track_match_count: 2, total_tracks: 2, local_track_count: 2,
    track_mapping: [
      { num: 1, status: 'matched', source_path: `${reviewFolder}/01 Review One.flac`, local_title: 'Review One', mb_title: 'Review One', mb_trackid: '' },
      { num: 2, status: 'matched', source_path: `${reviewFolder}/02 Review Two.flac`, local_title: 'Review Two', mb_title: 'Review Two', mb_trackid: '' },
    ],
    preflight_status: 'passed', preflight_reason: '', is_release_group_usable: true, is_importable: true,
    is_partial_import: false, confidence_score: 0.97, confidence_level: 'high', auto_fix_eligible: true,
    auto_fix_requires_review: false, auto_fix_reason: 'Validated manual MusicBrainz ID.', missing_track_count: 0,
    match_count: 2, preflight_ok: true, identity_validated: true, source: 'manual',
  };
  const json = (route: Route, body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

  await page.route('**/api/import/review-queue**', (route) => {
    const limit = new URL(route.request().url()).searchParams.get('limit');
    return json(route, { ok: true, items: limit === '0' ? [] : [item], total: 1, offset: 0, limit: 100, counts: { pending_ai: 1 }, origin_counts: {} });
  });
  await page.route('**/api/folders/ai-suggest', (route) => json(route, { ok: true }));
  await page.route('**/api/import-review/manual-id/validate', (route) => {
    const payload = JSON.parse(route.request().postData() || '{}');
    return json(route, {
      ok: true, entity_type: 'release-group', input_mbid: payload.musicbrainz_id, release_group_id: payload.musicbrainz_id,
      representative_release_id: REL_BEST, musicbrainz_url: `https://musicbrainz.org/release-group/${payload.musicbrainz_id}`,
      selected_match: selectedMatch, status_lines: ['2/2 tracks matched', 'Release group verified against the folder.'],
      message: 'Manual MusicBrainz ID validated.',
    });
  });
  await page.route('**/api/folders/import-target-preview', (route) => json(route, {
    ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: [], tracks_to_import_count: 2, real_conflict_count: 0,
    tracks: [{ source_path: `${reviewFolder}/01 Review One.flac` }, { source_path: `${reviewFolder}/02 Review Two.flac` }],
  }));

  await page.goto('/import?tab=review');
  const panel = page.getByRole('tabpanel', { name: /Review Queue/ });
  await expect(panel.getByRole('heading', { name: 'Review Album', level: 2 })).toBeVisible();

  // Before: the album-target Import action is blocked (no visible match selected).
  const importAction = panel.getByRole('button', { name: /Import with ID|Complete Verified Import/ }).first();
  await expect(importAction).toBeDisabled();

  // Act: use the best-verified ranked candidate.
  await panel.getByRole('button', { name: /Use this release/ }).first().click();

  // After: the chosen release is validated on the manual-id pipeline (this step
  // never ran for album targets before the fix)...
  await expect(panel.getByText('Manual MusicBrainz ID validated.').first()).toBeVisible();
  // ...and the Import action becomes available — a usable result from one click.
  await expect(panel.getByRole('button', { name: /Complete Verified Import|Import with ID/ }).first()).toBeEnabled();
});
