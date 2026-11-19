// ARCH-005 (PR2): the ReviewCard "Ready match" badge is driven SOLELY by the
// authoritative deterministic verdict. A refused folder (decide_auto_import
// eligible=false, e.g. release_lookup_failed) must never show "Ready match",
// even when the selected candidate is itself importable with a passed
// preflight. This guards the component badge path (visibleMatchBucket), which
// the decision-fixture parity suite does not render. Round-1 QA FAILED because
// this path was ungated. Reuses existing states: the badge demotes to the
// existing "No MB candidate" label, no new badge.
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import { ReviewCard } from '../src/features/importReview/ImportReviewPage';
import type { SelectedMatch, TargetPreviewState } from '../src/features/importReview/importReviewDecision';

afterEach(cleanup);

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';

// An importable, passed-preflight candidate: on its own this is a Ready match.
const importableMatch = {
  release_group_id: RG, representative_release_id: REL, artist: 'A', album: 'B', year: '2000',
  track_mapping: [{ num: 1, status: 'matched', source_path: '/downloads/a/01.flac', local_title: 't1', mb_title: 'T1', mb_trackid: '' }],
  preflight_status: 'passed', preflight_reason: '', is_release_group_usable: true, is_importable: true,
  is_partial_import: false, identity_validated: true, source: 'candidate',
} as unknown as SelectedMatch;

// A ready, safe target preview matching the one selected track.
const readyPreview = {
  status: 'ready', key: 'k', preview: {
    ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: [],
    tracks_to_import_count: 1, real_conflict_count: 0,
    tracks: [{ source_path: '/downloads/a/01.flac' }],
  },
} as unknown as TargetPreviewState;

function renderCard(evidence?: ReviewEvidence) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', status: 'Pending AI', path: '/downloads/a', album: 'B', artist: 'A',
    mb_valid: true, mb_albumid: REL, evidence,
  } as unknown as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item} mbid={RG} selectedMatch={importableMatch} targetPreviewState={readyPreview} manualFocusToken={0}
        onMbidChange={noop} onUseCandidate={noop} onFocusManualEntry={noop} onValidateManualId={noop}
        onClearManualId={noop} onSuggest={noop} onApply={noop} onDismiss={noop}
        onDeleteFolder={noop} onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

describe('ARCH-005 Import Review Ready badge is verdict-gated', () => {
  it('a refused deterministic verdict never shows "Ready match"', () => {
    renderCard({ origin_step: 'beets_skip', deterministic_decision: { auto_import: { eligible: false, reasons: ['release_lookup_failed:1'] } } } as unknown as ReviewEvidence);
    expect(screen.queryByText('Ready match')).toBeNull();
    expect(screen.getByText('No MB candidate')).toBeTruthy();
  });

  it('an eligible verdict still shows "Ready match"', () => {
    renderCard({ deterministic_decision: { auto_import: { eligible: true, reasons: [] } } } as unknown as ReviewEvidence);
    expect(screen.getByText('Ready match')).toBeTruthy();
  });

  it('no verdict leaves the importable match as "Ready match" (no over-regression)', () => {
    renderCard(undefined);
    expect(screen.getByText('Ready match')).toBeTruthy();
  });
});
