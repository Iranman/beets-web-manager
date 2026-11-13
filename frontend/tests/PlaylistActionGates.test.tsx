import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getPlaylistDetails, getPlaylistRows, getPlaylists } from '../src/api/client';
import { EngineOfflineContext } from '../src/components/EngineOfflineBanner';
import { PlaylistPipelinePanel } from '../src/features/playlists/PlaylistPipelinePanel';
import { PlaylistQualityPanel } from '../src/features/playlists/PlaylistQualityPanel';

vi.mock('../src/api/client', () => ({
  apiErrorBody: () => undefined,
  applyPlaylistTrackAction: vi.fn(),
  cleanupPlaylistQuality: vi.fn(),
  getJob: vi.fn(() => new Promise(() => undefined)),
  getPlaylistDetails: vi.fn(),
  getPlaylistDownloadStatus: vi.fn(() => new Promise(() => undefined)),
  getPlaylistRows: vi.fn(),
  getPlaylists: vi.fn(),
  placePlaylistQuality: vi.fn(),
  runPlaylistPipelineAction: vi.fn(),
  startPlaylistDownload: vi.fn(),
}));

const PLAYLIST = { name: 'Late Night Drive', tracks: 10, available: 8, missing: 2, has_m3u: true, quality_review: 1 };

// The moved actions keep their safety gates (PR #712).
describe('moved playlist actions keep their gates', () => {
  afterEach(() => { vi.clearAllMocks(); cleanup(); localStorage.clear(); });

  it('Workbench Run Pipeline is disabled while the engine is offline', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [PLAYLIST] } as never);
    vi.mocked(getPlaylistDetails).mockResolvedValue({ ok: true, name: PLAYLIST.name, available: 8, missing_count: 2 } as never);
    render(
      <EngineOfflineContext.Provider value>
        <MemoryRouter><PlaylistPipelinePanel initialPlaylist="Late Night Drive" /></MemoryRouter>
      </EngineOfflineContext.Provider>,
    );
    await waitFor(() => expect(getPlaylistDetails).toHaveBeenCalled());
    expect(screen.getByRole('button', { name: 'Run Pipeline' }).hasAttribute('disabled')).toBe(true);
  });

  it('Jobs Repair Review Rows is disabled while that playlist pipeline runs', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({
      ok: true,
      playlists: [{ ...PLAYLIST, last_pipeline: { status: 'running' } }],
    } as never);
    vi.mocked(getPlaylistRows).mockResolvedValue({ ok: true, rows: [{ id: 42, artist: 'A', title: 'Song', quality: 'review' }], has_more: false } as never);
    render(<MemoryRouter><PlaylistQualityPanel initialPlaylist="Late Night Drive" /></MemoryRouter>);
    const repair = await screen.findByRole('button', { name: 'Repair Review Rows (1)' });
    expect(repair.hasAttribute('disabled')).toBe(true);
  });
});
