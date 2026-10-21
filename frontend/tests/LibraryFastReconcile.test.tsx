import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import Library from '../src/views/Library';
import { apiGet } from '../src/lib/api';
import type { LibraryResponse } from '../src/types/api';

// Library.tsx fetches /api/library through the low-level apiGet helper, and
// pulls art-repair / artist-image / album-track data through api/client. Stub
// the client calls as never-resolving so they stay out of the way; the test is
// only about the /api/library fast-path + background disk reconciliation poll.
vi.mock('../src/lib/api', () => ({ apiGet: vi.fn() }));
vi.mock('../src/api/client', () => ({
  getArtistImageUrl: vi.fn(() => new Promise(() => {})),
  getLibraryArtRepairReport: vi.fn(() => new Promise(() => {})),
  getAlbumTracks: vi.fn(() => new Promise(() => {})),
}));

const mockApiGet = vi.mocked(apiGet);

function libraryResponse(overrides: Partial<LibraryResponse> = {}): LibraryResponse {
  return {
    ok: true,
    artists: [{ name: 'Radiohead', albums: [] }],
    stats: { artists: 1, albums: 0, tracks: 0 },
    library_version: 1,
    ...overrides,
  };
}

function renderLibrary() {
  return render(
    <MemoryRouter>
      <Library />
    </MemoryRouter>,
  );
}

describe('Library fast-path + disk reconciliation polling', () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it('paints immediately from the fast reconciled:false tree and shows a non-blocking disk-check indicator', async () => {
    mockApiGet.mockResolvedValue(libraryResponse({ reconciled: false, reconciling: true }));
    renderLibrary();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    // Imported content rendered without waiting on any disk walk.
    expect(screen.getByText('1 artists')).toBeTruthy();
    // Subtle, non-blocking "checking disk" indicator is present.
    expect(screen.getByText(/Checking disk/)).toBeTruthy();
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockApiGet.mock.calls[0][0]).toBe('/api/library?include_disk_only=1');
  });

  it('polls until reconciled becomes true, then drops the indicator and stops polling', async () => {
    mockApiGet
      .mockResolvedValueOnce(libraryResponse({ reconciled: false, reconciling: true }))
      .mockResolvedValue(
        libraryResponse({
          reconciled: true,
          stats: { artists: 1, albums: 1, tracks: 0, disk_only_albums: 1 },
        }),
      );
    renderLibrary();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText(/Checking disk/)).toBeTruthy();

    // Background poll fires after the base cadence (2s) and does NOT request a
    // heavy track pull (tracks were never loaded).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
    expect(mockApiGet.mock.calls[1][0]).toBe('/api/library?include_disk_only=1');
    expect(screen.queryByText(/Checking disk/)).toBeNull();

    // Reconciled: polling must stop — no further requests however long we wait.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
  });

  it('stops polling after a bounded number of attempts when disk state never arrives', async () => {
    mockApiGet.mockResolvedValue(libraryResponse({ reconciled: false, reconciling: true }));
    renderLibrary();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    // Step the clock so React re-renders between ticks (how real timers behave):
    // the backoff grows the delay each poll until the bounded budget (15 polls,
    // capped at a 15s cadence) is exhausted. Loop past the worst-case total.
    for (let i = 0; i < 120; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
      if (screen.queryByText(/Disk check incomplete/)) break;
    }

    const callsWhenStalled = mockApiGet.mock.calls.length;
    expect(callsWhenStalled).toBeGreaterThan(1); // it did poll
    expect(callsWhenStalled).toBeLessThanOrEqual(16); // initial + <=15 polls

    // Budget exhausted: no more polling however long we wait.
    for (let i = 0; i < 30; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
    }
    expect(mockApiGet).toHaveBeenCalledTimes(callsWhenStalled);
    // A stalled disk check is surfaced with a manual retry, not a spinner.
    expect(screen.getByText(/Disk check incomplete/)).toBeTruthy();
  });
});
