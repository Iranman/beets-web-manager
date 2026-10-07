import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getAiBatchStatus, getSetupStatus, getUnattendedCleanup, runPreflight, startDedupScan } from '../src/api/client';
import { DedupPanel } from '../src/features/dedup/DedupPanel';
import { IntakePanel } from '../src/features/intake/IntakePanel';

vi.mock('../src/api/client', () => ({
  fetchAlbumArt: vi.fn(),
  getAiBatchStatus: vi.fn(),
  getDedupScan: vi.fn(),
  getSetupStatus: vi.fn(),
  getUnattendedCleanup: vi.fn(),
  pauseAiBatch: vi.fn(),
  reconcileArtwork: vi.fn(),
  recoverAiBatch: vi.fn(),
  retryLibraryImportAllFailed: vi.fn(),
  runDedupCleanup: vi.fn(),
  runDuplicateMaintenance: vi.fn(),
  runPreflight: vi.fn(),
  setUnattendedCleanup: vi.fn(),
  skipAiBatch: vi.fn(),
  startAiBatchImport: vi.fn(),
  startDedupAiReview: vi.fn(),
  startDedupScan: vi.fn(),
  stopAiBatch: vi.fn(),
}));

const ROOT = '/srv/configured-downloads';

function wrap(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function setupStatusWith(path: string) {
  return { paths: { downloads: { path, exists: true } } } as never;
}

describe('configured downloads root (RC QA D4)', () => {
  beforeEach(() => {
    vi.mocked(getAiBatchStatus).mockResolvedValue({ state: { status: 'idle' } } as never);
    vi.mocked(getUnattendedCleanup).mockReturnValue(new Promise(() => undefined) as never);
    vi.mocked(runPreflight).mockResolvedValue({
      ok: true, path: ROOT, folders: [], audio_folders: 0, audio_files: 0, already_in_library_folders: 0,
      pending_review: 0, unsupported_files: 0, empty_dirs: 0, artist_folder_groups: 0,
    } as never);
  });
  afterEach(() => { vi.resetAllMocks(); cleanup(); });

  it('Intake prefills and previews the server-reported downloads root', async () => {
    vi.mocked(getSetupStatus).mockResolvedValue(setupStatusWith(ROOT));
    wrap(<IntakePanel />);
    const input = await screen.findByDisplayValue(ROOT);
    expect(input).toBeTruthy();
    expect(screen.getByText(`Downloads folder: ${ROOT}`)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Preview Import All' }));
    await waitFor(() => expect(runPreflight).toHaveBeenCalledWith(ROOT));
    fireEvent.click(screen.getByRole('button', { name: 'Failed' }));
    expect(screen.getByDisplayValue(`${ROOT}/failed_imports`)).toBeTruthy();
  });

  it('Intake shows an actionable warning and no default path when setup status fails', async () => {
    vi.mocked(getSetupStatus).mockRejectedValue(new Error('HTTP 503'));
    wrap(<IntakePanel />);
    expect(await screen.findByText(/Could not load the configured downloads folder \(DOWNLOADS_ROOT\).*HTTP 503/)).toBeTruthy();
    expect((screen.getByLabelText('Import/source path') as HTMLInputElement).value).toBe('');
    expect((screen.getByRole('button', { name: 'Preview Import All' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Downloads' }) as HTMLButtonElement).disabled).toBe(true);
    // A manually entered path still works.
    fireEvent.change(screen.getByLabelText('Import/source path'), { target: { value: '/manual' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview Import All' }));
    await waitFor(() => expect(runPreflight).toHaveBeenCalledWith('/manual'));
  });

  it('Intake shows a loading label while setup status is pending', () => {
    vi.mocked(getSetupStatus).mockReturnValue(new Promise(() => undefined) as never);
    wrap(<IntakePanel />);
    expect(screen.getByText('Downloads folder: loading…')).toBeTruthy();
  });

  it('Intake warns when the server reports no downloads root', async () => {
    vi.mocked(getSetupStatus).mockResolvedValue(setupStatusWith(''));
    wrap(<IntakePanel />);
    expect(await screen.findByText(/server reported no downloads folder/)).toBeTruthy();
  });

  it('Dedup Downloads button scans the server-reported root', async () => {
    vi.mocked(getSetupStatus).mockResolvedValue(setupStatusWith(ROOT));
    vi.mocked(startDedupScan).mockResolvedValue({ ok: true, jid: 'j1' } as never);
    wrap(<DedupPanel />);
    const btn = await screen.findByRole('button', { name: 'Downloads' });
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(btn);
    fireEvent.click(screen.getByRole('button', { name: 'Scan' }));
    await waitFor(() => expect(startDedupScan).toHaveBeenCalledWith(ROOT));
  });

  it('Dedup disables Downloads and warns when setup status fails', async () => {
    vi.mocked(getSetupStatus).mockRejectedValue(new Error('HTTP 503'));
    wrap(<DedupPanel />);
    expect(await screen.findByText(/Could not load the configured downloads folder/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Downloads' }) as HTMLButtonElement).disabled).toBe(true);
  });
});
