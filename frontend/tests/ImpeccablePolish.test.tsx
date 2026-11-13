import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Config from '../src/views/Config';
import System from '../src/views/System';

// Record shelf polish: optional integrations that are not set read as neutral, not as failures;
// required ones that fail keep the alarm. Page titles use the display serif.
// Integration status lives on System (it moved there from Settings so it has one home).
vi.mock('../src/api/client', () => ({
  getSetupStatus: vi.fn().mockResolvedValue({
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
    auth: { token_configured: true, token_auto_generated: false, password_configured: true },
    beets: { available: true, plugin_loader_ok: true, configured_plugins: ['musicbrainz'] },
    integrations: {
      musicbrainz: { configured: false, required: true, state: 'connection_test_failed', category: 'service' },
      lidarr: { configured: false, required: false, state: 'not_configured', category: 'service' },
      plex: { configured: true, required: false, state: 'configured', category: 'service' },
      zero: { configured: false, required: false, state: 'installed_but_disabled', category: 'beets_plugin', note: 'Plugin is installed but not enabled in config.yaml.' },
    },
  }),
  getSetupEnv: vi.fn().mockResolvedValue({ ok: true, env_file: '/data/.env', exists: true, variables: [] }),
  getStats: vi.fn().mockResolvedValue({ ok: true, artists: 1, albums: 1, tracks: 1 }),
  getMbidStatus: vi.fn().mockResolvedValue({ ok: false }),
  getMusicFormatPreferences: vi.fn(() => new Promise(() => {})),
  getMusicFormatReplacementStatuses: vi.fn(() => new Promise(() => {})),
  getConfigFile: vi.fn(() => new Promise(() => {})),
  saveMusicFormatPreferences: vi.fn(),
  startMusicFormatScan: vi.fn(),
  startMusicFormatReplacement: vi.fn(),
  completeSetup: vi.fn(),
  verifyPlugins: vi.fn(),
  testSetupAi: vi.fn(),
  testSetupMusicBrainz: vi.fn(),
  testSetupAcoustid: vi.fn(),
  testSetupPlex: vi.fn(),
  regenerateAuthToken: vi.fn(),
  saveSetupEnv: vi.fn(),
  revealSetupEnvValue: vi.fn(),
  authorizeSetupEnvReveal: vi.fn(),
  apiErrorBody: vi.fn(),
  saveConfigFile: vi.fn(),
  revertConfigFile: vi.fn(),
}));
vi.mock('../src/features/plugins/PluginsPanel', () => ({ PluginsPanel: () => null }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const stateFor = async (name: string) => {
  const row = (await screen.findByText(name)).closest('li') as HTMLElement;
  // Rows are name, state, detail in the DOM (state before detail for text order and screen readers).
  return row.children[1] as HTMLElement;
};

describe('System integration status', () => {
  it('marks a failing required integration amber and an unset optional one neutral', async () => {
    render(<MemoryRouter><System /></MemoryRouter>);
    expect((await stateFor('MusicBrainz')).className).toContain('text-amber-300');
    expect((await stateFor('lidarr')).className).toContain('text-zinc-300');
    expect((await stateFor('lidarr')).textContent).toContain('Not configured');
    expect((await stateFor('plex')).className).toContain('text-emerald-300');
  });

  it('reads a plugin row as name then state, before its note (e2e 09 and screen readers)', async () => {
    render(<MemoryRouter><System /></MemoryRouter>);
    const row = (await screen.findByText('zero')).closest('li') as HTMLElement;
    expect(row.textContent).toMatch(/^zero\W*Installed but disabled/);
    expect(row.textContent).toContain('Plugin is installed but not enabled in config.yaml.');
  });

  it('sets the page titles in the display serif', async () => {
    render(<Config />);
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' }).className).toContain('ops-title');
    cleanup();
    render(<MemoryRouter><System /></MemoryRouter>);
    expect((await screen.findByRole('heading', { level: 1, name: 'Diagnostics' })).className).toContain('lib-title');
  });
});
