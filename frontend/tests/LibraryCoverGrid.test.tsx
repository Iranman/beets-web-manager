import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import Library from '../src/views/Library';
import { apiGet } from '../src/lib/api';
import type { LibraryAlbum, LibraryResponse } from '../src/types/api';

// Record-shelf redesign: albums render as cover-art tiles with title under the cover,
// a placeholder sleeve when there is no art, and the existing actions still reachable.
vi.mock('../src/lib/api', () => ({ apiGet: vi.fn() }));
vi.mock('../src/api/client', () => ({
  getArtistImageUrl: vi.fn(() => new Promise(() => {})),
  getLibraryArtRepairReport: vi.fn(() => new Promise(() => {})),
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

afterEach(cleanup);

describe('Library cover-art grid', () => {
  it('shows covers from the album art endpoint, a placeholder sleeve without art, and keeps actions', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{
        name: 'Radiohead',
        albums: [
          album({ album_id: 5, album: 'OK Computer', aldir: '/music/a' }),
          album({ album: 'Loose Folder', aldir: '/music/b' }),
        ],
      }],
      stats: { artists: 1, albums: 2, tracks: 24 },
      library_version: 3,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});

    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));

    const tiles = screen.getAllByRole('article');
    const okc = tiles.find((t) => within(t).queryByText('OK Computer'))!;
    const loose = tiles.find((t) => within(t).queryByText('Loose Folder'))!;

    const img = within(okc).getByRole('img', { name: 'OK Computer cover' });
    expect(img.getAttribute('src')).toMatch(/^\/api\/albums\/5\/art/);
    // Title sits under the cover in document order.
    expect(img.compareDocumentPosition(within(okc).getByText('OK Computer')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(okc).getByRole('button', { name: 'View details' })).toBeTruthy();
    expect(within(okc).getByRole('button', { name: 'Remove from Library…' })).toBeTruthy();

    expect(within(loose).queryByRole('img')).toBeNull();
    expect(within(loose).getByTestId('sleeve-placeholder')).toBeTruthy();
    expect(within(loose).getByText('No cover art')).toBeTruthy();
    expect(within(loose).getByRole('button', { name: 'View details' })).toBeTruthy();
  });

  it('falls back to the sleeve when the cover image fails to load', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{ name: 'Radiohead', albums: [album({ album_id: 9, album: 'Kid A', aldir: '/music/c' })] }],
      stats: { artists: 1, albums: 1, tracks: 12 },
      library_version: 1,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});
    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));

    const tile = screen.getAllByRole('article').find((t) => within(t).queryByText('Kid A'))!;
    fireEvent.error(within(tile).getByRole('img', { name: 'Kid A cover' }));
    expect(within(tile).getByTestId('sleeve-placeholder')).toBeTruthy();
  });
});
