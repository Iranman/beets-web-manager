// #813 mutation-safety regression: the "Confirm match & import" control used
// to POST a BARE body ({path, mb_albumid, mb_releasegroupid, queue_review})
// omitting selected_source_files + track_mapping. The backend treats an empty
// selected_source_files as a WHOLE-folder `beet import --search-id`, so any
// unmatched local extras the preview promised would "stay in review" were
// swept into the library instead, and the review item was removed outright.
//
// This suite FAILS on the pre-fix code (c8846eab):
//   (a) the POST omitted selected_source_files / track_mapping;
//   (b) the review item was deleted on success (no keepReviewAfterSuccess);
//   (c) the confirm control showed even when unmatched extras existed.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';
import { ReviewCard } from '../src/features/importReview/ImportReviewPage';
import type { SelectedMatch, TargetPreviewState } from '../src/features/importReview/importReviewDecision';

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';
const MATCHED = '/downloads/a/01.flac';
const EXTRA = '/downloads/a/bonus.flac';

// A manual-confirmable PARTIAL match: one matched track plus one local "extra"
// that is NOT part of the release. The matched subset (what must be imported)
// is a strict subset of the folder's audio.
function partialConfirmMatch(overrides: Partial<SelectedMatch> = {}): SelectedMatch {
  return {
    release_group_id: RG, representative_release_id: REL, artist: 'A', album: 'B', year: '2000',
    track_mapping: [
      { num: 1, status: 'matched', source_path: MATCHED, local_title: 't1', mb_title: 'T1', mb_trackid: '' },
      { num: 2, status: 'extra', source_path: EXTRA, local_title: 'bonus', mb_title: '', mb_trackid: '' },
    ],
    preflight_status: 'passed', preflight_reason: 'Partial: one matched track, one local extra.',
    is_release_group_usable: true, is_importable: false, manual_import_allowed: true,
    is_partial_import: true, identity_validated: true, source: 'manual',
    ...overrides,
  } as unknown as SelectedMatch;
}

afterEach(cleanup);

// ---- (c) card-level: unmatched extras in the preview HIDE the control ----

function renderCard(targetPreviewState: TargetPreviewState) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', status: 'Pending AI', path: '/downloads/a', album: 'B', artist: 'A',
    mb_valid: true, mb_albumid: REL,
  } as unknown as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item} mbid={RG} selectedMatch={partialConfirmMatch()} targetPreviewState={targetPreviewState} manualFocusToken={0}
        onMbidChange={noop} onUseCandidate={noop} onFocusManualEntry={noop} onValidateManualId={noop}
        onClearManualId={noop} onSuggest={noop} onApply={noop} onConfirmImport={noop}
        onDismiss={noop} onDeleteFolder={noop} onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

function preview(extra: number): TargetPreviewState {
  return {
    status: 'ready', key: 'k', preview: {
      ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: [],
      tracks_to_import_count: 1, real_conflict_count: 0, unmatched_extra_count: extra,
      tracks: [{ source_path: MATCHED }],
    },
  } as unknown as TargetPreviewState;
}

describe('#813 confirm control hides when unmatched extras would stay in review', () => {
  it('(c) HIDES the confirm control when unmatched_extra_count > 0', () => {
    renderCard(preview(2));
    expect(screen.queryByTestId('review-confirm-import-button')).toBeNull();
    expect(screen.queryByTestId('review-manual-confirm-panel')).toBeNull();
  });

  it('shows the confirm control when there are no unmatched extras', () => {
    renderCard(preview(0));
    expect(screen.getByTestId('review-confirm-import-button')).toBeTruthy();
  });
});

// ---- (a)+(b) page-level: POST carries the matched subset; item retained ----

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    previewImportTarget: vi.fn(),
    validateManualMusicBrainzId: vi.fn(),
    importWithId: vi.fn(),
    deletePendingReview: vi.fn(),
    getJob: vi.fn(),
    revalidateImportReview: vi.fn(),
    cleanupStaleReview: vi.fn(),
    getAiBatchStatus: vi.fn(),
    getSetupStatus: vi.fn(),
  };
});
vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

const mocked = vi.mocked(client);

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}
async function settle() { await act(async () => { await new Promise((r) => setTimeout(r, 50)); }); }

describe('#813 confirmed import POSTs the matched subset and retains a partial review', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
    mocked.getReviewQueue.mockResolvedValue({
      ok: true,
      items: [{ id: 'a', type: 'pending_ai', path: '/downloads/a', album: 'The Slaughter Tape', artist: '21 Savage' } as unknown as ReviewItem],
      counts: { all: 1, pending_ai: 1 },
    } as never);
    mocked.validateManualMusicBrainzId.mockResolvedValue({
      ok: true, message: 'Release group validated.', release_group_id: RG, representative_release_id: REL,
      selected_match: partialConfirmMatch(),
    } as never);
    // Preview is "safe" with 0 real conflicts and 0 unmatched extras (the
    // backend reports the extra as a warning, not a block) — this is exactly
    // the state in which the old whole-folder import swept the extra in.
    mocked.previewImportTarget.mockResolvedValue({
      ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: ['1 unmatched local file(s) will stay in review'],
      tracks_to_import_count: 1, real_conflict_count: 0, unmatched_extra_count: 0, tracks: [{ source_path: MATCHED }],
    } as never);
    mocked.importWithId.mockResolvedValue({ ok: true, job_id: 'job9' } as never);
    mocked.getJob.mockResolvedValue({ ok: true, status: 'success', job_id: 'job9' } as never);
    mocked.deletePendingReview.mockResolvedValue({ ok: true } as never);
    mocked.revalidateImportReview.mockResolvedValue({ ok: true, items: [], reviewed_count: 1, queued_count: 0 } as never);
    mocked.cleanupStaleReview.mockResolvedValue({ ok: true, removed_total: 0, removed_folder_gone: 0, removed_mb_in_library: 0, remaining: 1 } as never);
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
  });
  afterEach(() => { vi.useRealTimers(); cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

  it('(a) POSTs selected_source_files (matched subset, not the whole folder) + track_mapping, and (b) does NOT delete the review on success', async () => {
    wrap(<ImportReviewPage />);
    await screen.findAllByText('The Slaughter Tape');
    await settle();

    fireEvent.change(screen.getByLabelText(/MusicBrainz Release or Release Group ID\/URL/), { target: { value: RG } });
    fireEvent.click(screen.getByRole('button', { name: 'Validate ID' }));
    await settle();

    const confirmButton = await screen.findByTestId('review-confirm-import-button');
    fireEvent.click(confirmButton);
    const dialog = await screen.findByRole('dialog');

    // Drive the confirm + background poll to success under fake timers so the
    // 2.5s poll interval is jumped deterministically.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm match & import' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });

    expect(mocked.importWithId).toHaveBeenCalledTimes(1);
    const body = mocked.importWithId.mock.calls[0][0] as Record<string, unknown>;
    // (a) The matched subset only — the local extra is NOT imported.
    expect(body.selected_source_files).toEqual([MATCHED]);
    expect((body.selected_source_files as string[])).not.toContain(EXTRA);
    expect(body.track_mapping).toBeTruthy();
    expect(body.mb_albumid).toBe(REL);
    expect(body.mb_releasegroupid).toBe(RG);
    expect(body.queue_review).toBe(true);

    // (b) The partial review item is RETAINED: deletePendingReview is skipped
    // on success so the unmatched extra is not orphaned out of the queue.
    expect(mocked.deletePendingReview).not.toHaveBeenCalled();
  });
});
