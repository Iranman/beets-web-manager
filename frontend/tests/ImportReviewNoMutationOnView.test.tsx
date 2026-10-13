// 0.5.1 hotfix (REV-01/02/15/16): viewing a review item must never delete files
// or start an import; those happen only after an explicit button + confirmation.
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
    deleteReviewFolder: vi.fn(),
    deletePendingReview: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage, ReviewCard } from '../src/features/importReview/ImportReviewPage';

const RG = '11111111-1111-4111-8111-111111111111';
const REL = '22222222-2222-4222-8222-222222222222';
const FOLDER = '/downloads/Artist/Album';
const GOOD = `${FOLDER}/01 Good.flac`;
const EXTRA = `${FOLDER}/99 Extra.flac`;

// A high-confidence, importable item with one extra (rejected) file: before the
// hotfix, merely viewing it auto-quarantined EXTRA and auto-enqueued an import.
function reviewItem(id: string, album: string): ReviewItem {
  return {
    id,
    type: 'pending_ai',
    path: `${FOLDER}-${id}`,
    album,
    artist: 'Artist',
    blocked_reason: 'One extra file needs review.',
    suggestion: {
      mb_releasegroupid: RG,
      mb_albumid: REL,
      confidence_score: 0.97,
      track_mapping: [
        { status: 'matched', source_path: GOOD, local_title: 'Good', mb_title: 'Good' },
        { status: 'extra', source_path: EXTRA, local_title: 'Extra' },
      ],
    },
  } as unknown as ReviewItem;
}

const mocked = vi.mocked(client);

function renderPage(active = true) {
  return render(
    <MemoryRouter>
      <ImportReviewPage active={active} />
    </MemoryRouter>,
  );
}

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [reviewItem('a', 'First Album'), reviewItem('b', 'Second Album')],
    counts: { all: 2, pending_ai: 2 },
  } as never);
  mocked.previewImportTarget.mockResolvedValue({
    ok: true,
    safe: true,
    real_conflict_count: 0,
    tracks_to_import_count: 1,
    tracks: [{ status: 'matched', source_path: GOOD }],
  } as never);
  mocked.cleanupReviewFiles.mockResolvedValue({ ok: true, quarantined_count: 1, deleted_count: 0, remaining_audio_count: 1 } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('Import Review: viewing an item makes no mutating request', () => {
  it('does not clean up files or enqueue/start an import when an item is shown', async () => {
    renderPage();
    await screen.findAllByText('First Album');
    await settle();
    expect(mocked.previewImportTarget).toHaveBeenCalled();
    expect(mocked.cleanupReviewFiles).not.toHaveBeenCalled();
    expect(mocked.autoEnqueueImport).not.toHaveBeenCalled();
    expect(mocked.importWithId).not.toHaveBeenCalled();
    expect(mocked.reimportDisk).not.toHaveBeenCalled();
    expect(mocked.deleteReviewFolder).not.toHaveBeenCalled();
  });

  it('removes rejected files only after the button and the confirmation dialog', async () => {
    renderPage();
    const button = await screen.findByRole('button', { name: /Remove 1 rejected file…/ });
    fireEvent.click(button);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Quarantine Rejected Files')).toBeTruthy();
    expect(within(dialog).getByText(/moved to the import-review quarantine folder, not deleted/)).toBeTruthy();
    expect(within(dialog).getByText(EXTRA)).toBeTruthy();
    expect(mocked.cleanupReviewFiles).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await settle();
    expect(mocked.cleanupReviewFiles).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Remove 1 rejected file…/ }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Quarantine 1 file' }));
    await waitFor(() => expect(mocked.cleanupReviewFiles).toHaveBeenCalledTimes(1));
    expect(mocked.cleanupReviewFiles).toHaveBeenCalledWith({
      path: `${FOLDER}-a`,
      review_item_id: 'a',
      files: [EXTRA],
      action: 'quarantine_rejected',
      allow_delete: false,
    });
  });
});

describe('Import Review keyboard shortcuts', () => {
  it('ignores ] while a modifier key is held, honours it without one', async () => {
    renderPage();
    await screen.findAllByText('First Album');
    await settle();
    fireEvent.keyDown(window, { key: ']', ctrlKey: true });
    fireEvent.keyDown(window, { key: ']', metaKey: true });
    fireEvent.keyDown(window, { key: ']', altKey: true });
    await settle();
    expect(screen.getByRole('heading', { level: 2, name: /First Album/ })).toBeTruthy();
    fireEvent.keyDown(window, { key: ']' });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: /Second Album/ })).toBeTruthy());
  });

  it('ignores ] when the review panel is not the active tab', async () => {
    const view = renderPage(true);
    await screen.findAllByText('First Album');
    await settle();
    view.rerender(
      <MemoryRouter>
        <ImportReviewPage active={false} />
      </MemoryRouter>,
    );
    fireEvent.keyDown(window, { key: ']' });
    await settle();
    expect(screen.getByRole('heading', { level: 2, name: /First Album/ })).toBeTruthy();
  });
});

describe('CandidateCarousel arrow keys are scoped to the focused carousel', () => {
  it('ignores window-level and modified arrows; moves only from inside the carousel', () => {
    const noop = () => undefined;
    const item = {
      id: 'c1',
      type: 'pending_ai',
      path: FOLDER,
      evidence: {
        top_candidates: [
          { mb_albumid: REL, mb_releasegroupid: RG, album: 'Cand One', artist: 'Artist', score: 0.9 },
          { mb_albumid: '33333333-3333-4333-8333-333333333333', mb_releasegroupid: '44444444-4444-4444-8444-444444444444', album: 'Cand Two', artist: 'Artist', score: 0.8 },
        ],
      },
    } as unknown as ReviewItem;
    render(
      <MemoryRouter>
        <ReviewCard
          item={item}
          mbid=""
          manualFocusToken={0}
          onMbidChange={noop}
          onUseCandidate={noop}
          onFocusManualEntry={noop}
          onValidateManualId={noop}
          onClearManualId={noop}
          onSuggest={noop}
          onApply={noop}
          onDismiss={noop}
          onDeleteFolder={noop}
          onCleanupFiles={noop}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText('Match 1 of 2')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(screen.getByText('Match 1 of 2')).toBeTruthy();
    const carousel = screen.getByRole('group', { name: /Candidate matches/ });
    fireEvent.keyDown(carousel, { key: 'ArrowRight', shiftKey: true });
    expect(screen.getByText('Match 1 of 2')).toBeTruthy();
    fireEvent.keyDown(carousel, { key: 'ArrowRight' });
    expect(screen.getByText('Match 2 of 2')).toBeTruthy();
  });
});
