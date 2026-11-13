import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  applyPlaylistTrackAction,
  cleanupPlaylistQuality,
  getPlaylistDetails,
  getPlaylistRows,
  getPlaylists,
  getPlaylistSyncStatus,
  runPlaylistPipelineAction,
} from '../src/api/client';
import { PlaylistPipelinePanel } from '../src/features/playlists/PlaylistPipelinePanel';
import { PlaylistQualityPanel } from '../src/features/playlists/PlaylistQualityPanel';
import Playlists from '../src/views/Playlists';

const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');

vi.mock('../src/api/client', async (importActual) => {
  const pending = () => vi.fn(() => new Promise(() => undefined));
  return {
    apiErrorBody: (await importActual<typeof import('../src/api/client')>()).apiErrorBody,
    applySafePlaylistSuggestions: pending(),
    applyPlaylistTrackAction: vi.fn(),
    cleanupPlaylistQuality: vi.fn(),
    createPlaylist: pending(),
    deletePlaylist: pending(),
    getJob: pending(),
    getPlaylistDetails: vi.fn(),
    getPlaylistDownloadStatus: pending(),
    getPlaylistRows: vi.fn(),
    getPlaylistSyncStatus: vi.fn(),
    getPlaylists: vi.fn(),
    getPlaylistSuggestions: pending(),
    parsePlaylist: pending(),
    placePlaylistQuality: vi.fn(),
    resolvePlaylistTrack: pending(),
    runPlaylistPipelineAction: vi.fn(),
    startPlaylistDownload: vi.fn(),
    syncPlaylists: pending(),
  };
});

const PLAYLIST = { name: 'Late Night Drive', tracks: 10, available: 8, missing: 2, has_m3u: true };

// Owner decision 2026-11-12: "playlist is for creation and syncing". Downloads, imports and
// pipeline runs live on the Workbench; review-row repair and placement live on Jobs & Cleanup.
describe('playlist actions have one home', () => {
  afterEach(() => { vi.clearAllMocks(); vi.restoreAllMocks(); cleanup(); localStorage.clear(); });

  it('Playlists keeps creation and sync only; the panels hold the moved actions', () => {
    const playlists = src('views/Playlists.tsx');
    for (const gone of [
      'startPlaylistDownload',
      'cleanupPlaylistQuality',
      'placePlaylistQuality',
      "'Run Pipeline'",
      "'Resume Pipeline'",
      'Repair Review Rows',
      'Queue Placement',
      "'retry_import'",
      "'retry_download'",
      "'delete_staged'",
      'Download Missing & Sync',
      "'Clear Checkpoint'",
    ]) {
      expect(playlists, gone).not.toContain(gone);
    }
    const pipeline = src('features/playlists/PlaylistPipelinePanel.tsx');
    expect(pipeline).toContain('startPlaylistDownload(');
    expect(pipeline).toContain("'Resume Pipeline' : 'Run Pipeline'");
    expect(pipeline).toContain("handleTrackAction('delete_staged', track)");
    const quality = src('features/playlists/PlaylistQualityPanel.tsx');
    expect(quality).toContain('cleanupPlaylistQuality(');
    expect(quality).toContain('placePlaylistQuality(');
    expect(src('views/Import.tsx')).toContain('<PlaylistPipelinePanel');
    expect(src('views/Import.tsx')).not.toContain('PlaylistQualityPanel');
    expect(src('views/Jobs.tsx')).toContain('<PlaylistQualityPanel');
    expect(src('views/Jobs.tsx')).not.toContain('PlaylistPipelinePanel');
  });

  it('Playlists shows no pipeline buttons and links a playlist that needs work to the Workbench', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [PLAYLIST] } as never);
    vi.mocked(getPlaylistSyncStatus).mockResolvedValue({ ok: true, enabled: false, interval: 0, running: false });
    render(<MemoryRouter><Playlists /></MemoryRouter>);
    await screen.findByText('Late Night Drive');
    expect(screen.queryByRole('button', { name: /Run Pipeline|Resume Pipeline|Download Missing|Repair Review Rows/ })).toBeNull();
    const link = screen.getByRole('link', { name: 'Open in Workbench' });
    expect(link.getAttribute('href')).toBe('/import?tab=playlists&playlist=Late%20Night%20Drive');
    // The Sync to Plex band holds Sync All only, no row-action notices.
    const sync = screen.getByRole('region', { name: 'Sync to Plex' });
    expect(within(sync).queryByRole('alert')).toBeNull();
  });

  it('Workbench Run Pipeline starts the same run-full action for the picked playlist', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [PLAYLIST] } as never);
    vi.mocked(getPlaylistDetails).mockResolvedValue({ ok: true, name: PLAYLIST.name, available: 8, missing_count: 2 } as never);
    vi.mocked(runPlaylistPipelineAction).mockResolvedValue({ ok: true } as never);
    render(<MemoryRouter><PlaylistPipelinePanel initialPlaylist="Late Night Drive" /></MemoryRouter>);
    const run = await screen.findByRole('button', { name: 'Run Pipeline' });
    await waitFor(() => expect(run.hasAttribute('disabled')).toBe(false));
    fireEvent.click(run);
    await waitFor(() => expect(runPlaylistPipelineAction).toHaveBeenCalledWith('Late Night Drive', 'run-full'));
  });

  it('Workbench Run Pipeline is disabled until a playlist is picked', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [] } as never);
    render(<MemoryRouter><PlaylistPipelinePanel /></MemoryRouter>);
    await screen.findByText('No saved playlists yet');
    expect(screen.getByRole('button', { name: 'Run Pipeline' }).hasAttribute('disabled')).toBe(true);
  });

  it('Workbench Delete Staged Download keeps its confirmation', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [PLAYLIST] } as never);
    vi.mocked(getPlaylistDetails).mockResolvedValue({ ok: true, name: PLAYLIST.name } as never);
    vi.mocked(getPlaylistRows).mockResolvedValue({
      ok: true,
      rows: [{ artist: 'A', title: 'Song', pipeline_status: 'downloaded', staged_path: 'staged/song.flac' }],
      has_more: false,
    } as never);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<MemoryRouter><PlaylistPipelinePanel initialPlaylist="Late Night Drive" /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Waiting Import' }));
    fireEvent.click(screen.getByRole('button', { name: 'Load Rows' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Actions' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete Staged Download' }));
    expect(confirm).toHaveBeenCalledWith('Delete this downloaded staging file? The Beets music library copy will not be deleted.');
    expect(applyPlaylistTrackAction).not.toHaveBeenCalled();
  });

  it('Jobs review-row repair asks first and sends the same repair payload', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({
      ok: true,
      playlists: [{ ...PLAYLIST, quality_review: 1 }],
    } as never);
    vi.mocked(getPlaylistRows).mockResolvedValue({
      ok: true,
      rows: [{ id: 42, artist: 'A', title: 'Song', quality: 'review' }],
      has_more: false,
    } as never);
    vi.mocked(cleanupPlaylistQuality).mockResolvedValue({ ok: true, queued: true, job_id: 'job-1' } as never);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<MemoryRouter><PlaylistQualityPanel initialPlaylist="Late Night Drive" /></MemoryRouter>);
    const repair = await screen.findByRole('button', { name: 'Repair Review Rows (1)' });
    fireEvent.click(repair);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(cleanupPlaylistQuality).not.toHaveBeenCalled();
    fireEvent.click(repair);
    await waitFor(() => expect(cleanupPlaylistQuality).toHaveBeenCalledWith({
      dry_run: false,
      action: 'repair',
      filter: 'repair',
      item_ids: [42],
      limit: 50,
    }));
    expect(await screen.findByText(/Queued playlist quality repair for 1 row\(s\)/)).toBeTruthy();
  });
});
