import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TrackReplacementModal } from '../src/components/TrackReplacementModal';
import { applyTrackReplacement, planTrackReplacement, rollbackTrackReplacement } from '../src/api/client';

afterEach(cleanup);

vi.mock('../src/api/client', () => ({
  planTrackReplacement: vi.fn(),
  applyTrackReplacement: vi.fn(),
  rollbackTrackReplacement: vi.fn(),
}));

const mockPlan = vi.mocked(planTrackReplacement);
const mockApply = vi.mocked(applyTrackReplacement);
const mockRollback = vi.mocked(rollbackTrackReplacement);

function makePlanResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ok: true,
    operation_id: 'txn_1700000000_abcdef012345',
    reversibility: 'RECOVERABLE',
    plan: {
      id: 'txn_1700000000_abcdef012345',
      status: 'Preview',
      metadata: {
        original_path: '/music/Artist/Album/01.mp3',
        replacement_path: '/downloads/01.flac',
        rollback_available: true,
        matching_contract: {
          identity_source: 'acoustid_fingerprint',
          original_recording_id: 'aaaaaaaa-0000-0000-0000-000000000000',
          replacement_recording_id: 'aaaaaaaa-0000-0000-0000-000000000000',
          original_release_group_id: 'bbbbbbbb-0000-0000-0000-000000000000',
          decision_reason: 'Replacement AcoustID fingerprint matches original recording.',
        },
      },
    },
    ...overrides,
  };
}

function renderModal(props: Partial<React.ComponentProps<typeof TrackReplacementModal>> = {}) {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  const utils = render(
    <TrackReplacementModal
      open
      itemId={7}
      trackTitle="Test Track"
      originalPath="/music/Artist/Album/01.mp3"
      onClose={onClose}
      onSuccess={onSuccess}
      {...props}
    />,
  );
  return { ...utils, onClose, onSuccess };
}

describe('TrackReplacementModal', () => {
  it('does not call Plan until the user enters a candidate path and submits', () => {
    renderModal();
    expect(mockPlan).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Generate Plan/i })).toBeTruthy();
  });

  it('disables Generate Plan until a candidate path is entered', () => {
    renderModal();
    const button = screen.getByRole('button', { name: /Generate Plan/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    expect((screen.getByRole('button', { name: /Generate Plan/i }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('plans with the entered candidate path and shows the review step with real evidence', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();

    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));

    await waitFor(() => expect(mockPlan).toHaveBeenCalledWith(7, '/downloads/01.flac', undefined));
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Replacement/i })).toBeTruthy());
    expect(screen.getByText(/acoustid_fingerprint/i)).toBeTruthy();
    // The same recording id intentionally appears twice here (original and
    // verified-matching replacement) -- assert on the count, not a single match.
    expect(screen.getAllByText(/aaaaaaaa-0000-0000-0000-000000000000/i).length).toBe(2);
  });

  it('shows a refusal and no Apply button when Plan fails (e.g. unverified candidate)', async () => {
    mockPlan.mockResolvedValue({ ok: false, error: 'Could not verify the replacement candidate is the same recording via AcoustID fingerprint.' });
    renderModal();

    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/wrong-song.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));

    await waitFor(() => expect(screen.getByText(/Could not verify the replacement candidate/i)).toBeTruthy());
    expect(screen.queryByRole('button', { name: /Apply Replacement/i })).toBeNull();
  });

  it('does not call Apply until the user reviews the plan and clicks Apply Replacement', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    renderModal();
    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Replacement/i })).toBeTruthy());
    expect(mockApply).not.toHaveBeenCalled();
  });

  it('applies with the item id and operation id, then shows completion with rollback available', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed', quarantined_to: '/config/track_replacement_quarantine/library/20260101/txn_1700000000_abcdef012345/01.mp3' });
    renderModal();

    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply Replacement/i })).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: /Apply Replacement/i }));
    await waitFor(() => expect(mockApply).toHaveBeenCalledWith(7, 'txn_1700000000_abcdef012345'));
    await waitFor(() => expect(screen.getByText(/Replacement Completed/i)).toBeTruthy());
    expect(screen.getByRole('button', { name: /Rollback Replacement/i })).toBeTruthy();
  });

  it('rolling back calls the rollback API and shows the result', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Completed' });
    mockRollback.mockResolvedValue({ ok: true, operation_id: 'txn_1700000000_abcdef012345', status: 'Rolled Back' });
    renderModal();

    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));
    await waitFor(() => screen.getByRole('button', { name: /Apply Replacement/i }));
    fireEvent.click(screen.getByRole('button', { name: /Apply Replacement/i }));
    await waitFor(() => screen.getByRole('button', { name: /Rollback Replacement/i }));

    fireEvent.click(screen.getByRole('button', { name: /Rollback Replacement/i }));
    await waitFor(() => expect(mockRollback).toHaveBeenCalledWith('txn_1700000000_abcdef012345'));
    await waitFor(() => expect(screen.getByText(/Rollback executed/i)).toBeTruthy());
  });

  it('shows a distinct partial-mutation state and never claims nothing changed when mutated is true', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    mockApply.mockResolvedValue({ ok: false, error: 'Database update failed after the original file was already quarantined.', code: 'db_update_failed', mutated: true });
    renderModal();

    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));
    await waitFor(() => screen.getByRole('button', { name: /Apply Replacement/i }));
    fireEvent.click(screen.getByRole('button', { name: /Apply Replacement/i }));

    await waitFor(() => expect(screen.getByText(/Did Not Complete/i)).toBeTruthy());
    expect(screen.queryByText(/Nothing was changed/i)).toBeNull();
  });

  it('hides the rollback control when the plan reports rollback_available: false', async () => {
    mockPlan.mockResolvedValue(makePlanResponse({
      plan: {
        id: 'txn_x',
        status: 'Preview',
        metadata: { original_path: '/music/a.mp3', replacement_path: '/downloads/a.flac', rollback_available: false, matching_contract: { identity_source: 'acoustid_fingerprint', replacement_recording_id: 'x' } },
      },
    }));
    mockApply.mockResolvedValue({ ok: true, operation_id: 'txn_x', status: 'Completed' });
    renderModal();

    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/a.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));
    await waitFor(() => screen.getByRole('button', { name: /Apply Replacement/i }));
    fireEvent.click(screen.getByRole('button', { name: /Apply Replacement/i }));

    await waitFor(() => expect(screen.getByText(/Replacement Completed/i)).toBeTruthy());
    expect(screen.queryByRole('button', { name: /Rollback Replacement/i })).toBeNull();
    expect(screen.getByText(/Rollback is not available/i)).toBeTruthy();
  });

  it('resets internal state back to the input step when closed', async () => {
    mockPlan.mockResolvedValue(makePlanResponse());
    const { onClose } = renderModal();
    fireEvent.change(screen.getByLabelText(/Replacement file path/i), { target: { value: '/downloads/01.flac' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate Plan/i }));
    await waitFor(() => screen.getByRole('button', { name: /Apply Replacement/i }));

    // The header Close button is the only "Close" role at the review step.
    fireEvent.click(screen.getByRole('button', { name: /^Close$/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    // handleClose() resets internal step state synchronously before
    // calling onClose -- a fresh mount (matching what actually happens
    // when the parent stops rendering this album's track and later opens
    // it again for a different track) must start at 'input', never a
    // stale 'review'/'completed' step from the previous track.
    cleanup();
    renderModal();
    expect(screen.getByRole('button', { name: /Generate Plan/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Apply Replacement/i })).toBeNull();
  });
});
