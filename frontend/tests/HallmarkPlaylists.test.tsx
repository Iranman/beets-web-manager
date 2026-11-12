import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Playlists from '../src/views/Playlists';
import { getPlaylists, getPlaylistSyncStatus } from '../src/api/client';

// Hallmark Playlists redesign: made around making a playlist and syncing it, with mixtape sleeves
// that stay decorative and filters that announce their state.
vi.mock('../src/api/client', async (importActual) => {
  const pending = () => vi.fn(() => new Promise(() => undefined));
  return {
    apiErrorBody: (await importActual<typeof import('../src/api/client')>()).apiErrorBody,
    applySafePlaylistSuggestions: pending(),
    cleanupPlaylistQuality: pending(),
    createPlaylist: pending(),
    deletePlaylist: pending(),
    applyPlaylistTrackAction: pending(),
    getJob: pending(),
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
    syncPlaylists: pending(),
  };
});

const styles = readFileSync(join(__dirname, '../src/styles.css'), 'utf8');

async function renderPlaylists() {
  vi.mocked(getPlaylists).mockResolvedValue({
    ok: true,
    playlists: [
      { name: 'Late Night Drive', tracks: 10, available: 8, missing: 2, has_m3u: true },
      { name: 'Sunday Records', tracks: 4, available: 4, missing: 0, has_m3u: true },
    ],
  } as never);
  vi.mocked(getPlaylistSyncStatus).mockResolvedValue({ ok: true, enabled: false, interval: 0, running: false });
  render(<MemoryRouter><Playlists /></MemoryRouter>);
  await screen.findByText('Late Night Drive');
}

describe('Hallmark Playlists', () => {
  afterEach(() => { vi.clearAllMocks(); cleanup(); });

  it('sums the saved playlists in one catalogue line, each figure a single text node', async () => {
    await renderPlaylists();
    expect(screen.getByText('2 saved', { exact: true })).toBeTruthy();
    expect(screen.getByText('12 of 14 tracks in Beets', { exact: true })).toBeTruthy();
  });

  it('gives syncing its own titled section that holds Sync All to Plex', async () => {
    await renderPlaylists();
    const sync = screen.getByRole('region', { name: 'Sync to Plex' });
    expect(within(sync).getByRole('button', { name: 'Sync All to Plex' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Make a playlist' })).toBeTruthy();
  });

  it('saved-playlist filters are toggles that report aria-pressed', async () => {
    await renderPlaylists();
    const filters = screen.getByRole('group', { name: 'Filter saved playlists' });
    const all = within(filters).getByRole('button', { name: 'All 2' });
    const clean = within(filters).getByRole('button', { name: /^Clean / });
    expect(all.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(clean);
    expect(clean.getAttribute('aria-pressed')).toBe('true');
    expect(all.getAttribute('aria-pressed')).toBe('false');
  });

  it('keeps every mixtape sleeve out of the accessibility tree', async () => {
    await renderPlaylists();
    const sleeves = document.querySelectorAll('.mixtape');
    expect(sleeves.length).toBe(3);
    sleeves.forEach((sleeve) => expect(sleeve.getAttribute('aria-hidden')).toBe('true'));
  });

  it('sizes the mixtape in rem, not in percentages of its row', () => {
    expect(styles).toMatch(/\.mixtape \{[^}]*padding: 0\.875rem;/);
    expect(styles).not.toMatch(/\.mixtape[^{]*\{[^}]*padding:[^;]*%/);
  });
});
