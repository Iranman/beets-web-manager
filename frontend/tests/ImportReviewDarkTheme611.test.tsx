import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #611: Import Review must use the graphite dark theme, not light-theme surfaces, violet headings or side stripes.
const dir = resolve(__dirname, '../src/features/importReview');
const files = readdirSync(dir).filter((f) => f.endsWith('.tsx'));
const LIGHT =
  /(?<![\w/-])(?:[\w-]+:)*(?:bg-white|(?:bg|text|border)-(?:zinc|violet|purple)-\d+|bg-(?:graphite|amber|rose|emerald|sky|cyan|red)-(?:50|100|200)|border-l-4)(?![\w-])/g;

describe('#611 Import Review dark theme', () => {
  it.each(files)('%s has no light-theme, violet or side-stripe classes', (file) => {
    expect(readFileSync(resolve(dir, file), 'utf8').match(LIGHT) ?? []).toEqual([]);
  });
});
