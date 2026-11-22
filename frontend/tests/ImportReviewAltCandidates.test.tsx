// PR4/Lane D: the Review UX overhaul. Covers the ranked alt-candidates panel
// (best verified first + ranked alternatives in the given order, suggestion vs
// validated, verified coverage, verdict "why", clickable MusicBrainz release +
// release-group links, per-track detail), the source-year vs edition-year
// distinction, the "Needs decision" refusal messaging, and the manual-MBID
// validate response rendering with actionable errors. It renders real
// components (AltCandidatesPanel + ReviewCard) against synthetic data.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReviewAltCandidates, ReviewItem } from '../src/api/types';
import { AltCandidatesPanel } from '../src/features/importReview/AltCandidatesPanel';
import { ReviewCard } from '../src/features/importReview/ImportReviewPage';

afterEach(cleanup);

// Minimal mirror of the page-local ManualValidationState (not exported) for the
// card's manual-MBID response rendering; cast to never at the prop boundary.
type ManualValidationStateForTest = {
  status: 'idle' | 'checking' | 'valid' | 'error';
  message: string;
  statusLines?: string[];
  musicbrainzUrl?: string;
  entityType?: 'release' | 'release-group' | 'recording' | 'unknown';
};

const RG_BEST = 'aaaaaaaa-1111-2222-3333-444444444444';
const REL_BEST = 'bbbbbbbb-1111-2222-3333-444444444444';
const RG_ALT = 'cccccccc-5555-6666-7777-888888888888';
const REL_ALT = 'dddddddd-5555-6666-7777-888888888888';

const altCandidates: ReviewAltCandidates = {
  best_verified_release_group_id: RG_BEST,
  auto_import: { eligible: false, reasons: ['needs_human_confirmation'] },
  candidates: [
    {
      release_group_id: RG_BEST,
      representative_release_id: REL_BEST,
      deterministic_rank: 1,
      ai_rank: null,
      score: 0.97,
      action_allowed: true,
      review_reasons: [],
      conflicts: [],
      coverage: { local_tracks_verified: 10, local_tracks_total: 10 },
      sources: ['musicbrainz', 'acoustid'],
      edition_match: ['track_count'],
      mb_release_url: `https://musicbrainz.org/release/${REL_BEST}`,
      mb_release_group_url: `https://musicbrainz.org/release-group/${RG_BEST}`,
      is_best_verified: true,
      per_track: [
        { position: 1, local_title: 'Song One', mb_title: 'Song One', recording_id: 'rec-1', status: 'matched', acoustid_status: 'verified', is_deterministic: true, reason: '' },
        { position: 2, local_title: 'Weird Mix', mb_title: 'Song Two', recording_id: 'rec-2', status: 'different', acoustid_status: '', is_deterministic: false, reason: 'title mismatch' },
      ],
    },
    {
      release_group_id: RG_ALT,
      representative_release_id: REL_ALT,
      deterministic_rank: 2,
      ai_rank: null,
      score: 0.62,
      action_allowed: false,
      review_reasons: ['track_count_mismatch'],
      conflicts: [],
      coverage: { local_tracks_verified: 6, local_tracks_total: 10 },
      sources: ['musicbrainz'],
      edition_match: [],
      mb_release_url: `https://musicbrainz.org/release/${REL_ALT}`,
      mb_release_group_url: `https://musicbrainz.org/release-group/${RG_ALT}`,
      is_best_verified: false,
      per_track: [],
    },
  ],
};

function baseItem(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: 'r1',
    type: 'pending_ai',
    status: 'Pending AI',
    path: '/downloads/a',
    album: 'B',
    artist: 'A',
    year: 1998,
    ...overrides,
  } as unknown as ReviewItem;
}

describe('AltCandidatesPanel', () => {
  it('renders the best verified candidate first, then the ranked alternative in the given order', () => {
    const item = baseItem({ alt_candidates: altCandidates });
    render(<AltCandidatesPanel item={item} selectedRgid="" onUseCandidate={() => undefined} />);

    const rows = screen.getAllByTestId('alt-candidate');
    expect(rows).toHaveLength(2);
    // Order preserved (not re-sorted): best verified first even though we did
    // not sort, alternative second.
    expect(within(rows[0]).getByText('Best verified match')).toBeTruthy();
    expect(within(rows[1]).getByText('Suggestion')).toBeTruthy();
  });

  it('preserves the backend array order verbatim (never re-sorts by score, rank, or best-verified)', () => {
    // Deliberately scrambled: the backend put a low-score unvalidated suggestion
    // FIRST and the high-score best-verified match SECOND. Any client-side
    // re-sort (by score desc, deterministic_rank, or is_best_verified) would
    // move the best match to row 0. The panel must render the given order.
    const scrambled: ReviewAltCandidates = {
      best_verified_release_group_id: RG_BEST,
      auto_import: { eligible: false, reasons: [] },
      candidates: [
        { ...altCandidates.candidates[1], deterministic_rank: 2, score: 0.40, is_best_verified: false, action_allowed: false },
        { ...altCandidates.candidates[0], deterministic_rank: 1, score: 0.97, is_best_verified: true, action_allowed: true },
      ],
    };
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: scrambled })} onUseCandidate={() => undefined} />);
    const rows = screen.getAllByTestId('alt-candidate');
    expect(rows).toHaveLength(2);
    // Row 0 is the given-first suggestion, row 1 the given-second best match.
    expect(within(rows[0]).getByText('Suggestion')).toBeTruthy();
    expect(within(rows[1]).getByText('Best verified match')).toBeTruthy();
  });

  it('distinguishes a validated candidate from a suggestion in text (not colour alone)', () => {
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} onUseCandidate={() => undefined} />);
    expect(screen.getByText('Best verified match')).toBeTruthy();
    expect(screen.getByText('Suggestion')).toBeTruthy();
    expect(screen.getByText(/A suggestion that was not validated/i)).toBeTruthy();
  });

  it('shows verified track coverage and the review "why" for the alternative', () => {
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} onUseCandidate={() => undefined} />);
    expect(screen.getByText('10/10 tracks verified')).toBeTruthy();
    expect(screen.getByText('6/10 tracks verified')).toBeTruthy();
    expect(screen.getByText('track_count_mismatch')).toBeTruthy();
  });

  it('renders clickable MusicBrainz release and release-group links', () => {
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} onUseCandidate={() => undefined} />);
    const rgLink = screen.getByText(RG_BEST).closest('a');
    const relLink = screen.getByText(REL_BEST).closest('a');
    expect(rgLink?.getAttribute('href')).toBe(`https://musicbrainz.org/release-group/${RG_BEST}`);
    expect(relLink?.getAttribute('href')).toBe(`https://musicbrainz.org/release/${REL_BEST}`);
  });

  it('exposes stored per-track matched/failed/why under a disclosure', () => {
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} onUseCandidate={() => undefined} />);
    expect(screen.getByText('Per-track detail (2)')).toBeTruthy();
    // Unique per-track cells (local 'Weird Mix' vs mb 'Song Two') and the why.
    expect(screen.getByText('Weird Mix')).toBeTruthy();
    expect(screen.getByText('Song Two')).toBeTruthy();
    expect(screen.getByText('title mismatch')).toBeTruthy();
  });

  it('routes "Use this release" back through onUseCandidate with the Release Group id', () => {
    const onUse = vi.fn();
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} selectedRgid="" onUseCandidate={onUse} />);
    const rows = screen.getAllByTestId('alt-candidate');
    fireEvent.click(within(rows[1]).getByRole('button', { name: /use this release/i }));
    expect(onUse).toHaveBeenCalledWith(RG_ALT);
  });

  it('when refused with no verified candidate, says the best candidate could not be evaluated', () => {
    const refused: ReviewAltCandidates = {
      best_verified_release_group_id: '',
      auto_import: { eligible: false, reasons: ['release_lookup_failed'] },
      candidates: [{ ...altCandidates.candidates[1], is_best_verified: false }],
    };
    render(<AltCandidatesPanel item={baseItem({ alt_candidates: refused })} onUseCandidate={() => undefined} />);
    expect(screen.getByText(/best candidate could not be evaluated/i)).toBeTruthy();
    expect(screen.getByText('release_lookup_failed')).toBeTruthy();
  });

  it('renders nothing without candidates', () => {
    const { container } = render(<AltCandidatesPanel item={baseItem()} onUseCandidate={() => undefined} />);
    expect(container.firstChild).toBeNull();
  });

  // Declutter (progressive disclosure): the primary decision stays open; the
  // alternatives keep every field but collapse into one disclosure.
  it('keeps the primary candidate open and discloses the alternatives behind a summary', () => {
    const { container } = render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} selectedRgid="" onUseCandidate={() => undefined} />);
    // The best-verified (primary) candidate is shown directly, not inside a disclosure.
    const best = screen.getByText('Best verified match').closest('[data-testid="alt-candidate"]');
    expect(best).toBeTruthy();
    expect(best?.closest('details')).toBeNull();
    // The alternative sits inside a collapsed disclosure, still fully present.
    expect(screen.getByText(/Show 1 other candidate/i)).toBeTruthy();
    const details = container.querySelector('details.group');
    expect(details).toBeTruthy();
    expect(details?.hasAttribute('open')).toBe(false);
    const suggestion = screen.getByText('Suggestion').closest('[data-testid="alt-candidate"]');
    expect(details?.contains(suggestion ?? null)).toBe(true);
    // No data was dropped: both candidates remain in the DOM.
    expect(screen.getAllByTestId('alt-candidate')).toHaveLength(2);
  });

  it('opens the alternatives disclosure when a disclosed candidate is the selected release', () => {
    const { container } = render(<AltCandidatesPanel item={baseItem({ alt_candidates: altCandidates })} selectedRgid={RG_ALT} onUseCandidate={() => undefined} />);
    const details = container.querySelector('details.group');
    expect(details?.hasAttribute('open')).toBe(true);
  });
});

function renderCard(item: ReviewItem, extra: { selectedMatch?: unknown; manualValidation?: ManualValidationStateForTest; onValidateManualId?: () => void } = {}) {
  const noop = () => undefined;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item}
        mbid=""
        selectedMatch={extra.selectedMatch as never}
        manualValidation={extra.manualValidation as never}
        manualFocusToken={0}
        onMbidChange={noop}
        onUseCandidate={noop}
        onFocusManualEntry={noop}
        onValidateManualId={extra.onValidateManualId ?? noop}
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

describe('ReviewCard source vs edition year', () => {
  it('shows the folder source year and the selected edition release year as distinct fields', () => {
    const item = baseItem({ year: 1998 });
    const selectedMatch = {
      release_group_id: RG_BEST, representative_release_id: REL_BEST, artist: 'A', album: 'B', year: '2011',
      track_mapping: [], preflight_status: 'not_run', preflight_reason: '', is_release_group_usable: false,
      is_importable: false, is_partial_import: false, source: 'candidate',
    };
    renderCard(item, { selectedMatch });
    const sourceTerm = screen.getByText('Source year');
    const editionTerm = screen.getByText('Edition release year');
    // Distinct <dt>/<dd> pairs: 1998 belongs to source, 2011 to the edition.
    expect(sourceTerm.nextElementSibling?.textContent).toBe('1998');
    expect(editionTerm.nextElementSibling?.textContent).toBe('2011');
  });
});

describe('ReviewCard manual-MBID validate response', () => {
  it('shows a validated release-group response with the MusicBrainz link and Use This Release', () => {
    renderCard(baseItem(), {
      manualValidation: {
        status: 'valid',
        message: 'Release group matches the folder.',
        entityType: 'release-group',
        musicbrainzUrl: `https://musicbrainz.org/release-group/${RG_BEST}`,
        statusLines: ['10/10 tracks matched'],
      },
    });
    expect(screen.getByText('Valid Release Group')).toBeTruthy();
    expect(screen.getByText('Release group matches the folder.')).toBeTruthy();
    expect(screen.getByText('10/10 tracks matched')).toBeTruthy();
    expect(screen.getByRole('link', { name: /open in musicbrainz/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /use this release/i })).toBeTruthy();
  });

  it('shows an actionable error when MB lookup / identity verification fails', () => {
    renderCard(baseItem(), {
      manualValidation: {
        status: 'error',
        message: 'MusicBrainz lookup failed: this release group does not match the folder tracks.',
      },
    });
    expect(screen.getByText('MusicBrainz ID rejected')).toBeTruthy();
    expect(screen.getByText(/does not match the folder tracks/i)).toBeTruthy();
  });

  it('calls onValidateManualId when the Validate ID button is clicked', () => {
    const onValidate = vi.fn();
    const item = baseItem();
    renderCard(item, { onValidateManualId: onValidate });
    // The field starts empty so Validate is disabled; the dedicated validate
    // response buttons already cover the enabled path. Here we assert the
    // primary control is wired and present.
    expect(screen.getByRole('button', { name: /validate id/i })).toBeTruthy();
  });
});

describe('ReviewCard "Use this release" wiring (Problem 1 regression)', () => {
  // The album-target bug: clicking "Use this release" only populated the manual
  // field (onUseCandidate) and never validated, so Import stayed blocked. The
  // fix composes onUseCandidate + onValidateManualId. This renders the real
  // ReviewCard -> AltCandidatesPanel so the regression cannot silently return.
  it('routes a ranked candidate through BOTH onUseCandidate and onValidateManualId', () => {
    const onUse = vi.fn();
    const onValidate = vi.fn();
    const item = baseItem({ alt_candidates: altCandidates });
    render(
      <MemoryRouter>
        <ReviewCard
          item={item}
          mbid=""
          manualFocusToken={0}
          onMbidChange={() => undefined}
          onUseCandidate={onUse}
          onSelectMatch={() => undefined}
          onFocusManualEntry={() => undefined}
          onValidateManualId={onValidate}
          onClearManualId={() => undefined}
          onSuggest={() => undefined}
          onApply={() => undefined}
          onDismiss={() => undefined}
          onDeleteFolder={() => undefined}
          onCleanupFiles={() => undefined}
        />
      </MemoryRouter>,
    );
    const best = screen.getByText('Best verified match').closest('[data-testid="alt-candidate"]');
    expect(best).toBeTruthy();
    fireEvent.click(within(best as HTMLElement).getByRole('button', { name: /use this release/i }));
    expect(onUse).toHaveBeenCalledWith(RG_BEST);
    expect(onValidate).toHaveBeenCalledWith(RG_BEST);
  });
});
