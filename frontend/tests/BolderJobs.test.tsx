import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Jobs from '../src/views/Jobs';

// Bolder Shelf elevation of Jobs & Cleanup: the restyle is additive (classNames, the shared
// RecordRing motif, one glass overlay). These tests pin the new structure and, crucially, that the
// "record turns only while a job runs" behaviour is unchanged.

function stubFetch(jobs: unknown[], detail?: Record<string, unknown>) {
  vi.spyOn(global, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    let body: Record<string, unknown> = { ok: true };
    if (url.endsWith('/api/jobs')) body = { ok: true, jobs };
    else if (url.includes('/api/jobs/feed')) body = { ok: true, entries: [] };
    else if (url.includes('/api/jobs/maintenance-runner/report')) body = { ok: true, exists: false, report: {} };
    else if (detail && url.includes(`/api/jobs/${detail.job_id}`)) body = { ok: true, ...detail };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  });
}

function renderAt(path = '/jobs') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}><Jobs /></MemoryRouter>
    </QueryClientProvider>,
  );
  return utils.container;
}

describe('Bolder Jobs & Cleanup', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup(); });

  it('scopes the bolder world to the Jobs page and keeps the oversized completion count', async () => {
    stubFetch([]);
    const container = renderAt();
    // Scope hook for the appended CSS block, so shared ops-* surfaces elsewhere stay untouched.
    expect(container.querySelector('.ops-page.jobs-bolder')).toBeTruthy();
    // The Clean All completion is the one oversized liner-note count (display serif).
    await waitFor(() => expect(container.querySelector('.jobs-clean-pct')).toBeTruthy());
  });

  it('turns the record and draws the brass progress ring only while a job runs', async () => {
    stubFetch([
      { job_id: 'scan-1', label: 'Folder scan', status: 'running', created_at: 1, started_at: 1, state: { progress_percent: 40 }, log: ['scanning'] },
    ]);
    const container = renderAt();
    await waitFor(() => expect(container.querySelector('.ops-platter[data-playing="true"]')).toBeTruthy());
    // The progress arc is drawn (fearless brass for the live state).
    expect(container.querySelector('.ops-platter[data-playing="true"] .ops-ring-progress')).toBeTruthy();
  });

  it('keeps the platter still and shows the record-ring spinner while idle/loading', async () => {
    stubFetch([]);
    const container = renderAt();
    // During the first load the station shows the shared RecordRing as a loading spinner.
    expect(container.querySelector('.record-ring.is-spinning')).toBeTruthy();
    // Once idle, the empty platter is present and not turning.
    await waitFor(() => {
      const platters = container.querySelectorAll('.ops-platter');
      expect(platters.length).toBeGreaterThan(0);
      expect(container.querySelector('.ops-platter[data-playing="true"]')).toBeNull();
    });
  });

  it('dresses the job-details overlay as the one frosted-glass moment', async () => {
    const detail = { job_id: 'sync-7', label: 'Playlist sync', status: 'success', created_at: 1, log: ['done'] };
    stubFetch([detail], detail);
    renderAt('/jobs?job=sync-7');
    expect(await screen.findByRole('dialog')).toBeTruthy();
    // Backdrop is portaled to the body, so query the document.
    expect(document.querySelector('.jobs-modal-glass')).toBeTruthy();
  });
});
