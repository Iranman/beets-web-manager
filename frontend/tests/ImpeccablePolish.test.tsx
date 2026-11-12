import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Config from '../src/views/Config';

// Record shelf polish: optional integrations that are not set read as neutral, not as failures;
// required checks keep the red alarm. Page titles use the display serif.
vi.mock('../src/api/client', () => ({
  getHealthDetail: vi.fn().mockResolvedValue({
    ok: true,
    checks: {
      library_path: true,
      beet_bin: true,
      music_root: false,
      lidarr_key: false,
      discogs_token: false,
      slskd_key: true,
      openai_key: false,
    },
  }),
  getStats: vi.fn().mockResolvedValue({ ok: true, artists: 1, albums: 1, tracks: 1 }),
  getMbidStatus: vi.fn().mockResolvedValue({ ok: false }),
  getMusicFormatPreferences: vi.fn(() => new Promise(() => {})),
  getMusicFormatReplacementStatuses: vi.fn(() => new Promise(() => {})),
  saveMusicFormatPreferences: vi.fn(),
  startMusicFormatScan: vi.fn(),
  startMusicFormatReplacement: vi.fn(),
}));
vi.mock('../src/features/plugins/PluginsPanel', () => ({ PluginsPanel: () => null }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const dotFor = (label: string) => {
  const row = screen.getByText(label).closest('div.flex') as HTMLElement;
  return row.querySelector('span[aria-label]') as HTMLElement;
};

describe('Settings integration status', () => {
  it('marks a missing required check red and an unset optional integration neutral', async () => {
    render(<Config />);
    await screen.findByText('Lidarr');

    expect(dotFor('Music root').className).toContain('bg-red-500');
    expect(dotFor('Lidarr').className).not.toContain('bg-red-500');
    expect(dotFor('Lidarr').className).toContain('border-graphite-500');
    expect(dotFor('Beets library').className).toContain('bg-emerald-400');
  });

  it('sets the page title in the display serif', () => {
    render(<Config />);
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' }).className).toContain('font-display');
  });
});
