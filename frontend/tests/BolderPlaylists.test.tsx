import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Playlists from '../src/views/Playlists';
import { getPlaylists, getPlaylistSyncStatus } from '../src/api/client';

// Bolder Playlists: the mixtape bench raised into the Bolder Shelf world (scoped .playlists-bolder),
// reusing the foundation tokens (brass rail, liner-label, record ring, one glass frame). Presentation
// only — the behavioural tests in the other playlist suites still own every handler and gate.
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
    playlists: [{ name: 'Late Night Drive', tracks: 10, available: 8, missing: 2, has_m3u: true }],
  } as never);
  vi.mocked(getPlaylistSyncStatus).mockResolvedValue({ ok: true, enabled: false, interval: 0, running: false });
  render(<MemoryRouter><Playlists /></MemoryRouter>);
  await screen.findByText('Late Night Drive');
}

describe('Bolder Playlists', () => {
  afterEach(() => { vi.clearAllMocks(); cleanup(); });

  it('scopes the bolder treatment to a .playlists-bolder wrapper without touching the sticky-bar root', async () => {
    await renderPlaylists();
    expect(document.querySelector('.playlists-bolder')).toBeTruthy();
    // The phone-layout test pins this exact root string; the wrapper must sit outside it.
    expect(document.querySelector('.playlists-bolder > .touch-controls')).toBeTruthy();
  });

  it('marks the expanded saved-playlist row with the shared brass rail', async () => {
    await renderPlaylists();
    expect(document.querySelector('.brass-rail')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Expand details for Late Night Drive' }));
    expect(document.querySelector('.brass-rail')).toBeTruthy();
  });

  it('sets saved-playlist metadata as liner-note small-caps', async () => {
    await renderPlaylists();
    expect(document.querySelector('.liner-label')).toBeTruthy();
  });

  it('keeps the one frosted-glass moment on the sticky track bar with an @supports fallback', () => {
    // The solid walnut fallback lives in the base rule; the blur is guarded by @supports.
    expect(styles).toMatch(/\.playlists-bolder \.pl-tracks-bar \{[^}]*background-color: color-mix\(in srgb, var\(--color-graphite-950\)/);
    expect(styles).toMatch(/@supports \([^)]*backdrop-filter[\s\S]*?\.playlists-bolder \.pl-tracks-bar \{[\s\S]*?backdrop-filter: blur/);
  });

  it('reuses the record ring as the saved-playlists loading spinner', () => {
    // The page imports the shared RecordRing mark rather than drawing its own spinner.
    const src = readFileSync(join(__dirname, '../src/views/Playlists.tsx'), 'utf8');
    expect(src).toContain("import { RecordRing } from '../components/RecordRing';");
    expect(src).toMatch(/<RecordRing spinning/);
  });
});
