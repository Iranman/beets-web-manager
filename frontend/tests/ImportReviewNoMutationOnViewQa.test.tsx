// QA edge cases for the 0.5.1 Review Queue hotfix (REV-01/02): browsing between items,
// a hidden (inactive) panel and the permanent-delete path must all stay explicit.
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
    cleanupReviewFiles: vi.fn(),
    autoEnqueueImport: vi.fn(),
    importWithId: vi.fn(),
    reimportDisk: vi.fn(),
    matchAlbum: vi.fn(),
    deleteReviewFolder: vi.fn(),
    deletePendingReview: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

const RG = '11111111-1111-4111-8111-111111111111';
const REL = '22222222-2222-4222-8222-222222222222';
const FOLDER = '/downloads/Artist/Album';
const GOOD = `${FOLDER}/01 Good.flac`;
const WRONG = `${FOLDER}/02 Wrong.flac`;

function reviewItem(id: string, album: string, wrongStatus: 'extra' | 'different'): ReviewItem {
  return {
    id,
    type: 'pending_ai',
    path: `${FOLDER}-${id}`,
    album,
    artist: 'Artist',
    suggestion: {
      mb_releasegroupid: RG,
      mb_albumid: REL,
      confidence_score: 0.97,
      track_mapping: [
        { status: 'matched', source_path: GOOD, local_title: 'Good', mb_title: 'Good' },
        { status: wrongStatus, source_path: WRONG, local_title: 'Wrong', mb_title: 'Other' },
      ],
    },
  } as unknown as ReviewItem;
}

const mocked = vi.mocked(client);
const MUTATORS = ['cleanupReviewFiles', 'autoEnqueueImport', 'importWithId', 'reimportDisk', 'matchAlbum', 'deleteReviewFolder', 'deletePendingReview'] as const;

function expectNoMutation() {
  for (const name of MUTATORS) expect(mocked[name], name).not.toHaveBeenCalled();
}

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

function page(active: boolean) {
  return (
    <MemoryRouter>
      <ImportReviewPage active={active} />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [reviewItem('a', 'First Album', 'extra'), reviewItem('b', 'Second Album', 'different')],
    counts: { all: 2, pending_ai: 2 },
  } as never);
  mocked.previewImportTarget.mockResolvedValue({
    ok: true,
    safe: true,
    real_conflict_count: 0,
    tracks_to_import_count: 1,
    tracks: [{ status: 'matched', source_path: GOOD }],
  } as never);
  mocked.cleanupReviewFiles.mockResolvedValue({ ok: true, quarantined_count: 0, deleted_count: 1, remaining_audio_count: 1 } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('Import Review QA: no mutation while browsing', () => {
  it('browsing to the next item does not clean up or import it', async () => {
    render(page(true));
    await screen.findAllByText('First Album');
    await settle();
    fireEvent.keyDown(window, { key: ']' });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: /Second Album/ })).toBeTruthy());
    await settle();
    expect(mocked.previewImportTarget).toHaveBeenCalledTimes(2);
    expectNoMutation();
  });

  it('a mounted but inactive panel sends nothing that mutates', async () => {
    const view = render(page(false));
    await settle();
    view.rerender(page(false));
    await settle();
    expectNoMutation();
  });
});

describe('Import Review QA: permanent delete needs its own explicit confirmation', () => {
  it('names permanent deletion and sends delete_rejected only after confirming', async () => {
    render(page(true));
    await screen.findAllByText('First Album');
    fireEvent.keyDown(window, { key: ']' });
    const button = await screen.findByRole('button', { name: /Remove 1 rejected file…/ });
    await settle();
    expectNoMutation();

    fireEvent.click(button);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Permanently Delete Rejected Files')).toBeTruthy();
    expect(within(dialog).getByText(/permanently deleted from disk/)).toBeTruthy();
    expect(within(dialog).getByText(WRONG)).toBeTruthy();
    expect(mocked.cleanupReviewFiles).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete 1 file' }));
    await waitFor(() => expect(mocked.cleanupReviewFiles).toHaveBeenCalledTimes(1));
    expect(mocked.cleanupReviewFiles).toHaveBeenCalledWith({
      path: `${FOLDER}-b`,
      review_item_id: 'b',
      files: [WRONG],
      action: 'delete_rejected',
      allow_delete: true,
    });
  });
});
