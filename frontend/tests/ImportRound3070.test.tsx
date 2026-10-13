// 0.7.0 Import page round 3: large revalidations run as a job (#396 B1), the
// Missing IDs list has no idle refresh, the batch retry reports what really
// happened, and results are announced to screen readers.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    getLibraryMissingMbIds: vi.fn(),
    previewImportTarget: vi.fn(() => new Promise(() => undefined)),
    revalidateImportReview: vi.fn(),
    getJob: vi.fn(),
    getAiBatchStatus: vi.fn(),
    recoverAiBatch: vi.fn(),
    getSetupStatus: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import {
  ImportReviewPage,
  REVALIDATE_JOB_STORAGE_KEY,
  revalidateJobCounts,
} from '../src/features/importReview/ImportReviewPage';
import { IntakePanel, retryOutcome, retryOutcomeMessage } from '../src/features/intake/IntakePanel';

const mocked = vi.mocked(client);
const JOB_TIMEOUT = { timeout: 8000 };

function pending(n: number): ReviewItem[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `p${i}`,
    type: 'pending_ai',
    path: `/downloads/p${i}`,
    album: `Album ${i}`,
    artist: 'Artist',
  })) as unknown as ReviewItem[];
}

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

function statusText() {
  return screen.getAllByRole('status').map((node) => node.textContent ?? '').join(' | ');
}

async function startRevalidate(count: number) {
  render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
  await screen.findAllByText('Album 0');
  await settle();
  fireEvent.click(screen.getByRole('button', { name: 'Revalidate & Import…' }));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: `Revalidate ${count} and import eligible` }));
  await waitFor(() => expect(mocked.revalidateImportReview).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('Revalidate & Import: synchronous and job forms (#396 B1)', () => {
  it('up to 25 items: reports the synchronous counts and polls no job', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: pending(2), total: 2, counts: { all: 2, pending_ai: 2 } } as never);
    mocked.revalidateImportReview.mockResolvedValue({ ok: true, reviewed_count: 2, updated_count: 1, queued_count: 1, failed_count: 0, items: [] } as never);
    await startRevalidate(2);
    expect(await screen.findByText('Revalidated 2; queued 1; updated 1; failed 0.')).toBeTruthy();
    expect(mocked.getJob).not.toHaveBeenCalled();
    expect(localStorage.getItem(REVALIDATE_JOB_STORAGE_KEY)).toBeNull();
  });

  it('202 job: says it runs in the background, never "Revalidated 0", then reports the job counts and reloads', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: pending(30), total: 30, counts: { all: 30, pending_ai: 30 } } as never);
    mocked.revalidateImportReview.mockResolvedValue({ ok: true, job_id: 'rv1', mode: 'job' } as never);
    mocked.getJob
      .mockResolvedValueOnce({ ok: true, job_id: 'rv1', status: 'running' } as never)
      .mockResolvedValue({
        ok: true, job_id: 'rv1', status: 'success',
        result: { ok: true, reviewed_count: 30, updated_count: 5, queued_count: 2, failed_count: 1, items: [] },
      } as never);
    await startRevalidate(30);
    expect(mocked.revalidateImportReview).toHaveBeenCalledWith({ review_item_ids: pending(30).map((i) => i.id), auto_enqueue: true, limit: 30 });

    expect(await screen.findByText(/running as a background job \(rv1\)/)).toBeTruthy();
    expect(screen.queryByText(/Revalidated 0/)).toBeNull();
    expect(localStorage.getItem(REVALIDATE_JOB_STORAGE_KEY)).toBe('rv1');
    expect(screen.getByRole('button', { name: 'Revalidating…' })).toBeTruthy();
    const loadsBefore = mocked.getReviewQueue.mock.calls.length;

    expect(await screen.findByText('Background revalidation finished. Revalidated 30; queued 2; updated 5; failed 1.', undefined, JOB_TIMEOUT)).toBeTruthy();
    expect(mocked.getJob).toHaveBeenCalledWith('rv1');
    expect(mocked.getReviewQueue.mock.calls.length).toBeGreaterThan(loadsBefore);
    expect(localStorage.getItem(REVALIDATE_JOB_STORAGE_KEY)).toBeNull();
    expect(screen.queryByText(/Revalidated 0/)).toBeNull();
  }, 15000);

  it('409 job_already_running (followed by apiJson): says nothing new started and follows that job', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: pending(30), total: 30, counts: { all: 30, pending_ai: 30 } } as never);
    mocked.revalidateImportReview.mockResolvedValue({ ok: true, job_id: 'rv2', already_running: true, replay: false } as never);
    mocked.getJob.mockResolvedValue({
      ok: true, job_id: 'rv2', status: 'success',
      result: { ok: true, reviewed_count: 12, updated_count: 3, queued_count: 0, failed_count: 0, items: [] },
    } as never);
    await startRevalidate(30);
    expect(await screen.findByText(/already running as a background job \(rv2\); nothing new was started/)).toBeTruthy();
    expect(screen.queryByText(/Revalidated 0/)).toBeNull();
    expect(await screen.findByText('Background revalidation finished. Revalidated 12; queued 0; updated 3; failed 0.', undefined, JOB_TIMEOUT)).toBeTruthy();
  }, 15000);

  it('a failed job reports its last log line and reloads the queue', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: pending(30), total: 30, counts: { all: 30, pending_ai: 30 } } as never);
    mocked.revalidateImportReview.mockResolvedValue({ ok: true, job_id: 'rv3', mode: 'job' } as never);
    mocked.getJob.mockResolvedValue({ ok: true, job_id: 'rv3', status: 'failed', log: ['start', 'MusicBrainz unavailable'] } as never);
    await startRevalidate(30);
    expect(await screen.findByText(/Background revalidation failed: MusicBrainz unavailable\. Items it reached/, undefined, JOB_TIMEOUT)).toBeTruthy();
    expect(localStorage.getItem(REVALIDATE_JOB_STORAGE_KEY)).toBeNull();
  }, 15000);

  it('a job still running after a reload is followed again', async () => {
    localStorage.setItem(REVALIDATE_JOB_STORAGE_KEY, 'rv4');
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: pending(1), total: 1, counts: { all: 1, pending_ai: 1 } } as never);
    mocked.getJob.mockResolvedValue({
      ok: true, job_id: 'rv4', status: 'success',
      result: { ok: true, reviewed_count: 40, updated_count: 0, queued_count: 0, failed_count: 0, items: [] },
    } as never);
    render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
    expect(await screen.findByText(/Following the background revalidation job \(rv4\)/)).toBeTruthy();
    expect(await screen.findByText(/Revalidated 40; queued 0/, undefined, JOB_TIMEOUT)).toBeTruthy();
  }, 15000);

  it('revalidateJobCounts reads full results and the summary of a truncated one', () => {
    expect(revalidateJobCounts({ ok: true, reviewed_count: 3, updated_count: 1, queued_count: 1, failed_count: 0, items: [] })?.reviewed_count).toBe(3);
    expect(revalidateJobCounts({ truncated: true, summary: { scalars: { ok: true, reviewed_count: 900, updated_count: 2, queued_count: 4, failed_count: 1 } } }))
      .toMatchObject({ reviewed_count: 900, queued_count: 4, items: [] });
    expect(revalidateJobCounts({ truncated: true, summary: { scalars: {} } })).toBeNull();
    expect(revalidateJobCounts(undefined)).toBeNull();
  });
});

describe('Missing IDs: no idle auto-refresh (QA #395 F3)', () => {
  it('sets no 60 s poll in missing_ids mode; the Review Queue still does', async () => {
    const spy = vi.spyOn(window, 'setInterval');
    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [], total: 0, counts: { all: 0, album: 0, item: 0 } } as never);
    const view = render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
    await waitFor(() => expect(mocked.getLibraryMissingMbIds).toHaveBeenCalled());
    await settle();
    expect(spy.mock.calls.some((call) => call[1] === 60000)).toBe(false);
    view.unmount();

    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: [], total: 0, counts: { all: 0 } } as never);
    render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
    await waitFor(() => expect(mocked.getReviewQueue).toHaveBeenCalled());
    await settle();
    expect(spy.mock.calls.some((call) => call[1] === 60000)).toBe(true);
  });
});

describe('Results are announced politely (QA #395 F5)', () => {
  it('announces the page range and search results on Missing IDs', async () => {
    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [], total: 250, counts: { all: 250, album: 250, item: 0 } } as never);
    render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
    await waitFor(() => expect(statusText()).toContain('Showing 1 to 100 of 250.'));
    const live = screen.getAllByRole('status').find((node) => node.textContent?.includes('Showing'));
    expect(live?.getAttribute('aria-live')).toBe('polite');

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(statusText()).toContain('Showing 101 to 200 of 250.'));

    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [], total: 0, counts: { all: 0, album: 0, item: 0 } } as never);
    fireEvent.change(screen.getByLabelText('Search title, artist, album or path'), { target: { value: 'nothing' } });
    await waitFor(() => expect(statusText()).toContain('No results for "nothing".'), { timeout: 3000 });
  });

  it('announces search results on the Review Queue', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: pending(1), total: 1, counts: { all: 1, pending_ai: 1 } } as never);
    render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
    await waitFor(() => expect(statusText()).toContain('Showing 1 to 1 of 1.'));
    fireEvent.change(screen.getByLabelText('Search review queue'), { target: { value: 'album' } });
    await waitFor(() => expect(statusText()).toContain('Showing 1 to 1 of 1 results for "album".'), { timeout: 3000 });
  });
});

describe('Batch retry reports what happened (QA #395 F4/F5)', () => {
  it('retryOutcome tells a new start from a reconnect and from nothing to retry', () => {
    expect(retryOutcome({ reconnected: false })).toBe('started');
    expect(retryOutcome({ reconnected: true, state: { status: 'running', folders_retryable: 2 } as never })).toBe('already_running');
    expect(retryOutcome({ reconnected: true, state: { status: 'completed', folders_retryable: 0 } as never })).toBe('nothing_to_retry');
    expect(retryOutcomeMessage('started', 1)).toBe('Retry started for 1 failed folder. Progress shows below.');
    expect(retryOutcomeMessage('already_running', 3)).toMatch(/^Nothing new was started/);
    expect(retryOutcomeMessage('nothing_to_retry', 3)).toMatch(/^Nothing was retried/);
  });

  function renderIntake() {
    return render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter><IntakePanel /></MemoryRouter>
      </QueryClientProvider>,
    );
  }

  const failedBatch = {
    state: {
      status: 'completed_with_warnings', batch_job_id: 'b1', job_id: 'j0', source_path: '/srv/dl', folders_retryable: 1,
      folders: [{ folder_id: 'f1', batch_job_id: 'b1', source_folder: '/srv/dl/Broken', status: 'import_failed' }],
    },
  };

  it('does not claim a retry started when the server only reconnected with nothing to retry', async () => {
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
    mocked.getAiBatchStatus.mockResolvedValue(failedBatch as never);
    mocked.recoverAiBatch.mockResolvedValue({
      ok: true, job_id: 'j0', batch_job_id: 'b1', reconnected: true,
      state: { ...failedBatch.state, status: 'completed', folders_retryable: 0 },
    } as never);
    renderIntake();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed imports' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Retry 1 folder' }));
    expect(await screen.findByText(/^Nothing was retried/)).toBeTruthy();
    expect(screen.queryByText(/Retry started/)).toBeNull();
    expect(localStorage.getItem('beets:ai-batch-import-job-id')).toBeNull();
  });

  it('says nothing new started when a worker is already running, and follows it', async () => {
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
    mocked.getAiBatchStatus.mockResolvedValue(failedBatch as never);
    mocked.recoverAiBatch.mockResolvedValue({
      ok: true, job_id: 'j9', batch_job_id: 'b1', reconnected: true,
      state: { ...failedBatch.state, status: 'running', job_id: 'j9' },
    } as never);
    renderIntake();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed imports' }));
    const dialog = await screen.findByRole('dialog');
    const retry = await within(dialog).findByRole('button', { name: 'Retry 1 folder' });
    mocked.getAiBatchStatus.mockResolvedValue({ state: { ...failedBatch.state, status: 'running', job_id: 'j9' } } as never);
    fireEvent.click(retry);
    expect(await screen.findByText(/^Nothing new was started: an import for this batch is already running/)).toBeTruthy();
    await waitFor(() => expect(localStorage.getItem('beets:ai-batch-import-job-id')).toBe('j9'));
  });

  it('the loading bar in the retry dialog has an accessible name', async () => {
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
    mocked.getAiBatchStatus.mockResolvedValueOnce({ state: { status: 'idle' } } as never);
    renderIntake();
    const button = await screen.findByRole('button', { name: 'Retry failed imports' });
    mocked.getAiBatchStatus.mockReturnValue(new Promise(() => undefined) as never);
    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('progressbar', { name: 'Loading the last batch import' })).toBeTruthy();
  });
});
