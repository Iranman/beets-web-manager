import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  RecoveryStageIndicator,
  getRecoveryStage,
} from '../src/features/importReview/MbidLink';
import type { ReviewItem, RecoveryStage } from '../src/api/types';

afterEach(cleanup);

// Build a carrier-backed Review item carrying the backend recovery_stage
// contract object, exactly as surfaced on evidence.recovery_carrier.
function itemWithStage(stage: Partial<RecoveryStage> | undefined): ReviewItem {
  return {
    id: 'r1',
    type: 'pending_ai',
    evidence: stage === undefined ? {} : { recovery_carrier: { recovery: true, recovery_stage: stage } },
  } as unknown as ReviewItem;
}

const LINEAR: Array<[string, number, string]> = [
  ['missing', 0, 'Missing'],
  ['searching', 1, 'Searching'],
  ['downloading', 2, 'Downloading'],
  ['verifying', 3, 'Verifying'],
  ['importing', 4, 'Importing'],
  ['plex_sync', 5, 'Plex Sync'],
  ['complete', 6, 'Complete'],
];

describe('getRecoveryStage — resilient extraction', () => {
  it('returns the stage object from evidence.recovery_carrier', () => {
    const item = itemWithStage({ stage: 'importing', stage_index: 4, label: 'Importing', terminal: false, note: '' });
    expect(getRecoveryStage(item)?.stage).toBe('importing');
  });

  it('returns null when there is no recovery_stage', () => {
    expect(getRecoveryStage(itemWithStage(undefined))).toBeNull();
  });

  it('returns null for a partial object missing stage/stage_index', () => {
    expect(getRecoveryStage(itemWithStage({ label: 'Importing' }))).toBeNull();
    expect(getRecoveryStage(itemWithStage({ stage: 'importing' }))).toBeNull();
  });
});

describe('RecoveryStageIndicator — each linear stage', () => {
  it.each(LINEAR)('renders %s as step %i of 7 with the current step marked', (stage, index, label) => {
    render(
      <RecoveryStageIndicator
        item={itemWithStage({ stage, stage_index: index, label, terminal: stage === 'complete', note: '' })}
      />,
    );

    // 7 linear steps as an ordered list.
    const list = screen.getByRole('list');
    const steps = within(list).getAllByRole('listitem');
    expect(steps).toHaveLength(7);

    // Screen-reader status announces position by TEXT, not color alone.
    expect(screen.getByRole('status').textContent).toContain(`step ${index + 1} of 7: ${label}`);

    // The current step is programmatically marked and labelled.
    const current = steps.find((li) => li.getAttribute('aria-current') === 'step');
    expect(current).toBeTruthy();
    expect(current!.getAttribute('aria-label')).toContain(`Step ${index + 1} of 7: ${label}`);
    expect(current!.getAttribute('data-step')).toBe(stage);

    // Prior steps are done, later steps upcoming (position by stage_index).
    steps.forEach((li, i) => {
      const expected = i < index ? 'done' : i === index ? (stage === 'complete' ? 'complete' : 'current') : 'upcoming';
      expect(li.getAttribute('data-step-state')).toBe(expected);
    });
  });
});

describe('RecoveryStageIndicator — Plex Sync is attention, not an error', () => {
  it('renders plex_sync as an in-progress linear step, never a failure', () => {
    render(
      <RecoveryStageIndicator
        item={itemWithStage({ stage: 'plex_sync', stage_index: 5, label: 'Plex Sync', terminal: false, note: 'pending' })}
      />,
    );
    // Still the linear list (not the error terminal badge).
    expect(screen.getByRole('list')).toBeTruthy();
    const current = within(screen.getByRole('list'))
      .getAllByRole('listitem')
      .find((li) => li.getAttribute('aria-current') === 'step');
    expect(current!.getAttribute('data-step')).toBe('plex_sync');
    // Shown as in progress, NOT complete and NOT an error terminal.
    expect(current!.getAttribute('data-step-state')).toBe('current');
    expect(screen.queryByText(/failed/i)).toBeNull();
    expect(screen.queryByText(/needs review/i)).toBeNull();
    // note surfaced compactly.
    expect(screen.getByText('pending')).toBeTruthy();
  });
});

describe('RecoveryStageIndicator — Complete terminal', () => {
  it('marks Complete as the final step', () => {
    render(
      <RecoveryStageIndicator
        item={itemWithStage({ stage: 'complete', stage_index: 6, label: 'Complete', terminal: true, note: 'synced' })}
      />,
    );
    expect(screen.getByRole('status').textContent).toContain('step 7 of 7: Complete');
    const steps = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(steps[6].getAttribute('data-step-state')).toBe('complete');
    expect(screen.getByText('synced')).toBeTruthy();
  });

  it('shows Complete (not an error) when Plex was skipped by config', () => {
    render(
      <RecoveryStageIndicator
        item={itemWithStage({ stage: 'complete', stage_index: 6, label: 'Complete', terminal: true, note: 'auto_refresh_disabled' })}
      />,
    );
    // Reads Complete, surfaces the skip reason, and is not an error terminal.
    expect(screen.getByRole('status').textContent).toContain('Complete');
    expect(screen.getByText('auto_refresh_disabled')).toBeTruthy();
    expect(screen.queryByText(/failed/i)).toBeNull();
    expect(screen.queryByText(/needs review/i)).toBeNull();
  });
});

describe('RecoveryStageIndicator — error terminals', () => {
  it('renders needs_review as a distinct attention terminal, not a linear step', () => {
    render(
      <RecoveryStageIndicator
        item={itemWithStage({ stage: 'needs_review', stage_index: -1, label: 'Needs Review', terminal: true, note: '' })}
      />,
    );
    // Not a linear list; a single status badge the SR reads as review-needed.
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.getByRole('status').textContent).toMatch(/needs review/i);
    expect(screen.getByRole('status').textContent).toContain('Needs Review');
  });

  it('renders failed as a distinct error terminal, not a linear step', () => {
    render(
      <RecoveryStageIndicator
        item={itemWithStage({ stage: 'failed', stage_index: -1, label: 'Failed', terminal: true, note: 'provider error' })}
      />,
    );
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.getByRole('status').textContent).toMatch(/recovery failed/i);
    // note surfaced.
    expect(screen.getByText('provider error')).toBeTruthy();
  });
});

describe('RecoveryStageIndicator — fallback', () => {
  it('renders nothing for an item with no recovery_stage', () => {
    const { container } = render(<RecoveryStageIndicator item={itemWithStage(undefined)} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing for a partial recovery_stage (older carrier)', () => {
    const { container } = render(<RecoveryStageIndicator item={itemWithStage({ label: 'Importing' })} />);
    expect(container.firstChild).toBeNull();
  });
});
