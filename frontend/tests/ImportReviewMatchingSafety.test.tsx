import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MatchingSafetyPanel } from '../src/features/importReview/ImportReviewPage';

afterEach(cleanup);

describe('MatchingSafetyPanel', () => {
  it('shows canonical release-group evidence, alignment counts, and blockers', () => {
    render(
      <MatchingSafetyPanel
        selectedMatch={{
          source: 'musicbrainz',
          matching_contract: {
            evidence: {
              canonical_match: {
                state: 'conflict',
                release_group_status: 'validated',
                suggested_identity: {
                  release_group_id: '11111111-1111-1111-1111-111111111111',
                },
                track_alignment: {
                  matched_count: 1,
                  total_target_tracks: 3,
                  missing_count: 2,
                  unmatched_local_count: 1,
                },
                conflicts: ['acoustid_conflict'],
                review_reasons: ['missing_canonical_tracks', 'extra_local_tracks'],
              },
            },
            warnings: ['title_differs'],
          },
        } as never}
      />,
    );

    expect(screen.getByText('Evidence state:')).toBeTruthy();
    expect(screen.getByText('conflict')).toBeTruthy();
    expect(screen.getByText('Release Group evidence:')).toBeTruthy();
    expect(screen.getByText('validated')).toBeTruthy();
    expect(screen.getByText('Canonical RGID:')).toBeTruthy();
    expect(screen.getByText('11111111-1111-1111-1111-111111111111')).toBeTruthy();
    expect(screen.getByText('Canonical alignment:')).toBeTruthy();
    expect(screen.getByText('1/3 tracks')).toBeTruthy();
    expect(screen.getByText('acoustid_conflict')).toBeTruthy();
    expect(screen.getByText('missing_canonical_tracks')).toBeTruthy();
    expect(screen.getByText('extra_local_tracks')).toBeTruthy();
  });
});
