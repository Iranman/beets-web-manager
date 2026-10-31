// Library > Missing IDs: batch fix of proven matches (#399). The server proves
// each row and decides what may be written; this panel only selects rows, shows
// that decision, asks for confirmation and follows the jobs.
import { errorMessage } from '../../lib/errors';
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { apiErrorBody, applyMissingMbBatch, getJob, killJob, previewMissingMbBatch } from '../../api/client';
import type {
  MissingMbApplyResult,
  MissingMbApplyRow,
  MissingMbPreviewResult,
  MissingMbPreviewRow,
  MissingMbWrite,
  ReviewItem,
} from '../../api/types';

export const MISSING_IDS_BATCH_MAX = 50;
export const MISSING_IDS_BATCH_STORAGE_KEY = 'libraryMissingIds_batchJob';
export const MISSING_IDS_BATCH_POLL_MS = 2000;

type BatchJob = { kind: 'preview' | 'apply'; job_id: string };

function readStoredJob(): BatchJob | null {
  try {
    const parsed = JSON.parse(localStorage.getItem(MISSING_IDS_BATCH_STORAGE_KEY) || 'null') as BatchJob | null;
    return parsed && (parsed.kind === 'preview' || parsed.kind === 'apply') && parsed.job_id ? parsed : null;
  } catch {
    return null;
  }
}

function storeJob(job: BatchJob | null) {
  try {
    if (job) localStorage.setItem(MISSING_IDS_BATCH_STORAGE_KEY, JSON.stringify(job));
    else localStorage.removeItem(MISSING_IDS_BATCH_STORAGE_KEY);
  } catch {
    // storage blocked: the job is still followed while the page stays open
  }
}

const REASON_LABELS: Record<string, string> = {
  target_missing: 'no longer in the library',
  already_has_release_id: 'already has a Release ID',
  already_has_recording_id: 'already has a Recording ID',
  in_import_review: 'its folder is in the Import Review queue',
  no_items: 'no tracks found for this album',
  items_outside_album_folder: 'tracks are spread over more than one folder',
  fingerprint_unavailable: 'AcoustID gave no answer',
  no_single_confirmed_fingerprint: 'the fingerprint does not confirm exactly one recording',
  no_safe_candidate: 'no candidate is safe to attach without review',
  multiple_safe_candidates: 'more than one candidate looks safe',
  recording_id_not_resolved: 'the Recording ID could not be resolved',
  fingerprint_names_other_recording: 'the fingerprint names a different recording',
  not_a_singleton: 'the track belongs to an album',
  not_in_missing_list: 'not in the Missing IDs list any more',
  evidence_unavailable: 'evidence could not be gathered',
  not_proven: 'not proven',
  existing_release_group_differs: 'already has a different release group; not changed',
  duplicate_release_group: 'another album already has this release group; use dedup review',
  symlinked_item_path: "a track's file is a symlink",
  symlinked_audio_in_folder: 'the album folder holds symlinked audio',
  folder_files_mismatch: "the folder's audio files do not match the album's tracks",
  release_group_not_resolved: "MusicBrainz did not return a release group for this album's release",
  embedded_release_group_conflict: "A track's tags name a different release group",
  item_release_id_conflict: "A track's tags name a different release",
};

/** Preview chip: a Release Group fill keeps the album's Release ID, so it is not a full match. */
function previewChipLabel(row: MissingMbPreviewRow) {
  if (!row.proven) return 'Needs review';
  return row.evidence?.fill === 'release_group' ? 'Add release group (keeps release)' : 'Proven';
}

/** The persisted job record redacts the token; such a preview cannot be applied. */
const REDACTED_TOKEN = '[REDACTED]';
export const PREVIEW_INTERRUPTED_MESSAGE = 'Preview again: the preview was interrupted by a restart.';

/** Display text for a server reason code; codes like "x:1/2" keep their detail. */
export function reasonLabel(code: string) {
  const [key, detail] = code.split(/:(.*)/s, 2);
  const label = REASON_LABELS[key] || key.replace(/_/g, ' ');
  return detail ? `${label} (${detail})` : label;
}

function writeText(write: MissingMbWrite) {
  return Object.entries(write)
    .filter(([, value]) => value)
    .map(([key, value]) => `${key} ${value}`)
    .join(', ');
}

function kindCounts(rows: Array<{ kind: string }>) {
  const albums = rows.filter((row) => row.kind === 'album').length;
  return { albums, tracks: rows.length - albums };
}

function plural(count: number, word: string) {
  return `${count} ${word}${count === 1 ? '' : 's'}`;
}

/** Error text for a refused preview start (routes_library, #399). */
export function previewErrorMessage(err: unknown) {
  const body = apiErrorBody(err);
  const code = body?.error_code || body?.code || '';
  const detail = body?.error || (errorMessage(err));
  if (code === 'NO_ROWS') return 'Nothing was previewed: none of the selected rows are still missing IDs. Click Refresh to reload the list.';
  if (code === 'INVALID_PARAM') return `Nothing was previewed: the selection was refused (${detail}). Select 1 to ${MISSING_IDS_BATCH_MAX} rows and try again.`;
  if (code === 'ENGINE_UNAVAILABLE') return 'Nothing was previewed: Beets is unavailable. Check the Beets status on the System page, then try again.';
  if (code === 'ENGINE_AUTH_ERROR') return 'Nothing was previewed: Web Manager could not sign in to Beets. Check the Beets connection on the System page.';
  if (code === 'ENGINE_ERROR') return 'Nothing was previewed: Beets returned an error. Check the Beets status on the System page, then try again.';
  return `Nothing was previewed: ${detail}. Try again, or check the Jobs page.`;
}

/** Error text for a refused apply (start refusal or the job's own not_preview answer). */
export function applyErrorMessage(code: string, detail: string) {
  if (code === 'confirm_token_mismatch') {
    return 'Nothing was written: the confirmation does not match this preview (for example after a restart). Preview again, then apply the new preview.';
  }
  if (code === 'not_preview') return 'Nothing was written: this preview was already applied or used. Preview again to see the current state.';
  if (code === 'not_found' || code === 'wrong_transaction' || code === 'confirm_token_required') {
    return 'Nothing was written: this preview is not valid any more. Preview again.';
  }
  return `Nothing was written: ${detail || 'the apply was refused'}. Try again, or check the Jobs page.`;
}

const APPLY_STATUS_LABELS: Record<string, string> = {
  written: 'Written',
  already_set: 'Already set',
  evidence_changed: 'Evidence changed',
  no_longer_proven: 'No longer proven',
  evidence_unavailable: 'Evidence unavailable',
  write_failed: 'Write failed',
  recovery_required: 'Recovery required',
  cancelled: 'Cancelled',
  not_attempted: 'Not attempted',
};

function applyStatusColor(status: string): 'success' | 'warning' | 'error' | 'default' {
  if (status === 'written' || status === 'already_set') return 'success';
  if (status === 'write_failed' || status === 'recovery_required') return 'error';
  if (status === 'cancelled' || status === 'not_attempted') return 'default';
  return 'warning';
}

function lastLogLine(log?: string[]) {
  return [...(log ?? [])].reverse().find((line) => line.trim()) || '';
}

export function MissingIdsBatchPanel({ rows, onWritten }: { rows: ReviewItem[]; onWritten: () => void }) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [job, setJob] = useState<BatchJob | null>(() => readStoredJob());
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [preview, setPreview] = useState<Omit<MissingMbPreviewResult, 'confirm_token'> | null>(null);
  const [previewInterrupted, setPreviewInterrupted] = useState(false);
  const [applied, setApplied] = useState<MissingMbApplyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  // Held only to pass to apply: never rendered, stored or logged.
  const confirmTokenRef = useRef<string | null>(null);
  const onWrittenRef = useRef(onWritten);
  useEffect(() => { onWrittenRef.current = onWritten; }, [onWritten]);

  const follow = useCallback((next: BatchJob | null) => {
    storeJob(next);
    setJob(next);
    setProgress(null);
  }, []);

  // Follow the active job until it ends.
  useEffect(() => {
    if (!job) return undefined;
    let cancelled = false;
    let failures = 0;
    let inFlight = false;
    const finish = () => { if (!cancelled) follow(null); };
    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const status = await getJob(job.job_id);
        if (cancelled) return;
        failures = 0;
        const result = status.result as (MissingMbPreviewResult & MissingMbApplyResult) | undefined;
        if (status.status === 'running') {
          const p = status.state?.progress;
          if (p?.total) setProgress({ done: p.done ?? 0, total: p.total });
          return;
        }
        if (job.kind === 'preview') {
          if (status.status === 'success' && result && Array.isArray(result.rows)) {
            const { confirm_token: token, ...shown } = result;
            const usable = typeof token === 'string' && token !== '' && token !== REDACTED_TOKEN;
            confirmTokenRef.current = usable ? token : null;
            setPreview(shown);
            setPreviewInterrupted(Boolean(shown.transaction_id) && !usable);
            setMessage(
              `Preview finished: ${shown.counts.proven} of ${shown.counts.rows} row${shown.counts.rows === 1 ? '' : 's'} proven; `
              + `${shown.counts.needs_review} need review. Nothing has been written.`,
            );
          } else {
            setError(`The preview did not finish (${status.status}${lastLogLine(status.log) ? `: ${lastLogLine(status.log)}` : ''}). Nothing was written. Preview again, or check the Jobs page.`);
          }
        } else {
          if (result && Array.isArray(result.rows)) {
            setApplied(result);
            if (result.code) setError(applyErrorMessage(result.code, result.error || ''));
            const written = result.counts?.written ?? 0;
            setMessage(`Apply ${String(result.status || status.status).toLowerCase()}: ${plural(written, 'row')} written.`);
          } else {
            setError(`The apply job ended (${status.status}${lastLogLine(status.log) ? `: ${lastLogLine(status.log)}` : ''}) without a result. Some rows may have been written; check Library Changes before trying again.`);
          }
          confirmTokenRef.current = null;
          setPreview(null);
          onWrittenRef.current();
        }
        finish();
      } catch (err) {
        if (cancelled) return;
        failures += 1;
        if (failures >= 3) {
          setError(`Could not check the ${job.kind} job (${job.job_id}): ${errorMessage(err)}. It may still be running; check the Jobs page.`);
          finish();
        }
      } finally {
        inFlight = false;
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), MISSING_IDS_BATCH_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [follow, job]);

  const toggle = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else if (next.size < MISSING_IDS_BATCH_MAX) next.add(id);
      return next;
    });
  };

  const selectPage = () => {
    setSelected((current) => {
      const next = new Set(current);
      for (const row of rows) {
        if (next.size >= MISSING_IDS_BATCH_MAX) break;
        next.add(row.id);
      }
      return next;
    });
  };

  const startPreview = async () => {
    const ids = [...selected];
    if (!ids.length || job) return;
    setBusy(true);
    setError('');
    setPreview(null);
    setPreviewInterrupted(false);
    setApplied(null);
    confirmTokenRef.current = null;
    setLabels((current) => {
      const next = { ...current };
      for (const row of rows) if (selected.has(row.id)) next[row.id] = [row.artist, row.album || row.title].filter(Boolean).join(' – ');
      return next;
    });
    try {
      const started = await previewMissingMbBatch({ ids });
      setMessage(started.already_running
        ? `A preview was already running (${started.job_id}); nothing new was started. Showing that preview when it finishes.`
        : `Preview started for ${plural(ids.length, 'row')} (${started.job_id}). Nothing is written by a preview.`);
      follow({ kind: 'preview', job_id: started.job_id });
    } catch (err) {
      setError(previewErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const proven = preview?.rows.filter((row) => row.proven) ?? [];
  const provenKinds = kindCounts(proven);

  const startApply = async () => {
    setConfirmOpen(false);
    const token = confirmTokenRef.current;
    if (!preview?.transaction_id || !token || job) return;
    setBusy(true);
    setError('');
    try {
      const started = await applyMissingMbBatch(preview.transaction_id, token);
      setMessage(started.already_running
        ? `An apply was already running (${started.job_id}); nothing new was started. Following it.`
        : `Writing ${plural(proven.length, 'proven row')} (${started.job_id}). Each row is checked again before it is written.`);
      follow({ kind: 'apply', job_id: started.job_id });
    } catch (err) {
      const body = apiErrorBody(err);
      setError(applyErrorMessage(body?.code || body?.error_code || '', body?.error || (errorMessage(err))));
    } finally {
      setBusy(false);
    }
  };

  const cancelJob = async () => {
    if (!job) return;
    try {
      await killJob(job.job_id);
      setMessage(job.kind === 'apply'
        ? 'Cancel requested: the batch stops after the row it is writing now.'
        : 'Cancel requested: the preview stops; nothing was written.');
    } catch (err) {
      setError(`Could not cancel the ${job.kind} job: ${errorMessage(err)}. Check the Jobs page.`);
    }
  };

  const label = (rowId: string) => labels[rowId] || rowId;

  return (
    <section aria-labelledby="missing-ids-batch-title" className="rounded border border-graphite-700 bg-graphite-800/40 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="missing-ids-batch-title" className="text-sm font-semibold text-zinc-100">Fix proven matches in a batch</h2>
        <div className="flex flex-wrap gap-2">
          <Button size="small" variant="outlined" disabled={!rows.length || selected.size >= MISSING_IDS_BATCH_MAX} onClick={selectPage}>
            Select this page (max {MISSING_IDS_BATCH_MAX})
          </Button>
          <Button size="small" variant="text" disabled={!selected.size} onClick={() => setSelected(new Set())}>Clear selection</Button>
          <Button size="small" variant="contained" disabled={!selected.size || busy || Boolean(job)} onClick={() => void startPreview()}>
            Preview fix ({selected.size})
          </Button>
        </div>
      </div>
      <p className="mt-1 text-xs text-zinc-400">
        Select up to {MISSING_IDS_BATCH_MAX} rows. The preview proves each one by fingerprint and MusicBrainz evidence and writes nothing.
        Only proven rows can be written, and only after you confirm.
      </p>
      {rows.length ? (
        <fieldset className="mt-2 max-h-56 overflow-auto rounded border border-graphite-700 p-2">
          <legend className="sr-only">Rows on this page</legend>
          {rows.map((row) => (
            <label key={row.id} className="flex items-center gap-2 py-0.5 text-sm text-zinc-200">
              <input
                type="checkbox"
                checked={selected.has(row.id)}
                disabled={!selected.has(row.id) && selected.size >= MISSING_IDS_BATCH_MAX}
                onChange={() => toggle(row.id)}
              />
              <span className="truncate">{[row.artist, row.album || row.title].filter(Boolean).join(' – ') || row.id}</span>
              <span className="text-xs text-zinc-500">{row.target_kind === 'item' ? 'track' : 'album'}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      <div className="mt-1 text-xs text-zinc-500">{selected.size} of {MISSING_IDS_BATCH_MAX} selected</div>

      <div role="status" aria-live="polite" className="mt-2 text-xs text-zinc-300">{message}</div>
      {error ? <Alert className="mt-2" severity="error">{error}</Alert> : null}

      {job ? (
        <div className="mt-2 flex items-center gap-2">
          <LinearProgress
            className="flex-1"
            aria-label={job.kind === 'preview' ? 'Missing IDs preview progress' : 'Missing IDs apply progress'}
            variant={progress ? 'determinate' : 'indeterminate'}
            value={progress ? (100 * progress.done) / progress.total : undefined}
          />
          <span className="text-xs text-zinc-400">{progress ? `${progress.done}/${progress.total}` : 'Starting…'}</span>
          <Button size="small" variant="outlined" color="warning" onClick={() => void cancelJob()}>Cancel</Button>
        </div>
      ) : null}

      {preview ? (
        <div className="mt-3">
          <div className="text-xs text-zinc-300">
            {preview.counts.proven} proven · {preview.counts.needs_review} need review
          </div>
          <PreviewRows rows={preview.rows} label={label} />
          {preview.transaction_id && previewInterrupted ? (
            <Alert className="mt-2" severity="warning">{PREVIEW_INTERRUPTED_MESSAGE}</Alert>
          ) : preview.transaction_id ? (
            <Button className="mt-2" variant="contained" color="warning" disabled={busy || Boolean(job)} onClick={() => setConfirmOpen(true)}>
              Apply {plural(proven.length, 'proven row')}…
            </Button>
          ) : (
            <div className="mt-2 text-xs text-zinc-400">No row was proven, so there is nothing to apply. Fix these rows one at a time below.</div>
          )}
        </div>
      ) : null}

      {applied ? <ApplyRows result={applied} label={label} /> : null}

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} className="relative z-50">
        <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <DialogPanel className="w-full max-w-lg rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
            <DialogTitle className="text-base font-semibold text-zinc-100">
              Write MusicBrainz IDs to {plural(provenKinds.albums, 'album')} and {plural(provenKinds.tracks, 'track')}?
            </DialogTitle>
            <p className="mt-2 text-sm leading-6 text-zinc-400">
              This changes your library. An album with no Release ID gets its Release Group and Release ID, then its tags
              are written and its files may be renamed or moved by your Beets path settings. An album that already has a
              Release ID keeps it and gets only its Release Group, written to its tags; its files are not moved. Each track
              gets its Recording ID. Every row is
              checked again first; a row whose evidence changed is skipped. The first failed write stops the batch. Rows
              that need review are not touched.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="outlined" onClick={() => setConfirmOpen(false)}>Cancel</Button>
              <Button variant="contained" color="warning" onClick={() => void startApply()}>
                Write {plural(proven.length, 'row')}
              </Button>
            </div>
          </DialogPanel>
        </div>
      </Dialog>
    </section>
  );
}

function PreviewRows({ rows, label }: { rows: MissingMbPreviewRow[]; label: (id: string) => string }) {
  return (
    <ul className="mt-2 space-y-1" aria-label="Preview results">
      {rows.map((row) => (
        <li key={row.row_id} className="rounded border border-graphite-700 px-2 py-1 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Chip size="small" color={row.proven ? 'success' : 'warning'} variant="outlined" label={previewChipLabel(row)} />
            <span className="truncate text-zinc-200">{label(row.row_id)}</span>
          </div>
          <div className="text-xs text-zinc-400">{row.message}</div>
          {row.proven ? (
            <div className="font-mono text-[0.7rem] text-zinc-300">Will write: {writeText(row.write)}</div>
          ) : row.reasons.length ? (
            <div className="text-xs text-zinc-400">Why: {row.reasons.map(reasonLabel).join('; ')}</div>
          ) : null}
          {row.evidence?.release_group_held_by?.length ? (
            <div className="text-xs text-amber-300">
              This release group is already on album {row.evidence.release_group_held_by.join(', ')}.{' '}
              <Link className="text-sky-400 underline" to="/jobs">Review duplicates in Duplicate Checks on the Jobs page</Link>
            </div>
          ) : null}
          {row.evidence?.duplicate_in_batch ? (
            <div className="text-xs text-amber-300">Two selected albums share this release group; neither is written. Review them by hand.</div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ApplyRows({ result, label }: { result: MissingMbApplyResult; label: (id: string) => string }) {
  const recovery = result.status === 'Recovery Required';
  return (
    <div className="mt-3">
      <div className="text-xs font-semibold text-zinc-200">Apply result: {result.status}</div>
      {recovery ? (
        <Alert className="mt-2" severity="error">
          A write could not be verified or rolled back, so the batch stopped. Check the affected album or track before retrying.{' '}
          <Link to={`/changes?tx=${encodeURIComponent(result.transaction_id)}`}>Open the batch transaction</Link>
        </Alert>
      ) : null}
      <ul className="mt-2 space-y-1" aria-label="Apply results">
        {result.rows.map((row: MissingMbApplyRow) => (
          <li key={row.row_id} className="rounded border border-graphite-700 px-2 py-1 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Chip size="small" color={applyStatusColor(row.status)} variant="outlined" label={APPLY_STATUS_LABELS[row.status] || row.status} />
              <span className="truncate text-zinc-200">{label(row.row_id)}</span>
            </div>
            <div className="text-xs text-zinc-400">{row.message}</div>
            {row.reasons?.length ? <div className="text-xs text-zinc-400">Why: {row.reasons.map(reasonLabel).join('; ')}</div> : null}
            {(row.transaction_ids ?? []).filter(Boolean).map((id) => (
              <Link key={id} className="mr-2 text-xs text-sky-400 underline" to={`/changes?tx=${encodeURIComponent(id)}`}>
                Transaction {id}
              </Link>
            ))}
          </li>
        ))}
      </ul>
    </div>
  );
}
