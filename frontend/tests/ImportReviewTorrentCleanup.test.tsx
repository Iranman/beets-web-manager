// "Remove N rejected file(s)…" must not be offered on rows whose folder is a
// qBittorrent torrent (moving files out would break seeding); local rows keep it.
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import { ReviewCard, TORRENT_CLEANUP_REASON } from '../src/features/importReview/ImportReviewPage';
import type { SelectedMatch } from '../src/features/importReview/importReviewDecision';

afterEach(cleanup);

const EXTRA = '/downloads/A/B/99 Extra.flac';

function renderCard(evidence?: ReviewEvidence) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', path: '/downloads/A/B', album: 'B', artist: 'A',
    blocked_reason: 'One extra file needs review.', evidence,
  } as unknown as ReviewItem;
  const selectedMatch = {
    release_group_id: '', representative_release_id: '', artist: 'A', album: 'B', year: '',
    track_mapping: [{ status: 'extra', source_path: EXTRA, local_title: 'Extra' }],
    preflight_status: 'failed', preflight_reason: '', is_importable: false,
  } as unknown as SelectedMatch;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item}
        mbid=""
        selectedMatch={selectedMatch}
        manualFocusToken={0}
        onMbidChange={noop}
        onUseCandidate={noop}
        onFocusManualEntry={noop}
        onValidateManualId={noop}
        onClearManualId={noop}
        onSuggest={noop}
        onApply={noop}
        onDismiss={noop}
        onDeleteFolder={noop}
        onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

const removeButton = () => screen.queryByRole('button', { name: /Remove 1 rejected file/ });

describe('Import Review rejected-file cleanup on torrent rows', () => {
  it('withholds cleanup on a qBittorrent row', () => {
    renderCard({ source_mode: 'qbittorrent' });
    expect(removeButton()).toBeNull();
    expect(screen.getByText(TORRENT_CLEANUP_REASON)).toBeTruthy();
  });

  it('withholds cleanup on a quality-upgrade row', () => {
    renderCard({ origin_step: 'quality_upgrade', duplicate_rule: 'quality_upgrade' });
    expect(removeButton()).toBeNull();
    expect(screen.getByText(TORRENT_CLEANUP_REASON)).toBeTruthy();
  });

  it('still offers cleanup on a normal local row', () => {
    renderCard({ source_mode: 'local' });
    expect(removeButton()).not.toBeNull();
    expect(screen.queryByText(TORRENT_CLEANUP_REASON)).toBeNull();
  });
});
