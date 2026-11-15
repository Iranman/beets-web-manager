import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../src/api/client';
import System from '../src/views/System';
import type { SetupStatusResponse } from '../src/api/types';

// Bolder Shelf elevation of System / Diagnostics: the restyle is additive (the .system-bolder scope
// hook, the shared RecordRing loading motif, .liner-label metric labels, the brass primary action).
// These tests pin the new structure; behaviour (content, status colours, actions) is covered by
// HallmarkSystem.test.tsx and ImpeccablePolish.test.tsx and stays unchanged.

vi.mock('../src/api/client', () => ({
  apiErrorBody: vi.fn(),
  authorizeSetupEnvReveal: vi.fn(),
  completeSetup: vi.fn(),
  getConfigFile: vi.fn(),
  getMbidStatus: vi.fn(),
  getMusicFormatPreferences: vi.fn(() => new Promise(() => {})),
  getMusicFormatReplacementStatuses: vi.fn(() => new Promise(() => {})),
  getSetupEnv: vi.fn(),
  getSetupStatus: vi.fn(),
  getStats: vi.fn(),
  regenerateAuthToken: vi.fn(),
  revealSetupEnvValue: vi.fn(),
  revertConfigFile: vi.fn(),
  saveConfigFile: vi.fn(),
  saveMusicFormatPreferences: vi.fn(),
  saveSetupEnv: vi.fn(),
  startMusicFormatReplacement: vi.fn(),
  startMusicFormatScan: vi.fn(),
  testSetupAcoustid: vi.fn(),
  testSetupAi: vi.fn(),
  testSetupMusicBrainz: vi.fn(),
  testSetupPlex: vi.fn(),
  verifyPlugins: vi.fn(),
}));
vi.mock('../src/features/plugins/PluginsPanel', () => ({ PluginsPanel: () => null }));

const mocked = vi.mocked(client);

function status(overrides: Partial<SetupStatusResponse> = {}): SetupStatusResponse {
  return {
    ok: true,
    status: 'ready',
    version: '0.4.0',
    demo_mode: false,
    setup_complete: true,
    blocking_reasons: [],
    paths: {
      config: { path: '/config', exists: true, writable: true },
      music_library: { path: '/music', exists: true },
      downloads: { path: '/downloads', exists: true, writable: true },
      beets_config: { path: '/config/config.yaml', exists: true },
    },
    fpcalc: { available: true, path: '/usr/bin/fpcalc' },
    beets: { available: true, version: '2.14.1', configured_plugins: ['musicbrainz', 'ftintitle'], plugin_loader_ok: true },
    auth: { token_configured: true, token_auto_generated: false, password_configured: true, username: 'admin' },
    integrations: {
      musicbrainz: { configured: true, required: true, state: 'connected', category: 'service' },
    },
    ...overrides,
  } as SetupStatusResponse;
}

beforeEach(() => {
  mocked.getSetupStatus.mockResolvedValue(status());
  mocked.getSetupEnv.mockResolvedValue({ ok: true, env_file: '/data/.env', exists: true, variables: [] } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const renderSystem = () => render(<MemoryRouter><System /></MemoryRouter>);

describe('Bolder System / Diagnostics', () => {
  it('shows the shared RecordRing spinner on first load, with a live status name', () => {
    // A status request that never resolves keeps the page in its loading state.
    mocked.getSetupStatus.mockReturnValue(new Promise(() => {}) as never);
    const { container } = renderSystem();
    expect(container.querySelector('.record-ring.is-spinning')).toBeTruthy();
    expect(screen.getByText('Loading diagnostics…')).toBeTruthy();
  });

  it('scopes the bolder world to the System page root and keeps the serif page title', async () => {
    const { container } = renderSystem();
    const root = await waitFor(() => {
      const el = container.querySelector('.system-bolder');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    // The title stays .lib-title (display serif) and lives inside the scoped root.
    const title = screen.getByRole('heading', { level: 1, name: 'Diagnostics' });
    expect(title.className).toContain('lib-title');
    expect(root.contains(title)).toBe(true);
  });

  it('makes the primary readiness action the brass contained button when setup is open', async () => {
    mocked.getSetupStatus.mockResolvedValue(status({ setup_complete: false }));
    renderSystem();
    const button = await screen.findByRole('button', { name: 'Mark system ready' });
    // The foundation's polished-brass primary button is MUI's "contained" variant.
    expect(button.className).toContain('MuiButton-contained');
  });

  it('dresses the metric labels as liner-note small-caps while keeping their text', async () => {
    renderSystem();
    const label = await screen.findByText('Enabled plugins:');
    expect(label.className).toContain('liner-label');
  });
});
