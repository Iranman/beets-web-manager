import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
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

const STATUS_OPTIONS = ['all', 'Pending', 'Preview', 'Approved', 'Running', 'Completed', 'Cancelled', 'Failed', 'Rolled Back', 'Partially Rolled Back'];
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

function formatDate(value?: number) {
  if (!value) return '-';
  return new Date(value * 1000).toLocaleString();
}

function pct(value?: number | null) {
  if (typeof value !== 'number') return 'Unknown';
  return `${Math.round(value * 100)}%`;
}

function statusColor(status: string): 'default' | 'primary' | 'success' | 'warning' | 'error' {
  if (status === 'Completed' || status === 'Rolled Back') return 'success';
  if (status === 'Running' || status === 'Approved') return 'primary';
  if (status === 'Preview' || status === 'Pending' || status === 'Partially Rolled Back') return 'warning';
  if (status === 'Failed' || status === 'Cancelled') return 'error';
  return 'default';
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
  if (!rows.length) return <div className="text-xs text-zinc-500">No field-level metadata diff captured.</div>;
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.field} className="rounded border border-graphite-700 bg-graphite-950 p-2 text-xs">
          <div className="font-semibold text-zinc-200">{row.field}</div>
          <div className="mt-1 grid gap-1 sm:grid-cols-2">
            <div><span className="text-zinc-500">Old:</span> <span className="text-rose-200">{String(row.old ?? 'None')}</span></div>
            <div><span className="text-zinc-500">New:</span> <span className="text-emerald-200">{String(row.new ?? 'None')}</span></div>
          </div>
        </div>
      ))}
    </div>
  );
}

function FilesystemRows({ change }: { change: TransactionChange }) {
  const rows = change.filesystem || [];
  if (!rows.length) return <div className="text-xs text-zinc-500">No filesystem diff captured.</div>;
  return (
    <div className="space-y-2">
      {rows.map((row, index) => (
        <div key={`${row.operation}-${index}`} className="rounded border border-graphite-700 bg-graphite-950 p-2 text-xs">
          <div className="font-semibold text-amber-200">{row.operation || 'File change'}</div>
          <div className="mt-1 break-all text-zinc-400">Old: {row.old || '-'}</div>
          <div className="break-all text-zinc-200">New: {row.new || '-'}</div>
        </div>
      ))}
    </div>
  );
}

function ChangePreview({ change }: { change: TransactionChange }) {
  return (
    <div className="rounded border border-graphite-700 bg-graphite-900 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-white">{change.track || change.album || change.artist || change.operation || 'Change'}</div>
          <div className="text-xs text-zinc-400">{[change.artist, change.album].filter(Boolean).join(' - ') || change.source || 'No source label'}</div>
        </div>
        <Chip size="small" label={pct(change.confidence?.overall)} variant="outlined" />
      </div>
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div>
          <div className="mb-1 text-[0.68rem] font-semibold uppercase text-zinc-500">Metadata diff</div>
          <DiffRows change={change} />
        </div>
        <div>
          <div className="mb-1 text-[0.68rem] font-semibold uppercase text-zinc-500">Filesystem diff</div>
          <FilesystemRows change={change} />
        </div>
      </div>
      {(change.reason || change.source) && (
        <div className="mt-3 rounded bg-graphite-950 p-2 text-xs text-zinc-300">
          {change.reason && <div>Reason: {change.reason}</div>}
          {change.source && <div>Source: {change.source}</div>}
        </div>
      )}
    </div>
  );
}

export default function LibraryChanges() {
  const [rows, setRows] = useState<TransactionSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState('all');
  const [operation, setOperation] = useState('all');
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<TransactionDetail | null>(null);
  const [settings, setSettings] = useState<TransactionSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const selected = useMemo(() => rows.find((row) => row.id === selectedId) || null, [rows, selectedId]);

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await getTransactions({ status, operation, q: query, offset, limit: pageSize });
      setRows(response.transactions);
      setTotal(response.total);
      if (!selectedId && response.transactions[0]) setSelectedId(response.transactions[0].id);
    } catch (ex) {
      setError(ex instanceof Error ? ex.message : String(ex));
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
      setError(ex instanceof Error ? ex.message : String(ex));
    }
  }, []);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  useEffect(() => {
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  useEffect(() => {
    void getTransactionSettings().then((response) => setSettings(response.settings)).catch(() => {});
  }, []);

  const updateSettings = async (patch: Partial<TransactionSettings>) => {
    if (!settings) return;
    const next = { ...settings, ...patch };
    setSettings(next);
    const response = await saveTransactionSettings(patch);
    setSettings(response.settings);
  };

  const doApprove = async () => {
    if (!detail) return;
    const response = await approveTransaction(detail.id);
    setDetail(response.transaction);
    setMessage('Transaction approved.');
    await loadRows();
  };

  const doCancel = async () => {
    if (!detail) return;
    const response = await cancelTransaction(detail.id);
    setDetail(response.transaction);
    setMessage('Transaction cancelled.');
    await loadRows();
  };
  const doApply = async () => {
    if (!detail) return;
    const response = await applyTransaction(detail.id);
    setDetail(response.transaction);
    setMessage(response.job_id ? `Apply job started: ${response.job_id}` : 'Transaction applied.');
    await loadRows();
  };

  const doRollback = async () => {
    if (!detail) return;
    if (!window.confirm('Rollback this transaction? Review the rollback availability before continuing.')) return;
    try {
      const response = await rollbackTransaction(detail.id);
      setDetail(response.transaction);
      setMessage('Rollback started.');
      await loadRows();
    } catch (ex) {
      setError(ex instanceof Error ? ex.message : String(ex));
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-white">Library Changes</h1>
          <p className="mt-1 text-sm text-zinc-400">Transaction history, previews, confidence, exports, and rollback status for library-changing work.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {detail && (
            <>
              <Button size="small" variant="outlined" href={transactionExportUrl(detail.id, 'markdown')}>Markdown</Button>
              <Button size="small" variant="outlined" href={transactionExportUrl(detail.id, 'json')}>JSON</Button>
              <Button size="small" variant="outlined" href={transactionExportUrl(detail.id, 'csv')}>CSV</Button>
            </>
          )}
        </div>
      </div>

      {loading && <LinearProgress />}
      {error && <Alert severity="error" onClose={() => setError('')}>{error}</Alert>}
      {message && <Alert severity="success" onClose={() => setMessage('')}>{message}</Alert>}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.35fr)]">
        <section className="space-y-3">
          <div className="grid gap-2 sm:grid-cols-3">
            <TextField select size="small" label="Status" value={status} onChange={(event) => { setOffset(0); setStatus(event.target.value); }}>
              {STATUS_OPTIONS.map((option) => <MenuItem key={option} value={option}>{option === 'all' ? 'All statuses' : option}</MenuItem>)}
            </TextField>
            <TextField select size="small" label="Operation" value={operation} onChange={(event) => { setOffset(0); setOperation(event.target.value); }}>
              {OPERATION_OPTIONS.map((option) => <MenuItem key={option} value={option}>{option === 'all' ? 'All operations' : option}</MenuItem>)}
            </TextField>
            <TextField size="small" label="Search" value={query} onChange={(event) => { setOffset(0); setQuery(event.target.value); }} />
          </div>

          <div className="overflow-hidden rounded border border-graphite-700">
            <div className="grid grid-cols-[1fr_auto] border-b border-graphite-700 bg-graphite-900 px-3 py-2 text-xs font-semibold uppercase text-zinc-500">
              <span>{total} transaction{total === 1 ? '' : 's'}</span>
              <span>{offset + 1}-{Math.min(offset + pageSize, total || 0)}</span>
            </div>
            <div className="divide-y divide-graphite-800">
              {rows.length === 0 && <div className="p-4 text-sm text-zinc-500">No transactions found.</div>}
              {rows.map((row) => (
                <button
                  key={row.id}
                  className={`grid w-full gap-1 px-3 py-3 text-left hover:bg-graphite-900 ${row.id === selectedId ? 'bg-red-950/25' : 'bg-graphite-950'}`}
                  onClick={() => setSelectedId(row.id)}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-white">{row.operation_type}</span>
                    <Chip size="small" color={statusColor(row.status)} label={row.status} />
                  </div>
                  <div className="text-xs text-zinc-400">{formatDate(row.created_at)} - {countText(row)}</div>
                  <div className="truncate text-xs text-zinc-500">{row.summary || row.reason || row.id}</div>
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-between">
            <Button size="small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>Previous</Button>
            <Button size="small" disabled={offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)}>Next</Button>
          </div>
        </section>

        <section className="space-y-3">
          {!detail && selected && <LinearProgress />}
          {detail ? (
            <>
              <div className="rounded border border-graphite-700 bg-graphite-900 p-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="text-lg font-semibold text-white">{detail.operation_type}</div>
                    <div className="mt-1 text-xs text-zinc-400">{detail.id}</div>
                    <div className="mt-1 text-sm text-zinc-300">{detail.summary || detail.reason || 'No summary captured.'}</div>
                  </div>
                  <Chip color={statusColor(detail.status)} label={detail.status} />
                </div>
                <div className="mt-3 grid gap-2 text-xs text-zinc-300 sm:grid-cols-2 lg:grid-cols-3">
                  <div>Date: {formatDate(detail.created_at)}</div>
                  <div>Job: {detail.originating_job || '-'}</div>
                  <div>Dry run: {detail.dry_run ? 'Yes' : 'No'}</div>
                  <div>Overall: {pct(detail.confidence?.overall)}</div>
                  <div>AI: {pct(detail.confidence?.ai)}</div>
                  <div>AcoustID: {pct(detail.confidence?.acoustid)}</div>
                  <div>MusicBrainz: {pct(detail.confidence?.musicbrainz)}</div>
                  <div>Artwork: {pct(detail.confidence?.artwork)}</div>
                  <div>Undo: {detail.rollback?.available ? 'Available' : 'Unavailable'}</div>
                </div>
                {!detail.rollback?.available && (
                  <Alert severity="warning" className="mt-3">
                    Rollback unavailable. {detail.rollback?.reason || 'This workflow has not recorded reversible operations yet.'}
                  </Alert>
                )}
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button size="small" variant="contained" disabled={detail.status === 'Approved' || detail.status === 'Completed' || detail.status === 'Running'} onClick={() => void doApprove()}>Approve</Button>
                  <Button size="small" color="success" variant="contained" disabled={detail.status !== 'Approved'} onClick={() => void doApply()}>Apply</Button>
                  <Button size="small" color="warning" variant="outlined" disabled={detail.status === 'Completed' || detail.status === 'Running'} onClick={() => void doCancel()}>Cancel</Button>
                  <Button size="small" color="error" variant="outlined" disabled={!detail.rollback?.available || detail.status === 'Running'} onClick={() => void doRollback()}>Rollback</Button>
                </div>
              </div>

              {settings && (
                <div className="rounded border border-graphite-700 bg-graphite-900 p-3">
                  <div className="mb-2 text-sm font-semibold text-white">Transaction Settings</div>
                  <div className="grid gap-2 text-xs text-zinc-300 sm:grid-cols-2 lg:grid-cols-4">
                    <label className="flex items-center gap-2"><input type="checkbox" checked={settings.enabled} onChange={(e) => void updateSettings({ enabled: e.target.checked })} /> History</label>
                    <label className="flex items-center gap-2"><input type="checkbox" checked={settings.backups_enabled} onChange={(e) => void updateSettings({ backups_enabled: e.target.checked })} /> Backups</label>
                    <label className="flex items-center gap-2"><input type="checkbox" checked={settings.rollback_enabled} onChange={(e) => void updateSettings({ rollback_enabled: e.target.checked })} /> Rollback</label>
                    <label className="flex items-center gap-2"><input type="checkbox" checked={settings.dry_run_by_default} onChange={(e) => void updateSettings({ dry_run_by_default: e.target.checked })} /> Dry run by default</label>
                  </div>
                </div>
              )}

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <div className="text-sm font-semibold text-white">Preview changes</div>
                  <div className="text-xs text-zinc-500">{detail.changes_total} captured</div>
                </div>
                {detail.changes.length === 0 ? (
                  <Alert severity="info">This transaction has job-level history only. Item-level diffs have not been captured for this workflow yet.</Alert>
                ) : (
                  detail.changes.map((change, index) => <ChangePreview key={change.id || index} change={change} />)
                )}
              </div>

              {detail.logs && detail.logs.length > 0 && (
                <div className="rounded border border-graphite-700 bg-graphite-950 p-3">
                  <div className="mb-2 text-sm font-semibold text-white">Log</div>
                  <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs text-zinc-400">{detail.logs.slice(-80).join('\n')}</pre>
                </div>
              )}
            </>
          ) : (
            <Alert severity="info">Select a transaction to inspect its preview, confidence, files, and rollback status.</Alert>
          )}
        </section>
      </div>
    </div>
  );
}
