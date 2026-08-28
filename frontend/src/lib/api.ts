export type ApiErrorCategory = 'network' | 'engine_offline' | 'auth' | 'timeout' | 'http_error';

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

async function apiFetch<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const method = (opts.method ?? 'GET').toUpperCase();
  let headers = opts.headers;
  if (!CSRF_EXEMPT_METHODS.has(method)) {
    const merged = new Headers(headers);
    if (!merged.has('X-Beets-CSRF')) merged.set('X-Beets-CSRF', '1');
    headers = merged;
  }

  let res: Response;
  try {
    res = await fetch(url, { ...opts, headers });
  } catch (err: unknown) {
    const name = (err as Error)?.name || '';
    if (name === 'AbortError' || name === 'TimeoutError') {
      throw new ApiError(0, 'Request timed out.', 'timeout');
    }
    throw new ApiError(0, 'Could not reach Beets Web Manager.', 'network');
  }

  if (!res.ok) {
    let serverMessage = `HTTP ${res.status}`;
    let errorCode = '';
    try {
      const body = (await res.json()) as { error?: string; error_code?: string };
      if (typeof body?.error === 'string' && body.error) {
        serverMessage = body.error;
      }
      if (typeof body?.error_code === 'string') {
        errorCode = body.error_code;
      }
    } catch {
      // ignore JSON parse failure
    }

    if (res.status === 401 || res.status === 403 || errorCode === 'AUTH_FAILED') {
      throw new ApiError(res.status, 'Your session expired. Sign in again.', 'auth');
    }
    if (errorCode === 'ENGINE_OFFLINE' || (res.status === 503 && !errorCode)) {
      throw new ApiError(res.status, 'Beets engine is unavailable.', 'engine_offline');
    }
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

