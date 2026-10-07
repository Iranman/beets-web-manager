import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Playlists from '../src/views/Playlists';
import { getPlaylistDetails, getPlaylists, getPlaylistSuggestions } from '../src/api/client';
import { MUSICBRAINZ_UNAVAILABLE_TEXT } from '../src/components/MusicBrainzUnavailableNotice';

vi.mock('../src/api/client', () => {
  const pending = () => vi.fn(() => new Promise(() => undefined));
  return {
    applySafePlaylistSuggestions: pending(),
    cleanupPlaylistQuality: pending(),
    createPlaylist: pending(),
    deletePlaylist: pending(),
    applyPlaylistTrackAction: pending(),
    getJob: pending(),
    getPlaylistDetails: vi.fn(),
    getPlaylistDownloadStatus: pending(),
    getPlaylistRows: pending(),
    getPlaylistSyncStatus: pending(),
    getPlaylists: vi.fn(),
    getPlaylistSuggestions: vi.fn(),
    placePlaylistQuality: pending(),
    parsePlaylist: pending(),
    resolvePlaylistTrack: pending(),
    runPlaylistPipelineAction: pending(),
    startPlaylistDownload: pending(),
  };
});

const missingTrack = { artist: 'Artist', title: 'Song', status: 'missing' };

describe('Playlists MusicBrainz outage notice (#252 NF-4, QA-3/QA-4)', () => {
  afterEach(() => { vi.clearAllMocks(); cleanup(); });

  it('shows the notice after suggestions and clears it when a playlist is viewed again', async () => {
    vi.mocked(getPlaylists).mockResolvedValue({ ok: true, playlists: [{ name: 'P1', tracks: 1, missing: 1, has_m3u: true }] } as never);
    vi.mocked(getPlaylistDetails).mockResolvedValue({
      ok: true, name: 'P1', m3u: '/playlists/P1.m3u', total: 1, matched: [], missing: [missingTrack], tracks_loaded: true,
    } as never);
    vi.mocked(getPlaylistSuggestions).mockResolvedValue({
      ok: true, name: 'P1', total_missing: 1, safe_count: 0, rows: [], musicbrainz_unavailable: true,
    });

    render(<MemoryRouter><Playlists /></MemoryRouter>);
    const view = async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Actions' }));
      fireEvent.click(await screen.findByRole('menuitem', { name: 'View Tracks' }));
      await waitFor(() => expect(getPlaylistDetails).toHaveBeenCalled());
    };
    await view();
    fireEvent.click(await screen.findByRole('button', { name: 'Review Fixes' }));
    const notice = await screen.findByText(MUSICBRAINZ_UNAVAILABLE_TEXT);
    expect(notice.closest('[role="status"]')).toBeTruthy();

    vi.mocked(getPlaylistDetails).mockClear();
    await view();
    await waitFor(() => expect(screen.queryByText(MUSICBRAINZ_UNAVAILABLE_TEXT)).toBeNull());
  });
});
