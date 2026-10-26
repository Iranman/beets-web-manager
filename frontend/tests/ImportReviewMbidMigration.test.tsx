// MBID addendum (ARCH-030): every MusicBrainz ID shown on the Import Review
// page must route through MbidLink — full UUID, clickable to the correct MB
// entity in a new tab, with Copy, a status chip, validated before linking. This
// covers the non-recovery renderers migrated off bare <a> tags, plus the
// handleSuggest success-truth fix (a reachable "no match" is a warning, not a
// green success).
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    suggestFolder: vi.fn(),
    suggestAlbum: vi.fn(),
    suggestItem: vi.fn(),
    previewImportTarget: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

const RG = '11111111-2222-3333-4444-555555555555';
const mocked = vi.mocked(client);

function renderPage() {
  return render(
    <MemoryRouter>
      <ImportReviewPage active />
    </MemoryRouter>,
  );
}

function queueWith(item: ReviewItem) {
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [item],
    counts: { all: 1 },
  } as never);
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  // Keep Find Match from resolving so the item renders without side effects in
  // the renderer tests.
  mocked.suggestFolder.mockImplementation(() => new Promise(() => undefined));
  mocked.suggestAlbum.mockImplementation(() => new Promise(() => undefined));
  mocked.suggestItem.mockImplementation(() => new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('Import Review MBID renderers route through MbidLink', () => {
  it('links a valid release-group MBID to the correct MB entity in a new tab, with Copy and a neutral status', async () => {
    queueWith({
      id: 'lib:1',
      type: 'library_no_mb',
      album: 'An Album',
      artist: 'Artist',
      album_id: 1,
      mb_releasegroupid: RG,
    } as unknown as ReviewItem);
    renderPage();

    const link = await screen.findByRole('link', { name: /on MusicBrainz in a new tab/i });
    expect(link.getAttribute('href')).toBe(`https://musicbrainz.org/release-group/${RG}`);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel') || '').toContain('noreferrer');
    expect(link.textContent).toContain(RG); // full UUID, not truncated

    const row = link.closest('[data-mbid-entity="release-group"]') as HTMLElement;
    expect(row).toBeTruthy();
    // Copy button present, and the status chip defaults to the neutral
    // "Suggested" (mb_valid not asserted) — never an invented "Verified".
    expect(within(row).getByRole('button', { name: /Copy/i })).toBeTruthy();
    expect(row.textContent).toContain('Suggested');
    expect(row.textContent).not.toContain('Verified');
  });

  it('shows a verified status only when the backend asserts mb_valid', async () => {
    queueWith({
      id: 'lib:2',
      type: 'library_no_mb',
      album: 'Verified Album',
      artist: 'Artist',
      album_id: 2,
      mb_releasegroupid: RG,
      mb_valid: true,
    } as unknown as ReviewItem);
    renderPage();

    const link = await screen.findByRole('link', { name: /on MusicBrainz in a new tab/i });
    const row = link.closest('[data-mbid-entity="release-group"]') as HTMLElement;
    expect(row.textContent).toContain('Verified');
  });

  it('never builds a link for an invalid MBID — it renders the invalid marker instead', async () => {
    queueWith({
      id: 'lib:3',
      type: 'library_no_mb',
      album: 'Bad Id Album',
      artist: 'Artist',
      album_id: 3,
      mb_releasegroupid: 'not-a-real-mbid',
    } as unknown as ReviewItem);
    renderPage();

    expect(await screen.findByText(/invalid MusicBrainz ID/i)).toBeTruthy();
    // The invalid id must not be a link to musicbrainz.org.
    for (const link of screen.queryAllByRole('link')) {
      expect(link.getAttribute('href') || '').not.toContain('not-a-real-mbid');
    }
  });
});

describe('handleSuggest success-truth (reachable "no match" is a warning, not success)', () => {
  it('renders a neutral warning — never a green success — when Find Match returns no selected match / no valid MBID', async () => {
    // A pending item auto-runs Find Match on render.
    queueWith({
      id: 'pending:/downloads/Artist/No Match Album',
      type: 'pending_ai',
      path: '/downloads/Artist/No Match Album',
      album: 'No Match Album',
      artist: 'Artist',
    } as unknown as ReviewItem);
    // Reachable 200 with no match: no selected_match, mb_valid false, no mb_albumid.
    mocked.suggestFolder.mockResolvedValue({ ok: true, suggestion: { mb_valid: false } } as never);
    renderPage();

    const alert = await waitFor(() => {
      const el = screen.getByText(/Find Match completed\. Verify the release-group ID before applying\./i)
        .closest('[role="alert"]') as HTMLElement | null;
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    expect(alert.className).toMatch(/warning/i);
    expect(alert.className).not.toMatch(/success/i);
  });
});
