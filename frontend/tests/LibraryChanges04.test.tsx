import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LibraryChanges from '../src/views/LibraryChanges';
import {
  cancelTransaction, getTransaction, getTransactionSettings, getTransactions, rollbackTransaction, saveTransactionSettings,
} from '../src/api/client';
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

const tx = (status: string, extra: Partial<TransactionDetail> = {}): TransactionDetail => ({
  id: 'tx-1', created_at: 0, updated_at: 0, operation_type: 'Album Move', status, dry_run: false,
  summary: 'Move album', confidence: {}, counts: {}, rollback: { available: true, allowed: true }, metadata: {},
  changes: [], changes_total: 0, ...extra,
});

function serve(detail: TransactionDetail) {
  vi.mocked(getTransactions).mockResolvedValue({ ok: true, transactions: [detail], total: 1 });
  vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: detail });
}

function apiError(message: string, httpStatus: number, body: Record<string, unknown>) {
  return Object.assign(new Error(message), { httpStatus, body });
}

describe('Library Changes 0.4 transaction contract (#348)', () => {
  afterEach(() => { vi.restoreAllMocks(); cleanup(); });

  it('offers Retry Rollback for Partially Rolled Back and explains a partial result', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    serve(tx('Partially Rolled Back'));
    vi.mocked(rollbackTransaction).mockRejectedValue(
      apiError('Only 1 of 2 quarantined files were restored.', 400, { ok: false, code: 'partial', status: 'Partially Rolled Back' }));
    render(<LibraryChanges />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry Rollback' }));
    expect(await screen.findByText(/finished only part-way/)).toBeTruthy();
    expect(screen.queryByText(/Do not roll back again/)).toBeNull();
  });

  it('shows restored and adopted counts after a rollback', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    serve(tx('Completed'));
    vi.mocked(rollbackTransaction).mockResolvedValue({ ok: true, restored: 3, adopted: 1 } as never);
    render(<LibraryChanges />);
    fireEvent.click(await screen.findByRole('button', { name: 'Rollback' }));
    expect(await screen.findByText(/3 items restored \(1 were already back/)).toBeTruthy();
  });

  it('allows cancelling an orphaned library sync and reports the engine cancel', async () => {
    serve(tx('Recovery Required', {
      rollback: { available: false, allowed: false, allowed_reason: 'No.' },
      metadata: { mutation_family: 'mbsync_library_v1', engine_operation_id: 'op-1' },
    }));
    vi.mocked(cancelTransaction).mockResolvedValue({ ok: true, code: 'engine_cancelling' } as never);
    render(<LibraryChanges />);
    const cancel = await screen.findByRole('button', { name: 'Cancel' });
    expect((cancel as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(cancel);
    expect(await screen.findByText(/Cancel sent to Beets/)).toBeTruthy();
  });

  it('keeps Cancel disabled for a running transaction of another family', async () => {
    serve(tx('Running', { metadata: { mutation_family: 'album_relocation_v1' } }));
    render(<LibraryChanges />);
    expect(((await screen.findByRole('button', { name: 'Cancel' })) as HTMLButtonElement).disabled).toBe(true);
  });

  it('reverts a rejected setting and shows the server message', async () => {
    serve(tx('Completed'));
    vi.mocked(getTransactionSettings).mockResolvedValue({ ok: true, settings: {
      enabled: true, backups_enabled: true, rollback_enabled: true, backup_retention_days: 30,
      automatic_approval_threshold: 0.9, require_review_below_threshold: true, maximum_undo_history: 100, dry_run_by_default: false,
    } });
    vi.mocked(saveTransactionSettings).mockRejectedValue(apiError('backup_retention_days must be between 1 and 3650.', 400, { code: 'invalid_setting' }));
    render(<LibraryChanges />);
    const box = await screen.findByRole('checkbox', { name: 'Backups' });
    fireEvent.click(box);
    expect(await screen.findByText(/Setting not saved: backup_retention_days must be between 1 and 3650/)).toBeTruthy();
    expect((box as HTMLInputElement).checked).toBe(true);
  });
});
