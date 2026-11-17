import { describe, expect, it } from 'vitest';
import { buildJobFeed } from '../src/lib/jobFeed';
import type { Job } from '../src/types/api';

// #762: a recovery-advance Job carries the rolling missing-track-recovery status
// as its result. The job detail must explain what the sweep did (recovered /
// still-unresolved / changed?) instead of a bare "completed", so that a sweep
// that changed nothing is visibly distinct from one that recovered something.

function recoveryJob(result: Record<string, unknown>, status: Job['status'] = 'success'): Job {
  return {
    job_id: 'rec-1',
    label: 'Advance missing-track recovery',
    status,
    created_at: 1,
    metadata: { type: 'recovery-advance' },
    result,
  };
}

const CHANGED = {
  changes_made: true,
  needs_attention: false,
  error: '',
  trigger: 'manual',
  counts: { examined: 12, scanned: 12, recovered: 3, handed_off: 3, still_unresolved: 2, kept_review: 2, skipped: 1, newly_review: 0 },
};

const IDLE = {
  changes_made: false,
  needs_attention: false,
  error: '',
  trigger: 'scheduled',
  counts: { examined: 8, scanned: 8, recovered: 0, handed_off: 0, still_unresolved: 0, kept_review: 0, skipped: 8, newly_review: 0 },
};

describe('recovery-advance job detail (#762)', () => {
  it('reports what was recovered instead of a bare completed', () => {
    const feed = buildJobFeed(recoveryJob(CHANGED));
    expect(feed.summary.resultTitle).toBe('Recovered 3 missing tracks.');
    expect(feed.summary.resultBullets).toEqual(
      expect.arrayContaining(['12 examined', '3 recovered', '2 still unresolved', 'Changed: yes']),
    );
    expect(feed.summary.needsAttention).toBe(false);
  });

  it('makes a no-change sweep visibly distinct from one that recovered something', () => {
    const feed = buildJobFeed(recoveryJob(IDLE));
    expect(feed.summary.resultTitle).toBe('Recovery sweep ran. No changes made.');
    expect(feed.summary.resultBullets).toEqual(expect.arrayContaining(['Changed: no', 'Background sweep']));
    expect(feed.summary.resultBullets).not.toContain('Changed: yes');
  });

  it('surfaces newly-review items as needs-attention', () => {
    const feed = buildJobFeed(recoveryJob({
      ...IDLE,
      needs_attention: true,
      counts: { ...IDLE.counts, newly_review: 2 },
    }));
    expect(feed.summary.needsAttention).toBe(true);
    expect(feed.summary.status).toBe('warning');
    expect(feed.summary.resultTitle).toMatch(/2 newly need review/);
  });

  it('surfaces a sweep error distinctly', () => {
    const feed = buildJobFeed(recoveryJob({ ...CHANGED, changes_made: false, needs_attention: true, error: 'OSError' }));
    expect(feed.summary.needsAttention).toBe(true);
    expect(feed.summary.resultTitle).toBe('Recovery sweep finished with an error.');
  });

  it('leaves non-recovery jobs unchanged', () => {
    const feed = buildJobFeed({ job_id: 'x', label: 'Folder scan', status: 'success', created_at: 1, result: { scanned: 5 } });
    expect(feed.summary.resultTitle).toBe('Folder scan completed.');
  });
});
