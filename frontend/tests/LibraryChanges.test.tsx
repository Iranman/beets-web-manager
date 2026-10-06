import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LibraryChanges from '../src/views/LibraryChanges';
import { approveTransaction, getTransaction, getTransactions } from '../src/api/client';
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

const mockApprove = vi.mocked(approveTransaction);
const PHRASE = 'DELETE ALBUM FILES';

function tx(metadata: Record<string, unknown>, status = 'Preview'): TransactionDetail {
  return {
    id: 'tx-1',
    created_at: 0,
    updated_at: 0,
    operation_type: 'album_cleanup',
    status,
    dry_run: false,
    summary: 'Clean up album',
    confidence: {},
    counts: {},
    rollback: { available: false },
    metadata,
    changes: [],
    changes_total: 0,
  };
}

const deleting = { mutation_family: 'album_cleanup_v1', delete_files: true };

async function renderWith(detail: TransactionDetail) {
  vi.mocked(getTransactions).mockResolvedValue({ ok: true, transactions: [detail], total: 1 });
  vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: detail });
  render(<LibraryChanges />);
  await screen.findByText('Clean up album', { selector: 'div.text-sm' });
}

function apiError(code: string, status: number) {
  return Object.assign(new Error(code), { body: { code, error: code }, httpStatus: status });
}

describe('LibraryChanges approve', () => {
  beforeEach(() => {
    mockApprove.mockReset();
  });
  afterEach(cleanup);

  it('approves row-only plans without asking for or sending the phrase', async () => {
    await renderWith(tx({ mutation_family: 'album_cleanup_v1', delete_files: false }));
    mockApprove.mockResolvedValue({ ok: true, transaction: tx({}, 'Approved') });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await screen.findByText('Transaction approved.');
    expect(mockApprove).toHaveBeenCalledWith('tx-1', undefined);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('requires the exact phrase before approving a file-deleting plan', async () => {
    await renderWith(tx(deleting));
    mockApprove.mockResolvedValue({ ok: true, transaction: tx(deleting, 'Approved') });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    const dialog = await screen.findByRole('dialog');
    const input = screen.getByLabelText(/to approve/);
    await waitFor(() => expect(document.activeElement).toBe(input));
    const confirm = screen.getByRole('button', { name: 'Approve deletion' });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'delete album files' } });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: PHRASE } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    expect(mockApprove).not.toHaveBeenCalled();

    fireEvent.click(confirm);
    await screen.findByText('Transaction approved.');
    expect(mockApprove).toHaveBeenCalledWith('tx-1', PHRASE);
    await waitFor(() => expect(dialog.isConnected).toBe(false));
  });

  it('announces the consequence text as the dialog description', async () => {
    await renderWith(tx(deleting));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    const ids = (dialog.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean);
    expect(ids.length).toBeGreaterThan(0);
    const description = ids.map((id) => document.getElementById(id)?.textContent ?? '').join(' ');
    expect(description).toMatch(/permanently deletes the album's audio files from disk/);
    expect(description).toMatch(/cannot be rolled back/);
  });

  it('closes on Escape without approving', async () => {
    await renderWith(tx(deleting));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(mockApprove).not.toHaveBeenCalled();
  });

  it('explains a confirmation_required refusal', async () => {
    await renderWith(tx(deleting));
    mockApprove.mockRejectedValue(apiError('confirmation_required', 400));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.change(await screen.findByLabelText(/to approve/), { target: { value: PHRASE } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve deletion' }));
    await screen.findByText(/server did not accept the confirmation\. Nothing was approved/);
    expect(screen.queryByText('Transaction approved.')).toBeNull();
  });

  it('explains a lost state-transition race', async () => {
    await renderWith(tx({}));
    mockApprove.mockRejectedValue(apiError('not_preview', 409));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await screen.findByText(/no longer in Preview/);
    expect(screen.queryByText('Transaction approved.')).toBeNull();
  });
});
