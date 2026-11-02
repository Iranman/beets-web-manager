import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiJson, syncPlaylists } from '../src/api/client';
import { apiPost } from '../src/lib/api';
import { beginIdempotentRequest, followedJobStart, idempotencyErrorMessage } from '../src/lib/jobStart';
import { buildJobFeed, formatJobStatus, interruptedJobText, reviewQueueText } from '../src/lib/jobFeed';
import {
  MusicBrainzUnavailableNotice,
  acoustidNotCheckedText,
  fingerprintNotCheckedLabel,
} from '../src/components/MusicBrainzUnavailableNotice';
import { PlaylistSyncSummary, playlistSyncStatusLabel } from '../src/features/playlists/PlaylistSyncSummary';
import { versionLabel } from '../src/views/System';
import { rollbackSuccessMessage } from '../src/views/LibraryChanges';
import type { Job } from '../src/types/api';

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('job start idempotency (#344)', () => {
  it('sends an Idempotency-Key the backend accepts on mutating requests', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch').mockResolvedValue(jsonResponse(200, { ok: true, job_id: 'j1' }));
    await syncPlaylists();
    const headers = new Headers(fetchSpy.mock.calls[0][1]?.headers);
    expect(headers.get('Idempotency-Key')).toMatch(/^[A-Za-z0-9._:-]{8,128}$/);
  });

  it('does not send a key on GET', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch').mockResolvedValue(jsonResponse(200, { ok: true }));
    await apiJson('/api/x');
    expect(new Headers(fetchSpy.mock.calls[0][1]?.headers).has('Idempotency-Key')).toBe(false);
  });

  it('reuses the key when the same action is retried after a network failure', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true, job_id: 'j1' }))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true, job_id: 'j2' }));
    await expect(syncPlaylists(['A'])).rejects.toThrow();
    await syncPlaylists(['A']);
    await syncPlaylists(['A']);
    const key = (i: number) => new Headers(fetchSpy.mock.calls[i][1]?.headers).get('Idempotency-Key');
    expect(key(1)).toBe(key(0));
    // A delivered request settles the action; the next click is a new action.
    expect(key(2)).not.toBe(key(1));
  });

  it('gives a fresh key per action', () => {
    const a = beginIdempotentRequest('POST', '/api/a', '{}');
    const b = beginIdempotentRequest('POST', '/api/b', '{}');
    expect(a.key).not.toBe(b.key);
    a.settle();
    b.settle();
  });

  it('scopes a multipart key to the upload fields and file metadata (H9)', () => {
    const file = new File(['abc'], 'one.flac', { type: 'audio/flac', lastModified: 123 });
    const first = new FormData();
    first.append('file', file);
    const same = new FormData();
    same.append('file', new File(['abc'], 'one.flac', { type: 'audio/flac', lastModified: 123 }));
    const different = new FormData();
    different.append('file', new File(['abcd'], 'two.flac', { type: 'audio/flac', lastModified: 456 }));

    const a = beginIdempotentRequest('POST', '/api/upload-scope', first);
    const b = beginIdempotentRequest('POST', '/api/upload-scope', same);
    const c = beginIdempotentRequest('POST', '/api/upload-scope', different);
    expect(b.key).toBe(a.key);
    expect(c.key).not.toBe(a.key);
    a.settle();
    c.settle();
  });

  it('keeps the multipart key after a 5xx and settles after a 4xx', async () => {
    const form = new FormData();
    form.append('file', new File(['abc'], 'retry.flac', { type: 'audio/flac', lastModified: 123 }));
    const fetchSpy = vi.spyOn(global, 'fetch')
      .mockResolvedValueOnce(jsonResponse(500, { error: 'boom' }))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }))
      .mockResolvedValueOnce(jsonResponse(500, { error: 'boom' }))
      .mockResolvedValueOnce(jsonResponse(400, { error: 'bad request' }))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    const key = (i: number) => new Headers(fetchSpy.mock.calls[i][1]?.headers).get('Idempotency-Key');

    await expect(apiJson('/api/upload-retry', { method: 'POST', body: form })).rejects.toThrow();
    await apiJson('/api/upload-retry', { method: 'POST', body: form });
    expect(key(1)).toBe(key(0)); // 5xx keeps the action open

    await expect(apiJson('/api/upload-retry', { method: 'POST', body: form })).rejects.toThrow();
    expect(key(2)).not.toBe(key(1)); // the 200 settled the first action

    await expect(apiJson('/api/upload-retry', { method: 'POST', body: form })).rejects.toThrow();
    await apiJson('/api/upload-retry', { method: 'POST', body: form });
    expect(key(4)).not.toBe(key(3)); // a 4xx is definitive, so the next upload is new
  });

  it('follows the running job on 409 job_already_running (both clients)', async () => {
    vi.spyOn(global, 'fetch').mockImplementation(async () =>
      jsonResponse(409, { ok: false, code: 'job_already_running', job_id: 'run-1', status: 'running', replay: false }));
    await expect(syncPlaylists()).resolves.toMatchObject({ ok: true, job_id: 'run-1', already_running: true });
    await expect(apiPost('/api/jobs/x', {})).resolves.toMatchObject({ job_id: 'run-1', already_running: true });
  });

  it('follows job_already_started replays and ignores other 409s', () => {
    expect(followedJobStart(409, { code: 'job_already_started', job_id: 'j', replay: true })).toMatchObject({ job_id: 'j', replay: true });
    expect(followedJobStart(409, { code: 'not_cancellable' })).toBeNull();
    expect(followedJobStart(409, { code: 'job_already_running' })).toBeNull();
    expect(followedJobStart(200, { code: 'job_already_running', job_id: 'j' })).toBeNull();
  });

  it('explains 422 idempotency_key_reused and 400 invalid_idempotency_key', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValueOnce(jsonResponse(422, { ok: false, code: 'idempotency_key_reused', job_id: 'j' }));
    await expect(syncPlaylists()).rejects.toThrow(idempotencyErrorMessage('idempotency_key_reused'));
    vi.spyOn(global, 'fetch').mockResolvedValueOnce(jsonResponse(400, { ok: false, code: 'invalid_idempotency_key' }));
    await expect(apiPost('/api/x', {})).rejects.toThrow(/request key/);
    expect(idempotencyErrorMessage('other')).toBe('');
  });
});

describe('interrupted jobs and Review Queue pointer', () => {
  const base: Job = { job_id: 'j', label: 'Import', status: 'success', created_at: 1 };

  it('labels recovery_required and explains an interrupted job', () => {
    expect(formatJobStatus('recovery_required')).toBe('Recovery required');
    expect(interruptedJobText({ status: 'recovery_required', interrupted: true })).toMatch(/Library Changes/);
    expect(interruptedJobText({ status: 'failed', interrupted: true })).toMatch(/changed nothing/);
    expect(interruptedJobText({ status: 'failed' })).toBe('');
    const feed = buildJobFeed({ ...base, status: 'recovery_required', interrupted: true });
    expect(feed.summary.status).toBe('warning');
    expect(feed.summary.resultTitle).toMatch(/interrupted and needs recovery/);
  });

  it('points as-is refusals and unmatched folders at the Review Queue', () => {
    const job = { ...base, result: { albums_imported: 2, asis_refused: 1, not_matched: 3 } };
    expect(reviewQueueText(job)).toMatch(/1 not imported as-is, 3 with no confident match.*Review Queue/);
    const feed = buildJobFeed(job);
    expect(feed.summary.needsAttention).toBe(true);
    expect(feed.items.find((i) => i.id === 'final-j')?.detail).toMatch(/Review Queue/);
    expect(reviewQueueText({ result: { albums_imported: 2, asis_refused: 0 } })).toBe('');
  });
});

describe('not checked is never no match (#252 NF-4)', () => {
  it('maps AcoustID failures to not-checked notices', () => {
    expect(acoustidNotCheckedText('not_configured')).toMatch(/ACOUSTID_API_KEY/);
    expect(acoustidNotCheckedText('auth_failed')).toMatch(/rejected/);
    expect(acoustidNotCheckedText('lookup_failed')).toMatch(/not checked/);
    expect(acoustidNotCheckedText('no_result')).toBe('');
    expect(acoustidNotCheckedText('')).toBe('');
    expect(fingerprintNotCheckedLabel('unavailable', 'rate limited')).toBe('Not checked (rate limited)');
    expect(fingerprintNotCheckedLabel('auth_failed')).toBe('Not checked (auth failed)');
    expect(fingerprintNotCheckedLabel('no_result')).toBeNull();
  });

  it('renders the AcoustID notice next to the MusicBrainz one', () => {
    render(<MusicBrainzUnavailableNotice acoustidStatus="auth_failed" />);
    expect(screen.getByRole('status').textContent).toMatch(/AcoustID rejected the API key/);
  });
});

describe('playlist sync summary (#343)', () => {
  it('shows totals, next run and failures in the status line', () => {
    const label = playlistSyncStatusLabel({
      ok: true, enabled: true, interval: 3600, running: false, last_run: 1_700_000_000, next_run_at: 1_700_003_600,
      last_result: { playlists_updated: 2, playlists_failed: 1, playlists_skipped_busy: 1, plex_writes: 4, pending: 3 },
    });
    expect(label).toMatch(/Auto every 60 min/);
    expect(label).toMatch(/Next /);
    expect(label).toMatch(/1 failed · 1 skipped \(busy\) · 4 Plex writes · 3 pending/);
  });

  it('lists failed, busy and pending playlists with reasons, and the legacy import notice', () => {
    render(<PlaylistSyncSummary status={{
      ok: true, enabled: false, interval: 0, running: false,
      last_result: {
        legacy_imported: ['Old Mix'],
        details: [
          { name: 'Broken', updated: false, error: 'sync_failed', error_type: 'OSError' },
          { name: 'Busy', updated: false, skipped_reason: 'pipeline_running' },
          { name: 'Partly', updated: true, pending_count: 1, pending: [{ artist: 'A', title: 'T', reason: 'not in the Beets library' }] },
          { name: 'Fine', updated: true, pending_count: 0 },
        ],
      },
    }} />);
    expect(screen.getByText(/older playlists folder \(Old Mix\)/)).toBeTruthy();
    expect(screen.getByText(/Sync failed \(OSError\)/)).toBeTruthy();
    expect(screen.getByText(/download or import job for this playlist was running/)).toBeTruthy();
    expect(screen.getByText(/A - T: not in the Beets library/)).toBeTruthy();
    expect(screen.queryByText(/Fine/)).toBeNull();
  });
});

describe('System, transactions and qBittorrent', () => {
  it('shows a short revision next to the version', () => {
    expect(versionLabel('0.4.0', '0123456789abcdef')).toBe('Version 0.4.0 (0123456)');
    expect(versionLabel('0.4.0', 'unknown')).toBe('Version 0.4.0');
    expect(versionLabel('0.4.0')).toBe('Version 0.4.0');
  });

  it('reports restored and adopted rollback counts', () => {
    expect(rollbackSuccessMessage({ restored: 5, adopted: 2 })).toMatch(/5 items restored \(2 were already back/);
    expect(rollbackSuccessMessage({ restored: 1 })).toBe('Rollback finished: 1 item restored.');
    expect(rollbackSuccessMessage({ job_id: 'j' })).toBe('Rollback job started: j');
    expect(rollbackSuccessMessage({})).toBe('Rollback finished.');
  });

});
