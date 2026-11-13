import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../src/api/client';
import Config from '../src/views/Config';
import System, { healthProblems } from '../src/views/System';
import type { SetupEnvResponse, SetupStatusResponse } from '../src/api/types';

// The System page is a short diagnostics page; config.yaml and Security are edited in Settings.

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
      zero: { configured: false, required: false, state: 'installed_but_disabled', category: 'beets_plugin' },
    },
    ...overrides,
  } as SetupStatusResponse;
}

const ENV: SetupEnvResponse = {
  ok: true,
  env_file: '/data/.env',
  exists: true,
  variables: [
    { name: 'MUSIC_ROOT', section: 'Storage & Paths', secret: false, configured: true, editable: true, value: '/music', source: 'environment' },
    { name: 'ACOUSTID_API_KEY', section: 'Music Services & Metadata', secret: true, configured: true, editable: true, revealable: true, value: '', source: 'persisted' },
    { name: 'BEETS_WEB_PASSWORD', section: 'Authentication & Security', secret: true, configured: true, editable: true, value: '', source: 'persisted' },
    { name: 'BEETS_WEB_USERNAME', section: 'Authentication & Security', secret: false, configured: true, editable: true, value: 'admin', source: 'persisted' },
  ],
} as SetupEnvResponse;

beforeEach(() => {
  mocked.getSetupStatus.mockResolvedValue(status());
  mocked.getSetupEnv.mockResolvedValue(ENV);
  mocked.getConfigFile.mockResolvedValue({ ok: true, content: 'plugins: musicbrainz\n', revision: 'r1', has_backup: true, backup_ts: null });
  mocked.getStats.mockResolvedValue({ artists: 1, albums: 1, tracks: 1 });
  mocked.getMbidStatus.mockResolvedValue({ ok: false } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const renderSystem = () => render(<MemoryRouter><System /></MemoryRouter>);

describe('System diagnostics page', () => {
  it('leads with one health line, then integrations and paths as rows', async () => {
    renderSystem();
    expect(await screen.findByRole('status')).toHaveProperty('textContent', 'All systems OK');
    expect(screen.getByText('Version 0.4.0')).toBeTruthy();
    expect(screen.getByText('Beets 2.14.1')).toBeTruthy();
    const integrations = screen.getByRole('region', { name: 'Integrations' });
    expect(within(integrations).getByText('MusicBrainz')).toBeTruthy();
    // Beets plugin integrations are reference detail, not service rows.
    expect(within(integrations).queryByText('zero')).toBeNull();
    const paths = screen.getByRole('region', { name: 'Paths' });
    expect(within(paths).getByText('/music')).toBeTruthy();
  });

  it('counts the backend blocking reasons and lists them word for word', async () => {
    mocked.getSetupStatus.mockResolvedValue(status({ status: 'warning', blocking_reasons: ['Cannot write to config directory /config'] }));
    renderSystem();
    expect(await screen.findByText('1 problem')).toBeTruthy();
    expect(within(screen.getByRole('list', { name: 'Problems' })).getByText('Cannot write to config directory /config')).toBeTruthy();
  });

  it('adds a failing required integration and unhealthy required plugins to the problems', () => {
    const s = status({
      integrations: { musicbrainz: { configured: true, required: true, state: 'connection_test_failed', category: 'service' } },
    });
    const plugins = { all_required_healthy: false, required_count: 5, required_healthy_count: 4 } as never;
    expect(healthProblems(s, plugins)).toEqual([
      'MusicBrainz: Connection test failed.',
      '1 of 5 required Beets plugins report a problem. Open Beets plugins below.',
    ]);
    expect(healthProblems(status(), null)).toEqual([]);
  });

  it('keeps reference material folded away and has no editing of config.yaml or sign-in', async () => {
    renderSystem();
    await screen.findByRole('status');
    const details = document.querySelectorAll('main details, details');
    expect(details.length).toBe(3);
    details.forEach((d) => expect((d as HTMLDetailsElement).open).toBe(false));
    expect(screen.getByText('Enabled plugins:')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Regenerate API token' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'config.yaml contents' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Settings › config.yaml' }).getAttribute('href')).toBe('/config#config-yaml');
    expect(screen.getByRole('link', { name: 'Settings › Security' }).getAttribute('href')).toBe('/config#security');
  });

  it('edits every env group except sign-in, and keeps secrets masked', async () => {
    renderSystem();
    expect(await screen.findByText('MUSIC_ROOT')).toBeTruthy();
    expect(screen.getByText('ACOUSTID_API_KEY')).toBeTruthy();
    expect(screen.queryByText('BEETS_WEB_PASSWORD')).toBeNull();
    expect(screen.queryByText('BEETS_WEB_USERNAME')).toBeNull();
    const secret = screen.getByPlaceholderText('••••••••••••••••') as HTMLInputElement;
    expect(secret.type).toBe('password');
    expect(secret.value).toBe('');
  });

  it('saves only the variables of its own group', async () => {
    mocked.saveSetupEnv.mockResolvedValue({ ...ENV, saved: ['MUSIC_ROOT'] });
    renderSystem();
    const field = await screen.findByDisplayValue('/music');
    fireEvent.change(field, { target: { value: '/media/music' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save environment' }));
    await waitFor(() => expect(mocked.saveSetupEnv).toHaveBeenCalledWith({ variables: { MUSIC_ROOT: '/media/music' }, clear: [] }));
  });
});

describe('Settings holds config.yaml and Security', () => {
  it('shows the config.yaml editor with its confirmation before saving', async () => {
    render(<Config />);
    const region = screen.getByRole('region', { name: 'config.yaml' });
    const editor = (await within(region).findByRole('textbox', { name: 'config.yaml contents' })) as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toBe('plugins: musicbrainz\n'));
    fireEvent.change(editor, { target: { value: 'plugins: musicbrainz fetchart\n' } });
    fireEvent.click(within(region).getByRole('button', { name: 'Save config.yaml' }));
    expect(await screen.findByText('Save config.yaml?')).toBeTruthy();
    expect(mocked.saveConfigFile).not.toHaveBeenCalled();
  });

  it('shows sign-in variables and asks before regenerating the API token', async () => {
    render(<Config />);
    const region = screen.getByRole('region', { name: 'Security' });
    expect(await within(region).findByText('BEETS_WEB_PASSWORD')).toBeTruthy();
    expect(within(region).queryByText('MUSIC_ROOT')).toBeNull();
    fireEvent.click(within(region).getByRole('button', { name: 'Regenerate API token' }));
    expect(await screen.findByText('Regenerate API token?')).toBeTruthy();
    expect(mocked.regenerateAuthToken).not.toHaveBeenCalled();
  });
});
