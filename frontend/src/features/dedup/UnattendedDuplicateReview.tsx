import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useState } from 'react';
import { getUnattendedCleanup, runDuplicateMaintenance, setUnattendedCleanup } from '../../api/client';
import type { DuplicateProposalCopy, DuplicateProposalRow, UnattendedCleanupStatus } from '../../api/types';

const ENABLE_CONFIRMATION = 'ENABLE UNATTENDED DUPLICATE DELETION';

function formatSize(size: number | null): string {
  if (size == null) return 'missing';
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.round(size / 1024)} KB`;
}

function CopyCell({ copy, role }: { copy: DuplicateProposalCopy; role: 'delete' | 'keep' }) {
  return (
    <div className="min-w-0">
      <div className={role === 'delete' ? 'text-red-300' : 'text-emerald-300'}>
        {role === 'delete' ? 'Would delete' : 'Keeps'}
      </div>
      <div className="break-all font-mono text-xs text-zinc-200">{copy.path}</div>
      <div className="text-xs text-zinc-400">
        {formatSize(copy.size)} · item {copy.item_id ?? '—'} · album {copy.album_id ?? '—'} · disc {copy.disc ?? '—'} track {copy.track ?? '—'}
      </div>
      <div className="break-all font-mono text-xs text-zinc-500">embedded Recording ID: {copy.recording_id || '—'}</div>
    </div>
  );
}

function ProposalRow({ row }: { row: DuplicateProposalRow }) {
  const fp = row.fingerprint;
  return (
    <div className="grid gap-3 rounded border border-zinc-800 p-3 md:grid-cols-2">
      <CopyCell copy={row.delete} role="delete" />
      <CopyCell copy={row.keep} role="keep" />
      <div className="text-xs text-zinc-400 md:col-span-2">
        <Chip size="small" label={row.release_relation || 'unknown slot'} sx={{ mr: 1 }} />
        <Chip
          size="small"
          color={fp.verified ? 'success' : 'default'}
          label={fp.verified ? 'Fingerprint-verified' : 'No fingerprint proof'}
          sx={{ mr: 1 }}
        />
        {row.embedded_id_contradicts_fingerprint && (
          <Chip size="small" color="warning" label="Embedded ID contradicts fingerprint" sx={{ mr: 1 }} />
        )}
        <span className="font-mono">
          AcoustID shared recording: {fp.shared_recording_id || '—'} · delete copy hears {fp.delete_copy_recording_ids.join(', ') || '—'} · kept copy hears {fp.keep_copy_recording_ids.join(', ') || '—'}
        </span>
      </div>
    </div>
  );
}

export function UnattendedDuplicateReview({ onMusicRoot }: { onMusicRoot?: (root: string) => void }) {
  const [status, setStatus] = useState<UnattendedCleanupStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    try {
      const s = await getUnattendedCleanup();
      setStatus(s);
      if (s.music_root) onMusicRoot?.(s.music_root);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [onMusicRoot]);

  useEffect(() => { void load(); }, [load]);

  const enabled = Boolean(status?.authorization?.unattended_delete_enabled);
  const proposal = Array.isArray(status?.proposal) ? status.proposal : [];

  const run = async () => {
    setBusy(true);
    setMessage('');
    try {
      const r = await runDuplicateMaintenance();
      setMessage(`Duplicate maintenance started (job ${r.job_id.slice(0, 8)}). ${enabled ? 'Unattended deletion is ENABLED.' : 'Deletion is disabled: this only builds the proposal.'}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (next: boolean) => {
    setBusy(true);
    setError('');
    try {
      setStatus(await setUnattendedCleanup(next, next ? confirmText : '', next ? 'Enabled after reviewing the proposal' : ''));
      setConfirmText('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mb-4 space-y-3 rounded border border-zinc-800 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-zinc-100">Scheduled duplicate cleanup</h3>
        <Chip size="small" color={enabled ? 'error' : 'default'} label={enabled ? 'Unattended deletion ENABLED' : 'Unattended deletion disabled'} />
        <span className="text-xs text-zinc-500">library root {status?.music_root ?? '…'}</span>
        <div className="ml-auto flex gap-2">
          <Button size="small" variant="outlined" disabled={busy} onClick={() => void run()}>Run duplicate check</Button>
          <Button size="small" variant="text" disabled={busy} onClick={() => void load()}>Refresh</Button>
        </div>
      </div>
      {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}
      {message && <Alert severity="info" onClose={() => setMessage('')}>{message}</Alert>}
      <div className="text-xs text-zinc-400">
        {proposal.length
          ? `Last run proposes ${proposal.length} audio-proven deletion(s); one copy of every group is kept. Anything without fingerprint or byte proof stays for review.`
          : 'No proposal recorded yet. Run the duplicate check to build one.'}
      </div>
      {proposal.map((row) => <ProposalRow key={row.delete.path} row={row} />)}
      {enabled ? (
        <Button size="small" color="inherit" variant="outlined" disabled={busy} onClick={() => void toggle(false)}>
          Disable unattended deletion
        </Button>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <TextField
            size="small"
            label={`Type "${ENABLE_CONFIRMATION}" to enable`}
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            sx={{ minWidth: 380 }}
          />
          <Button
            size="small"
            color="error"
            variant="outlined"
            disabled={busy || confirmText !== ENABLE_CONFIRMATION}
            onClick={() => void toggle(true)}
          >
            Enable unattended deletion
          </Button>
        </div>
      )}
    </section>
  );
}
