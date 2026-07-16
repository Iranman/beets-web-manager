import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  runDedupCleanup,
  startDedupAiReview,
  startDedupScan,
} from '../../api/client';
import { CleanActionBar, CleanEmptyState, CleanMetricGrid, CleanPanelHeader } from '../../components/CleanPanel';
import { LogViewer } from '../../components/LogViewer';
import type { DedupDuplicate } from '../../api/types';
import { useDedupScan } from '../../lib/hooks';

const MUSIC_ROOT    = '/data/media/music';
const DOWNLOADS_ROOT = '/data/torrents/music';
const DEFAULT_PATH  = MUSIC_ROOT;
type DedupScanKind = 'standard' | 'ai';

function shortScanId(scanId: string | null) {
  return scanId ? scanId.slice(0, 8) : 'None';
}

function scanKindLabel(kind: DedupScanKind) {
  return kind === 'ai' ? 'AI deep scan' : 'Duplicate scan';
}

function jobsUrl(jobId: string | null) {
  return jobId ? `/jobs?q=${encodeURIComponent(jobId)}` : '/jobs';
}

// ── Duplicate row ─────────────────────────────────────────────────────────────

function DupRow({
  dup,
  selected,
  onToggle,
}: {
  dup: DedupDuplicate;
  selected: boolean;
  onToggle: () => void;
}) {
  return (
    <label className="grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-start gap-3 border-t border-graphite-800/80 px-3 py-2.5 transition first:border-t-0 hover:bg-graphite-900/50">
      <input
        checked={selected}
        className="mt-0.5 accent-red-500"
        type="checkbox"
        onChange={onToggle}
      />
      <div className="min-w-0 space-y-0.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium text-zinc-200 text-sm">
            {dup.source_artist || '—'}{dup.source_title ? ` — ${dup.source_title}` : ''}
          </span>
          <Chip
            color={dup.confidence === 'high' ? 'error' : 'warning'}
            label={dup.confidence}
            size="small"
            variant="outlined"
          />
        </div>
        <div className="truncate font-mono text-[0.68rem] text-red-400" title="This file will be deleted">
          {dup.source_path}
        </div>
        {dup.lib_path && dup.lib_path !== dup.source_path && (
          <div className="truncate font-mono text-[0.68rem] text-emerald-600" title="Kept — library copy">
            ↳ {dup.lib_path}
          </div>
        )}
        {dup.reason && (
          <div className="text-[0.7rem] italic text-zinc-600">{dup.reason}</div>
        )}
      </div>
    </label>
  );
}

// ── Progress bar ──────────────────────────────────────────────────────────────

function ScanProgress({ scanned, total }: { scanned: number; total: number }) {
  const pct = total > 0 ? Math.round((scanned / total) * 100) : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-zinc-500">
        <span>Scanning…</span>
        <span>{scanned} / {total}</span>
      </div>
      <LinearProgress variant="determinate" value={pct} sx={{ borderRadius: 1 }} />
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export function DedupPanel() {
  const navigate = useNavigate();
  const [path, setPath] = useState(DEFAULT_PATH);
  const [starting, setStarting] = useState(false);
  const [scanJid, setScanJid] = useState<string | null>(null);
  const [scanKind, setScanKind] = useState<DedupScanKind>('standard');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [cleanupConfirm, setCleanupConfirm] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [cleanupResult, setCleanupResult] = useState<string>('');
  const [error, setError] = useState('');
  const [showRawScanLog, setShowRawScanLog] = useState(false);

  const { scan, error: scanError } = useDedupScan(scanJid);
  const duplicates = scan?.duplicates ?? [];
  const done = scan?.status === 'done';
  const running = scan?.status === 'running';
  const scanFailed = scan?.job_status === 'failed' || scan?.job_status === 'cancelled' || scan?.job_status === 'killed';
  const scanStatusLabel = running ? 'Running' : scanFailed ? 'Failed' : 'Done';
  const scanStatusColor = running ? 'info' : scanFailed ? 'error' : 'success';
  const canRunAiReview = done && !scanFailed && scanKind !== 'ai';

  // Auto-select high-confidence duplicates when a scan first completes
  const autoSelectedRef = useRef<string | null>(null);
  useEffect(() => {
    if (done && scanJid && autoSelectedRef.current !== scanJid) {
      autoSelectedRef.current = scanJid;
      const highConf = duplicates.filter((d) => d.confidence === 'high').map((d) => d.source_path);
      if (highConf.length) setSelected(new Set(highConf));
    }
  }, [done, scanJid, duplicates]);

  const startScan = async () => {
    setStarting(true);
    setError('');
    setSelected(new Set());
    setCleanupResult('');
    try {
      const r = await startDedupScan(path.trim() || DEFAULT_PATH);
      setScanJid(r.job_id);
      setScanKind('standard');
      window.dispatchEvent(new Event('beets:jobs-changed'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  };

  const startAiReview = async () => {
    if (!scanJid) return;
    setError('');
    try {
      const r = await startDedupAiReview(scanJid, path.trim() || DEFAULT_PATH);
      setScanJid(r.job_id);
      setScanKind('ai');
      setSelected(new Set());
      window.dispatchEvent(new Event('beets:jobs-changed'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const toggleAll = () => {
    if (selected.size === duplicates.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(duplicates.map((d) => d.source_path)));
    }
  };

  const handleCleanup = async (dryRun: boolean) => {
    if (!scanJid) return;
    setCleaning(true);
    setCleanupConfirm(false);
    try {
      const r = await runDedupCleanup(
        Array.from(selected),
        path.trim() || DEFAULT_PATH,
        dryRun,
      );
      setCleanupResult(
        dryRun
          ? `Dry run: would delete ${r.deleted ?? 0} file(s), skip ${r.skipped ?? 0}`
          : `Deleted ${r.deleted ?? 0} file(s), skipped ${r.skipped ?? 0}`,
      );
      if (!dryRun) {
        setSelected(new Set());
        void startScan();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCleaning(false);
    }
  };

  const isLibrary = path.trim().startsWith(MUSIC_ROOT);
  const scanScope = path.trim().startsWith(MUSIC_ROOT)
    ? 'Music Library'
    : path.trim().startsWith(DOWNLOADS_ROOT)
      ? 'Downloads'
      : 'Custom path';

  return (
    <div className="space-y-4">
      <CleanPanelHeader
        title="Duplicate Files"
        description={isLibrary
          ? 'Library duplicate scan compares imported files across different paths.'
          : 'Downloads duplicate scan finds files that already have a kept library copy.'}
        meta={scan ? (
          <>
            <span>Scope: {scanScope}</span>
            <span className="font-mono">{path.trim() || DEFAULT_PATH}</span>
            <span>{scan.scanned} scanned</span>
            <span>{done ? (scanFailed ? 'Job failed' : `${duplicates.length} duplicate(s)`) : `${scanKindLabel(scanKind)} running`}</span>
            <span>Scan {shortScanId(scanJid)}</span>
          </>
        ) : <span>No scan result loaded</span>}
        actions={(
          <>
          <Button
            color={isLibrary ? 'primary' : 'inherit'}
            size="small"
            variant={isLibrary ? 'contained' : 'outlined'}
            onClick={() => setPath(MUSIC_ROOT)}
          >
            Music Library
          </Button>
          <Button
            color={!isLibrary ? 'primary' : 'inherit'}
            size="small"
            variant={!isLibrary ? 'contained' : 'outlined'}
            onClick={() => setPath(DOWNLOADS_ROOT)}
          >
            Downloads
          </Button>
          </>
        )}
      />

      {/* Path input + scan */}
      <CleanActionBar>
        <TextField
          fullWidth
          label="Scan path"
          size="small"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void startScan()}
          slotProps={{ input: { style: { fontFamily: 'monospace', fontSize: '0.82rem' } } }}
        />
        <Button
          disabled={starting}
          variant="outlined"
          sx={{ whiteSpace: 'nowrap', minWidth: '5rem' }}
          onClick={() => void startScan()}
        >
          {starting ? 'Starting…' : 'Scan'}
        </Button>
      </CleanActionBar>

      {(error || scanError) && <Alert severity="error">{error || scanError}</Alert>}

      {!scan && (
      <CleanEmptyState
        title="No duplicate scan loaded"
        message="Choose a root and start a scan to populate duplicate candidates."
      />
      )}

      {/* Progress */}
      {scan && !done && (
        <ScanProgress scanned={scan.scanned} total={scan.total} />
      )}

      {scanJid && (
        <div className="space-y-2 rounded-md border border-graphite-800/90 bg-graphite-950/55 p-3">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-wrap items-center gap-2">
              <Chip color={scanStatusColor} label={scanStatusLabel} size="small" variant="outlined" />
              <Chip label={scanKindLabel(scanKind)} size="small" variant="outlined" />
              <Chip label={scanScope} size="small" variant="outlined" />
              <span className="font-mono text-xs text-zinc-500" title={scanJid}>{shortScanId(scanJid)}</span>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2 text-xs text-zinc-500">
              <span>
                {scan ? `${scan.scanned.toLocaleString()} / ${scan.total.toLocaleString()} scanned · ${scan.found.toLocaleString()} found` : 'Starting scan'}
              </span>
              <Button size="small" variant="text" onClick={() => navigate(jobsUrl(scanJid))}>
                Jobs
              </Button>
              <Button size="small" variant="text" onClick={() => setShowRawScanLog((value) => !value)}>
                {showRawScanLog ? 'Hide raw log' : 'Raw Log'}
              </Button>
            </div>
          </div>
          {showRawScanLog && scan?.log?.length ? (
            <LogViewer className="max-h-44 text-[0.7rem] leading-5 text-zinc-400" emptyText="No scan output yet." lines={scan.log} />
          ) : null}
        </div>
      )}

      {/* Summary */}
      {done && (
        <div className="space-y-3">
          <CleanMetricGrid
            items={[
              { label: 'Files scanned', value: scan.scanned, tone: 'info' },
              { label: 'Found', value: scan.found, tone: scan.found ? 'warning' : 'success' },
              { label: 'Duplicate files', value: duplicates.length, tone: duplicates.length ? 'warning' : 'success' },
              { label: 'Selected', value: selected.size, tone: selected.size ? 'danger' : 'neutral' },
            ]}
          />
          <CleanActionBar>
            <div className="min-w-0 flex-1 text-xs text-zinc-500">
              {scanFailed
                ? 'Scan job failed. Open Jobs for the full log.'
                : scanKind === 'ai'
                ? 'AI deep scan result is loaded.'
                : 'AI deep scan reviews unmatched files from this completed scan.'}
            </div>
            <Button disabled={!canRunAiReview} size="small" variant="outlined" onClick={() => void startAiReview()}>
              {scanKind === 'ai' ? 'AI scan loaded' : 'AI deep scan'}
            </Button>
          </CleanActionBar>
        </div>
      )}

      {/* Duplicate list */}
      {done && duplicates.length > 0 && (
        <div className="space-y-3">
          <CleanActionBar sticky>
            <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-400">
              <input
                checked={selected.size === duplicates.length}
                className="accent-red-500"
                type="checkbox"
                onChange={toggleAll}
              />
              Select all ({duplicates.length})
            </label>
            <div className="flex-1" />
            <div className="flex flex-wrap items-center justify-end gap-2">
              {selected.size === 0 && (
                <span className="text-xs text-zinc-500">Select duplicate rows to enable cleanup.</span>
              )}
                <Button
                  disabled={cleaning || selected.size === 0}
                  size="small"
                  variant="outlined"
                  onClick={() => void handleCleanup(true)}
                >
                  Dry run ({selected.size})
                </Button>
                <Button
                  color="error"
                  disabled={cleaning || selected.size === 0}
                  size="small"
                  variant="contained"
                  onClick={() => setCleanupConfirm(true)}
                >
                  {isLibrary ? `Delete from library (${selected.size})` : `Delete (${selected.size})`}
                </Button>
            </div>
          </CleanActionBar>

          {cleanupResult && (
            <Alert severity="info" onClose={() => setCleanupResult('')}>
              {cleanupResult}
            </Alert>
          )}

          <div className="overflow-hidden rounded-md border border-graphite-800/90 bg-graphite-950/60 shadow-sm shadow-black/20">
            {duplicates.map((dup) => (
              <DupRow
                key={dup.source_path}
                dup={dup}
                selected={selected.has(dup.source_path)}
                onToggle={() => {
                  setSelected((prev) => {
                    const next = new Set(prev);
                    next.has(dup.source_path) ? next.delete(dup.source_path) : next.add(dup.source_path);
                    return next;
                  });
                }}
              />
            ))}
          </div>
        </div>
      )}

      {done && duplicates.length === 0 && (
        <CleanEmptyState title="No duplicates found" tone="success" />
      )}

      {/* Confirm delete dialog */}
      <Dialog open={cleanupConfirm} onClose={() => setCleanupConfirm(false)} className="relative z-50">
        <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <DialogPanel className="w-full max-w-sm rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
            <DialogTitle className="text-base font-semibold text-zinc-100">
              Delete {selected.size} file{selected.size !== 1 ? 's' : ''}?
            </DialogTitle>
            <p className="mt-2 text-sm text-zinc-400">
              {isLibrary
                ? 'These files will be permanently deleted from the music library. The Beets DB entry for the kept copy remains. This cannot be undone.'
                : 'These files will be permanently deleted. The library copy is kept. This cannot be undone.'}
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outlined" size="small" onClick={() => setCleanupConfirm(false)}>
                Cancel
              </Button>
              <Button
                color="error"
                size="small"
                variant="contained"
                onClick={() => void handleCleanup(false)}
              >
                Delete
              </Button>
            </div>
          </DialogPanel>
        </div>
      </Dialog>
    </div>
  );
}
