import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Config from '../src/views/Config';
import { getMusicFormatPreferences, saveMusicFormatPreferences } from '../src/api/client';

// #609: after a failed preferences load the form holds defaults; saving must be impossible.
vi.mock('../src/api/client', () => ({
  getHealthDetail: vi.fn(() => new Promise(() => {})),
  getStats: vi.fn(() => new Promise(() => {})),
  getMbidStatus: vi.fn(() => new Promise(() => {})),
  getMusicFormatPreferences: vi.fn(),
  getMusicFormatReplacementStatuses: vi.fn().mockResolvedValue({ ok: true, tracks: [] }),
  saveMusicFormatPreferences: vi.fn(),
  startMusicFormatScan: vi.fn(),
  startMusicFormatReplacement: vi.fn(),
}));
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

describe('Music Format Preferences load failure (#609)', () => {
  it('keeps Save disabled after an edit and sends no POST when the load failed', async () => {
    vi.mocked(getMusicFormatPreferences).mockRejectedValue(new Error('Preferences service unavailable'));
    render(<Config />);

    expect(await screen.findByText('Preferences service unavailable')).toBeTruthy();

    // Edit a field the way a user would, then try to save.
    const box = screen.getAllByRole('checkbox').find((el) => !(el as HTMLInputElement).checked) as HTMLInputElement;
    fireEvent.click(box);
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    fireEvent.click(save);

    expect(save.disabled).toBe(true);
    expect(saveMusicFormatPreferences).not.toHaveBeenCalled();
    expect(screen.getByText(/could not be loaded, so the form is locked/)).toBeTruthy();
    const fieldset = screen.getByRole('group', { name: 'Music format preference fields' }) as HTMLFieldSetElement;
    expect(fieldset.disabled).toBe(true);
  });

  it('enables editing and Save once real preferences load', async () => {
    vi.mocked(getMusicFormatPreferences).mockResolvedValue({ ok: true, preferences: fakePrefs } as never);
    vi.mocked(saveMusicFormatPreferences).mockResolvedValue({ ok: true, preferences: fakePrefs } as never);
    render(<Config />);

    const fieldset = screen.getByRole('group', { name: 'Music format preference fields' }) as HTMLFieldSetElement;
    await waitFor(() => expect(fieldset.disabled).toBe(false));
    fireEvent.click(fieldset.querySelector('input[type="checkbox"]:not(:checked)') as HTMLInputElement);

    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(saveMusicFormatPreferences).toHaveBeenCalledTimes(1));
  });
});
