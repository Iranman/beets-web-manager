// #813: a genuinely-correct album (every track matched, 0 conflicts, Safe
// target preview) that the matcher could NOT automatically verify
// (is_importable=false) but the backend marked manual_import_allowed=true must
// NOT be a dead end. The old UI showed only a disabled "Import with ID" button
// + an "Import blocked / Not importable" banner with no way forward. The fix
// surfaces an explicit, enabled "Confirm match & import" control (gated behind
// a confirm dialog) that runs the controlled import-with-id path.
//
// This suite FAILS on the old behavior: the confirm control did not exist, and
// the only import button in this state was disabled.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import { ReviewCard, ImportReviewPage } from '../src/features/importReview/ImportReviewPage';
import type { SelectedMatch, TargetPreviewState } from '../src/features/importReview/importReviewDecision';

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';

// A match the human MAY confirm (manual_import_allowed) but which is NOT
// automatically importable (is_importable=false) — the #813 dead-end case.
function manualConfirmMatch(overrides: Partial<SelectedMatch> = {}): SelectedMatch {
  return {
    release_group_id: RG, representative_release_id: REL, artist: 'A', album: 'B', year: '2000',
    track_mapping: [{ num: 1, status: 'matched', source_path: '/downloads/a/01.flac', local_title: 't1', mb_title: 'T1', mb_trackid: '' }],
    preflight_status: 'passed', preflight_reason: 'Every track matched; recordings are not in AcoustID.',
    is_release_group_usable: true, is_importable: false, manual_import_allowed: true,
    is_partial_import: false, identity_validated: true, source: 'manual',
    ...overrides,
  } as unknown as SelectedMatch;
}

// A safe target preview matching the one selected track (0 conflicts).
const safePreview = {
  status: 'ready', key: 'k', preview: {
    ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: [],
    tracks_to_import_count: 1, real_conflict_count: 0,
    tracks: [{ source_path: '/downloads/a/01.flac' }],
  },
} as unknown as TargetPreviewState;

afterEach(cleanup);

function renderCard(selectedMatch: SelectedMatch, targetPreviewState: TargetPreviewState, onConfirmImport = () => undefined) {
  const noop = () => undefined;
  const item = {
    id: 'r1', type: 'pending_ai', status: 'Pending AI', path: '/downloads/a', album: 'B', artist: 'A',
    mb_valid: true, mb_albumid: REL,
  } as unknown as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item} mbid={RG} selectedMatch={selectedMatch} targetPreviewState={targetPreviewState} manualFocusToken={0}
        onMbidChange={noop} onUseCandidate={noop} onFocusManualEntry={noop} onValidateManualId={noop}
        onClearManualId={noop} onSuggest={noop} onApply={noop} onConfirmImport={onConfirmImport}
        onDismiss={noop} onDeleteFolder={noop} onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

describe('#813 ReviewCard: manual_import_allowed turns the dead end into a confirmable import', () => {
  it('shows an ENABLED "Confirm match & import" control (not the old disabled dead end)', () => {
    const onConfirmImport = vi.fn();
    renderCard(manualConfirmMatch(), safePreview, onConfirmImport);

    const confirmButton = screen.getByTestId('review-confirm-import-button') as HTMLButtonElement;
    expect(confirmButton).toBeTruthy();
    expect(confirmButton.disabled).toBe(false);
    expect(screen.getByTestId('review-manual-confirm-panel')).toBeTruthy();

    // The old dead end is gone: no disabled "Import with ID" primary button and
    // no "Import blocked" banner competing with the confirm control.
    expect(screen.queryByRole('button', { name: 'Import with ID' })).toBeNull();
    expect(screen.queryByText('Import blocked')).toBeNull();

    fireEvent.click(confirmButton);
    expect(onConfirmImport).toHaveBeenCalledTimes(1);
  });

  it('stays BLOCKED when manual_import_allowed is false (no confirm control)', () => {
    renderCard(manualConfirmMatch({ manual_import_allowed: false }), safePreview);
    expect(screen.queryByTestId('review-confirm-import-button')).toBeNull();
    expect(screen.queryByTestId('review-manual-confirm-panel')).toBeNull();
  });

  it('does not show the confirm control when the target preview is not safe', () => {
    const unsafe = { status: 'ready', key: 'k', preview: { ok: true, safe: false, status: 'blocked', blocked_reasons: ['target_conflict'], warnings: [], tracks_to_import_count: 1, real_conflict_count: 1, tracks: [] } } as unknown as TargetPreviewState;
    renderCard(manualConfirmMatch(), unsafe);
    expect(screen.queryByTestId('review-confirm-import-button')).toBeNull();
  });
});

// ---- Page-level: the click runs the controlled import-with-id POST ----

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    previewImportTarget: vi.fn(),
    validateManualMusicBrainzId: vi.fn(),
    importWithId: vi.fn(),
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

const mocked = vi.mocked(client);

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}
async function settle() { await act(async () => { await new Promise((r) => setTimeout(r, 50)); }); }

describe('#813 Import Review page: confirm dialog then controlled import-with-id POST', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
    mocked.getReviewQueue.mockResolvedValue({
      ok: true,
      items: [{ id: 'a', type: 'pending_ai', path: '/downloads/a', album: 'The Slaughter Tape', artist: '21 Savage' } as unknown as ReviewItem],
      counts: { all: 1, pending_ai: 1 },
    } as never);
    mocked.validateManualMusicBrainzId.mockResolvedValue({
      ok: true, message: 'Release group validated.', release_group_id: RG, representative_release_id: REL,
      selected_match: manualConfirmMatch(),
    } as never);
    mocked.previewImportTarget.mockResolvedValue({
      ok: true, safe: true, status: 'safe', blocked_reasons: [], warnings: [],
      tracks_to_import_count: 1, real_conflict_count: 0, tracks: [{ source_path: '/downloads/a/01.flac' }],
    } as never);
    mocked.importWithId.mockResolvedValue({ ok: true, job_id: 'job9' } as never);
    mocked.getJob.mockResolvedValue({ state: { status: 'success', job_id: 'job9' } } as never);
    mocked.revalidateImportReview.mockResolvedValue({ ok: true, items: [], reviewed_count: 1, queued_count: 0 } as never);
    mocked.cleanupStaleReview.mockResolvedValue({ ok: true, removed_total: 0, removed_folder_gone: 0, removed_mb_in_library: 0, remaining: 1 } as never);
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

  it('validate -> Confirm match & import issues POST import-with-id with the exact body; nothing before confirm', async () => {
    wrap(<ImportReviewPage />);
    await screen.findAllByText('The Slaughter Tape');
    await settle();

    // Paste the MusicBrainz id and validate it (seeds the manual_import_allowed match).
    fireEvent.change(screen.getByLabelText(/MusicBrainz Release or Release Group ID\/URL/), { target: { value: RG } });
    fireEvent.click(screen.getByRole('button', { name: 'Validate ID' }));
    await settle();

    // The confirmable control appears and nothing has been imported yet.
    const confirmButton = await screen.findByTestId('review-confirm-import-button');
    expect((confirmButton as HTMLButtonElement).disabled).toBe(false);
    expect(mocked.importWithId).not.toHaveBeenCalled();

    // Opens a confirm dialog; still no import until the dialog is confirmed.
    fireEvent.click(confirmButton);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/writes tags and moves files into the library/)).toBeTruthy();
    expect(mocked.importWithId).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm match & import' }));
    await waitFor(() => expect(mocked.importWithId).toHaveBeenCalledTimes(1));
    expect(mocked.importWithId).toHaveBeenCalledWith({
      path: '/downloads/a',
      mb_albumid: REL,
      mb_releasegroupid: RG,
      queue_review: true,
    });
  });
});
