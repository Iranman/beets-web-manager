// Workbench > Playlist Downloads: the one home for getting a playlist's tracks
// into Beets. Pick a saved playlist, then run or resume its pipeline, download
// missing tracks, import downloaded ones, retry single tracks, or download from a
// link or track list. Making playlists and syncing them to Plex stay on Playlists.
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router';
import {
  applyPlaylistTrackAction,
  getJob,
  getPlaylistDetails,
  getPlaylistDownloadStatus,
  getPlaylistRows,
  getPlaylists,
  runPlaylistPipelineAction,
  startPlaylistDownload,
} from '../../api/client';
import type {
  JobResponse,
  PlaylistCreateResponse,
  PlaylistDetailResponse,
  PlaylistDownloadStatusResponse,
  PlaylistEntry,
  PlaylistSource,
  PlaylistTrack,
} from '../../api/types';
import { LogViewer } from '../../components/LogViewer';
import { useEngineOffline } from '../../components/EngineOfflineBanner';
import { formatShortDateTime } from '../../lib/dates';
import { errorMessage } from '../../lib/errors';
import {
  ActionMenu,
  canonicalNote,
  isFailedReviewTrack,
  isWaitingImportTrack,
  matchedAsTracks,
  playlistHasResumableCheckpoint,
  playlistPollDelay,
  playlistStatusMessage,
  playlistStatusSeverity,
  statusLabel,
  trackKey,
  type MenuAction,
  type Notice,
  type PlaylistPipelineAction,
} from './playlistShared';

const PLAYLIST_ROW_PAGE_SIZE = 100;
const PLAYLIST_DOWNLOAD_POLL_MS = 3_000;
const PLAYLIST_DOWNLOAD_RETRY_POLL_MS = 6_000;
const PLAYLIST_PIPELINE_JOB_POLL_MS = 5_000;

const PLAYLIST_JOB_STORAGE_KEY = 'beets-playlist-download-job-id';
const PLAYLIST_LAST_JOB_STORAGE_KEY = 'beets-playlist-download-last-job-id';

const LINK_SOURCE_OPTIONS: Array<{ value: PlaylistSource; label: string }> = [
  { value: 'url', label: 'Playlist URL' },
  { value: 'text', label: 'Track list' },
];

const DOWNLOAD_METHOD_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'slskd', label: 'SLSKD' },
  { value: 'spotiflac', label: 'SpotiFLAC' },
  { value: 'ytdlp', label: 'YouTube' },
  { value: 'soundcloud', label: 'SoundCloud' },
];

type FetchGroup = 'missing' | 'waiting' | 'failed';
type FetchTrackAction = 'delete_staged' | 'retry_download' | 'retry_import';

const FETCH_GROUPS: Array<{ id: FetchGroup; label: string }> = [
  { id: 'missing', label: 'Missing' },
  { id: 'waiting', label: 'Waiting Import' },
  { id: 'failed', label: 'Failed' },
];

function isPendingPlexTrack(track: PlaylistTrack): boolean {
  return track.pipeline_status === 'plex_pending' || track.retry_action === 'retry_pending_plex_match';
}

function rowsForGroup(group: FetchGroup, rows: PlaylistTrack[]): PlaylistTrack[] {
  if (group === 'waiting') return rows.filter(isWaitingImportTrack);
  if (group === 'failed') return rows.filter(isFailedReviewTrack);
  return rows.filter((track) => !isWaitingImportTrack(track) && !isFailedReviewTrack(track) && !isPendingPlexTrack(track));
}

function statusTone(status: string): string {
  if (status === 'matched' || status === 'downloaded' || status === 'waiting_import') return 'text-success';
  if (status === 'searching' || status === 'importing' || status === 'queued') return 'text-info';
  if (status === 'failed' || status === 'source_failed') return 'text-danger-text';
  return 'text-zinc-300';
}

function metric(label: string, value: number | string, tone = 'text-zinc-100') {
  return (
    <div className="min-w-0 border-t border-graphite-800 pt-1.5">
      <div className={`text-lg font-semibold tabular-nums ${tone}`}>{value}</div>
      <div className="mt-0.5 text-xs text-muted">{label}</div>
    </div>
  );
}

function DownloadConfirmDialog({
  open,
  missing,
  total,
  needsPull,
  busy,
  onClose,
  onConfirm,
}: {
  open: boolean;
  missing: number;
  total: number;
  needsPull: boolean;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onClose={busy ? () => undefined : onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-md rounded-md border border-graphite-700 bg-graphite-900 p-5">
          <DialogTitle className="text-base font-semibold text-zinc-100">
            {needsPull ? 'Download missing tracks and sync?' : missing ? 'Download missing tracks and sync?' : 'Sync playlist to Plex?'}
          </DialogTitle>
          <p className="mt-2 text-sm text-zinc-400">
            {needsPull
              ? 'The backend will read the playlist, match it against Beets, download missing tracks, import them, then rebuild the playlist from library matches.'
              : missing
                ? `${missing} missing track(s) will be searched/downloaded, imported into Beets, then the playlist will be rebuilt from library matches.`
                : `${total} library-matched track(s) will be written to M3U and synced to Plex when Plex is configured.`}
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outlined" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button variant="contained" color="primary" onClick={onConfirm} disabled={busy}>
              {busy ? 'Starting...' : (needsPull || missing) ? 'Download Missing & Sync' : 'Sync'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function PlaylistJobProgress({
  downloadState,
  downloadError,
  detail,
  onClear,
  onRetryMissing,
  canRetryMissing,
}: {
  downloadState: PlaylistDownloadStatusResponse | null;
  downloadError: string;
  detail: string;
  onClear: () => void;
  onRetryMissing?: () => void;
  canRetryMissing?: boolean;
}) {
  if (!downloadState && !downloadError) return null;
  const trackStatuses = downloadState?.track_status_list ?? [];
  const statusCounts = trackStatuses.reduce<Record<string, number>>((counts, row) => {
    counts[row.status] = (counts[row.status] ?? 0) + 1;
    return counts;
  }, {});
  const available = downloadState?.matched_after_import
    ?? downloadState?.matched?.length
    ?? downloadState?.matched_initial
    ?? statusCounts.matched
    ?? 0;
  const missing = downloadState?.missing_after_import
    ?? downloadState?.missing?.length
    ?? downloadState?.missing_initial
    ?? statusCounts.missing
    ?? 0;
  const downloaded = (statusCounts.downloaded ?? 0) + (statusCounts.waiting_import ?? 0);
  const failed = (statusCounts.failed ?? 0) + (statusCounts.source_failed ?? 0);
  const total = downloadState?.tracks?.length || (available + missing) || downloadState?.total || trackStatuses.length;
  const percent = total ? Math.max(0, Math.min(100, Math.round((available / total) * 100))) : 0;
  const active = downloadState?.status === 'running';
  const methods = downloadState?.download_methods?.length ? downloadState.download_methods.join(', ') : 'auto';
  const orderedStatuses = ['matched', 'downloaded', 'waiting_import', 'importing', 'searching', 'queued', 'missing', 'failed', 'source_failed'];
  const statusSummary = orderedStatuses
    .filter((status) => statusCounts[status])
    .map((status) => `${statusLabel(status)} ${statusCounts[status]}`);
  const doneSeverity = playlistStatusSeverity(downloadState?.playlist);
  const doneLabel = doneSeverity === 'error'
    ? 'Playlist saved; Plex sync failed'
    : doneSeverity === 'warning'
      ? 'Playlist saved; Plex sync partially completed'
      : doneSeverity === 'info'
        ? 'Playlist saved; Plex not configured'
        : 'Playlist sync complete';
  const doneNoticeTone = doneSeverity === 'error'
    ? 'text-danger-text'
    : doneSeverity === 'warning'
      ? 'text-warning'
      : doneSeverity === 'info'
        ? 'text-info'
        : 'text-success';

  return (
    <div className="border-y border-graphite-800 py-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium text-zinc-200">
          {downloadState?.status === 'done'
            ? doneLabel
            : downloadState?.status === 'error'
              ? 'Playlist job failed'
              : downloadState?.current || 'Playlist job running'}
        </div>
        <div className="text-xs text-muted">
          {detail}
        </div>
      </div>
      {downloadError ? <Alert severity="error" sx={{ mb: 2 }}>{downloadError}</Alert> : null}
      {downloadState ? (
        <div className="mb-3 space-y-2">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
            {metric('Available', available, 'text-success')}
            {metric('Missing', missing, missing ? 'text-danger-text' : 'text-zinc-100')}
            {metric('Waiting Import', downloaded, downloaded ? 'text-info' : 'text-zinc-100')}
            {metric('Failed', failed, failed ? 'text-danger-text' : 'text-zinc-100')}
            {metric('Round', downloadState.round ? `${downloadState.round}/${downloadState.max_rounds || '?'}` : '-', 'text-brass-300')}
            {metric('Sources', methods, 'text-zinc-300')}
          </div>
          <LinearProgress
            value={percent}
            variant={total ? 'determinate' : 'indeterminate'}
            sx={{ borderRadius: 1 }}
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap gap-1.5">
              {downloadState.resumed ? <Chip label="resumed" size="small" variant="outlined" /> : null}
              {statusSummary.map((label) => (
                <Chip key={label} label={label} size="small" variant="outlined" />
              ))}
            </div>
            {!active ? (
              <div className="flex flex-wrap gap-2">
                {canRetryMissing && onRetryMissing ? (
                  <Button size="small" variant="contained" onClick={onRetryMissing}>
                    Continue Missing
                  </Button>
                ) : null}
                <Button size="small" variant="outlined" onClick={onClear}>
                  Clear
                </Button>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {trackStatuses.length ? (
        <div className="mb-3 max-h-52 overflow-auto border-y border-graphite-800">
          <div className="grid grid-cols-[minmax(0,1fr)_8rem_8rem] border-b border-graphite-800 px-3 py-2 text-xs font-semibold text-muted">
            <span>Track</span>
            <span>Source</span>
            <span>Status</span>
          </div>
          {trackStatuses.map((row) => (
            <div key={row.id} className="grid grid-cols-[minmax(0,1fr)_8rem_8rem] items-start gap-2 border-t border-graphite-800 px-3 py-2 text-xs first:border-t-0">
              <div className="min-w-0">
                <div className="truncate text-zinc-200">{[row.artist, row.title].filter(Boolean).join(' - ')}</div>
                {canonicalNote(row) ? <div className="truncate text-info">{canonicalNote(row)}</div> : null}
                {row.message ? <div className="truncate text-muted">{row.message}</div> : null}
              </div>
              <span className="truncate text-zinc-400">{row.method || '-'}</span>
              <span className={`truncate ${statusTone(row.status)}`}>
                {statusLabel(row.status)}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="text-xs text-muted">
        Live log output is shown in the top pipeline bar. Open Jobs for the full persisted job log.
      </div>
      {downloadState?.status === 'done' && playlistStatusMessage(downloadState.playlist) ? (
        <div className={`mt-3 border-t border-graphite-800 pt-2 text-sm ${doneNoticeTone}`}>
          {playlistStatusMessage(downloadState.playlist)}
          {downloadState.missing_after_import ? ` · ${downloadState.missing_after_import} still missing` : ''}
        </div>
      ) : null}
    </div>
  );
}


export function PlaylistPipelinePanel({
  initialPlaylist = '',
  onJobStarted,
}: {
  /** A ?playlist= deep link from Playlists: select that playlist. */
  initialPlaylist?: string;
  onJobStarted?: () => void;
}) {
  // Pipeline runs need the Beets engine; Run/Resume is disabled while it is down.
  const engineOffline = useEngineOffline();
  const [playlists, setPlaylists] = useState<PlaylistEntry[]>([]);
  const [loadingPlaylists, setLoadingPlaylists] = useState(true);
  const [playlistError, setPlaylistError] = useState('');
  const [selectedName, setSelectedName] = useState(initialPlaylist);
  const [detail, setDetail] = useState<PlaylistDetailResponse | null>(null);
  const [detailError, setDetailError] = useState('');
  const [notice, setNotice] = useState<Notice | null>(null);

  const [downloadJobId, setDownloadJobId] = useState(() => localStorage.getItem(PLAYLIST_JOB_STORAGE_KEY) ?? '');
  const [lastDownloadJobId, setLastDownloadJobId] = useState(() => localStorage.getItem(PLAYLIST_LAST_JOB_STORAGE_KEY) ?? '');
  const [downloadState, setDownloadState] = useState<PlaylistDownloadStatusResponse | null>(null);
  const [downloadError, setDownloadError] = useState('');
  const [startingDownload, setStartingDownload] = useState(false);
  const [confirmDownload, setConfirmDownload] = useState(false);
  const [downloadMethod, setDownloadMethod] = useState('auto');
  const [linkName, setLinkName] = useState('');
  const [linkSource, setLinkSource] = useState<PlaylistSource>('url');
  const [linkContent, setLinkContent] = useState('');

  const [pipelineJobId, setPipelineJobId] = useState('');
  const [pipelineJob, setPipelineJob] = useState<JobResponse | null>(null);
  const [pipelineActionBusy, setPipelineActionBusy] = useState('');
  const [logExpanded, setLogExpanded] = useState(false);

  const [fetchGroup, setFetchGroup] = useState<FetchGroup>('missing');
  const [groupRows, setGroupRows] = useState<Partial<Record<FetchGroup, PlaylistTrack[]>>>({});
  const [groupHasMore, setGroupHasMore] = useState<Partial<Record<FetchGroup, boolean>>>({});
  const [loadingRows, setLoadingRows] = useState<FetchGroup | ''>('');
  const [trackActionKey, setTrackActionKey] = useState('');

  const selectedPlaylist = playlists.find((playlist) => playlist.name === selectedName);
  // With nothing picked yet, an interrupted playlist is the one to resume.
  const fallbackResumablePlaylist = useMemo(
    () => playlists.find((playlist) => playlistHasResumableCheckpoint(null, playlist)),
    [playlists],
  );
  const activePlaylist = selectedPlaylist || (!selectedName ? fallbackResumablePlaylist : undefined);
  const activeName = activePlaylist?.name || selectedName;
  const counts = detail?.counts ?? {};

  const loadPlaylists = useCallback(async () => {
    setLoadingPlaylists(true);
    setPlaylistError('');
    try {
      const response = await getPlaylists();
      setPlaylists(response.playlists ?? []);
    } catch (err) {
      setPlaylistError(errorMessage(err));
    } finally {
      setLoadingPlaylists(false);
    }
  }, []);

  const loadDetail = useCallback(async (name: string) => {
    if (!name) {
      setDetail(null);
      return;
    }
    setDetailError('');
    try {
      const result = await getPlaylistDetails(name, { mode: 'summary' });
      setDetail(result);
      const activeJobId = result.last_pipeline?.jobs_job_id || '';
      if (activeJobId && result.last_pipeline?.status === 'running') setPipelineJobId(activeJobId);
    } catch (err) {
      setDetailError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void loadPlaylists();
  }, [loadPlaylists]);

  useEffect(() => {
    setGroupRows({});
    setGroupHasMore({});
    void loadDetail(activeName);
  }, [activeName, loadDetail]);

  const clearDownloadPanel = useCallback(() => {
    setDownloadJobId('');
    setLastDownloadJobId('');
    setDownloadState(null);
    setDownloadError('');
    localStorage.removeItem(PLAYLIST_JOB_STORAGE_KEY);
    localStorage.removeItem(PLAYLIST_LAST_JOB_STORAGE_KEY);
  }, []);

  const followDownloadJob = (jobId: string) => {
    setDownloadJobId(jobId);
    setLastDownloadJobId(jobId);
    localStorage.setItem(PLAYLIST_JOB_STORAGE_KEY, jobId);
    localStorage.setItem(PLAYLIST_LAST_JOB_STORAGE_KEY, jobId);
  };

  // Show the last finished download job again after a reload, or rejoin it if it still runs.
  useEffect(() => {
    if (downloadJobId || !lastDownloadJobId || downloadState || downloadError) return undefined;
    let cancelled = false;
    getPlaylistDownloadStatus(lastDownloadJobId)
      .then((state) => {
        if (cancelled) return;
        if (state.status === 'running') {
          localStorage.setItem(PLAYLIST_JOB_STORAGE_KEY, lastDownloadJobId);
          setDownloadJobId(lastDownloadJobId);
          return;
        }
        setDownloadState(state);
      })
      .catch(() => {
        if (cancelled) return;
        localStorage.removeItem(PLAYLIST_LAST_JOB_STORAGE_KEY);
        setLastDownloadJobId('');
      });
    return () => {
      cancelled = true;
    };
  }, [downloadError, downloadJobId, downloadState, lastDownloadJobId]);

  useEffect(() => {
    if (!downloadJobId) return undefined;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const state = await getPlaylistDownloadStatus(downloadJobId);
        if (cancelled) return;
        setDownloadState(state);
        setDownloadError('');
        if (state.status === 'done' || state.status === 'error') {
          setDownloadJobId('');
          setLastDownloadJobId(downloadJobId);
          localStorage.removeItem(PLAYLIST_JOB_STORAGE_KEY);
          localStorage.setItem(PLAYLIST_LAST_JOB_STORAGE_KEY, downloadJobId);
          if (state.status === 'done') {
            setNotice({
              severity: playlistStatusSeverity(state.playlist),
              message: playlistStatusMessage(state.playlist) || 'Playlist files saved.',
            });
            void loadPlaylists();
          } else {
            setDownloadError(state.interrupted
              ? 'Previous playlist job was interrupted. Use Resume Pipeline to continue from the checkpoint.'
              : 'Playlist import/sync job failed.');
          }
          return;
        }
        timer = window.setTimeout(poll, playlistPollDelay(PLAYLIST_DOWNLOAD_POLL_MS));
      } catch (err) {
        if (cancelled) return;
        setDownloadError(errorMessage(err));
        if (err instanceof Error && err.message.toLowerCase().includes('job not found')) {
          setDownloadJobId('');
          localStorage.removeItem(PLAYLIST_JOB_STORAGE_KEY);
          if (lastDownloadJobId === downloadJobId) {
            setLastDownloadJobId('');
            localStorage.removeItem(PLAYLIST_LAST_JOB_STORAGE_KEY);
          }
          return;
        }
        timer = window.setTimeout(poll, playlistPollDelay(PLAYLIST_DOWNLOAD_RETRY_POLL_MS));
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [downloadJobId, lastDownloadJobId, loadPlaylists]);

  useEffect(() => {
    if (!pipelineJobId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const job = await getJob(pipelineJobId);
        if (cancelled) return;
        setPipelineJob(job);
        if (job.status === 'running') {
          timer = window.setTimeout(poll, playlistPollDelay(PLAYLIST_PIPELINE_JOB_POLL_MS));
          return;
        }
        if (activeName) await loadDetail(activeName);
        await loadPlaylists();
        if (!cancelled) {
          const jobResult = job.result as { plex?: PlaylistCreateResponse['plex']; playlist?: PlaylistCreateResponse } | undefined;
          const plex = jobResult?.plex ?? jobResult?.playlist?.plex;
          const plexFailed = Boolean(plex?.error || plex?.status === 'failed');
          const plexPartial = Boolean(plex?.status === 'partial_success' || plex?.status === 'partial' || (plex?.pending_plex_count ?? plex?.tracks_unmatched ?? 0) > 0);
          setNotice({
            severity: job.status !== 'success'
              ? 'error'
              : plexFailed
                ? 'error'
                : plexPartial
                  ? 'warning'
                  : 'success',
            message: job.status !== 'success'
              ? 'Playlist pipeline action failed. The reason is shown in the job log.'
              : plexFailed
                ? `Playlist files saved; Plex sync failed: ${plex?.error || plex?.issue_reason || 'not updated'}`
                : plexPartial
                  ? `Plex sync partially completed: ${plex?.tracks_matched ?? 0} of ${plex?.tracks_requested ?? 0} tracks added; ${plex?.pending_plex_count ?? plex?.tracks_unmatched ?? 0} pending Plex match(es).`
                  : 'Playlist pipeline action completed.',
          });
        }
      } catch (err) {
        if (!cancelled) setDetailError(errorMessage(err));
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [pipelineJobId, activeName, loadDetail, loadPlaylists]);

  const downloadRunning = Boolean(downloadJobId) || startingDownload;
  const pipelineRunning = downloadRunning || pipelineJob?.status === 'running' || Boolean(pipelineActionBusy);
  const pipelineStatus = downloadState?.status
    || pipelineJob?.status
    || detail?.last_pipeline?.status
    || activePlaylist?.last_pipeline?.status
    || 'idle';
  const pipelineAction = downloadState?.phase
    || detail?.last_pipeline?.action
    || activePlaylist?.last_pipeline?.action
    || 'none';
  const pipelineIsActive = pipelineStatus === 'running' || pipelineRunning;
  const hasResumablePipeline = playlistHasResumableCheckpoint(detail, activePlaylist);
  const mainPipelineAction: PlaylistPipelineAction = hasResumablePipeline ? 'resume' : 'run-full';
  const mainPipelineLabel = hasResumablePipeline ? 'Resume Pipeline' : 'Run Pipeline';
  const pipelineFailed = pipelineStatus === 'failed' || pipelineStatus === 'error';
  const matchedCount = detail?.available ?? activePlaylist?.available ?? 0;
  const missingCount = detail?.missing_count ?? activePlaylist?.missing ?? 0;
  const waitingCount = ((counts.downloaded ?? 0) + (counts.waiting_import ?? 0))
    || detail?.checkpoint_waiting_for_import
    || activePlaylist?.checkpoint_waiting_for_import
    || activePlaylist?.downloaded
    || 0;
  const failedCount = (counts.failed ?? activePlaylist?.failed ?? 0) + (counts.review_required ?? activePlaylist?.review_required ?? 0);
  const lastCheckpointAt = detail?.checkpoint_updated_at || activePlaylist?.checkpoint_updated_at;
  const stateLabel = pipelineIsActive ? 'running' : hasResumablePipeline ? 'interrupted' : pipelineFailed ? 'failed' : 'idle';
  const nowLine = pipelineIsActive
    ? (downloadState?.current || pipelineAction.replace(/_/g, ' '))
    : hasResumablePipeline
      ? 'Reconcile, import staged downloads, then continue missing tracks'
      : waitingCount
        ? 'Import downloaded tracks'
        : missingCount
          ? 'Download missing tracks'
          : activeName
            ? 'Nothing to download or import'
            : 'Pick a saved playlist';
  const pipelineLog = downloadState?.log ?? pipelineJob?.log ?? [];
  const visiblePipelineLog = logExpanded ? pipelineLog : pipelineLog.slice(-5);
  const showLastPipelineError = Boolean(detail?.last_pipeline?.error && !hasResumablePipeline);
  const linkContentTrimmed = linkContent.trim();

  const handlePipelineAction = async (action: PlaylistPipelineAction) => {
    if (!activeName) return;
    setPipelineActionBusy(action);
    setNotice(null);
    setDetailError('');
    try {
      const started = await runPlaylistPipelineAction(activeName, action);
      if (action === 'clear') {
        clearDownloadPanel();
        setPipelineJobId('');
        setPipelineJob(null);
      } else if (action === 'pause' || action === 'stop') {
        setDownloadJobId('');
        setPipelineJobId('');
        setNotice({ severity: 'info', message: `Playlist pipeline ${action === 'pause' ? 'paused at its checkpoint' : 'stopped'}.` });
      } else if (action === 'download-missing' || action === 'run-full' || action === 'resume') {
        if (started.job_id) followDownloadJob(started.job_id);
      } else if (started.jobs_job_id || started.job_id) {
        const jobId = started.jobs_job_id || started.job_id || '';
        setPipelineJobId(jobId);
        setPipelineJob({ ok: true, job_id: jobId, status: 'running', log: ['(starting...)'] });
      }
      if (!['pause', 'stop', 'clear'].includes(action)) {
        setNotice({ severity: 'info', message: `${action.replace(/-/g, ' ')} started for ${activeName}.` });
        onJobStarted?.();
      }
      await loadDetail(activeName);
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setPipelineActionBusy('');
    }
  };

  const handleStartDownload = async (
    requestedTracks: PlaylistTrack[] = [],
    playlistTracks: PlaylistTrack[] = [],
  ) => {
    if (!playlistTracks.length && !requestedTracks.length && !linkContentTrimmed) return;
    const useParsedTracks = Boolean(playlistTracks.length || requestedTracks.length);
    const tracksToDownload = requestedTracks;
    const tracksForPlaylist = playlistTracks.length ? playlistTracks : requestedTracks;
    setStartingDownload(true);
    setConfirmDownload(false);
    setDownloadError('');
    setNotice(null);
    setDownloadState({
      ok: true,
      status: 'running',
      phase: useParsedTracks ? (tracksToDownload.length ? 'download' : 'sync') : 'parse',
      current: useParsedTracks ? (tracksToDownload.length ? 'starting missing-track download' : 'starting playlist sync') : 'reading playlist track list',
      log: ['(starting...)'],
      done: 0,
      failed: 0,
      total: useParsedTracks ? tracksToDownload.length : 0,
      download_methods: downloadMethod === 'auto' ? ['slskd', 'ytdlp', 'soundcloud'] : [downloadMethod],
    });
    try {
      const started = await startPlaylistDownload({
        name: linkName.trim() || (useParsedTracks ? activeName : '') || 'Playlist',
        tracks: useParsedTracks ? tracksToDownload : undefined,
        all_tracks: useParsedTracks ? tracksForPlaylist : undefined,
        source: useParsedTracks ? undefined : linkSource,
        content: useParsedTracks ? undefined : linkContentTrimmed,
        methods: downloadMethod === 'auto' ? undefined : [downloadMethod],
        sync_after_import: true,
      });
      followDownloadJob(started.job_id);
      onJobStarted?.();
      if (started.resumed) {
        setNotice({ severity: 'info', message: 'Resumed the existing playlist download job.' });
      }
    } catch (err) {
      setDownloadError(errorMessage(err));
    } finally {
      setStartingDownload(false);
    }
  };

  const handleRetryMissingFromJob = () => {
    const retryMissing = downloadState?.missing ?? [];
    const retryTracks = downloadState?.tracks?.length
      ? downloadState.tracks
      : [...matchedAsTracks(downloadState?.matched ?? []), ...retryMissing];
    if (!retryMissing.length && !retryTracks.length) return;
    void handleStartDownload(retryMissing, retryTracks);
  };

  const handleLoadRows = async (group: FetchGroup, more = false) => {
    if (!activeName) return;
    const current = more ? groupRows[group] ?? [] : [];
    setLoadingRows(group);
    setDetailError('');
    try {
      const result = await getPlaylistRows(activeName, {
        group,
        offset: current.length,
        limit: PLAYLIST_ROW_PAGE_SIZE,
      });
      setGroupRows((rows) => ({ ...rows, [group]: [...current, ...((result.rows ?? []) as PlaylistTrack[])] }));
      setGroupHasMore((rows) => ({ ...rows, [group]: Boolean(result.has_more) }));
    } catch (err) {
      setDetailError(errorMessage(err));
    } finally {
      setLoadingRows('');
    }
  };

  const handleTrackAction = async (action: FetchTrackAction, track: PlaylistTrack) => {
    if (!activeName) return;
    if (action === 'delete_staged') {
      const ok = window.confirm(
        'Delete this downloaded staging file? The Beets music library copy will not be deleted.',
      );
      if (!ok) return;
    }
    setTrackActionKey(`${action}:${trackKey(track)}`);
    setNotice(null);
    try {
      const result = await applyPlaylistTrackAction(activeName, action, track);
      setDetail(result.playlist);
      if (result.job?.job_id && action === 'retry_download') {
        setDownloadJobId(result.job.job_id);
        setLastDownloadJobId(result.job.job_id);
      } else if (result.job?.jobs_job_id) {
        setPipelineJobId(result.job.jobs_job_id);
      }
      setNotice({
        severity: result.retry_error ? 'error' : 'success',
        message: result.retry_error || `${action.replace(/_/g, ' ')} completed.`,
      });
      await handleLoadRows(fetchGroup);
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setTrackActionKey('');
    }
  };

  const fetchTrackActions = (track: PlaylistTrack): MenuAction[] => {
    const status = (track.pipeline_status || 'missing').toString().toLowerCase();
    const hasStagedFile = Boolean(track.staged_path);
    const failed = status === 'failed' || status === 'review_required' || Boolean(track.failure_reason);
    const downloadedNotImported = hasStagedFile || status === 'downloaded' || status === 'waiting_import' || status === 'importing';
    if (downloadedNotImported) {
      return [
        { label: 'Import Now', onClick: () => void handleTrackAction('retry_import', track) },
        ...(hasStagedFile ? [{ label: 'Delete Staged Download', onClick: () => void handleTrackAction('delete_staged', track), danger: true }] : []),
      ];
    }
    if (failed) {
      return [{ label: 'Retry', onClick: () => void handleTrackAction(hasStagedFile ? 'retry_import' : 'retry_download', track) }];
    }
    return [{ label: 'Retry Download', onClick: () => void handleTrackAction('retry_download', track) }];
  };

  const loadedGroupRows = groupRows[fetchGroup];
  const visibleRows = loadedGroupRows ? rowsForGroup(fetchGroup, loadedGroupRows) : [];
  const downloadRound = downloadState?.round
    ? `round ${downloadState.round}${downloadState.max_rounds ? `/${downloadState.max_rounds}` : ''}`
    : '';
  const downloadProgress = downloadState?.total
    ? `${downloadState.done ?? 0} / ${downloadState.total}${downloadState.failed ? ` (${downloadState.failed} failed)` : ''}`
    : downloadState?.matched_after_import !== undefined
      ? `${downloadState.matched_after_import} matched`
      : '';
  const downloadStatusDetail = [downloadState?.phase, downloadRound, downloadProgress].filter(Boolean).join(' · ');
  const stateTone = stateLabel === 'interrupted' ? 'text-warning' : stateLabel === 'running' ? 'text-info' : stateLabel === 'failed' ? 'text-danger-text' : 'text-zinc-300';

  return (
    <section aria-labelledby="playlist-downloads-title" className="space-y-8">
      <div className="ops-split">
        <div className="ops-split-aside">
          <h2 id="playlist-downloads-title" className="ops-section-title">Playlist Downloads</h2>
          <p className="mt-2 text-sm leading-relaxed text-muted">
            Download a playlist&apos;s missing tracks and import them into Beets. Make playlists and sync them to Plex on{' '}
            <Link className="text-zinc-300 underline underline-offset-4 hover:text-zinc-50" to="/playlists">Playlists</Link>.
          </p>
        </div>

        <div className="min-w-0 space-y-5">
          <label className="flex max-w-md flex-col gap-1 text-sm text-zinc-300">
            Saved playlist
            <select
              className="ops-field w-full min-w-0"
              value={activeName}
              onChange={(event) => {
                setSelectedName(event.target.value);
                setNotice(null);
                setPipelineJobId('');
                setPipelineJob(null);
              }}
              disabled={loadingPlaylists}
            >
              <option value="">{loadingPlaylists ? 'Loading playlists...' : playlists.length ? 'Pick a saved playlist' : 'No saved playlists yet'}</option>
              {playlists.map((playlist) => (
                <option key={playlist.name} value={playlist.name}>{playlist.name}</option>
              ))}
            </select>
          </label>
          {playlistError ? (
            <Alert severity="error">Saved playlists could not be loaded: {playlistError}. Refresh the page to try again.</Alert>
          ) : null}

          <div aria-live="polite">
            <p className="flex min-w-0 flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
              <span className="min-w-0 break-words font-display text-lg font-semibold text-zinc-100">{activeName || 'No playlist picked'}</span>
              <span className={stateTone}>{stateLabel}</span>
              <span className="text-zinc-400">{pipelineAction.replace(/_/g, ' ')}</span>
              {lastCheckpointAt ? <span className="text-xs text-muted">checkpoint {formatShortDateTime(lastCheckpointAt * 1000)}</span> : null}
            </p>
            <dl className="ops-figures mt-3">
              <div><dt>In Beets</dt><dd className="text-success">{matchedCount.toLocaleString()}</dd></div>
              <div><dt>Missing</dt><dd className={missingCount ? 'text-danger-text' : undefined}>{missingCount.toLocaleString()}</dd></div>
              <div><dt>Waiting import</dt><dd className={waitingCount ? 'text-info' : undefined}>{waitingCount.toLocaleString()}</dd></div>
              <div><dt>Failed</dt><dd className={failedCount ? 'text-warning' : undefined}>{failedCount.toLocaleString()}</dd></div>
            </dl>
            <p className="mt-2 min-w-0 break-words text-sm text-zinc-300"><span className="text-muted">Now:</span> {nowLine}</p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {pipelineIsActive ? (
              <>
                <Button variant="outlined" size="small" onClick={() => void handlePipelineAction('pause')} disabled={!activeName}>
                  Pause
                </Button>
                <Button variant="outlined" color="error" size="small" onClick={() => void handlePipelineAction('stop')} disabled={!activeName}>
                  Stop
                </Button>
              </>
            ) : (
              <Button
                variant="contained"
                color={hasResumablePipeline ? 'warning' : 'primary'}
                size="small"
                disabled={!activeName || engineOffline}
                onClick={() => void handlePipelineAction(mainPipelineAction)}
              >
                {mainPipelineLabel}
              </Button>
            )}
            <Button
              variant="outlined"
              size="small"
              disabled={!activeName || pipelineIsActive || !missingCount}
              onClick={() => void handlePipelineAction('download-missing')}
            >
              Download Missing
            </Button>
            <Button
              variant="outlined"
              size="small"
              disabled={!activeName || pipelineIsActive || !waitingCount}
              onClick={() => void handlePipelineAction('import-downloaded')}
            >
              Import Downloaded
            </Button>
            {hasResumablePipeline || pipelineFailed ? (
              <Button
                variant="outlined"
                color="error"
                size="small"
                disabled={!activeName || pipelineIsActive}
                onClick={() => void handlePipelineAction('clear')}
              >
                Clear Checkpoint
              </Button>
            ) : null}
          </div>

          {notice ? <Alert severity={notice.severity} onClose={() => setNotice(null)}>{notice.message}</Alert> : null}
          {detailError ? <Alert severity="error">{detailError}</Alert> : null}
          {showLastPipelineError ? <Alert severity="error">{detail?.last_pipeline?.error}</Alert> : null}

          {pipelineLog.length ? (
            <div>
              <LogViewer
                className={`${logExpanded ? 'max-h-72' : 'max-h-24'} text-xs leading-5 text-zinc-400`}
                emptyText="No playlist pipeline log yet."
                lines={visiblePipelineLog}
                showControls={false}
              />
              <button
                type="button"
                className="mt-1 min-h-10 rounded px-2 text-xs text-zinc-300 underline underline-offset-4 hover:text-zinc-100 active:text-brass-200"
                onClick={() => setLogExpanded((value) => !value)}
              >
                {logExpanded ? 'Collapse Log' : 'Expand Log'}
              </button>
            </div>
          ) : null}

          <PlaylistJobProgress
            downloadState={downloadState}
            downloadError={downloadError}
            detail={downloadStatusDetail}
            onClear={clearDownloadPanel}
            onRetryMissing={handleRetryMissingFromJob}
            canRetryMissing={Boolean((downloadState?.missing?.length ?? 0) > 0 && !downloadRunning)}
          />
        </div>
      </div>

      {activeName ? (
        <section aria-labelledby="playlist-fetch-tracks-title" className="ops-split">
          <div className="ops-split-aside">
            <h3 id="playlist-fetch-tracks-title" className="ops-section-title">Single tracks</h3>
            <p className="mt-2 text-sm leading-relaxed text-muted">Retry one download or import at a time. Resolving and excluding tracks is on Playlists.</p>
          </div>
          <div className="min-w-0 space-y-3">
            <div role="group" aria-label="Track state" className="flex flex-wrap gap-1 border-b border-graphite-800">
              {FETCH_GROUPS.map((group) => (
                <button
                  key={group.id}
                  type="button"
                  className={`ops-filter${fetchGroup === group.id ? ' is-on' : ''}`}
                  aria-pressed={fetchGroup === group.id}
                  onClick={() => setFetchGroup(group.id)}
                >
                  {group.label}
                </button>
              ))}
            </div>
            {!loadedGroupRows || groupHasMore[fetchGroup] ? (
              <Button
                variant="outlined"
                size="small"
                disabled={Boolean(loadingRows)}
                onClick={() => void handleLoadRows(fetchGroup, Boolean(loadedGroupRows))}
              >
                {loadingRows === fetchGroup ? 'Loading Rows...' : loadedGroupRows ? 'Load More Rows' : 'Load Rows'}
              </Button>
            ) : null}
            {loadedGroupRows ? (
              visibleRows.length ? (
                <ul className="ops-track-list max-h-96 overflow-auto border-y border-graphite-800">
                  {visibleRows.map((track, idx) => (
                    <li key={`${idx}:${trackKey(track)}`} className="flex flex-wrap items-start justify-between gap-2 py-2 text-sm">
                      <div className="min-w-0 flex-1 basis-48">
                        <div className="break-words text-zinc-200">{[track.artist, track.title].filter(Boolean).join(' - ') || '(untitled)'}</div>
                        {canonicalNote(track) ? <div className="break-words text-xs text-info">{canonicalNote(track)}</div> : null}
                        <div className="mt-0.5 text-xs text-muted">{statusLabel(track.pipeline_status || 'missing')}</div>
                        {track.failure_reason || track.reason || track.pipeline_message ? (
                          <div className="mt-0.5 break-words text-xs text-danger-text">{track.failure_reason || track.reason || track.pipeline_message}</div>
                        ) : null}
                      </div>
                      <ActionMenu
                        actions={fetchTrackActions(track)}
                        disabled={pipelineRunning || Boolean(trackActionKey)}
                      />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted">No {FETCH_GROUPS.find((group) => group.id === fetchGroup)?.label.toLowerCase()} tracks.</p>
              )
            ) : null}
          </div>
        </section>
      ) : null}

      <section aria-labelledby="playlist-link-download-title" className="ops-split">
        <div className="ops-split-aside">
          <h3 id="playlist-link-download-title" className="ops-section-title">From a link</h3>
          <p className="mt-2 text-sm leading-relaxed text-muted">Read a playlist link or track list, download what Beets is missing, import it, then save the playlist.</p>
        </div>
        <div className="min-w-0 space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="flex min-w-0 flex-col gap-1 text-sm text-zinc-300">
              Playlist name
              <input className="ops-field w-full min-w-0" value={linkName} onChange={(event) => setLinkName(event.target.value)} placeholder="Playlist" />
            </label>
            <label className="flex min-w-0 flex-col gap-1 text-sm text-zinc-300">
              Source
              <select className="ops-field w-full min-w-0" value={linkSource} onChange={(event) => setLinkSource(event.target.value as PlaylistSource)}>
                {LINK_SOURCE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label className="flex min-w-0 flex-col gap-1 text-sm text-zinc-300">
              Download Source
              <select className="ops-field w-full min-w-0" value={downloadMethod} onChange={(event) => setDownloadMethod(event.target.value)}>
                {DOWNLOAD_METHOD_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
          </div>
          {linkSource === 'url' ? (
            <label className="flex min-w-0 flex-col gap-1 text-sm text-zinc-300">
              Playlist URL
              <input
                className="ops-field w-full min-w-0"
                value={linkContent}
                onChange={(event) => setLinkContent(event.target.value)}
                placeholder="https://..."
              />
            </label>
          ) : (
            <label className="flex min-w-0 flex-col gap-1 text-sm text-zinc-300">
              Track list
              <textarea
                className="ops-field h-auto min-h-32 w-full min-w-0 py-2"
                value={linkContent}
                onChange={(event) => setLinkContent(event.target.value)}
                placeholder="Artist - Title, one per line"
                spellCheck={false}
              />
            </label>
          )}
          <Button variant="contained" size="small" onClick={() => setConfirmDownload(true)} disabled={!linkContentTrimmed || downloadRunning}>
            Download Missing & Sync
          </Button>
        </div>
      </section>

      {startingDownload ? <LinearProgress sx={{ borderRadius: 1 }} /> : null}

      <DownloadConfirmDialog
        open={confirmDownload}
        missing={0}
        total={0}
        needsPull
        busy={startingDownload}
        onClose={() => setConfirmDownload(false)}
        onConfirm={() => {
          void handleStartDownload();
        }}
      />
    </section>
  );
}
