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
    expect(offenders(/text-\[(0\.6\d*rem|1[01]px)\]/)).toEqual([]);
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

  it('uses sentence case for plugin readiness labels', () => {
    expect(offenders(/Required Plugins (Healthy|Ready)/)).toEqual([]);
    expect(offenders(/`✓ All \$\{/)).toEqual([]);
  });

  it('capitalises only the first letter of the album folder issue kind', () => {
    const jobs = readFileSync(join(root, 'views/Jobs.tsx'), 'utf8');
    expect(jobs).toContain('text.charAt(0).toUpperCase() + text.slice(1)');
  });

  it('uses no decorative dot joins in fixed copy', () => {
    expect(offenders(/'Recovery · |'Target exists · |AcoustID · available/)).toEqual([]);
  });

  it('renders the warning icon as decoration', () => {
    const { container } = render(<WarningIcon />);
    expect(container.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
  });
});
