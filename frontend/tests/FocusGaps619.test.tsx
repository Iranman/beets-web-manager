import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #619: `outline-none` removes the global `:focus-visible` ring (#606). It may only appear on a
// line that supplies its own focus indicator, or on a programmatic focus target (tabIndex={-1}).
const root = resolve(__dirname, '../src');
const files = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? files(join(dir, e.name)) : /\.tsx?$/.test(e.name) ? [join(dir, e.name)] : [],
  );

describe('#619 no control hides keyboard focus', () => {
  it('every outline-none is paired with a replacement focus style', () => {
    const offenders = files(root).flatMap((f) =>
      readFileSync(f, 'utf8')
        .split('\n')
        .map((line, i) => ({ line, at: `${f.slice(root.length + 1)}:${i + 1}` }))
        .filter(({ line }) => /\boutline-none\b/.test(line))
        .filter(({ line }) => !/focus(-visible)?:(border|ring)-|tabIndex=\{-1\}/.test(line))
        .map(({ at }) => at),
    );
    expect(offenders).toEqual([]);
  });
});
