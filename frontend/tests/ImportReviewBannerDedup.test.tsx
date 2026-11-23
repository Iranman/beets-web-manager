// #813 follow-up (frontend-ux-engineer): a refused no-candidate Import Review
// folder used to stack ~4 redundant top-of-card notices (the decision note, the
// raw backend reason, the "no valid ID" match note, and the "Beets skipped this
// folder" evidence line). They are now consolidated into the single decision
// banner. This locks that consolidation so the stack cannot silently return, and
// — critically — that the Beets skip-reason code is NOT lost in the process.
//
// Fixtures are backend-shaped: the Beets skip code ("no_strong_match") is carried
// ONLY in item.reason / evidence.beets_skip_reason, NEVER in auto_import.reasons
// (decide_auto_import only folds "duplicate"), so the AltCandidatesPanel "Why
// auto-import was refused" disclosure does not hold it. The code must therefore
// stay reachable on the card another way — it is relocated into the decision
// banner's own "Why Beets skipped this folder" disclosure.
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import { ReviewCard, UNRESOLVED_ID_GUIDANCE_SHORT } from '../src/features/importReview/ImportReviewPage';

afterEach(cleanup);

const RG = '11111111-2222-3333-4444-555555555555';
const RG_CAND = '99999999-8888-7777-6666-555555555555';
// The backend assembles item.reason with the Beets skip code as the FIRST
// parenthetical and the candidate "why" as the second (ai_service.py).
const SKIP_CODE = 'no_strong_match';
const REASON =
  `Beets skipped this folder (${SKIP_CODE}). Best MusicBrainz candidate needs a decision (fingerprint_group_not_evaluated, no_release_group_verified).`;
const SKIP_NOTE = 'Beets skipped this folder; the evidence below is from Web Manager.';
const MATCH_NOTE_LEAD = 'No valid MusicBrainz release-group ID is selected yet.';

// A deterministically-refused folder that Beets skipped (origin_step beets_skip).
// The skip code lives in beets_skip_reason, NOT in deterministic auto_import.reasons.
const refusedSkipEvidence = {
  origin_step: 'beets_skip',
  beets_skip_reason: SKIP_CODE,
  deterministic_decision: { auto_import: { eligible: false, reasons: ['needs_human_confirmation'] } },
} as unknown as ReviewEvidence;

// A ranked (unverified) candidate present, so the "Why auto-import was refused"
// disclosure renders — but its reasons do NOT contain the skip code (true to the
// backend), proving the skip code must survive via the relocated reason instead.
const altNoVerified = {
  best_verified_release_group_id: '',
  auto_import: { eligible: false, reasons: ['needs_human_confirmation'] },
  candidates: [{
    release_group_id: RG_CAND, representative_release_id: '', deterministic_rank: 1, score: 0.6,
    action_allowed: false, review_reasons: ['no_release_group_verified'], conflicts: [],
    coverage: { local_tracks_verified: 1, local_tracks_total: 3 }, sources: ['musicbrainz'], edition_match: [],
    mb_release_url: '', mb_release_group_url: `https://musicbrainz.org/release-group/${RG_CAND}`, is_best_verified: false,
  }],
} as unknown as ReviewItem['alt_candidates'];

function renderCard(overrides: Partial<ReviewItem>) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', status: 'Pending AI', path: '/downloads/a', album: 'Boom Box', artist: 'AC/DC',
    mb_valid: false, mb_releasegroupid: RG, reason: REASON, evidence: refusedSkipEvidence, ...overrides,
  } as unknown as ReviewItem;
  // No selected match: the card shows its deterministic refusal state (the state
  // in which the four banners stacked).
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

function decisionBanner(): HTMLElement {
  const sentence = screen.getByText(/Needs decision: the best candidate could not be evaluated/i);
  const banner = sentence.closest('div');
  expect(banner).toBeTruthy();
  return banner as HTMLElement;
}

describe('#813 follow-up: refused-folder notices consolidate into one decision banner', () => {
  it('shows one decision banner, folds the guidance once, and keeps the Beets skip code reachable', () => {
    renderCard({ alt_candidates: altNoVerified });
    const banner = decisionBanner();

    // The actionable guidance is folded into the banner — and the duplicate
    // "enter a MusicBrainz ID" clause is dropped (the decision sentence says it).
    expect(within(banner).getByText(UNRESOLVED_ID_GUIDANCE_SHORT)).toBeTruthy();
    expect((banner.textContent || '').split('enter a MusicBrainz ID').length - 1).toBe(1);

    // The three redundant *top-level* notices are gone as their own boxes.
    expect(screen.queryByText(MATCH_NOTE_LEAD, { exact: false })).toBeNull();
    expect(screen.queryByText(SKIP_NOTE)).toBeNull();

    // HARD CONSTRAINT: the Beets skip code ("no_strong_match") is still reachable
    // on the card — relocated into the banner's "Why Beets skipped" disclosure,
    // not left only in auto_import.reasons (where the backend never puts it).
    const whyBeets = within(banner).getByText(/Why Beets skipped this folder/i);
    expect(whyBeets).toBeTruthy();
    const reasonText = within(banner).getByText(REASON);
    expect(reasonText.closest('details')).toBeTruthy(); // inside a disclosure, not a top banner
    expect(reasonText.textContent).toContain(SKIP_CODE);
  });

  it('keeps the skip code reachable for a release-group-only row with no ranked-candidates disclosure', () => {
    // No alt_candidates: the AltCandidatesPanel does not render at all, so the
    // skip code has only the relocated reason to live in — it must still be there.
    renderCard({});
    const banner = decisionBanner();
    expect(within(banner).getByText(/Why Beets skipped this folder/i)).toBeTruthy();
    const reasonText = within(banner).getByText(REASON);
    expect(reasonText.textContent).toContain(SKIP_CODE);
    // Still no standalone "Beets skipped … evidence below" top-level line.
    expect(screen.queryByText(SKIP_NOTE)).toBeNull();
  });
});
