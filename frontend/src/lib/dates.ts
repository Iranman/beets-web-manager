/** Backend timestamps are epoch seconds, a few older ones milliseconds. */
export function normaliseEpochSeconds(ts?: number | string | null) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value > 100000000000 ? value / 1000 : value;
}

/** "Oct 30, 4:05 PM" in the viewer's locale. Takes epoch milliseconds. */
export function formatShortDateTime(epochMs: number): string {
  return new Date(epochMs).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

/** "4:05 PM" in the viewer's locale. Takes epoch seconds. */
export function formatShortTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/** Full locale date and time. Takes epoch seconds. */
export function formatEpochDateTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString();
}

/** A client-side scan time (epoch milliseconds), or null before the first scan. */
export function formatScanTime(timestamp: number | null): string {
  return timestamp ? formatShortDateTime(timestamp) : 'Not scanned this session';
}
