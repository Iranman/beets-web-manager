// #275: the review-folder delete success message read "Deleted undefined audio
// file(s)" because it used a field POST /api/import/review-folder/delete never
// returns. The endpoint returns operation_id, deleted[], quarantined[],
// files_removed and status.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    previewImportTarget: vi.fn(),
    getFolderStats: vi.fn(),
    deleteReviewFolder: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { deleteFolderResultMessage, ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

const FOLDER = '/downloads/Artist/Album';
const mocked = vi.mocked(client);

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [{ id: 'a', type: 'pending_ai', path: FOLDER, album: 'Album', artist: 'Artist' } as unknown as ReviewItem],
    counts: { all: 1, pending_ai: 1 },
  } as never);
  mocked.previewImportTarget.mockResolvedValue({ ok: true, safe: true, tracks: [] } as never);
  mocked.getFolderStats.mockResolvedValue({ ok: true, path: FOLDER, exists: true, audio_count: 2, total_count: 3 } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('deleteFolderResultMessage', () => {
  it('reports deleted and quarantined counts from the real response shape', () => {
    expect(deleteFolderResultMessage({
      ok: true, operation_id: 'op1', status: 'Completed', files_removed: 3,
      deleted: ['/d/a.flac', '/d/b.flac'], quarantined: ['/m/c.flac'],
    })).toBe('Source folder removed: 3 files (2 deleted, 1 moved to quarantine).');
  });

  it('falls back to list lengths when files_removed is missing', () => {
    expect(deleteFolderResultMessage({ ok: true, deleted: ['/d/a.flac'] }))
      .toBe('Source folder removed: 1 file (1 deleted).');
  });

  it('uses files_removed alone when the lists are missing', () => {
    expect(deleteFolderResultMessage({ ok: true, files_removed: 4 })).toBe('Source folder removed: 4 files.');
  });

  it('never shows undefined or NaN for zero or missing counts', () => {
    for (const result of [{ ok: true }, { ok: true, files_removed: 0, deleted: [], quarantined: [] }]) {
      const message = deleteFolderResultMessage(result);
      expect(message).toBe('Source folder removed. No files needed to be deleted or quarantined.');
    }
  });
});

describe('Import Review: Delete Folder success message', () => {
  it('shows the real file count after a confirmed delete', async () => {
    mocked.deleteReviewFolder.mockResolvedValue({
      ok: true, operation_id: 'op1', status: 'Completed', files_removed: 3,
      deleted: [`${FOLDER}/01.flac`, `${FOLDER}/02.flac`, `${FOLDER}/cover.jpg`], quarantined: [],
    });
    render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
    const btn = await screen.findByRole('button', { name: 'Delete Folder' });
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(btn);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete Folder' }));
    await waitFor(() => expect(mocked.deleteReviewFolder).toHaveBeenCalledTimes(1));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });

    // The page status line carries the message; the item card is gone (or,
    // here, re-listed by the mocked reload), so its card message is not enough.
    await waitFor(() => {
      const lines = screen.queryAllByRole('status').map((el) => el.textContent);
      expect(lines).toContain('Source folder removed: 3 files (3 deleted).');
    });
    expect(document.body.textContent).not.toMatch(/undefined/);
  });
});
