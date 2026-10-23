// Automatic Find Match runs one request at a time: each holds a server worker
// for seconds, so stepping through the Review queue must not start one per item.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    suggestFolder: vi.fn(),
    previewImportTarget: vi.fn(),
    importWithId: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

const BUSY_TEXT = 'Find Match is already running for other items; try again in a moment.';

function pendingItem(album: string): ReviewItem {
  return {
    id: `pending:/downloads/Artist/${album}`,
    type: 'pending_ai',
    path: `/downloads/Artist/${album}`,
    album,
    artist: 'Artist',
  } as unknown as ReviewItem;
}

const mocked = vi.mocked(client);

function renderPage() {
  return render(
    <MemoryRouter>
      <ImportReviewPage active />
    </MemoryRouter>,
  );
}

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

async function moveTo(album: string) {
  fireEvent.keyDown(window, { key: ']' });
  await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: new RegExp(album) })).toBeTruthy());
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [pendingItem('First Album'), pendingItem('Second Album'), pendingItem('Third Album')],
    counts: { all: 3, pending_ai: 3 },
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('Import Review automatic Find Match', () => {
  it('runs one at a time and then serves the item under the cursor', async () => {
    const answers: Array<(value: unknown) => void> = [];
    mocked.suggestFolder.mockImplementation(() => new Promise((resolve) => { answers.push(resolve); }));
    renderPage();
    await screen.findAllByText('First Album');
    await waitFor(() => expect(mocked.suggestFolder).toHaveBeenCalledTimes(1));
    expect(mocked.suggestFolder).toHaveBeenLastCalledWith('/downloads/Artist/First Album');

    await moveTo('Second Album');
    await moveTo('Third Album');
    await settle();
    expect(mocked.suggestFolder).toHaveBeenCalledTimes(1);

    await act(async () => { answers[0]({ ok: true, suggestion: { mb_valid: false, reason: 'No match.' } }); });
    await waitFor(() => expect(mocked.suggestFolder).toHaveBeenCalledTimes(2));
    expect(mocked.suggestFolder).toHaveBeenLastCalledWith('/downloads/Artist/Third Album');
  });

  it('shows a busy server as a retryable warning and does not retry in a loop', async () => {
    const busy = Object.assign(new Error(BUSY_TEXT), {
      body: { ok: false, code: 'suggest_busy', retryable: true, error: BUSY_TEXT },
      httpStatus: 503,
    });
    mocked.suggestFolder.mockRejectedValue(busy);
    renderPage();
    await screen.findAllByText('First Album');
    const alert = (await screen.findByText(BUSY_TEXT)).closest('[role="alert"]');
    expect(alert?.className).toMatch(/warning/i);
    await settle();
    await settle();
    expect(mocked.suggestFolder).toHaveBeenCalledTimes(1);
  });

  it('tries a failed request again when the operator comes back to the item', async () => {
    mocked.suggestFolder.mockRejectedValue(new Error('Could not reach Beets Web Manager.'));
    renderPage();
    await screen.findAllByText('First Album');
    await waitFor(() => expect(mocked.suggestFolder).toHaveBeenCalledTimes(1));
    await moveTo('Second Album');
    await waitFor(() => expect(mocked.suggestFolder).toHaveBeenCalledTimes(2));
    fireEvent.keyDown(window, { key: '[' });
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: /First Album/ })).toBeTruthy());
    await waitFor(() => expect(mocked.suggestFolder).toHaveBeenCalledTimes(3));
    expect(mocked.suggestFolder).toHaveBeenLastCalledWith('/downloads/Artist/First Album');
  });
});
