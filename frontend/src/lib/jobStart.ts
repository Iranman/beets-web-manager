// Shared by both fetch helpers (lib/api.ts apiFetch, api/client.ts apiJson):
// the job-start contract of routes_jobs.py (#344).

/** Body a job start returns when nothing new was started because the same
 * job is already running (`job_already_running`) or this request's
 * Idempotency-Key already started it (`job_already_started`). The UI follows
 * `job_id` instead of showing an error. */
export interface FollowedJobStart {
  ok: true;
  job_id: string;
  already_running: true;
  replay: boolean;
  job_status?: string;
}

const FOLLOW_CODES = new Set(['job_already_running', 'job_already_started']);

export function followedJobStart(httpStatus: number, body: unknown): FollowedJobStart | null {
  if (httpStatus !== 409 || !body || typeof body !== 'object') return null;
  const b = body as { code?: unknown; error_code?: unknown; job_id?: unknown; replay?: unknown; status?: unknown };
  const code = typeof b.code === 'string' ? b.code : typeof b.error_code === 'string' ? b.error_code : '';
  if (!FOLLOW_CODES.has(code) || typeof b.job_id !== 'string' || !b.job_id) return null;
  return {
    ok: true,
    job_id: b.job_id,
    already_running: true,
    replay: b.replay === true,
    ...(typeof b.status === 'string' ? { job_status: b.status } : {}),
  };
}

/** User-facing text for the Idempotency-Key refusals; '' for any other code. */
export function idempotencyErrorMessage(code: string): string {
  if (code === 'idempotency_key_reused') {
    return 'Nothing new was started: this retry did not match the request that first used its key. Reload the page, check Jobs for a run that already started, then start the action again if needed.';
  }
  if (code === 'invalid_idempotency_key') {
    return 'Nothing was started: the server rejected the request key this browser sent. Reload the page and try again; if it keeps happening, update Web Manager so the web UI and server versions match.';
  }
  return '';
}

function newKey(): string {
  // randomUUID needs a secure context; plain-HTTP LAN installs fall back to
  // getRandomValues (both 128-bit, both match the server's key format).
  if (typeof crypto.randomUUID === 'function') {
    try {
      return crypto.randomUUID();
    } catch {
      // insecure context
    }
  }
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
}

// One key per user action: an identical request sent again while the first
// is in flight, or after it got no answer (network loss, timeout), reuses the
// key, so the server reports the job it already started instead of starting
// a second one. Any HTTP answer settles the action.
const RETRY_WINDOW_MS = 10 * 60_000;
const openActions = new Map<string, { key: string; at: number }>();

/** Begin a mutating request. Returns the key to send and a settle callback
 * to call once the server answered (any HTTP status). */
export function beginIdempotentRequest(method: string, url: string, body: unknown): { key: string; settle: () => void } {
  if (typeof body !== 'string' && body !== undefined && body !== null) {
    // FormData cannot be fingerprinted; reuse the most recent key for this
    // method+url within the window, and NEVER settle it on a 5xx retry.
    // A 5xx must not discard the key or we start a second job on retry (#344).
    const fingerprint = `${method} ${url}`;
    const now = Date.now();
    const open = openActions.get(fingerprint);
    const key = open && now - open.at < RETRY_WINDOW_MS ? open.key : newKey();
    openActions.set(fingerprint, { key, at: now });
    return { key, settle: () => {} };
  }
  const fingerprint = `${method} ${url}\n${body ?? ''}`;
  const now = Date.now();
  const open = openActions.get(fingerprint);
  const key = open && now - open.at < RETRY_WINDOW_MS ? open.key : newKey();
  openActions.set(fingerprint, { key, at: now });
  return { key, settle: () => openActions.delete(fingerprint) };
}
