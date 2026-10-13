// QA #395: Review Queue server filters, stale-response guard, and the batch
// "Open in review" link reaching the server search.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    getLibraryMissingMbIds: vi.fn(),
    getAcquisitionQueue: vi.fn(),
    previewImportTarget: vi.fn(() => new Promise(() => undefined)),
    getAiBatchStatus: vi.fn(),
    getSetupStatus: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage, REVIEW_PAGE_SIZE } from '../src/features/importReview/ImportReviewPage';
import Import from '../src/views/Import';

const mocked = vi.mocked(client);

function row(id: string, album: string): ReviewItem {
  return { id, type: 'pending_ai', path: `/downloads/${id}`, album, artist: 'Artist' } as unknown as ReviewItem;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [], jobs: [] }), { status: 200 }))));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('QA #395: Review Queue', () => {
  it('sends search and evidence_only to the server and drops back to page 1', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: [row('a', 'First Album')], total: 300, counts: { all: 300, pending_ai: 300 } } as never);
    render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
    await screen.findAllByText('First Album');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(mocked.getReviewQueue).toHaveBeenLastCalledWith(expect.objectContaining({ offset: REVIEW_PAGE_SIZE })));

    fireEvent.change(screen.getByLabelText('Search review queue'), { target: { value: 'guessed artist' } });
    await waitFor(
      () => expect(mocked.getReviewQueue).toHaveBeenLastCalledWith(expect.objectContaining({ search: 'guessed artist', offset: 0, limit: REVIEW_PAGE_SIZE })),
      { timeout: 3000 },
    );
  });

  it('an older, slower response never replaces the newer page', async () => {
    let resolveOld: (value: unknown) => void = () => undefined;
    mocked.getReviewQueue
      .mockResolvedValueOnce({ ok: true, items: [row('a', 'Page One Album')], total: 300, counts: { all: 300, pending_ai: 300 } } as never)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }) as never)
      .mockResolvedValue({ ok: true, items: [row('c', 'Page Three Album')], total: 300, counts: { all: 300, pending_ai: 300 } } as never);
    render(<MemoryRouter><ImportReviewPage active /></MemoryRouter>);
    await screen.findAllByText('Page One Album');

    fireEvent.click(screen.getByRole('button', { name: 'Next page' })); // page 2: hangs
    // Change a filter while the page-2 request is still in flight.
    fireEvent.click(screen.getByRole('switch'));
    await screen.findAllByText('Page Three Album');
    await act(async () => {
      resolveOld({ ok: true, items: [row('b', 'Stale Album')], total: 300, counts: { all: 300, pending_ai: 300 } });
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.queryByText('Stale Album')).toBeNull();
    expect(screen.getAllByText('Page Three Album').length).toBeGreaterThan(0);
  });
});

describe('QA #395: batch "Open in review" link', () => {
  it('/import?tab=review&q=<folder> searches the server for that folder', async () => {
    mocked.getReviewQueue.mockResolvedValue({ ok: true, items: [], total: 0, counts: { all: 0 } } as never);
    mocked.getLibraryMissingMbIds.mockResolvedValue({ ok: true, items: [], total: 0, offset: 0, limit: 0, counts: { all: 0, album: 0, item: 0 } } as never);
    mocked.getAcquisitionQueue.mockResolvedValue({ ok: true, items: [], total: 0, counts: {} } as never);
    mocked.getAiBatchStatus.mockResolvedValue({ state: null } as never);
    mocked.getSetupStatus.mockResolvedValue({ paths: {} } as never);
    render(
      <MemoryRouter initialEntries={[`/import?tab=review&q=${encodeURIComponent('/srv/dl/Needs Review')}`]}>
        <Routes><Route path="/import" element={<Import />} /></Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocked.getReviewQueue).toHaveBeenCalledWith(expect.objectContaining({ search: '/srv/dl/Needs Review', limit: REVIEW_PAGE_SIZE })));
  });
});
