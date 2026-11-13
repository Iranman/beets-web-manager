import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Config from '../src/views/Config';
import { getMusicFormatPreferences, saveMusicFormatPreferences } from '../src/api/client';

// #623: a save from a stale load gets 409; the UI says so and keeps the user's edits.
vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    apiErrorBody: actual.apiErrorBody,
    getHealthDetail: vi.fn(() => new Promise(() => {})),
    getStats: vi.fn(() => new Promise(() => {})),
    getMbidStatus: vi.fn(() => new Promise(() => {})),
    getMusicFormatPreferences: vi.fn(),
    getMusicFormatReplacementStatuses: vi.fn().mockResolvedValue({ ok: true, tracks: [] }),
    saveMusicFormatPreferences: vi.fn(),
    startMusicFormatScan: vi.fn(),
    startMusicFormatReplacement: vi.fn(),
  };
});
vi.mock('../src/features/plugins/PluginsPanel', () => ({ PluginsPanel: () => null }));
vi.mock('../src/features/system/ConfigYamlEditor', () => ({ ConfigYamlEditor: () => null }));
vi.mock('../src/features/system/SecuritySection', () => ({ SecuritySection: () => null }));

const fakePrefs = {
  allowed_layouts: { stereo: true },
  preferred_formats: ['flac', 'mp3'],
  rejected_download_handling: 'reject',
  replacement_fallback: { keep_current: true },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Music Format Preferences revision conflict (#623)', () => {
  it('sends the loaded revision and keeps edits when the save gets 409', async () => {
    vi.mocked(getMusicFormatPreferences).mockResolvedValue({ ok: true, preferences: fakePrefs, revision: 'rev-1' } as never);
    const conflict = Object.assign(new Error('Preferences changed elsewhere; reload before saving.'), {
      httpStatus: 409,
      body: { ok: false, code: 'music_format_revision_conflict' },
    });
    vi.mocked(saveMusicFormatPreferences).mockRejectedValue(conflict);
    render(<Config />);

    const fieldset = screen.getByRole('group', { name: 'Music format preference fields' }) as HTMLFieldSetElement;
    await waitFor(() => expect(fieldset.disabled).toBe(false));
    const box = fieldset.querySelector('input[type="checkbox"]:not(:checked)') as HTMLInputElement;
    fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText(/changed elsewhere .* Your edits are still in the form/)).toBeTruthy();
    expect(vi.mocked(saveMusicFormatPreferences).mock.calls[0][1]).toBe('rev-1');
    expect(box.checked).toBe(true);
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
