import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { IntakePanel } from '../src/features/intake/IntakePanel';
import * as client from '../src/api/client';

afterEach(cleanup);

vi.mock('../src/api/client', () => ({
  getImportRoots: vi.fn(),
  getAiBatchStatus: vi.fn().mockResolvedValue({
    ok: true,
    state: { status: 'idle' },
  }),
  runPreflight: vi.fn(),
  startAiBatchImport: vi.fn(),
  pauseAiBatch: vi.fn(),
  recoverAiBatch: vi.fn(),
  retryLibraryImportAllFailed: vi.fn(),
  skipAiBatch: vi.fn(),
  stopAiBatch: vi.fn(),
  reconcileArtwork: vi.fn(),
  fetchAlbumArt: vi.fn(),
}));

vi.mock('react-router', () => ({
  useNavigate: () => vi.fn(),
}));

describe('IntakePanel Configurable Import Roots', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('loads dynamic roots and selects last_saved_source when available', async () => {
    vi.mocked(client.getImportRoots).mockResolvedValue({
      ok: true,
      music_root: '/data/media/music',
      staging_roots: ['/data/torrents/music', '/data/manual-imports'],
      recommended_source: '/data/torrents/music',
      recommended_import_roots: ['/data/torrents/music', '/data/manual-imports'],
      failed_imports_root: '/data/torrents/music/failed_imports',
      last_saved_source: '/data/manual-imports/Artist - Album (2024)',
    });

    render(<IntakePanel />);

    await waitFor(() => {
      const input = screen.getByLabelText('Import/source path') as HTMLInputElement;
      expect(input.value).toBe('/data/manual-imports/Artist - Album (2024)');
    });

    expect(screen.getByText('/data/manual-imports/Artist - Album (2024) source')).toBeTruthy();
  });

  it('falls back to recommended_source when last_saved_source is not set', async () => {
    vi.mocked(client.getImportRoots).mockResolvedValue({
      ok: true,
      music_root: '/srv/library',
      staging_roots: ['/srv/intake'],
      recommended_source: '/srv/intake',
      recommended_import_roots: ['/srv/intake'],
      failed_imports_root: '/srv/intake/failed_imports',
      last_saved_source: null,
    });

    render(<IntakePanel />);

    await waitFor(() => {
      const input = screen.getByLabelText('Import/source path') as HTMLInputElement;
      expect(input.value).toBe('/srv/intake');
    });

    expect(screen.getByText('/srv/intake source')).toBeTruthy();
  });

  it('switches paths when quick buttons are clicked', async () => {
    vi.mocked(client.getImportRoots).mockResolvedValue({
      ok: true,
      music_root: '/data/media/music',
      staging_roots: ['/data/custom-staging'],
      recommended_source: '/data/custom-staging',
      recommended_import_roots: ['/data/custom-staging'],
      failed_imports_root: '/data/custom-staging/failed_imports',
      last_saved_source: null,
    });

    render(<IntakePanel />);

    await waitFor(() => {
      const input = screen.getByLabelText('Import/source path') as HTMLInputElement;
      expect(input.value).toBe('/data/custom-staging');
    });

    // Click Failed button
    const failedBtn = screen.getByRole('button', { name: 'Failed' });
    fireEvent.click(failedBtn);

    const input = screen.getByLabelText('Import/source path') as HTMLInputElement;
    expect(input.value).toBe('/data/custom-staging/failed_imports');

    // Click Downloads (Default source) button
    const defaultBtn = screen.getByRole('button', { name: 'Downloads' });
    fireEvent.click(defaultBtn);
    expect(input.value).toBe('/data/custom-staging');
  });

  it('renders approved roots selector buttons when multiple staging roots are configured', async () => {
    vi.mocked(client.getImportRoots).mockResolvedValue({
      ok: true,
      music_root: '/data/media/music',
      staging_roots: ['/data/torrents', '/data/manual-imports', '/data/bandcamp'],
      recommended_source: '/data/torrents',
      recommended_import_roots: ['/data/torrents', '/data/manual-imports', '/data/bandcamp'],
      failed_imports_root: '/data/torrents/failed_imports',
      last_saved_source: null,
    });

    render(<IntakePanel />);

    await waitFor(() => {
      expect(screen.getByText('Approved roots:')).toBeTruthy();
    });

    expect(screen.getByRole('button', { name: '/data/torrents' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '/data/manual-imports' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '/data/bandcamp' })).toBeTruthy();

    // Click /data/bandcamp root button
    fireEvent.click(screen.getByRole('button', { name: '/data/bandcamp' }));
    const input = screen.getByLabelText('Import/source path') as HTMLInputElement;
    expect(input.value).toBe('/data/bandcamp');
  });

  it('maps backend error codes to user-friendly messages', async () => {
    vi.mocked(client.getImportRoots).mockResolvedValue({
      ok: true,
      music_root: '/data/media/music',
      staging_roots: ['/data/torrents'],
      recommended_source: '/data/torrents',
      recommended_import_roots: ['/data/torrents'],
      failed_imports_root: '/data/torrents/failed_imports',
      last_saved_source: null,
    });

    vi.mocked(client.runPreflight).mockRejectedValueOnce(new Error('root_self_rejected'));

    render(<IntakePanel />);

    await waitFor(() => {
      expect((screen.getByLabelText('Import/source path') as HTMLInputElement).value).toBe('/data/torrents');
    });

    const previewBtn = screen.getByRole('button', { name: 'Preview Import All' });
    fireEvent.click(previewBtn);

    await waitFor(() => {
      expect(screen.getByText('Importing the root directory itself is not permitted. Please select or specify an album subfolder.')).toBeTruthy();
    });
  });
});
