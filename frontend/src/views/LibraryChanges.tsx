import { formatEpochDateTime } from '../lib/dates';
import { useReloadOnEngineReturn } from '../components/EngineOfflineBanner';
import { errorMessage, sentences } from '../lib/errors';
import { Description, Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  apiErrorBody,
  approveTransaction,
  applyTransaction,
  cancelTransaction,
  getTransaction,
  getTransactionSettings,
  getTransactions,
  rollbackTransaction,
  saveTransactionSettings,
  transactionExportUrl,
} from '../api/client';
import type {
  TransactionChange,
  TransactionDetail,
  TransactionSettings,
  TransactionSummary,
} from '../api/types';

const STATUS_OPTIONS = ['all', 'Pending', 'Preview', 'Approved', 'Running', 'Completed', 'Cancelled', 'Failed', 'Rolled Back', 'Partially Rolled Back', 'Recovery Required'];
const OPERATION_OPTIONS = [
  'all',
  'Import',
  'Rename',
  'Metadata Update',
  'Artwork Update',
  'Move',
  'Delete',
  'Replace',
  'Merge Artist',
  'Merge Album',
  'Split Album',
  'Playlist Import',
  'Library Cleanup',
  'Duplicate Removal',
  'AcoustID Match',
  'MusicBrainz Match',
  'AI Suggestion',
  'Repair',
  'Rescan',
];

const pageSize = 50;

// Cancel eligibility is server-owned (#356): the UI reads cancel.allowed and
// never re-derives it. A response without the field (older backend) keeps
// Cancel disabled.
function cancelBlockedReason(tx: TransactionDetail) {
  if (typeof tx.cancel?.allowed !== 'boolean') {
    return 'Cancel unavailable: the server did not report whether this transaction can be cancelled, so Cancel is disabled. Reload the page; if this persists, update Web Manager so the web UI and server versions match.';
  }
  return tx.cancel.reason || 'Cancel unavailable: the server does not allow cancelling this transaction.';
}

export function rollbackSuccessMessage(r: { job_id?: string; restored?: unknown; adopted?: unknown }) {
  if (r.job_id) return `Rollback job started: ${r.job_id}`;
  const restored = typeof r.restored === 'number' ? r.restored : Array.isArray(r.restored) ? r.restored.length : null;
  const adopted = typeof r.adopted === 'number' ? r.adopted : 0;
  if (restored === null) return 'Rollback finished.';
  return `Rollback finished: ${restored} item${restored === 1 ? '' : 's'} restored`
    + (adopted > 0 ? ` (${adopted} were already back after an interrupted rollback, so they were not moved again).` : '.');
}
// Rollback eligibility is server-owned (#228): the UI reads rollback.allowed
// and never re-derives it. A response without the field (older backend) keeps
// the action disabled.
function rollbackBlockedReason(tx: TransactionDetail) {
  if (typeof tx.rollback?.allowed !== 'boolean') {
    return 'Rollback unavailable: the server did not report whether this transaction can be rolled back, so Rollback is disabled. Reload the page; if this persists, update Web Manager so the web UI and server versions match.';
  }
  return tx.rollback.allowed_reason || 'Rollback unavailable: the server does not allow rolling back this transaction.';
}

// The phrase the backend requires (and verifies) before approving an
// album-cleanup plan that deletes audio files. The UI only detects the plan
// type to ask for it; the server stays the authority.
const DELETE_ALBUM_FILES_PHRASE = 'DELETE ALBUM FILES';

function deletesAlbumFiles(tx: TransactionSummary) {
  const meta = tx.metadata || {};
  return meta.mutation_family === 'album_cleanup_v1' && Boolean(meta.delete_files);
}

function approveErrorMessage(ex: unknown) {
  const code = apiErrorBody(ex)?.code;
  if (code === 'confirmation_required') {
    return `Approval refused: this album cleanup deletes audio files and the server did not accept the confirmation. Nothing was approved. Click Approve again and type ${DELETE_ALBUM_FILES_PHRASE} exactly.`;
  }
  if (code === 'not_preview') {
    return 'Approval refused: this transaction is no longer in Preview (it changed state, for example in another tab or job). Nothing was approved by this request. Review its current status below before acting.';
  }
  const detail = errorMessage(ex);
  return sentences(`Approval failed: ${detail}`, 'Review the transaction status below before retrying.');
}

// Only claim "nothing changed" when the response proves the request was
// refused before any mutation. Apply can fail after mutating (mutated: true,
// partial_mutation), or with an unknown outcome (5xx, timeout, network loss).
function actionErrorMessage(verb: 'Cancel' | 'Apply' | 'Rollback', ex: unknown) {
  const raw = errorMessage(ex);
  const reason = /[.!?]$/.test(raw) ? raw : `${raw}.`;
  const status = (ex as { httpStatus?: number } | null)?.httpStatus;
  const body = apiErrorBody(ex) as { code?: string; mutated?: boolean; partial_mutation?: boolean; status?: string } | undefined;
  if (verb === 'Rollback' && (body?.code === 'partial' || body?.status === 'Partially Rolled Back')) {
    return `Rollback finished only part-way: ${reason} Some changes were restored and the rest were not. Use Retry Rollback to restore the rest (items already restored are skipped), or check the log below.`;
  }
  if (verb === 'Cancel' && body?.code === 'engine_not_running') {
    return `Cancel not needed: ${reason} Reload to see the sync's final status.`;
  }
  if (verb === 'Cancel' && body?.code === 'engine_unavailable') {
    return `Cancel not sent: ${reason} The sync may still be running. Check that Beets is up, then try Cancel again.`;
  }
  // Apply and Rollback both write the library; Cancel only changes status.
  if (verb !== 'Cancel' && (body?.mutated === true || body?.partial_mutation === true || body?.code === 'partial_mutation'
    || status === undefined || status === 0 || status >= 500)) {
    return `${verb} failed: ${reason} The library may have been partly changed, or the outcome is unknown. Do not ${verb === 'Apply' ? 'apply' : 'roll back'} again. Check the transaction's current status below (for example Recovery Required) and follow its recovery guidance.`;
  }
  // A timed-out or 5xx cancel may still have landed, so only 404/409 (CAS
  // refusals) or an explicit mutated:false prove nothing changed.
  if (status === 404 || status === 409 || body?.mutated === false) {
    return `${verb} refused: ${reason} Nothing was changed by this request. Review the transaction's current status below before acting.`;
  }
  return `${verb} refused: ${reason} Review the transaction's current status below before acting.`;
}

function DeleteFilesApproveDialog({ open, busy, onCancel, onConfirm }: {
  open: boolean;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (phrase: string) => void;
}) {
  const [typed, setTyped] = useState('');
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);
  const matches = typed === DELETE_ALBUM_FILES_PHRASE;
  return (
    <Dialog className="relative z-50" open={open} onClose={() => { if (!busy) onCancel(); }}>
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-lg rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="font-display text-xl font-semibold text-zinc-50">Approve album cleanup that deletes files</DialogTitle>
          <form
            className="mt-3 space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              if (matches && !busy) onConfirm(typed);
            }}
          >
            <Alert severity="error">
              <Description as="span">
                Applying this plan permanently deletes the album&apos;s audio files from disk. Album cleanup cannot be rolled back.
              </Description>
            </Alert>
            <label htmlFor="confirm-delete-album-files" className="block text-sm text-zinc-300">
              Type <span className="font-mono font-semibold text-white">{DELETE_ALBUM_FILES_PHRASE}</span> to approve
            </label>
            <input
              id="confirm-delete-album-files"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              className="h-10 w-full rounded-md border border-field-border bg-graphite-950 px-3 font-mono text-sm text-zinc-50 focus:border-brass-400 pointer-coarse:h-11"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
            />
            <div className="ops-page changes-page flex flex-wrap justify-end gap-2">
              <Button size="small" variant="outlined" disabled={busy} onClick={onCancel}>Cancel</Button>
              <Button size="small" color="error" variant="contained" type="submit" disabled={!matches || busy}>Approve deletion</Button>
            </div>
          </form>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function formatDate(value?: number) {
  if (!value) return '-';
  return formatEpochDateTime(value);
}

function pct(value?: number | null) {
  if (typeof value !== 'number') return 'Unknown';
  return `${Math.round(value * 100)}%`;
}

// Status is a word in a state colour (DESIGN.md, the Label-With-Colour Rule), never colour alone.
function statusTone(status: string) {
  if (status === 'Completed' || status === 'Rolled Back') return 'text-emerald-300';
  if (status === 'Running' || status === 'Approved') return 'text-sky-300';
  if (status === 'Preview' || status === 'Pending' || status === 'Partially Rolled Back') return 'text-amber-300';
  if (status === 'Failed' || status === 'Cancelled' || status === 'Recovery Required') return 'text-red-300';
  return 'text-zinc-300';
}

function StatusWord({ status, className = '' }: { status: string; className?: string }) {
  return <span className={`font-semibold whitespace-nowrap ${statusTone(status)} ${className}`}>{status}</span>;
}

function countText(tx: TransactionSummary) {
  const counts = tx.counts || {};
  const items = counts.items ?? counts.item_count ?? counts.items_affected ?? counts.affected ?? 0;
  const files = counts.files ?? counts.files_changed ?? counts.moved ?? counts.renamed ?? 0;
  const changes = counts.changes ?? counts.changed ?? counts.updated ?? 0;
  return `${items || changes || 0} item${items === 1 ? '' : 's'} / ${files || 0} file${files === 1 ? '' : 's'}`;
}

function DiffRows({ change }: { change: TransactionChange }) {
  const rows = (change.metadata_diff || []).filter((row) => row.changed !== false);
  if (!rows.length) return <p className="text-xs text-muted">No field-level metadata diff captured.</p>;
  return (
    <dl className="ops-track-list text-xs">
      {rows.map((row) => (
        <div key={row.field} className="grid gap-x-3 gap-y-0.5 py-2 sm:grid-cols-[7rem_minmax(0,1fr)]">
          <dt className="font-semibold break-words text-zinc-200">{row.field}</dt>
          <dd className="m-0 min-w-0 space-y-0.5 [overflow-wrap:anywhere]">
            <div><span className="text-muted">Old:</span> <span className="text-rose-200">{String(row.old ?? 'None')}</span></div>
            <div><span className="text-muted">New:</span> <span className="text-emerald-200">{String(row.new ?? 'None')}</span></div>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function FilesystemRows({ change }: { change: TransactionChange }) {
  const rows = change.filesystem || [];
  if (!rows.length) return <p className="text-xs text-muted">No filesystem diff captured.</p>;
  return (
    <ul className="ops-track-list text-xs">
      {rows.map((row, index) => (
        <li key={`${row.operation}-${index}`} className="py-2">
          <div className="font-semibold text-amber-200">{row.operation || 'File change'}</div>
          <div className="mt-0.5 font-mono break-all text-zinc-400">Old: {row.old || '-'}</div>
          <div className="font-mono break-all text-zinc-200">New: {row.new || '-'}</div>
        </li>
      ))}
    </ul>
  );
}

function ChangePreview({ change }: { change: TransactionChange }) {
  return (
    <li className="py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="min-w-0">
          <div className="text-sm font-semibold break-words text-zinc-50">{change.track || change.album || change.artist || change.operation || 'Change'}</div>
          <div className="text-xs break-words text-zinc-400">{[change.artist, change.album].filter(Boolean).join(' - ') || change.source || 'No source label'}</div>
        </div>
        <span className="text-xs text-zinc-300 tabular-nums">Confidence {pct(change.confidence?.overall)}</span>
      </div>
      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-1 text-xs font-semibold text-muted">Metadata diff</div>
          <DiffRows change={change} />
        </div>
        <div className="min-w-0">
          <div className="mb-1 text-xs font-semibold text-muted">Filesystem diff</div>
          <FilesystemRows change={change} />
        </div>
      </div>
      {(change.reason || change.source) && (
        <div className="mt-3 space-y-0.5 text-xs break-words text-zinc-300">
          {change.reason && <div>Reason: {change.reason}</div>}
          {change.source && <div>Source: {change.source}</div>}
        </div>
      )}
    </li>
  );
}

export default function LibraryChanges() {
  const [rows, setRows] = useState<TransactionSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState('all');
  const [operation, setOperation] = useState('all');
  const [query, setQuery] = useState('');
  // ?tx=<id> opens that transaction (links from Recovery Required results).
  // Read once on mount from the real URL (BrowserRouter); no router hook, so the page renders standalone.
  const [selectedId, setSelectedId] = useState(() => new URLSearchParams(window.location.search).get('tx') || '');
  const [detail, setDetail] = useState<TransactionDetail | null>(null);
  const [settings, setSettings] = useState<TransactionSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  // The list failed to load: its count and emptiness are unknown, not zero.
  const [listFailed, setListFailed] = useState(false);

  const selected = useMemo(() => rows.find((row) => row.id === selectedId) || null, [rows, selectedId]);

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await getTransactions({ status, operation, q: query, offset, limit: pageSize });
      setRows(response.transactions);
      setTotal(response.total);
      setListFailed(false);
      if (!selectedId && response.transactions[0]) setSelectedId(response.transactions[0].id);
    } catch (ex) {
      // Rows from the previous load would contradict "Transactions unknown" (#618).
      setRows([]);
      setListFailed(true);
      setError(errorMessage(ex));
    } finally {
      setLoading(false);
    }
  }, [status, operation, query, offset, selectedId]);

  const loadDetail = useCallback(async (id: string) => {
    if (!id) {
      setDetail(null);
      return;
    }
    try {
      const response = await getTransaction(id, { limit: 100 });
      setDetail(response.transaction);
    } catch (ex) {
      setError(errorMessage(ex));
    }
  }, []);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);
  useReloadOnEngineReturn(loadRows);

  useEffect(() => {
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  useEffect(() => {
    void getTransactionSettings().then((response) => setSettings(response.settings)).catch(() => {});
  }, []);

  const updateSettings = async (patch: Partial<TransactionSettings>) => {
    if (!settings) return;
    const previous = settings;
    setSettings({ ...settings, ...patch });
    try {
      const response = await saveTransactionSettings(patch);
      setSettings(response.settings);
    } catch (ex) {
      setSettings(previous);
      setError(sentences(`Setting not saved: ${errorMessage(ex)}`, 'The previous value is kept.'));
    }
  };

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [approving, setApproving] = useState(false);

  const doApprove = async (confirmDeleteFiles?: string) => {
    if (!detail) return;
    setApproving(true);
    setError('');
    setMessage('');
    let failure = '';
    try {
      await approveTransaction(detail.id, confirmDeleteFiles);
      // Action responses omit rollback.allowed; only the detail GET has it.
      await loadDetail(detail.id);
      setMessage('Transaction approved.');
    } catch (ex) {
      failure = approveErrorMessage(ex);
      await loadDetail(detail.id);
    } finally {
      setApproving(false);
      setConfirmOpen(false);
    }
    await loadRows();
    // loadRows clears the error banner, so surface the refusal after it.
    if (failure) setError(failure);
  };

  // Same refusal handling as doApprove: show the server's reason, then reload
  // the detail and rows so the user sees the transaction's real state.
  const runAction = async (
    verb: 'Cancel' | 'Apply' | 'Rollback',
    request: (id: string) => Promise<{ transaction?: TransactionDetail; job_id?: string }>,
    success: (response: { job_id?: string; restored?: unknown; adopted?: unknown; code?: string }) => string,
  ) => {
    if (!detail) return;
    setError('');
    setMessage('');
    let failure = '';
    try {
      const response = await request(detail.id);
      // Action responses omit rollback.allowed (and engine families omit the
      // transaction); only the detail GET has it, so always reload.
      await loadDetail(detail.id);
      setMessage(success(response));
    } catch (ex) {
      failure = actionErrorMessage(verb, ex);
      await loadDetail(detail.id);
    }
    await loadRows();
    // loadRows clears the error banner, so surface the refusal after it.
    if (failure) setError(failure);
  };
  const doCancel = () => runAction('Cancel', cancelTransaction, (r) => (r.code === 'engine_cancelling'
    ? 'Cancel sent to Beets. It stops after the album it is on; albums already synced keep their changes.'
    : 'Transaction cancelled.'));
  const doApply = () => runAction('Apply', applyTransaction, (r) => (r.job_id ? `Apply job started: ${r.job_id}` : 'Transaction applied.'));

  const doRollback = () => {
    if (!window.confirm('Rollback this transaction? Review the rollback availability before continuing.')) return;
    void runAction('Rollback', rollbackTransaction, rollbackSuccessMessage);
  };

  const figures: [string, string][] = detail ? [
    ['Date', formatDate(detail.created_at)],
    ['Job', detail.originating_job || '-'],
    ['Dry run', detail.dry_run ? 'Yes' : 'No'],
    ['Undo', detail.rollback?.allowed === true ? 'Available' : 'Unavailable'],
    ['Overall', pct(detail.confidence?.overall)],
    ['MusicBrainz', pct(detail.confidence?.musicbrainz)],
    ['AcoustID', pct(detail.confidence?.acoustid)],
    ['Artwork', pct(detail.confidence?.artwork)],
    ['AI', pct(detail.confidence?.ai)],
  ] : [];

  return (
    <section aria-labelledby="library-changes-title" className="ops-page changes-page space-y-6">
      <div className="max-w-3xl">
        <h2 id="library-changes-title" className="ops-section-title">Library Changes</h2>
        <p className="mt-1 text-sm text-zinc-400">Transaction history, previews, confidence, exports, and rollback status for library-changing work.</p>
      </div>

      {loading && <LinearProgress />}
      {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}
      <DeleteFilesApproveDialog
        open={confirmOpen}
        busy={approving}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={(phrase) => void doApprove(phrase)}
      />
      {message && <Alert severity="success" onClose={() => setMessage('')}>{message}</Alert>}

      <div className="grid gap-x-8 gap-y-6 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.35fr)]">
        <section aria-label="Transaction ledger" className="min-w-0 space-y-4">
          <div className="grid gap-2 sm:grid-cols-3">
            <TextField select size="small" label="Status" value={status} onChange={(event) => { setOffset(0); setStatus(event.target.value); }}>
              {STATUS_OPTIONS.map((option) => <MenuItem key={option} value={option}>{option === 'all' ? 'All statuses' : option}</MenuItem>)}
            </TextField>
            <TextField select size="small" label="Operation" value={operation} onChange={(event) => { setOffset(0); setOperation(event.target.value); }}>
              {OPERATION_OPTIONS.map((option) => <MenuItem key={option} value={option}>{option === 'all' ? 'All operations' : option}</MenuItem>)}
            </TextField>
            <TextField size="small" label="Search" value={query} onChange={(event) => { setOffset(0); setQuery(event.target.value); }} />
          </div>

          <div className="overflow-hidden">
            <div className="flex items-baseline justify-between gap-3 border-b border-graphite-700 pb-2 text-sm text-zinc-300 tabular-nums">
              {listFailed ? (
                <span>Transactions unknown</span>
              ) : (
                <>
                  <span className="font-semibold text-zinc-50">{total} transaction{total === 1 ? '' : 's'}</span>
                  <span className="text-xs text-muted">{total ? `${offset + 1}-${Math.min(offset + pageSize, total)}` : '—'}</span>
                </>
              )}
            </div>
            <div>
              {rows.length === 0 && !loading && !listFailed && <p className="py-6 text-sm text-muted">No transactions found.</p>}
              {rows.map((row) => (
                <button
                  key={row.id}
                  type="button"
                  aria-pressed={row.id === selectedId}
                  className={`ops-ledger-row grid w-full grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-0.5 border-b border-graphite-800 px-2 py-3 text-left ${row.id === selectedId ? 'is-selected' : ''}`}
                  onClick={() => setSelectedId(row.id)}
                >
                  <span className="truncate text-sm font-semibold text-zinc-50">{row.operation_type}</span>
                  <StatusWord status={row.status} className="text-xs" />
                  <span className="col-span-2 text-xs text-zinc-400 tabular-nums">{formatDate(row.created_at)} · {countText(row)}</span>
                  <span className="col-span-2 truncate text-xs text-muted">{row.summary || row.reason || row.id}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between">
            <Button size="small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</Button>
            <Button size="small" disabled={offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)}>Next</Button>
          </div>
        </section>

        <div className="min-w-0 space-y-6">
          {!detail && selected && <LinearProgress />}
          {detail ? (
            <>
              <article aria-labelledby="transaction-detail-title" className="rounded-lg border border-graphite-700 bg-graphite-900 p-4 sm:p-5">
                <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <h3 id="transaction-detail-title" className="font-display text-2xl leading-tight font-semibold break-words text-zinc-50">{detail.operation_type}</h3>
                    <div className="mt-1 text-sm break-words text-zinc-300">{detail.summary || detail.reason || 'No summary captured.'}</div>
                    <div className="mt-1 font-mono text-xs break-all text-muted">{detail.id}</div>
                  </div>
                  <StatusWord status={detail.status} className="text-base" />
                </div>

                <dl className="ops-figures changes-figures mt-4">
                  {figures.map(([label, value]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>

                {detail.rollback?.allowed !== true && (
                  // role="status": selecting a transaction should not announce an alert.
                  <Alert id="rollback-blocked-reason" role="status" severity="warning" className="mt-4">
                    {rollbackBlockedReason(detail)}
                  </Alert>
                )}
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button size="small" variant="contained" disabled={approving || detail.status === 'Approved' || detail.status === 'Completed' || detail.status === 'Running'} onClick={() => (deletesAlbumFiles(detail) ? setConfirmOpen(true) : void doApprove())}>Approve</Button>
                  <Button size="small" color="success" variant="contained" disabled={detail.status !== 'Approved'} onClick={() => void doApply()}>Apply</Button>
                  <Button size="small" color="warning" variant="outlined" disabled={detail.cancel?.allowed !== true} aria-describedby={detail.cancel?.allowed === true ? undefined : 'cancel-blocked-reason'} onClick={() => void doCancel()}>{detail.cancel?.mode === 'engine' ? 'Cancel Sync' : 'Cancel'}</Button>
                  <Button size="small" color="error" variant="outlined" disabled={detail.rollback?.allowed !== true} aria-describedby={detail.rollback?.allowed === true ? undefined : 'rollback-blocked-reason'} onClick={doRollback}>{detail.status === 'Partially Rolled Back' ? 'Retry Rollback' : 'Rollback'}</Button>
                </div>
                {detail.cancel?.allowed !== true && (
                  <p id="cancel-blocked-reason" className="mt-2 text-xs text-zinc-400">{cancelBlockedReason(detail)}</p>
                )}

                <div role="group" aria-label="Export this transaction" className="mt-4 flex flex-wrap items-center gap-2 border-t border-graphite-700 pt-3">
                  <span className="mr-1 text-xs font-semibold text-muted">Export</span>
                  <Button size="small" variant="text" href={transactionExportUrl(detail.id, 'markdown')}>Markdown</Button>
                  <Button size="small" variant="text" href={transactionExportUrl(detail.id, 'json')}>JSON</Button>
                  <Button size="small" variant="text" href={transactionExportUrl(detail.id, 'csv')}>CSV</Button>
                </div>
              </article>

              <section aria-labelledby="transaction-preview-title">
                <div className="flex items-baseline justify-between gap-3 border-b border-graphite-700 pb-2">
                  <h3 id="transaction-preview-title" className="text-base font-semibold text-zinc-50">Preview changes</h3>
                  <span className="text-xs text-muted tabular-nums">{detail.changes_total} captured</span>
                </div>
                {detail.changes.length === 0 ? (
                  <p className="py-4 text-sm text-zinc-400">This transaction has job-level history only. Item-level diffs have not been captured for this workflow yet.</p>
                ) : (
                  <ul className="ops-track-list">
                    {detail.changes.map((change, index) => <ChangePreview key={change.id || index} change={change} />)}
                  </ul>
                )}
              </section>

              {detail.logs && detail.logs.length > 0 && (
                <section aria-labelledby="transaction-log-title">
                  <h3 id="transaction-log-title" className="border-b border-graphite-700 pb-2 text-base font-semibold text-zinc-50">Log</h3>
                  <pre tabIndex={0} aria-labelledby="transaction-log-title" className="mt-3 max-h-72 overflow-auto rounded-md bg-graphite-950 p-3 text-xs whitespace-pre-wrap text-zinc-400">{detail.logs.slice(-80).join('\n')}</pre>
                </section>
              )}

              {settings && (
                <section aria-labelledby="transaction-settings-title">
                  <h3 id="transaction-settings-title" className="border-b border-graphite-700 pb-2 text-base font-semibold text-zinc-50">Transaction Settings</h3>
                  <div className="mt-2 grid gap-x-4 text-sm text-zinc-300 sm:grid-cols-2 xl:grid-cols-4">
                    <label className="flex min-h-10 cursor-pointer items-center gap-2 pointer-coarse:min-h-11"><input type="checkbox" className="size-4 accent-brass-500" checked={settings.enabled} onChange={(e) => void updateSettings({ enabled: e.target.checked })} /> History</label>
                    <label className="flex min-h-10 cursor-pointer items-center gap-2 pointer-coarse:min-h-11"><input type="checkbox" className="size-4 accent-brass-500" checked={settings.backups_enabled} onChange={(e) => void updateSettings({ backups_enabled: e.target.checked })} /> Backups</label>
                    <label className="flex min-h-10 cursor-pointer items-center gap-2 pointer-coarse:min-h-11"><input type="checkbox" className="size-4 accent-brass-500" checked={settings.rollback_enabled} onChange={(e) => void updateSettings({ rollback_enabled: e.target.checked })} /> Rollback</label>
                    <label className="flex min-h-10 cursor-pointer items-center gap-2 pointer-coarse:min-h-11"><input type="checkbox" className="size-4 accent-brass-500" checked={settings.dry_run_by_default} onChange={(e) => void updateSettings({ dry_run_by_default: e.target.checked })} /> Dry run by default</label>
                  </div>
                </section>
              )}
            </>
          ) : (
            <p className="border-t border-graphite-700 pt-4 text-sm text-zinc-400">Select a transaction to inspect its preview, confidence, files, and rollback status.</p>
          )}
        </div>
      </div>
    </section>
  );
}
