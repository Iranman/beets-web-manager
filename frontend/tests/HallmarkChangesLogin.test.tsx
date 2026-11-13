import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LibraryChanges from '../src/views/LibraryChanges';
import Login from '../src/views/Login';
import { getTransaction, getTransactions, login } from '../src/api/client';
import type { TransactionDetail } from '../src/api/types';

vi.mock('../src/api/client', async (importActual) => ({
  apiErrorBody: (await importActual<typeof import('../src/api/client')>()).apiErrorBody,
  approveTransaction: vi.fn(),
  applyTransaction: vi.fn(),
  cancelTransaction: vi.fn(),
  getTransaction: vi.fn(),
  getTransactionSettings: vi.fn(() => new Promise(() => {})),
  getTransactions: vi.fn(),
  login: vi.fn(),
  rollbackTransaction: vi.fn(),
  saveTransactionSettings: vi.fn(),
  transactionExportUrl: vi.fn((id: string, format: string) => `/api/transactions/${id}/export?format=${format}`),
}));

afterEach(cleanup);

const detail: TransactionDetail = {
  id: 'tx-1',
  created_at: 0,
  updated_at: 0,
  operation_type: 'Metadata Update',
  status: 'Recovery Required',
  dry_run: false,
  summary: 'Fix tags',
  confidence: {},
  counts: {},
  rollback: { available: false, allowed: false, allowed_reason: 'Not now.' },
  cancel: { allowed: false, mode: null, code: 'x', reason: 'No.' },
  metadata: {},
  changes: [],
  changes_total: 0,
};

describe('Hallmark Library Changes ledger', () => {
  it('reads as a ledger: section heading, pressed row, status word, export links on the record', async () => {
    vi.mocked(getTransactions).mockResolvedValue({ ok: true, transactions: [detail], total: 1 });
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: detail });
    render(<LibraryChanges />);
    expect(screen.getByRole('heading', { level: 2, name: 'Library Changes' })).toBeTruthy();
    const title = await screen.findByRole('heading', { level: 3, name: 'Metadata Update' });
    expect(title.className).toContain('font-display');
    const row = screen.getByRole('button', { name: /Metadata Update/, pressed: true });
    expect(row.className).toContain('is-selected');
    // Recovery Required is a failure-family state: a red word, never colour alone.
    const words = screen.getAllByText('Recovery Required');
    expect(words.length).toBe(2);
    for (const w of words) expect(w.className).toContain('text-red-300');
    const exportGroup = screen.getByRole('group', { name: 'Export this transaction' });
    expect(exportGroup.querySelectorAll('a')).toHaveLength(3);
  });
});

describe('Hallmark Login', () => {
  it('is a main landmark with one serif title, labelled fields and an announced error', async () => {
    vi.mocked(login).mockResolvedValue({ ok: false, error: 'Too many attempts. Try again in 60 seconds.' } as Awaited<ReturnType<typeof login>>);
    render(<Login onSuccess={() => {}} />);
    expect(screen.getByRole('main')).toBeTruthy();
    expect(screen.getByRole('heading', { level: 1, name: 'Beets Web Manager' }).className).toContain('font-display');
    expect(screen.getByLabelText('Username').id).toBe('login-username');
    const password = screen.getByLabelText('Password');
    expect(password.id).toBe('login-password');
    fireEvent.change(password, { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign In' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('Sign-In Error: Too many attempts. Try again in 60 seconds.');
  });
});
