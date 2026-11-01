import { beginIdempotentRequest, followedJobStart, idempotencyErrorMessage } from './jobStart';

export type ApiErrorCategory = 'network' | 'engine_offline' | 'engine_auth' | 'auth' | 'timeout' | 'http_error';

export class ApiError extends Error {
  httpStatus: number;
  category: ApiErrorCategory;

  constructor(status: number, message: string, category: ApiErrorCategory = 'http_error') {
    super(message);
    this.name = 'ApiError';
    this.httpStatus = status;
    this.category = category;
  }
}

export function formatTransportErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return err.message;
  }
  if (err instanceof Error) {
    const msg = err.message || '';
    if (msg.includes('Failed to fetch') || msg.includes('NetworkError') || msg.includes('Failed to communicate')) {
      return 'Could not reach Beets Web Manager.';
    }
    if (msg.includes('AbortError') || msg.includes('TimeoutError') || msg.includes('timed out')) {
      return 'Request timed out.';
    }
    return msg;
  }
  return String(err || 'Unknown error');
}

const CSRF_EXEMPT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/** Adds the CSRF header and Idempotency-Key every mutating request carries.
 * `settle` forgets the key once the outcome is known. */
export function prepareRequestHeaders(
  method: string,
  url: string,
  body: RequestInit['body'],
  headers: RequestInit['headers'],
): { headers: RequestInit['headers']; settle: () => void } {
  if (CSRF_EXEMPT_METHODS.has(method)) return { headers, settle: () => {} };
  const merged = new Headers(headers);
  if (!merged.has('X-Beets-CSRF')) merged.set('X-Beets-CSRF', '1');
  let settle = () => {};
  if (!merged.has('Idempotency-Key')) {
    const action = beginIdempotentRequest(method, url, body);
    merged.set('Idempotency-Key', action.key);
    settle = action.settle;
  }
  return { headers: merged, settle };
}

/** What a rejected fetch() means for the user. */
export function fetchFailure(err: unknown): { message: string; isTimeout: boolean } {
  const name = (err as Error)?.name || '';
  const isTimeout = name === 'AbortError' || name === 'TimeoutError';
  return { message: isTimeout ? 'Request timed out.' : 'Could not reach Beets Web Manager.', isTimeout };
}

export const SESSION_EXPIRED_MESSAGE = 'Your session expired. Sign in again.';
export const ENGINE_OFFLINE_MESSAGE = 'Beets engine is unavailable.';
export const ENGINE_AUTH_MESSAGE = 'Beets engine authentication failed.';

/** Classifies a non-2xx answer from its status and structured error code only,
 * never its free text. A provider outage (e.g. MusicBrainz) is a 503 with
 * unavailable:true and is not a Beets engine outage. */
export function classifyHttpFailure(status: number, errorCode: string, unavailable: boolean) {
  const isEngineAuthError = errorCode === 'ENGINE_AUTH_FAILED';
  return {
    isEngineAuthError,
    isAuthError: (status === 401 || status === 403 || errorCode === 'AUTH_FAILED' || errorCode === 'USER_AUTH_FAILED') && !isEngineAuthError,
    isEngineOffline: errorCode === 'ENGINE_OFFLINE' || (status === 503 && !errorCode && !unavailable),
  };
}

async function apiFetch<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const method = (opts.method ?? 'GET').toUpperCase();
  const { headers, settle } = prepareRequestHeaders(method, url, opts.body, opts.headers);

  let res: Response;
  try {
    res = await fetch(url, { ...opts, headers });
  } catch (err: unknown) {
    const { message, isTimeout } = fetchFailure(err);
    throw new ApiError(0, message, isTimeout ? 'timeout' : 'network');
  }
  // A 5xx may still have started the job server-side: keep the key so the
  // retry follows that job instead of starting a second one (#344).
  if (res.status < 500) {
    settle();
  }

  if (!res.ok) {
    let serverMessage = `HTTP ${res.status}`;
    let errorCode = '';
    let providerUnavailable = false;
    try {
      const body = (await res.json()) as { error?: string; error_code?: string; code?: string; unavailable?: boolean };
      // Same job already running, or this key already started it: follow it.
      const followed = followedJobStart(res.status, body);
      if (followed) return followed as T;
      providerUnavailable = body?.unavailable === true;
      if (typeof body?.error === 'string' && body.error) {
        serverMessage = body.error;
      }
      if (typeof body?.error_code === 'string') {
        errorCode = body.error_code;
      }
      serverMessage = idempotencyErrorMessage(body?.code ?? '') || serverMessage;
    } catch {
      // ignore JSON parse failure
    }

    const failure = classifyHttpFailure(res.status, errorCode, providerUnavailable);
    if (failure.isEngineAuthError) throw new ApiError(res.status, ENGINE_AUTH_MESSAGE, 'engine_auth');
    if (failure.isAuthError) throw new ApiError(res.status, SESSION_EXPIRED_MESSAGE, 'auth');
    if (failure.isEngineOffline) throw new ApiError(res.status, ENGINE_OFFLINE_MESSAGE, 'engine_offline');
    throw new ApiError(res.status, serverMessage, 'http_error');
  }

  return res.json() as Promise<T>;
}

export function apiGet<T>(url: string): Promise<T> {
  return apiFetch<T>(url);
}

export function apiPost<T>(url: string, body?: unknown): Promise<T> {
  return apiFetch<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

export function apiDelete<T>(url: string, body?: unknown): Promise<T> {
  return apiFetch<T>(url, {
    method: 'DELETE',
    headers: body !== undefined
      ? { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' }
      : { 'X-Beets-CSRF': '1' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

