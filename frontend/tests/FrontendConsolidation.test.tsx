// QA follow-ups from PR #507, and the helpers the frontend consolidation added.
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getAcquisitionQueue: vi.fn(),
    getAcquisitionDownloadAllActive: vi.fn(),
    getYtdlpStatus: vi.fn(),
    startAlbumDownload: vi.fn(),
  };
});

import * as client from '../src/api/client';
import { AcquisitionPanel } from '../src/features/acquisition/AcquisitionPanel';
import { classifyHttpFailure, fetchFailure } from '../src/lib/api';
import { errorMessage } from '../src/lib/errors';
import { formatScanTime, normaliseEpochSeconds } from '../src/lib/dates';
import Jobs from '../src/views/Jobs';
import { albumPrimaryAction } from '../src/views/Library';
import type { LibraryAlbum } from '../src/types/api';

const mocked = vi.mocked(client);
const RGID = '11111111-2222-3333-4444-555555555555';
const RELEASE_ID = '99999999-8888-7777-6666-555555555555';
const RG_URL = `https://musicbrainz.org/release-group/${RGID}`;

const withProviders = (ui: React.ReactNode, path = '/') => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
  </QueryClientProvider>,
);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Find Music: Lidarr release-group rows (QA M1)', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)));
    mocked.getAcquisitionDownloadAllActive.mockResolvedValue({ active: false } as never);
    mocked.getYtdlpStatus.mockResolvedValue({ ok: true, ready: false, enabled: false, cookie_file: '', cookie_candidates: [], message: '' } as never);
    mocked.startAlbumDownload.mockResolvedValue({ ok: true, job_id: 'j1' } as never);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  it.each([
    ['no local album', null],
    ['a local album holding a release ID', { album_id: 5, mb_albumid: RELEASE_ID, track_count: 10 }],
  ])('sends the release-group reference with %s', async (_label, local) => {
    mocked.getAcquisitionQueue.mockResolvedValue({
      ok: true,
      counts: { beets: 0, lidarr: 1, merged: 1 },
      items: [{
        key: 'k1', artist: 'Some Artist', album: 'Some Album', year: '2001', mbid: RG_URL,
        sources: ['lidarr'], sort_key: 'a', actions: { can_download: true },
        wanted: { monitored: true, mb_albumid: '', mb_releasegroupid: RGID }, local,
      }],
    } as never);
    withProviders(<AcquisitionPanel />);

    fireEvent.click(await screen.findByRole('button', { name: 'SLSKD' }));

    await waitFor(() => expect(mocked.startAlbumDownload).toHaveBeenCalledTimes(1));
    expect(mocked.startAlbumDownload.mock.calls[0][0].mb_albumid).toBe(RG_URL);
  });
});

describe('Old Clean links reach their Jobs panel (QA L1)', () => {
  const album = (fields: Partial<LibraryAlbum>) => ({
    id: 1, album: 'A', albumartist: 'B', track_count: 1, tracks: [{ id: 1, ok: true, missing: false, mb_trackid: RGID }], mb_albumid: RELEASE_ID, ...fields,
  }) as LibraryAlbum;

  function stubJobsApi() {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(
      JSON.stringify(String(input).endsWith('/api/jobs') ? { ok: true, jobs: [] } : { ok: true, groups: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))));
  }
  const detailsOf = (name: string) => screen.getByText(name).closest('details') as HTMLDetailsElement;

  it('Library actions link straight to the panel', () => {
    expect(albumPrimaryAction(album({ mb_duplicate_recording_id_count: 2 }), new Map())?.path).toBe('/jobs?tab=library-db');
    expect(albumPrimaryAction(album({ not_imported: 1, not_imported_is_extra: true }), new Map())?.path).toBe('/jobs?tab=album-tracks');
  });

  it('?tab=album-tracks opens Advanced Maintenance with the Album Track Repair panel', async () => {
    stubJobsApi();
    withProviders(<Jobs />, '/jobs?tab=album-tracks');
    expect(await screen.findByText('Album Track Repair')).toBeTruthy();
    expect(detailsOf('Advanced Maintenance').open).toBe(true);
    expect(detailsOf('Database Health').open).toBe(false);
  });

  it('?tab=library-db opens Database Health only', async () => {
    stubJobsApi();
    withProviders(<Jobs />, '/jobs?tab=library-db');
    await screen.findByText('Database Health');
    expect(detailsOf('Database Health').open).toBe(true);
    expect(detailsOf('Advanced Maintenance').open).toBe(false);
  });

  it('plain /jobs leaves both closed', async () => {
    stubJobsApi();
    withProviders(<Jobs />, '/jobs');
    await screen.findByText('Database Health');
    expect(detailsOf('Database Health').open).toBe(false);
    expect(detailsOf('Advanced Maintenance').open).toBe(false);
  });
});

describe('shared helpers', () => {
  it('errorMessage keeps the message, then the fallback, then the string form', () => {
    expect(errorMessage(new Error('boom'), 'fallback')).toBe('boom');
    expect(errorMessage('text', 'fallback')).toBe('fallback');
    expect(errorMessage('text')).toBe('text');
  });

  it('normaliseEpochSeconds accepts seconds and milliseconds, and rejects junk', () => {
    expect(normaliseEpochSeconds(1_700_000_000)).toBe(1_700_000_000);
    expect(normaliseEpochSeconds(1_700_000_000_000)).toBe(1_700_000_000);
    expect(normaliseEpochSeconds('nope')).toBeNull();
    expect(normaliseEpochSeconds(0)).toBeNull();
  });

  it('formatScanTime names the never-scanned state', () => {
    expect(formatScanTime(null)).toBe('Not scanned this session');
  });

  it('fetchFailure separates timeouts from unreachable', () => {
    expect(fetchFailure(Object.assign(new Error('x'), { name: 'AbortError' })).isTimeout).toBe(true);
    expect(fetchFailure(new TypeError('Failed to fetch'))).toEqual({ message: 'Could not reach Beets Web Manager.', isTimeout: false });
  });

  it('classifyHttpFailure keeps engine, auth and provider outages apart', () => {
    expect(classifyHttpFailure(503, '', false).isEngineOffline).toBe(true);
    expect(classifyHttpFailure(503, '', true).isEngineOffline).toBe(false);
    expect(classifyHttpFailure(401, '', false).isAuthError).toBe(true);
    expect(classifyHttpFailure(401, 'ENGINE_AUTH_FAILED', false)).toEqual({ isEngineAuthError: true, isAuthError: false, isEngineOffline: false });
  });
});
