import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { WarningIcon } from '../src/components/WarningIcon';

// #612: guard against the generated-looking tells removed from the UI.
const root = resolve(__dirname, '../src');
const walk = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : /\.tsx?$/.test(e.name) ? [join(dir, e.name)] : [],
  );
const sources = walk(root).map((file) => ({ file, text: readFileSync(file, 'utf8') }));
const offenders = (re: RegExp) => sources.filter(({ text }) => re.test(text)).map(({ file }) => file);

describe('template tells (#612)', () => {
  it('uses no all-caps or tracked label classes', () => {
    expect(offenders(/["'`\s]uppercase["'`\s]/)).toEqual([]);
    expect(offenders(/tracking-wid(e|er|est)\b/)).toEqual([]);
  });

  it('uses no labels below 12px', () => {
    expect(offenders(/text-\[(0\.6\d*rem|10px)\]/)).toEqual([]);
  });

  it('uses no emoji warning glyphs', () => {
    expect(offenders(/⚠/)).toEqual([]);
  });

  it('hides every inline SVG from assistive tech', () => {
    const svgs = sources.flatMap(({ text }) => text.match(/<svg\b[^>]*>/g) ?? []);
    expect(svgs.length).toBeGreaterThan(0);
    expect(svgs.filter((tag) => !tag.includes('aria-hidden="true"'))).toEqual([]);
  });

  it('uses no transition-all and only motion-safe pulses', () => {
    expect(offenders(/transition-all/)).toEqual([]);
    expect(offenders(/(?<!motion-safe:)animate-pulse/)).toEqual([]);
  });

  it('has no radial glow behind pages', () => {
    expect(readFileSync(join(root, 'styles.css'), 'utf8')).not.toMatch(/radial-gradient/);
  });

  it('renders the warning icon as decoration', () => {
    const { container } = render(<WarningIcon />);
    expect(container.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
  });
});
