import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// Record shelf, slice 2: brand and selection use brass; red is left for errors and destructive actions.
const root = resolve(__dirname, '../src');
const src = (p: string) => readFileSync(resolve(root, p), 'utf8');
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

const sources = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? sources(p) : /\.tsx?$/.test(name) ? [p] : [];
  });

describe('record shelf theme, slice 2', () => {
  it('Login and first-run setup use the shared record mark and brass primary buttons', () => {
    for (const file of ['views/Login.tsx', 'components/FirstRunSetup.tsx']) {
      const s = src(file);
      expect(s).toContain("import { RecordMark } from '");
      expect(s).not.toContain('BeetsLogo');
      expect(s).not.toMatch(/bg-red-700|focus:border-red-|ring-red-/);
      expect(s).toContain('bg-brass-500');
    }
    expect(src('components/layout/Shell.tsx')).toContain("import { RecordMark } from '../RecordMark'");
  });

  it('no checkbox uses a red accent and no component uses a hex colour in a style prop', () => {
    for (const file of sources(root)) {
      const s = readFileSync(file, 'utf8');
      expect(s, file).not.toContain('accent-red-');
      if (file.endsWith('theme.ts')) continue;
      expect(s, file).not.toMatch(/(?:backgroundColor|bgcolor|borderColor|color|fill|stroke)[=:] ?['"]#[0-9a-fA-F]{3,8}['"]/);
    }
  });

  it('brass buttons, the disabled look and the selector hover meet WCAG AA', () => {
    // Dark label on brass fill, at rest and on hover.
    expect(contrast(token('color-graphite-950'), token('color-brass-500'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('color-graphite-950'), token('color-brass-400'))).toBeGreaterThanOrEqual(4.5);
    // Disabled brass buttons fall back to the #610 disabled look.
    expect(contrast(token('color-graphite-400'), token('color-graphite-900'))).toBeGreaterThanOrEqual(4.5);
    // Unselected Jobs selectors: label on the hover fill (was zinc-400, 3.8:1).
    expect(contrast(token('color-zinc-300'), token('color-graphite-700'))).toBeGreaterThanOrEqual(4.5);
    const jobs = src('views/Jobs.tsx');
    expect(jobs).not.toMatch(/text-zinc-400 hover:bg-graphite-700/);
    expect(jobs.match(/bg-graphite-800 text-zinc-300 hover:bg-graphite-700 hover:text-zinc-50/g)).toHaveLength(2);
  });

  it('the Library cover grid fits two columns on phones and lets tile actions wrap', () => {
    const library = src('views/Library.tsx');
    expect(library).toContain('grid-cols-[repeat(auto-fill,minmax(min(100%,8.5rem),1fr))]');
    expect(library).toContain("const tileButtonSx = { whiteSpace: 'normal'");
    expect(library.match(/sx=\{tileButtonSx\}/g)).toHaveLength(4);
  });
});
