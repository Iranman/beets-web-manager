import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LibraryChanges from '../src/views/LibraryChanges';
import { applyTransaction, approveTransaction, cancelTransaction, getTransaction, getTransactions, rollbackTransaction } from '../src/api/client';
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

describe('LibraryChanges cancel/apply refusals', () => {
  const mockCancel = vi.mocked(cancelTransaction);
  const mockApply = vi.mocked(applyTransaction);
  const onUnhandled = vi.fn();

  beforeEach(() => {
    mockCancel.mockReset();
    mockApply.mockReset();
    onUnhandled.mockReset();
    process.on('unhandledRejection', onUnhandled);
  });
  afterEach(() => {
    process.off('unhandledRejection', onUnhandled);
    cleanup();
  });

  it('shows a 409 not_cancellable refusal and reloads the transaction', async () => {
    await renderWith(tx({}, 'Approved'));
    vi.mocked(getTransaction).mockClear();
    vi.mocked(getTransactions).mockClear();
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: tx({}, 'Running') });
    mockCancel.mockRejectedValue(Object.assign(new Error('Transaction is Running and cannot be cancelled.'), {
      body: { code: 'not_cancellable', error: 'Transaction is Running and cannot be cancelled.' },
      httpStatus: 409,
    }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await screen.findByText(/Cancel refused: Transaction is Running and cannot be cancelled\./);
    expect(screen.queryByText('Transaction cancelled.')).toBeNull();
    expect(getTransaction).toHaveBeenCalledWith('tx-1', { limit: 100 });
    expect(getTransactions).toHaveBeenCalled();
    await waitFor(() => expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true));
    expect(onUnhandled).not.toHaveBeenCalled();
  });

  it('shows a failed apply and reloads the transaction', async () => {
    await renderWith(tx({}, 'Approved'));
    vi.mocked(getTransaction).mockClear();
    mockApply.mockRejectedValue(apiError('not_approved', 409));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await screen.findByText(/Apply refused: not_approved\./);
    expect(screen.queryByText(/Apply job started|Transaction applied/)).toBeNull();
    expect(getTransaction).toHaveBeenCalledWith('tx-1', { limit: 100 });
    expect(onUnhandled).not.toHaveBeenCalled();
  });

  it('does not add a second period to a reason that already ends with one', async () => {
    await renderWith(tx({}, 'Approved'));
    mockCancel.mockRejectedValue(Object.assign(new Error('Already cancelled.'), { body: { code: 'not_cancellable' }, httpStatus: 409 }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    const banner = await screen.findByText(/Cancel refused/);
    expect(banner.textContent).toMatch(/Already cancelled\. Nothing was changed/);
    expect(banner.textContent).not.toMatch(/\.\./);
  });

  // QA #211 F1: apply can fail after mutating; never claim nothing changed.
  it('apply failing after mutating (verification_failed, mutated:true) does not claim nothing changed', async () => {
    await renderWith(tx({ mutation_family: 'album_cleanup_v1' }, 'Approved'));
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: tx({}, 'Recovery Required') });
    mockApply.mockRejectedValue(Object.assign(new Error('Album row still present after removal.'), {
      body: { ok: false, code: 'verification_failed', mutated: true, error: 'Album row still present after removal.' },
      httpStatus: 400,
    }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    const banner = await screen.findByText(/Apply (refused|failed)/);
    expect(banner.textContent).not.toMatch(/Nothing was changed/);
    expect(banner.textContent).toMatch(/partly changed/);
    expect(banner.textContent).toMatch(/Do not apply again/);
    await screen.findAllByText('Recovery Required');
    expect(onUnhandled).not.toHaveBeenCalled();
  });

  it.each([
    ['timeout', Object.assign(new Error('Request timed out.'), { isTimeout: true, httpStatus: 0 })],
    ['503', Object.assign(new Error('Beets transport error; do not re-apply.'), { body: { error: 'x' }, httpStatus: 503 })],
  ])('apply transport failure (%s) does not claim nothing changed', async (_label, err) => {
    await renderWith(tx({}, 'Approved'));
    mockApply.mockRejectedValue(err);
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    const banner = await screen.findByText(/Apply (refused|failed)/);
    expect(banner.textContent).not.toMatch(/Nothing was changed/);
    expect(banner.textContent).toMatch(/outcome is unknown/);
  });

  it('cancel transport failure does not claim nothing changed', async () => {
    await renderWith(tx({}, 'Approved'));
    mockCancel.mockRejectedValue(Object.assign(new Error('Request timed out.'), { isTimeout: true, httpStatus: 0 }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    const banner = await screen.findByText(/Cancel refused/);
    expect(banner.textContent).not.toMatch(/Nothing was changed/);
  });

  it('apply refused with mutated:false says nothing changed', async () => {
    await renderWith(tx({}, 'Approved'));
    mockApply.mockRejectedValue(Object.assign(new Error('Plan is stale'), { body: { code: 'stale', mutated: false }, httpStatus: 400 }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    const banner = await screen.findByText(/Apply refused: Plan is stale\. Nothing was changed/);
    expect(banner).toBeTruthy();
  });

  it.each(['Running', 'Completed', 'Failed', 'Cancelled', 'Rolled Back', 'Partially Rolled Back', 'Recovery Required'])(
    'disables Cancel for %s',
    async (status) => {
      await renderWith(tx({}, status));
      expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true);
    },
  );

  it.each(['Pending', 'Preview', 'Approved'])('enables Cancel for %s', async (status) => {
    await renderWith(tx({}, status));
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('LibraryChanges rollback', () => {
  const mockRollback = vi.mocked(rollbackTransaction);
  const rollbackable = (status: string, metadata: Record<string, unknown> = {}) => ({
    ...tx(metadata, status),
    rollback: { available: true },
  });
  const button = () => screen.getByRole('button', { name: 'Rollback' }) as HTMLButtonElement;

  beforeEach(() => {
    mockRollback.mockReset();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    cleanup();
  });

  it.each([
    ['Completed', {}],
    ['Failed', { engine_result: { ok: false } }],
    // Engine families retry rollback from Recovery Required (QA F1).
    ['Recovery Required', { engine_result: { ok: true } }],
    ['Partially Rolled Back', { engine_result: { ok: true } }],
  ])('enables Rollback for %s %o', async (status, metadata) => {
    await renderWith(rollbackable(status, metadata));
    expect(button().disabled).toBe(false);
  });

  it.each([
    ['Pending', {}], ['Preview', {}], ['Approved', {}], ['Running', {}], ['Cancelled', {}],
    ['Failed', {}], ['Rolled Back', {}], ['Partially Rolled Back', {}], ['Recovery Required', {}],
    ['Preview', { engine_result: { ok: true } }], ['Approved', { engine_result: { ok: true } }],
    ['Cancelled', { engine_result: { ok: true } }], ['Rolled Back', { engine_result: { ok: true } }],
    ['Running', { engine_result: { ok: true } }],
  ])('disables Rollback for %s %o', async (status, metadata) => {
    await renderWith(rollbackable(status, metadata));
    expect(button().disabled).toBe(true);
  });

  it('disables Rollback for Completed when rollback is unavailable', async () => {
    await renderWith(tx({}, 'Completed'));
    expect(button().disabled).toBe(true);
  });

  it('shows a 409 refusal, does not claim success, and reloads the detail and list', async () => {
    await renderWith(rollbackable('Completed'));
    vi.mocked(getTransaction).mockClear();
    vi.mocked(getTransactions).mockClear();
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: rollbackable('Running') });
    const msg = 'Only a completed transaction can be rolled back (status is Running).';
    mockRollback.mockRejectedValue(Object.assign(new Error(msg), {
      body: { ok: false, mutated: false, status: 'Running', error: msg },
      httpStatus: 409,
    }));
    fireEvent.click(button());
    const banner = await screen.findByText(/Rollback refused: Only a completed transaction can be rolled back/);
    expect(banner.textContent).toMatch(/Nothing was changed/);
    expect(screen.queryByText('Rollback started.')).toBeNull();
    expect(getTransaction).toHaveBeenCalledWith('tx-1', { limit: 100 });
    expect(getTransactions).toHaveBeenCalled();
    await waitFor(() => expect(button().disabled).toBe(true));
  });

  it.each([
    ['timeout', Object.assign(new Error('Request timed out.'), { isTimeout: true, httpStatus: 0 })],
    ['503', Object.assign(new Error('Beets engine is unavailable.'), { body: { error: 'x' }, httpStatus: 503 })],
  ])('rollback transport failure (%s) does not claim nothing changed', async (_label, err) => {
    await renderWith(rollbackable('Completed'));
    vi.mocked(getTransaction).mockClear();
    mockRollback.mockRejectedValue(err);
    fireEvent.click(button());
    const banner = await screen.findByText(/Rollback failed/);
    expect(banner.textContent).not.toMatch(/Nothing was changed/);
    expect(banner.textContent).toMatch(/outcome is unknown/);
    expect(banner.textContent).toMatch(/Do not roll back again/);
    expect(screen.queryByText('Rollback started.')).toBeNull();
    expect(getTransaction).toHaveBeenCalledWith('tx-1', { limit: 100 });
  });

  it('reloads the detail when an engine rollback response has no transaction (QA F2)', async () => {
    const recovering = rollbackable('Recovery Required', { engine_result: { ok: true } });
    await renderWith(recovering);
    vi.mocked(getTransaction).mockClear();
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: { ...recovering, status: 'Rolled Back' } });
    mockRollback.mockResolvedValue({ ok: true } as unknown as Awaited<ReturnType<typeof rollbackTransaction>>);
    fireEvent.click(button());
    await screen.findByText('Rollback finished.');
    expect(screen.queryByText('Rollback started.')).toBeNull();
    expect(getTransaction).toHaveBeenCalledWith('tx-1', { limit: 100 });
    // The detail pane stays open and shows the reloaded state.
    await waitFor(() => expect(button().disabled).toBe(true));
  });

  it('does nothing when the confirmation is declined', async () => {
    await renderWith(rollbackable('Completed'));
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(button());
    expect(mockRollback).not.toHaveBeenCalled();
  });
});
