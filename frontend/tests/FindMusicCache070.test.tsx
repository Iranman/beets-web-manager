// 0.7.0 Find Music: the list is a cached server snapshot (PR #385), paged 100 at a time.
// Covers each cache state, paging, the POST refresh, polling while the list rebuilds,
// Download All's 409 queue_not_ready, the server's fallback chain, and the kept confirmations.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getAcquisitionQueue: vi.fn(),
    refreshAcquisitionQueue: vi.fn(),
    getAcquisitionDownloadAllActive: vi.fn(),
    getYtdlpStatus: vi.fn(),
    startAlbumDownload: vi.fn(),
    startAcquisitionDownloadAll: vi.fn(),
    runLidarrAlbumSearch: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import type { AcquisitionQueueCache, AcquisitionQueueResponse } from '../src/api/types';
import { AcquisitionPanel, PAGE_SIZE, REFRESH_POLL_MS } from '../src/features/acquisition/AcquisitionPanel';

const mocked = vi.mocked(client);
const MBID = '11111111-2222-3333-4444-555555555555';

function row(n: number) {
  return {
    key: `k${n}`, artist: `Artist ${n}`, album: `Album ${n}`, year: '2001', mbid: MBID,
    sources: ['lidarr'], sort_key: `a${n}`, actions: { can_download: true },
    wanted: { monitored: true, mb_albumid: MBID }, local: null,
  };
}

function cache(overrides: Partial<AcquisitionQueueCache> = {}): AcquisitionQueueCache {
  return {
    state: 'ready', built_at: 1_700_000_000, age_seconds: 720, build_seconds: 4, max_age_seconds: 21600,
    job_id: null, refreshing: false, last_error: '', last_error_at: null, include_unmonitored: false,
    ...overrides,
  };
}

function response(overrides: Partial<AcquisitionQueueResponse> = {}): AcquisitionQueueResponse {
  return {
    ok: true,
    items: [row(1)],
    total: 1,
    offset: 0,
    limit: PAGE_SIZE,
    counts: { all: 1, beets: 0, lidarr: 1, merged: 0, unmonitored: 0 },
    album_types: { album: 1 },
    cache: cache(),
    download_sources: {
      slskd_configured: true, slskd_error: '', fallback_chain: ['ytdlp', 'soundcloud'],
      fallbacks: { ytdlp: { available: true, reason: '' }, soundcloud: { available: true, reason: '' }, spotiflac: { available: false, reason: 'not installed' } },
    },
    library_error: '',
    wanted_error: '',
    ...overrides,
  } as AcquisitionQueueResponse;
}

const renderPanel = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><AcquisitionPanel /></MemoryRouter>
  </QueryClientProvider>,
);

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)));
  mocked.getAcquisitionQueue.mockResolvedValue(response());
  mocked.refreshAcquisitionQueue.mockResolvedValue({ ok: true, job_id: 'q1', already_running: false } as never);
  mocked.getAcquisitionDownloadAllActive.mockResolvedValue({ active: false } as never);
  mocked.getYtdlpStatus.mockResolvedValue({ ok: true, ready: true, enabled: true, cookie_file: '', cookie_candidates: [], message: '' } as never);
  mocked.startAlbumDownload.mockResolvedValue({ ok: true, job_id: 'j1' } as never);
  mocked.startAcquisitionDownloadAll.mockResolvedValue({ ok: true, job_id: 'b1' } as never);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Find Music cache states', () => {
  it('ready: shows the list age and the rows', async () => {
    renderPanel();
    expect(await screen.findByText('List updated 12 min ago.')).toBeTruthy();
    expect(screen.getByText(/Artist 1 - Album 1/)).toBeTruthy();
    expect(mocked.getAcquisitionQueue).toHaveBeenCalledWith(expect.objectContaining({ offset: 0, limit: 100 }));
  });

  it('building: says it is building and never shows an empty list as complete', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({
      items: [], total: 0, counts: { all: 0, beets: 0, lidarr: 0, merged: 0 },
      cache: cache({ state: 'building', built_at: null, age_seconds: null, refreshing: true, job_id: 'q1' }),
    }));
    renderPanel();
    const status = await screen.findByRole('status');
    await waitFor(() => expect(status.textContent).toMatch(/Building the Find Music list/));
    expect(screen.queryByText(/need download/)).toBeNull();
    expect(screen.queryByRole('button', { name: /Download next/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Updating...' }).hasAttribute('disabled')).toBe(true);
  });

  it('empty: asks for a refresh, no success message', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({
      items: [], total: 0, cache: cache({ state: 'empty', built_at: null, age_seconds: null }),
    }));
    renderPanel();
    expect(await screen.findByText(/has not been built yet/)).toBeTruthy();
    expect(screen.queryByText(/need download/)).toBeNull();
  });

  it('failed with no earlier list: shows the error and nothing as complete', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({
      items: [], total: 0,
      cache: cache({ state: 'failed', built_at: null, age_seconds: null, last_error: 'Lidarr timed out', last_error_at: Date.now() / 1000 - 120 }),
    }));
    renderPanel();
    expect(await screen.findByText(/could not be built \(2 min ago\): Lidarr timed out/)).toBeTruthy();
    expect(screen.queryByText(/need download/)).toBeNull();
  });

  it('failed with an earlier list: keeps the old list, clearly marked', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({
      cache: cache({ state: 'failed', age_seconds: 7200, last_error: 'Lidarr timed out', last_error_at: Date.now() / 1000 - 60 }),
    }));
    renderPanel();
    expect(await screen.findByText(/last list update failed \(1 min ago\): Lidarr timed out\. The list below is the previous one, built 2 h ago/)).toBeTruthy();
    expect(screen.getByText(/Artist 1 - Album 1/)).toBeTruthy();
  });

  it('stale and refreshing: old list plus an updating note', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({
      cache: cache({ state: 'stale', age_seconds: 3 * 86400, refreshing: true, job_id: 'q1' }),
    }));
    renderPanel();
    expect(await screen.findByText(/built 3 d ago and may be out of date\. Updating in the background/)).toBeTruthy();
    expect(screen.getByText(/Artist 1 - Album 1/)).toBeTruthy();
  });

  it('ready with no rows and no filters: says nothing needs download', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({ items: [], total: 0 }));
    renderPanel();
    expect(await screen.findByText(/need download/)).toBeTruthy();
  });
});

describe('Find Music paging and filters', () => {
  it('pages 100 at a time with the server total', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({ total: 250 }));
    renderPanel();
    expect(await screen.findByText('Showing 1-1 of 250')).toBeTruthy();
    expect(screen.getByText('Page 1 of 3')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Previous page' }).hasAttribute('disabled')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenCalledWith(expect.objectContaining({ offset: 100, limit: 100 })));
  });

  it('source, album type and sort go to the server and reset to page 1', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({ total: 250, album_types: { album: 200, ep: 50 } }));
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenCalledWith(expect.objectContaining({ offset: 100 })));

    fireEvent.click(screen.getByRole('button', { name: 'In both' }));
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0, source: 'merged' })));
    fireEvent.change(screen.getByLabelText('Album type'), { target: { value: 'ep' } });
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenLastCalledWith(expect.objectContaining({ album_type: ['ep'] })));
    fireEvent.change(screen.getByLabelText('Sort'), { target: { value: 'year_desc' } });
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenLastCalledWith(expect.objectContaining({ sort: 'year_desc' })));
  });

  it('QA #390: search is debounced into one server request and resets to page 1', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({ total: 250 }));
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 100 })));
    const before = mocked.getAcquisitionQueue.mock.calls.length;

    const input = screen.getByLabelText('Search');
    fireEvent.change(input, { target: { value: 'ab' } });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 100)); });
    expect(mocked.getAcquisitionQueue.mock.calls.length).toBe(before); // nothing inside the 300 ms debounce
    fireEvent.change(input, { target: { value: 'abba ' } });
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenLastCalledWith(expect.objectContaining({ search: 'abba', offset: 0 })));
    const searchCalls = mocked.getAcquisitionQueue.mock.calls.slice(before).filter(([opts]) => opts?.search);
    expect(searchCalls.map(([opts]) => opts?.search)).toEqual(['abba']);
  });
});

describe('Find Music refresh', () => {
  it('Refresh list POSTs a rebuild, then polls limit=0 until done and reloads the page', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocked.getAcquisitionQueue
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(response({ cache: cache({ refreshing: true, job_id: 'q1' }) }))
      .mockResolvedValueOnce(response({ items: [], cache: cache({ refreshing: true, job_id: 'q1' }) }))
      .mockResolvedValueOnce(response({ items: [], cache: cache({ refreshing: false, age_seconds: 1 }) }))
      .mockResolvedValue(response({ items: [row(2)], cache: cache({ age_seconds: 2 }) }));
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh list' }));
    await waitFor(() => expect(mocked.refreshAcquisitionQueue).toHaveBeenCalledWith(false, false));
    expect(await screen.findByText(/Updating in the background/)).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_POLL_MS); });
    await act(async () => { await vi.advanceTimersByTimeAsync(REFRESH_POLL_MS); });
    expect(mocked.getAcquisitionQueue).toHaveBeenCalledWith({ limit: 0, include_unmonitored: false });
    expect(await screen.findByText(/Artist 2 - Album 2/)).toBeTruthy();
    expect(screen.getByText('List updated just now.')).toBeTruthy();
  });

  it('a failed refresh says the list is unchanged', async () => {
    mocked.refreshAcquisitionQueue.mockRejectedValue(new Error('HTTP 500'));
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh list' }));
    expect(await screen.findByText(/refresh could not be started: HTTP 500\. The list shown is unchanged/)).toBeTruthy();
  });

  it('the client sends POST /queue/refresh and never GET ?refresh=1', async () => {
    const actual = await vi.importActual<typeof import('../src/api/client')>('../src/api/client');
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true, job_id: 'q1', already_running: false, items: [], total: 0, counts: {} }), { status: 202 }));
    vi.stubGlobal('fetch', fetchMock);
    await actual.refreshAcquisitionQueue(true);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/acquisition/queue/refresh');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ include_unmonitored: true });

    await actual.getAcquisitionQueue({ offset: 100, limit: 100, search: ' x ', source: 'beets', album_type: ['ep', 'single'], sort: 'year_desc', include_unmonitored: true });
    const getUrl = String((fetchMock.mock.calls[1] as unknown as [string])[0]);
    expect(getUrl).not.toMatch(/refresh/);
    expect(Object.fromEntries(new URL(getUrl, 'http://x').searchParams)).toEqual({
      offset: '100', limit: '100', search: 'x', source: 'beets', album_type: 'ep,single', sort: 'year_desc', include_unmonitored: '1',
    });
  });
});

describe('Find Music downloads', () => {
  it('Download All 409 queue_not_ready says the list is still building', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const err = Object.assign(new Error('The Find Music list is still being built'), {
      httpStatus: 409, body: { ok: false, code: 'queue_not_ready', job_id: 'q1' },
    });
    mocked.startAcquisitionDownloadAll.mockRejectedValue(err);
    renderPanel();
    fireEvent.click(await screen.findByRole('button', { name: 'Download next 1' }));
    expect(await screen.findByText(/Nothing was started: the Find Music list is still building/)).toBeTruthy();
  });

  it('renders the server fallback chain, not a hardcoded SpotiFLAC chain', async () => {
    renderPanel();
    expect(await screen.findByText('Fallback: YouTube -> SoundCloud')).toBeTruthy();
    expect(screen.queryByText(/SpotiFLAC -> YouTube/)).toBeNull();
  });

  it('marks unavailable fallback sources and shows the slskd error', async () => {
    mocked.getAcquisitionQueue.mockResolvedValue(response({
      download_sources: {
        slskd_configured: false, slskd_error: 'slskd is not configured (SLSKD_API_KEY)', fallback_chain: ['ytdlp'],
        fallbacks: { ytdlp: { available: false, reason: 'yt-dlp is not installed' } },
      },
    }));
    renderPanel();
    expect(await screen.findByText('Fallback: YouTube (unavailable)')).toBeTruthy();
    expect(screen.getByText(/SLSKD downloads cannot start: slskd is not configured \(SLSKD_API_KEY\)/)).toBeTruthy();
  });

  it('per-row download still confirms, and never sends fallback_method spotiflac', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPanel();
    const button = await screen.findByRole('button', { name: 'SLSKD + sources' });
    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/imported into the library/));
    expect(mocked.startAlbumDownload).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.startAlbumDownload).toHaveBeenCalledTimes(1));
    const payload = mocked.startAlbumDownload.mock.calls[0][0];
    expect(payload).toMatchObject({ auto_import: true, try_source_fallback: true });
    expect(payload.fallback_method).toBeUndefined();
  });

  it('QA #390 F1: Download next N sends exactly the rows shown (year_desc, missing first off, N < page)', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    // Server order for year_desc differs from sort_key order: k3 (2020), k1 (2010), k2 (2000).
    const shown = [
      { ...row(3), year: '2020', sort_key: 'c' },
      { ...row(1), year: '2010', sort_key: 'a' },
      { ...row(2), year: '2000', sort_key: 'b' },
    ];
    mocked.getAcquisitionQueue.mockResolvedValue(response({ items: shown, total: 3 }));
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Sort'), { target: { value: 'year_desc' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Missing first' }));
    fireEvent.change(screen.getByLabelText('Limit'), { target: { value: '2' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Download next 2' }));
    await waitFor(() => expect(mocked.startAcquisitionDownloadAll).toHaveBeenCalledTimes(1));
    expect(mocked.startAcquisitionDownloadAll.mock.calls[0][0]).toMatchObject({ keys: ['k3', 'k1'], limit: 2 });
  });

  it('QA N3: Retry failed covers failed rows on every page of the current filter', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const failure = (key: string) => ({ key, artist: 'A', album: key, error: 'no result' });
    mocked.getAcquisitionDownloadAllActive.mockResolvedValue({
      active: false,
      last_job: { job_id: 'b0', status: 'success', result: { total: 3, success: 0, failed: 3, failures: [failure('k1'), failure('k150'), failure('k999')] } },
    } as never);
    const all = Array.from({ length: 250 }, (_, i) => row(i + 1));
    mocked.getAcquisitionQueue.mockImplementation(async (opts = {}) => {
      const offset = opts.offset ?? 0;
      const limit = opts.limit ?? PAGE_SIZE;
      // The page shown holds one row; only the 500-row scan sees k150 on page 2.
      return response({ items: limit === PAGE_SIZE ? [row(1)] : all.slice(offset, offset + limit), total: all.length, offset, limit });
    });
    renderPanel();
    const retry = await screen.findByRole('button', { name: 'Retry 2 failed' }); // k999 is not in this filter
    fireEvent.click(retry);
    await waitFor(() => expect(mocked.startAcquisitionDownloadAll).toHaveBeenCalledTimes(1));
    expect(mocked.startAcquisitionDownloadAll.mock.calls[0][0]).toMatchObject({ keys: ['k1', 'k150'], limit: 2 });
  });

  it('Download next N still confirms; Cancel sends nothing', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPanel();
    const button = await screen.findByRole('button', { name: 'Download next 1' });
    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/imported into the library/));
    expect(mocked.startAcquisitionDownloadAll).not.toHaveBeenCalled();
  });
});
