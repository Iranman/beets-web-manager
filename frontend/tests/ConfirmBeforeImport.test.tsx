// 0.5.1: every Import-page button that starts imports or deletes data asks first.
// The API must not be called before Confirm, must be called after it, and not on Cancel.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    previewImportTarget: vi.fn(),
    revalidateImportReview: vi.fn(),
    cleanupStaleReview: vi.fn(),
    getAiBatchStatus: vi.fn(),
    getSetupStatus: vi.fn(),
    runPreflight: vi.fn(),
    recoverAiBatch: vi.fn(),
    retryLibraryImportAllFailed: vi.fn(),
    getRecentImports: vi.fn(),
    getAiMatchHistory: vi.fn(),
    clearRecentImports: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { HistoryPanel } from '../src/features/history/HistoryPanel';
import { ImportReviewPage } from '../src/features/importReview/ImportReviewPage';
import { IntakePanel } from '../src/features/intake/IntakePanel';

const mocked = vi.mocked(client);
const JOB_KEY = 'beets:ai-batch-import-job-id';

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

function pending(id: string, album: string): ReviewItem {
  return { id, type: 'pending_ai', path: `/downloads/${id}`, album, artist: 'Artist' } as unknown as ReviewItem;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [pending('a', 'First Album'), pending('b', 'Second Album')],
    counts: { all: 2, pending_ai: 2 },
  } as never);
  mocked.previewImportTarget.mockReturnValue(new Promise(() => undefined) as never);
  mocked.revalidateImportReview.mockResolvedValue({ ok: true, items: [], reviewed_count: 2, queued_count: 1 } as never);
  mocked.cleanupStaleReview.mockResolvedValue({ ok: true, removed_total: 0, removed_folder_gone: 0, removed_mb_in_library: 0, remaining: 2 } as never);
  mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
  mocked.retryLibraryImportAllFailed.mockResolvedValue({ ok: true, job_id: 'r1' } as never);
  mocked.recoverAiBatch.mockResolvedValue({ ok: true, job_id: 'job1', state: { status: 'running', job_id: 'job1' } } as never);
  mocked.getRecentImports.mockResolvedValue({ imports: [{ artist: 'A', album: 'B', timestamp: 1 }] } as never);
  mocked.getAiMatchHistory.mockResolvedValue({ history: [] } as never);
  mocked.clearRecentImports.mockResolvedValue({ ok: true } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('Import Review: Revalidate & Import and Clean Stale confirm first', () => {
  it('Revalidate & Import calls nothing until Confirm, nothing on Cancel, auto_enqueue after Confirm', async () => {
    wrap(<ImportReviewPage />);
    await screen.findAllByText('First Album');
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'Revalidate & Import…' }));
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/re-checks 2 visible pending review items and may start import jobs/)).toBeTruthy();
    expect(mocked.revalidateImportReview).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(mocked.revalidateImportReview).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Revalidate & Import…' }));
    dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revalidate 2 and import eligible' }));
    await waitFor(() => expect(mocked.revalidateImportReview).toHaveBeenCalledTimes(1));
    expect(mocked.revalidateImportReview).toHaveBeenCalledWith({ review_item_ids: ['a', 'b'], auto_enqueue: true, limit: 2 });
  });

  it('Clean Stale calls nothing until Confirm and nothing on Cancel', async () => {
    wrap(<ImportReviewPage />);
    await screen.findAllByText('First Album');
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'Clean Stale…' }));
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/no audio files are deleted/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(mocked.cleanupStaleReview).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Clean Stale…' }));
    dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clean stale entries' }));
    await waitFor(() => expect(mocked.cleanupStaleReview).toHaveBeenCalledTimes(1));
  });

  it('AltGr (Ctrl+Alt) + ] still moves to the next item; Ctrl+] alone does not', async () => {
    wrap(<ImportReviewPage />);
    await screen.findAllByText('First Album');
    await settle();
    fireEvent.keyDown(window, { key: ']', ctrlKey: true });
    await settle();
    expect(screen.getByRole('heading', { level: 2, name: /First Album/ })).toBeTruthy();
    fireEvent.keyDown(window, { key: ']', ctrlKey: true, altKey: true });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: /Second Album/ })).toBeTruthy());
  });
});

describe('Intake: retry and recover actions confirm first', () => {
  it('Retry failed imports: no call on cancel, call after confirm', async () => {
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'idle' } } as never);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    wrap(<IntakePanel />);
    const button = await screen.findByRole('button', { name: 'Retry failed imports' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/starts an import job/));
    await settle();
    expect(mocked.retryLibraryImportAllFailed).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.retryLibraryImportAllFailed).toHaveBeenCalledTimes(1));
  });

  it('Retry eligible failures: no call on cancel, recoverAiBatch(job, true) after confirm', async () => {
    localStorage.setItem(JOB_KEY, 'job1');
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'completed_with_warnings', job_id: 'job1', folders_retryable: 2 } } as never);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    wrap(<IntakePanel />);
    const button = await screen.findByRole('button', { name: 'Retry eligible failures' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/starts imports again/));
    await settle();
    expect(mocked.recoverAiBatch).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.recoverAiBatch).toHaveBeenCalledWith('job1', true));
  });

  it('Recover batch: no call on cancel, recoverAiBatch(job, false) after confirm', async () => {
    localStorage.setItem(JOB_KEY, 'job1');
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'paused', job_id: 'job1' } } as never);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    wrap(<IntakePanel />);
    const button = await screen.findByRole('button', { name: 'Recover batch' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/resumes imports/));
    await settle();
    expect(mocked.recoverAiBatch).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.recoverAiBatch).toHaveBeenCalledWith('job1', false));
  });
});

describe('History: Clear confirms first', () => {
  it('no call on cancel, clearRecentImports after confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    wrap(<HistoryPanel />);
    const button = await screen.findByRole('button', { name: 'Clear' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await settle();
    expect(mocked.clearRecentImports).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.clearRecentImports).toHaveBeenCalledTimes(1));
  });
});
