import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  RecoveryReviewCard,
  classifyAcoustid,
  classifyAi,
  recoveryCounts,
} from '../src/features/importReview/RecoveryReviewCard';
import type { AiSuggestResponse, RecoveryCarrierIdentity, ReviewItem } from '../src/api/types';

afterEach(cleanup);

// Canonical target: 112 "Forever". A deliberately unrelated title-collision
// candidate (FLEUR "FOREVER FOREVER") is injected into the suggestion to prove
// it can never become the primary identity or the primary action.
const TARGET_RG = '11111111-1111-4111-8111-111111111111';
const REC_1 = '22222222-2222-4222-8222-222222222222';
const REC_2 = '33333333-3333-4333-8333-333333333333';
const FLEUR_RG = '99999999-9999-4999-8999-999999999999';

function carrier(overrides: Partial<RecoveryCarrierIdentity> = {}): RecoveryCarrierIdentity {
  return {
    recovery: true,
    recovery_carrier_id: 'carrier-1',
    recovery_identity_key: 'key-1',
    existing_album_id: 42,
    mb_releasegroupid: TARGET_RG,
    release_id: '',
    destination_album_path: '/music/112/Forever',
    wanted_tracks: [],
    expected_tracks: [
      { recording_id: REC_1, track: 3, disc: 1, title: 'Peaches & Cream' },
      { recording_id: REC_2, track: 7, disc: 1, title: 'It’s Over Now' },
    ],
    tracks: [
      { recording_id: REC_1, title: 'Peaches & Cream', position: 3, disc: 1, status: 'verified' },
      { recording_id: REC_2, title: 'It’s Over Now', position: 7, disc: 1, status: 'pending' },
    ],
    provenance: { method: 'slskd', query: '112 forever' },
    confidence: 'review_required',
    review_reason: 'Downloaded recording not verified against the expected track.',
    matching_decision: {
      action_allowed: false,
      identity_proof: 'release_group_id',
      recommended_action: 'Verify the download before importing.',
      dimensions: {
        identifier_syntax: { ok: true, status: 'valid', detail: 'Release Group identifier is a well-formed MBID.' },
        mb_entity_lookup: { ok: true, status: 'resolved', detail: 'MusicBrainz release resolved with 12 track(s).' },
        album_identity_verification: { ok: true, status: 'verified', detail: 'Album identity verified.' },
        downloaded_recording_verification: { ok: false, status: 'none', detail: 'Per-track identity proof: insufficient.' },
        import_eligibility: { ok: false, status: 'review_required', detail: 'Verify the download before importing.' },
      },
    },
    ...overrides,
  };
}

function item(c: RecoveryCarrierIdentity, extra: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: 'r1',
    type: 'pending_ai',
    album: 'Forever',
    artist: '112',
    evidence: { recovery_carrier: c },
    ...extra,
  } as unknown as ReviewItem;
}

// A suggestion response carrying the unrelated FLEUR candidate + AI status.
function suggestionWithFleur(aiContributed: boolean, acoustidStatus = 'no_result'): AiSuggestResponse {
  return {
    ok: true,
    recording_candidates: [
      {
        mb_trackid: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
        mb_releasegroupid: FLEUR_RG,
        recording_title: 'FOREVER FOREVER',
        recording_artist: 'FLEUR',
        album: 'FOREVER FOREVER',
      },
    ],
    evidence: { fingerprint: { status: acoustidStatus } },
    suggestions: {
      ai_status: {
        configured: true,
        attempted: true,
        contributed: aiContributed,
        available: aiContributed,
        unavailable_reason: aiContributed ? '' : 'AI refusal: cannot identify',
        last_outcome: aiContributed ? 'answered' : 'rejected',
        last_status_code: aiContributed ? 200 : 401,
      },
    },
    confidence_source: aiContributed ? 'ai' : 'none',
  } as unknown as AiSuggestResponse;
}

describe('RecoveryReviewCard — information architecture', () => {
  it('renders WANTED / DOWNLOADED / WENT WRONG / NEXT sections', () => {
    render(<RecoveryReviewCard item={item(carrier())} onPrimaryAction={() => {}} />);
    const card = screen.getByLabelText('Missing-track recovery review');
    expect(within(card).getByText('Wanted')).toBeTruthy();
    expect(within(card).getByText('Downloaded')).toBeTruthy();
    expect(within(card).getByText('What needs checking')).toBeTruthy();
    expect(card.querySelector('[data-recovery-section="next"]')).toBeTruthy();
  });

  it('shows which tracks are missing with titles, numbers and recording MBID links', () => {
    render(<RecoveryReviewCard item={item(carrier())} onPrimaryAction={() => {}} />);
    const expected = screen.getByTestId('recovery-expected-tracks');
    expect(within(expected).getByText(/Peaches & Cream/)).toBeTruthy();
    expect(within(expected).getByText('Track 3')).toBeTruthy();
    // Recording MBID is a real MusicBrainz link.
    const link = within(expected).getByRole('link', { name: new RegExp(REC_1, 'i') });
    expect(link.getAttribute('href')).toBe(`https://musicbrainz.org/recording/${REC_1}`);
  });

  it('shows expected-vs-present counts', () => {
    render(<RecoveryReviewCard item={item(carrier())} onPrimaryAction={() => {}} />);
    // 1 verified present of 2 expected, 1 still missing.
    expect(screen.getByText(/1 of 2 expected tracks present/)).toBeTruthy();
    expect(screen.getByText(/1 still missing/)).toBeTruthy();
  });

  it('shows per-track download/verification status', () => {
    render(<RecoveryReviewCard item={item(carrier())} onPrimaryAction={() => {}} />);
    const status = screen.getByTestId('recovery-track-status');
    expect(status.querySelector('[data-track-status="verified"]')).toBeTruthy();
    expect(status.querySelector('[data-track-status="pending"]')).toBeTruthy();
  });

  it('renders the five decision dimensions separately, not one blob', () => {
    render(<RecoveryReviewCard item={item(carrier())} onPrimaryAction={() => {}} />);
    const dims = screen.getByTestId('recovery-dimensions');
    for (const key of [
      'identifier_syntax',
      'mb_entity_lookup',
      'album_identity_verification',
      'downloaded_recording_verification',
      'import_eligibility',
    ]) {
      expect(dims.querySelector(`[data-dimension="${key}"]`)).toBeTruthy();
    }
  });

  it('exposes exactly one primary action, disabled while the contract does not allow import', () => {
    render(<RecoveryReviewCard item={item(carrier())} onPrimaryAction={() => {}} />);
    const buttons = screen.getAllByTestId('recovery-primary-action');
    expect(buttons).toHaveLength(1);
    expect((buttons[0] as HTMLButtonElement).disabled).toBe(true);
    expect(buttons[0].textContent).toMatch(/needs verification/i);
  });

  it('enables the primary action and fires it when the contract allows import', () => {
    const onPrimary = vi.fn();
    const c = carrier({
      matching_decision: {
        action_allowed: true,
        identity_proof: 'deterministic_track_recording_id',
        dimensions: carrier().matching_decision!.dimensions,
      },
    });
    render(<RecoveryReviewCard item={item(c)} onPrimaryAction={onPrimary} />);
    const btn = screen.getByTestId('recovery-primary-action') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    btn.click();
    expect(onPrimary).toHaveBeenCalledTimes(1);
  });

  it('returns null for a non-recovery item', () => {
    const { container } = render(
      <RecoveryReviewCard item={{ id: 'x', type: 'pending_ai', evidence: {} } as unknown as ReviewItem} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe('RecoveryReviewCard — expected vs suggested (req 6): unrelated never primary', () => {
  it('shows the carrier Release Group as the primary identity and keeps an unrelated candidate OUT of it', () => {
    render(
      <RecoveryReviewCard item={item(carrier())} suggestion={suggestionWithFleur(false)} onPrimaryAction={() => {}} />,
    );
    const primary = screen.getByTestId('recovery-primary-identity');
    // The primary identity is the canonical target RG.
    expect(within(primary).getByText(TARGET_RG)).toBeTruthy();
    // The unrelated title-collision candidate is NEVER in the primary identity.
    expect(within(primary).queryByText(/FOREVER FOREVER/)).toBeNull();
    expect(within(primary).queryByText(FLEUR_RG)).toBeNull();
  });

  it('shows the unrelated candidate ONLY in the clearly separated secondary section', () => {
    render(
      <RecoveryReviewCard item={item(carrier())} suggestion={suggestionWithFleur(false)} onPrimaryAction={() => {}} />,
    );
    const alts = screen.getByTestId('recovery-alternatives');
    expect(within(alts).getByText('FOREVER FOREVER')).toBeTruthy();
    // The alternatives section carries no primary action.
    expect(alts.querySelector('[data-testid="recovery-primary-action"]')).toBeNull();
  });

  it('never promotes the unrelated candidate to the single primary action', () => {
    render(
      <RecoveryReviewCard item={item(carrier())} suggestion={suggestionWithFleur(false)} onPrimaryAction={() => {}} />,
    );
    const btn = screen.getByTestId('recovery-primary-action');
    expect(btn.textContent).not.toMatch(/FOREVER FOREVER|FLEUR/i);
    // The disabled, carrier-bound primary never carries the collision identity.
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('RecoveryReviewCard — provider chips (req 7)', () => {
  it('reads AcoustID "no match in database" differently from "not configured"', () => {
    const { rerender } = render(
      <RecoveryReviewCard item={item(carrier())} suggestion={suggestionWithFleur(false, 'no_result')} onPrimaryAction={() => {}} />,
    );
    let chips = screen.getByTestId('recovery-provider-chips');
    expect(chips.querySelector('[data-provider="AcoustID"]')?.getAttribute('data-provider-state')).toBe('No match in database');

    rerender(
      <RecoveryReviewCard
        item={item(carrier())}
        suggestion={{ ok: true, evidence: { fingerprint: { status: 'unavailable', unavailable_reason: 'not_configured' } } } as unknown as AiSuggestResponse}
        onPrimaryAction={() => {}}
      />,
    );
    chips = screen.getByTestId('recovery-provider-chips');
    expect(chips.querySelector('[data-provider="AcoustID"]')?.getAttribute('data-provider-state')).toBe('Not configured');
  });

  it('NEVER shows AI confidence when AI did not contribute (rejected key)', () => {
    render(
      <RecoveryReviewCard
        item={item(carrier(), { suggestion: { confidence: 'low' } as unknown as ReviewItem['suggestion'] })}
        suggestion={suggestionWithFleur(false)}
        onPrimaryAction={() => {}}
      />,
    );
    const chips = screen.getByTestId('recovery-provider-chips');
    // AI chip reports rejected, not a confidence.
    expect(chips.querySelector('[data-provider="AI"]')?.getAttribute('data-provider-state')).toBe('Rejected');
    // No AI confidence is rendered anywhere.
    expect(screen.queryByTestId('recovery-ai-confidence')).toBeNull();
  });

  it('shows AI confidence only when AI actually contributed', () => {
    render(
      <RecoveryReviewCard
        item={item(carrier(), { suggestion: { confidence: 'high' } as unknown as ReviewItem['suggestion'] })}
        suggestion={suggestionWithFleur(true)}
        onPrimaryAction={() => {}}
      />,
    );
    expect(screen.getByTestId('recovery-ai-confidence').textContent).toMatch(/high/i);
    expect(screen.getByTestId('recovery-provider-chips').querySelector('[data-provider="AI"]')?.getAttribute('data-provider-state')).toBe('Contributed');
  });
});

describe('classifier units', () => {
  it('classifyAcoustid distinguishes no_result from not_configured', () => {
    expect(classifyAcoustid({ status: 'no_result' }).text).toBe('No match in database');
    expect(classifyAcoustid({ status: 'unavailable', unavailable_reason: 'not_configured' }).tone).toBe('muted');
    expect(classifyAcoustid({ status: 'unavailable', unavailable_reason: 'not_configured' }).text).toBe('Not configured');
  });

  it('classifyAi never shows confidence unless contributed', () => {
    expect(classifyAi({ configured: true, contributed: false, last_status_code: 401 }).showConfidence).toBe(false);
    expect(classifyAi({ configured: true, contributed: false, last_status_code: 401 }).text).toBe('Rejected');
    expect(classifyAi({ configured: false, contributed: false }).text).toBe('Not configured');
    expect(classifyAi({ configured: true, attempted: true, contributed: true }).showConfidence).toBe(true);
    // Absent ai_status => suppressed by default.
    expect(classifyAi(undefined, undefined).showConfidence).toBe(false);
  });

  it('recoveryCounts derives present/missing from per-track status', () => {
    const counts = recoveryCounts(carrier());
    expect(counts.expected).toBe(2);
    expect(counts.present).toBe(1);
    expect(counts.pending).toBe(1);
  });
});
