import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import Library from '../src/views/Library';
import { apiGet } from '../src/lib/api';
import type { LibraryAlbum, LibraryResponse } from '../src/types/api';

// Hallmark Library redesign: crate browser, toggles that announce their state, the header shelf
// kept decorative, and the sleeve flip on a dialog whose hidden face is inert.
vi.mock('../src/lib/api', () => ({ apiGet: vi.fn() }));
vi.mock('../src/api/client', () => ({
  getArtistImageUrl: vi.fn(() => new Promise(() => {})),
  getLibraryArtRepairReport: vi.fn(() => new Promise(() => {})),
  getAlbumTracks: vi.fn(() => Promise.resolve({ tracks: [] })),
}));

const album = (overrides: Partial<LibraryAlbum>): LibraryAlbum => ({
  album_id: 0,
  album: '',
  albumartist: 'Radiohead',
  year: 1997,
  mb_albumid: '',
  mb_releasegroupid: 'rg',
  aldir: '',
  track_count: 12,
  not_imported: 0,
  ...overrides,
});

const styles = readFileSync(join(__dirname, '../src/styles.css'), 'utf8');

afterEach(cleanup);

async function renderLibrary() {
  vi.mocked(apiGet).mockResolvedValue({
    ok: true,
    reconciled: true,
    artists: [
      { name: 'Radiohead', albums: [album({ album_id: 5, album: 'OK Computer', aldir: '/music/a' })] },
      { name: 'Air', albums: [album({ album_id: 6, album: 'Moon Safari', albumartist: 'Air', aldir: '/music/b' })] },
    ],
    stats: { artists: 2, albums: 2, tracks: 24 },
    library_version: 3,
  } as LibraryResponse);
  render(<MemoryRouter><Library /></MemoryRouter>);
  await act(async () => {});
}

describe('Hallmark Library', () => {
  it('keeps each header count a single text node and marks the review pill in words', async () => {
    await renderLibrary();
    expect(screen.getByText('2 artists', { exact: true })).toBeTruthy();
    expect(screen.getByText('2 albums', { exact: true })).toBeTruthy();
    expect(screen.getByText('24 tracks', { exact: true })).toBeTruthy();
  });

  it('issue filters and initial tabs are toggles that report aria-pressed', async () => {
    await renderLibrary();
    const filters = screen.getByRole('group', { name: 'Filter the library by issue' });
    const all = within(filters).getByRole('button', { name: /Artists\s*All/ });
    expect(all.getAttribute('aria-pressed')).toBe('true');

    const tabs = screen.getByRole('group', { name: 'Artists by initial' });
    const r = within(tabs).getByRole('button', { name: 'R' });
    expect(r.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(r);
    expect(r.getAttribute('aria-pressed')).toBe('true');
    expect(within(tabs).getByRole('button', { name: 'Q' })).toHaveProperty('disabled', true);
  });

  it('files artists under crate divider tabs when sorted A to Z', async () => {
    await renderLibrary();
    expect(screen.getByRole('heading', { name: 'A', level: 3 })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'R', level: 3 })).toBeTruthy();
  });

  it('flips the album open: the back face takes focus and the cover face is inert', async () => {
    await renderLibrary();
    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));
    const tile = screen.getAllByRole('article').find((t) => within(t).queryByText('OK Computer'))!;
    const trigger = within(tile).getByRole('button', { name: 'View details' });
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog');
    fireEvent.click(trigger);
    const dialog = await screen.findByRole('dialog', { name: 'OK Computer' });
    const front = dialog.querySelector('.flip-front')!;
    expect(front.getAttribute('aria-hidden')).toBe('true');
    expect(front.hasAttribute('inert')).toBe(true);
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeTruthy();
  });

  it('shows repair actions only as links naming the page that owns them', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{ name: 'Radiohead', albums: [album({ album_id: 5, album: 'OK Computer', aldir: '/music/a' })] }],
      stats: { artists: 1, albums: 1, tracks: 12 },
      library_version: 3,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});
    const links = screen.getAllByRole('button', { name: /^Fix missing IDs on\s*Missing IDs$/ });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) expect(link.className).not.toMatch(/MuiButton-contained/);
  });

  it('runs the flip on transform only and drops the rotation under reduced motion', () => {
    expect(styles).toMatch(/\.flip-inner\s*\{[^}]*transform-style: preserve-3d/);
    expect(styles).toMatch(/@keyframes sleeve-flip\s*\{\s*from \{ transform: rotateY\(0deg\); \}\s*to \{ transform: rotateY\(180deg\); \}/);
    expect(styles).toMatch(/prefers-reduced-motion: reduce\)\s*\{\s*\.flip-inner,\s*\.flip-back\s*\{\s*animation: none;\s*transform: none;/);
  });
});
