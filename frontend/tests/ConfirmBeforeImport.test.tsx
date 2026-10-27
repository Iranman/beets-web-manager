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
  it('Retry failed imports: lists the batch failures, no call on cancel, recoverAiBatch(batch, true) after confirm, then tracks the job', async () => {
    mocked.getAiBatchStatus.mockResolvedValue({
      state: {
        status: 'completed_with_warnings',
        batch_job_id: 'batch1',
        job_id: 'job0',
        source_path: '/srv/dl',
        folders_retryable: 1,
        folders: [
          { folder_id: 'f1', batch_job_id: 'batch1', source_folder: '/srv/dl/Artist - Broken', status: 'import_failed' },
          { folder_id: 'f2', batch_job_id: 'batch1', source_folder: '/srv/dl/Artist - Exhausted', status: 'failed', retry_exhausted: true },
          { folder_id: 'f3', batch_job_id: 'batch1', source_folder: '/srv/dl/Artist - Fine', status: 'imported' },
        ],
      },
    } as never);
    mocked.recoverAiBatch.mockResolvedValue({ ok: true, job_id: 'job2', state: { status: 'running', job_id: 'job2', batch_job_id: 'batch1' } } as never);
    const confirmSpy = vi.spyOn(window, 'confirm');
    wrap(<IntakePanel />);
    const button = await screen.findByRole('button', { name: 'Retry failed imports' });

    fireEvent.click(button);
    let dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(/retries 1 failed folder from the last batch import/)).toBeTruthy();
    expect(within(dialog).getByText(/Artist - Broken/)).toBeTruthy();
    expect(within(dialog).queryByText(/Artist - Exhausted/)).toBeNull();
    expect(within(dialog).queryByText(/Artist - Fine/)).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(mocked.recoverAiBatch).not.toHaveBeenCalled();

    fireEvent.click(button);
    dialog = await screen.findByRole('dialog');
    const retryButton = await within(dialog).findByRole('button', { name: 'Retry 1 folder' });
    // After the retry starts, the status endpoint reports the new job.
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'running', job_id: 'job2', batch_job_id: 'batch1', folders: [] } } as never);
    fireEvent.click(retryButton);
    await waitFor(() => expect(mocked.recoverAiBatch).toHaveBeenCalledWith('batch1', true));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(localStorage.getItem(JOB_KEY)).toBe('job2'));
    expect(await screen.findByText(/Retry started for \d+ failed folders?\. Progress shows below\./)).toBeTruthy();
  });

  it('Retry failed imports: nothing to retry keeps the confirm button disabled', async () => {
    mocked.getAiBatchStatus.mockResolvedValue({
      state: { status: 'completed', batch_job_id: 'batch1', source_path: '/srv/dl', folders_retryable: 0, folders: [] },
    } as never);
    wrap(<IntakePanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed imports' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(/has no failed folders that can be retried/)).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Retry 0 folders' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('Retry failed imports: a status error is shown and nothing is retried', async () => {
    mocked.getAiBatchStatus.mockRejectedValue(new Error('HTTP 503'));
    wrap(<IntakePanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed imports' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(/Could not load the last batch import: HTTP 503\. Nothing was retried/)).toBeTruthy();
    expect(mocked.recoverAiBatch).not.toHaveBeenCalled();
  });

  it('Retry eligible failures: confirm dialog first, no call on cancel, recoverAiBatch(job, true) after confirm', async () => {
    localStorage.setItem(JOB_KEY, 'job1');
    mocked.getAiBatchStatus.mockResolvedValue({
      state: {
        status: 'completed_with_warnings',
        job_id: 'job1',
        batch_job_id: 'job1',
        folders_retryable: 2,
        folders: [
          { folder_id: 'a', batch_job_id: 'job1', source_folder: '/srv/dl/One', status: 'timed_out' },
          { folder_id: 'b', batch_job_id: 'job1', source_folder: '/srv/dl/Two', status: 'ai_failed' },
        ],
      },
    } as never);
    wrap(<IntakePanel />);
    const button = await screen.findByRole('button', { name: 'Retry eligible failures' });

    fireEvent.click(button);
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/retries 2 failed folders/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(mocked.recoverAiBatch).not.toHaveBeenCalled();

    fireEvent.click(button);
    dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry 2 folders' }));
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
