import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Playlists from '../src/views/Playlists';
import { getJob, getPlaylists, getPlaylistSyncStatus, syncPlaylists } from '../src/api/client';

vi.mock('../src/api/client', async (importActual) => {
  const pending = () => vi.fn(() => new Promise(() => undefined));
  return {
    apiErrorBody: (await importActual<typeof import('../src/api/client')>()).apiErrorBody,
    applySafePlaylistSuggestions: pending(),
    cleanupPlaylistQuality: pending(),
    createPlaylist: pending(),
    deletePlaylist: pending(),
    applyPlaylistTrackAction: pending(),
    getJob: vi.fn(),
    getPlaylistDetails: pending(),
    getPlaylistDownloadStatus: pending(),
    getPlaylistRows: pending(),
    getPlaylistSyncStatus: vi.fn(),
    getPlaylists: vi.fn(),
    getPlaylistSuggestions: pending(),
    placePlaylistQuality: pending(),
    parsePlaylist: pending(),
    resolvePlaylistTrack: pending(),
    runPlaylistPipelineAction: pending(),
    startPlaylistDownload: pending(),
    syncPlaylists: vi.fn(),
  };
});

const idle = { ok: true, enabled: false, interval: 0, running: false };

describe('Playlists sync all (#343)', () => {
  afterEach(() => { vi.clearAllMocks(); cleanup(); });

  it('follows an already-running sync and reports a failed job as failed', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({
      ok: true, playlists: [{ name: 'P1', tracks: 1, missing: 0, has_m3u: true, sync_status: 'failed', last_sync_status: 'synced' }],
    } as never);
    vi.mocked(getPlaylistSyncStatus).mockResolvedValue(idle);
    vi.mocked(syncPlaylists).mockResolvedValue({ ok: true, job_id: 'sync-1', already_running: true });
    vi.mocked(getJob).mockResolvedValue({ ok: true, status: 'failed', result: { error: '1 playlist(s) failed to sync' } });

    render(<MemoryRouter><Playlists /></MemoryRouter>);
    expect((await screen.findAllByText('Plex Sync Failed')).length).toBeGreaterThan(0);
    fireEvent.click(await screen.findByRole('button', { name: 'Sync All to Plex' }));
    await waitFor(() => expect(getJob).toHaveBeenCalledWith('sync-1'));
    expect(await screen.findByText(/Playlist sync failed: 1 playlist\(s\) failed to sync/)).toBeTruthy();
  });

  it('links the last sync job to the Jobs page (#356)', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [] } as never);
    vi.mocked(getPlaylistSyncStatus).mockResolvedValue({ ...idle, last_run: 1, last_job_id: 'sync 7' });
    render(<MemoryRouter><Playlists /></MemoryRouter>);
    const link = await screen.findByRole('link', { name: 'View last sync job' });
    expect(link.getAttribute('href')).toBe('/jobs?job=sync%207');
  });

  it('shows no job link when the status has no last_job_id', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [] } as never);
    vi.mocked(getPlaylistSyncStatus).mockResolvedValue(idle);
    render(<MemoryRouter><Playlists /></MemoryRouter>);
    expect(await screen.findByText(/Auto off/)).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'View last sync job' })).toBeNull();
  });
});