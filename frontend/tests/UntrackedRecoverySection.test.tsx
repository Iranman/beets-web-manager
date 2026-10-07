import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UntrackedRecoverySection } from '../src/features/libraryHealth/UntrackedRecoverySection';
import {
  applyTransaction,
  approveTransaction,
  getTransaction,
  getUntrackedCandidates,
  planUntrackedRecovery,
  rollbackTransaction,
} from '../src/api/client';
import type { TransactionDetail } from '../src/api/types';

vi.mock('../src/api/client', () => ({
  applyTransaction: vi.fn(),
  approveTransaction: vi.fn(),
  getTransaction: vi.fn(),
  getUntrackedCandidates: vi.fn(),
  getUntrackedInventorySummary: vi.fn(() => Promise.resolve({ ok: true, summary: null })),
  planUntrackedRecovery: vi.fn(),
  rollbackTransaction: vi.fn(),
  startUntrackedInventory: vi.fn(),
}));

const txWith = (rollback: TransactionDetail['rollback']) => ({
  ok: true,
  transaction: {
    id: 'op-1', created_at: 0, updated_at: 0, operation_type: 'untracked', status: 'Completed', dry_run: false,
    summary: '', confidence: {}, counts: {}, rollback, changes: [], changes_total: 0,
  } as TransactionDetail,
});

async function renderWithPlan() {
  vi.mocked(getUntrackedCandidates).mockResolvedValue({
    ok: true, total: 1, offset: 0, limit: 50,
    rows: [{ path: '/m/a.flac', action: 'quarantine', action_eligibility: 'eligible', reason: 'dup' }],
  } as never);
  vi.mocked(planUntrackedRecovery).mockResolvedValue({ ok: true, operation_id: 'op-1', action: 'quarantine' });
  render(<UntrackedRecoverySection />);
  fireEvent.click(await screen.findByRole('button', { name: 'Plan' }));
  return (await screen.findByRole('button', { name: 'Roll back' })) as HTMLButtonElement;
}

describe('UntrackedRecoverySection rollback gating (#250 QA F4)', () => {
  afterEach(() => { vi.resetAllMocks(); cleanup(); });

  it('keeps Roll back disabled before apply and explains why', async () => {
    const button = await renderWithPlan();
    expect(button.disabled).toBe(true);
    const reason = document.getElementById(button.getAttribute('aria-describedby')!);
    expect(reason?.textContent).toMatch(/after this plan is applied/);
    expect(getTransaction).not.toHaveBeenCalled();
  });

  it('enables Roll back when the transaction reports rollback.allowed after apply', async () => {
    await renderWithPlan();
    vi.mocked(approveTransaction).mockResolvedValue({ ok: true } as never);
    vi.mocked(applyTransaction).mockResolvedValue({ ok: true, status: 'Completed' } as never);
    vi.mocked(getTransaction).mockResolvedValue(txWith({ available: true, allowed: true, allowed_code: 'allowed', allowed_reason: '' }));
    vi.mocked(rollbackTransaction).mockResolvedValue({ ok: true, status: 'Rolled Back' } as never);
    fireEvent.click(screen.getByRole('button', { name: 'Approve and apply' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Roll back' }) as HTMLButtonElement).disabled).toBe(false));
    expect(getTransaction).toHaveBeenCalledWith('op-1');
    fireEvent.click(screen.getByRole('button', { name: 'Roll back' }));
    await waitFor(() => expect(rollbackTransaction).toHaveBeenCalledWith('op-1'));
  });

  it('shows the server reason and stays disabled when not allowed after a failed apply', async () => {
    await renderWithPlan();
    vi.mocked(approveTransaction).mockResolvedValue({ ok: true } as never);
    vi.mocked(applyTransaction).mockRejectedValue(new Error('Apply failed'));
    vi.mocked(getTransaction).mockResolvedValue(txWith({
      available: false, allowed: false, allowed_code: 'not_applied', allowed_reason: 'Nothing was applied, so there is nothing to roll back.',
    }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve and apply' }));
    await screen.findByText('Nothing was applied, so there is nothing to roll back.');
    expect((screen.getByRole('button', { name: 'Roll back' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
