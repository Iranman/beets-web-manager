// 0.7.0 FE round 4: Find Music refresh after downloads (#398), the Missing IDs
// batch fix (#399 contract) and the deterministic import's new folder outcomes (#394).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiBatchFolderState, ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getAcquisitionQueue: vi.fn(),
    refreshAcquisitionQueue: vi.fn(),
    getAcquisitionDownloadAllActive: vi.fn(),
    getYtdlpStatus: vi.fn(),
    previewMissingMbBatch: vi.fn(),
    applyMissingMbBatch: vi.fn(),
    getJob: vi.fn(),
    killJob: vi.fn(),
    getLibraryMissingMbIds: vi.fn(),
    previewImportTarget: vi.fn(() => new Promise(() => undefined)),
    getAiBatchStatus: vi.fn(),
    getSetupStatus: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import * as libApi from '../src/lib/api';
import { AcquisitionPanel } from '../src/features/acquisition/AcquisitionPanel';
import { BatchStopEvidence, ImportReviewPage } from '../src/features/importReview/ImportReviewPage';
import {
  MISSING_IDS_BATCH_STORAGE_KEY,
  MissingIdsBatchPanel,
  applyErrorMessage,
  previewErrorMessage,
  reasonLabel,
} from '../src/features/importReview/MissingIdsBatchPanel';
import { IntakePanel, codeLabel, folderAttentionLabel, folderFailureText } from '../src/features/intake/IntakePanel';

const mocked = vi.mocked(client);
const TOKEN = 'secret-confirm-token-value';
const MBID = '11111111-2222-3333-4444-555555555555';

function apiError(status: number, body: Record<string, unknown>) {
  return Object.assign(new Error(String(body.error || `HTTP ${status}`)), { httpStatus: status, body });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

// ── Find Music refresh (#398) ────────────────────────────────────────────────

describe('Find Music refresh contract (#398)', () => {
  const queue = {
    ok: true, items: [{ key: 'k1', artist: 'Artist 1', album: 'Album 1', year: '2001', mbid: MBID, sources: ['lidarr'], sort_key: 'a', actions: { can_download: true }, wanted: { monitored: true, mb_albumid: MBID }, local: null }],
    total: 1, offset: 0, limit: 100, counts: { all: 1, beets: 0, lidarr: 1, merged: 0 }, album_types: {},
    cache: { state: 'ready', built_at: 1_700_000_000, age_seconds: 60, refreshing: false, job_id: null },
  };
  const renderPanel = () => render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><AcquisitionPanel /></MemoryRouter>
    </QueryClientProvider>,
  );

  beforeEach(() => {
    mocked.getAcquisitionQueue.mockResolvedValue(queue as never);
    mocked.refreshAcquisitionQueue.mockResolvedValue({ ok: true, job_id: 'q1', already_running: false, reused: false } as never);
    mocked.getAcquisitionDownloadAllActive.mockResolvedValue({ active: false } as never);
    mocked.getYtdlpStatus.mockResolvedValue({ ok: true, ready: true, enabled: true, cookie_file: '', cookie_candidates: [], message: '' } as never);
  });

  it('the client sends after_download only when asked', async () => {
    const actual = await vi.importActual<typeof import('../src/api/client')>('../src/api/client');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true, job_id: null, already_running: false, reused: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await actual.refreshAcquisitionQueue(false);
    await actual.refreshAcquisitionQueue(true, true);
    const bodies = fetchMock.mock.calls.map((call) => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)));
    expect(bodies).toEqual([{}, { include_unmonitored: true, after_download: true }]);
  });

  it('the Refresh button never sends after_download', async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh list' }));
    await waitFor(() => expect(mocked.refreshAcquisitionQueue).toHaveBeenCalledWith(false, false));
    expect(mocked.refreshAcquisitionQueue).not.toHaveBeenCalledWith(false, true);
  });

  it('a finished batch download refreshes with after_download', async () => {
    mocked.getAcquisitionDownloadAllActive.mockResolvedValue({ active: true, job: { job_id: 'b9' } } as never);
    vi.mocked(libApi.apiGet).mockImplementation(((path: string) => (
      path === '/api/jobs/b9' ? Promise.resolve({ ok: true, job_id: 'b9', status: 'success', result: {} }) : new Promise(() => undefined)
    )) as never);
    renderPanel();
    await waitFor(() => expect(mocked.refreshAcquisitionQueue).toHaveBeenCalledWith(false, true));
    expect(mocked.refreshAcquisitionQueue).not.toHaveBeenCalledWith(false, false);
  });

  it('reused: true reloads the page and follows no job', async () => {
    mocked.refreshAcquisitionQueue.mockResolvedValue({ ok: true, job_id: null, already_running: false, reused: true } as never);
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh list' }));
    await waitFor(() => expect(mocked.getAcquisitionQueue.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(vi.mocked(libApi.apiGet).mock.calls.some(([path]) => String(path).includes('/api/jobs/'))).toBe(false);
    expect(screen.queryByText(/could not be started/)).toBeNull();
  });
});

// ── Missing IDs batch (#399) ─────────────────────────────────────────────────

function missing(id: string, album: string, kind: 'album' | 'item' = 'album'): ReviewItem {
  return { id, type: 'library_no_mb', target_kind: kind, artist: 'Artist', album, title: album, path: `/music/Artist/${album}` } as unknown as ReviewItem;
}

const ROWS = [missing('album:12', 'Proven Album'), missing('item:34', 'Proven Track', 'item'), missing('album:56', 'Unsure Album')];

const PREVIEW = {
  ok: true, transaction_id: 'tx-1', confirm_token: TOKEN,
  counts: { rows: 3, proven: 2, needs_review: 1 },
  rows: [
    { row_id: 'album:12', kind: 'album', target_id: 12, proven: true, status: 'proven', reasons: [], write: { mb_releasegroupid: 'rg-1', mb_albumid: 'rel-1' }, message: 'Proven match; will be written on apply.' },
    { row_id: 'item:34', kind: 'item', target_id: 34, proven: true, status: 'proven', reasons: [], write: { mb_trackid: 'rec-1' }, message: 'Proven match; will be written on apply.' },
    { row_id: 'album:56', kind: 'album', target_id: 56, proven: false, status: 'needs_review', reasons: ['in_import_review', 'track_count_mismatch:10/12'], write: {}, message: 'Not proven; nothing will be written. Review this row by hand.' },
  ],
};

function renderBatch(onWritten = vi.fn()) {
  render(<MemoryRouter><MissingIdsBatchPanel rows={ROWS} onWritten={onWritten} /></MemoryRouter>);
  return onWritten;
}

async function previewAll() {
  mocked.previewMissingMbBatch.mockResolvedValue({ ok: true, job_id: 'pj1', rows: 3 } as never);
  fireEvent.click(screen.getByRole('button', { name: /Select this page/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Preview fix (3)' }));
  await waitFor(() => expect(mocked.previewMissingMbBatch).toHaveBeenCalledWith({ ids: ['album:12', 'item:34', 'album:56'] }));
}

describe('Missing IDs batch fix (#399)', () => {
  it('preview shows proven and needs-review rows; apply needs a click and a confirmation', async () => {
    mocked.getJob.mockImplementation(async (id: string) => (id === 'pj1'
      ? { ok: true, job_id: 'pj1', status: 'success', result: PREVIEW }
      : { ok: true, job_id: 'aj1', status: 'success', result: {
        ok: true, transaction_id: 'tx-1', status: 'Completed', counts: { written: 1, evidence_changed: 1 },
        rows: [
          { row_id: 'album:12', kind: 'album', target_id: 12, status: 'written', message: 'Written and verified.', write: {}, transaction_ids: ['op-9'] },
          { row_id: 'item:34', kind: 'item', target_id: 34, status: 'evidence_changed', message: 'The evidence changed since the preview; nothing was written. Preview again.', write: {} },
        ] } }) as never);
    mocked.applyMissingMbBatch.mockResolvedValue({ ok: true, job_id: 'aj1', transaction_id: 'tx-1' } as never);
    const onWritten = renderBatch();
    await previewAll();

    const preview = await screen.findByRole('list', { name: 'Preview results' });
    expect(within(preview).getAllByText('Proven')).toHaveLength(2);
    expect(within(preview).getByText('Needs review')).toBeTruthy();
    expect(within(preview).getByText('Will write: mb_releasegroupid rg-1, mb_albumid rel-1')).toBeTruthy();
    expect(within(preview).getByText('Will write: mb_trackid rec-1')).toBeTruthy();
    expect(within(preview).getByText(/its folder is in the Import Review queue; track count mismatch \(10\/12\)/)).toBeTruthy();
    expect(screen.getAllByRole('status').some((node) => /2 of 3 rows proven/.test(node.textContent ?? ''))).toBe(true);
    // The token is never shown or stored.
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(JSON.stringify({ ...localStorage })).not.toContain(TOKEN);
    expect(mocked.applyMissingMbBatch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Apply 2 proven rows…' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Write MusicBrainz IDs to 1 album and 1 track?')).toBeTruthy();
    expect(mocked.applyMissingMbBatch).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Write 2 rows' }));

    await waitFor(() => expect(mocked.applyMissingMbBatch).toHaveBeenCalledWith('tx-1', TOKEN));
    const results = await screen.findByRole('list', { name: 'Apply results' });
    expect(within(results).getByText('Written')).toBeTruthy();
    expect(within(results).getByText('Written and verified.')).toBeTruthy();
    expect(within(results).getByText('Evidence changed')).toBeTruthy();
    expect(within(results).getByRole('link', { name: 'Transaction op-9' }).getAttribute('href')).toBe('/changes?tx=op-9');
    expect(onWritten).toHaveBeenCalled();
    expect(localStorage.getItem(MISSING_IDS_BATCH_STORAGE_KEY)).toBeNull();
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it('cancelling the confirmation writes nothing', async () => {
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj1', status: 'success', result: PREVIEW } as never);
    renderBatch();
    await previewAll();
    fireEvent.click(await screen.findByRole('button', { name: 'Apply 2 proven rows…' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mocked.applyMissingMbBatch).not.toHaveBeenCalled();
  });

  it('hides Apply when the preview has no transaction', async () => {
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj1', status: 'success', result: {
      ...PREVIEW, transaction_id: null, confirm_token: null, counts: { rows: 1, proven: 0, needs_review: 1 }, rows: [PREVIEW.rows[2]],
    } } as never);
    renderBatch();
    await previewAll();
    expect(await screen.findByText(/nothing to apply/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Apply/ })).toBeNull();
  });

  it('apply refusals say nothing was written', async () => {
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj1', status: 'success', result: PREVIEW } as never);
    mocked.applyMissingMbBatch.mockRejectedValue(apiError(409, { ok: false, code: 'not_preview', error: 'This preview was already used.' }));
    renderBatch();
    await previewAll();
    fireEvent.click(await screen.findByRole('button', { name: 'Apply 2 proven rows…' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Write 2 rows' }));
    expect(await screen.findByText(/Nothing was written: this preview was already applied or used/)).toBeTruthy();

    expect(applyErrorMessage('confirm_token_mismatch', '')).toMatch(/confirmation does not match/);
    for (const code of ['confirm_token_required', 'wrong_transaction', 'not_found']) {
      expect(applyErrorMessage(code, '')).toMatch(/not valid any more\. Preview again/);
    }
    expect(applyErrorMessage('', 'boom')).toMatch(/^Nothing was written: boom/);
  });

  it('preview refusals name the cause', () => {
    expect(previewErrorMessage(apiError(400, { error_code: 'NO_ROWS' }))).toMatch(/none of the selected rows/);
    expect(previewErrorMessage(apiError(400, { error_code: 'INVALID_PARAM', error: 'ids must be a list' }))).toMatch(/refused \(ids must be a list\)/);
    expect(previewErrorMessage(apiError(503, { error_code: 'ENGINE_UNAVAILABLE' }))).toMatch(/Beets is unavailable/);
    expect(previewErrorMessage(apiError(502, { error_code: 'ENGINE_AUTH_ERROR' }))).toMatch(/could not sign in to Beets/);
    expect(previewErrorMessage(apiError(502, { error_code: 'ENGINE_ERROR' }))).toMatch(/Beets returned an error/);
    expect(reasonLabel('folder_audio_count_mismatch:9/10')).toBe('folder audio count mismatch (9/10)');
  });

  it('a Recovery Required apply links to the transaction; a stored job is resumed and can be cancelled', async () => {
    localStorage.setItem(MISSING_IDS_BATCH_STORAGE_KEY, JSON.stringify({ kind: 'apply', job_id: 'aj2' }));
    let done = false;
    mocked.getJob.mockImplementation(async () => (done
      ? { ok: true, job_id: 'aj2', status: 'success', result: {
        ok: false, transaction_id: 'tx-2', status: 'Recovery Required', counts: { recovery_required: 1, not_attempted: 1 },
        rows: [
          { row_id: 'album:12', kind: 'album', target_id: 12, status: 'recovery_required', message: 'The write could not be verified or rolled back and the batch stopped. Check this album or track before retrying.', write: {} },
          { row_id: 'item:34', kind: 'item', target_id: 34, status: 'not_attempted', message: 'Not attempted because an earlier row failed.', write: {} },
        ] } }
      : { ok: true, job_id: 'aj2', status: 'running', state: { progress: { done: 1, total: 2 } } }) as never);
    mocked.killJob.mockResolvedValue({ ok: true } as never);
    renderBatch();
    const bar = await screen.findByRole('progressbar', { name: 'Missing IDs apply progress' });
    await waitFor(() => expect(bar.getAttribute('aria-valuenow')).toBe('50'));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(mocked.killJob).toHaveBeenCalledWith('aj2'));
    expect(await screen.findByText(/stops after the row it is writing now/)).toBeTruthy();

    done = true;
    const link = await screen.findByRole('link', { name: 'Open the batch transaction' }, { timeout: 5000 });
    expect(link.getAttribute('href')).toBe('/changes?tx=tx-2');
    expect(screen.getByText('Recovery required')).toBeTruthy();
    expect(screen.getByText('Not attempted because an earlier row failed.')).toBeTruthy();
    expect(localStorage.getItem(MISSING_IDS_BATCH_STORAGE_KEY)).toBeNull();
  });

  it('caps the selection at 50 rows', () => {
    const many = Array.from({ length: 60 }, (_, i) => missing(`album:${i}`, `Album ${i}`));
    render(<MemoryRouter><MissingIdsBatchPanel rows={many} onWritten={() => undefined} /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: /Select this page/ }));
    expect(screen.getByRole('button', { name: 'Preview fix (50)' })).toBeTruthy();
    expect((screen.getByRole('checkbox', { name: /Album 55/ }) as HTMLInputElement).disabled).toBe(true);
  });

  it('is shown on the Missing IDs page, not on the Review Queue', async () => {
    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: ROWS, total: 3, offset: 0, limit: 100, counts: { all: 3, album: 2, item: 1 } } as never);
    render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
    expect(await screen.findByRole('button', { name: 'Preview fix (0)' })).toBeTruthy();
  });
});

describe('Missing IDs batch: #399 faab2cf reasons, duplicate evidence, restart', () => {
  it('names the new reasons and keeps unknown codes readable', () => {
    expect(reasonLabel('existing_release_group_differs')).toBe('already has a different release group; not changed');
    expect(reasonLabel('duplicate_release_group')).toBe('another album already has this release group; use dedup review');
    expect(reasonLabel('symlinked_item_path')).toBe("a track's file is a symlink");
    expect(reasonLabel('symlinked_audio_in_folder')).toBe('the album folder holds symlinked audio');
    expect(reasonLabel('folder_files_mismatch:11/12')).toBe("the folder's audio files do not match the album's tracks (11/12)");
    expect(reasonLabel('brand_new_reason')).toBe('brand new reason');
  });

  it('shows who holds the release group and duplicates within the batch', async () => {
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj1', status: 'success', result: {
      ok: true, transaction_id: null, confirm_token: null, counts: { rows: 2, proven: 0, needs_review: 2 },
      rows: [
        { ...PREVIEW.rows[2], row_id: 'album:12', reasons: ['duplicate_release_group'], evidence: { release_group_held_by: [7, 9] } },
        { ...PREVIEW.rows[2], row_id: 'album:56', reasons: ['duplicate_release_group'], evidence: { duplicate_in_batch: true } },
      ],
    } } as never);
    renderBatch();
    await previewAll();
    const preview = await screen.findByRole('list', { name: 'Preview results' });
    expect(within(preview).getByText(/already on album 7, 9/)).toBeTruthy();
    expect(within(preview).getByRole('link', { name: /Review duplicates/ }).getAttribute('href')).toBe('/jobs');
    expect(within(preview).getByText(/Two selected albums share this release group/)).toBeTruthy();
    expect(within(preview).getAllByText(/use dedup review/)).toHaveLength(2);
  });

  it.each([['[REDACTED]'], [null], ['']])('a resumed preview with token %j hides Apply and asks to preview again', async (token) => {
    localStorage.setItem(MISSING_IDS_BATCH_STORAGE_KEY, JSON.stringify({ kind: 'preview', job_id: 'pj9' }));
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj9', status: 'success', result: { ...PREVIEW, confirm_token: token } } as never);
    renderBatch();
    expect(await screen.findByText('Preview again: the preview was interrupted by a restart.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Apply/ })).toBeNull();
    expect(document.body.textContent).not.toContain('[REDACTED]');
    expect(mocked.applyMissingMbBatch).not.toHaveBeenCalled();
  });

  it('confirm_token_mismatch tells the user to preview again', async () => {
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj1', status: 'success', result: PREVIEW } as never);
    mocked.applyMissingMbBatch.mockRejectedValue(apiError(409, { ok: false, code: 'confirm_token_mismatch', error: 'mismatch' }));
    renderBatch();
    await previewAll();
    fireEvent.click(await screen.findByRole('button', { name: 'Apply 2 proven rows…' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Write 2 rows' }));
    expect(await screen.findByText(/Nothing was written: the confirmation does not match this preview.*Preview again, then apply the new preview\./)).toBeTruthy();
  });
});

describe('Missing IDs: Release Group fill (#401)', () => {
  const RELEASE = '99999999-8888-7777-6666-555555555555';

  it('names the Release Group fill refusals', () => {
    expect(reasonLabel('release_group_not_resolved')).toBe("MusicBrainz did not return a release group for this album's release");
    expect(reasonLabel('embedded_release_group_conflict')).toBe("A track's tags name a different release group");
    expect(reasonLabel('item_release_id_conflict')).toBe("A track's tags name a different release");
  });

  it('labels a proven fill row as adding the release group, not a full match', async () => {
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'pj1', status: 'success', result: {
      ...PREVIEW,
      rows: [
        { ...PREVIEW.rows[0], write: { mb_releasegroupid: 'rg-1' }, evidence: { fill: 'release_group', release_id: RELEASE, release_group_from_release: 'rg-1' } },
        PREVIEW.rows[1],
        PREVIEW.rows[2],
      ],
    } } as never);
    renderBatch();
    await previewAll();
    const preview = await screen.findByRole('list', { name: 'Preview results' });
    expect(within(preview).getByText('Add release group (keeps release)')).toBeTruthy();
    expect(within(preview).getAllByText('Proven')).toHaveLength(1);
  });

  it('shows a missing_release_group row as "Needs release group" with its Release ID; rows without the fields are unchanged', async () => {
    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [
      { ...missing('album:12', 'Has Release'), status: 'Needs MB ID', status_key: 'needs_mb_id', missing_id_type: 'Release Group ID', missing_reason: 'missing_release_group', current_mb_albumid: RELEASE },
      { ...missing('album:56', 'Old Row'), status: 'Needs MB ID', status_key: 'needs_mb_id' },
    ], total: 2, offset: 0, limit: 100, counts: { all: 2, album: 2, item: 0 } } as never);
    render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
    expect(await screen.findByText('Needs release group')).toBeTruthy();
    expect(screen.getByText(`Release ID: ${RELEASE}`)).toBeTruthy();
    expect(screen.getByText('Albums (no Release or Release Group ID) (2)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Next item' }));
    expect(await screen.findByText('Needs MB ID')).toBeTruthy();
    expect(screen.queryByText('Needs release group')).toBeNull();
    expect(screen.queryByText(/^Release ID:/)).toBeNull();
  });
});

// ── Import batch outcomes (#394) ─────────────────────────────────────────────

describe('Import batch folder outcomes (#394)', () => {
  const folder = (overrides: Partial<AiBatchFolderState>): AiBatchFolderState => ({
    folder_id: 'f', batch_job_id: 'b', source_folder: '/srv/dl/F', status: 'review_created', ...overrides,
  });

  it('labels symlinked audio and partial imports', () => {
    expect(folderAttentionLabel(folder({ failure_reason: 'symlinked_audio', review_item_id: 'r1' }))).toBe('Contains a linked file or folder');
    expect(folderAttentionLabel(folder({ status: 'review_required', metadata_imported: true, review_item_id: 'r2' }))).toBe('Imported in part');
    expect(folderAttentionLabel(folder({ status: 'review_required', metadata_imported: true }))).toBe('Review required');
    expect(folderAttentionLabel(folder({ status: 'importing' }))).toBe('Importing');
    expect(folderFailureText('symlinked_audio')).toMatch(/contains a linked file or folder.*even an empty one.*path runs through one/);
    expect(folderAttentionLabel(folder({ failure_reason: 'unverifiable_folder', review_item_id: 'r3' }))).toBe('Folder could not be checked');
    expect(folderFailureText('unverifiable_folder')).toMatch(/outside the configured import folders, part of it could not be read, or it holds no audio.*Check the folder location and permissions/);
    expect(folderFailureText('Beets imported part of this folder')).toBe('Beets imported part of this folder');
  });

  it('labels unknown server codes in plain words and keeps sentences', () => {
    expect(folderFailureText('duplicate_release_group')).toBe('duplicate release group');
    expect(folderFailureText('already in library')).toBe('already in library');
    expect(codeLabel('identify_failed')).toBe('identify failed');
    expect(codeLabel('')).toBe('');
  });

  it('review evidence names unreadable paths as text, capped at five', () => {
    const paths = Array.from({ length: 8 }, (_, i) => `/srv/dl/U/<b>d${i}</b>`);
    render(<BatchStopEvidence evidence={{ origin_step: 'pre_import', conflict: 'unverifiable_folder', unreadable_paths: paths }} />);
    expect(screen.getByText(/could not check this folder/)).toBeTruthy();
    expect(screen.getByText('Could not be read')).toBeTruthy();
    expect(screen.getByText('/srv/dl/U/<b>d0</b>')).toBeTruthy();
    expect(screen.getByText('/srv/dl/U/<b>d4</b>')).toBeTruthy();
    expect(screen.queryByText('/srv/dl/U/<b>d5</b>')).toBeNull();
    expect(screen.getByText('and 3 more')).toBeTruthy();
    expect(document.querySelector('b')).toBeNull();
  });

  it('review evidence covers links, partial imports, unknown steps, and empty path lists', () => {
    const { rerender } = render(<BatchStopEvidence evidence={{ origin_step: 'pre_import', conflict: 'symlinked_audio', symlinked_paths: ['/srv/dl/L/empty-link'] }} />);
    expect(screen.getByText(/contains a linked file or folder/)).toBeTruthy();
    expect(screen.getByText('Links found')).toBeTruthy();
    expect(screen.getByText('/srv/dl/L/empty-link')).toBeTruthy();
    rerender(<BatchStopEvidence evidence={{ origin_step: 'beets_partial', skipped_paths: ['/srv/dl/P/CD2'] }} />);
    expect(screen.getByText(/imported part of this folder/)).toBeTruthy();
    expect(screen.getByText('Skipped by Beets')).toBeTruthy();
    rerender(<BatchStopEvidence evidence={{ origin_step: 'some_new_step' }} />);
    expect(screen.getByText('Batch import stopped: some new step.')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
    rerender(<BatchStopEvidence evidence={{ origin_step: 'pre_import', conflict: 'unverifiable_folder', unreadable_paths: [] }} />);
    expect(screen.getByText(/could not check this folder/)).toBeTruthy();
    expect(screen.queryByText('and 0 more')).toBeNull();
    rerender(<BatchStopEvidence evidence={{}} />);
    expect(screen.queryByText(/Import refused|Batch import/)).toBeNull();
  });

  function Probe() {
    const location = useLocation();
    return <div data-testid="probe">{`${location.pathname}${location.search}`}</div>;
  }

  it('the batch shows the new outcomes and links the review item', async () => {
    localStorage.setItem('beets:ai-batch-import-job-id', 'job1');
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
    mocked.getAiBatchStatus.mockResolvedValue({
      state: {
        status: 'completed_with_warnings', job_id: 'job1', batch_job_id: 'job1', source_path: '/srv/dl',
        total_folders_found: 3, folders_processed: 3, folders_queued: 0, folders_running: 0, folders_completed: 0,
        folders_failed: 0, folders_skipped: 0, folders_review: 2,
        warnings: ['Beets fingerprinting (chroma plugin with fpcalc) is not available: Beets matches on tags, titles and durations only.'],
        folders: [
          { folder_id: 'f1', batch_job_id: 'job1', source_folder: '/srv/dl/Linked', status: 'review_created', failure_reason: 'symlinked_audio', review_item_id: 'r1', stage: 'done' },
          { folder_id: 'f3', batch_job_id: 'job1', source_folder: '/srv/dl/Unreadable', status: 'review_created', failure_reason: 'unverifiable_folder', review_item_id: 'r3', stage: 'done' },
          { folder_id: 'f2', batch_job_id: 'job1', source_folder: '/srv/dl/Partial', status: 'review_required', metadata_imported: true, review_item_id: 'r2', album_ids: [7],
            failure_reason: 'Beets imported part of this folder and skipped 1 path(s): /srv/dl/Partial/CD2', warnings: ['imported_without_fingerprint'] },
        ],
      },
    } as never);
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><IntakePanel /><Probe /></MemoryRouter></QueryClientProvider>);

    expect(await screen.findByText(/fpcalc\) is not available/)).toBeTruthy();
    expect(screen.getByText('Contains a linked file or folder')).toBeTruthy();
    expect(screen.getByText(/contains a linked file or folder \(a symlink/)).toBeTruthy();
    expect(screen.getByText('Folder could not be checked')).toBeTruthy();
    expect(screen.getByText(/Web Manager could not check this folder/)).toBeTruthy();
    expect(screen.getByText('Imported in part')).toBeTruthy();
    expect(screen.getByText(/skipped 1 path\(s\): \/srv\/dl\/Partial\/CD2/)).toBeTruthy();
    const links = screen.getAllByRole('button', { name: 'Open review item' });
    expect(links).toHaveLength(3);
    fireEvent.click(links[2]);
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe(`/import?tab=review&q=${encodeURIComponent('/srv/dl/Partial')}`));
  });
});
