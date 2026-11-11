import { ENGINE_OFFLINE_MESSAGE } from './api';

/** The thrown error's own text. Pass `fallback` for a non-Error throw that has
 * a better description than its string form. Use formatTransportErrorMessage
 * only where its fetch/timeout text rewriting is wanted. */
export function errorMessage(err: unknown, fallback?: string): string {
  if (err instanceof Error) return err.message;
  return fallback ?? String(err);
}

/** Joins sentences into one message, ending each with exactly one period.
 * Server text such as "Beets engine is unavailable." already ends in one, so
 * `${errorMessage(err)}. Next step` would print "..". */
export function sentences(...parts: string[]): string {
  return parts
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => (/[.!?]$/.test(part) ? part : `${part}.`))
    .join(' ');
}

/** True when the API layer classified a failure as the Beets engine being down
 * (lib/api ApiError category, or api/client's isEngineOffline flag). */
export function isEngineOffline(err: unknown): boolean {
  const e = err as { category?: string; isEngineOffline?: boolean } | null;
  return e?.category === 'engine_offline' || e?.isEngineOffline === true;
}

/** A panel's error text, or '' when it only repeats the page-level engine
 * banner. Panels keep every other error. */
export function panelError(message: string, engineOffline: boolean): string {
  return engineOffline && message === ENGINE_OFFLINE_MESSAGE ? '' : message;
}
