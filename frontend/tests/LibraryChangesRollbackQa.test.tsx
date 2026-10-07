import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LibraryChanges from '../src/views/LibraryChanges';
import { approveTransaction, cancelTransaction, getTransaction, getTransactions } from '../src/api/client';
import type { TransactionDetail } from '../src/api/types';

vi.mock('../src/api/client', async (importActual) => ({
  apiErrorBody: (await importActual<typeof import('../src/api/client')>()).apiErrorBody,
  approveTransaction: vi.fn(),
  applyTransaction: vi.fn(),
  cancelTransaction: vi.fn(),
  getTransaction: vi.fn(),
  getTransactionSettings: vi.fn(() => new Promise(() => {})),
  getTransactions: vi.fn(),
  rollbackTransaction: vi.fn(),
  saveTransactionSettings: vi.fn(),
  transactionExportUrl: vi.fn(() => '#'),
}));

const base = (status: string, rollback: TransactionDetail['rollback']): TransactionDetail => ({
  id: 'tx-1', created_at: 0, updated_at: 0, operation_type: 'Metadata Update', status, dry_run: false,
  summary: 'Fix tags', confidence: {}, counts: {}, rollback, metadata: {}, changes: [], changes_total: 0,
});

// The server's approve/cancel/apply/rollback(job) responses embed the stored
// transaction WITHOUT rollback.allowed (routes_maintenance.py only adds it in
// the list and detail GETs). A current server must never be reported as an
// out-of-date one after an action.
describe('QA PR #250: action responses lack rollback.allowed', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup(); });

  it.each([
    ['Cancel', cancelTransaction, 'Preview', 'Cancelled', 'Transaction cancelled.'],
    ['Approve', approveTransaction, 'Preview', 'Approved', 'Transaction approved.'],
  ] as const)('after %s shows the server reason, not a version mismatch', async (name, fn, before, after, done) => {
    const reason = `Only a completed transaction can be rolled back (status is ${after}).`;
    const fresh = base(before, { available: true, allowed: false, allowed_code: 'not_completed', allowed_reason: `Only a completed transaction can be rolled back (status is ${before}).` });
    vi.mocked(getTransactions).mockResolvedValue({ ok: true, transactions: [fresh], total: 1 });
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: fresh });
    render(<LibraryChanges />);
    await screen.findByText('Fix tags', { selector: 'div.text-sm' });
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: base(after, { available: true, allowed: false, allowed_code: 'not_completed', allowed_reason: reason }) });
    vi.mocked(fn).mockResolvedValue({ ok: true, transaction: base(after, { available: true }) } as never);
    fireEvent.click(screen.getByRole('button', { name }));
    await screen.findByText(done);
    expect(screen.queryByText(/did not report whether this transaction can be rolled back/)).toBeNull();
    expect(screen.getByText((t) => t.includes(reason))).toBeTruthy();
  });
});
