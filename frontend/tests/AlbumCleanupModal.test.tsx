import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AlbumCleanupModal } from '../src/components/AlbumCleanupModal';
import { planAlbumCleanup, applyAlbumCleanup, rollbackAlbumCleanup } from '../src/api/client';

afterEach(cleanup);

vi.mock('../src/api/client', () => ({
  planAlbumCleanup: vi.fn(),
  applyAlbumCleanup: vi.fn(),
  rollbackAlbumCleanup: vi.fn(),
}));

const mockPlan = vi.mocked(planAlbumCleanup);
const mockApply = vi.mocked(applyAlbumCleanup);
const mockRollback = vi.mocked(rollbackAlbumCleanup);

function makePlanResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ok: true,
    operation_id: 'txn_1700000000_abcdef012345',
    status: 'Preview',
    album_id: 42,
    target_path: '/music/Artist/Album',
    file_count: 2,
    transaction: {
      metadata: {
        reversibility: 'IRREVERSIBLE',
        rollback_available: false,
        steps: [
          { step_id: 'step_1', type: 'delete_file', source: '/music/Artist/Album/01.flac', status: 'pending', reversibility: 'IRREVERSIBLE' },
          { step_id: 'step_2', type: 'delete_file', source: '/music/Artist/Album/02.flac', status: 'pending', reversibility: 'IRREVERSIBLE' },
        ],
      },
    },
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

  it('itemizes the real plan steps, not a generic placeholder', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    await waitFor(() => expect(screen.getByText(/Proposed Mutations \(2 steps\)/i)).toBeTruthy());
    expect(screen.getByText(/01\.flac/)).toBeTruthy();
    expect(screen.getByText(/02\.flac/)).toBeTruthy();
  });

  it('shows the irreversible warning prominently before Apply is available', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    // Multiple elements legitimately say "Irreversible" (the banner, the
    // steps-list chip, and each per-step badge) -- assert on the specific
    // warning banner text, not just presence of the word anywhere.
    await waitFor(() => expect(screen.getByText(/permanently delete every catalogued track file/i)).toBeTruthy());
    expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy();
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
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', deleted: ['/music/Artist/Album/01.flac'], log: [] });
    const { onClose, onSuccess } = renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Done & Refresh View/i })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: /Done & Refresh View/i }));
    expect(onSuccess).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('hides the rollback button when the transaction reports rollback_available: false', async () => {
    mockPlan.mockResolvedValue(makePlanResponse()); // rollback_available: false
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', deleted: [], log: [] });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByText(/Rollback is unavailable for this transaction/i)).toBeTruthy());
    expect(screen.queryByRole('button', { name: /Rollback Cleanup/i })).toBeNull();
  });

  it('shows a rollback control only when the transaction truthfully reports rollback_available: true, and it calls the rollback API', async () => {
    mockPlan.mockResolvedValue(
      makePlanResponse({
        transaction: {
          metadata: {
            reversibility: 'RECOVERABLE',
            rollback_available: true,
            steps: [],
          },
        },
      }),
    );
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', deleted: [], log: [] });
    mockRollback.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Rolled Back', restored: [], log: [] });
    renderModal();

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Apply Cleanup/i }));

    await waitFor(() => expect(screen.getByRole('button', { name: /Rollback Cleanup/i })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /Rollback Cleanup/i }));
    await waitFor(() => expect(mockRollback).toHaveBeenCalledWith('txn_1700000000_abcdef012345'));
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
