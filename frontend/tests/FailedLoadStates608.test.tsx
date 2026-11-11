import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Shell from '../src/components/layout/Shell';
import { sentences } from '../src/lib/errors';
import Jobs from '../src/views/Jobs';
import Library from '../src/views/Library';
import LibraryChanges from '../src/views/LibraryChanges';
import Playlists from '../src/views/Playlists';

// #608: with the Beets engine down, every /api call answers 503 ENGINE_OFFLINE
// (the backend's beets_offline_response shape).
function engineDown() {
  vi.spyOn(global, 'fetch').mockImplementation(() => Promise.resolve(new Response(
    JSON.stringify({ ok: false, error: 'Beets library unavailable', error_code: 'ENGINE_OFFLINE' }),
    { status: 503, headers: { 'Content-Type': 'application/json' } },
  )));
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
}

// The page banner, once the Shell's engine probe has answered.
const banner = () => screen.findByText(/Library data on this page can't be loaded/);
// Alerts that open with the bare engine message: only the banner may.
const engineAlerts = () => screen.getAllByText(/^Beets engine is unavailable\./);

describe('failed loads are not shown as real results (#608)', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup(); });

  it('Library shows unknown counts, no "No review issues" and no empty list', async () => {
    engineDown();
    renderInShell('/library', <Library />);
    await banner();
    await waitFor(() => expect(screen.queryByText(/Loading library/)).toBeNull());
    expect(screen.getByText('— artists')).toBeTruthy();
    expect(screen.queryByText(/^0 (artists|albums|tracks)$/)).toBeNull();
    expect(screen.queryByText('No review issues')).toBeNull();
    expect(screen.queryByText('No matching artists.')).toBeNull();
    expect(engineAlerts()).toHaveLength(1);
  });

  it('Jobs shows one engine banner linking to System, unknown counters and a disabled Clean All', async () => {
    engineDown();
    renderInShell('/jobs', <Jobs />);
    await banner();
    await waitFor(() => expect(screen.getByText('— running')).toBeTruthy());
    await waitFor(() => expect(engineAlerts()).toHaveLength(1));
    expect(screen.getByRole('link', { name: 'System page' }).getAttribute('href')).toBe('/system');
    expect(screen.queryByText(/^0 (running|failed|completed)$/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Clean All' })).toHaveProperty('disabled', true);
  });

  it('Playlists disables Sync All to Plex while the engine is down', async () => {
    engineDown();
    renderInShell('/playlists', <Playlists />);
    await banner();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sync All to Plex' })).toHaveProperty('disabled', true));
    await waitFor(() => expect(engineAlerts()).toHaveLength(1));
  });

  it('Library Changes does not claim "No transactions found" when the list failed', async () => {
    engineDown();
    renderInShell('/changes', <LibraryChanges />);
    await banner();
    await waitFor(() => expect(screen.getByText('Transactions unknown')).toBeTruthy());
    expect(screen.queryByText('No transactions found.')).toBeNull();
    expect(screen.queryByText('1-0')).toBeNull();
  });

  it('joins server text that already ends in a period without doubling it', () => {
    const text = sentences('Could not load the review queue: Beets engine is unavailable.', 'Click Refresh to try again.');
    expect(text).toBe('Could not load the review queue: Beets engine is unavailable. Click Refresh to try again.');
    expect(sentences('Revalidation failed: HTTP 500', 'Click Refresh.')).toBe('Revalidation failed: HTTP 500. Click Refresh.');
  });
});
