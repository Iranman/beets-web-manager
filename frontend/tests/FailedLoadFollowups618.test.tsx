import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { EngineOfflineContext } from '../src/components/EngineOfflineBanner';
import Shell from '../src/components/layout/Shell';
import { HistoryPanel } from '../src/features/history/HistoryPanel';
import Jobs from '../src/views/Jobs';
import Library from '../src/views/Library';
import LibraryChanges from '../src/views/LibraryChanges';
import Submissions from '../src/views/Submissions';

// #618 follow-ups to #608. `engineUp` flips every /api answer between the
// backend's 503 ENGINE_OFFLINE shape and the per-path success bodies below.
let engineUp = false;
let failAll = false;
const ok: Record<string, unknown> = {
  '/api/library': { ok: true, artists: [], stats: { artists: 2, albums: 3, tracks: 4 }, library_version: 1, reconciled: true },
  '/api/transactions': { ok: true, transactions: [{ id: 'tx-1', created_at: 0, updated_at: 0, operation_type: 'album_cleanup', status: 'Applied', dry_run: false, summary: 'Old row', confidence: {}, counts: {} }], total: 1 },
  '/api/jobs': { ok: true, jobs: [] },
};

function mockFetch() {
  vi.spyOn(global, 'fetch').mockImplementation((input) => {
    const path = new URL(String(input), 'http://localhost').pathname;
    if (failAll || !engineUp) {
      return Promise.resolve(new Response(
        JSON.stringify({ ok: false, error: 'Beets library unavailable', error_code: 'ENGINE_OFFLINE' }),
        { status: 503, headers: { 'Content-Type': 'application/json' } },
      ));
    }
    return Promise.resolve(new Response(JSON.stringify(ok[path] ?? { ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  });
}

function renderInShell(path: string, page: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<Shell />}>
            <Route path={path} element={page} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}

const banner = () => screen.findByText(/Library data on this page can't be loaded/);
const bannerGone = () => waitFor(() => expect(screen.queryByText(/Library data on this page can't be loaded/)).toBeNull());

// The engine comes back and the Shell's probe (normally every 15 s) sees it.
async function engineReturns(client: QueryClient) {
  engineUp = true;
  await act(() => client.refetchQueries({ queryKey: ['engine-probe'] }));
  await bannerGone();
}

describe('failed loads: missed pages, recovery and stale rows (#618)', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup(); engineUp = false; failAll = false; });

  it('Recently imported does not claim "No entries yet." when the list failed', async () => {
    mockFetch();
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><HistoryPanel /></QueryClientProvider>);
    await screen.findByText(/Beets engine is unavailable/);
    expect(screen.queryByText('No entries yet.')).toBeNull();
  });

  it('Submissions does not claim "No items match this filter." when the queue failed', async () => {
    mockFetch();
    renderInShell('/submissions', <Submissions />);
    await banner();
    await waitFor(() => expect(screen.queryByRole('progressbar')).toBeNull());
    expect(screen.queryByText('No items match this filter.')).toBeNull();
  });

  it('Library reloads when the engine comes back', async () => {
    mockFetch();
    const client = renderInShell('/library', <Library />);
    await banner();
    await waitFor(() => expect(screen.getByText('— artists')).toBeTruthy());
    await engineReturns(client);
    await waitFor(() => expect(screen.getByText('2 artists')).toBeTruthy());
    await waitFor(() => expect(screen.queryByText(/Beets engine is unavailable/)).toBeNull());
  });

  it('Jobs reloads when the engine comes back', async () => {
    mockFetch();
    const client = renderInShell('/jobs', <Jobs />);
    await banner();
    await waitFor(() => expect(screen.getByText('— running')).toBeTruthy());
    await engineReturns(client);
    await waitFor(() => expect(screen.getByText('0 running')).toBeTruthy());
  });

  it('Library Changes reloads when the engine comes back', async () => {
    mockFetch();
    const client = renderInShell('/changes', <LibraryChanges />);
    await banner();
    await waitFor(() => expect(screen.getByText('Transactions unknown')).toBeTruthy());
    await engineReturns(client);
    await waitFor(() => expect(screen.getByText('1 transaction')).toBeTruthy());
  });

  it('Library Changes drops the previous rows when a refresh fails', async () => {
    engineUp = true;
    mockFetch();
    render(<MemoryRouter><LibraryChanges /></MemoryRouter>);
    await screen.findByText('1 transaction');
    expect(screen.getByRole('button', { name: /album_cleanup/ })).toBeTruthy();
    failAll = true;
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'x' } });
    await waitFor(() => expect(screen.getByText('Transactions unknown')).toBeTruthy());
    const list = screen.getByText('Transactions unknown').closest('div.overflow-hidden') as HTMLElement;
    expect(within(list).queryByRole('button', { name: /album_cleanup/ })).toBeNull();
  });

  // QA of #635: the banner is already up (probe cached in Shell context) when
  // the user opens the page. The page must still load, and show the failure as
  // unknown, not as zero or empty.
  function renderOffline(page: ReactElement) {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <EngineOfflineContext.Provider value>
          <MemoryRouter>{page}</MemoryRouter>
        </EngineOfflineContext.Provider>
      </QueryClientProvider>,
    );
  }
  const called = (path: string) => vi.mocked(global.fetch).mock.calls.some(([input]) => new URL(String(input), 'http://localhost').pathname === path);

  it('Library Changes still loads on mount while the probe already says offline', async () => {
    mockFetch();
    renderOffline(<LibraryChanges />);
    await waitFor(() => expect(screen.getByText('Transactions unknown')).toBeTruthy());
    expect(called('/api/transactions')).toBe(true);
    expect(screen.queryByText('No transactions found.')).toBeNull();
  });

  it('Library still loads on mount while the probe already says offline', async () => {
    mockFetch();
    renderOffline(<Library />);
    await waitFor(() => expect(screen.queryByText(/Loading library/)).toBeNull());
    expect(called('/api/library')).toBe(true);
    expect(screen.getByText('— artists')).toBeTruthy();
    expect(screen.queryByText(/^0 (artists|albums|tracks)$/)).toBeNull();
  });

  it('Jobs still loads on mount while the probe already says offline', async () => {
    mockFetch();
    renderOffline(<Jobs />);
    await waitFor(() => expect(screen.getByText('— running')).toBeTruthy());
    expect(called('/api/jobs')).toBe(true);
    expect(screen.queryByText(/^0 (running|failed|completed)$/)).toBeNull();
  });
});
