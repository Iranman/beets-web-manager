import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UntrackedRecoverySection } from '../src/features/libraryHealth/UntrackedRecoverySection';
import {
  applyTransaction,
  approveTransaction,
  getUntrackedCandidates,
  planUntrackedRecovery,
  rollbackTransaction,
} from '../src/api/client';

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

// One home per action: Jobs > Untracked files only makes plans. Approve, apply
// and roll back (with their confirmations and the server's rollback.allowed
// gate, #228/#250) live in Workbench > Library Changes and are tested there.
describe('UntrackedRecoverySection hands plans to Library Changes', () => {
  afterEach(() => { vi.resetAllMocks(); cleanup(); });

  it('links a new plan to its Library Changes entry and offers no inline mutation', async () => {
    vi.mocked(getUntrackedCandidates).mockResolvedValue({
      ok: true, total: 1, offset: 0, limit: 50,
      rows: [{ path: '/m/a.flac', action: 'quarantine', action_eligibility: 'eligible', reason: 'dup' }],
    } as never);
    vi.mocked(planUntrackedRecovery).mockResolvedValue({ ok: true, operation_id: 'op 1', action: 'quarantine' });
    render(<MemoryRouter><UntrackedRecoverySection /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Plan' }));
    const link = await screen.findByRole('link', { name: /in Library Changes/ });
    expect(link.getAttribute('href')).toBe('/import?tab=changes&tx=op%201');
    expect(screen.queryByRole('button', { name: /Approve|Apply|Roll back/ })).toBeNull();
    expect(approveTransaction).not.toHaveBeenCalled();
    expect(applyTransaction).not.toHaveBeenCalled();
    expect(rollbackTransaction).not.toHaveBeenCalled();
  });
});
