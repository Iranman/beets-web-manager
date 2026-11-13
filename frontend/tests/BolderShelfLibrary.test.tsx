import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import Library from '../src/views/Library';
import { apiGet } from '../src/lib/api';
import type { LibraryAlbum, LibraryResponse } from '../src/types/api';

// Bolder Shelf: the Library's deeper-walnut / bolder-brass evolution. Covers the new liner-note
// stat band (counts still single text nodes), the designed placeholder initial, the loading
// skeleton, and the character empty state with its onboarding way in.
vi.mock('../src/lib/api', () => ({ apiGet: vi.fn() }));
vi.mock('../src/api/client', () => ({
  getArtistImageUrl: vi.fn(() => new Promise(() => {})),
  getAlbumTracks: vi.fn(() => new Promise(() => {})),
}));

const album = (overrides: Partial<LibraryAlbum>): LibraryAlbum => ({
  album_id: 0,
  album: '',
  albumartist: 'Radiohead',
  year: 1997,
  mb_albumid: '',
  mb_releasegroupid: '',
  aldir: '',
  track_count: 12,
  not_imported: 0,
  ...overrides,
});

const styles = readFileSync(join(__dirname, '../src/styles.css'), 'utf8');

beforeEach(() => localStorage.setItem('library.view', 'grid'));
afterEach(cleanup);

describe('Bolder Shelf Library', () => {
  it('renders the oversized counts as single text nodes on the liner band', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{ name: 'Radiohead', albums: [album({ album_id: 5, album: 'OK Computer', aldir: '/music/a' })] }],
      stats: { artists: 1, albums: 1, tracks: 12 },
      library_version: 7,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});

    const artists = screen.getByText('1 artist', { exact: true });
    expect(artists.classList.contains('lib-liner-figure')).toBe(true);
    expect(artists.closest('.lib-liner')).toBeTruthy();
    expect(screen.getByText('1 album', { exact: true })).toBeTruthy();
    expect(screen.getByText('12 tracks', { exact: true })).toBeTruthy();
  });

  it('keeps the frosted-glass moment to the liner band with a solid fallback', () => {
    // Warm glass is a seasoning: the blur lives only on .lib-liner, behind an @supports guard with a
    // near-opaque solid fallback. No backdrop-filter anywhere else (never behind the lists).
    const blurs = styles.match(/backdrop-filter:[^;]+/g) ?? [];
    expect(blurs.length).toBeGreaterThan(0);
    expect(styles).toMatch(/@supports[^{]*backdrop-filter[\s\S]*?\.lib-liner\s*\{/);
    expect(styles).toMatch(/\.lib-liner\s*\{[\s\S]*?background-color: color-mix\(in srgb, var\(--bw-surface\) 94%/);
  });

  it('prints the album initial in the display face on the no-art placeholder', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{ name: 'Radiohead', albums: [album({ album: 'Loose Folder', aldir: '/music/b' })] }],
      stats: { artists: 1, albums: 1, tracks: 12 },
      library_version: 1,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});
    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));

    const tile = screen.getAllByRole('article').find((t) => within(t).queryByText('Loose Folder'))!;
    const placeholder = within(tile).getByTestId('sleeve-placeholder');
    const initial = placeholder.querySelector('.lib-sleeve-initial');
    expect(initial?.textContent).toBe('L');
    expect(initial?.getAttribute('aria-hidden')).toBe('true');
    // Still the accessible "No cover art" name, and no action buttons on the tile.
    expect(within(tile).getByText('No cover art')).toBeTruthy();
    expect(within(tile).getAllByRole('button')).toHaveLength(1);
  });

  it('shows a designed skeleton while the first payload loads, with unknown counts', async () => {
    vi.mocked(apiGet).mockReturnValue(new Promise(() => {}) as Promise<LibraryResponse>);
    const { container } = render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});

    expect(container.querySelector('.lib-skeleton-grid')).toBeTruthy();
    expect(screen.getByText('— artists', { exact: true })).toBeTruthy();
    // No "No matching artists" claim while the library is still loading.
    expect(screen.queryByText(/No matching artists/)).toBeNull();
    // The polite status is announced for assistive tech.
    expect(screen.getByText('Loading library…')).toBeTruthy();
  });

  it('shows the character empty state and the way in when the library is empty', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [],
      stats: { artists: 0, albums: 0, tracks: 0 },
      library_version: 1,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});

    expect(screen.getByText('Nothing on this shelf yet…')).toBeTruthy();
    const cta = screen.getByRole('link', { name: 'Go to Import' });
    expect(cta.getAttribute('href')).toBe('/import');
    // The empty library still reports its zero counts on the band.
    expect(screen.getByText('0 artists', { exact: true })).toBeTruthy();
    // The browse controls step aside for the empty state.
    expect(screen.queryByRole('textbox', { name: 'Search' })).toBeNull();
    expect(screen.queryByText(/artists visible/)).toBeNull();
  });
});
