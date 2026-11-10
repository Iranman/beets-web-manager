// #580: Missing IDs caches a validated suggestion's Release Group and Release,
// and the ID box prefills the Release Group. POST /api/albums/<id>/match reads a
// bare UUID as a Release, so Match album must send the cached Release (or a
// release-group URL), never the bare Release Group UUID.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getLibraryMissingMbIds: vi.fn(),
    suggestAlbum: vi.fn(),
    previewImportTarget: vi.fn(),
    decideImportReviewRemote: vi.fn(),
    matchAlbum: vi.fn(),
    getJob: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage, matchAlbumInput } from '../src/features/importReview/ImportReviewPage';

const mocked = vi.mocked(client);
const RG = '11111111-2222-4333-8444-555555555555';
const REL = '66666666-7777-4888-9999-aaaaaaaaaaaa';

function albumRow(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: 'library:7',
    type: 'library_no_mb',
    target_kind: 'album',
    album_id: 7,
    title: 'An Album',
    album: 'An Album',
    artist: 'Artist',
    path: '/music/Artist/An Album',
    mb_releasegroupid: RG,
    mb_albumid: REL,
    identity_validated: true,
    ...overrides,
  } as unknown as ReviewItem;
}

async function matchWith(row: ReviewItem) {
  mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [row], total: 1, counts: { all: 1, album: 1, item: 0 } } as never);
  render(<MemoryRouter><ImportReviewPage active mode="missing_ids" /></MemoryRouter>);
  const button = await screen.findByRole('button', { name: 'Match album' });
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
  fireEvent.click(button);
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(mocked.matchAlbum).toHaveBeenCalledTimes(1));
  return mocked.matchAlbum.mock.calls[0];
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.suggestAlbum.mockImplementation(() => new Promise(() => undefined));
  mocked.previewImportTarget.mockReturnValue(new Promise(() => undefined) as never);
  mocked.decideImportReviewRemote.mockResolvedValue({ ok: true, decisions: [{ can_apply: true }] } as never);
  mocked.matchAlbum.mockResolvedValue({ ok: true, job_id: 'job-1' } as never);
  mocked.getJob.mockReturnValue(new Promise(() => undefined) as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('Missing IDs Match album with a prefilled Release Group (#580)', () => {
  it('sends the cached Release, not the bare Release Group UUID', async () => {
    expect(await matchWith(albumRow())).toEqual([7, REL]);
  });

  it('sends a release-group URL when no Release is cached', async () => {
    expect(await matchWith(albumRow({ mb_albumid: '' }))).toEqual([7, `https://musicbrainz.org/release-group/${RG}`]);
  });

  it('leaves any other id (a typed Release or URL) untouched', () => {
    expect(matchAlbumInput(albumRow(), REL)).toBe(REL);
    const other = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff';
    expect(matchAlbumInput(albumRow(), other)).toBe(other);
  });
});
