// frontend-ux-engineer: most of the owner's stuck Review folders are INCOMPLETE
// downloads (e.g. 1 of 14 tracks present). beets correctly refuses to tag a
// 1-track folder as a full album, but the card only showed the generic "no
// confident match" reason. The review card now surfaces a prominent
// "Incomplete — N of M tracks present" banner, driven by the backend's
// alt_candidates.completeness projection. This locks that it shows for an
// incomplete folder and is absent for a complete one.
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewItem } from '../src/api/types';
import { ReviewCard } from '../src/features/importReview/ImportReviewPage';

afterEach(cleanup);

const RG = '11111111-2222-3333-4444-555555555555';

function altCandidates(completeness: Record<string, unknown>): ReviewItem['alt_candidates'] {
  return {
    best_verified_release_group_id: '',
    auto_import: { eligible: false, reasons: ['no_release_group_verified'] },
    completeness,
    candidates: [{
      release_group_id: RG, representative_release_id: '', deterministic_rank: 1, score: 0.4,
      action_allowed: false, review_reasons: ['no_release_group_verified'], conflicts: [],
      coverage: { local_tracks_verified: 1, local_tracks_total: 1 }, sources: ['musicbrainz'],
      edition_match: [], mb_release_url: '', mb_release_group_url: `https://musicbrainz.org/release-group/${RG}`,
      is_best_verified: false,
    }],
  } as unknown as ReviewItem['alt_candidates'];
}

function renderCard(overrides: Partial<ReviewItem>) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', status: 'Pending AI', path: '/downloads/Chasing Summer',
    album: 'Chasing Summer', artist: 'SiR', mb_valid: false, mb_releasegroupid: RG,
    reason: 'Beets could not confidently match this source to MusicBrainz release … no confident match.',
    evidence: {} as unknown, ...overrides,
  } as unknown as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item} mbid="" selectedMatch={undefined} targetPreviewState={undefined}
        manualFocusToken={0} onMbidChange={noop} onUseCandidate={noop} onFocusManualEntry={noop} onValidateManualId={noop}
        onClearManualId={noop} onSuggest={noop} onApply={noop} onDismiss={noop} onDeleteFolder={noop} onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

describe('Import Review incomplete-download banner', () => {
  it('shows "Incomplete — N of M tracks present" for an incomplete folder', () => {
    renderCard({
      alt_candidates: altCandidates({
        tracks_present: 1, tracks_total: 14, extra_files: 0, incomplete: true,
        summary: 'Incomplete download — 1 of 14 tracks present',
      }),
    });
    const banner = screen.getByTestId('review-incomplete-banner');
    expect(banner.textContent).toContain('1 of 14 tracks present');
  });

  it('does not show the incomplete banner for a complete folder', () => {
    renderCard({
      alt_candidates: altCandidates({
        tracks_present: 14, tracks_total: 14, extra_files: 0, incomplete: false, summary: '',
      }),
    });
    expect(screen.queryByTestId('review-incomplete-banner')).toBeNull();
    expect(screen.queryByText(/tracks present/i)).toBeNull();
  });
});
