export class ApiError extends Error {
  httpStatus: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.httpStatus = status;
  }
}

async function apiFetch<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string };
      if (typeof body?.error === 'string') message = body.error;
    } catch {
      // ignore — keep the HTTP status message
    }
    throw new ApiError(res.status, message);
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
