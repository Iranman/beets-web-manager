/**
 * #521: an album Find Match the AI artist gate rejected (identity_validated
 * false) must stay blocked. The candidate track comparison
 * (/api/candidates/<id>/tracks) does not run the artist gate and always
 * answers identity_validated true, so the carousel must not let that rebuilt
 * match overwrite the suggestion's rejection.
 */
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn() };
});

import { apiGet } from '../src/lib/api';
import { CandidateCarousel } from '../src/features/importReview/ImportReviewPage';
import { applyBlockReason, actionLabel, type SelectedMatch } from '../src/features/importReview/importReviewDecision';

afterEach(cleanup);

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';
const ARTIST_ERROR = 'AI candidate artist Other does not match album artist Right.';
const libraryAlbum = { id: 'library:7', type: 'library_no_mb', target_kind: 'album', album_id: 7 } as unknown as ReviewItem;

const tracks = {
  ok: true,
  mb_albumid: REL,
  representative_release_id: REL,
  mb_releasegroupid: RG,
  selected_release_group_id: RG,
  identity_validated: true,
  candidate_identity_error: '',
  comparison: [{ num: 1, local_title: 'a', mb_title: 'A', mb_trackid: '', status: 'matched', source_path: '/m/01.flac' }],
  matched_count: 1,
  fuzzy_count: 0,
  extra_count: 0,
  mb_track_count: 1,
  local_track_count: 1,
  preflight: { ok: true, matches: 1, expected: 1, audio_count: 1 },
};

async function lastSelectedMatch(identityRejection?: Parameters<typeof CandidateCarousel>[0]['identityRejection']) {
  vi.mocked(apiGet).mockResolvedValue(tracks);
  const onSelectMatch = vi.fn<(match: SelectedMatch) => void>();
  render(
    <CandidateCarousel
      candidates={[{ artist: 'Other', album: 'B', mb_albumid: REL, mb_releasegroupid: RG }]}
      selectedMbid={RG}
      onUseCandidate={() => undefined}
      onSelectMatch={onSelectMatch}
      identityRejection={identityRejection}
    />,
  );
  // Wait for the match rebuilt from the track comparison, not the pending one.
  await waitFor(() => expect(onSelectMatch.mock.lastCall?.[0].track_mapping.length).toBe(1));
  return onSelectMatch.mock.lastCall![0];
}

describe('Candidate carousel keeps the artist identity rejection (#521)', () => {
  it('a rebuilt match for the rejected Release Group stays blocked', async () => {
    const match = await lastSelectedMatch({ identity_validated: false, candidate_identity_error: ARTIST_ERROR, mb_releasegroupid: RG });
    expect(match.identity_validated).toBe(false);
    expect(match.candidate_identity_error).toBe(ARTIST_ERROR);
    expect(match.is_importable).toBe(false);
    expect(applyBlockReason(libraryAlbum, RG, match)).toBe(ARTIST_ERROR);
    expect(actionLabel(libraryAlbum, match)).toBe('Import blocked');
  });

  it('a validated suggestion leaves the rebuilt match usable', async () => {
    const match = await lastSelectedMatch({ identity_validated: true, mb_releasegroupid: RG });
    expect(match.identity_validated).toBe(true);
    expect(applyBlockReason(libraryAlbum, RG, match)).toBe('');
    expect(actionLabel(libraryAlbum, match)).toBe('Match album');
  });

  it('a rejection of another Release Group does not block this candidate', async () => {
    const match = await lastSelectedMatch({
      identity_validated: false,
      candidate_identity_error: ARTIST_ERROR,
      mb_releasegroupid: 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff',
    });
    expect(match.identity_validated).toBe(true);
  });
});
