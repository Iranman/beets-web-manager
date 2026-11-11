import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { theme } from '../src/theme';

// #607: phone layout must not overflow, wrap the nav, or stack sticky bars over the app header.
const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');

describe('#607 phone layout', () => {
  it('nav tabs live in a non-wrapping horizontal scroller with an inset focus ring', () => {
    const shell = src('components/layout/Shell.tsx');
    const nav = shell.match(/<nav aria-label="Primary"[^>]*className="([^"]+)"/)?.[1] ?? '';
    expect(nav.split(' ')).toEqual(expect.arrayContaining(['flex', 'min-w-0', 'overflow-x-auto']));
    expect(shell).not.toMatch(/flex-wrap items-center gap-x-1/);
    expect(shell).toContain('focus-visible:-outline-offset-2');
    expect(shell).toContain('h-[var(--bw-header-h)]');
  });

  it('Playlists sticky bars sit below the app header, under its z-index', () => {
    const playlists = src('views/Playlists.tsx');
    expect(playlists).not.toMatch(/sticky top-0/);
    expect(playlists.match(/sticky top-\[var\(--bw-header-h\)\]/g)).toHaveLength(2);
    expect(playlists).not.toMatch(/sticky[^"]*\bz-(?:[2-9]\d)\b/);
  });

  it('Submissions grids clamp columns and the action bar is in flow on phones', () => {
    const submissions = src('views/Submissions.tsx');
    expect(submissions).toContain('grid grid-cols-[minmax(0,1fr)] gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]');
    expect(submissions).toContain('grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-2');
    const bar = submissions.match(/data-testid="submission-action-bar" className="([^"]+)"/)?.[1] ?? '';
    expect(bar.split(' ')).toEqual(expect.arrayContaining(['sticky', 'bottom-0', 'md:fixed']));
    expect(bar.split(' ')).not.toContain('fixed');
  });

  it('page has an overflow-x clip backstop and button labels do not wrap', () => {
    expect(src('styles.css')).toMatch(/html,\s*body\s*\{\s*overflow-x:\s*clip;/);
    const root = theme.components?.MuiButton?.styleOverrides?.root as Record<string, unknown>;
    expect(root.whiteSpace).toBe('nowrap');
  });
});
