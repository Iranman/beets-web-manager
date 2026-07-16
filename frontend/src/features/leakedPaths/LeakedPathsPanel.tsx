import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fixLeakedDbPaths, getJobResult, scanLeakedDbPaths } from '../../api/client';
import type { LeakedDbPathRow, LeakedDbPathsResponse } from '../../api/types';
import { CleanActionBar, CleanEmptyState, CleanMetricGrid, CleanPanelHeader, CleanSection } from '../../components/CleanPanel';
import { JobStatusCard } from '../../components/JobStatusCard';
import { useJobPoll } from '../../lib/hooks';

// ── helpers ───────────────────────────────────────────────────────────────────

type LeakedFilter = 'all' | 'safe' | 'needs_review' | 'source_exists' | 'target_exists' | 'target_missing';
type LeakedSort = 'item' | 'album' | 'safety' | 'source_exists' | 'target_exists';

const LEAKED_FILTERS: Array<{ value: LeakedFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'safe', label: 'Safe' },
  { value: 'needs_review', label: 'Needs review' },
  { value: 'source_exists', label: 'Source exists' },
  { value: 'target_exists', label: 'Target exists' },
  { value: 'target_missing', label: 'Target missing' },
];

const LEAKED_SORTS: Array<{ value: LeakedSort; label: string }> = [
  { value: 'safety', label: 'Safety status' },
  { value: 'item', label: 'Item ID' },
  { value: 'album', label: 'Album ID' },
  { value: 'source_exists', label: 'Source exists' },
  { value: 'target_exists', label: 'Target exists' },
];

function formatScanTime(timestamp: number | null) {
  if (!timestamp) return 'Not scanned this session';
  return new Date(timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function basename(p: string): string {
  return p.split('/').filter(Boolean).at(-1) ?? p;
}

function dirpart(p: string): string {
  const parts = p.split('/').filter(Boolean);
  return parts.length > 1 ? '/' + parts.slice(0, -1).join('/') : '';
}

// ── Row card ──────────────────────────────────────────────────────────────────

function LeakedRow({ row, selected, onToggle }: {
  row: LeakedDbPathRow;
  selected: boolean;
  onToggle: () => void;
}) {
  return (
    <div className={`rounded border px-3 py-2.5 text-xs ${row.safe ? 'border-emerald-900/50 bg-emerald-950/10' : 'border-amber-900/50 bg-amber-950/15'}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono font-semibold text-zinc-100">{basename(row.db_path)}</span>
            {row.safe
              ? <Chip size="small" color="success" label="safe to fix" />
              : <Chip size="small" color="warning" label="needs review" />}
          </div>
          <div className="text-zinc-500">
            <span className="font-medium">item</span> {row.item_id}
            {row.album_id ? <> · <span className="font-medium">album</span> {row.album_id}</> : null}
          </div>
          <div>
            <span className="font-medium text-zinc-500">Old DB path: </span>
            <span className="font-mono text-rose-700 break-all">{row.db_path}</span>
          </div>
          {row.resolved_path ? (
            <div>
              <span className="font-medium text-zinc-500">Proposed fix: </span>
              <span className="font-mono text-emerald-700 break-all">{row.resolved_path}</span>
            </div>
          ) : null}
          <div className="flex flex-wrap gap-3 text-zinc-500">
            <span>
              Source exists:{' '}
              <span className={row.file_exists_at_db_path ? 'text-amber-600' : 'text-zinc-400'}>
                {row.file_exists_at_db_path ? 'yes' : 'no'}
              </span>
            </span>
            {row.resolved_path ? (
              <span>
                Target exists:{' '}
                <span className={row.file_exists_at_resolved ? 'text-emerald-600' : 'text-rose-500'}>
                  {row.file_exists_at_resolved ? 'yes' : 'no'}
                </span>
              </span>
            ) : null}
            <span>Dir: <span className="font-mono">{dirpart(row.abs_path)}</span></span>
          </div>
          <div className="text-zinc-400">
            <span className="font-medium">Action: </span>
            {row.safe
              ? 'DB path can be repaired after preview and confirmation; no files are moved.'
              : row.skip_reason || 'Skipped until the source/target path can be proven safe.'}
          </div>
          {row.skip_reason ? (
            <div className="text-amber-300">Skip reason: {row.skip_reason}</div>
          ) : null}
        </div>
        {row.safe ? (
          <label className="flex cursor-pointer items-center gap-1.5 select-none shrink-0 pt-0.5">
            <input type="checkbox" checked={selected} onChange={onToggle} className="h-4 w-4" />
            <span className="text-zinc-500">Include</span>
          </label>
        ) : null}
      </div>
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

type ScanData = LeakedDbPathsResponse;

export function LeakedPathsPanel() {
  const [scanJobId, setScanJobId] = useState<string | null>(null);
  const [fixJobId, setFixJobId] = useState<string | null>(null);
  const [scanData, setScanData] = useState<ScanData | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [showAll, setShowAll] = useState(false);
  const [filter, setFilter] = useState<LeakedFilter>('all');
  const [sort, setSort] = useState<LeakedSort>('safety');
  const [query, setQuery] = useState('');
  const [lastScanAt, setLastScanAt] = useState<number | null>(null);
  const [copyMsg, setCopyMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const prevScanStatus = useRef<string | null>(null);
  const prevFixStatus = useRef<string | null>(null);

  const { job: scanJob } = useJobPoll(scanJobId);
  const { job: fixJob } = useJobPoll(fixJobId);

  const safeRows = scanData?.rows.filter((r) => r.safe) ?? [];
  const unsafeRows = scanData?.rows.filter((r) => !r.safe) ?? [];

  // Capture scan results when scan job finishes
  useEffect(() => {
    if (!scanJob || scanJob.status === prevScanStatus.current) return;
    prevScanStatus.current = scanJob.status;
    if (scanJob.status === 'success') {
      const result = getJobResult<ScanData>(scanJob);
      if (result) {
        setScanData(result);
        setSelectedIds(new Set(result.rows.filter((r) => r.safe).map((r) => r.item_id)));
        setLastScanAt(Date.now());
      }
      setBusy(false);
    } else if (scanJob.status === 'failed' || scanJob.status === 'killed') {
      setBusy(false);
    }
  }, [scanJob?.status]);

  // Re-scan after a successful non-dry-run apply
  useEffect(() => {
    if (!fixJob || fixJob.status === prevFixStatus.current) return;
    prevFixStatus.current = fixJob.status;
    if (fixJob.status === 'success') {
      const isDryRun = (fixJob.metadata as Record<string, unknown> | undefined)?.dry_run !== false;
      if (!isDryRun) void doScan();
      setBusy(false);
    } else if (fixJob.status === 'failed' || fixJob.status === 'killed') {
      setBusy(false);
    }
  }, [fixJob?.status]);

  const doScan = useCallback(async () => {
    setError('');
    setBusy(true);
    setScanData(null);
    try {
      const res = await scanLeakedDbPaths();
      setScanJobId(res.job_id);
      setFixJobId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, []);

  const doFix = useCallback(async (dry_run: boolean) => {
    if (!dry_run && selectedIds.size === 0) return;
    if (!dry_run && !window.confirm(
      `Apply DB path fixes to ${selectedIds.size} item(s)?\n\nThis updates stored DB paths only — no files are moved or deleted.`
    )) return;
    setError('');
    setBusy(true);
    try {
      const ids = dry_run ? undefined : Array.from(selectedIds);
      const res = await fixLeakedDbPaths({ dry_run, item_ids: ids, confirmed: !dry_run });
      setFixJobId(res.job_id);
      setScanJobId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, [selectedIds]);

  const toggleAll = useCallback(() => {
    setSelectedIds((prev) =>
      prev.size === safeRows.length ? new Set() : new Set(safeRows.map((r) => r.item_id))
    );
  }, [safeRows]);

  const activeJob = fixJobId ? fixJob : scanJob;
  const metricItems = scanData ? [
    { label: 'Total leaked', value: scanData.total },
    { label: 'Safe to fix', value: scanData.safe_count, tone: scanData.safe_count ? 'success' as const : 'neutral' as const },
    { label: 'Needs review', value: scanData.unsafe_count, tone: scanData.unsafe_count ? 'warning' as const : 'success' as const },
    { label: 'Source exists', value: scanData.rows.filter((r) => r.file_exists_at_db_path).length, tone: scanData.rows.some((r) => r.file_exists_at_db_path) ? 'warning' as const : 'neutral' as const },
    { label: 'Target exists', value: scanData.rows.filter((r) => r.file_exists_at_resolved).length, tone: scanData.rows.some((r) => r.file_exists_at_resolved) ? 'success' as const : 'warning' as const },
  ] : [];

  const filteredRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = (scanData?.rows ?? []).filter((row) => {
      if (filter === 'safe' && !row.safe) return false;
      if (filter === 'needs_review' && row.safe) return false;
      if (filter === 'source_exists' && !row.file_exists_at_db_path) return false;
      if (filter === 'target_exists' && !row.file_exists_at_resolved) return false;
      if (filter === 'target_missing' && row.file_exists_at_resolved) return false;
      if (!needle) return true;
      return `${row.item_id} ${row.album_id} ${row.db_path} ${row.resolved_path ?? ''} ${row.skip_reason}`.toLowerCase().includes(needle);
    });
    return rows.sort((a, b) => {
      if (sort === 'item') return a.item_id - b.item_id;
      if (sort === 'album') return a.album_id - b.album_id || a.item_id - b.item_id;
      if (sort === 'source_exists') return Number(b.file_exists_at_db_path) - Number(a.file_exists_at_db_path) || a.item_id - b.item_id;
      if (sort === 'target_exists') return Number(b.file_exists_at_resolved) - Number(a.file_exists_at_resolved) || a.item_id - b.item_id;
      return Number(a.safe) - Number(b.safe) || a.item_id - b.item_id;
    });
  }, [filter, query, scanData?.rows, sort]);
  const visibleRows = showAll ? filteredRows : filteredRows.slice(0, 60);

  const copyPreview = useCallback(async () => {
    if (!scanData) return;
    const preview = filteredRows.map((row) => ({
      item_id: row.item_id,
      album_id: row.album_id,
      old_path: row.db_path,
      proposed_path: row.resolved_path,
      source_exists: row.file_exists_at_db_path,
      target_exists: row.file_exists_at_resolved,
      safe_to_repair: row.safe,
      skip_reason: row.skip_reason,
    }));
    try {
      await navigator.clipboard?.writeText(JSON.stringify(preview, null, 2));
      setCopyMsg(`Copied ${preview.length} preview row(s).`);
      window.setTimeout(() => setCopyMsg(''), 1800);
    } catch {
      setCopyMsg('Copy failed.');
    }
  }, [filteredRows, scanData]);

  return (
    <div>
      <CleanPanelHeader
        title="Leaked Path Cleanup"
        description="Find and fix DB rows where item paths contain unresolved Beets template tokens (e.g. $disc_subfolder). Updates DB paths only — no files are moved."
        meta={(
          <>
            <span>Mode: preview first</span>
            <span>Last scan: {formatScanTime(lastScanAt)}</span>
            <span>Status: {busy ? 'Running' : scanData ? 'Preview loaded' : 'Idle'}</span>
          </>
        )}
      />

      {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}

      <CleanActionBar>
        <Button variant="outlined" size="small" onClick={() => void doScan()} disabled={busy}>
          Scan DB
        </Button>
        {scanData && safeRows.length > 0 ? (
          <>
            <Button
              variant="outlined"
              size="small"
              onClick={() => void doFix(true)}
              disabled={busy || selectedIds.size === 0}
              title={selectedIds.size === 0 ? 'No rows selected' : undefined}
            >
              Preview fixes ({selectedIds.size})
            </Button>
            <Button
              variant="contained"
              size="small"
              color="primary"
              onClick={() => void doFix(false)}
              disabled={busy || selectedIds.size === 0}
              title={selectedIds.size === 0 ? 'Select rows to fix' : undefined}
            >
              Fix selected ({selectedIds.size})
            </Button>
          </>
        ) : null}
      </CleanActionBar>

      {busy && <LinearProgress sx={{ mt: 1 }} />}
      {scanData && <CleanMetricGrid items={metricItems} />}

      {activeJob && (
        <div className="mt-3">
          <JobStatusCard job={activeJob} runningLabel="Scanning leaked DB paths…" logLines={2} />
        </div>
      )}

      {scanData && scanData.total === 0 && (
        <CleanEmptyState title="No leaked path rows found" message="DB paths are clean — no unresolved template tokens detected." />
      )}

      {scanData && scanData.total > 0 && (
        <CleanSection
          title={`Leaked DB path rows (${filteredRows.length})`}
          description="Preview old and proposed DB paths before applying. Repairs update DB paths only; no files or folders are moved."
          count={<Chip label={`${safeRows.length} safe · ${unsafeRows.length} review`} size="small" variant="outlined" />}
        >
          <CleanActionBar>
            <TextField
              label="Search rows"
              size="small"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              sx={{ minWidth: 220 }}
            />
            <div className="flex flex-wrap gap-1">
              {LEAKED_FILTERS.map((item) => (
                <Button
                  key={item.value}
                  size="small"
                  variant={filter === item.value ? 'contained' : 'outlined'}
                  onClick={() => setFilter(item.value)}
                >
                  {item.label}
                </Button>
              ))}
            </div>
            <label className="flex min-w-[160px] flex-col gap-1 text-[0.68rem] font-medium uppercase tracking-wide text-zinc-500">
              Sort
              <select
                className="rounded border border-graphite-700 bg-graphite-950 px-2 py-2 text-sm normal-case tracking-normal text-zinc-200"
                value={sort}
                onChange={(event) => setSort(event.target.value as LeakedSort)}
              >
                {LEAKED_SORTS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
            </label>
            <Button size="small" variant="outlined" onClick={() => void copyPreview()}>
              Copy preview
            </Button>
          </CleanActionBar>
          {copyMsg ? <div className="mt-2 text-xs text-zinc-400">{copyMsg}</div> : null}
          <div className="mt-3 flex items-center gap-2">
            <Button size="small" variant="text" onClick={toggleAll}>
              {selectedIds.size === safeRows.length ? 'Deselect all' : 'Select all'}
            </Button>
            <span className="text-xs text-zinc-500">{selectedIds.size} of {safeRows.length} selected</span>
          </div>
          <div className="mt-2 space-y-2">
            {visibleRows.map((row) => (
              <LeakedRow
                key={row.item_id}
                row={row}
                selected={selectedIds.has(row.item_id)}
                onToggle={() => setSelectedIds((prev) => {
                  const next = new Set(prev);
                  if (next.has(row.item_id)) next.delete(row.item_id);
                  else next.add(row.item_id);
                  return next;
                })}
              />
            ))}
          </div>
          {filteredRows.length === 0 && (
            <CleanEmptyState title="No leaked DB rows match the current filters" />
          )}
          {filteredRows.length > 60 && !showAll && (
            <Button size="small" variant="text" sx={{ mt: 1 }} onClick={() => setShowAll(true)}>
              Show all {filteredRows.length} rows
            </Button>
          )}
        </CleanSection>
      )}
    </div>
  );
}
