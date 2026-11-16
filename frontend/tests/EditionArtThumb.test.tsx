/**
 * Edition candidate cards show a release-level Cover Art Archive thumbnail,
 * fetched-and-stored via getReleaseArt(mbid, artist, album, 'release'). The
 * thumbnail is purely visual; on {ok:false} / no id it shows the calm blank
 * sleeve placeholder, never a broken-image icon.
 */
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api/client', async (importActual) => {
  const actual = await importActual<typeof import('../src/api/client')>();
  return { ...actual, getReleaseArt: vi.fn() };
});

import { getReleaseArt } from '../src/api/client';
import { EditionArtThumb } from '../src/features/importReview/ImportReviewPage';

const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';
const mockArt = vi.mocked(getReleaseArt);

afterEach(() => {
  cleanup();
  mockArt.mockReset();
});

describe('EditionArtThumb', () => {
  it('renders a lazy release-level art img from the stored cache url', async () => {
    mockArt.mockResolvedValue({ ok: true, url: `/api/release-art-cache/${REL}?v=1` });
    const { container } = render(<EditionArtThumb mbid={REL} album="B" artist="A" />);
    const img = await waitFor(() => {
      const el = container.querySelector('img');
      expect(el).toBeTruthy();
      return el!;
    });
    expect(img.getAttribute('src')).toBe(`/api/release-art-cache/${REL}?v=1`);
    expect(img.getAttribute('loading')).toBe('lazy');
    expect(img.getAttribute('alt')).toBe('B cover art');
    expect(mockArt).toHaveBeenCalledWith(REL, 'A', 'B', 'release');
  });

  it('shows the blank-sleeve placeholder on {ok:false}', async () => {
    mockArt.mockResolvedValue({ ok: false, error: 'no art found' });
    const { container } = render(<EditionArtThumb mbid={REL} album="B" artist="A" />);
    await waitFor(() => expect(mockArt).toHaveBeenCalled());
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('svg')).toBeTruthy();
  });

  it('never fetches for a non-UUID id and shows the placeholder', () => {
    const { container } = render(<EditionArtThumb mbid="not-an-mbid" album="B" />);
    expect(mockArt).not.toHaveBeenCalled();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('svg')).toBeTruthy();
  });
});
