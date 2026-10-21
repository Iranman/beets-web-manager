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

// NOTE: callers set `library_version` explicitly. The real backend emits a
// STABLE version while cold (library_cache.generation) that only changes once
// reconciled disk state is ready -- so the frontend must treat "same version,
// still reconciling" as "keep polling" and must NOT reset its poll budget when
// the version does change. Hard-coding a constant version (as this test used
// to) masked the infinite-poll bug.
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

  it('keeps the SAME version across cold reconciled:false polls, backs off, then stops when a CHANGED reconciled:true tree lands', async () => {
    // Two cold responses carry the SAME library_version (7) -- modelling the
    // stable cold generation -- then the reconciled tree lands at a CHANGED
    // version (8). The budget must survive the stable version (so the delay
    // backs off 2s -> 4s) and the poll must stop once fresh.
    mockApiGet
      .mockResolvedValueOnce(libraryResponse({ reconciled: false, reconciling: true, library_version: 7 }))
      .mockResolvedValueOnce(libraryResponse({ reconciled: false, reconciling: true, library_version: 7 }))
      .mockResolvedValue(
        libraryResponse({
          reconciled: true,
          reconciling: false,
          library_version: 8,
          stats: { artists: 1, albums: 1, tracks: 0, disk_only_albums: 1 },
        }),
      );
    renderLibrary();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText(/Checking disk/)).toBeTruthy();

    // First poll fires at the 2s base cadence; version unchanged -> still cold.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
    expect(mockApiGet.mock.calls[1][0]).toBe('/api/library?include_disk_only=1');
    expect(screen.getByText(/Checking disk/)).toBeTruthy();

    // The budget was NOT reset by the stable version, so the next poll backs off
    // to 4s: 2s more elapses with no fetch...
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);

    // ...and only fires once the full 4s has passed. That response is the
    // reconciled:true tree at the advanced version, so polling stops.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(3);
    expect(screen.queryByText(/Checking disk/)).toBeNull();

    // Reconciled: polling must stop -- no further requests however long we wait.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(3);
  });

  it('polls a STALE cache (reconciled:true + reconciling:true) and refreshes the OPEN page when the background refresh lands', async () => {
    // Backend served a cache past its 180s TTL: it looks reconciled but is stale
    // and a background refresh is running (reconciling:true). The open page must
    // keep polling and swap in the fresh tree -- not sit on stale disk state
    // until the user re-navigates. (Under the old reconciled===false-only gate
    // this never polled, so the page froze on the stale payload.)
    mockApiGet
      .mockResolvedValueOnce(
        libraryResponse({
          reconciled: true,
          reconciling: true,
          library_version: 5,
          stats: { artists: 1, albums: 2, tracks: 9 },
        }),
      )
      .mockResolvedValue(
        libraryResponse({
          reconciled: true,
          reconciling: false,
          library_version: 6,
          stats: { artists: 1, albums: 3, tracks: 12 },
        }),
      );
    renderLibrary();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    // Stale cache painted immediately, and because it is still reconciling the
    // page keeps checking disk.
    expect(screen.getByText('2 albums')).toBeTruthy();
    expect(screen.getByText(/Checking disk/)).toBeTruthy();

    // The next background poll brings the refreshed tree; the OPEN page updates.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText('3 albums')).toBeTruthy();
    expect(screen.queryByText(/Checking disk/)).toBeNull();

    // Fresh (reconciled && !reconciling): polling stops.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
  });

  it('stops polling after a bounded number of attempts when a stable version never reconciles', async () => {
    // Stable cold version, reconciliation never completes: poll must back off and
    // stop at the bounded budget, surfacing a manual retry.
    mockApiGet.mockResolvedValue(libraryResponse({ reconciled: false, reconciling: true, library_version: 1 }));
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

  it('stays bounded even if library_version changes on EVERY poll (the old B2 bug)', async () => {
    // Regression guard: the old code reset the poll budget whenever
    // library_version changed, so a backend that churned the version on every
    // response would poll forever (and the stall/retry UI was unreachable).
    // With a never-reset budget the poll must still terminate at the stall UI.
    let version = 100;
    mockApiGet.mockImplementation(async () =>
      libraryResponse({ reconciled: false, reconciling: true, library_version: version++ }),
    );
    renderLibrary();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    let stalled = false;
    for (let i = 0; i < 120; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
      if (screen.queryByText(/Disk check incomplete/)) {
        stalled = true;
        break;
      }
    }

    // The stall/retry path is reachable despite the churning version...
    expect(stalled).toBe(true);
    // ...and polling is bounded (initial + <=15 polls), not infinite.
    expect(mockApiGet.mock.calls.length).toBeLessThanOrEqual(16);

    // And it stays stopped.
    const finalCalls = mockApiGet.mock.calls.length;
    for (let i = 0; i < 30; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
    }
    expect(mockApiGet.mock.calls.length).toBe(finalCalls);
  });
});
