// PR4/Lane D round-2 QA regressions (frontend-ux-engineer). Each test locks a
// QA-confirmed defect in the Review UX overhaul so it cannot silently return:
//   A  "Source year" must reflect the folder/source, never a candidate edition.
//   B  a folder-refused verdict must not show a green folder-level
//      "Auto-import eligible" chip beside the amber "Needs decision" badge.
//   C  the refusal card note must match the panel: say "could not be evaluated"
//      only when there is no verified candidate.
//   D  a refused row carrying only the canonical release-group id (no mb_albumid,
//      no projected alternatives) is "Needs decision", not "No MB candidate".
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import { ReviewCard } from '../src/features/importReview/ImportReviewPage';
import type { SelectedMatch, TargetPreviewState } from '../src/features/importReview/importReviewDecision';

afterEach(cleanup);

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';

// A refused deterministic folder verdict (e.g. release_lookup_failed).
const refusedEvidence = {
  deterministic_decision: { auto_import: { eligible: false, reasons: ['release_lookup_failed:1'] } },
} as unknown as ReviewEvidence;

const baseMatch = {
  release_group_id: RG, representative_release_id: REL, artist: 'A', album: 'B', year: '2000',
  track_mapping: [{ num: 1, status: 'matched', source_path: '/downloads/a/01.flac', local_title: 't1', mb_title: 'T1', mb_trackid: '' }],
  preflight_status: 'passed', preflight_reason: '', is_release_group_usable: true, is_importable: true,
  is_partial_import: false, identity_validated: true, source: 'candidate',
} as unknown as SelectedMatch;

const readyPreview = {
  status: 'ready', key: 'k', preview: {
    ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: [],
    tracks_to_import_count: 1, real_conflict_count: 0, tracks: [{ source_path: '/downloads/a/01.flac' }],
  },
} as unknown as TargetPreviewState;

function renderCard(
  itemOverrides: Partial<ReviewItem>,
  opts: { match?: SelectedMatch | undefined } = {},
) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', status: 'Pending AI', path: '/downloads/a', album: 'B', artist: 'A',
    evidence: refusedEvidence, ...itemOverrides,
  } as unknown as ReviewItem;
  const match = 'match' in opts ? opts.match : baseMatch;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item} mbid={match?.release_group_id ?? ''} selectedMatch={match} targetPreviewState={match ? readyPreview : undefined}
        manualFocusToken={0} onMbidChange={noop} onUseCandidate={noop} onFocusManualEntry={noop} onValidateManualId={noop}
        onClearManualId={noop} onSuggest={noop} onApply={noop} onDismiss={noop} onDeleteFolder={noop} onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

describe('PR4 QA defect A: Source year never conflated with the edition year', () => {
  it('shows the folder guessed_year as "Source year", not item.year (which can be a candidate edition year)', () => {
    renderCard({
      mb_valid: true, mb_albumid: REL,
      year: 1998, // suggestion/edition-derived year — must NOT be shown as the source
      evidence: { ...refusedEvidence, folder: { guessed_year: '2015' } } as unknown as ReviewEvidence,
    });
    const sourceDd = screen.getByText('Source year').nextElementSibling;
    expect(sourceDd?.textContent).toBe('2015');
    const editionDd = screen.getByText('Edition release year').nextElementSibling;
    expect(editionDd?.textContent).toBe('2000');
    // The edition year must not have leaked into the source field.
    expect(sourceDd?.textContent).not.toBe('1998');
  });

  it('falls back to item.year for the source only when folder evidence has no year', () => {
    renderCard({ mb_valid: true, mb_albumid: REL, year: 1998, evidence: refusedEvidence }, { match: undefined });
    expect(screen.getByText('Source year').nextElementSibling?.textContent).toBe('1998');
  });
});

describe('PR4 QA defect B: no contradictory eligibility chip on a refused folder', () => {
  it('a folder-refused verdict with a candidate that passed its own preflight shows no green "Auto-import eligible" chip', () => {
    renderCard({ mb_valid: true, mb_albumid: REL }, {
      match: { ...baseMatch, auto_fix_eligible: true, identity_validated: true } as unknown as SelectedMatch,
    });
    // Folder badge still signals the refusal.
    expect(screen.getByText('Needs decision')).toBeTruthy();
    // No folder-level "Auto-import eligible" indicator (chip OR summary box) may
    // remain — both contradicted the "Needs decision" badge.
    expect(screen.queryByText('Auto-import eligible')).toBeNull();
    // Replaced everywhere by explicitly candidate-scoped wording.
    expect(screen.getAllByText('Selected candidate import-eligible').length).toBeGreaterThanOrEqual(1);
  });

  it('keeps the green "Auto-import eligible" wording when the folder verdict is eligible', () => {
    renderCard(
      { mb_valid: true, mb_albumid: REL, evidence: { deterministic_decision: { auto_import: { eligible: true, reasons: [] } } } as unknown as ReviewEvidence },
      { match: { ...baseMatch, auto_fix_eligible: true, identity_validated: true } as unknown as SelectedMatch },
    );
    expect(screen.getAllByText('Auto-import eligible').length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('Selected candidate import-eligible')).toBeNull();
  });
});

describe('PR4 QA defect C: refusal note consistent with the candidates panel', () => {
  const altWithVerified = {
    best_verified_release_group_id: RG,
    auto_import: { eligible: false, reasons: ['release_lookup_failed:1'] },
    candidates: [{
      release_group_id: RG, representative_release_id: REL, deterministic_rank: 1, score: 0.9,
      action_allowed: false, review_reasons: [], conflicts: [],
      coverage: { local_tracks_verified: 1, local_tracks_total: 1 }, sources: [], edition_match: [],
      mb_release_url: '', mb_release_group_url: '', is_best_verified: true,
    }],
  };

  it('uses the "verified candidate is below" wording (never "could not be evaluated") when a verified candidate exists', () => {
    renderCard({ mb_valid: true, mb_albumid: REL, alt_candidates: altWithVerified as unknown as ReviewItem['alt_candidates'] });
    expect(screen.getByText(/Needs decision: auto-import was refused\. A verified candidate is below/i)).toBeTruthy();
    // Neither the card note nor the panel should claim the candidate was unevaluable.
    expect(screen.queryByText(/could not be evaluated/i)).toBeNull();
  });

  it('keeps the "could not be evaluated" wording when no verified candidate exists', () => {
    const altNoVerified = { ...altWithVerified, best_verified_release_group_id: '' };
    renderCard({ mb_valid: true, mb_albumid: REL, alt_candidates: altNoVerified as unknown as ReviewItem['alt_candidates'] });
    // Both the card note and the panel consistently say it when nothing verified.
    expect(screen.getAllByText(/could not be evaluated/i).length).toBeGreaterThanOrEqual(1);
  });
});

describe('PR4 QA defect D: a release-group-only refused row reads "Needs decision"', () => {
  it('treats a provided canonical mb_releasegroupid (no mb_albumid, no alternatives) as a candidate present', () => {
    renderCard({ mb_valid: false, mb_releasegroupid: RG }, { match: undefined });
    expect(screen.getByText('Needs decision')).toBeTruthy();
    expect(screen.queryByText('No MB candidate')).toBeNull();
  });

  it('still reads "No MB candidate" when the refused row carries no candidate at all', () => {
    renderCard({ mb_valid: false }, { match: undefined });
    expect(screen.getByText('No MB candidate')).toBeTruthy();
    expect(screen.queryByText('Needs decision')).toBeNull();
  });
});
