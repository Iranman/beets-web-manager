import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LibraryChanges from '../src/views/LibraryChanges';
import { getTransaction, getTransactions } from '../src/api/client';
import type { TransactionDetail } from '../src/api/types';

// Bolder Shelf elevation of Library Changes: the restyle is additive (a .changes-bolder scope hook,
// the walnut .changes-record focal panel, the shared RecordRing spinner, a scoped CSS block). These
// tests pin the new structure and that the ledger behaviour (selection, status words) is unchanged.

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
  transactionExportUrl: vi.fn((id: string, format: string) => `/api/transactions/${id}/export?format=${format}`),
}));

afterEach(cleanup);

const detail: TransactionDetail = {
  id: 'tx-1',
  created_at: 0,
  updated_at: 0,
  operation_type: 'Metadata Update',
  status: 'Completed',
  dry_run: false,
  summary: 'Fix tags',
  confidence: {},
  counts: {},
  rollback: { available: true, allowed: true, allowed_reason: '' },
  cancel: { allowed: false, mode: null, code: 'x', reason: 'No.' },
  metadata: {},
  changes: [],
  changes_total: 0,
};

describe('Bolder Library Changes', () => {
  it('scopes the bolder world and renders the selected transaction as the walnut record panel', async () => {
    vi.mocked(getTransactions).mockResolvedValue({ ok: true, transactions: [detail], total: 1 });
    vi.mocked(getTransaction).mockResolvedValue({ ok: true, transaction: detail });
    const { container } = render(<LibraryChanges />);
    // Scope hook for the appended CSS block, so shared ops-* surfaces elsewhere stay untouched.
    expect(container.querySelector('.ops-page.changes-bolder')).toBeTruthy();
    // The one focal object: the selected transaction on its walnut record panel.
    await waitFor(() => expect(container.querySelector('article.changes-record')).toBeTruthy());
    // The metadata liner-note list is still the ops-figures definition list.
    expect(container.querySelector('article.changes-record .ops-figures dt')).toBeTruthy();
  });

  it('shows the shared record-ring spinner while the ledger loads, not a progress bar', () => {
    // A never-resolving list keeps the view in its loading state.
    vi.mocked(getTransactions).mockReturnValue(new Promise(() => {}) as ReturnType<typeof getTransactions>);
    vi.mocked(getTransaction).mockReturnValue(new Promise(() => {}) as ReturnType<typeof getTransaction>);
    const { container } = render(<LibraryChanges />);
    expect(container.querySelector('.record-ring.is-spinning')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain('Loading transactions');
    expect(screen.queryByRole('progressbar')).toBeNull();
  });
});
