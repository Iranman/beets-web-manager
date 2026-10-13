import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  getAcquisitionDownloadAllActive,
  getAcquisitionQueue,
  getYtdlpStatus,
  refreshAcquisitionQueue,
  runLidarrAlbumSearch,
  startAcquisitionDownloadAll,
  startAlbumDownload,
} from '../../api/client';
import type {
  AcquisitionDownloadAllLastJob,
  AcquisitionDownloadAllResult,
  AcquisitionQueueCache,
  AcquisitionQueueItem,
  AcquisitionQueueResponse,
  AcquisitionQueueSort,
  AcquisitionQueueSourceFilter,
  DownloadMethod,
  DownloadAlbumPayload,
  YtdlpStatusResponse,
} from '../../api/types';
import {
  anyDirectDownloadMethodEnabled,
  DIRECT_DOWNLOAD_METHODS,
  directDownloadMethodEnabled,
  directDownloadMethodTitle,
  downloadMethodLabel,
} from '../../lib/downloadMethods';
import { useJobPoll } from '../../lib/hooks';

export type SourceFilter = AcquisitionQueueSourceFilter;
type QueueAction = DownloadMethod | 'lidarr';
type BatchMethod = DownloadMethod;

export const PAGE_SIZE = 100;
export const REFRESH_POLL_MS = 3000;
const SEARCH_DEBOUNCE_MS = 300;

const SOURCE_OPTIONS: Array<{ value: SourceFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'beets', label: 'Missing' },
  { value: 'lidarr', label: 'Wanted' },
  { value: 'merged', label: 'In both' },
  { value: 'unmonitored', label: 'Unmonitored' },
];

const SORT_OPTIONS: Array<{ value: AcquisitionQueueSort; label: string }> = [
  { value: 'sort_key', label: 'Artist A-Z' },
  { value: 'year_desc', label: 'Newest first' },
];

/** "just now", "12 min ago", "3 h ago", "2 d ago". */
export function formatAge(seconds?: number | null) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function errorMessage(err: unknown) {
  return err instanceof Error ? err.message : String(err);
}

function errorCode(err: unknown) {
  return (err as { body?: { code?: string; error_code?: string } } | null)?.body?.code
    || (err as { body?: { error_code?: string } } | null)?.body?.error_code
    || '';
}

/** What the list status line says; `severity` picks the Alert colour. */
function cacheStatus(cache: AcquisitionQueueCache | undefined) {
  if (!cache) return null;
  const age = formatAge(cache.age_seconds);
  const updating = cache.refreshing ? ' Updating in the background...' : '';
  if (cache.state === 'building') {
    return { severity: 'info' as const, text: 'Building the Find Music list in the background. For a large library this can take a few minutes; the list appears here when it is ready.' };
  }
  if (cache.state === 'empty') {
    return { severity: 'info' as const, text: 'The Find Music list has not been built yet. Click Refresh list to build it.' };
  }
  if (cache.state === 'failed') {
    const errorAge = cache.last_error_at ? formatAge(Date.now() / 1000 - cache.last_error_at) : '';
    const reason = cache.last_error || 'unknown error';
    if (!cache.built_at) {
      return { severity: 'error' as const, text: `The Find Music list could not be built${errorAge ? ` (${errorAge})` : ''}: ${reason}. No earlier list exists, so nothing is shown. Check the Beets and Lidarr status on the System page, then click Refresh list.${updating}` };
    }
    return { severity: 'warning' as const, text: `The last list update failed${errorAge ? ` (${errorAge})` : ''}: ${reason}. The list below is the previous one, built ${age}, and may be out of date. Click Refresh list to try again.${updating}` };
  }
  if (cache.state === 'stale') {
    return { severity: 'warning' as const, text: `This list was built ${age} and may be out of date.${updating || ' Click Refresh list to update it.'}` };
  }
  return { severity: null, text: `List updated ${age}.${updating}` };
}

function lastLogLine(log?: string[]) {
  return (log ?? []).filter(Boolean).slice(-1)[0] ?? '';
}

function normaliseEpochSeconds(ts?: number | string | null) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value > 100000000000 ? value / 1000 : value;
}

function formatBatchTimestamp(ts?: number | string | null) {
  const seconds = normaliseEpochSeconds(ts);
  if (seconds === null) return '';
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function jobStatusColor(status?: string) {
  return status === 'success'
    ? 'success'
    : status === 'failed' || status === 'killed'
      ? 'error'
      : 'info';
}

function humanJobStatus(status?: string) {
  if (status === 'success') return 'Completed';
  if (status === 'failed' || status === 'killed') return 'Failed';
  if (status === 'cancelled') return 'Cancelled';
  if (status === 'running') return 'Running';
  return status || 'Working';
}

function batchOutcomeText(success: number, failed: number, skipped: number, total: number) {
  if (!total) return '';
  const parts = [`${success}/${total} downloaded`];
  if (failed) parts.push(`${failed} need attention`);
  if (skipped) parts.push(`${skipped} skipped`);
  return parts.join(', ');
}

function slskdActionLabel(ytdlpEnabled: boolean) {
  return ytdlpEnabled ? 'SLSKD + sources' : 'SLSKD';
}

function AcquisitionRow({
  item,
  onChanged,
  ytdlpEnabled,
  ytdlpMessage,
  ytdlpStatus,
}: {
  item: AcquisitionQueueItem;
  onChanged: () => void;
  ytdlpEnabled: boolean;
  ytdlpMessage: string;
  ytdlpStatus: YtdlpStatusResponse | null;
}) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [action, setAction] = useState<QueueAction | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const successHandledRef = useRef(false);
  const { job, error: jobError } = useJobPoll(jobId);

  const running = Boolean(jobId && (!job || job.status === 'running'));
  const latest = lastLogLine(job?.log);
  const local = item.local;
  const wanted = item.wanted;
  const mbid = item.mbid || local?.mb_albumid || wanted?.mb_albumid || '';
  const canDownload = Boolean(mbid && item.actions?.can_download);
  const canSearchLidarr = Boolean(wanted?.lidarr_id);
  const year = item.year ? String(item.year) : '';
  const primaryActionLabel = slskdActionLabel(ytdlpEnabled);

  useEffect(() => {
    if (job?.status !== 'success' || successHandledRef.current) return;
    successHandledRef.current = true;
    onChanged();
  }, [job?.status, onChanged]);

  async function runDownload(method: DownloadMethod) {
    // 0.5.1: auto_import is on, so a download ends in a Beets import -- ask first.
    if (!window.confirm(`Download and import ${item.artist} - ${item.album}? When the download finishes it is imported into the library, which can write tags and move files.`)) return;
    setAction(method);
    setError('');
    setNotice('');
    successHandledRef.current = false;
    try {
      const sourceFallback = method === 'slskd' && ytdlpEnabled;
      const payload: DownloadAlbumPayload = {
        artist: item.artist,
        albumartist: item.artist,
        album: item.album,
        year: item.year,
        track_count: local ? local.expected_track_count || local.track_count || undefined : undefined,
        mb_albumid: mbid,
        existing_album_id: local?.album_id,
        method,
        auto_import: true,
        try_ytdlp_fallback: sourceFallback,
        try_source_fallback: sourceFallback,
      };
      const started = await startAlbumDownload(payload);
      setJobId(started.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAction(null);
    }
  }

  async function runLidarrSearch() {
    if (!wanted?.lidarr_id) return;
    setAction('lidarr');
    setError('');
    setNotice('');
    try {
      const result = await runLidarrAlbumSearch(wanted.lidarr_id);
      setNotice(result.command_id ? `Lidarr search queued: command ${result.command_id}` : 'Lidarr search queued.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAction(null);
    }
  }

  return (
    <div className="border-t border-graphite-800 px-4 py-4 first:border-t-0">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div className="min-w-0 space-y-2">
          <div>
            <div className="whitespace-normal break-words text-base font-semibold leading-6 text-zinc-100">
              {item.artist} - {item.album}{year ? <span className="text-zinc-500"> ({year})</span> : null}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {!mbid ? <Chip color="error" label="No MB release ID" size="small" variant="filled" /> : null}
              {item.sources.includes('beets') ? <Chip color="info" label="Missing" size="small" variant="outlined" /> : null}
              {item.sources.includes('lidarr') ? <Chip color="secondary" label="Wanted" size="small" variant="outlined" /> : null}
              {wanted && !wanted.monitored ? <Chip color="warning" label="unmonitored" size="small" variant="outlined" /> : null}
              {local?.health ? <Chip color={local.health.color} label={local.health.label} size="small" variant="outlined" /> : null}
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-sm text-zinc-400">
              <span className={!mbid ? 'font-medium text-rose-300' : undefined}>
                {!mbid ? 'Download disabled until a MusicBrainz release ID is linked.' : item.issue || 'Needs acquisition'}
              </span>
              {local ? <span>{local.health?.imported ?? 0}/{local.health?.expected || local.expected_track_count || local.track_count || 0} local</span> : null}
            </div>
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-500">
            {mbid ? <span className="font-mono">{mbid}</span> : null}
            {local?.aldir ? <span className="max-w-3xl truncate font-mono">{local.aldir}</span> : null}
            {wanted?.mb_url ? (
              <a className="text-red-400 hover:text-red-300" href={wanted.mb_url} rel="noreferrer" target="_blank">
                MusicBrainz
              </a>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap gap-2">
          <Button
            disabled={running || action !== null || !canDownload}
            size="small"
            title={!mbid ? 'No MusicBrainz release ID is linked for this row' : undefined}
            variant="contained"
            onClick={() => void runDownload('slskd')}
          >
            {action === 'slskd' ? 'Starting...' : primaryActionLabel}
          </Button>
          {DIRECT_DOWNLOAD_METHODS.map((source) => (
            <Button
              key={source.method}
              disabled={running || action !== null || !canDownload || !directDownloadMethodEnabled(source.method, ytdlpStatus)}
              size="small"
              title={directDownloadMethodTitle(source.method, ytdlpStatus, ytdlpMessage)}
              variant="outlined"
              onClick={() => void runDownload(source.method)}
            >
              {action === source.method ? 'Starting...' : source.shortLabel}
            </Button>
          ))}
          {canSearchLidarr ? (
            <Button disabled={running || action !== null} size="small" variant="outlined" onClick={() => void runLidarrSearch()}>
              {action === 'lidarr' ? 'Queuing...' : 'Search Lidarr'}
            </Button>
          ) : null}
        </div>
      </div>

      {!anyDirectDownloadMethodEnabled(ytdlpStatus) ? <div className="mt-2 text-xs text-zinc-500">Fallback unavailable: {ytdlpMessage}</div> : null}
      {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
      {notice ? <Alert severity="info" sx={{ mt: 2 }} onClose={() => setNotice('')}>{notice}</Alert> : null}
      {jobId || jobError ? (
        <div className="mt-3 rounded border border-graphite-800 bg-graphite-950/70 p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {running ? <span className="inline-block size-2 animate-pulse rounded-full bg-sky-400" /> : null}
            <span className="font-medium text-zinc-200">
              {job?.status === 'success'
                ? 'Job complete'
                : job?.status === 'failed' || job?.status === 'killed'
                  ? 'Job failed'
                  : 'Job running'}
            </span>
            {job?.status ? (
              <Chip
                color={job.status === 'success' ? 'success' : job.status === 'failed' || job.status === 'killed' ? 'error' : 'info'}
                label={job.status}
                size="small"
                variant="outlined"
              />
            ) : null}
            {jobId ? <span className="font-mono text-xs text-zinc-500">{jobId}</span> : null}
          </div>
          {running ? <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} /> : null}
          {jobError ? <div className="mt-2 text-xs text-red-300">{jobError}</div> : null}
          {latest ? <div className="mt-2 text-xs text-zinc-400">{latest}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

export function AcquisitionPanel({
  initialSourceFilter = 'all',
  onSourceFilterChange,
}: {
  initialSourceFilter?: SourceFilter;
  onSourceFilterChange?: (filter: SourceFilter) => void;
}) {
  const navigate = useNavigate();
  const [queue, setQueue] = useState<AcquisitionQueueResponse | null>(null);
  const [ytdlpStatus, setYtdlpStatus] = useState<YtdlpStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [wantedError, setWantedError] = useState('');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>(initialSourceFilter);
  const [albumType, setAlbumType] = useState('');
  const [sort, setSort] = useState<AcquisitionQueueSort>('sort_key');
  const [offset, setOffset] = useState(0);
  const [showUnmonitored, setShowUnmonitored] = useState(false);
  const [refreshStarting, setRefreshStarting] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const requestRef = useRef(0);
  const searchRef = useRef('');
  const listTopRef = useRef<HTMLHeadingElement>(null);
  const [batchJobId, setBatchJobId] = useState<string | null>(null);
  const [batchStarting, setBatchStarting] = useState(false);
  const [batchError, setBatchError] = useState('');
  const [batchLimit, setBatchLimit] = useState('25');
  const [batchMethod, setBatchMethod] = useState<BatchMethod>('slskd');
  const [batchMissingFirst, setBatchMissingFirst] = useState(true);
  const [batchYtdlpFallback, setBatchYtdlpFallback] = useState(true);
  const [lastBatch, setLastBatch] = useState<AcquisitionDownloadAllLastJob | null>(null);
  const batchSuccessHandledRef = useRef(false);
  const { job: batchJob, error: batchJobError } = useJobPoll(batchJobId);

  useEffect(() => {
    setSourceFilter(initialSourceFilter);
    setOffset(0);
  }, [initialSourceFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const next = query.trim();
      if (next === searchRef.current) return;
      searchRef.current = next;
      setSearch(next);
      setOffset(0);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  // Reads one page of the server's cached list; never asks for a rebuild.
  const load = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    try {
      const next = await getAcquisitionQueue({
        offset,
        limit: PAGE_SIZE,
        search,
        source: sourceFilter,
        album_type: albumType ? [albumType] : undefined,
        sort,
        include_unmonitored: showUnmonitored,
      });
      if (requestId !== requestRef.current) return;
      setQueue(next);
      setError(next.library_error ? `Beets library could not be loaded: ${next.library_error}` : '');
      setWantedError(next.wanted_error || '');
    } catch (err) {
      if (requestId !== requestRef.current) return;
      setQueue(null);
      setWantedError('');
      setError(`The Find Music list could not be loaded: ${errorMessage(err)}. Nothing is shown. Reload the page to try again.`);
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [albumType, offset, search, showUnmonitored, sort, sourceFilter]);

  useEffect(() => { void load(); }, [load]);

  const cacheRefreshing = Boolean(queue?.cache?.refreshing);
  // While the server rebuilds the list, poll its status cheaply (limit=0) and reload the page once it is done.
  useEffect(() => {
    if (!cacheRefreshing) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void getAcquisitionQueue({ limit: 0, include_unmonitored: showUnmonitored })
        .then((status) => {
          if (cancelled) return;
          if (status.cache?.refreshing) {
            setQueue((current) => (current ? { ...current, cache: status.cache } : current));
          } else {
            void load();
          }
        })
        .catch(() => undefined); // ponytail: a failed poll just waits for the next tick
    }, REFRESH_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [cacheRefreshing, load, showUnmonitored]);

  // A refresh or filter change can shrink the list under the current page.
  const total = queue?.total ?? 0;
  useEffect(() => {
    if (queue && offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor((total - 1) / PAGE_SIZE) * PAGE_SIZE));
  }, [offset, queue, total]);

  const refreshList = useCallback(async () => {
    setRefreshStarting(true);
    setRefreshError('');
    try {
      await refreshAcquisitionQueue(showUnmonitored);
      await load();
    } catch (err) {
      setRefreshError(`The list refresh could not be started: ${errorMessage(err)}. The list shown is unchanged. Try again, or check the Jobs page.`);
    } finally {
      setRefreshStarting(false);
    }
  }, [load, showUnmonitored]);

  useEffect(() => {
    let cancelled = false;
    void getAcquisitionDownloadAllActive()
      .then((active) => {
        const activeJobId = active.active && active.job?.job_id ? active.job.job_id : '';
        if (!cancelled && activeJobId) {
          batchSuccessHandledRef.current = false;
          setBatchJobId((current) => current || activeJobId);
        } else if (!cancelled && active.last_job) {
          setLastBatch(active.last_job);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    void getYtdlpStatus()
      .then(setYtdlpStatus)
      .catch((err) => {
        setYtdlpStatus({
          ok: false,
          ready: false,
          enabled: false,
          cookie_file: '',
          cookie_candidates: [],
          message: err instanceof Error ? err.message : String(err),
        });
      });
  }, []);

  // The server filters and pages; this page is what is visible.
  const visibleItems = useMemo(() => queue?.items ?? [], [queue]);
  const cache = queue?.cache;
  const cacheState = cache?.state ?? 'ready';
  const hasList = Boolean(queue && (!cache || cache.built_at));
  const status = cacheStatus(cache);
  const filtersActive = Boolean(search || sourceFilter !== 'all' || albumType);
  const albumTypeOptions = Object.entries(queue?.album_types ?? {}).sort(([a], [b]) => a.localeCompare(b));
  if (albumType && !albumTypeOptions.some(([kind]) => kind === albumType)) albumTypeOptions.push([albumType, 0]);
  const sources = queue?.download_sources;
  const fallbackChain = (sources?.fallback_chain ?? []).map((method) => {
    const label = downloadMethodLabel(method as DownloadMethod);
    return sources?.fallbacks?.[method]?.available === false ? `${label} (unavailable)` : label;
  });
  const fallbackReasons = (sources?.fallback_chain ?? [])
    .map((method) => sources?.fallbacks?.[method]?.reason || '')
    .filter((reason, index) => reason && sources?.fallbacks?.[sources.fallback_chain[index]]?.available === false)
    .join('; ');
  const fallbackLabel = fallbackChain.length ? `Fallback: ${fallbackChain.join(' -> ')}` : 'Fallback: configured sources';

  const beetsCount = queue?.counts?.beets ?? 0;
  const lidarrCount = queue?.counts?.lidarr ?? 0;
  const mergedCount = queue?.counts?.merged ?? 0;
  const ytdlpEnabled = anyDirectDownloadMethodEnabled(ytdlpStatus);
  const ytdlpMessage = ytdlpStatus?.message || 'Checking yt-dlp status...';
  const batchYtdlpFallbackActive = batchMethod === 'slskd' && batchYtdlpFallback && ytdlpEnabled;
  const batchYtdlpFallbackBlocked = batchMethod === 'slskd' && batchYtdlpFallback && ytdlpStatus && !ytdlpEnabled;
  const batchRunning = Boolean(batchJobId && (!batchJob || batchJob.status === 'running'));
  const processableItems = useMemo(
    () => visibleItems.filter((item) => Boolean(item.mbid && item.actions?.can_download)),
    [visibleItems],
  );
  const orderedProcessableItems = useMemo(() => {
    if (!batchMissingFirst) return processableItems;
    return [...processableItems].sort((a, b) => {
      const aMissing = a.sources.includes('beets') ? 0 : 1;
      const bMissing = b.sources.includes('beets') ? 0 : 1;
      if (aMissing !== bMissing) return aMissing - bMissing;
      return (a.sort_key || '').localeCompare(b.sort_key || '');
    });
  }, [batchMissingFirst, processableItems]);
  const parsedBatchLimit = Math.max(1, Math.min(500, Number.parseInt(batchLimit, 10) || 25));
  const batchItems = orderedProcessableItems.slice(0, parsedBatchLimit);
  const batchHasDownloads = batchItems.some((item) => Boolean(item.actions?.can_download));
  const batchCanStart = Boolean(
    batchItems.length &&
    !batchStarting &&
    !batchRunning &&
    (
      batchMethod === 'slskd' ||
      directDownloadMethodEnabled(batchMethod, ytdlpStatus) ||
      !batchHasDownloads
    )
  );
  const currentBatchResult = (batchJob?.result as AcquisitionDownloadAllResult | undefined) ?? lastBatch?.result;
  const currentFailures = currentBatchResult?.failures ?? [];
  const batchLatest = lastLogLine(batchJob?.log) || lastLogLine(lastBatch?.log);
  const batchStatus = batchJob?.status || lastBatch?.status;
  const batchTime = formatBatchTimestamp(lastBatch?.finished_at ?? lastBatch?.created_at);
  const batchId = batchJobId || lastBatch?.job_id || '';
  const batchFailureCount = Math.max(0, Number(currentBatchResult?.failed ?? currentFailures.length ?? 0));
  const batchSuccessCount = Math.max(0, Number(currentBatchResult?.success ?? 0));
  const batchSkippedCount = Math.max(0, Number(currentBatchResult?.skipped ?? 0));
  const batchTotalCount = Math.max(0, Number(currentBatchResult?.total ?? 0));
  const batchOutcome = batchOutcomeText(batchSuccessCount, batchFailureCount, batchSkippedCount, batchTotalCount);
  const batchStatusLabel = humanJobStatus(batchStatus);
  const batchNeedsAttention = batchFailureCount > 0 || batchStatus === 'failed' || batchStatus === 'killed' || Boolean(batchError || batchJobError);
  const batchTitle = batchRunning
    ? 'Downloading albums'
    : batchNeedsAttention
      ? 'Previous batch needs attention'
      : 'Previous batch completed';
  const retryableFailedItems = useMemo(() => {
    if (!currentFailures.length || batchRunning) return [];
    const failedKeys = new Set(currentFailures.map((failure) => failure.key).filter(Boolean));
    return orderedProcessableItems.filter((item) => failedKeys.has(item.key) && item.actions?.can_download);
  }, [batchRunning, currentFailures, orderedProcessableItems]);
  const canRetryFailedItems = Boolean(retryableFailedItems.length && !batchStarting && !batchRunning);
  const primaryBatchLabel = retryableFailedItems.length
    ? `Retry ${retryableFailedItems.length} failed with ${slskdActionLabel(ytdlpEnabled)}`
    : batchItems.length
      ? `Download next ${batchItems.length}`
      : 'No downloadable rows';

  useEffect(() => {
    if (batchJob?.status !== 'success' || batchSuccessHandledRef.current) return;
    batchSuccessHandledRef.current = true;
    void refreshList();
  }, [batchJob?.status, refreshList]);

  useEffect(() => {
    if (!batchJob || batchJob.status === 'running') return;
    setLastBatch({
      job_id: batchJob.job_id || batchJobId || undefined,
      status: batchJob.status,
      log: batchJob.log,
      result: batchJob.result as AcquisitionDownloadAllResult | undefined,
    });
  }, [batchJob, batchJobId]);

  async function runDownloadAll(keysOverride?: string[], methodOverride?: BatchMethod) {
    const retryMode = Boolean(keysOverride?.length);
    const effectiveMethod = methodOverride ?? batchMethod;
    const selectedKeys = keysOverride?.length
      ? keysOverride
      : orderedProcessableItems.map((item) => item.key);
    const count = retryMode ? selectedKeys.length : batchItems.length;
    const methodAllowed = effectiveMethod === 'slskd' || directDownloadMethodEnabled(effectiveMethod, ytdlpStatus);
    if ((!retryMode && !batchCanStart) || (retryMode && !methodAllowed) || !count) return;
    const confirmed = window.confirm(
      `Start sequential ${downloadMethodLabel(effectiveMethod)} download for ${count} ${retryMode ? 'failed ' : ''}Acquire item(s)? Each finished download is imported into the library, which can write tags and move files.`,
    );
    if (!confirmed) return;
    setBatchStarting(true);
    setBatchError('');
    batchSuccessHandledRef.current = false;
    try {
      const useFallback = effectiveMethod === 'slskd' && batchYtdlpFallback && ytdlpEnabled;
      const started = await startAcquisitionDownloadAll({
        keys: selectedKeys,
        method: effectiveMethod,
        include_unmonitored: showUnmonitored,
        try_ytdlp_fallback: useFallback,
        try_source_fallback: useFallback,
        prioritize: batchMissingFirst ? 'beets_first' : 'queue',
        limit: retryMode ? count : parsedBatchLimit,
      });
      setBatchJobId(started.job_id);
    } catch (err) {
      if (errorCode(err) === 'queue_not_ready') {
        setBatchError('Nothing was started: the Find Music list is still building. Wait until the list status above says it is ready, then start the download again.');
        void load();
      } else {
        setBatchError(`The batch download could not be started: ${errorMessage(err)}. Nothing was downloaded. Try again, or check the Jobs page.`);
      }
    } finally {
      setBatchStarting(false);
    }
  }

  function selectSourceFilter(nextFilter: SourceFilter) {
    setSourceFilter(nextFilter);
    setOffset(0);
    onSourceFilterChange?.(nextFilter);
  }

  function goToPage(nextOffset: number) {
    setOffset(nextOffset);
    listTopRef.current?.focus();
  }

  const pageStart = visibleItems.length ? offset + 1 : 0;
  const pageEnd = offset + visibleItems.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h2 className="text-base font-medium text-zinc-200">Music downloads</h2>
          {hasList ? (
            <div className="mt-1 flex flex-wrap gap-2">
              <Chip label={`${beetsCount} missing music`} size="small" variant="outlined" />
              <Chip label={`${lidarrCount} wanted music`} size="small" variant="outlined" />
              {mergedCount ? <Chip color="info" label={`${mergedCount} merged`} size="small" variant="outlined" /> : null}
            </div>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 rounded border border-graphite-700 px-2 py-1.5 text-sm text-zinc-400">
            <span>Method</span>
            <select
              className="rounded border border-graphite-700 bg-graphite-950 px-2 py-1 text-sm text-zinc-200 outline-none focus:border-red-400"
              value={batchMethod}
              onChange={(event) => setBatchMethod(event.target.value as BatchMethod)}
            >
              <option value="slskd">SLSKD</option>
              {DIRECT_DOWNLOAD_METHODS.map((source) => (
                <option key={source.method} value={source.method}>{source.label}</option>
              ))}
            </select>
          </label>
          <TextField
            label="Limit"
            size="small"
            type="number"
            value={batchLimit}
            sx={{ width: '6rem' }}
            slotProps={{ htmlInput: { min: 1, max: 500 } }}
            onChange={(event) => setBatchLimit(event.target.value)}
          />
          <label className="flex cursor-pointer items-center gap-2 rounded border border-graphite-700 px-3 py-1.5 text-sm text-zinc-400">
            <input
              checked={batchMissingFirst}
              className="accent-red-500"
              type="checkbox"
              onChange={(event) => setBatchMissingFirst(event.target.checked)}
            />
            Missing first
          </label>
          <label className="flex cursor-pointer items-center gap-2 rounded border border-graphite-700 px-3 py-1.5 text-sm text-zinc-400">
            <input
              checked={batchYtdlpFallbackActive}
              className="accent-red-500"
              disabled={batchMethod !== 'slskd' || !ytdlpEnabled}
              type="checkbox"
              title={batchYtdlpFallbackBlocked ? ytdlpMessage : fallbackReasons || undefined}
              onChange={(event) => setBatchYtdlpFallback(event.target.checked)}
            />
            {batchYtdlpFallbackBlocked ? `Fallback unavailable: ${ytdlpMessage}` : fallbackLabel}
          </label>
          {batchRunning ? (
            <span className="rounded border border-sky-700/70 px-3 py-1.5 text-sm font-medium text-sky-200">
              Batch running
            </span>
          ) : retryableFailedItems.length ? (
            <Button
              disabled={loading || !canRetryFailedItems}
              size="small"
              title={`${retryableFailedItems.length} failed row(s) still present in the visible queue`}
              variant="contained"
              onClick={() => void runDownloadAll(retryableFailedItems.map((item) => item.key), 'slskd')}
            >
              {batchStarting ? 'Starting...' : primaryBatchLabel}
            </Button>
          ) : batchItems.length ? (
            <Button
              disabled={loading || !batchCanStart}
              size="small"
              title={`${processableItems.length} downloadable row(s) on this page`}
              variant="contained"
              onClick={() => void runDownloadAll()}
            >
              {batchStarting ? 'Starting...' : primaryBatchLabel}
            </Button>
          ) : (
            <span className="rounded border border-graphite-700 px-3 py-1.5 text-sm font-medium text-zinc-500">
              No downloadable rows
            </span>
          )}
          <Button
            disabled={refreshStarting || cacheRefreshing}
            size="small"
            title="Rebuild the list from Beets and Lidarr in the background"
            variant="outlined"
            onClick={() => void refreshList()}
          >
            {refreshStarting ? 'Starting...' : cacheRefreshing ? 'Updating...' : 'Refresh list'}
          </Button>
        </div>
      </div>

      {loading ? <LinearProgress aria-label="Loading the Find Music list" sx={{ borderRadius: 1 }} /> : null}
      {/* Polite live region: building / refreshing / age changes are announced. */}
      <div aria-live="polite" role="status">
        {status && status.severity ? (
          <Alert role="presentation" severity={status.severity}>{status.text}</Alert>
        ) : status ? (
          <p className="text-xs text-zinc-500">{status.text}</p>
        ) : null}
      </div>
      {error ? <Alert severity="error">{error}</Alert> : null}
      {refreshError ? <Alert severity="error" onClose={() => setRefreshError('')}>{refreshError}</Alert> : null}
      {batchError && !batchRunning ? <Alert severity="error" onClose={() => setBatchError('')}>{batchError}</Alert> : null}
      {sources && !sources.slskd_configured ? (
        <Alert severity="warning">
          SLSKD downloads cannot start: {sources.slskd_error || 'slskd is not configured'}. Set it in the container environment and recreate the container, or use a direct source button on a row.
        </Alert>
      ) : null}
      {batchYtdlpFallbackBlocked ? (
        <Alert severity="warning">
          Download All will run SLSKD only. Fallback unavailable: {ytdlpMessage}
        </Alert>
      ) : null}
      {wantedError ? (
        <Alert severity={wantedError.includes('not configured') ? 'warning' : 'error'}>
          {wantedError.includes('not configured')
            ? 'Lidarr API key is not configured. Set LIDARR_API_KEY and LIDARR_URL in the container environment.'
            : `Lidarr wanted list could not be loaded: ${wantedError}`}
        </Alert>
      ) : null}
      {batchRunning && (batchJobId || batchError || batchJobError) ? (
        <div className="rounded border border-sky-500/30 bg-sky-950/15 p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="inline-block size-2 animate-pulse rounded-full bg-sky-400" />
            <span className="font-medium text-zinc-200">{batchTitle}</span>
            {batchStatus ? <Chip color={jobStatusColor(batchStatus)} label={batchStatusLabel} size="small" variant="outlined" /> : null}
            {batchOutcome ? <span className="text-xs text-zinc-400">{batchOutcome}</span> : null}
            {currentBatchResult?.ytdlp_fallback_disabled ? (
              <Chip color="warning" label="Fallback disabled" size="small" variant="outlined" />
            ) : null}
            {batchId ? <Button size="small" variant="outlined" onClick={() => navigate('/jobs')}>View in Jobs</Button> : null}
          </div>
          <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} />
          {batchError ? <div className="mt-2 text-xs text-red-300">{batchError}</div> : null}
          {batchJobError ? <div className="mt-2 text-xs text-red-300">{batchJobError}</div> : null}
          {batchLatest ? <div className="mt-2 text-xs text-zinc-400">Current step: {batchLatest}</div> : null}
        </div>
      ) : lastBatch || batchJobError ? (
        <details className="rounded border border-graphite-800 bg-graphite-950/45">
          <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-sm text-zinc-300 hover:text-zinc-100">
            <span className="font-medium text-zinc-300">{batchTitle}</span>
            {batchStatus ? <Chip color={jobStatusColor(batchStatus)} label={batchStatusLabel} size="small" variant="outlined" /> : null}
            {batchOutcome ? <span className="text-xs text-zinc-500">{batchOutcome}</span> : null}
            {batchTime ? <span className="text-xs text-zinc-500">Finished {batchTime}</span> : null}
            {retryableFailedItems.length ? (
              <button
                className="rounded border border-rose-500/50 px-2 py-1 text-xs font-medium text-rose-200 hover:bg-rose-950/40"
                disabled={!canRetryFailedItems}
                type="button"
                onClick={(event) => {
                  event.preventDefault();
                  void runDownloadAll(retryableFailedItems.map((item) => item.key), 'slskd');
                }}
              >
                {`Retry ${retryableFailedItems.length} failed`}
              </button>
            ) : null}
            <span className="ml-auto text-xs text-zinc-500">Details</span>
          </summary>
          <div className="border-t border-graphite-800 px-3 py-2">
            {batchLatest ? <div className="mb-2 text-xs text-zinc-400">Last update: {batchLatest}</div> : null}
            {batchId ? <div className="mb-2 text-xs text-zinc-600">Job ID: <span className="font-mono">{batchId}</span></div> : null}
            {batchJobError ? <div className="text-xs text-red-300">{batchJobError}</div> : null}
            {currentFailures.length ? (
              <div className="space-y-1 text-xs text-zinc-400">
                <div className="font-semibold uppercase text-zinc-500">Failures</div>
                {currentFailures.map((failure) => (
                  <div key={`${failure.key}-${failure.artist}-${failure.album}`}>
                    <span className="text-zinc-300">{failure.artist} - {failure.album}</span>
                    {failure.error ? <span className="text-zinc-500">: {failure.error}</span> : null}
                  </div>
                ))}
              </div>
            ) : null}
            {(lastBatch?.log ?? []).length ? (
              <div className="mt-2 space-y-1 text-xs text-zinc-500">
                <div className="font-semibold uppercase text-zinc-600">Log excerpt</div>
                {(lastBatch?.log ?? []).filter(Boolean).slice(-6).map((line, index) => (
                  <div key={`${index}-${line}`} className="truncate" title={line}>{line}</div>
                ))}
              </div>
            ) : null}
            {!currentFailures.length && !(lastBatch?.log ?? []).length && !batchJobError ? (
              <div className="text-xs text-zinc-500">No detailed batch output recorded.</div>
            ) : null}
          </div>
        </details>
      ) : null}

      {hasList ? (
        <>
          <div className="flex flex-wrap gap-3">
            <TextField
              label="Search"
              placeholder="artist, album, year, or MBID"
              size="small"
              value={query}
              sx={{ minWidth: '18rem' }}
              onChange={(event) => setQuery(event.target.value)}
            />
            <div aria-label="Show rows from" className="flex overflow-hidden rounded border border-graphite-700" role="group">
              {SOURCE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  aria-pressed={sourceFilter === option.value}
                  className={[
                    'px-3 py-2 text-sm transition-colors',
                    sourceFilter === option.value
                      ? 'bg-red-500 text-white'
                      : 'bg-graphite-950 text-zinc-300 hover:bg-graphite-900',
                  ].join(' ')}
                  type="button"
                  onClick={() => selectSourceFilter(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 rounded border border-graphite-700 px-2 text-sm text-zinc-400">
              <span>Album type</span>
              <select
                className="rounded border border-graphite-700 bg-graphite-950 px-2 py-1 text-sm text-zinc-200 outline-none focus:border-red-400"
                value={albumType}
                onChange={(event) => { setAlbumType(event.target.value); setOffset(0); }}
              >
                <option value="">All types</option>
                {albumTypeOptions.map(([kind, count]) => (
                  <option key={kind} value={kind}>{`${kind} (${count})`}</option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 rounded border border-graphite-700 px-2 text-sm text-zinc-400">
              <span>Sort</span>
              <select
                className="rounded border border-graphite-700 bg-graphite-950 px-2 py-1 text-sm text-zinc-200 outline-none focus:border-red-400"
                value={sort}
                onChange={(event) => { setSort(event.target.value as AcquisitionQueueSort); setOffset(0); }}
              >
                {SORT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label className="flex cursor-pointer items-center gap-2 rounded border border-graphite-700 px-3 text-sm text-zinc-400">
              <input
                checked={showUnmonitored}
                className="accent-red-500"
                type="checkbox"
                onChange={(event) => { setShowUnmonitored(event.target.checked); setOffset(0); }}
              />
              Show unmonitored
            </label>
          </div>

          <h3 ref={listTopRef} className="text-sm text-zinc-400 outline-none" tabIndex={-1}>
            {total ? `Showing ${pageStart}-${pageEnd} of ${total}` : 'No rows'}
          </h3>

          {!visibleItems.length ? (
            filtersActive ? (
              <p className="text-sm text-zinc-500">No rows match these filters.</p>
            ) : cacheState === 'ready' || cacheState === 'stale' ? (
              <Alert severity="success">No missing library music or monitored Lidarr wanted albums need download.</Alert>
            ) : (
              <p className="text-sm text-zinc-500">The previous list had no rows. It may be out of date; see the status above.</p>
            )
          ) : (
            <div className="overflow-hidden rounded border border-graphite-800 bg-graphite-950">
              {visibleItems.map((item) => (
                <AcquisitionRow
                  key={item.key}
                  item={item}
                  onChanged={() => void refreshList()}
                  ytdlpEnabled={ytdlpEnabled}
                  ytdlpMessage={ytdlpMessage}
                  ytdlpStatus={ytdlpStatus}
                />
              ))}
            </div>
          )}

          {total > PAGE_SIZE ? (
            <nav aria-label="Find Music pages" className="flex flex-wrap items-center gap-2">
              <Button disabled={loading || offset === 0} size="small" variant="outlined" onClick={() => goToPage(Math.max(0, offset - PAGE_SIZE))}>
                Previous page
              </Button>
              <span className="text-sm text-zinc-400">
                Page {Math.floor(offset / PAGE_SIZE) + 1} of {Math.ceil(total / PAGE_SIZE)}
              </span>
              <Button disabled={loading || offset + PAGE_SIZE >= total} size="small" variant="outlined" onClick={() => goToPage(offset + PAGE_SIZE)}>
                Next page
              </Button>
            </nav>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
