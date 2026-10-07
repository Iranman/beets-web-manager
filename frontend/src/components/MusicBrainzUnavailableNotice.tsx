import Alert from '@mui/material/Alert';

export const MUSICBRAINZ_UNAVAILABLE_TEXT = 'MusicBrainz is unavailable; showing local/fingerprint results only.';

/** Non-blocking notice for responses that set `musicbrainz_unavailable`.
 * role="status" (polite) rather than MUI's default role="alert". */
export function MusicBrainzUnavailableNotice({ show }: { show?: boolean }) {
  if (!show) return null;
  return (
    <Alert role="status" severity="info" sx={{ mt: 2 }}>
      {MUSICBRAINZ_UNAVAILABLE_TEXT}
    </Alert>
  );
}
