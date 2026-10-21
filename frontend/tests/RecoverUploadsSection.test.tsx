import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  RecoverUploadsSection,
  mapRecoverState,
} from '../src/features/libraryHealth/RecoverUploadsSection';
import {
  getRecoverRuns,
  importRecoverRun,
  recoverProbe,
} from '../src/api/client';

vi.mock('../src/api/client', () => ({
  getRecoverRuns: vi.fn(() => Promise.resolve({ ok: true, runs: [] })),
  importRecoverRun: vi.fn(),
  recoverProbe: vi.fn(),
  retryRecoverRun: vi.fn(),
  startRecoverFetch: vi.fn(),
}));

describe('mapRecoverState (backend verdict -> user label)', () => {
  it('maps each known provenance_status to its label', () => {
    expect(mapRecoverState('accessible').label).toBe('Available');
    expect(mapRecoverState('auth-needed').label).toBe('Authentication required');
    expect(mapRecoverState('unsupported-format').label).toBe('Unsupported format');
    expect(mapRecoverState('genuinely-DRM').label).toBe('DRM-protected');
    expect(mapRecoverState('unavailable').label).toBe('Provider unavailable');
    expect(mapRecoverState('expired-url').label).toBe('Provider unavailable');
    expect(mapRecoverState('recovered').label).toBe('Recovered');
    expect(mapRecoverState('verified').label).toBe('Verified');
    expect(mapRecoverState('imported').label).toBe('Imported');
  });

  it('shows DRM-protected ONLY for genuinely-DRM, never inferred from a failure', () => {
    expect(mapRecoverState('rate-limited').label).toBe('Download failed');
    expect(mapRecoverState('hls-assembly').label).toBe('Download failed');
    expect(mapRecoverState('unknown').label).toBe('Download failed');
    // An unknown/garbage value must not become DRM-protected or Available.
    const fallback = mapRecoverState('something-new');
    expect(fallback.label).toBe('Download failed');
  });

  it('falls back to category when provenance_status is empty', () => {
    expect(mapRecoverState('', 'genuinely-DRM').label).toBe('DRM-protected');
    expect(mapRecoverState(undefined, 'rate-limited').label).toBe('Download failed');
  });
});

describe('RecoverUploadsSection', () => {
  afterEach(() => { vi.resetAllMocks(); cleanup(); });

  it('renders the probe state label and sanitized diagnostics without any secret field', async () => {
    vi.mocked(getRecoverRuns).mockResolvedValue({ ok: true, runs: [] } as never);
    vi.mocked(recoverProbe).mockResolvedValue({
      ok: true,
      platform: 'youtube',
      title: 'My Upload',
      uploader: 'me',
      provenance_status: 'accessible',
      stream_diagnostics: {
        manifest_types: ['hls'],
        protocols: ['https'],
        drm_status: 'none',
        eme_detected: false,
        key_systems: [],
        best_audio_format: { ext: 'webm', acodec: 'opus', vcodec: 'none' },
        // Planted secret-looking fields that must NEVER be rendered.
        key: 'SECRET_CONTENT_KEY',
        kid: 'SECRET_KID',
        license: 'SECRET_LICENSE',
        cookie: 'SECRET_COOKIE',
        token: 'SECRET_TOKEN',
      },
    } as never);

    render(<RecoverUploadsSection />);
    fireEvent.change(screen.getByLabelText('Upload URL'), { target: { value: 'https://youtu.be/abc' } });
    fireEvent.click(screen.getByRole('button', { name: 'Probe' }));

    await screen.findByText('Available');
    expect(screen.getByText(/Container: webm/)).toBeTruthy();

    const body = document.body.textContent || '';
    for (const secret of ['SECRET_CONTENT_KEY', 'SECRET_KID', 'SECRET_LICENSE', 'SECRET_COOKIE', 'SECRET_TOKEN']) {
      expect(body).not.toContain(secret);
    }
  });

  it('requires an explicit confirm before importing a run', async () => {
    vi.mocked(getRecoverRuns).mockResolvedValue({
      ok: true,
      runs: [{ run_id: 'run-123456789012', status: 'recovered', recovered_count: 1, imported_count: 0, failed_count: 0 }],
    } as never);
    vi.mocked(importRecoverRun).mockResolvedValue({ ok: true } as never);

    render(<RecoverUploadsSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Import' }));
    // First click only arms the confirm; no mutation yet.
    expect(importRecoverRun).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm import' }));
    await waitFor(() => expect(importRecoverRun).toHaveBeenCalledWith('run-123456789012'));
  });
});
