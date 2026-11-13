import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import NowWorkingBar from '../src/components/NowWorkingBar';
import type { Job } from '../src/types/api';

// The "now working" bar is presentational over the shared ['jobs'] query: it shows when a job runs,
// hides when idle, and conveys completion with text + icon (not motion or colour alone). It must
// degrade to nothing when the job state is empty.
afterEach(cleanup);

const state = vi.hoisted(() => ({
  value: { running: 0, failed: 0, jobs: [] as Job[], refresh: () => {} },
}));
vi.mock('../src/lib/hooks', () => ({
  useGlobalJobs: () => state.value,
}));

function mkJob(over: Partial<Job>): Job {
  return {
    job_id: 'j1',
    label: 'Import album',
    status: 'running',
    created_at: 0,
    metadata: {},
    state: {},
    log: [],
    ...over,
  };
}

function setJobs(jobs: Job[]) {
  state.value = { running: jobs.filter((j) => j.status === 'running').length, failed: 0, jobs, refresh: () => {} };
}

function renderBar() {
  return render(
    <MemoryRouter>
      <NowWorkingBar />
    </MemoryRouter>,
  );
}

describe('Now Working bar', () => {
  it('renders the running job with its title, step and determinate brass progress', () => {
    setJobs([mkJob({ label: 'Import album', state: { progress_percent: 42 } })]);
    const { container } = renderBar();
    const bar = screen.getByTestId('now-working-bar');
    expect(bar).toBeTruthy();
    // Title comes from the shared job feed model.
    expect(screen.getByText('Import album')).toBeTruthy();
    // Determinate percent is shown as text (not motion alone) and drives the fill width.
    expect(screen.getByText('42%')).toBeTruthy();
    const fill = container.querySelector('.nowbar-progress-fill') as HTMLElement | null;
    expect(fill?.style.width).toBe('42%');
    // The live-activity spinner is the record ring (motion-gated in CSS).
    expect(container.querySelector('.record-ring.is-spinning')).toBeTruthy();
  });

  it('renders nothing when no job is running and none just finished', () => {
    setJobs([]);
    renderBar();
    expect(screen.queryByTestId('now-working-bar')).toBeNull();
  });

  it('shows an indeterminate track when no percent is reported', () => {
    setJobs([mkJob({ state: {} })]);
    const { container } = renderBar();
    const track = container.querySelector('.nowbar-progress') as HTMLElement | null;
    expect(track?.getAttribute('data-indeterminate')).toBe('true');
    // No percent text when progress is unknown.
    expect(screen.queryByText(/%$/)).toBeNull();
  });

  it('reveals the live log when expanded, from job state already on the frontend', async () => {
    setJobs([mkJob({ log: ['Matched 12 / 40 tracks', 'Tagging files'] })]);
    renderBar();
    const toggle = screen.getByRole('button', { name: /details/i });
    toggle.click();
    expect(await screen.findByText('Matched 12 / 40 tracks')).toBeTruthy();
  });

  it('conveys completion with text and an icon when the running job finishes', async () => {
    setJobs([mkJob({ status: 'running' })]);
    const { rerender } = renderBar();
    // The job finishes successfully: it leaves the running set but stays in the list.
    setJobs([mkJob({ status: 'success' })]);
    rerender(
      <MemoryRouter>
        <NowWorkingBar />
      </MemoryRouter>,
    );
    // Completion is announced politely (role=status) and said in words, not motion/colour alone.
    const status = await screen.findByRole('status');
    expect(status.textContent).toContain('Completed');
    expect(status.querySelector('svg')).toBeTruthy();
  });

  it('marks a failed finish without relying on colour alone', async () => {
    setJobs([mkJob({ status: 'running' })]);
    const { rerender } = renderBar();
    setJobs([mkJob({ status: 'failed' })]);
    rerender(
      <MemoryRouter>
        <NowWorkingBar />
      </MemoryRouter>,
    );
    const status = await screen.findByRole('status');
    expect(status.textContent).toContain('Finished with errors');
  });
});
