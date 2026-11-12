import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { theme } from '../src/theme';

// #610: disabled buttons must read as disabled (not primary) and the theme must give
// contained > outlined > text a clear primary / secondary / tertiary hierarchy.
type Rules = Record<string, Record<string, string>>;
const root = theme.components?.MuiButton?.styleOverrides?.root as unknown as Rules & Record<string, unknown>;
const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');

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

describe('#610 button hierarchy', () => {
  const disabled = root['&.MuiButton-root.Mui-disabled'];
  const primary = root['&.MuiButton-contained'];
  const secondary = root['&.MuiButton-outlined'];
  const tertiary = root['&.MuiButton-text'];

  it('disabled differs from the enabled primary and secondary styles', () => {
    expect(disabled.backgroundColor).not.toBe(primary.backgroundColor);
    expect(disabled.color).not.toBe(primary.color);
    expect(disabled.borderColor).not.toBe(secondary.borderColor);
    expect(disabled.color).not.toBe(secondary.color);
  });

  it('disabled label stays legible (>= 3:1) on every app surface', () => {
    for (const s of ['#171310', '#201a16', '#2a231d', '#342c25']) {
      expect(contrast(disabled.color, s)).toBeGreaterThanOrEqual(3);
    }
  });

  it('one disabled rule is declared after every variant rule so no colour variant overrides it', () => {
    const keys = Object.keys(root);
    const at = keys.indexOf('&.MuiButton-root.Mui-disabled');
    const variants = keys.filter((k) => /^&\.MuiButton-(contained|outlined|text)/.test(k) && !k.includes('Mui-disabled'));
    expect(variants.length).toBeGreaterThan(5);
    for (const v of variants) {
      expect(keys.indexOf(v)).toBeLessThan(at);
      expect(root[v]).not.toHaveProperty('&.Mui-disabled');
    }
    expect(root['button&.Mui-disabled']).toEqual({ pointerEvents: 'auto', cursor: 'not-allowed' });
  });

  it('primary is filled, secondary is bordered, tertiary has neither', () => {
    expect(primary.backgroundColor).toBe(theme.palette.primary.main);
    expect(secondary.borderColor).not.toBe(primary.backgroundColor);
    expect(tertiary).not.toHaveProperty('backgroundColor');
    expect(tertiary).not.toHaveProperty('border');
    expect(root['&.MuiButton-text.Mui-disabled']).toEqual({ backgroundColor: 'transparent' });
    for (const v of [primary, secondary, tertiary]) expect(v).toHaveProperty('&:active');
    expect(root.minHeight).toBe(32);
  });

  it('read-only and shortcut actions are not styled as primaries', () => {
    expect(src('views/Jobs.tsx')).toMatch(/variant="outlined" disabled=\{busy\} onClick=\{\(\) => void doScan\(\)\}/);
    expect(src('views/Jobs.tsx')).toMatch(/variant="outlined" onClick=\{\(\) => onApply\(issue\)\}>\s*Apply selected fixes/);
    const intake = src('features/intake/IntakePanel.tsx');
    expect(intake).toMatch(/variant="text"\s*onClick=\{\(\) => \{\s*setPath\(downloadsRoot\)/);
    expect(intake).toMatch(/variant="text"\s*onClick=\{\(\) => \{\s*setPath\(failedImportsPath\)/);
  });
});
