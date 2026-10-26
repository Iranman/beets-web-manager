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
// a second one. A definitive answer (< 500) settles the action; a 5xx or a
// network loss keeps it, because the server may have started the job.
//
// A multipart upload has no body string to fingerprint, so its key is scoped
// to the upload's own fields and file metadata. Sharing one key per
// method+url made two different uploads collide for ten minutes (H9).
const RETRY_WINDOW_MS = 10 * 60_000;
const openActions = new Map<string, { key: string; at: number }>();

function multipartFingerprint(body: unknown): string {
  if (typeof FormData !== 'undefined' && body instanceof FormData) {
    const parts: string[] = [];
    try {
      for (const [name, value] of body.entries()) {
        if (typeof value === 'string') {
          parts.push(`${name}=${value}`);
        } else {
          const file = value as File;
          parts.push(`${name}=file:${file.name}:${file.size}:${file.lastModified}:${file.type}`);
        }
      }
    } catch {
      return '';
    }
    return parts.join('\n');
  }
  if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
    return body.toString();
  }
  if (typeof Blob !== 'undefined' && body instanceof Blob) {
    return `blob:${body.size}:${body.type}`;
  }
  return '';
}

/** Begin a mutating request. Returns the key to send and a settle callback
 * to call on a definitive server answer (< 500). */
export function beginIdempotentRequest(method: string, url: string, body: unknown): { key: string; settle: () => void } {
  const isForm = typeof body !== 'string' && body !== undefined && body !== null;
  const detail = isForm ? multipartFingerprint(body) : String(body ?? '');
  const fingerprint = detail ? `${method} ${url}\n${detail}` : `${method} ${url}\n#no-body-detail`;
  const now = Date.now();
  const open = openActions.get(fingerprint);
  const key = open && now - open.at < RETRY_WINDOW_MS ? open.key : newKey();
  openActions.set(fingerprint, { key, at: now });
  // Cleared on any definitive answer, including a 4xx: the next deliberate
  // upload of the same file must get its own key. A 5xx/timeout keeps it for
  // the retry (#344).
  return { key, settle: () => openActions.delete(fingerprint) };
}
