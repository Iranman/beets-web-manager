// 0.5.1 QA: Find Music downloads run with auto_import, so they end in a Beets import.
// A per-row download and Download All must ask first; Cancel sends nothing.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    startAcquisitionDownloadAll: vi.fn(),
    runLidarrAlbumSearch: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { AcquisitionPanel } from '../src/features/acquisition/AcquisitionPanel';

const mocked = vi.mocked(client);
const renderPanel = () => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><AcquisitionPanel /></MemoryRouter>
  </QueryClientProvider>,
);
const MBID = '11111111-2222-3333-4444-555555555555';

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)));
  mocked.getAcquisitionQueue.mockResolvedValue({
    ok: true,
    counts: { beets: 0, lidarr: 1, merged: 1 },
    items: [{
      key: 'k1', artist: 'Some Artist', album: 'Some Album', year: '2001', mbid: MBID,
      sources: ['lidarr'], sort_key: 'a', actions: { can_download: true },
      wanted: { monitored: true, mb_albumid: MBID }, local: null,
    }],
  } as never);
  mocked.getAcquisitionDownloadAllActive.mockResolvedValue({ active: false } as never);
  mocked.getYtdlpStatus.mockResolvedValue({ ok: true, ready: false, enabled: false, cookie_file: '', cookie_candidates: [], message: '' } as never);
  mocked.startAlbumDownload.mockResolvedValue({ ok: true, job_id: 'j1' } as never);
  mocked.startAcquisitionDownloadAll.mockResolvedValue({ ok: true, job_id: 'b1' } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Find Music: downloads that auto-import confirm first', () => {
  it('per-row download: nothing on Cancel, auto_import download after Confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPanel();
    const button = await screen.findByRole('button', { name: 'SLSKD' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/imported into the library/));
    expect(mocked.startAlbumDownload).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.startAlbumDownload).toHaveBeenCalledTimes(1));
    expect(mocked.startAlbumDownload.mock.calls[0][0]).toMatchObject({ auto_import: true, mb_albumid: MBID });
  });

  it('Download All: confirmation says items are imported; nothing on Cancel', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPanel();
    const button = await screen.findByRole('button', { name: 'Download next 1' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/imported into the library/));
    expect(mocked.startAcquisitionDownloadAll).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.startAcquisitionDownloadAll).toHaveBeenCalledTimes(1));
  });
});
