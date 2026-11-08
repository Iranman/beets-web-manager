// qBittorrent as a batch import source: the selector sends source, the
// fail-closed state never offers a folder scan, and source_missing is neutral.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getAiBatchStatus: vi.fn(),
    getSetupStatus: vi.fn(),
    getJob: vi.fn(() => new Promise(() => undefined)),
    runPreflight: vi.fn(),
    startAiBatchImport: vi.fn(),
  };
});

import * as client from '../src/api/client';
import { folderStatusLabel, IntakePanel } from '../src/features/intake/IntakePanel';

const mocked = vi.mocked(client);
const ROOT = '/srv/dl';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

function renderIntake() {
  mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: ROOT, exists: true } } } as never);
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter><IntakePanel /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('startAiBatchImport payload', () => {
  async function bodyFor(call: () => Promise<unknown>) {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true, job_id: 'j' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    await call();
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe('/api/ai-batch-import');
    return JSON.parse(String(init.body));
  }

  it('sends source qbittorrent without a path, and local with its path', async () => {
    const { startAiBatchImport } = await vi.importActual<typeof import('../src/api/client')>('../src/api/client');
    expect(await bodyFor(() => startAiBatchImport('/ignored', undefined, 'qbittorrent'))).toEqual({ source: 'qbittorrent' });
    expect(await bodyFor(() => startAiBatchImport(ROOT))).toEqual({ source: 'local', path: ROOT });
  });
});

describe('source selector', () => {
  it('qBittorrent hides the path, explains copy-only, and starts with source qbittorrent', async () => {
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'idle' } } as never);
    mocked.startAiBatchImport.mockResolvedValue({ ok: true, job_id: 'q1', state: { status: 'running', job_id: 'q1' } } as never);
    renderIntake();
    await screen.findByDisplayValue(ROOT);
    expect((screen.getByRole('radio', { name: 'Local folder' }) as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByRole('radio', { name: 'qBittorrent' }));
    expect(screen.queryByLabelText('Import/source path')).toBeNull();
    expect(screen.queryByRole('button', { name: /Preview Import All/ })).toBeNull();
    expect(screen.getByText(/copied, never moved/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Import All' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Source: qBittorrent/)).toBeTruthy();
    expect(mocked.startAiBatchImport).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import eligible only' }));
    await waitFor(() => expect(mocked.startAiBatchImport).toHaveBeenCalledWith('', undefined, 'qbittorrent'));
  });

  it('shows a refused qBittorrent start as nothing imported', async () => {
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'idle' } } as never);
    mocked.startAiBatchImport.mockRejectedValue(new Error('qBittorrent is not configured. Set QBITTORRENT_URL.'));
    renderIntake();
    await screen.findByDisplayValue(ROOT);
    fireEvent.click(screen.getByRole('radio', { name: 'qBittorrent' }));
    fireEvent.click(screen.getByRole('button', { name: 'Import All' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Import eligible only' }));
    expect(await screen.findByText(/Could not start the qBittorrent import: qBittorrent is not configured.*Nothing was imported/)).toBeTruthy();
  });
});

describe('qBittorrent batch states', () => {
  it('source_verification_required says nothing was queued and offers no folder scan', async () => {
    localStorage.setItem('beets:ai-batch-import-job-id', 'qb1');
    mocked.getAiBatchStatus.mockResolvedValue({
      state: {
        status: 'failed', batch_job_id: 'qb1', job_id: 'qb1', source_path: ROOT, source_mode: 'qbittorrent',
        current_step: 'source_verification_required', last_error: 'connection refused',
        source_error: { code: 'source_verification_required', message: 'connection refused' },
        total_folders_found: 0, folders_processed: 0, folders_queued: 0, folders_running: 0, folders_completed: 0,
        folders_failed: 0, folders_skipped: 0, folders_retryable: 0, folders: [],
      },
    } as never);
    renderIntake();
    expect(await screen.findByText(/qBittorrent could not be verified: connection refused/)).toBeTruthy();
    expect(screen.getByText(/downloads folder was not scanned instead/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Retry eligible failures/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Preview Import All/ })).toBeNull();
  });

  it('shows the source summary and source_missing as a neutral status', async () => {
    localStorage.setItem('beets:ai-batch-import-job-id', 'qb2');
    mocked.getAiBatchStatus.mockResolvedValue({
      state: {
        status: 'completed', batch_job_id: 'qb2', job_id: 'qb2', source_path: ROOT, source_mode: 'qbittorrent',
        source_report: {
          source: 'qbittorrent', torrents: 5, torrents_complete: 4, audio_files: 40, ownership_verified: 38,
          ownership_verified_pct: 95, eligible_files: 30, units: 3, exclusions: { app_managed: 6, missing: 2, symlink: 0 },
        },
        folders_source_missing: 1, total_folders_found: 3, folders_processed: 3, folders_queued: 0, folders_running: 0,
        folders_completed: 2, folders_failed: 0, folders_skipped: 0, folders_attention: 0,
        folders: [{ folder_id: 'f1', batch_job_id: 'qb2', source_folder: `${ROOT}/Gone`, status: 'source_missing' }],
      },
    } as never);
    renderIntake();
    const summary = await screen.findByRole('region', { name: 'qBittorrent source summary' });
    expect(within(summary).getByText('95%')).toBeTruthy();
    expect(within(summary).getByText('30')).toBeTruthy();
    expect(within(summary).getByText(/Web Manager's own downloads 6 · Missing on disk 2/)).toBeTruthy();
    expect(within(summary).queryByText(/Linked file/)).toBeNull();
    // Not an error: no attention row, no retry, no review button for the missing folder.
    expect(screen.queryByText('Needs attention')).toBeNull();
    expect(screen.queryByRole('button', { name: /Review \d+ issue/ })).toBeNull();
    expect(folderStatusLabel('source_missing')).toBe('Source missing');
  });
});
