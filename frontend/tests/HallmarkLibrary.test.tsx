import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import Library from '../src/views/Library';
import { apiGet } from '../src/lib/api';
import { getAlbumTracks } from '../src/api/client';
import type { LibraryAlbum, LibraryResponse } from '../src/types/api';

// Hallmark Library redesign: crate browser, toggles that announce their state, the header shelf
// kept decorative, and the sleeve flip on a dialog whose hidden face is inert.
vi.mock('../src/lib/api', () => ({ apiGet: vi.fn() }));
vi.mock('../src/api/client', () => ({
  getArtistImageUrl: vi.fn(() => new Promise(() => {})),
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

// These cover the crate grid, now the alternate view behind the Cover Flow / Grid toggle.
beforeEach(() => localStorage.setItem('library.view', 'grid'));
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

const FIX_LINKS = /Manage on Import|Missing MusicBrainz IDs|Fix missing IDs|Acquire missing|Remove from Library|Replace|Track Cleanup|Import files|Repair recording IDs|Fetch art|Fix art|Merge split album/;
const REVIEW_TEXT = /to review|need review|Needs attention|with review items|No review issues|Recording IDs OK|Needs IDs|In review queue|Review information|\d+ missing\b|not imported|Art (local|missing|broken|repair)|Clean\b/;

async function openAlbum(name: string) {
  const tile = screen.getAllByRole('article').find((t) => within(t).queryByText(name))!;
  const trigger = within(tile).getByRole('button', { name: `Open ${name}` });
  trigger.focus();
  fireEvent.click(trigger);
  return { trigger, dialog: await screen.findByRole('dialog', { name }) };
}

describe('Hallmark Library', () => {
  it('keeps each header count a single text node', async () => {
    await renderLibrary();
    expect(screen.getByText('2 artists', { exact: true })).toBeTruthy();
    expect(screen.getByText('2 albums', { exact: true })).toBeTruthy();
    expect(screen.getByText('24 tracks', { exact: true })).toBeTruthy();
  });

  it('is browse-only: no repair block, no issue filters or sorts, and initial tabs report aria-pressed', async () => {
    await renderLibrary();
    expect(screen.queryByRole('region', { name: 'Next repair' })).toBeNull();
    expect(screen.queryByRole('group', { name: 'Filter the library by issue' })).toBeNull();
    expect(document.body.textContent).not.toMatch(/Needs attention first|Most issues/);

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

  it('shows no fix links and no review indicators, even for an album with issues', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{ name: 'Radiohead', albums: [album({ album_id: 5, album: 'OK Computer', aldir: '/music/a', mb_releasegroupid: '', not_imported: 2, missing: 1, pending_review: true })] }],
      stats: { artists: 1, albums: 1, tracks: 12 },
      library_version: 3,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});
    expect(document.body.textContent).not.toMatch(REVIEW_TEXT);
    expect(screen.queryByRole('button', { name: FIX_LINKS })).toBeNull();
    expect(screen.queryByRole('link', { name: FIX_LINKS })).toBeNull();

    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));
    expect(document.body.textContent).not.toMatch(REVIEW_TEXT);
    const tile = screen.getAllByRole('article').find((t) => within(t).queryByText('OK Computer'))!;
    expect(within(tile).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(['Open OK Computer']);

    const { dialog } = await openAlbum('OK Computer');
    expect(dialog.textContent).not.toMatch(REVIEW_TEXT);
    expect(within(dialog).queryByRole('button', { name: FIX_LINKS })).toBeNull();
    expect(within(dialog).getAllByRole('button').map((b) => b.textContent)).toEqual(['Close']);
  });

  it('flips the album open from its sleeve and back: focus lands on the title, the cover face is inert, focus returns', async () => {
    await renderLibrary();
    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));
    const tile = screen.getAllByRole('article').find((t) => within(t).queryByText('OK Computer'))!;
    expect(within(tile).getByRole('button', { name: 'Open OK Computer' }).getAttribute('aria-haspopup')).toBe('dialog');
    const { trigger, dialog } = await openAlbum('OK Computer');
    const heading = within(dialog).getByRole('heading', { name: 'OK Computer' });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    const front = dialog.querySelector('.flip-front')!;
    expect(front.getAttribute('aria-hidden')).toBe('true');
    expect(front.hasAttribute('inert')).toBe(true);
    // Close turns the sleeve back over first, then the dialog goes and focus returns to the sleeve.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(dialog.querySelector('.flip-inner')!.classList.contains('is-closing')).toBe(true);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('prints the tracklist as Side A and Side B, marking only missing tracks', async () => {
    const track = (n: number, extra = {}) => ({ id: n, track: n, disc: 1, title: `Song ${n}`, length: 185, path: `/music/a/${n}.flac`, mb_trackid: '', ...extra });
    vi.mocked(getAlbumTracks).mockResolvedValueOnce({ ok: true, album_id: 5, tracks: [track(1), track(2), track(3, { missing: true, status: 'missing_file' }), track(4, { status: 'not_imported' }), track(5)] } as never);
    await renderLibrary();
    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));
    const { dialog } = await openAlbum('OK Computer');
    const sideA = await within(dialog).findByRole('heading', { name: 'Side A' });
    const sideB = within(dialog).getByRole('heading', { name: 'Side B' });
    expect(within(sideA.closest('section')!).getAllByRole('listitem')).toHaveLength(3);
    expect(within(sideB.closest('section')!).getAllByRole('listitem')).toHaveLength(2);
    const missing = within(dialog).getByText('Song 3').closest('li')!;
    expect(missing.classList.contains('is-missing')).toBe(true);
    expect(within(missing).getByText('missing')).toBeTruthy();
    expect(within(dialog).getAllByText('missing')).toHaveLength(1);
    expect(within(dialog).getAllByText('3:05')).toHaveLength(5);
    expect(within(dialog).getByText('Total time 15:25')).toBeTruthy();
  });

  it('prints the back cover over the album art when there is art, and on plain paper when there is none', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      ok: true,
      reconciled: true,
      artists: [{ name: 'Radiohead', albums: [
        album({ album_id: 5, album: 'OK Computer', aldir: '/music/a' }),
        album({ album_id: 0, album: 'Loose Folder', aldir: '/music/b' }),
      ] }],
      stats: { artists: 1, albums: 2, tracks: 24 },
      library_version: 3,
    } as LibraryResponse);
    render(<MemoryRouter><Library /></MemoryRouter>);
    await act(async () => {});
    fireEvent.click(await screen.findByRole('button', { name: /Radiohead/ }));

    let { dialog } = await openAlbum('OK Computer');
    const back = dialog.querySelector('.flip-back')!;
    const art = back.querySelector('img.sleeve-back-art')!;
    expect(back.classList.contains('has-art')).toBe(true);
    // The same URL the front sleeve uses: no new request.
    expect(art.getAttribute('src')).toBe(dialog.querySelector('.flip-front img')!.getAttribute('src'));
    expect(art.getAttribute('alt')).toBe('');
    fireEvent.error(art);
    expect(back.classList.contains('has-art')).toBe(false);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

    ({ dialog } = await openAlbum('Loose Folder'));
    expect(dialog.querySelector('.flip-back')!.classList.contains('has-art')).toBe(false);
    expect(dialog.querySelector('img.sleeve-back-art')).toBeNull();
  });

  it('keeps the sleeve square, scrolls long tracklists inside it, and crossfades under reduced motion', () => {
    expect(styles).toMatch(/\.flip-card \{[^}]*aspect-ratio: 1;/);
    expect(styles).toMatch(/\.sleeve-body \{[^}]*min-height: 0;[^}]*overflow-y: auto;/);
    expect(styles).toMatch(/\.flip-inner \{[^}]*transform-style: preserve-3d/);
    expect(styles).toMatch(/@keyframes sleeve-flip\s*\{\s*from \{ transform: rotateY\(0deg\); \}\s*to \{ transform: rotateY\(180deg\); \}/);
    expect(styles).toMatch(/@keyframes sleeve-unflip\s*\{\s*from \{ transform: rotateY\(180deg\); \}\s*to \{ transform: rotateY\(0deg\); \}/);
    expect(styles).toMatch(/prefers-reduced-motion: reduce\)\s*\{\s*\.flip-inner,\s*\.flip-back\s*\{\s*animation: sleeve-fade 180ms ease-out both;\s*transform: none;/);
  });
});
