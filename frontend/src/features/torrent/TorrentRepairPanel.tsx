import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import Switch from '@mui/material/Switch';
import TextField from '@mui/material/TextField';
import { useEffect, useMemo, useState } from 'react';
import { getJobResult, getQbitStatus, startQbitHardlinkRepair } from '../../api/client';
import type { QbitHardlinkRepairResult, QbitStatusResponse } from '../../api/types';
import { LogViewer } from '../../components/LogViewer';
import { useJobPoll } from '../../lib/hooks';

function metric(label: string, value: number, tone = 'text-zinc-100') {
  return (
    <div key={label} className="rounded border border-graphite-800 bg-graphite-950/70 px-3 py-3">
      <div className={`text-xl font-semibold tabular-nums ${tone}`}>{value.toLocaleString()}</div>
      <div className="mt-1 text-[0.68rem] uppercase tracking-wide text-zinc-500">{label}</div>
    </div>
  );
}

function actionText(action: Record<string, unknown>, key: string) {
  const value = action[key];
  return typeof value === 'string' ? value : value == null ? '' : String(value);
}

function actionDetail(action: Record<string, unknown>) {
  const status = actionText(action, 'status');
  if (status === 'size_mismatch') {
    const actual = Number(action.actual_size ?? 0);
    const expected = Number(action.expected_size ?? action.size ?? 0);
    const sizes = actual && expected ? ` (${actual.toLocaleString()} vs ${expected.toLocaleString()} bytes)` : '';
    return `${actionText(action, 'reason')}${sizes}`;
  }
  return actionText(action, 'target') || actionText(action, 'reason');
}

export function TorrentRepairPanel() {
  const [status, setStatus] = useState<QbitStatusResponse | null>(null);
  const [category, setCategory] = useState('music');
  const [filter, setFilter] = useState('errored');
  const [search, setSearch] = useState('');
  const [limit, setLimit] = useState('0');
  const [recheck, setRecheck] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<QbitHardlinkRepairResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { job, error: pollError } = useJobPoll(jobId);

  useEffect(() => {
    let active = true;
    getQbitStatus()
      .then((next) => {
        if (!active) return;
        setStatus(next);
        setCategory(next.category || 'music');
        setFilter(next.filter || 'errored');
      })
      .catch((err) => active && setError(err instanceof Error ? err.message : String(err)));
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!job || job.status === 'running') return;
    if (job.status === 'success') {
      setResult(getJobResult<QbitHardlinkRepairResult>(job));
    } else {
      setError(`Hardlink repair finished with status ${job.status}.`);
    }
    setJobId(null);
  }, [job]);

  const busy = loading || Boolean(jobId);
  const summary = useMemo(() => result, [result]);

  async function run(dryRun: boolean) {
    setLoading(true);
    setError('');
    if (dryRun) setResult(null);
    try {
      const started = await startQbitHardlinkRepair({
        dryRun,
        category: category.trim(),
        filter: filter.trim(),
        search: search.trim(),
        limit: Number(limit) || 0,
        recheck,
      });
      if (started.result) {
        setResult(started.result);
        setJobId(null);
      } else if (started.job_id) {
        setJobId(started.job_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  const configured = Boolean(status?.configured);
  const canApply = Boolean(summary?.dry_run && summary.would_link > 0 && configured);

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-4">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-start">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold text-zinc-100">qBittorrent Hardlinks</h2>
                <Chip
                  color={configured ? 'success' : 'warning'}
                  label={configured ? 'Configured' : 'Not configured'}
                  size="small"
                  variant="outlined"
                />
                {status?.username_configured ? <Chip label="Auth" size="small" variant="outlined" /> : null}
              </div>
              <div className="mt-2 truncate font-mono text-xs text-zinc-500">
                {status?.url || 'QBITTORRENT_URL is not set'}
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-[minmax(9rem,12rem)_minmax(9rem,12rem)_minmax(12rem,1fr)_minmax(8rem,10rem)_auto]">
              <TextField
                label="Category"
                size="small"
                value={category}
                onChange={(event) => setCategory(event.target.value)}
              />
              <TextField
                label="qBit filter"
                size="small"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              />
              <TextField
                label="Torrent search"
                size="small"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && configured && !busy && void run(true)}
              />
              <TextField
                label="Limit"
                size="small"
                value={limit}
                onChange={(event) => setLimit(event.target.value.replace(/[^\d]/g, ''))}
              />
              <label className="flex items-center gap-1 text-sm text-zinc-400">
                <Switch checked={recheck} size="small" onChange={(event) => setRecheck(event.target.checked)} />
                Recheck
              </label>
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            <Chip label={`Roots: ${(status?.repair_allowed_roots ?? []).join(', ') || 'none'}`} size="small" variant="outlined" />
            <Chip label={`Aliases: ${status?.path_aliases || 'none'}`} size="small" variant="outlined" />
          </div>

          <div className="flex flex-wrap gap-2">
            <Button disabled={!configured || busy} variant="outlined" onClick={() => void run(true)}>
              {jobId ? 'Running...' : 'Dry Run'}
            </Button>
            <Button disabled={!canApply || busy} color="primary" variant="contained" onClick={() => void run(false)}>
              Create Hardlinks
            </Button>
          </div>
        </CardContent>
        {busy ? <LinearProgress /> : null}
      </Card>

      {error || pollError ? <Alert severity="error">{error || pollError}</Alert> : null}
      {!configured ? (
        <Alert severity="warning">
          qBittorrent URL is missing from the Beets container environment.
        </Alert>
      ) : null}

      {summary ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-7">
            {metric('Checked', summary.checked, 'text-zinc-100')}
            {metric(summary.dry_run ? 'Would Link' : 'Linked', summary.dry_run ? summary.would_link : summary.linked, 'text-emerald-300')}
            {metric('Already There', summary.already_present, 'text-sky-300')}
            {metric('Size Mismatch', summary.size_mismatch ?? 0, summary.size_mismatch ? 'text-orange-300' : 'text-zinc-100')}
            {metric('Skipped', summary.skipped, summary.skipped ? 'text-amber-300' : 'text-zinc-100')}
            {metric('Errors', summary.errors, summary.errors ? 'text-red-300' : 'text-zinc-100')}
            {metric('Rechecked', summary.rechecked_hashes ?? 0, 'text-red-300')}
          </div>

          {summary.actions?.length ? (
            <div className="overflow-hidden rounded border border-graphite-800">
              <div className="grid grid-cols-[8rem_5rem_minmax(8rem,0.8fr)_minmax(0,1.1fr)_minmax(0,1.1fr)_minmax(0,1.1fr)] gap-3 bg-graphite-950 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                <span>Status</span>
                <span>Prog</span>
                <span>Torrent</span>
                <span>File</span>
                <span>Source</span>
                <span>Target / Reason</span>
              </div>
              <div className="max-h-80 overflow-y-auto">
                {summary.actions.slice(0, 80).map((action, index) => (
                  <div key={`${actionText(action, 'torrent')}-${actionText(action, 'file')}-${index}`} className="grid grid-cols-[8rem_5rem_minmax(8rem,0.8fr)_minmax(0,1.1fr)_minmax(0,1.1fr)_minmax(0,1.1fr)] gap-3 border-t border-graphite-800 px-3 py-2 text-xs">
                    <span className="font-semibold text-zinc-300">{actionText(action, 'status')}</span>
                    <span className="tabular-nums text-zinc-500">{Math.round(Number(action.progress ?? 0) * 100)}%</span>
                    <span className="truncate text-zinc-400">{actionText(action, 'torrent')}</span>
                    <span className="truncate font-mono text-zinc-400">{actionText(action, 'file')}</span>
                    <span className="truncate font-mono text-zinc-400">{actionText(action, 'source') || '-'}</span>
                    <span className="truncate font-mono text-zinc-400">{actionDetail(action)}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <Alert severity="info">
              No qBittorrent file rows were returned for category "{summary.category || 'all'}", filter "{summary.filter || 'all'}"{summary.search ? `, search "${summary.search}"` : ''}.
            </Alert>
          )}
        </div>
      ) : null}

      {job ? <LogViewer lines={job.log ?? []} emptyText="Waiting for qBittorrent repair log..." /> : null}
    </div>
  );
}
