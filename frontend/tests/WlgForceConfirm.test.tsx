import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WLG_FORCE_CONFIRM } from '../src/features/plugins/PluginsPanel';

const src = readFileSync(resolve(__dirname, '../src/features/plugins/PluginsPanel.tsx'), 'utf8');

// QA #686 N5: "Force Re-tag All" (wlg -f) overwrites every genre tag, so it asks
// first, like the lastgenre force run in Jobs & Cleanup.
describe('wlg force re-tag asks for confirmation', () => {
  it('returns before running wlg -f when the confirm is declined', () => {
    expect(WLG_FORCE_CONFIRM).toMatch(/Overwrite all existing genre tags/);
    const guard = src.indexOf('if (!window.confirm(WLG_FORCE_CONFIRM)) return;');
    const runForce = src.indexOf("run(['wlg', '-f']");
    expect(guard).toBeGreaterThan(-1);
    expect(runForce).toBeGreaterThan(guard);
  });
});
