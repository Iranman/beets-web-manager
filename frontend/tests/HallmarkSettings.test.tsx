import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../src/api/client';
import Config from '../src/views/Config';
import type { MusicFormatPreferences } from '../src/api/types';

// Settings: masthead with the library counts, an index of sections beside the work, deep links
// that land on their section after the sections above have loaded.

vi.mock('../src/api/client', () => ({
  apiErrorBody: vi.fn(),
  getMbidStatus: vi.fn(),
  getMusicFormatPreferences: vi.fn(),
  getMusicFormatReplacementStatuses: vi.fn(),
  getStats: vi.fn(),
  saveMusicFormatPreferences: vi.fn(),
  startMusicFormatReplacement: vi.fn(),
  startMusicFormatScan: vi.fn(),
}));
vi.mock('../src/features/plugins/PluginsPanel', () => ({ PluginsPanel: () => null }));
vi.mock('../src/features/system/ConfigYamlEditor', () => ({ ConfigYamlEditor: () => null }));
vi.mock('../src/features/system/SecuritySection', () => ({ SecuritySection: () => null }));

const mocked = vi.mocked(client);

const PREFS: MusicFormatPreferences = {
  allowed_layouts: { mono: false, stereo: true, '2.1': true, '5.1': false, '7.1': false, atmos: true },
  allow_atmos: true,
  custom_max_channels: null,
  preferred_formats: ['flac', 'mp3'],
  rejected_download_handling: 'quarantine',
  replacement_fallback: {
    keep_current: true,
    mark_needs_replacement: true,
    queue_retry: true,
    try_lower_ranked: true,
    try_alternate_source: true,
    allow_temporary_exception: false,
  },
};

beforeEach(() => {
  mocked.getStats.mockResolvedValue({ artists: 1200, albums: 3400, tracks: 41000 });
  mocked.getMbidStatus.mockResolvedValue({
    ok: true, root: '/music', total_albums: 100, missing_album_mb: 5, total_tracks: 1000, missing_track_mb: 50,
    item_release_gap_rows: 3, inferred_album_mbid_rows: 0, template_token_rows: 0, examples: [],
  });
  mocked.getMusicFormatPreferences.mockResolvedValue({ ok: true, preferences: PREFS, revision: 'r1' } as never);
  mocked.getMusicFormatReplacementStatuses.mockResolvedValue({ ok: true, tracks: [] } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, '', '/');
});

describe('Settings page', () => {
  it('sets the counts on the masthead and indexes every section', async () => {
    render(<Config />);
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' }).className).toContain('ops-title');
    expect(await screen.findByText('1,200 artists')).toBeTruthy();
    expect(screen.getByText('41,000 tracks')).toBeTruthy();

    const nav = screen.getByRole('navigation', { name: 'Settings sections' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['#musicbrainz', '#music-format', '#plugins', '#config-yaml', '#security']);
    for (const link of links) {
      const id = link.getAttribute('href')!.slice(1);
      const section = document.getElementById(id)!;
      expect(section.tagName).toBe('SECTION');
      // Each section is a named region whose h2 is its title.
      expect(within(section).getAllByRole('heading', { level: 2 })).toHaveLength(1);
    }
    expect(screen.getByRole('region', { name: 'Music Format Preferences' })).toBeTruthy();
  });

  it('keeps sub-groups under h3 and names the reorder buttons', async () => {
    render(<Config />);
    const region = screen.getByRole('region', { name: 'Music Format Preferences' });
    expect(within(region).getByRole('heading', { level: 3, name: 'Preferred formats' })).toBeTruthy();
    expect(await within(region).findByRole('button', { name: 'Move MP3 up' })).toBeTruthy();
    expect((within(region).getByRole('button', { name: 'Move FLAC up' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('lands a /config#section link on its section after the sections above load, until the reader takes over', () => {
    let relayout: () => void = () => {};
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { relayout = callback; }
      observe() {}
      disconnect() { relayout = () => {}; }
    });
    const scrolled: string[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.id); };
    try {
      window.history.replaceState(null, '', '/config#security');
      render(<Config />);
      expect(scrolled).toEqual(['security']);

      act(() => relayout()); // a section above finished loading and pushed the target down
      expect(scrolled).toEqual(['security', 'security']);

      window.dispatchEvent(new Event('wheel'));
      act(() => relayout());
      expect(scrolled).toEqual(['security', 'security']);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
