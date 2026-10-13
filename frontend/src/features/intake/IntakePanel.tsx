import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { fetchAlbumArt, getAiBatchStatus, pauseAiBatch, reconcileArtwork, recoverAiBatch, runPreflight, skipAiBatch, startAiBatchImport, stopAiBatch } from '../../api/client';
import type { AiBatchFolderState, AiBatchState, PreflightFolder, PreflightResponse } from '../../api/types';
import { LogViewer } from '../../components/LogViewer';
import { downloadsRootProblem, useDownloadsRoot, useJobPoll } from '../../lib/hooks';

const AI_BATCH_JOB_STORAGE_KEY = 'beets:ai-batch-import-job-id';

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(' ');
}

function folderTone(folder: PreflightFolder) {
  if (folder.already_in_library) return { color: 'success' as const, label: 'In library' };
  if (folder.path.includes('/failed_imports/')) return { color: 'error' as const, label: 'Failed import' };
  return { color: 'warning' as const, label: 'New' };
}

function FolderRow({ folder }: { folder: PreflightFolder }) {
  const tone = folderTone(folder);
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-t border-graphite-800 px-3 py-2.5 text-sm sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center">
      <div className="min-w-0">
        <div className="truncate font-medium text-zinc-200">{folder.name}</div>
        <div className="truncate font-mono text-[0.68rem] text-zinc-500">{folder.path}</div>
      </div>
      <span className="shrink-0 tabular-nums text-xs text-zinc-500">
        {folder.audio_files} file{folder.audio_files !== 1 ? 's' : ''}
      </span>
      <Chip color={tone.color} label={tone.label} size="small" variant="outlined" />
    </div>
  );
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: 'slate' | 'amber' | 'emerald' | 'sky' | 'red';
}) {
  const toneClass = {
    slate: 'text-zinc-200',
    amber: 'text-amber-300',
    emerald: 'text-emerald-300',
    sky: 'text-sky-300',
    red: 'text-red-300',
  }[tone];

  return (
    <div className="rounded border border-graphite-800 bg-graphite-950/70 px-3 py-3">
      <div className={cx('text-xl font-semibold tabular-nums', toneClass)}>{value.toLocaleString()}</div>
      <div className="mt-1 text-[0.7rem] uppercase tracking-wide text-zinc-500">{label}</div>
    </div>
  );
}

function PreflightSummary({ result }: { result: PreflightResponse }) {
  const newFolders = Math.max(0, result.audio_folders - result.already_in_library_folders);
  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4 xl:grid-cols-6">
      <StatCard label="Audio files" value={result.audio_files} tone="slate" />
      <StatCard label="New folders" value={newFolders} tone={newFolders > 0 ? 'amber' : 'slate'} />
      <StatCard label="In library" value={result.already_in_library_folders} tone="emerald" />
      <StatCard label="Pending review" value={result.pending_review} tone={result.pending_review > 0 ? 'sky' : 'slate'} />
      <StatCard label="Unsupported" value={result.unsupported_files} tone={result.unsupported_files > 0 ? 'red' : 'slate'} />
      <StatCard label="Empty dirs" value={result.empty_dirs} tone="slate" />
    </div>
  );
}

function ConfirmStartDialog({
  open,
  path,
  folderCount,
  fileCount,
  onClose,
  onConfirm,
  busy,
}: {
  open: boolean;
  path: string;
  folderCount: number;
  fileCount: number;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  return (
    <Dialog open={open} onClose={busy ? () => undefined : onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-lg rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">Run Import All?</DialogTitle>
          <p className="mt-2 text-sm leading-6 text-zinc-400">
            The backend will process the selected import source, import eligible MusicBrainz matches, and leave unsafe
            folders in Import Review. This can write tags and move imported files according to the Beets config.
          </p>
          <div className="mt-4 rounded border border-graphite-800 bg-graphite-950 p-3">
            <div className="truncate font-mono text-xs text-zinc-300">{path}</div>
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-zinc-500">
              <span>{folderCount} new folders</span>
              <span>{fileCount} audio files</span>
            </div>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button disabled={busy} variant="outlined" onClick={onClose}>
              Cancel
            </Button>
            <Button disabled={busy} variant="contained" onClick={onConfirm}>
              {busy ? 'Starting...' : 'Import eligible only'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

const AI_BATCH_TERMINAL_STATUSES = new Set(['completed', 'completed_with_warnings', 'failed', 'canceled', 'cancelled']);
const AI_BATCH_ACTIVE_FOLDER_STATUSES = new Set(['claimed', 'scanning', 'ai_running', 'importing', 'retrying']);
const AI_BATCH_ATTENTION_FOLDER_STATUSES = new Set([
  'review_created',
  'review_required',
  'policy_rejected',
  'replacement_unavailable',
  'import_failed',
  'ai_failed',
  'failed',
  'timed_out',
]);

function isAiBatchTerminal(status?: string) {
  return Boolean(status && AI_BATCH_TERMINAL_STATUSES.has(status));
}

export function folderStatusLabel(status?: string) {
  const labels: Record<string, string> = {
    queued: 'Queued',
    claimed: 'Starting',
    scanning: 'Scanning',
    ai_queued: 'Waiting for match',
    ai_running: 'Finding match',
    ai_completed: 'Match found',
    importing: 'Importing',
    retrying: 'Retrying',
    failed: 'Failed',
    canceled: 'Canceled',
    imported: 'Imported',
    completed: 'Completed',
    review_created: 'Review created',
    review_required: 'Review required',
    policy_rejected: 'Policy rejected',
    replacement_queued: 'Replacement queued',
    replacement_unavailable: 'Replacement unavailable',
    completed_with_fallback: 'Completed with fallback',
    handled_warning: 'Handled warning',
    import_failed: 'Import failed',
    ai_failed: 'AI failed',
    timed_out: 'Timed out',
    skipped: 'Skipped',
  };
  return labels[String(status || '')] || String(status || 'Unknown');
}

const STAGE_LABELS: Record<string, string> = {
  beets_import: 'Beets import',
  identify: 'Identifying with MusicBrainz',
  pinned_import: 'Importing with the chosen release',
  done: 'Done',
};

const BEETS_SKIP_LABELS: Record<string, string> = {
  duplicate: 'Beets skipped it: already in the library',
  blank_identity: 'Beets skipped it: the files have no usable tags',
  no_strong_match: 'Beets skipped it: no strong MusicBrainz match',
  no_candidates: 'Beets skipped it: no MusicBrainz candidates',
};

const FOLDER_WARNING_LABELS: Record<string, string> = {
  imported_without_fingerprint: 'Imported without fingerprint proof',
};

/** Label for a folder warning code; unknown codes are shown as-is. */
export function folderWarningLabel(code: string) {
  return FOLDER_WARNING_LABELS[code] || code.replace(/_/g, ' ');
}

/** Chip label for a folder that needs attention: names the specific outcome
 * where the status alone would be vague (backend/ai_service.py, #394). */
export function folderAttentionLabel(folder: AiBatchFolderState) {
  if (folder.failure_reason === 'symlinked_audio') return 'Symlinked audio';
  // A partial Beets import: some albums were kept, the skipped paths have a review row.
  if (folder.status === 'review_required' && folder.metadata_imported && folder.review_item_id) return 'Imported in part';
  return folderStatusLabel(folder.status);
}

/** Readable text for failure_reason codes; sentences from the server pass through. */
export function folderFailureText(reason?: string) {
  if (reason === 'symlinked_audio') {
    return 'Not imported: some audio files are symlinks, which could point anywhere (even into the library). Replace them with the real files, then decide in the Review Queue.';
  }
  return reason || '';
}

export function folderStageLabel(stage?: string) {
  return stage ? STAGE_LABELS[stage] || stage.replace(/_/g, ' ') : '';
}

export function beetsSkipLabel(reason?: string) {
  return reason ? BEETS_SKIP_LABELS[reason] || `Beets skipped it: ${reason.replace(/_/g, ' ')}` : '';
}

// Display only: the backend decides what a retry restarts and reports the count
// as folders_retryable. This lists the folders so the confirmation can name them.
const AI_BATCH_RETRYABLE_STATUSES = new Set(['failed', 'import_failed', 'ai_failed', 'timed_out', 'replacement_unavailable']);

export function retryableFolders(state: AiBatchState | null | undefined) {
  return (state?.folders ?? []).filter((folder) => AI_BATCH_RETRYABLE_STATUSES.has(folder.status) && !folder.retry_exhausted);
}

/** Batch-level evidence warnings: AcoustID fingerprinting is off, so imports
 * rely on tags and MusicBrainz metadata only. */
export function batchEvidenceWarnings(state: AiBatchState | null | undefined) {
  const caps = state?.evidence_capabilities;
  const messages = [...(state?.warnings ?? [])];
  if (caps && (!caps.beets_fingerprinting || !caps.acoustid_key) && !messages.length) {
    messages.push(
      !caps.beets_fingerprinting
        ? 'Beets fingerprinting (chroma plugin) is not available, so albums are imported without fingerprint proof.'
        : 'No AcoustID API key is configured, so albums are imported without fingerprint proof.',
    );
  }
  return messages;
}

export type RetryOutcome = 'started' | 'already_running' | 'nothing_to_retry';

/** What a recover-with-retry call actually did. `reconnected` means nothing
 * new was started: either a worker for the batch is already running, or the
 * batch had nothing left to retry (routes_import.ai_batch_import_recover). */
export function retryOutcome(result: { reconnected?: boolean; state?: AiBatchState | null }): RetryOutcome {
  if (!result.reconnected) return 'started';
  const state = result.state;
  if (state && isAiBatchTerminal(state.status) && !(state.folders_retryable ?? 0)) return 'nothing_to_retry';
  return 'already_running';
}

export function retryOutcomeMessage(outcome: RetryOutcome, requested: number): string {
  if (outcome === 'started') {
    return `Retry started for ${requested} failed folder${requested !== 1 ? 's' : ''}. Progress shows below.`;
  }
  if (outcome === 'already_running') {
    return 'Nothing new was started: an import for this batch is already running. Its progress shows below.';
  }
  return 'Nothing was retried: the batch has no failed folders left that can be retried. Folders at their retry limit need manual review in the Review Queue.';
}

function RetryFailuresDialog({
  open,
  state,
  loading,
  error,
  busy,
  onClose,
  onConfirm,
}: {
  open: boolean;
  state: AiBatchState | null;
  loading: boolean;
  error: string;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const folders = retryableFolders(state);
  const count = state?.folders_retryable ?? folders.length;
  const shown = folders.slice(0, 10);
  return (
    <Dialog open={open} onClose={busy ? () => undefined : onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-lg rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">Retry failed imports from this batch?</DialogTitle>
          {loading ? (
            <div className="mt-3"><LinearProgress aria-label="Loading the last batch import" /></div>
          ) : error ? (
            <Alert className="mt-3" severity="error">
              Could not load the last batch import: {error}. Nothing was retried. Refresh and try again.
            </Alert>
          ) : !state ? (
            <p className="mt-2 text-sm text-zinc-400">No batch import has run yet, so there is nothing to retry.</p>
          ) : count === 0 ? (
            <p className="mt-2 text-sm text-zinc-400">
              The last batch import ({state.source_path || state.batch_job_id}) has no failed folders that can be retried.
              Folders that reached their retry limit need manual review in the Review Queue.
            </p>
          ) : (
            <>
              <p className="mt-2 text-sm leading-6 text-zinc-400">
                This retries {count} failed folder{count !== 1 ? 's' : ''} from the last batch import. Imports can write
                tags and move files into the library. Completed folders are not redone.
              </p>
              <div className="mt-3 rounded border border-graphite-800 bg-graphite-950 p-3">
                <div className="truncate font-mono text-xs text-zinc-300">{state.source_path || state.batch_job_id}</div>
                <ul className="mt-2 space-y-1 text-xs text-zinc-400">
                  {shown.map((folder) => (
                    <li key={folder.folder_id} className="truncate">
                      {folderName(folder.source_folder)} <span className="text-zinc-600">({folderStatusLabel(folder.status)})</span>
                    </li>
                  ))}
                </ul>
                {count > shown.length ? <div className="mt-1 text-xs text-zinc-600">and {count - shown.length} more</div> : null}
              </div>
            </>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <Button disabled={busy} variant="outlined" onClick={onClose}>
              Cancel
            </Button>
            <Button disabled={busy || loading || Boolean(error) || !state || count === 0} variant="contained" onClick={onConfirm}>
              {busy ? 'Starting...' : `Retry ${count} folder${count !== 1 ? 's' : ''}`}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function artworkStatusChipLabel(status?: string) {
  const labels: Record<string, string> = {
    cancelled: 'Artwork retry cancelled',
    timed_out: 'Artwork retry timed out',
    failed: 'Artwork failed',
  };
  return labels[String(status || '')] || 'Artwork failed';
}

function folderName(path?: string) {
  if (!path) return '';
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

function formatAgeSeconds(age?: number | null) {
  if (age == null || !Number.isFinite(age)) return 'unknown';
  if (age < 2) return 'just now';
  if (age < 60) return `${Math.round(age)}s ago`;
  if (age < 3600) return `${Math.round(age / 60)}m ago`;
  return `${Math.round(age / 3600)}h ago`;
}

function ageFromTimestamp(timestamp?: number | null) {
  if (!timestamp) return null;
  return Math.max(0, Date.now() / 1000 - timestamp);
}

function BatchMetric({ label, value, tone = 'text-zinc-100' }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded border border-graphite-800 bg-graphite-950/60 px-3 py-2">
      <div className={cx('text-lg font-semibold tabular-nums', tone)}>{value}</div>
      <div className="mt-0.5 text-[0.68rem] uppercase tracking-wide text-zinc-500">{label}</div>
    </div>
  );
}

function JobLog({
  jobId,
  onDone,
  onJobIdChange,
}: {
  jobId: string;
  onDone: () => void;
  onJobIdChange: (jobId: string | null) => void;
}) {
  const navigate = useNavigate();
  const [batchState, setBatchState] = useState<AiBatchState | null>(null);
  const [statusLoaded, setStatusLoaded] = useState(false);
  const [statusError, setStatusError] = useState('');
  const batchStatus = batchState?.status;
  const pollJobLog = statusLoaded && (!batchStatus || (!isAiBatchTerminal(batchStatus) && batchStatus !== 'stale'));
  const { job, error } = useJobPoll(jobId, pollJobLog);
  const [skipNotice, setSkipNotice] = useState('');
  const [actionError, setActionError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [stopping, setStopping] = useState(false);

  const loadStatus = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const result = await getAiBatchStatus(jobId);
      setBatchState(result.state);
      setStatusError('');
      if (result.state.job_id && result.state.job_id !== jobId) {
        onJobIdChange(result.state.job_id);
      }
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : String(err));
    } finally {
      setStatusLoaded(true);
      if (manual) setRefreshing(false);
    }
  }, [jobId, onJobIdChange]);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      if (!active) return;
      await loadStatus(false);
    };
    void poll();
    const intervalId = window.setInterval(() => {
      if (!document.hidden) void poll();
    }, 2500);
    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, [loadStatus]);

  const log = job?.log ?? [];
  const batchDone = isAiBatchTerminal(batchState?.status);
  // A missing batch state must never render as "Running" forever: once the
  // status poll has settled at least once and came back empty, treat it as
  // a terminal-ish "not found" state instead of falling through to the
  // default Running label below.
  const missing = statusLoaded && !batchState && Boolean(statusError);
  const jobEndedWithoutState = Boolean(!batchState && job && job.status !== 'running');
  const done = batchDone || missing || jobEndedWithoutState;
  const stale = batchState?.status === 'stale';
  const staleWorker = stale && batchState?.worker_alive === false;
  const paused = batchState?.status === 'paused' || batchState?.status === 'pausing';
  const total = batchState?.total_folders_found ?? 0;
  const processed = batchState?.folders_processed ?? 0;
  const progressValue = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
  const heartbeatAge = batchState?.heartbeat_age_seconds ?? ageFromTimestamp(batchState?.heartbeat_at);
  const lastUpdateAge = ageFromTimestamp(batchState?.updated_at);
  const activeFolders = (batchState?.folders ?? []).filter((folder) => AI_BATCH_ACTIVE_FOLDER_STATUSES.has(folder.status));
  const attentionFolders = (batchState?.folders ?? [])
    .filter((folder) => (
      AI_BATCH_ATTENTION_FOLDER_STATUSES.has(folder.status)
      || folder.artwork_retryable
      // imported_without_fingerprint is summarised once in the batch warning.
      || (folder.warnings ?? []).some((code) => code !== 'imported_without_fingerprint')
    ))
    .slice(0, 8);
  const importedCount = batchState?.folders_completed ?? 0;
  const reviewCount = batchState?.folders_review ?? 0;
  const warningCount = (batchState?.folders_warning ?? 0) + (batchState?.folders_replacement_unavailable ?? 0);
  const replacementQueuedCount = batchState?.folders_replacement_queued ?? 0;
  const failedCount = batchState?.folders_failed ?? 0;
  const skippedCount = batchState?.folders_skipped ?? 0;
  const retryableCount = batchState?.folders_retryable ?? 0;
  const attentionCount = batchState?.folders_attention ?? (reviewCount + warningCount + failedCount);
  const runningMetricLabel = staleWorker ? 'Stale active' : 'AI running';
  const runningMetricValue = staleWorker ? activeFolders.length : batchState?.folders_running ?? 0;
  const runningMetricTone = staleWorker ? 'text-amber-200' : 'text-red-200';
  const lastUpdateText = staleWorker ? 'stale' : formatAgeSeconds(lastUpdateAge ?? heartbeatAge);
  const heartbeatText = staleWorker ? 'stale' : formatAgeSeconds(heartbeatAge);
  const lastCompleted = batchState?.last_completed_folder ? folderName(batchState.last_completed_folder) : '';
  const lastFailed = batchState?.last_failed_folder ? folderName(batchState.last_failed_folder) : '';
  const statusText = stale
    ? 'Batch appears stuck'
    : missing
      ? 'Not found'
      : paused
        ? 'Paused'
        : done
          ? (batchState?.status === 'completed_with_warnings' ? 'Completed with warnings' : batchState?.status || job?.status || 'Complete')
          : 'Running';

  const skipCurrent = async (folderId?: string) => {
    setActionError('');
    setSkipNotice('');
    try {
      await skipAiBatch(jobId, folderId);
      setSkipNotice(folderId ? 'Folder skip requested.' : 'Skip requested for active folder(s).');
      await loadStatus(true);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  // Retries only the artwork stage for an already-imported/verified album --
  // reuses the same Album Art Repair job the manual retry button uses, so a
  // FetchArt failure never requires reimporting or retagging the album. Job
  // creation alone never proves success: this polls the job to a terminal
  // state, then calls the backend reconciliation endpoint, which re-verifies
  // the album's actual on-disk art before the folder is ever cleared from
  // "Needs attention" -- never trusts the job's own claimed outcome.
  const [retryingArtworkAlbumId, setRetryingArtworkAlbumId] = useState<number | null>(null);
  const [artworkRetryJobId, setArtworkRetryJobId] = useState<string | null>(null);
  const [artworkRetryFolderId, setArtworkRetryFolderId] = useState<string | null>(null);
  const { job: artworkRetryJob } = useJobPoll(artworkRetryJobId, Boolean(artworkRetryJobId));

  useEffect(() => {
    if (!artworkRetryJobId || !artworkRetryFolderId) return;
    if (!artworkRetryJob || artworkRetryJob.status === 'running') return;
    let active = true;
    (async () => {
      try {
        const result = await reconcileArtwork(jobId, artworkRetryFolderId, artworkRetryJobId);
        if (active) setBatchState(result.state);
      } catch (err) {
        if (active) setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        if (active) {
          setArtworkRetryJobId(null);
          setArtworkRetryFolderId(null);
          setRetryingArtworkAlbumId(null);
        }
      }
    })();
    return () => { active = false; };
  }, [artworkRetryJob, artworkRetryJobId, artworkRetryFolderId, jobId]);

  const retryArtwork = async (albumId: number, folderId: string) => {
    if (retryingArtworkAlbumId) return; // one retry in flight at a time
    setActionError('');
    setRetryingArtworkAlbumId(albumId);
    try {
      const started = await fetchAlbumArt(albumId);
      if (!started.job_id) throw new Error('Backend did not return a job id');
      setArtworkRetryJobId(started.job_id);
      setArtworkRetryFolderId(folderId);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setRetryingArtworkAlbumId(null);
    }
  };

  const pauseBatch = async () => {
    setPausing(true);
    setActionError('');
    try {
      const result = await pauseAiBatch(jobId);
      setBatchState(result.state);
      setSkipNotice('Pause requested. Active folder work can finish or time out.');
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setPausing(false);
    }
  };

  const [retryDialogOpen, setRetryDialogOpen] = useState(false);

  const recoverBatch = async (retryFailed = false) => {
    // Retry is confirmed in RetryFailuresDialog, which lists the folders.
    if (!retryFailed && !window.confirm('Recover this batch? This resumes imports for unfinished folders. Imports can write tags and move files into the library; completed folders are not redone.')) return;
    setRecovering(true);
    setActionError('');
    try {
      const result = await recoverAiBatch(jobId, retryFailed);
      const nextJobId = result.state?.job_id || result.job_id;
      onJobIdChange(nextJobId);
      if (result.state) setBatchState(result.state);
      setSkipNotice(
        retryFailed
          ? retryOutcomeMessage(retryOutcome(result), batchState?.folders_retryable ?? retryableFolders(batchState).length)
          : 'Batch reconciled. Completed folders will not be redone.',
      );
      setRetryDialogOpen(false);
    } catch (err) {
      setActionError(`Could not start the retry: ${err instanceof Error ? err.message : String(err)}. Nothing was retried.`);
      setRetryDialogOpen(false);
    } finally {
      setRecovering(false);
    }
  };
  const evidenceWarnings = batchEvidenceWarnings(batchState);
  const unprovenImports = (batchState?.folders ?? []).filter((folder) => folder.warnings?.includes('imported_without_fingerprint')).length;

  const stopJob = async () => {
    if (!window.confirm('Stop the running AI batch import job?')) return;
    setStopping(true);
    setActionError('');
    try {
      const result = await stopAiBatch(jobId);
      setBatchState(result.state);
      setSkipNotice('Stop requested. The batch was marked canceled safely.');
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setStopping(false);
    }
  };

  return (
    <Card>
      <CardContent className="space-y-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-zinc-100">Batch Import</h3>
              <Chip
                label={statusText}
                size="small"
                color={stale ? 'warning' : missing ? 'error' : done ? 'success' : paused ? 'info' : 'primary'}
                variant="outlined"
              />
            </div>
            <div className="mt-1 truncate font-mono text-xs text-zinc-500">{batchState?.batch_job_id || jobId}</div>
            {batchState?.current_step ? <div className="mt-1 text-xs text-zinc-400">{batchState.current_step}</div> : null}
          </div>
          <div className="flex flex-wrap gap-2">
            {!done ? (
              <>
                <Button disabled={pausing || paused || stale} size="small" variant="outlined" onClick={() => void pauseBatch()}>
                  {pausing ? 'Pausing...' : 'Pause'}
                </Button>
                <Button disabled={activeFolders.length === 0} size="small" variant="outlined" onClick={() => void skipCurrent()}>
                  Skip current
                </Button>
                <Button color="error" disabled={stopping} size="small" variant="outlined" onClick={() => void stopJob()}>
                  {stopping ? 'Stopping...' : 'Stop'}
                </Button>
              </>
            ) : (
              <>
                {attentionCount ? (
                  <Button size="small" variant="contained" onClick={() => navigate('/import?tab=review')}>
                    Review {attentionCount} issue{attentionCount !== 1 ? 's' : ''}
                  </Button>
                ) : null}
                {retryableCount ? (
                  <Button disabled={recovering} size="small" variant="outlined" onClick={() => setRetryDialogOpen(true)}>
                    {recovering ? 'Retrying...' : 'Retry eligible failures'}
                  </Button>
                ) : null}
                <Button size="small" variant="outlined" onClick={() => navigate('/jobs')}>
                  View report
                </Button>
                <Button size="small" variant="outlined" onClick={onDone}>
                  Dismiss
                </Button>
              </>
            )}
            {(stale || paused) ? (
              <Button disabled={recovering} size="small" variant="contained" onClick={() => void recoverBatch()}>
                {recovering ? 'Recovering...' : 'Recover batch'}
              </Button>
            ) : null}
            <Button disabled={refreshing} size="small" variant="outlined" onClick={() => void loadStatus(true)}>
              {refreshing ? 'Refreshing...' : 'Refresh status'}
            </Button>
            {!done ? (
              <Button size="small" variant="outlined" onClick={() => navigate('/jobs')}>
                Open Jobs
              </Button>
            ) : null}
          </div>
        </div>

        {stale ? (
          <Alert severity="warning">
            {staleWorker
              ? 'Worker is not alive. Recover will resume unfinished folders; Stop cancels the batch.'
              : 'Batch appears stuck. Refresh, recover, or stop the batch.'}
          </Alert>
        ) : null}

        {missing ? (
          <Alert severity="error">
            Batch job no longer exists for this ID. Use Re-scan to start a fresh import, or check Jobs for its history.
          </Alert>
        ) : null}

        {evidenceWarnings.length ? (
          <Alert severity="warning">
            {evidenceWarnings.map((message) => <div key={message}>{message}</div>)}
            {unprovenImports ? (
              <div className="mt-1">
                {unprovenImports} folder{unprovenImports !== 1 ? 's were' : ' was'} imported without fingerprint proof.
              </div>
            ) : null}
            {unprovenImports || batchState?.evidence_capabilities?.beets_fingerprinting === false || batchState?.evidence_capabilities?.acoustid_key === false ? (
              <div className="mt-1 text-xs">
                To add fingerprint proof, enable the Beets chroma plugin and set an AcoustID API key (see System).
              </div>
            ) : null}
          </Alert>
        ) : null}

        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4 xl:grid-cols-7">
          <BatchMetric label="Processed" value={total > 0 ? `${processed} / ${total}` : processed} tone="text-sky-200" />
          {done ? (
            <>
              <BatchMetric label="Imported" value={importedCount} tone="text-emerald-200" />
              <BatchMetric label="Review" value={reviewCount} tone="text-amber-200" />
              <BatchMetric label="Warnings" value={warningCount} tone="text-orange-200" />
              <BatchMetric label="Replacements" value={replacementQueuedCount} tone="text-violet-200" />
              <BatchMetric label="Failed" value={failedCount} tone="text-rose-200" />
              <BatchMetric label="Skipped" value={skippedCount} tone="text-zinc-300" />
            </>
          ) : (
            <>
              <BatchMetric label={runningMetricLabel} value={runningMetricValue} tone={runningMetricTone} />
              <BatchMetric label="Queued" value={batchState?.folders_queued ?? 0} tone="text-zinc-200" />
              <BatchMetric label="Review" value={reviewCount} tone="text-amber-200" />
              <BatchMetric label="Failed" value={failedCount} tone="text-rose-200" />
              <BatchMetric label="Last update" value={lastUpdateText} tone={staleWorker ? 'text-amber-200' : 'text-emerald-200'} />
            </>
          )}
        </div>

        <LinearProgress
          sx={{ borderRadius: 1 }}
          variant={total > 0 ? 'determinate' : 'indeterminate'}
          value={done && total > 0 ? 100 : progressValue}
        />

        {done ? (
          <div className="rounded border border-graphite-800 bg-graphite-950/50 p-3">
            <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="text-sm font-semibold text-zinc-100">
                  {statusText}{total ? ` — ${processed} folder${processed !== 1 ? 's' : ''} processed` : ''}
                </div>
                <div className="mt-1 text-xs text-zinc-500">
                  {batchState?.batch_summary || (attentionCount ? `${attentionCount} issue${attentionCount !== 1 ? 's' : ''} need attention.` : 'No remaining batch issues.')}
                </div>
              </div>
              <div className="text-xs text-zinc-500">Last update {lastUpdateText}</div>
            </div>
            {attentionFolders.length ? (
              <div className="mt-3 space-y-2">
                <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Needs attention</div>
                {attentionFolders.map((folder: AiBatchFolderState) => (
                  <div key={folder.folder_id} className="grid gap-2 rounded border border-graphite-800 px-2 py-2 text-sm sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                    <div className="min-w-0">
                      <div className="truncate text-zinc-200">{folderName(folder.source_folder)}</div>
                      {folder.retry_exhausted ? (
                        <div className="truncate text-xs text-amber-400">
                          Retry limit reached ({folder.retry_count ?? folder.max_retries ?? '?'}/{folder.max_retries ?? '?'}) — manual review required
                        </div>
                      ) : null}
                      <div className="truncate text-xs text-zinc-500">
                        {folder.artwork_retryable
                          ? 'Imported and tagged successfully; album artwork could not be fetched.'
                          : folderFailureText(folder.failure_reason) || beetsSkipLabel(folder.beets_skip_reason) || folder.ai_suggest_error || folder.current_step || 'Needs review'}
                      </div>
                      {folder.auto_import && !folder.auto_import.eligible && folder.auto_import.reasons.length ? (
                        <div className="truncate text-xs text-zinc-500">
                          Not auto-imported: {folder.auto_import.reasons.join('; ')}
                        </div>
                      ) : null}
                      {folder.warnings?.length ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {folder.warnings.map((code) => (
                            <Chip key={code} label={folderWarningLabel(code)} size="small" color="warning" variant="outlined" />
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      {folder.review_item_id || folder.status === 'review_created' || folder.status === 'review_required' ? (
                        // Review rows are keyed by folder path, so a path search lands on the row review_item_id names.
                        <Button
                          size="small"
                          variant="outlined"
                          onClick={() => navigate(`/import?tab=review&q=${encodeURIComponent(folder.source_folder)}`)}
                        >
                          {folder.review_item_id ? 'Open review item' : 'Open in review'}
                        </Button>
                      ) : null}
                      {folder.retry_exhausted || folder.manual_review_required ? (
                        <Chip label="Manual review" size="small" color="warning" variant="outlined" />
                      ) : null}
                      {folder.artwork_retryable ? (
                        <>
                          <Chip label={artworkStatusChipLabel(folder.artwork_status)} size="small" color="warning" variant="outlined" />
                          {folder.album_id ? (
                            <Button
                              size="small"
                              variant="outlined"
                              disabled={retryingArtworkAlbumId !== null}
                              onClick={() => void retryArtwork(folder.album_id!, folder.folder_id)}
                            >
                              {retryingArtworkAlbumId === folder.album_id ? 'Retrying…' : 'Retry artwork'}
                            </Button>
                          ) : null}
                        </>
                      ) : null}
                      <Chip label={folderAttentionLabel(folder)} size="small" variant="outlined" />
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            <div className="rounded border border-graphite-800 bg-graphite-950/50 p-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                {staleWorker ? 'Stale folder claims' : 'Current folders'}
              </div>
              {activeFolders.length ? (
                <div className="mt-2 space-y-2">
                  {activeFolders.map((folder: AiBatchFolderState) => (
                    <div key={folder.folder_id} className="flex min-w-0 items-center justify-between gap-2 rounded border border-graphite-800 px-2 py-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm text-zinc-200">{folderName(folder.source_folder)}</div>
                        <div className="truncate font-mono text-[0.7rem] text-zinc-500">
                          {staleWorker
                            ? 'worker stopped before this folder finished'
                            : [folderStatusLabel(folder.status), folderStageLabel(folder.stage), folder.current_step].filter(Boolean).join(' · ') || folder.ai_suggest_status}
                        </div>
                      </div>
                      <Button size="small" variant="outlined" onClick={() => void skipCurrent(folder.folder_id)}>
                        Skip
                      </Button>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2 text-sm text-zinc-500">
                  {staleWorker ? 'No stale active folder claims.' : 'No active folder claim right now.'}
                </div>
              )}
            </div>
            <div className="rounded border border-graphite-800 bg-graphite-950/50 p-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Recent folder state</div>
              <div className="mt-2 space-y-2 text-sm">
                <div className="min-w-0">
                  <div className="text-zinc-500">Last completed</div>
                  <div className="truncate text-zinc-200">{lastCompleted || 'None yet'}</div>
                </div>
                <div className="min-w-0">
                  <div className="text-zinc-500">Last failed</div>
                  <div className="truncate text-zinc-200">{lastFailed || 'None yet'}</div>
                  {batchState?.last_failed_reason ? <div className="mt-0.5 text-xs text-rose-300">{batchState.last_failed_reason}</div> : null}
                </div>
                <div className="text-xs text-zinc-500">Heartbeat {heartbeatText}</div>
              </div>
            </div>
          </div>
        )}
        {skipNotice ? <Alert severity="info" onClose={() => setSkipNotice('')}>{skipNotice}</Alert> : null}
        {actionError ? <Alert severity="error">{actionError}</Alert> : null}
        {statusError ? <Alert severity="warning">Status polling: {statusError}</Alert> : null}
        {error && !batchState ? <Alert severity="error">{error}</Alert> : null}

        {done ? (
          <details className="rounded border border-graphite-800 bg-graphite-950/40 p-3">
            <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-zinc-500">Activity log</summary>
            <LogViewer
              className="mt-3 max-h-[18rem]"
              emptyText="(no activity recorded)"
              lines={log.length ? log : batchState?.current_step ? [`[status] ${batchState.current_step}`] : []}
            />
          </details>
        ) : (
          <LogViewer
            className="max-h-[28rem]"
            emptyText="(waiting for output...)"
            lines={log.length ? log : batchState?.current_step ? [`[status] ${batchState.current_step}`] : []}
          />
        )}
        <RetryFailuresDialog
          busy={recovering}
          error=""
          loading={false}
          open={retryDialogOpen}
          state={batchState}
          onClose={() => setRetryDialogOpen(false)}
          onConfirm={() => void recoverBatch(true)}
        />
      </CardContent>
    </Card>
  );
}
type IntakePanelProps = {
  onJobStarted?: () => void | Promise<void>;
};

export function IntakePanel({ onJobStarted }: IntakePanelProps = {}) {
  const downloads = useDownloadsRoot();
  const downloadsRoot = downloads.root;
  const downloadsProblem = downloadsRootProblem(downloads);
  const [path, setPath] = useState('');
  // Prefill with the configured root once it loads, unless the user (or a
  // resumed batch) already chose a path.
  useEffect(() => {
    if (downloadsRoot) setPath((current) => current || downloadsRoot);
  }, [downloadsRoot]);
  const failedImportsPath = downloadsRoot ? `${downloadsRoot.replace(/\/+$/, '')}/failed_imports` : '';
  const [scanning, setScanning] = useState(false);
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [preflightError, setPreflightError] = useState('');
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState('');
  const [retryingFailed, setRetryingFailed] = useState(false);
  const [retryNotice, setRetryNotice] = useState('');
  const [retryError, setRetryError] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [jobId, setJobId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(AI_BATCH_JOB_STORAGE_KEY);
    } catch {
      return null;
    }
  });

  const rememberJobId = useCallback((nextJobId: string | null) => {
    setJobId(nextJobId);
    try {
      if (nextJobId) localStorage.setItem(AI_BATCH_JOB_STORAGE_KEY, nextJobId);
      else localStorage.removeItem(AI_BATCH_JOB_STORAGE_KEY);
    } catch {
      // localStorage can be unavailable in hardened browser contexts.
    }
  }, []);

  useEffect(() => {
    if (jobId) return;
    let active = true;
    getAiBatchStatus()
      .then((result) => {
        if (!active || isAiBatchTerminal(result.state.status)) return;
        const nextJobId = result.state.job_id || result.state.batch_job_id;
        if (nextJobId) rememberJobId(nextJobId);
        if (result.state.source_path) setPath(result.state.source_path);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [jobId, rememberJobId]);
  const scan = useCallback(async () => {
    const nextPath = path.trim() || downloadsRoot;
    if (!nextPath) return;
    setScanning(true);
    setPreflightError('');
    setImportError('');
    setPreflight(null);
    try {
      const result = await runPreflight(nextPath);
      setPreflight(result);
      setPath(result.path || nextPath);
    } catch (err) {
      setPreflightError(err instanceof Error ? err.message : String(err));
    } finally {
      setScanning(false);
    }
  }, [path, downloadsRoot]);

  // A cold scan of a large downloads folder can genuinely take 40-60+
  // seconds (confirmed live: 2217 folders / 14461 files took ~41s server-side)
  // with nothing else in the UI changing for the whole wait -- easy to read
  // as hung rather than working. This just keeps a visible, ticking sign of
  // life instead of a static "Previewing..." label.
  const [scanElapsedSeconds, setScanElapsedSeconds] = useState(0);
  useEffect(() => {
    if (!scanning) {
      setScanElapsedSeconds(0);
      return undefined;
    }
    const startedAt = Date.now();
    setScanElapsedSeconds(0);
    const intervalId = window.setInterval(() => {
      setScanElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [scanning]);

  // preflight.folders is capped by the backend to the first 100 entries for
  // the preview table ("showing first 100" note below) -- it is NOT the
  // full set. newFolders (this filtered array) must only be used to render
  // that capped preview list. The real total eligible for Import All is
  // audio_folders - already_in_library_folders, matching what
  // PreflightSummary already displays as "New folders". Using
  // newFolders.length anywhere that claims to describe the actual scope of
  // the Import All operation (the confirm dialog, the summary text) was a
  // real bug: a library with 2217 new folders showed "100 new folders" in
  // the confirm dialog immediately before the user committed to the import,
  // silently misrepresenting the scale of a real, mutating operation by
  // more than 20x (confirmed live 2026-07-20).
  const newFolders = useMemo(
    () => preflight?.folders.filter((folder) => !folder.already_in_library) ?? [],
    [preflight],
  );

  const newFolderCount = useMemo(
    () => (preflight ? Math.max(0, preflight.audio_folders - preflight.already_in_library_folders) : 0),
    [preflight],
  );

  const failedImportFolders = useMemo(
    () => newFolders.filter((folder) => folder.path.includes('/failed_imports/')).length,
    [newFolders],
  );

  // "Retry failed imports" retries the failed folders of this tab's last batch
  // import, after a confirmation that names them; the job is then tracked below.
  const [retryDialogOpen, setRetryDialogOpen] = useState(false);
  const [retryState, setRetryState] = useState<AiBatchState | null>(null);
  const [retryLoading, setRetryLoading] = useState(false);
  const openRetryDialog = async () => {
    setRetryDialogOpen(true);
    setRetryNotice('');
    setRetryError('');
    setRetryState(null);
    setRetryLoading(true);
    try {
      const result = await getAiBatchStatus();
      setRetryState(result.state?.batch_job_id || result.state?.job_id ? result.state : null);
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : String(err));
    } finally {
      setRetryLoading(false);
    }
  };
  const retryFailedImports = async () => {
    if (!retryState) return;
    setRetryingFailed(true);
    try {
      const result = await recoverAiBatch(retryState.batch_job_id || retryState.job_id, true);
      const outcome = retryOutcome(result);
      const nextJobId = result.state?.job_id || result.job_id;
      if (outcome !== 'nothing_to_retry') {
        if (!nextJobId) throw new Error('the server did not return a job id');
        rememberJobId(nextJobId);
      }
      setRetryDialogOpen(false);
      setRetryNotice(retryOutcomeMessage(outcome, retryState.folders_retryable ?? retryableFolders(retryState).length));
      if (outcome === 'started') await onJobStarted?.();
    } catch (err) {
      setRetryDialogOpen(false);
      setRetryError(`Could not start the retry: ${err instanceof Error ? err.message : String(err)}. Nothing was retried.`);
    } finally {
      setRetryingFailed(false);
    }
  };
  const startImport = async () => {
    const nextPath = path.trim() || downloadsRoot;
    if (!nextPath) return;
    setImporting(true);
    setImportError('');
    try {
      const result = await startAiBatchImport(nextPath);
      rememberJobId(result.state?.job_id || result.job_id);
      setConfirmOpen(false);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err));
    } finally {
      setImporting(false);
    }
  };

  const onJobDone = () => {
    rememberJobId(null);
    void scan();
  };

  return (
    <div className="space-y-5">
      <Card>
        <CardContent className="space-y-4">
          <div>
            <h2 className="text-base font-semibold text-zinc-100">Import Source</h2>
            <p className="mt-1 text-sm text-zinc-400">
              Preview the configured downloads/staging folder before starting the backend import job.
            </p>
          </div>

          <div className="flex flex-col gap-3 lg:flex-row">
            <TextField
              fullWidth
              label="Import/source path"
              size="small"
              value={path}
              onChange={(event) => setPath(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && void scan()}
              slotProps={{ input: { style: { fontFamily: 'monospace', fontSize: '0.82rem' } } }}
            />
            <div className="flex flex-wrap gap-2">
              <Button disabled={scanning || Boolean(jobId) || !(path.trim() || downloadsRoot)} variant="outlined" onClick={() => void scan()}>
                {scanning ? `Previewing... ${scanElapsedSeconds}s` : 'Preview Import All'}
              </Button>
              {!jobId ? (
                <Button
                  disabled={importing || scanning || !preflight || newFolderCount === 0}
                  title={!preflight ? 'Preview Import All first' : newFolderCount === 0 ? 'No new source folders in the latest preview' : ''}
                  variant="contained"
                  onClick={() => setConfirmOpen(true)}
                >
                  Import All
                </Button>
              ) : null}
              <Button
                disabled={scanning || Boolean(jobId) || !downloadsRoot}
                title={downloadsRoot ? `Use the configured downloads folder (${downloadsRoot})` : 'Configured downloads folder not available'}
                variant="outlined"
                onClick={() => {
                  setPath(downloadsRoot);
                  setPreflight(null);
                }}
              >
                Downloads
              </Button>
              <Button
                disabled={scanning || Boolean(jobId) || !failedImportsPath}
                title={failedImportsPath ? `Preview ${failedImportsPath}` : 'Configured downloads folder not available'}
                variant="outlined"
                onClick={() => {
                  setPath(failedImportsPath);
                  setPreflight(null);
                }}
              >
                Failed
              </Button>
              <Button
                color="warning"
                disabled={scanning || Boolean(jobId) || retryingFailed}
                variant="outlined"
                title="Retry the failed folders from the last batch import"
                onClick={() => void openRetryDialog()}
              >
                {retryingFailed ? 'Retrying...' : 'Retry failed imports'}
              </Button>
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            <Chip
              label={downloads.loading ? 'Downloads folder: loading…' : `Downloads folder: ${downloadsRoot || 'unavailable'}`}
              size="small"
              variant="outlined"
            />
            <Chip label="Preview is read-only" size="small" color="info" variant="outlined" />
            <Chip label="Eligible matches import" size="small" color="success" variant="outlined" />
            <Chip label="Unsafe matches stay in Review" size="small" color="warning" variant="outlined" />
          </div>
          {downloadsProblem ? <Alert severity="warning">{downloadsProblem}</Alert> : null}
          {scanning ? (
            <div className="text-xs text-zinc-500">
              Scanning the source folder{scanElapsedSeconds > 0 ? ` (${scanElapsedSeconds}s elapsed)` : ''} — a large
              downloads folder can take a minute or more on the first pass. This is still working.
            </div>
          ) : null}
        </CardContent>
        {scanning ? <LinearProgress /> : null}
      </Card>

      {preflightError ? <Alert severity="error">{preflightError}</Alert> : null}
      {retryNotice ? <Alert severity="info" onClose={() => setRetryNotice('')}>{retryNotice}</Alert> : null}
      {retryError && !retryDialogOpen ? <Alert severity="error">{retryError}</Alert> : null}

      {preflight && !jobId ? (
        <div className="space-y-4">
          <PreflightSummary result={preflight} />

          <Card>
            <CardContent className="flex flex-col gap-3 lg:flex-row lg:items-center">
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-zinc-200">
                  {newFolderCount} new folder{newFolderCount !== 1 ? 's' : ''} eligible for Import All
                </div>
                <div className="mt-1 text-xs text-zinc-500">
                  {failedImportFolders > 0 ? `${failedImportFolders} folder(s) in this preview are under failed_imports. ` : ''}
                  Import All will run as one backend job, import eligible matches, and keep unsafe folders in Review.
                </div>
              </div>
              <div className="flex gap-2">
                <Button disabled={scanning} variant="outlined" onClick={() => void scan()}>
                  Re-scan
                </Button>
                <Button
                  disabled={importing || newFolderCount === 0}
                  variant="contained"
                  onClick={() => setConfirmOpen(true)}
                >
                  Import All
                </Button>
              </div>
            </CardContent>
          </Card>

          {importError ? <Alert severity="error">{importError}</Alert> : null}

          {preflight.folders.length > 0 ? (
            <div className="rounded border border-graphite-800 bg-graphite-950/30">
              <div className="flex items-center justify-between border-b border-graphite-800 px-3 py-2">
                <span className="text-[0.75rem] font-semibold uppercase tracking-wide text-zinc-500">
                  Folders ({preflight.folders.length})
                </span>
                {preflight.folders.length === 100 ? (
                  <span className="text-[0.7rem] text-zinc-600">showing first 100</span>
                ) : null}
              </div>
              {preflight.folders.map((folder) => (
                <FolderRow key={folder.path} folder={folder} />
              ))}
            </div>
          ) : (
            <Alert severity="success">No audio folders found.</Alert>
          )}
        </div>
      ) : null}

      {jobId ? <JobLog jobId={jobId} onDone={onJobDone} onJobIdChange={rememberJobId} /> : null}

      <ConfirmStartDialog
        busy={importing}
        fileCount={preflight?.audio_files ?? 0}
        folderCount={newFolderCount}
        open={confirmOpen}
        path={path.trim() || downloadsRoot}
        onClose={() => setConfirmOpen(false)}
        onConfirm={() => void startImport()}
      />
      <RetryFailuresDialog
        busy={retryingFailed}
        error={retryError}
        loading={retryLoading}
        open={retryDialogOpen}
        state={retryState}
        onClose={() => {
          setRetryDialogOpen(false);
          setRetryError('');
        }}
        onConfirm={() => void retryFailedImports()}
      />
    </div>
  );
}
