/** The thrown error's own text. Pass `fallback` for a non-Error throw that has
 * a better description than its string form. Use formatTransportErrorMessage
 * only where its fetch/timeout text rewriting is wanted. */
export function errorMessage(err: unknown, fallback?: string): string {
  if (err instanceof Error) return err.message;
  return fallback ?? String(err);
}
