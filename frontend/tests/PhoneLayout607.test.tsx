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
    expect(shell).toContain('h-[var(--bw-header-h)]');
  });

  it('the tab inset focus ring actually overrides the unlayered global ring', () => {
    const css = src('styles.css');
    // The global rule is unlayered (so it beats `outline-none` utilities), and an unlayered rule beats
    // every non-important Tailwind v4 utility. Without `!` the inset offset never applies and the
    // nav scroller clips the ring.
    const unlayered = css.replace(/@layer\s+\w+\s*\{[\s\S]*?\n\}/g, '');
    expect(unlayered).toMatch(/:focus-visible\s*\{\s*outline: 2px solid var\(--bw-focus\);\s*outline-offset: 2px;/);
    const link = src('components/layout/Shell.tsx').match(/'flex shrink-0 items-center whitespace-nowrap[^']*'/)?.[0] ?? '';
    expect(link.split(/[' ]/)).toContain('focus-visible:-outline-offset-2!');
  });

  it('Playlists sticky bars sit below the app header, under its z-index', () => {
    const playlists = src('views/Playlists.tsx');
    // overflow-x-hidden would make the root a scroll container and stop the bars sticking.
    expect(playlists).toContain('<div className="touch-controls min-w-0 space-y-6 overflow-x-clip">');
    expect(playlists).not.toContain('overflow-x-hidden');
    expect(playlists).not.toMatch(/sticky top-0/);
    // The pipeline deck (the other sticky bar) moved to the Workbench's Playlist Downloads tab.
    expect(playlists.match(/sticky top-\[var\(--bw-header-h\)\]/g)).toHaveLength(1);
    expect(src('features/playlists/PlaylistPipelinePanel.tsx')).not.toMatch(/sticky top-0/);
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
