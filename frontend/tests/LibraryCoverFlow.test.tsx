import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import Library from '../src/views/Library';
import { apiGet } from '../src/lib/api';
import {
  baseWidth,
  clampIndex,
  coverOpacity,
  coverTransform,
  flowKeyTarget,
  flowWindow,
  sideCount,
  windowRadius,
} from '../src/components/CoverFlow';
import type { LibraryAlbum, LibraryResponse } from '../src/types/api';

vi.mock('../src/lib/api', () => ({ apiGet: vi.fn() }));
vi.mock('../src/api/client', () => ({
  getArtistImageUrl: vi.fn(() => new Promise(() => {})),
  getAlbumTracks: vi.fn(() => Promise.resolve({ tracks: [] })),
}));

describe('Cover Flow windowing math', () => {
  it('renders only a clamped window around the centre', () => {
    expect(flowWindow(0, 5000, 8)).toEqual([0, 9]);
    expect(flowWindow(2500, 5000, 8)).toEqual([2492, 2509]);
    expect(flowWindow(4999, 5000, 8)).toEqual([4991, 5000]);
    expect(flowWindow(99, 3, 8)).toEqual([0, 3]);
    expect(flowWindow(0, 0, 8)).toEqual([0, 0]);
  });

  it('shows fewer side covers on a phone and never renders more than eight each way', () => {
    expect(sideCount(296)).toBe(1);
    expect(sideCount(366)).toBe(1);
    expect(sideCount(700)).toBe(2);
    expect(sideCount(1400)).toBe(3);
    for (const width of [296, 700, 1400, 4000]) expect(windowRadius(width)).toBeLessThanOrEqual(8);
    // One slot is the stage over (2 * sides + 1), capped.
    expect(baseWidth(296)).toBe(99);
    expect(baseWidth(1400)).toBe(170);
  });

  it('turns side covers about Y and keeps the reduced-motion strip flat', () => {
    expect(coverTransform(0, 100, false)).toContain('rotateY(0deg)');
    expect(coverTransform(-1, 100, false)).toContain('rotateY(40deg)');
    expect(coverTransform(2, 100, false)).toContain('rotateY(-40deg)');
    expect(coverTransform(2, 100, false)).toContain('translateX(200px)');
    expect(coverTransform(-3, 100, true)).not.toContain('rotate');
    expect(coverOpacity(0, 2)).toBe(1);
    expect(coverOpacity(2, 2)).toBeGreaterThan(0);
    expect(coverOpacity(3, 2)).toBe(0);
  });

  it('maps Left, Right, Home and End to a clamped target', () => {
    expect(flowKeyTarget('ArrowRight', 0, 3)).toBe(1);
    expect(flowKeyTarget('ArrowRight', 2, 3)).toBe(2);
    expect(flowKeyTarget('ArrowLeft', 0, 3)).toBe(0);
    expect(flowKeyTarget('Home', 2, 3)).toBe(0);
    expect(flowKeyTarget('End', 0, 3)).toBe(2);
    expect(flowKeyTarget('a', 0, 3)).toBeNull();
    expect(flowKeyTarget('End', 0, 0)).toBeNull();
    expect(clampIndex(7.4, 3)).toBe(2);
  });
});

const album = (overrides: Partial<LibraryAlbum>): LibraryAlbum => ({
  album_id: 0,
  album: '',
  albumartist: '',
  year: 2001,
  mb_albumid: '',
  mb_releasegroupid: '',
  aldir: '',
  track_count: 3,
  not_imported: 0,
  ...overrides,
});

async function renderLibrary() {
  vi.mocked(apiGet).mockResolvedValue({
    ok: true,
    reconciled: true,
    artists: [
      { name: 'Alpha Band', albums: [album({ album_id: 1, album: 'First', aldir: '/a/1' })] },
      { name: 'Beta Choir', albums: [album({ album: 'Loose', aldir: '/b/1' })] },
      {
        name: 'Gamma Ray',
        albums: [
          album({ album_id: 3, album: 'One', aldir: '/g/1', year: 1999 }),
          album({ album_id: 4, album: 'Two', aldir: '/g/2', year: 2004 }),
        ],
      },
    ],
    stats: { artists: 3, albums: 4, tracks: 12 },
    library_version: 1,
  } as LibraryResponse);
  render(<MemoryRouter><Library /></MemoryRouter>);
  await act(async () => {});
}

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe('Library Cover Flow', () => {
  it('is the default view: a labelled carousel driven by the keyboard, announcing the centred sleeve', async () => {
    await renderLibrary();
    const flow = screen.getByRole('region', { name: 'Artists' });
    expect(flow.getAttribute('aria-roledescription')).toBe('carousel');
    expect(flow.tabIndex).toBe(0);
    expect(screen.getByRole('button', { name: 'Artist Alpha Band, 1 of 3' }).getAttribute('aria-current')).toBe('true');
    // Artists with no cached image borrow the first album cover from the payload: no extra request.
    expect(screen.getByRole('button', { name: 'Artist Alpha Band, 1 of 3' }).querySelector('img')?.getAttribute('src')).toMatch(/^\/api\/albums\/1\/art/);

    fireEvent.keyDown(flow, { key: 'End' });
    expect(screen.getByRole('button', { name: 'Artist Gamma Ray, 3 of 3' }).getAttribute('aria-current')).toBe('true');
    expect(screen.getByText('Artist Gamma Ray, 3 of 3', { selector: '[aria-live]' })).toBeTruthy();
    fireEvent.keyDown(flow, { key: 'ArrowLeft' });
    expect(screen.getByRole('button', { name: 'Artist Beta Choir, 2 of 3' }).getAttribute('aria-current')).toBe('true');
    fireEvent.keyDown(flow, { key: 'Home' });
    expect(screen.getByRole('button', { name: 'Artist Alpha Band, 1 of 3' }).getAttribute('aria-current')).toBe('true');
  });

  it('A to Z jumps to the first match, and clicking a side cover centres it', async () => {
    await renderLibrary();
    fireEvent.click(screen.getByRole('button', { name: 'G' }));
    expect(screen.getByRole('button', { name: 'Artist Gamma Ray, 3 of 3' }).getAttribute('aria-current')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Artist Beta Choir, 2 of 3' }));
    expect(screen.getByRole('button', { name: 'Artist Beta Choir, 2 of 3' }).getAttribute('aria-current')).toBe('true');
  });

  it('Enter drills into the artist, then opens the centred album on its back cover', async () => {
    await renderLibrary();
    const artists = screen.getByRole('region', { name: 'Artists' });
    fireEvent.keyDown(artists, { key: 'End' });
    fireEvent.keyDown(artists, { key: 'Enter' });
    const albums = screen.getByRole('region', { name: 'Albums' });
    expect(screen.getByRole('button', { name: 'Album One by Gamma Ray, 1 of 2' })).toBeTruthy();
    fireEvent.keyDown(albums, { key: 'ArrowRight' });
    fireEvent.keyDown(albums, { key: 'Enter' });
    await act(async () => {});
    expect(await screen.findByRole('dialog', { name: 'Two' })).toBeTruthy();
  });

  it('remembers the Grid choice in localStorage', async () => {
    await renderLibrary();
    fireEvent.click(screen.getByRole('button', { name: 'Grid' }));
    expect(localStorage.getItem('library.view')).toBe('grid');
    expect(screen.queryByRole('region', { name: 'Artists' })).toBeNull();
    expect(screen.getByRole('button', { name: /Gamma Ray artwork/ })).toBeTruthy();
    cleanup();
    await renderLibrary();
    expect(screen.getByRole('button', { name: 'Grid' }).getAttribute('aria-pressed')).toBe('true');
  });
});
