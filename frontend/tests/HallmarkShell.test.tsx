import { cleanup, render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Shell from '../src/components/layout/Shell';

// Hallmark shell redesign: the badge says what its colour means, failures stay
// visible while a job runs, the record mark's tonearm marks a running job, and
// the System menu reads as current on the pages it holds.
afterEach(cleanup);

const jobs = vi.hoisted(() => ({ running: 0, failed: 0 }));
vi.mock('../src/lib/hooks', () => ({
  useGlobalJobs: () => ({ ...jobs, refresh: () => {} }),
  useEngineProbe: () => false,
}));
vi.mock('../src/api/client', () => ({
  getHealth: vi.fn(),
  logout: vi.fn(),
  restartApp: vi.fn(),
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Shell username="admin" />}>
          <Route path="*" element={<div>PAGE</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('Hallmark app shell', () => {
  it('names running and failed counts on the Jobs tab and in the header status line', () => {
    jobs.running = 2;
    jobs.failed = 1;
    renderAt('/library');
    expect(screen.getByRole('link', { name: /^Jobs\s*\(2 running\)\s*\(1 failed\)$/ })).toBeTruthy();
    expect(screen.getByText('2 running')).toBeTruthy();
    expect(screen.getByText('1 failed')).toBeTruthy();
    expect(screen.getByTestId('record-tonearm')).toBeTruthy();
  });

  it('lifts the tonearm and drops the badge when nothing runs', () => {
    jobs.running = 0;
    jobs.failed = 0;
    renderAt('/library');
    expect(screen.getByRole('link', { name: 'Jobs' })).toBeTruthy();
    expect(screen.queryByTestId('record-tonearm')).toBeNull();
  });

  it('marks the System menu button as current on a page it holds', () => {
    jobs.running = 0;
    renderAt('/changes');
    expect(screen.getByRole('button', { name: 'System' }).className).toContain('border-brass-400 bg-brass-950/60');
    cleanup();
    renderAt('/library');
    expect(screen.getByRole('button', { name: 'System' }).className).toContain('border-transparent');
  });

  it('keeps the header flat and gives page titles the display serif', () => {
    const shell = readFileSync(resolve(__dirname, '../src/components/layout/Shell.tsx'), 'utf8');
    expect(shell).not.toMatch(/backdrop-blur|bg-gradient-to-r|animate-pulse/);
    const css = readFileSync(resolve(__dirname, '../src/styles.css'), 'utf8');
    expect(css).toMatch(/main h1\s*\{\s*font-family: var\(--font-display\)/);
  });
});
