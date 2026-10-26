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
import { ConfirmDialog, ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

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

  it('never promotes the chip to "Verified" from mb_valid (UUID syntax only), yet still links the valid id', async () => {
    // `mb_valid` is set by UUID syntax alone (backend ai_service), not a
    // confirmed match. A syntactically-valid-but-unverified id must stay
    // neutral "Suggested" — never a green "Verified" chip — while still
    // being a working, copyable MB link (validity governs linking, not
    // verification).
    queueWith({
      id: 'lib:2',
      type: 'library_no_mb',
      album: 'Syntactically Valid Album',
      artist: 'Artist',
      album_id: 2,
      mb_releasegroupid: RG,
      mb_valid: true,
    } as unknown as ReviewItem);
    renderPage();

    const link = await screen.findByRole('link', { name: /on MusicBrainz in a new tab/i });
    expect(link.getAttribute('href')).toBe(`https://musicbrainz.org/release-group/${RG}`);
    const row = link.closest('[data-mbid-entity="release-group"]') as HTMLElement;
    expect(row.textContent).toContain('Suggested');
    expect(row.textContent).not.toContain('Verified');
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

  it('a malformed id never produces a MusicBrainz outbound Button link', async () => {
    // The outlined "MusicBrainz" Button builds its href from the id via the
    // musicBrainz* helpers; a malformed id must guard the href to '' so no
    // dead outbound link renders (no element points at the bad id).
    queueWith({
      id: 'lib:4',
      type: 'library_no_mb',
      album: 'Bad Button Album',
      artist: 'Artist',
      album_id: 4,
      target_kind: 'album',
      mb_releasegroupid: RG, // valid -> an MbidLink anchor we can await
      mb_albumid: 'not-a-real-mbid', // invalid -> feeds the "MusicBrainz" Button href
    } as unknown as ReviewItem);
    renderPage();

    // Wait for the card to render (its valid release-group MbidLink).
    await screen.findByRole('link', { name: /on MusicBrainz in a new tab/i });
    // The outlined "MusicBrainz" release Button must be absent (guarded href '').
    expect(screen.queryByRole('link', { name: /^MusicBrainz$/ })).toBeNull();
    for (const el of [...screen.queryAllByRole('link'), ...screen.queryAllByRole('button')]) {
      expect(el.getAttribute('href') || '').not.toContain('not-a-real-mbid');
    }
  });
});

describe('apply-confirm dialog routes its MBID through MbidLink', () => {
  const applyItem = (overrides: Partial<ReviewItem>): ReviewItem =>
    ({
      id: 'lib:apply',
      type: 'library_no_mb',
      album: 'Apply Album',
      artist: 'Artist',
      album_id: 7,
      target_kind: 'album',
      ...overrides,
    }) as unknown as ReviewItem;

  it('renders a valid apply MBID as a validated, copyable, linked MbidLink (not a bare truncated id)', () => {
    render(
      <ConfirmDialog
        intent={{ kind: 'apply', item: applyItem({}), mbid: RG } as never}
        onClose={() => undefined}
        onConfirm={() => undefined}
      />,
    );
    const link = screen.getByRole('link', { name: /on MusicBrainz in a new tab/i });
    expect(link.getAttribute('href')).toBe(`https://musicbrainz.org/release/${RG}`);
    expect(link.textContent).toContain(RG); // full UUID, not truncated
    const row = link.closest('[data-mbid-entity="release"]') as HTMLElement;
    expect(row).toBeTruthy();
    expect(within(row).getByRole('button', { name: /Copy/i })).toBeTruthy();
    expect(row.textContent).toContain('Suggested');
    expect(row.textContent).not.toContain('Verified');
  });

  it('shows the invalid-not-linked marker for a malformed apply MBID', () => {
    render(
      <ConfirmDialog
        intent={{ kind: 'apply', item: applyItem({}), mbid: 'not-a-real-mbid' } as never}
        onClose={() => undefined}
        onConfirm={() => undefined}
      />,
    );
    expect(screen.getByText(/invalid MusicBrainz ID/i)).toBeTruthy();
    for (const link of screen.queryAllByRole('link')) {
      expect(link.getAttribute('href') || '').not.toContain('not-a-real-mbid');
    }
  });

  it('uses the recording entity for a per-item (target_kind=item) apply', () => {
    const REC = '99999999-8888-7777-6666-555555555555';
    render(
      <ConfirmDialog
        intent={{ kind: 'apply', item: applyItem({ target_kind: 'item', item_id: 3 }), mbid: REC } as never}
        onClose={() => undefined}
        onConfirm={() => undefined}
      />,
    );
    const link = screen.getByRole('link', { name: /on MusicBrainz in a new tab/i });
    expect(link.getAttribute('href')).toBe(`https://musicbrainz.org/recording/${REC}`);
    expect(link.closest('[data-mbid-entity="recording"]')).toBeTruthy();
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
