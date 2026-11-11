import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { theme } from '../src/theme';

// #606: keyboard focus must be visible and muted text / field borders must meet WCAG contrast.
const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');
const css = src('styles.css');
const token = (name: string) => css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`))?.[1] ?? '';

const lum = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const v = parseInt(hex.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a: string, b: string) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const surfaces = ['bw-bg', 'bw-surface', 'bw-surface-2', 'bw-surface-3'].map(token);

describe('#606 focus and contrast tokens', () => {
  it('muted text is at least 4.5:1 on every app surface', () => {
    expect(token('color-muted')).toBe('#9aa1ad');
    for (const s of surfaces) expect(contrast(token('color-muted'), s)).toBeGreaterThanOrEqual(4.5);
  });

  it('field borders and the focus ring are at least 3:1 on every app surface', () => {
    for (const s of surfaces) {
      expect(contrast(token('color-field-border'), s)).toBeGreaterThanOrEqual(3);
      expect(contrast(token('bw-focus'), s)).toBeGreaterThanOrEqual(3);
    }
    const input = theme.components?.MuiOutlinedInput?.styleOverrides?.root as Record<string, { borderColor: string }>;
    expect(input['& .MuiOutlinedInput-notchedOutline'].borderColor).toBe(token('color-field-border'));
  });

  it('MUI buttons show a solid 2px outline on focus-visible, not a translucent shadow', () => {
    const root = theme.components?.MuiButtonBase?.styleOverrides?.root as Record<string, Record<string, unknown>>;
    expect(root['&.Mui-focusVisible']).toEqual({ outline: `2px solid ${token('bw-focus')}`, outlineOffset: 2 });
    expect(css).toMatch(/:focus-visible\s*\{\s*outline: 2px solid var\(--bw-focus\)/);
  });

  it('gear menu items style the Headless UI keyboard focus state', () => {
    const shell = src('components/layout/Shell.tsx');
    expect(shell.match(/data-\[focus\]:outline-red-400/g)).toHaveLength(5);
  });

  it('no zinc-500/600/700 text utilities remain outside the light-surface review panels', () => {
    for (const p of ['views/Jobs.tsx', 'views/System.tsx', 'views/Login.tsx', 'components/layout/Shell.tsx']) {
      expect(src(p)).not.toMatch(/text-zinc-[567]00/);
    }
  });
});
