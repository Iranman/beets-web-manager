import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AlbumCleanupModal } from '../src/components/AlbumCleanupModal';
import { planAlbumCleanup, applyAlbumCleanup } from '../src/api/client';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

vi.mock('../src/api/client', () => ({
  planAlbumCleanup: vi.fn(),
  applyAlbumCleanup: vi.fn(),
}));

const mockPlan = vi.mocked(planAlbumCleanup);
const mockApply = vi.mocked(applyAlbumCleanup);

// Exactly the keys plan_album_cleanup (backend/composite_workflows.py)
// returns: album and items are the Beets web API rows it snapshotted.
function makePlanResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ok: true,
    operation_id: 'txn_1700000000_abcdef012345',
    token: 'txn_1700000000_abcdef012345',
    status: 'Preview',
    requires_approval: true,
    delete_files: false,
    album: {
      id: 42,
      album: 'Plan Album',
      albumartist: 'Plan Artist',
      mb_releasegroupid: 'rg-0000-plan',
      year: 2001,
    },
    items: [
      { id: 101, album_id: 42, title: 'First Song', artist: 'Plan Artist', track: 1, disc: 1, path: '/music/Plan Artist/Plan Album/01 First Song.flac' },
      { id: 102, album_id: 42, title: 'Second Song', artist: 'Plan Artist', track: 2, disc: 1, path: '/music/Plan Artist/Plan Album/02 Second Song.flac' },
    ],
    ...overrides,
  };
}

function renderModal(props: Partial<React.ComponentProps<typeof AlbumCleanupModal>> = {}) {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  const utils = render(
    <AlbumCleanupModal
      open
      albumId={42}
      albumTitle="Test Album"
      artistName="Test Artist"
      onClose={onClose}
      onSuccess={onSuccess}
      {...props}
    />,
  );
  return { ...utils, onClose, onSuccess };
}

describe('AlbumCleanupModal', () => {
  it('requests a plan for the given album as soon as it opens', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    await waitFor(() => expect(mockPlan).toHaveBeenCalledWith(42));
  });

  it('shows a loading state before the plan resolves, with no Apply button available yet', async () => {
    let resolvePlan: (v: ReturnType<typeof makePlanResponse>) => void = () => {};
    mockPlan.mockReturnValue(new Promise((resolve) => { resolvePlan = resolve; }));
    renderModal();

    expect(screen.getByText(/Generating authoritative album cleanup plan/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Apply Cleanup/i })).toBeNull();

    resolvePlan(makePlanResponse());
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
  });

  // QA F-2: the review step used to read target_path and
  // transaction.metadata.steps, which the plan never returns, so it showed an
  // empty directory and "Proposed Mutations (0 steps)".
  it('renders the album and tracks from the real plan shape', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    await waitFor(() => expect(screen.getByText(/Tracks to remove from the library \(2\)/i)).toBeTruthy());
    expect(screen.getByText('Plan Album')).toBeTruthy();
    expect(screen.getByText('Plan Artist')).toBeTruthy();
    expect(screen.getByText('rg-0000-plan')).toBeTruthy();
    expect(screen.getByText(/First Song$/)).toBeTruthy();
    expect(screen.getByText('/music/Plan Artist/Plan Album/02 Second Song.flac')).toBeTruthy();
    const text = screen.getByRole('dialog').textContent ?? '';
    expect(text).not.toMatch(/Target Album Directory/);
    expect(text).not.toMatch(/Proposed Mutations/);
    expect(text).toMatch(/audio files are kept on\s+disk/i);
  });

  it('says so when the plan has no tracks', async () => {
    mockPlan.mockResolvedValue(makePlanResponse({ items: [] }));
    renderModal();
    await waitFor(() => expect(screen.getByText(/Tracks to remove from the library \(0\)/i)).toBeTruthy());
    expect(screen.getByText(/Beets reported no tracks for this album/)).toBeTruthy();
  });

  it('never offers Apply for a plan that deletes files and points to the typed approval', async () => {
    mockPlan.mockResolvedValue(makePlanResponse({ delete_files: true }));
    renderModal();
    await waitFor(() => expect(screen.getByText(/This plan deletes files/)).toBeTruthy());
    expect(screen.getByText(/DELETE ALBUM FILES/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Apply Cleanup/i })).toBeNull();
    expect(screen.getByRole('dialog').textContent).not.toMatch(/kept on\s+disk/i);
  });

  it('shows the irreversible warning prominently before Apply is available', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    // Multiple elements legitimately say "Irreversible" (the banner, the
    // steps-list chip, and each per-step badge) -- assert on the specific
    // warning banner text, not just presence of the word anywhere.
    await waitFor(() => expect(screen.getByText(/remove the\s+album and its track records from the Beets library/i)).toBeTruthy());
    expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy();
  });

  // #184: planAlbumCleanup sends no delete_files, so the plan is row-only.
  // Nothing in this flow may claim that files are deleted.
  it('says the cleanup removes library rows only and keeps files, never that it deletes files', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({
      ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed',
      deleted: [], removed_item_ids: [1, 2], log: [],
    });
    renderModal();
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    const dialog = screen.getByRole('dialog');
    const deletesFiles = /delet\w* (every |the )?(catalogued )?(track |audio )?files?|files? deleted/i;
    expect(dialog.textContent).not.toMatch(deletesFiles);
    expect(dialog.textContent).toMatch(/files stay on disk/i);
    expect(screen.getByRole('heading', { name: 'Remove Album from Library' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));
    await waitFor(() => expect(screen.getByText(/Album Cleanup Completed/i)).toBeTruthy());
    expect(screen.getByRole('dialog').textContent).not.toMatch(deletesFiles);
    expect(screen.getByText(/Track row\(s\) removed from the library: 2/)).toBeTruthy();
    expect(screen.getByText(/Audio files kept on disk$/)).toBeTruthy();
  });

  it('does not call Apply until the user clicks Apply Cleanup', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    expect(mockApply).not.toHaveBeenCalled();
  });

  it('shows an applying state and calls Apply with the plan operation_id when confirmed', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    let resolveApply: (v: ReturnType<typeof makePlanResponse>) => void = () => {};
    mockApply.mockReturnValue(new Promise((resolve) => { resolveApply = resolve; }));
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(mockApply).toHaveBeenCalledWith('txn_1700000000_abcdef012345'));
    expect(screen.getByText(/Applying cleanup transaction/i)).toBeTruthy();
    // The Apply button must not still be present/clickable mid-flight.
    expect(screen.queryByRole('button', { name: /Apply Cleanup/i })).toBeNull();

    resolveApply({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', deleted: [], log: [] });
    await waitFor(() => expect(screen.getByText(/Album Cleanup Completed/i)).toBeTruthy());
  });

  it('shows the stale-plan path and truthfully says nothing changed only when error_kind is stale_plan', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({
      ok: false,
      error: 'The album changed after this cleanup plan was created. Nothing was changed. Generate a new plan to continue.',
      error_kind: 'stale_plan',
      mutated: false,
    });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByText(/Stale Plan Refused/i)).toBeTruthy());
    expect(screen.getByText(/Nothing was changed/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /Generate New Plan/i })).toBeTruthy();
  });

  it('generates a fresh plan when "Generate New Plan" is clicked after a stale refusal', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({
      ok: false,
      error: 'nothing changed',
      error_kind: 'stale_plan',
      mutated: false,
    });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Generate New Plan/i })).toBeTruthy());

    mockPlan.mockClear();
    mockPlan.mockResolvedValue(makePlanResponse());
    fireEvent.click(screen.getByRole('button', { name: /Generate New Plan/i }));
    await waitFor(() => expect(mockPlan).toHaveBeenCalledTimes(1));
  });

  it('shows a distinct, more urgent state for a partial-mutation failure, never the stale-plan copy', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({
      ok: false,
      error: 'The cleanup plan could not finish: some file(s) were already deleted before this error occurred.',
      error_kind: 'partial_mutation',
      mutated: true,
    });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByText(/Partially Modified/i)).toBeTruthy());
    expect(screen.queryByText(/^Nothing was changed/i)).toBeNull();
    expect(screen.queryByRole('button', { name: /Generate New Plan/i })).toBeNull();
  });

  it('shows a generic failure state for a non-staleness, non-partial error', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({
      ok: false,
      error: 'Beets database not found at /config/musiclibrary.blb',
      error_kind: 'other',
      mutated: false,
    });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByText(/Cleanup Operation Refused/i)).toBeTruthy());
    expect(screen.getByText(/musiclibrary\.blb/)).toBeTruthy();
  });

  it('refreshes the library and closes on "Done & Refresh View" after a successful Apply', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', deleted: [101], log: [] });
    const { onClose, onSuccess } = renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Done & Refresh View/i })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: /Done & Refresh View/i }));
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('says rollback is unavailable after a completed cleanup, with no rollback control', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', deleted: [], log: [] });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByText(/Rollback is unavailable for this transaction/i)).toBeTruthy());
    expect(screen.queryByRole('button', { name: /Rollback Cleanup/i })).toBeNull();
  });
  it('does not treat a thrown network error as "nothing changed"', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockRejectedValue(new Error('Failed to fetch'));
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByText(/Cleanup Operation Refused/i)).toBeTruthy());
    expect(screen.queryByText(/Nothing was changed/i)).toBeNull();
  });

  it('shows a failed state and lets the user retry the plan if planning itself fails', async () => {
    mockPlan.mockResolvedValue({ ok: false, error: 'Album 999 not found in database.' });
    renderModal();

    await waitFor(() => expect(screen.getByText(/Cleanup Operation Refused/i)).toBeTruthy());
    expect(screen.getByRole('button', { name: /Retry Plan/i })).toBeTruthy();
  });
});
