import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Jobs from '../src/views/Jobs';

const job = { job_id: 'sync-7', label: 'Playlist sync', status: 'success', created_at: 1, log: ['synced 3 playlists'] };

function stubJobs() {
  vi.spyOn(global, 'fetch').mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith('/api/jobs') ? { ok: true, jobs: [job] }
      : url.includes('/api/jobs/sync-7') ? { ok: true, ...job }
        : { ok: true };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  });
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}><Jobs /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Jobs deep link (#356 last_job_id)', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup(); });

  it('opens the job named by ?job=', async () => {
    stubJobs();
    renderAt('/jobs?job=sync-7');
    expect(await screen.findByRole('dialog')).toBeTruthy();
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([u]) => String(u).includes('/api/jobs/sync-7'))).toBe(true));
  });

  it('says so when the linked job is not in the list', async () => {
    stubJobs();
    renderAt('/jobs?job=gone-1');
    expect(await screen.findByText(/Job gone-1 is not in the job list/)).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
