import Alert from '@mui/material/Alert';

export const MUSICBRAINZ_UNAVAILABLE_TEXT = 'MusicBrainz is unavailable; showing local/fingerprint results only.';

/** Why AcoustID did not check fingerprints (#252 NF-4). A "not checked" is
 * never a "no match": the audio was simply not compared. */
const ACOUSTID_NOT_CHECKED: Record<string, string> = {
  not_configured: 'AcoustID is not configured, so fingerprints were not checked. Set ACOUSTID_API_KEY in Web Manager settings to compare the audio.',
  auth_failed: 'AcoustID rejected the API key, so fingerprints were not checked. Check ACOUSTID_API_KEY in Web Manager settings.',
  lookup_failed: 'AcoustID could not be reached (outage, rate limit, or fpcalc missing or timed out), so fingerprints were not checked. Try again later.',
  unavailable: 'AcoustID was unavailable, so fingerprints were not checked. Try again later.',
};

/** Notice text when AcoustID did not run; '' when it did (or the status is
 * a real answer such as no_result). */
export function acoustidNotCheckedText(status?: string | null): string {
  const key = (status ?? '').toLowerCase();
  if (!key) return '';
  return ACOUSTID_NOT_CHECKED[key] ?? '';
}

/** Short badge for a per-track fingerprint status; null when there is no
 * "not checked" to report. */
export function fingerprintNotCheckedLabel(status?: string | null, reason?: string | null): string | null {
  const key = (status ?? '').toLowerCase();
  if (key !== 'unavailable' && !ACOUSTID_NOT_CHECKED[key]) return null;
  const why = reason || (key === 'unavailable' ? '' : key.replace(/_/g, ' '));
  return `Not checked${why ? ` (${why})` : ''}`;
}

/** Non-blocking notices for responses that set `musicbrainz_unavailable`
 * or a failing `acoustid_status`. role="status" (polite) rather than MUI's
 * default role="alert". */
export function MusicBrainzUnavailableNotice({ show, acoustidStatus }: { show?: boolean; acoustidStatus?: string | null }) {
  const acoustidText = acoustidNotCheckedText(acoustidStatus);
  if (!show && !acoustidText) return null;
  return (
    <>
      {show ? (
        <Alert role="status" severity="info" sx={{ mt: 2 }}>
          {MUSICBRAINZ_UNAVAILABLE_TEXT}
        </Alert>
      ) : null}
      {acoustidText ? (
        <Alert role="status" severity="warning" sx={{ mt: 2 }}>
          {acoustidText}
        </Alert>
      ) : null}
    </>
  );
}
