import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RecoveryStatusPanel } from '../src/features/recovery/RecoveryStatusPanel';

// #762: passive, read-only status of the missing-track recovery advancer. No
// action buttons; it only surfaces the rolling sidecar the backend writes.

const { getRecoveryAdvanceStatus } = vi.hoisted(() => ({
  getRecoveryAdvanceStatus: vi.fn(),
}));

vi.mock('../src/api/client', () => ({ getRecoveryAdvanceStatus }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function mockStatus(has_run: boolean, status: Record<string, unknown>) {
  getRecoveryAdvanceStatus.mockResolvedValue({ ok: true, has_run, status });
}

describe('RecoveryStatusPanel (#762)', () => {
  it('shows the empty state before the first sweep', async () => {
    mockStatus(false, {});
    render(<RecoveryStatusPanel />);
    await waitFor(() => expect(screen.getByText(/No recovery sweep has run yet/)).toBeTruthy());
  });

  it('reports a quiet, no-change sweep plainly', async () => {
    mockStatus(true, {
      last_run: Date.now() / 1000 - 30,
      trigger: 'scheduled',
      changes_made: false,
      needs_attention: false,
      error: '',
      next_scheduled_check: Date.now() / 1000 + 120,
      counts: { examined: 9, recovered: 0, still_unresolved: 0, skipped: 9 },
    });
    render(<RecoveryStatusPanel />);
    await waitFor(() =>
      expect(screen.getByText(/No missing tracks required recovery\. No changes made\./)).toBeTruthy(),
    );
    // Passive: no buttons on the panel.
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('reports recovered tracks when a sweep made changes', async () => {
    mockStatus(true, {
      last_run: Date.now() / 1000 - 10,
      trigger: 'manual',
      changes_made: true,
      needs_attention: false,
      counts: { examined: 12, recovered: 3, still_unresolved: 2, skipped: 1 },
    });
    render(<RecoveryStatusPanel />);
    await waitFor(() => expect(screen.getByText(/Recovered 3 missing tracks/)).toBeTruthy());
  });

  it('surfaces newly-review items as a warning', async () => {
    mockStatus(true, {
      last_run: Date.now() / 1000 - 10,
      trigger: 'scheduled',
      changes_made: false,
      needs_attention: true,
      counts: { examined: 5, recovered: 0, still_unresolved: 1, skipped: 4, newly_review: 2 },
    });
    render(<RecoveryStatusPanel />);
    await waitFor(() => expect(screen.getByText(/2 newly need review/)).toBeTruthy());
  });

  it('surfaces a sweep error distinctly', async () => {
    mockStatus(true, {
      last_run: Date.now() / 1000 - 10,
      trigger: 'scheduled',
      needs_attention: true,
      error: 'OSError',
      counts: { examined: 3 },
    });
    render(<RecoveryStatusPanel />);
    await waitFor(() => expect(screen.getByText(/last sweep reported an error/)).toBeTruthy());
    expect(screen.getByText(/OSError/)).toBeTruthy();
  });

  it('treats a failed load as unknown, not empty', async () => {
    getRecoveryAdvanceStatus.mockRejectedValue(new Error('network'));
    render(<RecoveryStatusPanel />);
    await waitFor(() => expect(screen.getByText(/could not be loaded, so it is unknown/)).toBeTruthy());
  });
});
