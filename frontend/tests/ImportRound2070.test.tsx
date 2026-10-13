// 0.7.0 Import page round 2: Library > Missing IDs split out of the Review
// Queue, server-side paging, quarantine-only cleanup messages, the summary's
// next action, and the batch import's evidence warnings / review links.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
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
import { cleanupResultMessage, ImportReviewPage, REVIEW_PAGE_SIZE } from '../src/features/importReview/ImportReviewPage';
import {
  batchEvidenceWarnings,
  beetsSkipLabel,
  folderStageLabel,
  folderStatusLabel,
  folderWarningLabel,
  IntakePanel,
  retryableFolders,
} from '../src/features/intake/IntakePanel';
import Import, { importNextAction } from '../src/views/Import';

const mocked = vi.mocked(client);

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

function Probe() {
  const location = useLocation();
  return <div data-testid="probe">{`${location.pathname}${location.search}`}</div>;
}

function missingRow(id: number, album: string, inReview = false): ReviewItem {
  return {
    id: `library_no_mb:album:${id}`,
    type: 'library_no_mb',
    target_kind: 'album',
    album_id: id,
    path: `/music/Artist/${album}`,
    album,
    artist: 'Artist',
    in_import_review: inReview,
  } as unknown as ReviewItem;
}

function pendingRow(id: string, album: string, type = 'pending_ai'): ReviewItem {
  return { id, type, path: `/downloads/${id}`, album, artist: 'Artist' } as unknown as ReviewItem;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('Library > Missing IDs (ImportReviewPage mode="missing_ids")', () => {
  it('reads the library endpoint, pages and filters by kind on the server, and shows the in-review badge', async () => {
    mocked.getLibraryMissingMbIds.mockResolvedValue({
      ok: true,
      items: [missingRow(1, 'First Album', true), missingRow(2, 'Second Album')],
      total: 250,
      offset: 0,
      limit: REVIEW_PAGE_SIZE,
      counts: { all: 250, album: 200, item: 50 },
    } as never);
    render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);

    await screen.findAllByText('First Album');
    expect(mocked.getLibraryMissingMbIds).toHaveBeenCalledWith({ kind: 'all', search: '', offset: 0, limit: REVIEW_PAGE_SIZE });
    expect(mocked.getReviewQueue).not.toHaveBeenCalled();
    expect(screen.getAllByText('In import review').length).toBeGreaterThan(0);
    expect(screen.getByText(`Showing 1–${REVIEW_PAGE_SIZE} of 250`)).toBeTruthy();
    // Import-queue-only actions are not offered on library rows.
    expect(screen.queryByRole('button', { name: 'Revalidate & Import…' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Clean Stale…' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(mocked.getLibraryMissingMbIds).toHaveBeenLastCalledWith({ kind: 'all', search: '', offset: REVIEW_PAGE_SIZE, limit: REVIEW_PAGE_SIZE }));

    const kindGroup = screen.getByRole('group', { name: 'Missing ID kind' });
    fireEvent.click(within(kindGroup).getByRole('button', { name: /Tracks \(no Recording ID\) \(50\)/ }));
    // A filter change goes back to the first page.
    await waitFor(() => expect(mocked.getLibraryMissingMbIds).toHaveBeenLastCalledWith({ kind: 'item', search: '', offset: 0, limit: REVIEW_PAGE_SIZE }));
    expect(within(kindGroup).getByRole('button', { name: /Tracks/ }).getAttribute('aria-pressed')).toBe('true');
  });

  it('sends search to the server after the debounce', async () => {
    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [missingRow(1, 'First Album')], total: 1, counts: { all: 1, album: 1, item: 0 } } as never);
    render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
    await screen.findAllByText('First Album');
    fireEvent.change(screen.getByLabelText('Search title, artist, album or path'), { target: { value: 'beatles' } });
    await waitFor(
      () => expect(mocked.getLibraryMissingMbIds).toHaveBeenLastCalledWith({ kind: 'all', search: 'beatles', offset: 0, limit: REVIEW_PAGE_SIZE }),
      { timeout: 3000 },
    );
  });

  it('says what failed and what to do next when Beets cannot be read', async () => {
    mocked.getLibraryMissingMbIds.mockRejectedValue(new Error('Beets is unavailable'));
    render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
    expect(await screen.findByText(/Could not load library albums and tracks missing MusicBrainz IDs/)).toBeTruthy();
    expect(screen.getByText(/check the Beets status on the System page/)).toBeTruthy();
  });
});

describe('Review Queue paging and filters go to the server', () => {
  it('pages with offset and asks for counts separately when a status filter is set', async () => {
    mocked.getReviewQueue.mockResolvedValue({
      ok: true,
      items: [pendingRow('a', 'First Album', 'skipped')],
      total: 150,
      counts: { all: 150, pending_ai: 140, skipped: 10 },
    } as never);
    render(<MemoryRouter><ImportReviewPage active initialFilter="skipped" /></MemoryRouter>);
    await screen.findAllByText('First Album');
    await waitFor(() => expect(mocked.getReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ status: 'skipped', offset: 0, limit: REVIEW_PAGE_SIZE })));
    expect(mocked.getReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ limit: 0 }));
    expect(mocked.getReviewQueue).not.toHaveBeenCalledWith(expect.objectContaining({ status: 'library_no_mb' }));

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(mocked.getReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ status: 'skipped', offset: REVIEW_PAGE_SIZE })));
  });

  it('links to Library > Missing IDs instead of listing library rows', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: [pendingRow('a', 'First Album')], total: 1, counts: { all: 1, pending_ai: 1 } } as never);
    render(<MemoryRouter><ImportReviewPage active /><Probe /></MemoryRouter>);
    await screen.findAllByText('First Album');
    fireEvent.click(screen.getByRole('link', { name: /Missing IDs/ }));
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('/library/missing-ids'));
  });
});

describe('cleanupResultMessage', () => {
  it('reports quarantine counts and that files can be restored', () => {
    expect(cleanupResultMessage({ ok: true, quarantined_count: 2, deleted_count: 0, remaining_audio_count: 3 } as never))
      .toMatch(/^Quarantined 2 files.*Quarantined files can be restored\.$/);
  });

  it('says when the server downgraded a delete to quarantine', () => {
    expect(cleanupResultMessage({ ok: true, quarantined_count: 1, deleted_count: 0, downgraded_to_quarantine: true } as never))
      .toMatch(/quarantined them instead of deleting them/);
  });

  it('never claims files were handled when none were', () => {
    expect(cleanupResultMessage({ ok: true, quarantined_count: 0, deleted_count: 0 } as never)).not.toMatch(/can be restored/);
  });
});

describe('Import summary next action', () => {
  const base = {
    loading: false, error: '', acquireTotal: 0, missingMusic: 0, lidarrWanted: 0, merged: 0,
    acquireCache: 'ready' as const, acquireCacheBuilt: true, partial: false,
    reviewTotal: 0, pendingAi: 0, skipped: 0, missingMb: 0, runningJobs: 0, failedJobs: 0, latestJob: '',
  };

  it('is Ready only when every count was read and the Find Music list is built', () => {
    expect(importNextAction(base)).toBe('Ready');
    expect(importNextAction({ ...base, acquireCache: 'building' })).toBe('Find Music list building');
    expect(importNextAction({ ...base, acquireCache: 'empty', acquireCacheBuilt: false })).toBe('Find Music list not built');
    expect(importNextAction({ ...base, acquireCache: 'failed', acquireCacheBuilt: false })).toBe('Find Music list failed');
    expect(importNextAction({ ...base, partial: true })).toBe('Status incomplete');
    expect(importNextAction({ ...base, acquireCache: null, partial: true })).not.toBe('Ready');
  });

  it('a failed rebuild with an older built list still shows what that list says', () => {
    expect(importNextAction({ ...base, acquireCache: 'failed', acquireCacheBuilt: true, missingMusic: 3 })).toBe('Find music');
  });

  it('review work comes first', () => {
    expect(importNextAction({ ...base, reviewTotal: 2, acquireCache: 'building' })).toBe('Review tags');
    expect(importNextAction({ ...base, missingMb: 4 })).toBe('Review IDs');
  });
});

describe('Batch import labels and warnings', () => {
  it('labels statuses, stages, skip reasons and warning codes', () => {
    expect(folderStatusLabel('importing')).toBe('Importing');
    expect(folderStatusLabel('review_created')).toBe('Review created');
    expect(folderStatusLabel('something_new')).toBe('something_new');
    expect(folderStageLabel('pinned_import')).toBe('Importing with the chosen release');
    expect(beetsSkipLabel('duplicate')).toBe('Beets skipped it: already in the library');
    expect(folderWarningLabel('imported_without_fingerprint')).toBe('Imported without fingerprint proof');
  });

  it('warns when fingerprinting is unavailable and prefers the server sentences', () => {
    expect(batchEvidenceWarnings({ evidence_capabilities: { beets_fingerprinting: false, acoustid_key: true, ai_configured: false } } as never)[0])
      .toMatch(/chroma plugin\) is not available/);
    expect(batchEvidenceWarnings({ evidence_capabilities: { beets_fingerprinting: true, acoustid_key: false, ai_configured: false } } as never)[0])
      .toMatch(/No AcoustID API key/);
    expect(batchEvidenceWarnings({ evidence_capabilities: { beets_fingerprinting: true, acoustid_key: true, ai_configured: true } } as never)).toEqual([]);
    expect(batchEvidenceWarnings({ warnings: ['Server says so.'], evidence_capabilities: { beets_fingerprinting: false, acoustid_key: false, ai_configured: false } } as never))
      .toEqual(['Server says so.']);
  });

  it('retryableFolders skips folders at their retry limit', () => {
    const folders = retryableFolders({
      folders: [
        { folder_id: '1', status: 'import_failed' },
        { folder_id: '2', status: 'timed_out', retry_exhausted: true },
        { folder_id: '3', status: 'imported' },
      ],
    } as never);
    expect(folders.map((f) => f.folder_id)).toEqual(['1']);
  });

  it('JobLog shows the no-fingerprint warning, folder warnings and an Open in review link', async () => {
    localStorage.setItem('beets:ai-batch-import-job-id', 'job1');
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
    mocked.getAiBatchStatus.mockResolvedValue({
      state: {
        status: 'completed_with_warnings',
        job_id: 'job1',
        batch_job_id: 'job1',
        source_path: '/srv/dl',
        total_folders_found: 2,
        folders_processed: 2,
        folders_queued: 0,
        folders_running: 0,
        folders_completed: 1,
        folders_failed: 0,
        folders_skipped: 0,
        folders_review: 1,
        evidence_capabilities: { beets_fingerprinting: false, acoustid_key: true, ai_configured: false },
        folders: [
          { folder_id: 'f1', batch_job_id: 'job1', source_folder: '/srv/dl/Imported One', status: 'imported', warnings: ['imported_without_fingerprint'] },
          {
            folder_id: 'f2', batch_job_id: 'job1', source_folder: '/srv/dl/Needs Review', status: 'review_created',
            beets_skip_reason: 'no_strong_match', warnings: ['identity_conflict'],
            auto_import: { eligible: false, reasons: ['confidence below threshold'] },
          },
        ],
      },
    } as never);
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><IntakePanel /><Probe /></MemoryRouter></QueryClientProvider>);

    expect(await screen.findByText(/chroma plugin\) is not available/)).toBeTruthy();
    expect(screen.getByText(/1 folder was imported without fingerprint proof/)).toBeTruthy();
    expect(screen.getByText('Beets skipped it: no strong MusicBrainz match')).toBeTruthy();
    expect(screen.getByText('Not auto-imported: confidence below threshold')).toBeTruthy();
    expect(screen.getByText('identity conflict')).toBeTruthy();
    // The imported-without-fingerprint folder is summarised once, not listed.
    expect(screen.queryByText('Imported One')).toBeNull();
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'Open in review' }));
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe(`/import?tab=review&q=${encodeURIComponent('/srv/dl/Needs Review')}`));
  });
});

describe('Old Review Queue links to library rows', () => {
  it('/import?tab=review&filter=library_no_mb redirects to /library/missing-ids', async () => {
    render(
      <MemoryRouter initialEntries={['/import?tab=review&filter=library_no_mb']}>
        <Routes>
          <Route path="/import" element={<Import />} />
          <Route path="/library/missing-ids" element={<div>MISSING_IDS</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText('MISSING_IDS')).toBeTruthy();
  });
});
