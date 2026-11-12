import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');

// QA #686 N7: POST /api/plugins/run was removed by SEC-002, so no UI may call it.
describe('no controls call the removed plugin runner', () => {
  it('the client and Plugin Controls no longer reference /api/plugins/run', () => {
    expect(src('api/client.ts')).not.toContain('/api/plugins/run');
    const panel = src('features/plugins/PluginsPanel.tsx');
    expect(panel).not.toContain('runPlugin');
    expect(panel).not.toMatch(/Force Re-tag All|YouTube Import|AI Metadata|View Counts/);
    expect(panel).toContain('<PlexPlugin />');
  });
});
