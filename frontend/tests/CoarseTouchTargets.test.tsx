import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// Coarse-pointer (touch) targets must reach 44px (2.75rem). MUI's emotion styles — including the
// theme's 32px MuiButton minHeight and MUI small inputs (~40px) — are unlayered, so the bump has to
// live outside @layer components (a layered rule always loses to an unlayered one) and stay two
// classes deep for the MUI surfaces so it outranks emotion on specificity. This mirrors the
// unlayered-rule proof in PhoneLayout607.test.tsx.
const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');
// Strip every @layer block, leaving only the unlayered CSS (same technique as #607).
const unlayered = (css: string) => css.replace(/@layer\s+\w+\s*\{[\s\S]*?\n\}/g, '');

describe('coarse-pointer touch targets', () => {
  const css = src('styles.css');

  it('the shared 44px coarse rule is unlayered and covers buttons, inputs, filters, fields and tabs', () => {
    const u = unlayered(css);
    // Isolate the shared touch-targets block (not the earlier .changes-page one).
    const touchBlock = u.slice(u.indexOf('Touch targets (unlayered)'), u.indexOf('end touch targets'));
    const block = touchBlock.match(
      /@media \(pointer: coarse\) \{([\s\S]*?)min-height: 2\.75rem;[\s\S]*?\n\}/,
    );
    expect(block, 'coarse 2.75rem rule must survive the @layer strip, i.e. be unlayered').toBeTruthy();
    const selectors = block![1];
    // MUI small buttons AND small inputs, on both hooks (ops pages and the .touch-controls pages).
    expect(selectors).toContain('.ops-page .MuiButton-sizeSmall');
    expect(selectors).toContain('.ops-page .MuiInputBase-sizeSmall');
    expect(selectors).toContain('.touch-controls .MuiButton-sizeSmall');
    expect(selectors).toContain('.touch-controls .MuiInputBase-sizeSmall');
    // Custom filter/field/tab surfaces.
    expect(selectors).toContain('.ops-filter');
    expect(selectors).toContain('.ops-field');
    expect(selectors).toContain('.crate-tab');
  });

  it('the MUI-scoped bumps are two classes deep so they beat emotion on specificity', () => {
    // A bare `.MuiInputBase-sizeSmall` would tie emotion's own single-class rule and lose on source
    // order; the hook prefix (.ops-page / .touch-controls) is what wins the cascade.
    expect(css).not.toMatch(/\n\s*\.MuiInputBase-sizeSmall\s*,/);
    expect(css).not.toMatch(/\n\s*\.MuiButton-sizeSmall\s*,/);
  });

  it('Library carries a touch hook so its MUI search/scope/sort fields get the bump', () => {
    // Library's root is neither an ops page nor the changes ledger, so without .touch-controls its
    // MUI small TextFields stay ~40px on coarse pointers.
    expect(src('views/Library.tsx')).toContain('<div className="touch-controls space-y-4">');
  });

  it('every in-scope page root wears a touch hook (ops-page or touch-controls)', () => {
    const hooked = (p: string, needle: string) => expect(src(p)).toContain(needle);
    hooked('views/Config.tsx', 'ops-page');
    hooked('views/Import.tsx', 'ops-page');
    hooked('views/Jobs.tsx', 'ops-page');
    hooked('views/Submissions.tsx', 'ops-page');
    hooked('views/LibraryChanges.tsx', 'ops-page');
    hooked('views/Playlists.tsx', 'touch-controls');
    hooked('views/System.tsx', 'touch-controls');
    hooked('views/Library.tsx', 'touch-controls');
  });

  it('does not touch the fine-pointer (desktop) appearance', () => {
    // The only non-coarse min-height in the block is the ops-page button's existing 40px base.
    const u = unlayered(css);
    const touchBlock = u.slice(u.indexOf('Touch targets (unlayered)'), u.indexOf('end touch targets'));
    const nonCoarse = touchBlock.split('@media (pointer: coarse)')[0];
    expect(nonCoarse).toContain('.ops-page .MuiButton-sizeSmall {\n  min-height: 2.5rem;');
    expect(nonCoarse).not.toContain('2.75rem');
  });
});
