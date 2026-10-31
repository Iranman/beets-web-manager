import { errorMessage } from '../../lib/errors';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Paper from '@mui/material/Paper';
import TextField from '@mui/material/TextField';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableContainer from '@mui/material/TableContainer';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import Typography from '@mui/material/Typography';
import React, { useCallback, useEffect, useState } from 'react';
import {
  getRecoverRuns,
  importRecoverRun,
  recoverProbe,
  retryRecoverRun,
  startRecoverFetch,
} from '../../api/client';
import type {
  RecoverAuthorization,
  RecoverAuthorizationBasis,
  RecoverProbeResponse,
  RecoverRun,
  RecoverStreamDiagnostics,
} from '../../api/types';
import { CleanSection } from '../../components/CleanPanel';

type Severity = 'success' | 'info' | 'warning' | 'error';

export type RecoverStateLabel =
  | 'Available'
  | 'Authentication required'
  | 'Unsupported format'
  | 'DRM-protected'
  | 'Provider unavailable'
  | 'Download failed'
  | 'Recovered'
  | 'Verified'
  | 'Imported';

interface RecoverState {
  label: RecoverStateLabel;
  severity: Severity;
  /** What failed / what to do next, user-facing. */
  hint: string;
}

// Backend provenance_status / category -> user-facing state. The backend owns
// the decision; this only renames it. DRM-protected is reachable ONLY from the
// explicit `genuinely-DRM` status -- never inferred from a generic failure.
const STATE_MAP: Record<string, RecoverState> = {
  accessible: { label: 'Available', severity: 'success', hint: 'This upload is accessible. Use Fetch to download and stage it.' },
  'auth-needed': { label: 'Authentication required', severity: 'warning', hint: 'The provider needs an authenticated session for this upload. Load owner cookies, then probe again.' },
  'access-restricted': { label: 'Authentication required', severity: 'warning', hint: 'The provider restricted access to this upload. Load cookies for the account that owns it, then probe again.' },
  'unsupported-format': { label: 'Unsupported format', severity: 'warning', hint: 'No downloadable audio format is available for this upload, so it cannot be recovered.' },
  format: { label: 'Unsupported format', severity: 'warning', hint: 'The available formats could not be assembled into audio, so this upload cannot be recovered.' },
  'genuinely-DRM': { label: 'DRM-protected', severity: 'error', hint: 'This upload is DRM-protected and cannot be recovered.' },
  unavailable: { label: 'Provider unavailable', severity: 'error', hint: 'The provider could not serve this upload (removed, private, or unavailable). Check the URL or try again later.' },
  'expired-url': { label: 'Provider unavailable', severity: 'error', hint: 'This link has expired. Open the upload again to get a fresh URL, then probe it.' },
  recovered: { label: 'Recovered', severity: 'success', hint: 'Downloaded and staged. Import it to add it to your library.' },
  verified: { label: 'Verified', severity: 'success', hint: 'Verified against AcoustID / MusicBrainz and ready to import.' },
  imported: { label: 'Imported', severity: 'success', hint: 'Imported into your library.' },
  'rate-limited': { label: 'Download failed', severity: 'warning', hint: 'The provider rate-limited the request. Wait a few minutes and retry.' },
  'hls-assembly': { label: 'Download failed', severity: 'warning', hint: 'The audio stream could not be assembled. Retry; if it persists the upload may be unavailable.' },
  misconfig: { label: 'Download failed', severity: 'error', hint: 'Recovery is misconfigured (e.g. yt-dlp or the downloads path). Check setup, then retry.' },
  unknown: { label: 'Download failed', severity: 'error', hint: 'The provider returned an error. Retry; if it persists, check the URL.' },
};

const FALLBACK_STATE: RecoverState = {
  label: 'Download failed',
  severity: 'error',
  hint: 'The provider returned an error. Retry; if it persists, check the URL.',
};

/**
 * Maps the backend `provenance_status` (with `category` as a fallback) to the
 * user-facing recovery state. Any unknown value falls through to
 * "Download failed" -- it never silently becomes "Available" or "DRM-protected".
 */
export function mapRecoverState(provenanceStatus?: string, category?: string): RecoverState {
  const prov = (provenanceStatus ?? '').trim();
  if (prov && STATE_MAP[prov]) return STATE_MAP[prov];
  const cat = (category ?? '').trim();
  if (cat && STATE_MAP[cat]) return STATE_MAP[cat];
  return FALLBACK_STATE;
}

// User-facing labels for the authorization basis the backend requires. The
// backend owns validation; these only name the four accepted bases.
export const BASIS_LABELS: Record<RecoverAuthorizationBasis, string> = {
  original_creation: 'I created this recording',
  own_account: 'Uploaded to an account I control',
  licensed_permission: 'Licensed/permitted archival copy',
  open_license: 'Public-domain or openly licensed',
};

const BASIS_ORDER: RecoverAuthorizationBasis[] = [
  'original_creation',
  'own_account',
  'licensed_permission',
  'open_license',
];

function basisLabel(basis?: string): string {
  if (!basis) return 'Unspecified';
  return BASIS_LABELS[basis as RecoverAuthorizationBasis] ?? basis;
}

function runStatusSeverity(status?: string): Severity {
  const s = (status ?? '').toLowerCase();
  if (s.includes('fail') || s.includes('error')) return 'error';
  if (s.includes('complete') || s.includes('import') || s.includes('recovered') || s.includes('success')) return 'success';
  if (s.includes('running') || s.includes('pending') || s.includes('queued')) return 'info';
  return 'warning';
}

/** Read-only diagnostic rows, rendered from an explicit allowlist of fields.
 * Nothing named key/kid/license/cookie/token is ever read or shown; key_systems
 * is a DRM *scheme* name (e.g. "widevine"), not a content key. */
function diagnosticRows(diag: RecoverStreamDiagnostics, platform?: string): Array<{ label: string; value: string }> {
  const best = diag.best_audio_format ?? undefined;
  const rows: Array<{ label: string; value: string }> = [];
  if (platform) rows.push({ label: 'Platform', value: platform });
  if (diag.manifest_types?.length) rows.push({ label: 'Manifest type', value: diag.manifest_types.join(', ') });
  if (diag.protocols?.length) rows.push({ label: 'Stream protocol', value: diag.protocols.join(', ') });
  if (best?.ext) rows.push({ label: 'Container', value: best.ext });
  const codecs = [best?.acodec, best?.vcodec].filter((c) => c && c !== 'none').join(' / ');
  if (codecs) rows.push({ label: 'Codecs', value: codecs });
  if (diag.drm_status) rows.push({ label: 'DRM status', value: diag.drm_status });
  if (diag.key_systems?.length) rows.push({ label: 'Key systems', value: diag.key_systems.join(', ') });
  rows.push({ label: 'EME detected', value: diag.eme_detected ? 'yes' : 'no' });
  return rows;
}

/** Renders a run's recorded authorization: the asserted-basis label, the
 * optional user note, and a Verified-vs-Asserted badge driven ONLY by the
 * backend's `authorization.verified`. It never fabricates a verified state, and
 * it states that an asserted (unverified) claim is routed to review. */
export const AuthorizationCell: React.FC<{ authorization?: RecoverAuthorization }> = ({ authorization }) => {
  if (!authorization || (authorization.asserted_basis == null && authorization.verified == null)) {
    return <Typography variant="caption" color="text.secondary">—</Typography>;
  }
  const verified = authorization.verified === true;
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, alignItems: 'flex-start' }}>
      <Chip
        size="small"
        color={verified ? 'success' : 'warning'}
        variant={verified ? 'filled' : 'outlined'}
        label={verified ? 'Verified' : 'Asserted (pending review)'}
      />
      <Typography variant="caption">{basisLabel(authorization.asserted_basis)}</Typography>
      {authorization.asserted_note && (
        <Typography variant="caption" color="text.secondary">&ldquo;{authorization.asserted_note}&rdquo;</Typography>
      )}
      {!verified && (
        <Typography variant="caption" color="text.secondary">
          Your claim — routed to review, not auto-imported.
        </Typography>
      )}
    </Box>
  );
};

/**
 * Recover My Uploads. A user re-downloads their OWN YouTube/SoundCloud uploads
 * through the guarded yt-dlp path. The backend owns every decision (what is
 * accessible, DRM-protected, recovered, imported); this panel only probes,
 * lists runs, and confirms the mutating Import.
 */
export const RecoverUploadsSection: React.FC = () => {
  const [url, setUrl] = useState('');
  // Required authorization declaration shared by Fetch and Import. '' = none chosen.
  const [basis, setBasis] = useState<RecoverAuthorizationBasis | ''>('');
  const [note, setNote] = useState('');
  const [probe, setProbe] = useState<RecoverProbeResponse | null>(null);
  const [runs, setRuns] = useState<RecoverRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ severity: Severity; text: string } | null>(null);
  // run_id awaiting a second click to confirm the mutating import; null = none.
  const [confirmImportId, setConfirmImportId] = useState<string | null>(null);

  const loadRuns = useCallback(async () => {
    try {
      const res = await getRecoverRuns();
      setRuns(res.runs ?? []);
    } catch (err) {
      setRuns([]);
      setMessage({ severity: 'info', text: errorMessage(err, 'Could not load recovery runs.') });
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
    } catch (err) {
      setMessage({ severity: 'error', text: errorMessage(err, 'Request failed') });
    } finally {
      setBusy(false);
    }
  };

  // Both Fetch and Import require an authorization basis. Block with a clear
  // inline message until one is chosen; the backend re-validates it regardless.
  const requireAuthorization = (): { basis: RecoverAuthorizationBasis; note?: string } | null => {
    if (!basis) {
      setMessage({
        severity: 'warning',
        text: 'Choose how you are authorized to recover this upload before continuing. An asserted basis is reviewed, not independently confirmed.',
      });
      return null;
    }
    const trimmedNote = note.trim();
    return { basis, note: trimmedNote || undefined };
  };

  const onProbe = () =>
    run(async () => {
      const trimmed = url.trim();
      if (!trimmed) {
        setMessage({ severity: 'warning', text: 'Paste the URL of an upload you own first.' });
        return;
      }
      const res = await recoverProbe(trimmed);
      setProbe(res);
      if (!res.ok && res.error) {
        setMessage({ severity: 'error', text: res.error });
      }
    });

  const onFetch = () =>
    run(async () => {
      const trimmed = url.trim();
      if (!trimmed) {
        setMessage({ severity: 'warning', text: 'Paste a URL to fetch first.' });
        return;
      }
      const authorization = requireAuthorization();
      if (!authorization) return;
      const res = await startRecoverFetch([trimmed], authorization);
      setMessage({ severity: 'success', text: `Fetch started (run ${res.run_id ?? ''}). It downloads and stages the upload.` });
      await loadRuns();
    });

  const onRetry = (runId: string) =>
    run(async () => {
      const res = await retryRecoverRun(runId);
      setMessage({ severity: 'success', text: `Retry started (run ${res.run_id ?? res.job_id ?? ''}).` });
      await loadRuns();
    });

  const onImport = (runId: string) =>
    run(async () => {
      const authorization = requireAuthorization();
      if (!authorization) return;
      setConfirmImportId(null);
      const res = await importRecoverRun(runId, authorization);
      setMessage({
        severity: res.ok ? 'success' : 'error',
        text: res.ok ? `Import started for run ${runId}.` : (res.error || 'Import failed.'),
      });
      await loadRuns();
    });

  const probeState = probe ? mapRecoverState(probe.provenance_status, probe.category) : null;
  const diag = probe?.stream_diagnostics;

  return (
    <CleanSection
      title="Recover my uploads"
      description="Re-download your own YouTube or SoundCloud uploads. Beets decides what is recoverable; nothing is imported until you confirm."
    >
      <Box sx={{ mb: 2, display: 'flex', gap: 1.5, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <TextField
          size="small"
          label="Upload URL"
          placeholder="https://…"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={busy}
          sx={{ flex: '1 1 320px', minWidth: 0 }}
        />
        <Button variant="contained" onClick={onProbe} disabled={busy}>Probe</Button>
        <Button variant="outlined" onClick={onFetch} disabled={busy}>Fetch</Button>
      </Box>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ mb: 0.5 }}>Authorization declaration</Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
          Required for Fetch and Import. This records your claim — it is not an
          independent verification. An asserted basis routes the item to review,
          not auto-import.
        </Typography>
        <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <TextField
            select
            required
            size="small"
            label="Basis for recovery"
            value={basis}
            onChange={(e) => setBasis(e.target.value as RecoverAuthorizationBasis | '')}
            disabled={busy}
            error={!basis}
            helperText={basis ? ' ' : 'Choose a basis to enable Fetch and Import.'}
            slotProps={{ select: { native: true }, inputLabel: { shrink: true } }}
            sx={{ flex: '1 1 280px', minWidth: 0 }}
          >
            <option value="">Select a basis…</option>
            {BASIS_ORDER.map((b) => (
              <option key={b} value={b}>{BASIS_LABELS[b]}</option>
            ))}
          </TextField>
          <TextField
            size="small"
            label="Note (optional)"
            placeholder="e.g. my own band's channel"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            disabled={busy}
            sx={{ flex: '1 1 280px', minWidth: 0 }}
          />
        </Box>
      </Paper>

      {message && <Alert severity={message.severity} sx={{ mb: 2 }}>{message.text}</Alert>}

      {probe && probeState && (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap', mb: 1 }}>
            <Chip size="small" color={probeState.severity} label={probeState.label} />
            {probe.is_collection && (
              <Chip size="small" variant="outlined" label={`Collection: ${probe.entry_count ?? 0} item(s)`} />
            )}
            {probe.title && <Typography variant="subtitle2">{probe.title}</Typography>}
            {probe.uploader && <Typography variant="body2" color="text.secondary">by {probe.uploader}</Typography>}
          </Box>
          <Alert severity={probeState.severity} sx={{ mb: diag || probe.authorization ? 2 : 0 }}>{probeState.hint}</Alert>

          {probe.authorization && (
            <Box sx={{ mb: diag ? 2 : 0 }}>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>Authorization</Typography>
              <AuthorizationCell authorization={probe.authorization} />
            </Box>
          )}

          {diag && (
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>Stream diagnostics</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                Diagnostics only — no content keys, credentials, or cookies are shown.
              </Typography>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                {diagnosticRows(diag, probe.platform).map((row) => (
                  <Chip key={row.label} size="small" variant="outlined" label={`${row.label}: ${row.value}`} />
                ))}
              </Box>
            </Box>
          )}
        </Paper>
      )}

      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        {runs.length} recovery run(s).
      </Typography>
      <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 420 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell>Run</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>Results</TableCell>
              <TableCell>Authorization</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {runs.length === 0 && (
              <TableRow>
                <TableCell colSpan={5}>
                  <Typography variant="body2" color="text.secondary">No recovery runs yet. Probe a URL, then Fetch to start one.</Typography>
                </TableCell>
              </TableRow>
            )}
            {runs.map((r) => (
              <TableRow key={r.run_id}>
                <TableCell sx={{ fontFamily: 'monospace', fontSize: '0.75rem' }}>{r.run_id.slice(0, 12)}</TableCell>
                <TableCell>
                  <Chip size="small" color={runStatusSeverity(r.status)} label={r.status || 'unknown'} />
                </TableCell>
                <TableCell sx={{ fontSize: '0.8rem' }}>
                  {`recovered ${r.recovered_count ?? 0} · imported ${r.imported_count ?? 0} · failed ${r.failed_count ?? 0}`}
                </TableCell>
                <TableCell>
                  <AuthorizationCell authorization={r.authorization} />
                </TableCell>
                <TableCell align="right">
                  {confirmImportId === r.run_id ? (
                    <Box sx={{ display: 'inline-flex', gap: 1 }}>
                      <Button size="small" color="warning" variant="contained" onClick={() => onImport(r.run_id)} disabled={busy}>Confirm import</Button>
                      <Button size="small" onClick={() => setConfirmImportId(null)} disabled={busy}>Cancel</Button>
                    </Box>
                  ) : (
                    <Box sx={{ display: 'inline-flex', gap: 1 }}>
                      <Button size="small" onClick={() => onRetry(r.run_id)} disabled={busy}>Retry</Button>
                      <Button size="small" onClick={() => setConfirmImportId(r.run_id)} disabled={busy}>Import</Button>
                    </Box>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </CleanSection>
  );
};
