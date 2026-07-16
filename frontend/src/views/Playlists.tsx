import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import Menu from '@mui/material/Menu';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  applySafePlaylistSuggestions,
  cleanupPlaylistQuality,
  createPlaylist,
  deletePlaylist,
  applyPlaylistTrackAction,
  getJob,
  getPlaylistDetails,
  getPlaylistDownloadStatus,
  getPlaylistRows,
  getPlaylistSyncStatus,
  getPlaylists,
  getPlaylistSuggestions,
  placePlaylistQuality,
  parsePlaylist,
  resolvePlaylistTrack,
  runPlaylistPipelineAction,
  startPlaylistDownload,
} from '../api/client';
import type {
  PlaylistCreateResponse,
  PlaylistDownloadStatusResponse,
  PlaylistDetailResponse,
  PlaylistEntry,
  PlaylistMatchedTrack,
  PlaylistParseResponse,
  PlaylistSuggestionRow,
  PlaylistSource,
  PlaylistSyncStatusResponse,
  JobResponse,
  PlaylistTrack,
  PlaylistTrackSuggestion,
} from '../api/types';
import { LogViewer } from '../components/LogViewer';

type Notice = {
  severity: 'info' | 'success' | 'warning' | 'error';
  message: string;
};

type ResolveDraft = {
  key: string;
  track: PlaylistTrack;
  artist: string;
  title: string;
};

type PlaylistPipelineAction =
  | 'sync-sources'
  | 'download-missing'
  | 'import-downloaded'
  | 'sync-plex'
  | 'reconcile-state'
  | 'run-full'
  | 'resume'
  | 'pause'
  | 'stop'
  | 'clear';

type TrackGroupId = 'available' | 'missing' | 'waiting' | 'failed' | 'pending_plex' | 'removed';

const PLAYLIST_ROW_PAGE_SIZE = 100;
const PLAYLIST_SYNC_STATUS_POLL_MS = 60_000;
const PLAYLIST_DOWNLOAD_POLL_MS = 3_000;
const PLAYLIST_DOWNLOAD_RETRY_POLL_MS = 6_000;
const PLAYLIST_PIPELINE_JOB_POLL_MS = 5_000;
const PLAYLIST_HIDDEN_POLL_MS = 15_000;

function playlistPollDelay(ms: number): number {
  return typeof document !== 'undefined' && document.hidden ? Math.max(ms, PLAYLIST_HIDDEN_POLL_MS) : ms;
}

const TRACK_GROUP_LABELS: Record<TrackGroupId, string> = {
  available: 'Available',
  missing: 'Missing',
  waiting: 'Waiting Import',
  failed: 'Failed/Review',
  pending_plex: 'Pending Plex Match',
  removed: 'Removed/Excluded',
};

type TrackAction =
  | 'remove'
  | 'exclude'
  | 'restore'
  | 'delete_staged'
  | 'retry_download'
  | 'retry_import';

type MenuAction = {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
};

type QualityPlaceDraft = {
  itemId: number;
  artist: string;
  title: string;
  albumartist: string;
  album: string;
  year: string;
  track: string;
  disc: string;
  tracktotal: string;
  disctotal: string;
  mbTrackId: string;
  mbAlbumId: string;
  mbReleaseGroupId: string;
};

const SOURCE_OPTIONS: Array<{ value: PlaylistSource; label: string }> = [
  { value: 'local_m3u', label: 'Local M3U' },
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

const sampleTrackList = `Pink Floyd - Breathe
Radiohead - Creep
The Beatles - Come Together`;

const PLAYLIST_JOB_STORAGE_KEY = 'beets-playlist-download-job-id';
const PLAYLIST_LAST_JOB_STORAGE_KEY = 'beets-playlist-download-last-job-id';

function platformLabel(value: string): { label: string; tone: string } {
  const url = value.toLowerCase();
  if (!url.trim()) return { label: 'Paste a URL', tone: 'border-graphite-700 bg-graphite-900 text-zinc-500' };
  if (url.includes('music.youtube') || url.includes('youtube.com') || url.includes('youtu.be')) {
    return { label: 'YouTube', tone: 'border-red-900 bg-red-950 text-red-300' };
  }
  if (url.includes('spotify.com')) {
    return { label: 'Spotify', tone: 'border-emerald-900 bg-emerald-950 text-emerald-300' };
  }
  if (url.includes('soundcloud.com')) {
    return { label: 'SoundCloud', tone: 'border-orange-900 bg-orange-950 text-orange-300' };
  }
  if (url.includes('music.apple.com')) {
    return { label: 'Apple Music', tone: 'border-rose-900 bg-rose-950 text-rose-300' };
  }
  return { label: 'URL', tone: 'border-graphite-700 bg-graphite-900 text-zinc-300' };
}

function playlistStatusMessage(result: PlaylistCreateResponse | null | undefined): string {
  if (!result) return '';
  const tracks = result.tracks_in_m3u ?? 0;
  const desired = result.desired_tracks ?? tracks;
  const missing = result.missing_tracks ?? 0;
  const plex = result.plex ?? {};
  const parts = [`M3U saved with ${tracks.toLocaleString()}${desired > tracks ? ` of ${desired.toLocaleString()}` : ''} track${tracks === 1 ? '' : 's'}`];
  if (missing) parts.push(`${missing.toLocaleString()} still unavailable`);
  const matched = plex.tracks_matched ?? plex.tracks_added ?? 0;
  const pending = plex.pending_plex_count ?? plex.tracks_unmatched ?? 0;
  if (plex.error || plex.status === 'failed') {
    parts.push(plex.error || plex.summary_message || plex.issue_reason || 'Plex sync failed');
    if ((plex.issue_reason || '').toLowerCase().includes('path mapping') && plex.path_mapping_used) {
      parts.push(`Path map ${plex.path_mapping_used}`);
    }
  } else if (plex.created && (plex.status === 'partial_success' || plex.status === 'partial' || pending > 0)) {
    parts.push(`Plex playlist updated with ${matched.toLocaleString()} track${matched === 1 ? '' : 's'}; ${pending.toLocaleString()} pending Plex match${pending === 1 ? '' : 'es'}`);
  } else if (plex.created) {
    const verified = plex.verified_count ?? plex.tracks_added ?? matched;
    parts.push(`Plex synced ${verified.toLocaleString()} track${verified === 1 ? '' : 's'}`);
  } else {
    parts.push('Plex not configured');
  }
  return parts.join(' · ');
}

function playlistStatusSeverity(result: PlaylistCreateResponse | null | undefined): Notice['severity'] {
  const plex = result?.plex;
  if (!plex) return 'success';
  if (plex.error || plex.status === 'failed') return 'error';
  if (plex.status === 'partial_success' || plex.status === 'partial' || (plex.pending_plex_count ?? plex.tracks_unmatched ?? 0) > 0) return 'warning';
  if (plex.status === 'not_configured' || (!plex.created && !plex.error)) return 'info';
  return 'success';
}

function queryLabel(track: PlaylistMatchedTrack): string {
  const artist = track.query_artist || track.artist || '';
  const title = track.query_title || track.title || '';
  return [artist, title].filter(Boolean).join(' - ');
}

function canonicalNote(track: PlaylistTrack | PlaylistMatchedTrack): string {
  if (!track.canonicalized) return '';
  const source = [track.source_artist, track.source_title].filter(Boolean).join(' - ');
  const current = [track.artist, track.title].filter(Boolean).join(' - ');
  if (!source || source === current) return '';
  return `corrected from ${source}`;
}

function scoreLabel(score: number | undefined): string {
  if (score === undefined || Number.isNaN(score)) return '';
  const pct = score <= 1 ? score * 100 : score;
  return `${Math.round(pct)}%`;
}

function suggestionLabel(suggestion: PlaylistTrackSuggestion): string {
  const confidence = scoreLabel(suggestion.confidence);
  const source = suggestion.source === 'beets-title' ? 'beets' : suggestion.source;
  return `${suggestion.artist ? `${suggestion.artist} - ` : ''}${suggestion.title}${confidence ? ` · ${confidence}` : ''} · ${source}`;
}

function durationLabel(seconds: number | undefined): string {
  if (!seconds || Number.isNaN(seconds)) return '';
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60).toString().padStart(2, '0');
  return `${minutes}:${rest}`;
}

function optionalNumber(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function yearDraft(value: number | undefined): string {
  if (!value || value < 1000 || value > 9999) return '';
  return String(value);
}

function qualityPlaceDraftForTrack(track: PlaylistMatchedTrack): QualityPlaceDraft {
  return {
    itemId: track.id,
    artist: track.artist || track.query_artist || '',
    title: track.title || track.query_title || '',
    albumartist: track.albumartist || track.artist || track.query_artist || '',
    album: track.album && track.album !== 'Unknown' ? track.album : '',
    year: yearDraft(track.year),
    track: track.track && track.track > 0 ? String(track.track) : '',
    disc: track.disc && track.disc > 0 ? String(track.disc) : '1',
    tracktotal: '',
    disctotal: '1',
    mbTrackId: '',
    mbAlbumId: '',
    mbReleaseGroupId: '',
  };
}

function qualityColor(quality: string | undefined): 'success' | 'warning' | 'error' | 'default' {
  if (quality === 'bad') return 'error';
  if (quality === 'review') return 'warning';
  if (quality === 'ok') return 'success';
  return 'default';
}

function statusTone(status: string): string {
  if (status === 'matched' || status === 'downloaded' || status === 'waiting_import') return 'border-emerald-900 bg-emerald-950/30 text-emerald-300';
  if (status === 'searching' || status === 'importing' || status === 'queued') return 'border-sky-900 bg-sky-950/30 text-sky-300';
  if (status === 'failed' || status === 'source_failed') return 'border-rose-900 bg-rose-950/30 text-rose-300';
  return 'border-graphite-700 bg-graphite-950/40 text-zinc-300';
}

function statusLabel(status: string): string {
  return status.replace(/_/g, ' ');
}

function identityEvidenceLabel(track: PlaylistTrack): string {
  const parts: string[] = [];
  const identity = (track.identity_status || '').toString().toLowerCase();
  if (identity === 'verified') parts.push('Audio identity: Verified');
  else if (identity === 'conflict') parts.push('Audio identity: Conflict');
  else if (identity === 'review_required') parts.push('Audio identity: Review required');

  const fingerprint = (track.fingerprint_status || '').toString().toLowerCase();
  if (fingerprint === 'matched') parts.push('Fingerprint: Matched');
  else if (fingerprint === 'no_result') parts.push('Fingerprint: No result');
  else if (fingerprint === 'failed') parts.push('Fingerprint: Failed');

  if (typeof track.acoustid_score === 'number' && track.acoustid_score > 0) {
    parts.push(`AcoustID: ${track.acoustid_score.toFixed(2)}`);
  } else if (track.acoustid_status) {
    parts.push(`AcoustID: ${statusLabel(track.acoustid_status)}`);
  }
  if (track.identity_mb_trackid || track.mb_trackid) parts.push('Recording: MusicBrainz ID');
  if (track.identity_mb_releasegroupid || track.mb_releasegroupid) parts.push('Release group: Resolved');
  return parts.join(' · ');
}

function metric(label: string, value: number | string, tone = 'text-zinc-100') {
  return (
    <div className="rounded border border-graphite-800 bg-graphite-950/60 px-3 py-2">
      <div className={`text-lg font-semibold tabular-nums ${tone}`}>{value}</div>
      <div className="mt-0.5 text-[0.65rem] uppercase tracking-wide text-zinc-500">{label}</div>
    </div>
  );
}

function compactStat(label: string, value: number | string, tone = 'text-zinc-300') {
  return (
    <span className={`inline-flex h-7 items-center rounded-full border border-graphite-800 bg-graphite-950/60 px-2.5 text-xs ${tone}`}>
      <span className="font-semibold tabular-nums text-zinc-100">{value}</span>
      <span className="ml-1 text-zinc-500">{label}</span>
    </span>
  );
}

function isWaitingImportTrack(track: PlaylistTrack): boolean {
  const status = (track.pipeline_status || '').toString().toLowerCase();
  return Boolean(track.staged_path) || status === 'downloaded' || status === 'waiting_import' || status === 'importing';
}

function isFailedReviewTrack(track: PlaylistTrack): boolean {
  const status = (track.pipeline_status || '').toString().toLowerCase();
  return status === 'failed' || status === 'source_failed' || status === 'review_required' || Boolean(track.failure_reason);
}

function ActionMenu({
  label = 'Actions',
  actions,
  disabled,
  color = 'primary',
}: {
  label?: string;
  actions: MenuAction[];
  disabled?: boolean;
  color?: 'primary' | 'warning';
}) {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
  const visibleActions = actions.filter(Boolean);
  if (!visibleActions.length) return null;
  const open = Boolean(anchorEl);
  return (
    <>
      <Button
        variant="outlined"
        size="small"
        color={color}
        disabled={disabled}
        onClick={(event) => setAnchorEl(event.currentTarget)}
      >
        {label}
      </Button>
      <Menu
        anchorEl={anchorEl}
        open={open}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        {visibleActions.map((action, idx) => (
          <MenuItem
            key={`${action.label}:${idx}`}
            disabled={action.disabled}
            sx={action.danger ? { color: 'error.main' } : undefined}
            onClick={() => {
              setAnchorEl(null);
              action.onClick();
            }}
          >
            {action.label}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

function trackKey(track: PlaylistTrack): string {
  return `${(track.artist || '').toLowerCase()}|${(track.title || '').toLowerCase()}|${track.path || ''}`;
}

function suggestionRowsByTrack(rows: PlaylistSuggestionRow[]): Map<string, PlaylistSuggestionRow> {
  const map = new Map<string, PlaylistSuggestionRow>();
  rows.forEach((row) => {
    map.set(trackKey(row.track), row);
  });
  return map;
}

function matchedAsTracks(items: PlaylistMatchedTrack[]): PlaylistTrack[] {
  return items.map((item) => ({
    artist: item.query_artist || item.artist || '',
    title: item.query_title || item.title || '',
    source_artist: item.source_artist,
    source_title: item.source_title,
    canonicalized: item.canonicalized,
    canonical_source: item.canonical_source,
  }));
}

function allTracksFromResult(result: PlaylistParseResponse | null): PlaylistTrack[] {
  if (!result) return [];
  if (result.tracks?.length) return result.tracks;
  return [...matchedAsTracks(result.matched ?? []), ...(result.missing ?? [])];
}

function playlistSyncStatusLabel(status: PlaylistSyncStatusResponse | null): string {
  if (!status) return '';
  const mode = status.enabled ? `Auto every ${Math.round((status.interval || 0) / 60)} min` : 'Auto off';
  const result = status.last_result;
  const last = status.last_run
    ? new Date(status.last_run * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : 'never';
  const detail = result
    ? ` · ${result.playlists_updated ?? 0} updated · +${result.local_added ?? 0} M3U · +${result.plex_added ?? 0} Plex`
    : '';
  return `${mode} · Last ${last}${detail}`;
}

function playlistCoverageNote(playlist: PlaylistEntry): string {
  if (playlist.m3u_tracks === undefined || playlist.m3u_tracks === playlist.tracks) return '';
  const source = playlist.desired_source === 'checkpoint' ? 'checkpoint' : 'desired';
  return `M3U ${playlist.m3u_tracks.toLocaleString()} · ${source} ${playlist.tracks.toLocaleString()}`;
}

const RESUMABLE_PLAYLIST_STATUSES = new Set(['interrupted', 'paused', 'stopped', 'failed', 'error']);
const DIRECT_RESUME_PLAYLIST_STATUSES = new Set(['interrupted', 'paused', 'stopped']);
const FAILED_RESUME_PLAYLIST_STATUSES = new Set(['failed', 'error']);

function normalizedPlaylistStatus(value?: string | null): string {
  return String(value || '').trim().toLowerCase();
}

function playlistHasResumableCheckpoint(
  detail?: Partial<PlaylistDetailResponse> | null,
  playlist?: Partial<PlaylistEntry> | null,
): boolean {
  const checkpointStatus = normalizedPlaylistStatus(detail?.checkpoint_status || playlist?.checkpoint_status);
  const checkpointPhase = normalizedPlaylistStatus(detail?.checkpoint_phase || playlist?.checkpoint_phase);
  const lastPipelineStatus = normalizedPlaylistStatus(detail?.last_pipeline?.status || playlist?.last_pipeline?.status);
  const hasCheckpointEvidence = Boolean(
    detail?.checkpoint_job_id
      || playlist?.checkpoint_job_id
      || detail?.checkpoint_updated_at
      || playlist?.checkpoint_updated_at,
  );
  if (detail?.checkpoint_interrupted || playlist?.checkpoint_interrupted) return true;
  if (RESUMABLE_PLAYLIST_STATUSES.has(checkpointStatus)) return true;
  if (RESUMABLE_PLAYLIST_STATUSES.has(checkpointPhase)) return true;
  if (DIRECT_RESUME_PLAYLIST_STATUSES.has(lastPipelineStatus)) return true;
  return hasCheckpointEvidence && FAILED_RESUME_PLAYLIST_STATUSES.has(lastPipelineStatus);
}

function playlistCheckpointNote(playlist: PlaylistEntry): string {
  const checkpointStatus = normalizedPlaylistStatus(playlist.checkpoint_status);
  const checkpointPhase = normalizedPlaylistStatus(playlist.checkpoint_phase);
  const pipelineStatus = normalizedPlaylistStatus(playlist.last_pipeline?.status);
  const interrupted = Boolean(playlist.checkpoint_interrupted)
    || ['interrupted', 'paused', 'stopped'].includes(checkpointStatus)
    || ['interrupted', 'paused', 'stopped'].includes(checkpointPhase)
    || ['interrupted', 'paused', 'stopped'].includes(pipelineStatus);
  if (!interrupted) return '';
  const missing = playlist.checkpoint_missing ?? playlist.missing ?? 0;
  return `Interrupted checkpoint${missing ? ` · ${missing.toLocaleString()} missing` : ''}`;
}

type SavedBadgeTone = 'ok' | 'warn' | 'danger' | 'idle' | 'info';

type SavedPlaylistSummary = {
  totalTracks: number;
  libraryMatched: number;
  matchPercent: number;
  missing: number;
  downloadedTotal: number;
  waitingImport: number;
  importedFromDownloads: number;
  failed: number;
  pipelineReview: number;
  qualityReview: number;
  review: number;
  removed: number;
  excluded: number;
  plexKnown: boolean;
  plexSynced: number;
  plexMatched: number;
  plexEligible: number;
  plexPending: number;
  plexMissing: number;
  plexNeedsSync: boolean;
  plexIssue: string;
  sourceLabel: string;
  sourceStatus: string;
  updatedLabel: string;
  statusBadge: string;
  statusTone: SavedBadgeTone;
  nextStep: string;
  nextStepTone: SavedBadgeTone;
  nextStepKey: 'resume' | 'import-downloaded' | 'download-missing' | 'review' | 'sync-plex' | 'all-good';
  activeFailures: number;
  historicalFailures: number;
  interrupted: boolean;
  pipelineFailed: boolean;
  running: boolean;
  hasIssueAction: boolean;
  lastPipelineLabel: string;
  lastCheckpointLabel: string;
  lastError: string;
};

type SavedPlaylistFilter = 'all' | 'needs-action' | 'running' | 'missing' | 'waiting-import' | 'review' | 'plex' | 'clean';

type SavedPlaylistRow = {
  playlist: PlaylistEntry;
  summary: SavedPlaylistSummary;
};

type SavedPlaylistMetrics = {
  total: number;
  needsAction: number;
  running: number;
  missing: number;
  waitingImport: number;
  review: number;
  plex: number;
  clean: number;
  tracks: number;
  available: number;
};

const SAVED_PLAYLIST_FILTERS: Array<{ key: SavedPlaylistFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'needs-action', label: 'Needs Action' },
  { key: 'running', label: 'Running' },
  { key: 'missing', label: 'Missing' },
  { key: 'waiting-import', label: 'Waiting Import' },
  { key: 'review', label: 'Review' },
  { key: 'plex', label: 'Plex' },
  { key: 'clean', label: 'Clean' },
];

function countValue(value: number | undefined | null): number {
  return Math.max(0, Number(value || 0));
}

function plural(value: number, singular: string, pluralLabel = `${singular}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : pluralLabel}`;
}

function formatSavedPlaylistSource(value?: string): string {
  if (value === 'url') return 'URL playlist';
  if (value === 'text') return 'Track list';
  if (value === 'local_m3u') return 'Local M3U';
  if (value === 'manifest') return 'Manifest only';
  return value ? statusLabel(value) : 'Local M3U';
}

function playlistFileBadge(playlist: PlaylistEntry): { label: string; tone: SavedBadgeTone } | null {
  if (playlist.has_m3u) return { label: 'Local M3U', tone: 'info' };
  if (playlist.has_manifest) return { label: 'Manifest only', tone: 'idle' };
  if (playlist.has_checkpoint) return { label: 'Checkpoint only', tone: 'warn' };
  return null;
}

function formatPlaylistTime(value?: number): string {
  if (!value) return 'not updated';
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return 'not updated';
  return `updated ${date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
}

function statusIn(value: string | undefined | null, statuses: string[]): boolean {
  return statuses.includes(normalizedPlaylistStatus(value));
}

function summarizeSavedPlaylist(playlist: PlaylistEntry): SavedPlaylistSummary {
  const totalTracks = countValue(playlist.tracks);
  const libraryMatched = countValue(playlist.available);
  const missing = countValue(playlist.missing);
  const removed = countValue(playlist.removed);
  const excluded = countValue(playlist.excluded);
  const rawDownloaded = countValue(playlist.downloaded);
  const importedFromDownloads = countValue(playlist.imported);
  const waitingImport = Math.max(
    countValue(playlist.checkpoint_waiting_for_import),
    rawDownloaded - importedFromDownloads,
    0,
  );
  const downloadedTotal = Math.max(rawDownloaded, importedFromDownloads + waitingImport);
  const failed = countValue(playlist.failed);
  const pipelineReview = countValue(playlist.review_required);
  const qualityReview = countValue(playlist.quality_review) + countValue(playlist.quality_bad);
  const review = pipelineReview + qualityReview;
  const matchPercent = totalTracks ? Math.round((libraryMatched / totalTracks) * 100) : 0;
  const checkpointStatus = normalizedPlaylistStatus(playlist.checkpoint_status);
  const checkpointPhase = normalizedPlaylistStatus(playlist.checkpoint_phase);
  const pipelineStatus = normalizedPlaylistStatus(playlist.last_pipeline?.status);
  const interrupted = Boolean(playlist.checkpoint_interrupted)
    || ['interrupted', 'paused', 'stopped'].includes(checkpointStatus)
    || ['interrupted', 'paused', 'stopped'].includes(checkpointPhase)
    || ['interrupted', 'paused', 'stopped'].includes(pipelineStatus);
  const running = ['running', 'queued', 'parse', 'searching', 'download', 'downloaded', 'importing', 'syncing'].includes(checkpointStatus)
    || ['running', 'queued', 'parse', 'searching', 'download', 'downloaded', 'importing', 'syncing'].includes(checkpointPhase)
    || ['running', 'queued', 'parse', 'searching', 'download', 'downloaded', 'importing', 'syncing'].includes(pipelineStatus);
  const pipelineFailed = statusIn(checkpointStatus, ['failed', 'error'])
    || statusIn(checkpointPhase, ['failed', 'error'])
    || statusIn(pipelineStatus, ['failed', 'error']);

  const plexStatus = normalizedPlaylistStatus(playlist.last_sync_status);
  const lastPlex = playlist.last_plex ?? {};
  const lastPlexStatus = normalizedPlaylistStatus(lastPlex.status);
  const hasPlexState = playlist.plex_synced !== null && playlist.plex_synced !== undefined;
  const plexMatched = countValue(lastPlex.tracks_matched ?? playlist.plex_tracks_matched ?? playlist.plex_tracks);
  const plexEligible = countValue(lastPlex.tracks_requested ?? playlist.available ?? plexMatched);
  const plexPending = countValue(lastPlex.pending_plex_count ?? playlist.plex_pending_count ?? lastPlex.tracks_unmatched ?? playlist.plex_tracks_unmatched);
  const plexFailed = plexStatus === 'failed' || lastPlexStatus === 'failed';
  const plexKnown = hasPlexState
    || plexMatched > 0
    || plexPending > 0
    || countValue(playlist.plex_synced_count) > 0
    || (plexStatus !== '' && plexStatus !== 'not_run')
    || (lastPlexStatus !== '' && lastPlexStatus !== 'not_run');
  const plexSynced = Math.max(
    countValue(lastPlex.verified_count),
    countValue(lastPlex.existing_playlist_count),
    countValue(playlist.plex_synced_count),
    countValue(playlist.plex_tracks),
    plexMatched,
  );
  const plexMissing = plexPending;
  const plexIssue = plexFailed
    ? (lastPlex.error || playlist.last_sync_error || 'Plex sync failed')
    : (plexPending ? `${plexPending.toLocaleString()} pending Plex match${plexPending === 1 ? '' : 'es'}` : '');
  const plexNeedsSync = !plexKnown || plexFailed || playlist.plex_synced === false || plexPending > 0;
  const actionableReview = failed + pipelineReview;
  // Failures are "active" only while tracks are still missing; once missing==0, they're historical
  const activeFailures = missing > 0 ? Math.min(actionableReview, missing) : 0;
  const historicalFailures = actionableReview - activeFailures;

  let nextStepKey: SavedPlaylistSummary['nextStepKey'] = 'all-good';
  let nextStep = 'All good';
  let nextStepTone: SavedBadgeTone = 'ok';
  if (interrupted) {
    nextStepKey = 'resume';
    nextStep = 'Resume interrupted job';
    nextStepTone = 'warn';
  } else if (waitingImport > 0) {
    nextStepKey = 'import-downloaded';
    nextStep = `Import ${plural(waitingImport, 'downloaded track')}`;
    nextStepTone = 'warn';
  } else if (missing > 0) {
    nextStepKey = 'download-missing';
    nextStep = `Download ${plural(missing, 'missing track')}`;
    nextStepTone = 'danger';
  } else if (activeFailures > 0) {
    nextStepKey = 'review';
    nextStep = `Review ${plural(activeFailures, 'unresolved track')}`;
    nextStepTone = 'danger';
  } else if (plexNeedsSync) {
    nextStepKey = 'sync-plex';
    nextStep = plexIssue ? 'Sync to Plex / Fix Plex match' : 'Sync to Plex';
    nextStepTone = 'warn';
  }

  let statusBadge = 'Synced';
  let statusTone: SavedBadgeTone = 'ok';
  if (interrupted) {
    statusBadge = 'Interrupted';
    statusTone = 'warn';
  } else if (waitingImport > 0) {
    statusBadge = 'Waiting Import';
    statusTone = 'warn';
  } else if (missing > 0) {
    statusBadge = 'Needs Downloads';
    statusTone = 'danger';
  } else if (activeFailures > 0) {
    statusBadge = 'Has Failures';
    statusTone = 'danger';
  } else if (plexNeedsSync) {
    statusBadge = 'Needs Plex Sync';
    statusTone = 'warn';
  } else if (pipelineFailed) {
    statusBadge = 'Pipeline Failed';
    statusTone = 'danger';
  } else if (historicalFailures > 0 || review > 0) {
    statusBadge = 'Review Needed';
    statusTone = 'warn';
  }

  const statusText = interrupted
    ? 'interrupted'
    : waitingImport
      ? 'waiting import'
      : missing
        ? 'needs downloads'
        : activeFailures > 0
          ? 'has failures'
          : pipelineFailed
            ? 'pipeline failed'
            : plexNeedsSync
              ? 'needs Plex sync'
              : 'synced';
  const updatedAt = playlist.last_pipeline?.updated_at
    || playlist.checkpoint_updated_at
    || 0;
  const lastPipelineUpdated = formatPlaylistTime(playlist.last_pipeline?.updated_at).replace(/^updated /, '');
  const lastPipelineLabel = playlist.last_pipeline?.action || playlist.last_pipeline?.status
    ? `${statusLabel(playlist.last_pipeline?.action || 'pipeline')} · ${statusLabel(playlist.last_pipeline?.status || 'unknown')} · ${lastPipelineUpdated}`
    : 'No pipeline run recorded';
  const lastCheckpointLabel = playlist.checkpoint_updated_at
    ? `${statusLabel(playlist.checkpoint_status || playlist.checkpoint_phase || 'checkpoint')} · ${formatPlaylistTime(playlist.checkpoint_updated_at).replace(/^updated /, '')}`
    : 'No checkpoint';
  const lastError = playlist.last_pipeline?.error || (plexFailed ? (lastPlex.error || playlist.last_sync_error || '') : '');

  return {
    totalTracks,
    libraryMatched,
    matchPercent,
    missing,
    downloadedTotal,
    waitingImport,
    importedFromDownloads,
    failed,
    pipelineReview,
    qualityReview,
    review,
    removed,
    excluded,
    plexKnown,
    plexSynced,
    plexMatched,
    plexEligible,
    plexPending,
    plexMissing,
    plexNeedsSync,
    plexIssue,
    sourceLabel: formatSavedPlaylistSource(playlist.source),
    sourceStatus: statusText,
    updatedLabel: formatPlaylistTime(updatedAt),
    statusBadge,
    statusTone,
    nextStep,
    nextStepTone,
    nextStepKey,
    activeFailures,
    historicalFailures,
    interrupted,
    pipelineFailed,
    running,
    hasIssueAction: Boolean(failed || review || pipelineFailed || lastError || plexIssue || playlist.plex_synced === false),
    lastPipelineLabel,
    lastCheckpointLabel,
    lastError,
  };
}

function savedPlaylistMatchesFilter(summary: SavedPlaylistSummary, filter: SavedPlaylistFilter): boolean {
  if (filter === 'all') return true;
  if (filter === 'needs-action') return summary.nextStepKey !== 'all-good' || summary.pipelineFailed;
  if (filter === 'running') return summary.running;
  if (filter === 'missing') return summary.missing > 0;
  if (filter === 'waiting-import') return summary.waitingImport > 0;
  if (filter === 'review') return summary.activeFailures > 0 || summary.review > 0 || summary.pipelineFailed;
  if (filter === 'plex') return summary.plexNeedsSync;
  if (filter === 'clean') return summary.nextStepKey === 'all-good' && !summary.pipelineFailed;
  return true;
}

function savedPlaylistMatchesSearch(playlist: PlaylistEntry, summary: SavedPlaylistSummary, search: string): boolean {
  const needle = search.trim().toLowerCase();
  if (!needle) return true;
  return [
    playlist.name,
    playlist.playlist_id,
    playlist.source,
    playlist.desired_source,
    summary.sourceLabel,
    summary.statusBadge,
    summary.nextStep,
    summary.lastPipelineLabel,
    summary.lastCheckpointLabel,
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(needle));
}

function savedPlaylistFilterCount(metrics: SavedPlaylistMetrics, filter: SavedPlaylistFilter): number {
  if (filter === 'all') return metrics.total;
  if (filter === 'needs-action') return metrics.needsAction;
  if (filter === 'running') return metrics.running;
  if (filter === 'missing') return metrics.missing;
  if (filter === 'waiting-import') return metrics.waitingImport;
  if (filter === 'review') return metrics.review;
  if (filter === 'plex') return metrics.plex;
  if (filter === 'clean') return metrics.clean;
  return 0;
}

function SavedBadge({ label, tone = 'idle', title }: { label: string; tone?: SavedBadgeTone; title?: string }) {
  const toneClass = {
    ok: 'border-emerald-800 bg-emerald-950/40 text-emerald-300',
    warn: 'border-amber-800 bg-amber-950/40 text-amber-300',
    danger: 'border-rose-800 bg-rose-950/40 text-rose-300',
    idle: 'border-graphite-700 bg-graphite-950/50 text-zinc-400',
    info: 'border-sky-800 bg-sky-950/40 text-sky-300',
  }[tone];
  return (
    <span title={title} className={`inline-flex min-h-6 items-center rounded-full border px-2 py-0.5 text-[0.68rem] font-medium ${toneClass}`}>
      {label}
    </span>
  );
}

function savedTextTone(tone: SavedBadgeTone): string {
  if (tone === 'ok') return 'text-emerald-300';
  if (tone === 'warn') return 'text-amber-300';
  if (tone === 'danger') return 'text-rose-300';
  if (tone === 'info') return 'text-sky-300';
  return 'text-zinc-300';
}

function SavedPlaylistRowContext({ summary }: { summary: SavedPlaylistSummary }) {
  const activity = summary.lastPipelineLabel !== 'No pipeline run recorded'
    ? summary.lastPipelineLabel
    : summary.lastCheckpointLabel !== 'No checkpoint'
      ? summary.lastCheckpointLabel
      : 'No recent activity';
  const activityTone = summary.running
    ? 'text-sky-300'
    : summary.pipelineFailed || summary.lastError
      ? 'text-rose-300'
      : summary.interrupted
        ? 'text-amber-300'
        : 'text-zinc-400';

  return (
    <div className="mt-1 min-w-0 space-y-0.5 text-[0.68rem]">
      <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-center sm:gap-2">
        <span className="min-w-0 truncate">
          <span className="text-zinc-500">Next step: </span>
          <span className={`font-medium ${savedTextTone(summary.nextStepTone)}`}>{summary.nextStep}</span>
        </span>
        <span className="hidden text-zinc-700 sm:inline">/</span>
        <span className="min-w-0 truncate">
          <span className="text-zinc-500">Last activity: </span>
          <span className={activityTone}>{activity}</span>
        </span>
      </div>
      {summary.lastError ? (
        <div className="truncate text-rose-300" title={summary.lastError}>
          Issue: {summary.lastError}
        </div>
      ) : null}
    </div>
  );
}
function SavedPlaylistDetailMetric({
  label,
  value,
  tone = 'text-zinc-100',
}: {
  label: string;
  value: number | string;
  tone?: string;
}) {
  return (
    <div className="rounded border border-graphite-800 bg-graphite-950/50 px-3 py-2">
      <div className={`text-sm font-semibold tabular-nums ${tone}`}>{typeof value === 'number' ? value.toLocaleString() : value}</div>
      <div className="mt-1 text-[0.65rem] uppercase tracking-wide text-zinc-500">{label}</div>
    </div>
  );
}

function SavedPlaylistExpandedDetails({ summary }: { summary: SavedPlaylistSummary }) {
  return (
    <div className="border-t border-graphite-800 bg-graphite-950/30 px-3 py-3">
      <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-6">
        <SavedPlaylistDetailMetric label="Total playlist tracks" value={summary.totalTracks} />
        <SavedPlaylistDetailMetric label="In Beets library" value={summary.libraryMatched} tone="text-emerald-300" />
        <SavedPlaylistDetailMetric label="Missing from Beets" value={summary.missing} tone={summary.missing ? 'text-rose-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Downloaded total" value={summary.downloadedTotal} tone={summary.downloadedTotal ? 'text-sky-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Downloaded waiting import" value={summary.waitingImport} tone={summary.waitingImport ? 'text-amber-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Imported from downloads" value={summary.importedFromDownloads} tone={summary.importedFromDownloads ? 'text-emerald-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Active failures" value={summary.activeFailures} tone={summary.activeFailures ? 'text-rose-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Historical failures" value={summary.historicalFailures} tone={summary.historicalFailures ? 'text-zinc-400' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Review needed" value={summary.review} tone={summary.review ? 'text-amber-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Removed" value={summary.removed} tone={summary.removed ? 'text-amber-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Excluded" value={summary.excluded} tone={summary.excluded ? 'text-amber-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Plex matched" value={summary.plexKnown ? `${summary.plexMatched.toLocaleString()}/${summary.plexEligible.toLocaleString()}` : 'Not synced yet'} tone={summary.plexNeedsSync ? 'text-amber-300' : 'text-emerald-300'} />
        <SavedPlaylistDetailMetric label="Pending Plex match" value={summary.plexKnown ? summary.plexPending : '-'} tone={summary.plexPending ? 'text-amber-300' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Last pipeline run" value={summary.lastPipelineLabel} tone="text-zinc-300" />
        <SavedPlaylistDetailMetric label="Last checkpoint" value={summary.lastCheckpointLabel} tone="text-zinc-300" />
      </div>
      {summary.lastError ? (
        <div className="mt-3 rounded border border-rose-900/70 bg-rose-950/20 px-3 py-2">
          <div className="text-[0.65rem] font-semibold uppercase tracking-wide text-rose-300">Last error</div>
          <div className="mt-1 whitespace-normal break-words text-xs text-rose-100">{summary.lastError}</div>
        </div>
      ) : null}
      {summary.plexIssue && summary.plexIssue !== summary.lastError ? (
        <div className="mt-3 rounded border border-amber-900/70 bg-amber-950/20 px-3 py-2">
          <div className="text-[0.65rem] font-semibold uppercase tracking-wide text-amber-300">Plex issue</div>
          <div className="mt-1 whitespace-normal break-words text-xs text-amber-100">{summary.plexIssue}</div>
        </div>
      ) : null}
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
        <DialogPanel className="w-full max-w-md rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
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

function TrackList({
  title,
  tone,
  empty,
  children,
}: {
  title: string;
  tone: string;
  empty: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-0">
      <div className={`mb-2 text-[0.72rem] font-semibold uppercase tracking-wide ${tone}`}>
        {title}
      </div>
      <div className="max-h-80 overflow-auto rounded border border-graphite-800 bg-graphite-950/40">
        {empty ? <div className="px-4 py-5 text-sm text-zinc-500">{children}</div> : children}
      </div>
    </div>
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
    ? 'border-rose-900 bg-rose-950/40 text-rose-300'
    : doneSeverity === 'warning'
      ? 'border-amber-900 bg-amber-950/40 text-amber-300'
      : doneSeverity === 'info'
        ? 'border-sky-900 bg-sky-950/40 text-sky-300'
      : 'border-emerald-900 bg-emerald-950/40 text-emerald-300';

  return (
    <div className="rounded border border-graphite-800 bg-graphite-950/40 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium text-zinc-200">
          {downloadState?.status === 'done'
            ? doneLabel
            : downloadState?.status === 'error'
              ? 'Playlist job failed'
              : downloadState?.current || 'Playlist job running'}
        </div>
        <div className="text-xs text-zinc-500">
          {detail}
        </div>
      </div>
      {downloadError ? <Alert severity="error" sx={{ mb: 2 }}>{downloadError}</Alert> : null}
      {downloadState ? (
        <div className="mb-3 space-y-2">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
            {metric('Available', available, 'text-emerald-300')}
            {metric('Missing', missing, missing ? 'text-rose-300' : 'text-zinc-100')}
            {metric('Waiting Import', downloaded, downloaded ? 'text-sky-300' : 'text-zinc-100')}
            {metric('Failed', failed, failed ? 'text-rose-300' : 'text-zinc-100')}
            {metric('Round', downloadState.round ? `${downloadState.round}/${downloadState.max_rounds || '?'}` : '-', 'text-red-300')}
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
        <div className="mb-3 max-h-52 overflow-auto rounded border border-graphite-800 bg-graphite-950/50">
          <div className="grid grid-cols-[minmax(0,1fr)_8rem_8rem] border-b border-graphite-800 px-3 py-2 text-[0.7rem] font-semibold uppercase tracking-wide text-zinc-500">
            <span>Track</span>
            <span>Source</span>
            <span>Status</span>
          </div>
          {trackStatuses.map((row) => (
            <div key={row.id} className="grid grid-cols-[minmax(0,1fr)_8rem_8rem] items-start gap-2 border-t border-graphite-800 px-3 py-2 text-xs first:border-t-0">
              <div className="min-w-0">
                <div className="truncate text-zinc-200">{[row.artist, row.title].filter(Boolean).join(' - ')}</div>
                {canonicalNote(row) ? <div className="truncate text-sky-300">{canonicalNote(row)}</div> : null}
                {row.message ? <div className="truncate text-zinc-500">{row.message}</div> : null}
              </div>
              <span className="truncate text-zinc-400">{row.method || '-'}</span>
              <span className={`rounded border px-2 py-0.5 text-center ${statusTone(row.status)}`}>
                {statusLabel(row.status)}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="rounded border border-graphite-800 bg-graphite-950/40 px-3 py-2 text-xs text-zinc-500">
        Live log output is shown in the top pipeline bar. Open Jobs for the full persisted job log.
      </div>
      {downloadState?.status === 'done' && playlistStatusMessage(downloadState.playlist) ? (
        <div className={`mt-3 rounded border px-3 py-2 text-sm ${doneNoticeTone}`}>
          {playlistStatusMessage(downloadState.playlist)}
          {downloadState.missing_after_import ? ` · ${downloadState.missing_after_import} still missing` : ''}
        </div>
      ) : null}
    </div>
  );
}

export default function Playlists() {
  const [playlists, setPlaylists] = useState<PlaylistEntry[]>([]);
  const [loadingPlaylists, setLoadingPlaylists] = useState(true);
  const [playlistError, setPlaylistError] = useState('');
  const [playlistSyncStatus, setPlaylistSyncStatus] = useState<PlaylistSyncStatusResponse | null>(null);
  const [playlistSyncNotice, setPlaylistSyncNotice] = useState<Notice | null>(null);
  const [loadingPlaylistDetails, setLoadingPlaylistDetails] = useState('');
  const [loadingPlaylistRows, setLoadingPlaylistRows] = useState<TrackGroupId | ''>('');
  const [trackPageHasMore, setTrackPageHasMore] = useState<Partial<Record<TrackGroupId, boolean>>>({});
  const [playlistDetailsError, setPlaylistDetailsError] = useState('');

  const [name, setName] = useState('');
  const [source, setSource] = useState<PlaylistSource>('url');
  const [url, setUrl] = useState('');
  const [trackText, setTrackText] = useState('');
  const [parsing, setParsing] = useState(false);
  const [parseError, setParseError] = useState('');
  const [parseResult, setParseResult] = useState<PlaylistParseResponse | null>(null);

  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [confirmDownload, setConfirmDownload] = useState(false);
  const [downloadJobId, setDownloadJobId] = useState(() => localStorage.getItem(PLAYLIST_JOB_STORAGE_KEY) ?? '');
  const [lastDownloadJobId, setLastDownloadJobId] = useState(() => localStorage.getItem(PLAYLIST_LAST_JOB_STORAGE_KEY) ?? '');
  const [downloadState, setDownloadState] = useState<PlaylistDownloadStatusResponse | null>(null);
  const [downloadError, setDownloadError] = useState('');
  const [startingDownload, setStartingDownload] = useState(false);
  const [downloadMethod, setDownloadMethod] = useState('auto');
  const [resolveDraft, setResolveDraft] = useState<ResolveDraft | null>(null);
  const [resolvingKey, setResolvingKey] = useState('');
  const [suggestionRows, setSuggestionRows] = useState<PlaylistSuggestionRow[]>([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [applyingSuggestions, setApplyingSuggestions] = useState(false);
  const [repairingQuality, setRepairingQuality] = useState(false);
  const [qualityPlaceDraft, setQualityPlaceDraft] = useState<QualityPlaceDraft | null>(null);
  const [placingQuality, setPlacingQuality] = useState(false);
  const [pipelineJobId, setPipelineJobId] = useState('');
  const [pipelineJob, setPipelineJob] = useState<JobResponse | null>(null);
  const [pipelineActionBusy, setPipelineActionBusy] = useState('');
  const [savedRowActionBusy, setSavedRowActionBusy] = useState('');
  const [expandedPlaylistRows, setExpandedPlaylistRows] = useState<Record<string, boolean>>({});
  const [savedPlaylistFilter, setSavedPlaylistFilter] = useState<SavedPlaylistFilter>('all');
  const [savedPlaylistSearch, setSavedPlaylistSearch] = useState('');
  const [trackActionKey, setTrackActionKey] = useState('');
  const [logExpanded, setLogExpanded] = useState(false);
  const [importExpanded, setImportExpanded] = useState(false);
  const [trackGroup, setTrackGroup] = useState<TrackGroupId>('available');

  const detected = platformLabel(url);
  const content = source === 'url' ? url.trim() : trackText.trim();
  const matched = parseResult?.matched ?? [];
  const missing = parseResult?.missing ?? [];
  const allTracks = useMemo(() => allTracksFromResult(parseResult), [parseResult]);
  const qualityRows = useMemo(
    () => matched.filter((track) => track.quality && track.quality !== 'ok'),
    [matched],
  );
  const qualityRepairRows = useMemo(
    () => qualityRows.filter((track) => track.quality !== 'bad' && track.id > 0),
    [qualityRows],
  );
  const suggestionsByTrack = useMemo(() => suggestionRowsByTrack(suggestionRows), [suggestionRows]);
  const safeSuggestionCount = suggestionRows.filter((row) => row.best?.safe).length;
  const savedPlaylistName = (parseResult as { name?: string } | null)?.name || '';
  const viewingSavedPlaylist = Boolean(
    (parseResult as { m3u?: string; manifest?: string } | null)?.m3u
      || (parseResult as { m3u?: string; manifest?: string } | null)?.manifest,
  );
  const savedDetail = viewingSavedPlaylist ? parseResult as PlaylistDetailResponse : null;
  const removedExcluded = savedDetail?.removed_excluded ?? [];
  const pipelineCounts = savedDetail?.counts ?? {};
  const selectedPlaylist = playlists.find((playlist) => playlist.name === savedPlaylistName);
  const fallbackResumablePlaylist = useMemo(
    () => playlists.find((playlist) => playlistHasResumableCheckpoint(null, playlist)),
    [playlists],
  );
  const activePlaylist = selectedPlaylist || (!savedPlaylistName ? fallbackResumablePlaylist : undefined);
  const savedPlaylistRows = useMemo<SavedPlaylistRow[]>(
    () => playlists.map((playlist) => ({ playlist, summary: summarizeSavedPlaylist(playlist) })),
    [playlists],
  );
  const savedPlaylistMetrics = useMemo<SavedPlaylistMetrics>(() => savedPlaylistRows.reduce((metrics, row) => ({
    total: metrics.total + 1,
    needsAction: metrics.needsAction + (row.summary.nextStepKey !== 'all-good' || row.summary.pipelineFailed ? 1 : 0),
    running: metrics.running + (row.summary.running ? 1 : 0),
    missing: metrics.missing + (row.summary.missing > 0 ? 1 : 0),
    waitingImport: metrics.waitingImport + (row.summary.waitingImport > 0 ? 1 : 0),
    review: metrics.review + (row.summary.activeFailures > 0 || row.summary.review > 0 || row.summary.pipelineFailed ? 1 : 0),
    plex: metrics.plex + (row.summary.plexNeedsSync ? 1 : 0),
    clean: metrics.clean + (row.summary.nextStepKey === 'all-good' && !row.summary.pipelineFailed ? 1 : 0),
    tracks: metrics.tracks + row.summary.totalTracks,
    available: metrics.available + row.summary.libraryMatched,
  }), {
    total: 0,
    needsAction: 0,
    running: 0,
    missing: 0,
    waitingImport: 0,
    review: 0,
    plex: 0,
    clean: 0,
    tracks: 0,
    available: 0,
  }), [savedPlaylistRows]);
  const filteredSavedPlaylistRows = useMemo(
    () => savedPlaylistRows.filter(({ playlist, summary }) => (
      savedPlaylistMatchesFilter(summary, savedPlaylistFilter)
      && savedPlaylistMatchesSearch(playlist, summary, savedPlaylistSearch)
    )),
    [savedPlaylistRows, savedPlaylistFilter, savedPlaylistSearch],
  );
  const savedPlaylistCoveragePercent = savedPlaylistMetrics.tracks
    ? Math.round((savedPlaylistMetrics.available / savedPlaylistMetrics.tracks) * 100)
    : 0;
  const downloadRunning = Boolean(downloadJobId) || startingDownload;
  const pipelineRunning = downloadRunning || pipelineJob?.status === 'running' || Boolean(pipelineActionBusy);
  const canSave = Boolean(name.trim() && allTracks.length && !saving);
  const canDownload = Boolean((allTracks.length || content) && !downloadRunning);
  const downloadNeedsPull = Boolean(!allTracks.length && content);
  const hasPartialTrackRows = Boolean(matched.length || missing.length || removedExcluded.length);
  const partialDetailRows = Boolean(savedDetail?.partial_tracks_loaded);
  const savedDetailRowsLoaded = !viewingSavedPlaylist || savedDetail?.tracks_loaded !== false || partialDetailRows || hasPartialTrackRows;
  const matchedCount = partialDetailRows
    ? (savedDetail?.available ?? activePlaylist?.available ?? matched.length)
    : (matched.length || savedDetail?.available || activePlaylist?.available || 0);
  const missingCount = partialDetailRows
    ? (savedDetail?.missing_count ?? activePlaylist?.missing ?? missing.length)
    : (missing.length || savedDetail?.missing_count || activePlaylist?.missing || 0);

  const loadPlaylists = useCallback(async () => {
    setLoadingPlaylists(true);
    setPlaylistError('');
    try {
      const response = await getPlaylists();
      setPlaylists(response.playlists ?? []);
    } catch (err) {
      setPlaylistError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingPlaylists(false);
    }
  }, []);

  const loadPlaylistSyncStatus = useCallback(async () => {
    try {
      setPlaylistSyncStatus(await getPlaylistSyncStatus());
    } catch {
      setPlaylistSyncStatus(null);
    }
  }, []);

  useEffect(() => {
    void loadPlaylists();
    void loadPlaylistSyncStatus();
  }, [loadPlaylists, loadPlaylistSyncStatus]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadPlaylistSyncStatus();
    }, PLAYLIST_SYNC_STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadPlaylistSyncStatus]);

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
        if (state.status !== 'error' && (state.tracks?.length || state.matched?.length || state.missing?.length)) {
          setParseResult((current) => ({
            ...current,
            ok: true,
            tracks: state.tracks ?? current?.tracks ?? [],
            matched: state.matched ?? current?.matched ?? [],
            missing: state.missing ?? current?.missing ?? [],
            total: state.tracks?.length ?? ((state.matched?.length ?? 0) + (state.missing?.length ?? 0)),
          }));
        }
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
        if (state.status !== 'error' && (state.tracks?.length || state.matched?.length || state.missing?.length)) {
          setParseResult((current) => ({
            ...current,
            ok: true,
            tracks: state.tracks ?? current?.tracks ?? [],
            matched: state.matched ?? current?.matched ?? [],
            missing: state.missing ?? current?.missing ?? [],
            total: state.tracks?.length ?? ((state.matched?.length ?? 0) + (state.missing?.length ?? 0)),
          }));
        }
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
            setParseResult((current) => {
              return {
                ...current,
                ok: true,
                tracks: state.tracks ?? current?.tracks ?? [],
                matched: state.matched ?? current?.matched ?? [],
                missing: state.missing ?? current?.missing ?? [],
                total: (state.matched_after_import ?? state.matched?.length ?? current?.matched.length ?? 0)
                  + (state.missing_after_import ?? state.missing?.length ?? current?.missing.length ?? 0),
              };
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
        setDownloadError(err instanceof Error ? err.message : String(err));
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
        if (savedPlaylistName) {
          const detail = await getPlaylistDetails(savedPlaylistName, { mode: 'summary' });
          if (!cancelled) setParseResult(detail);
        }
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
        if (!cancelled) setPlaylistDetailsError(err instanceof Error ? err.message : String(err));
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [pipelineJobId, savedPlaylistName, loadPlaylists]);

  const clearDownloadPanel = useCallback(() => {
    setDownloadJobId('');
    setLastDownloadJobId('');
    setDownloadState(null);
    setDownloadError('');
    localStorage.removeItem(PLAYLIST_JOB_STORAGE_KEY);
    localStorage.removeItem(PLAYLIST_LAST_JOB_STORAGE_KEY);
  }, []);

  const handleParse = async () => {
    if (!content) {
      setParseError(source === 'url' ? 'Paste a playlist URL first.' : 'Paste at least one track first.');
      return;
    }
    setParsing(true);
    setParseError('');
    setNotice(null);
    setSuggestionRows([]);
    setDownloadState(null);
    setDownloadError('');
    setDownloadJobId('');
    setLastDownloadJobId('');
    localStorage.removeItem(PLAYLIST_JOB_STORAGE_KEY);
    localStorage.removeItem(PLAYLIST_LAST_JOB_STORAGE_KEY);
    try {
      const result = await parsePlaylist(source, content);
      setParseResult(result);
      if (!name.trim() && source === 'url') {
        const fallback = detected.label === 'Paste a URL' || detected.label === 'URL' ? 'Playlist' : `${detected.label} Playlist`;
        setName(fallback);
      }
    } catch (err) {
      setParseError(err instanceof Error ? err.message : String(err));
    } finally {
      setParsing(false);
    }
  };

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setNotice(null);
    try {
      const result = await createPlaylist(name.trim(), matched, allTracks, missing, source, content);
      setNotice({ severity: playlistStatusSeverity(result), message: playlistStatusMessage(result) });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setSaving(false);
    }
  };

  const handleDeletePlaylist = async (playlist: PlaylistEntry) => {
    const ok = window.confirm(
      `Delete the saved playlist "${playlist.name}" from M3U/Plex only? Songs in Beets will not be deleted.`,
    );
    if (!ok) return;
    setPlaylistSyncNotice(null);
    setPlaylistDetailsError('');
    try {
      const result = await deletePlaylist(playlist.name, true);
      const plexNote = result.plex_error
        ? ` Plex delete error: ${result.plex_error}`
        : result.plex_deleted
          ? ` Deleted ${result.plex_deleted} Plex playlist(s).`
          : '';
      setPlaylistSyncNotice({
        severity: 'success',
        message: `Deleted playlist ${playlist.name}. Library songs were not deleted.${plexNote}`,
      });
      if (name === playlist.name) {
        setParseResult(null);
      }
      await loadPlaylists();
    } catch (err) {
      setPlaylistDetailsError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleViewPlaylist = async (playlist: PlaylistEntry) => {
    setLoadingPlaylistDetails(playlist.name);
    setPlaylistDetailsError('');
    setNotice(null);
    clearDownloadPanel();
    setSuggestionRows([]);
    try {
      const result = await getPlaylistDetails(playlist.name, { mode: 'summary' });
      setName(result.name || playlist.name);
      setParseResult(result);
      setTrackPageHasMore({});
      const activeJobId = result.last_pipeline?.jobs_job_id || '';
      if (activeJobId && result.last_pipeline?.status === 'running') {
        setPipelineJobId(activeJobId);
      } else {
        setPipelineJobId('');
        setPipelineJob(null);
      }
      const m3uNote =
        result.m3u_tracks !== undefined && result.m3u_tracks !== result.total
          ? ` M3U has ${result.m3u_tracks.toLocaleString()} saved.`
          : '';
      const checkpointNote = playlistHasResumableCheckpoint(result, playlist)
        ? ` Resumable checkpoint is ready.`
        : '';
      setNotice({
        severity: 'info',
        message: `${result.name || playlist.name}: ${(result.available ?? result.matched.length).toLocaleString()} available, ${(result.missing_count ?? result.missing.length).toLocaleString()} missing.${m3uNote}${checkpointNote}`,
      });
    } catch (err) {
      setPlaylistDetailsError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingPlaylistDetails('');
    }
  };

  const isPendingPlexTrack = (track: PlaylistTrack): boolean => (
    track.pipeline_status === 'plex_pending' || track.retry_action === 'retry_pending_plex_match'
  );

  const currentRowsForGroup = (group: TrackGroupId): PlaylistTrack[] => {
    if (group === 'available') return matched;
    if (group === 'removed') return removedExcluded;
    if (group === 'waiting') return missing.filter(isWaitingImportTrack);
    if (group === 'failed') return missing.filter(isFailedReviewTrack);
    if (group === 'pending_plex') return missing.filter(isPendingPlexTrack);
    return missing.filter((track) => !isWaitingImportTrack(track) && !isFailedReviewTrack(track) && !isPendingPlexTrack(track));
  };

  const handleLoadPlaylistRows = async (group: TrackGroupId = trackGroup) => {
    if (!savedPlaylistName) return;
    const offset = currentRowsForGroup(group).length;
    setLoadingPlaylistRows(group);
    setPlaylistDetailsError('');
    try {
      const result = await getPlaylistRows(savedPlaylistName, {
        group,
        offset,
        limit: PLAYLIST_ROW_PAGE_SIZE,
      });
      const rows = result.rows ?? [];
      setParseResult((current) => {
        if (!current) return current;
        const detail = current as PlaylistDetailResponse;
        const next: PlaylistDetailResponse = {
          ...detail,
          ...(result.summary ?? {}),
          tracks: detail.tracks ?? [],
          matched: detail.matched ?? [],
          missing: detail.missing ?? [],
          removed_excluded: detail.removed_excluded ?? [],
          tracks_loaded: false,
          partial_tracks_loaded: true,
        };
        if (group === 'available') {
          next.matched = [...(detail.matched ?? []), ...(rows as PlaylistMatchedTrack[])];
        } else if (group === 'removed') {
          next.removed_excluded = [...(detail.removed_excluded ?? []), ...(rows as PlaylistTrack[])];
        } else {
          next.missing = [...(detail.missing ?? []), ...(rows as PlaylistTrack[])];
        }
        return next;
      });
      setTrackPageHasMore((current) => ({ ...current, [group]: Boolean(result.has_more) }));
      setNotice({
        severity: 'info',
        message: rows.length
          ? `Loaded ${rows.length.toLocaleString()} ${TRACK_GROUP_LABELS[group].toLowerCase()} row(s).`
          : `No ${TRACK_GROUP_LABELS[group].toLowerCase()} rows found.`,
      });
    } catch (err) {
      setPlaylistDetailsError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingPlaylistRows('');
    }
  };

  const handlePipelineAction = async (action: PlaylistPipelineAction) => {
    if (!savedPlaylistName) return;
    setPipelineActionBusy(action);
    setNotice(null);
    setPlaylistDetailsError('');
    try {
      const started = await runPlaylistPipelineAction(savedPlaylistName, action);
      if (action === 'clear') {
        clearDownloadPanel();
        setPipelineJobId('');
        setPipelineJob(null);
      } else if (action === 'pause' || action === 'stop') {
        setDownloadJobId('');
        setPipelineJobId('');
        setNotice({ severity: 'info', message: `Playlist pipeline ${action === 'pause' ? 'paused at its checkpoint' : 'stopped'}.` });
      } else if (action === 'download-missing' || action === 'run-full' || action === 'resume') {
        if (started.job_id) {
          setDownloadJobId(started.job_id);
          setLastDownloadJobId(started.job_id);
          localStorage.setItem(PLAYLIST_JOB_STORAGE_KEY, started.job_id);
          localStorage.setItem(PLAYLIST_LAST_JOB_STORAGE_KEY, started.job_id);
        }
      } else if (started.jobs_job_id || started.job_id) {
        const jobId = started.jobs_job_id || started.job_id || '';
        setPipelineJobId(jobId);
        setPipelineJob({ ok: true, job_id: jobId, status: 'running', log: ['(starting...)'] });
      }
      if (!['pause', 'stop', 'clear'].includes(action)) {
        setNotice({ severity: 'info', message: `${action.replace(/-/g, ' ')} started for ${savedPlaylistName}.` });
      }
      if (action === 'clear') {
        const detail = await getPlaylistDetails(savedPlaylistName, { mode: 'summary' });
        setParseResult(detail);
      }
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setPipelineActionBusy('');
    }
  };

  const handleTrackAction = async (
    action: TrackAction,
    track: PlaylistTrack,
  ) => {
    if (!savedPlaylistName) return;
    if (action === 'delete_staged') {
      const ok = window.confirm(
        'Delete this downloaded staging file? The Beets music library copy will not be deleted.',
      );
      if (!ok) return;
    }
    const key = `${action}:${trackKey(track)}`;
    setTrackActionKey(key);
    setNotice(null);
    try {
      const result = await applyPlaylistTrackAction(savedPlaylistName, action, track);
      setParseResult(result.playlist);
      if (result.job?.job_id && (action === 'retry_download')) {
        setDownloadJobId(result.job.job_id);
        setLastDownloadJobId(result.job.job_id);
      } else if (result.job?.jobs_job_id) {
        setPipelineJobId(result.job.jobs_job_id);
      }
      setNotice({
        severity: result.retry_error ? 'error' : 'success',
        message: result.retry_error || `${action.replace(/_/g, ' ')} completed.`,
      });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setTrackActionKey('');
    }
  };

  const handleStartDownload = async (
    requestedTracks: PlaylistTrack[] = missing,
    playlistTracks: PlaylistTrack[] = allTracks,
  ) => {
    if (!playlistTracks.length && !requestedTracks.length && !content) return;
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
      download_methods: downloadMethod === 'auto' ? ['slskd', 'spotiflac', 'ytdlp', 'soundcloud'] : [downloadMethod],
    });
    try {
      const started = await startPlaylistDownload({
        name: name.trim() || 'Playlist',
        tracks: useParsedTracks ? tracksToDownload : undefined,
        all_tracks: useParsedTracks ? tracksForPlaylist : undefined,
        source: useParsedTracks ? undefined : source,
        content: useParsedTracks ? undefined : content,
        methods: downloadMethod === 'auto' ? undefined : [downloadMethod],
        sync_after_import: true,
      });
      localStorage.setItem(PLAYLIST_JOB_STORAGE_KEY, started.job_id);
      localStorage.setItem(PLAYLIST_LAST_JOB_STORAGE_KEY, started.job_id);
      setLastDownloadJobId(started.job_id);
      setDownloadJobId(started.job_id);
      if (started.resumed) {
        setNotice({ severity: 'info', message: 'Resumed the existing playlist download job.' });
      }
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : String(err));
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

  const handleIgnoreMissing = (track: PlaylistTrack) => {
    setParseResult((current) => {
      if (!current) return current;
      const removeKey = trackKey(track);
      const nextMissing = (current.missing ?? []).filter((item) => trackKey(item) !== removeKey);
      const nextTracks = (current.tracks ?? []).filter((item) => trackKey(item) !== removeKey);
      return {
        ...current,
        tracks: nextTracks,
        missing: nextMissing,
        total: nextTracks.length || ((current.matched?.length ?? 0) + nextMissing.length),
      };
    });
  };

  const handleBeginResolve = (track: PlaylistTrack, key: string) => {
    setResolveDraft({
      key,
      track,
      artist: track.artist || '',
      title: track.title || '',
    });
  };

  const handleSaveResolve = async () => {
    if (!resolveDraft || !savedPlaylistName) return;
    const artist = resolveDraft.artist.trim();
    const title = resolveDraft.title.trim();
    if (!title) {
      setNotice({ severity: 'error', message: 'A title is required to resolve a missing playlist row.' });
      return;
    }
    setResolvingKey(resolveDraft.key);
    setNotice(null);
    try {
      const result = await resolvePlaylistTrack(savedPlaylistName, resolveDraft.track, { artist, title });
      setParseResult(result);
      setResolveDraft(null);
      setNotice({
        severity: 'success',
        message: `Updated ${artist ? `${artist} - ` : ''}${title}. ${result.matched.length.toLocaleString()} available, ${result.missing.length.toLocaleString()} missing.`,
      });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setResolvingKey('');
    }
  };

  const handleLoadSuggestions = async () => {
    if (!savedPlaylistName) return;
    setLoadingSuggestions(true);
    setNotice(null);
    try {
      const result = await getPlaylistSuggestions(savedPlaylistName);
      setSuggestionRows(result.rows ?? []);
      setNotice({
        severity: 'info',
        message: `${result.safe_count.toLocaleString()} safe suggestion(s) found for ${result.total_missing.toLocaleString()} missing track(s).`,
      });
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setLoadingSuggestions(false);
    }
  };

  const handleApplySafeSuggestions = async () => {
    if (!savedPlaylistName) return;
    setApplyingSuggestions(true);
    setNotice(null);
    try {
      const result = await applySafePlaylistSuggestions(savedPlaylistName);
      setParseResult(result);
      setSuggestionRows([]);
      setResolveDraft(null);
      setNotice({
        severity: 'success',
        message: `Applied ${(result.resolved_count ?? 0).toLocaleString()} safe suggestion(s). ${result.matched.length.toLocaleString()} available, ${result.missing.length.toLocaleString()} missing.`,
      });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setApplyingSuggestions(false);
    }
  };

  const handleRepairQualityRows = async () => {
    const itemIds = qualityRepairRows.map((track) => track.id).filter((id) => id > 0);
    if (!itemIds.length) {
      setNotice({ severity: 'info', message: 'No repairable playlist quality rows are selected.' });
      return;
    }
    const ok = window.confirm(
      `Queue playlist quality repair for ${itemIds.length} review row(s)? The job will write tags and move repaired tracks into normal album folders.`,
    );
    if (!ok) return;
    setRepairingQuality(true);
    setNotice(null);
    try {
      const result = await cleanupPlaylistQuality({
        dry_run: false,
        action: 'repair',
        filter: 'repair',
        item_ids: itemIds,
        limit: Math.max(50, itemIds.length),
      });
      if (result.queued && result.job_id) {
        setNotice({
          severity: 'success',
          message: `Queued playlist quality repair for ${itemIds.length.toLocaleString()} row(s). Track progress on Jobs: ${result.job_id}`,
        });
      } else {
        setNotice({
          severity: 'info',
          message: 'No matching repair rows were queued. Refresh the playlist after current jobs finish.',
        });
      }
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setRepairingQuality(false);
    }
  };

  const handleBeginQualityPlace = (track: PlaylistMatchedTrack) => {
    setQualityPlaceDraft(qualityPlaceDraftForTrack(track));
    setNotice(null);
  };

  const updateQualityPlaceDraft = (field: keyof Omit<QualityPlaceDraft, 'itemId'>, value: string) => {
    setQualityPlaceDraft((draft) => (draft ? { ...draft, [field]: value } : draft));
  };

  const handleQueueQualityPlace = async () => {
    if (!qualityPlaceDraft) return;
    if (!qualityPlaceDraft.artist.trim() || !qualityPlaceDraft.title.trim() || !qualityPlaceDraft.albumartist.trim() || !qualityPlaceDraft.album.trim()) {
      setNotice({ severity: 'error', message: 'Artist, title, album artist, and album are required to place a review row.' });
      return;
    }
    setPlacingQuality(true);
    setNotice(null);
    try {
      const result = await placePlaylistQuality({
        item_id: qualityPlaceDraft.itemId,
        playlist: savedPlaylistName || undefined,
        placement: {
          artist: qualityPlaceDraft.artist.trim(),
          title: qualityPlaceDraft.title.trim(),
          albumartist: qualityPlaceDraft.albumartist.trim(),
          album: qualityPlaceDraft.album.trim(),
          year: optionalNumber(qualityPlaceDraft.year),
          track: optionalNumber(qualityPlaceDraft.track),
          disc: optionalNumber(qualityPlaceDraft.disc),
          tracktotal: optionalNumber(qualityPlaceDraft.tracktotal),
          disctotal: optionalNumber(qualityPlaceDraft.disctotal),
          mb_trackid: qualityPlaceDraft.mbTrackId.trim() || undefined,
          mb_albumid: qualityPlaceDraft.mbAlbumId.trim() || undefined,
          mb_releasegroupid: qualityPlaceDraft.mbReleaseGroupId.trim() || undefined,
        },
      });
      setQualityPlaceDraft(null);
      setNotice({
        severity: 'success',
        message: `Queued manual placement job ${result.job_id}. Track progress on Jobs; the playlist will sync after the file moves.`,
      });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setPlacingQuality(false);
    }
  };

  const downloadRound = downloadState?.round
    ? `round ${downloadState.round}${downloadState.max_rounds ? `/${downloadState.max_rounds}` : ''}`
    : '';
  const downloadProgress = downloadState?.total
    ? `${downloadState.done ?? 0} / ${downloadState.total}${downloadState.failed ? ` (${downloadState.failed} failed)` : ''}`
    : downloadState?.matched_after_import !== undefined
      ? `${downloadState.matched_after_import} matched`
      : '';
  const downloadStatusDetail = [downloadState?.phase, downloadRound, downloadProgress].filter(Boolean).join(' · ');
  const pipelineStatus = downloadState?.status
    || pipelineJob?.status
    || savedDetail?.last_pipeline?.status
    || activePlaylist?.last_pipeline?.status
    || 'idle';
  const pipelineAction = downloadState?.phase
    || savedDetail?.last_pipeline?.action
    || activePlaylist?.last_pipeline?.action
    || 'none';
  const pipelineLog = downloadState?.log ?? pipelineJob?.log ?? [];
  const pipelineIsActive = pipelineStatus === 'running' || pipelineRunning;
  const downloadedWaitingFromCounts = (pipelineCounts.downloaded ?? 0) + (pipelineCounts.waiting_import ?? 0);
  const downloadedWaitingCount = downloadedWaitingFromCounts
    || savedDetail?.checkpoint_waiting_for_import
    || activePlaylist?.checkpoint_waiting_for_import
    || activePlaylist?.downloaded
    || 0;
  const failedReviewCount = (pipelineCounts.failed ?? activePlaylist?.failed ?? 0)
    + (pipelineCounts.review_required ?? activePlaylist?.review_required ?? 0)
    + qualityRows.length;
  const removedExcludedCount = (pipelineCounts.removed ?? activePlaylist?.removed ?? 0)
    + (pipelineCounts.excluded ?? activePlaylist?.excluded ?? 0);
  const plexSyncedCount = pipelineCounts.plex_synced
    ?? activePlaylist?.plex_synced_count
    ?? savedDetail?.last_plex?.verified_count
    ?? savedDetail?.last_plex?.existing_playlist_count
    ?? activePlaylist?.plex_tracks
    ?? savedDetail?.last_plex?.tracks_added
    ?? savedDetail?.last_plex?.tracks_matched
    ?? 0;
  const hasResumablePipeline = playlistHasResumableCheckpoint(savedDetail, activePlaylist);
  const mainPipelineAction: PlaylistPipelineAction = hasResumablePipeline ? 'resume' : 'run-full';
  const mainPipelineLabel = hasResumablePipeline ? 'Resume Pipeline' : 'Run Pipeline';
  const lastCheckpointLabel = savedDetail?.checkpoint_updated_at
    ? new Date(savedDetail.checkpoint_updated_at * 1000).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })
    : activePlaylist?.checkpoint_updated_at
      ? new Date((activePlaylist?.checkpoint_updated_at || 0) * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      })
      : '-';
  const nextPipelineAction = pipelineIsActive
    ? (downloadState?.current || pipelineAction.replace(/_/g, ' '))
    : hasResumablePipeline
      ? 'Reconcile, import staged downloads, then continue missing tracks'
      : downloadedWaitingCount
        ? 'Import downloaded tracks'
        : missingCount
          ? 'Download missing tracks'
          : matchedCount
            ? 'Sync playlist to Plex'
            : 'Select a saved playlist';
  const pipelineMoreActions: MenuAction[] = [
    { label: 'Sync Sources', onClick: () => void handlePipelineAction('sync-sources') },
    { label: 'Download Missing Only', onClick: () => void handlePipelineAction('download-missing'), disabled: !missingCount },
    { label: 'Import Downloaded Only', onClick: () => void handlePipelineAction('import-downloaded'), disabled: !downloadedWaitingCount },
    { label: 'Sync to Plex Only', onClick: () => void handlePipelineAction('sync-plex'), disabled: !matchedCount },
    { label: 'Reconcile State', onClick: () => void handlePipelineAction('reconcile-state') },
    { label: 'Clear Job Status', onClick: () => void handlePipelineAction('clear'), danger: true },
  ];
  const waitingImportTracks = missing.filter(isWaitingImportTrack);
  const failedReviewTracks = missing.filter(isFailedReviewTrack);
  const pendingPlexTracks = missing.filter(isPendingPlexTrack);
  const missingOnlyTracks = missing.filter((track) => !isWaitingImportTrack(track) && !isFailedReviewTrack(track) && !isPendingPlexTrack(track));
  const selectedPlaylistTotal = parseResult?.total ?? activePlaylist?.tracks ?? matchedCount + missingCount;
  const pipelineStateLabel = pipelineIsActive
    ? 'running'
    : hasResumablePipeline
      ? 'interrupted'
      : pipelineStatus === 'failed' || pipelineStatus === 'error'
        ? 'failed'
        : 'idle';
  const latestStatusLine = pipelineIsActive
    ? (downloadState?.current || pipelineAction.replace(/_/g, ' '))
    : nextPipelineAction;
  const showLastPipelineError = Boolean(savedDetail?.last_pipeline?.error && !hasResumablePipeline);
  const visiblePipelineLog = logExpanded ? pipelineLog : pipelineLog.slice(-5);
  const loadedRowsAreComplete = savedDetailRowsLoaded && !partialDetailRows;
  const trackGroups: Array<{ id: TrackGroupId; label: string; count: number }> = [
    { id: 'available', label: 'Available', count: matchedCount },
    { id: 'missing', label: 'Missing', count: loadedRowsAreComplete ? missingOnlyTracks.length : Math.max(0, missingCount - downloadedWaitingCount - failedReviewCount) },
    { id: 'waiting', label: 'Waiting Import', count: loadedRowsAreComplete ? waitingImportTracks.length : downloadedWaitingCount },
    { id: 'failed', label: 'Failed/Review', count: loadedRowsAreComplete ? failedReviewTracks.length + qualityRows.length : failedReviewCount },
    { id: 'pending_plex', label: 'Pending Plex Match', count: loadedRowsAreComplete ? pendingPlexTracks.length : (savedDetail?.last_plex?.pending_plex_count ?? activePlaylist?.plex_pending_count ?? 0) },
    { id: 'removed', label: 'Removed/Excluded', count: loadedRowsAreComplete ? removedExcluded.length : removedExcludedCount },
  ];
  const currentGroupRows = currentRowsForGroup(trackGroup);
  const currentGroupTotal = trackGroups.find((group) => group.id === trackGroup)?.count ?? currentGroupRows.length;
  const canLoadCurrentGroupRows = viewingSavedPlaylist
    && currentGroupTotal > currentGroupRows.length
    && (trackPageHasMore[trackGroup] ?? true);

  const visibleMissingTracks = trackGroup === 'waiting'
    ? waitingImportTracks
    : trackGroup === 'failed'
      ? failedReviewTracks
      : trackGroup === 'pending_plex'
        ? pendingPlexTracks
        : missingOnlyTracks;
  const visibleMissingTitle = trackGroup === 'waiting'
    ? 'Waiting Import'
    : trackGroup === 'failed'
      ? 'Failed / Review'
      : trackGroup === 'pending_plex'
        ? 'Pending Plex Match'
        : 'Missing from Beets';
  const playlistFixActions: MenuAction[] = [
    ...(safeSuggestionCount > 0 ? [{
      label: `Apply Safe Fixes (${safeSuggestionCount})`,
      onClick: () => {
        void handleApplySafeSuggestions();
      },
      disabled: applyingSuggestions || loadingSuggestions || downloadRunning,
    }] : []),
    ...(!viewingSavedPlaylist && canSave ? [{
      label: 'Save Playlist',
      onClick: () => {
        void handleSave();
      },
      disabled: saving,
    }] : []),
    ...(viewingSavedPlaylist ? [{
      label: 'Reconcile Playlist State',
      onClick: () => void handlePipelineAction('reconcile-state'),
      disabled: pipelineIsActive,
    }] : []),
  ];

  const showTrackError = (track: PlaylistTrack) => {
    const message = track.plex_issue || track.reason || track.failure_reason || track.pipeline_message || 'No saved detail for this track.';
    setNotice({
      severity: isPendingPlexTrack(track) ? 'warning' : 'error',
      message,
    });
  };

  const trackStatus = (track: PlaylistTrack, fallback: string) => (
    track.pipeline_status || fallback
  ).toString().toLowerCase();

  const availableTrackActions = (track: PlaylistMatchedTrack): MenuAction[] => {
    if (!viewingSavedPlaylist) return [];
    return [
      { label: 'Remove from Playlist', onClick: () => void handleTrackAction('remove', track) },
      { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
      { label: 'Sync to Plex', onClick: () => void handlePipelineAction('sync-plex'), disabled: pipelineIsActive },
    ];
  };

  const missingTrackActions = (track: PlaylistTrack, rowKey: string): MenuAction[] => {
    if (!viewingSavedPlaylist) {
      return [
        { label: 'Download', onClick: () => void handleStartDownload([track], allTracks), disabled: downloadRunning },
        { label: 'Ignore', onClick: () => handleIgnoreMissing(track), disabled: downloadRunning },
      ];
    }
    const status = trackStatus(track, 'missing');
    const hasStagedFile = Boolean(track.staged_path);
    const plexPending = status === 'plex_pending' || track.retry_action === 'retry_pending_plex_match' || Boolean(track.plex_issue);
    if (plexPending) {
      return [
        { label: 'Retry Pending Plex Match', onClick: () => void handlePipelineAction('sync-plex'), disabled: pipelineIsActive },
        { label: 'View Details', onClick: () => showTrackError(track) },
        { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
      ];
    }
    const failed = status === 'failed' || status === 'review_required' || Boolean(track.failure_reason);
    const downloadedNotImported = hasStagedFile || status === 'downloaded' || status === 'waiting_import' || status === 'importing';
    if (downloadedNotImported) {
      return [
        { label: 'Import Now', onClick: () => void handleTrackAction('retry_import', track) },
        ...(hasStagedFile ? [{ label: 'Delete Staged Download', onClick: () => void handleTrackAction('delete_staged', track), danger: true }] : []),
        { label: 'Resolve Match', onClick: () => handleBeginResolve(track, rowKey) },
        { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
      ];
    }
    if (failed) {
      return [
        { label: 'Retry', onClick: () => void handleTrackAction(hasStagedFile ? 'retry_import' : 'retry_download', track) },
        { label: 'View Error', onClick: () => showTrackError(track) },
        { label: 'Resolve Manually', onClick: () => handleBeginResolve(track, rowKey) },
        { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
      ];
    }
    return [
      { label: 'Resolve Match', onClick: () => handleBeginResolve(track, rowKey) },
      { label: 'Retry Download', onClick: () => void handleTrackAction('retry_download', track) },
      { label: 'Remove from Playlist', onClick: () => void handleTrackAction('remove', track) },
      { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
    ];
  };

  const removedTrackActions = (track: PlaylistTrack): MenuAction[] => [
    { label: 'Restore to Playlist', onClick: () => void handleTrackAction('restore', track) },
  ];

  const toggleSavedPlaylistDetails = (playlistName: string) => {
    setExpandedPlaylistRows((current) => ({
      ...current,
      [playlistName]: !current[playlistName],
    }));
  };

  const handleSavedPlaylistPipelineAction = async (
    playlist: PlaylistEntry,
    action: Extract<PlaylistPipelineAction, 'download-missing' | 'import-downloaded' | 'sync-plex' | 'reconcile-state' | 'run-full' | 'resume' | 'clear'>,
  ) => {
    const busyKey = `${playlist.name}:${action}`;
    setSavedRowActionBusy(busyKey);
    setPlaylistSyncNotice(null);
    setPlaylistDetailsError('');
    try {
      const started = await runPlaylistPipelineAction(playlist.name, action);
      if ((action === 'download-missing' || action === 'run-full' || action === 'resume') && started.job_id) {
        setDownloadJobId(started.job_id);
        setLastDownloadJobId(started.job_id);
        localStorage.setItem(PLAYLIST_JOB_STORAGE_KEY, started.job_id);
        localStorage.setItem(PLAYLIST_LAST_JOB_STORAGE_KEY, started.job_id);
      } else if (started.jobs_job_id || started.job_id) {
        const jobId = started.jobs_job_id || started.job_id || '';
        setPipelineJobId(jobId);
        setPipelineJob({ ok: true, job_id: jobId, status: 'running', log: ['(starting...)'] });
      }
      setPlaylistSyncNotice({
        severity: 'info',
        message: `${action.replace(/-/g, ' ')} started for ${playlist.name}.`,
      });
      if (savedPlaylistName === playlist.name) {
        const detail = await getPlaylistDetails(playlist.name, { mode: 'summary' });
        setParseResult(detail);
      }
      await loadPlaylists();
    } catch (err) {
      setPlaylistSyncNotice({ severity: 'error', message: err instanceof Error ? err.message : String(err) });
    } finally {
      setSavedRowActionBusy('');
    }
  };

  const handleViewPlaylistIssues = async (playlist: PlaylistEntry, summary: SavedPlaylistSummary) => {
    setExpandedPlaylistRows((current) => ({ ...current, [playlist.name]: true }));
    if (summary.plexPending > 0) {
      setTrackGroup('pending_plex');
    } else if (summary.failed || summary.review) {
      setTrackGroup('failed');
    }
    await handleViewPlaylist(playlist);
    if (summary.plexIssue) {
      setPlaylistSyncNotice({ severity: summary.plexPending > 0 ? 'warning' : 'error', message: summary.plexIssue });
    }
  };

  const savedPlaylistRowActions = (playlist: PlaylistEntry, summary: SavedPlaylistSummary): MenuAction[] => [
    ...(summary.interrupted ? [{
      label: 'Resume Pipeline',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'resume');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
    }] : []),
    {
      label: 'Run Pipeline',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'run-full');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
    },
    ...(summary.missing > 0 ? [{
      label: 'Download Missing',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'download-missing');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
    }] : []),
    ...(summary.waitingImport > 0 ? [{
      label: 'Import Downloaded',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'import-downloaded');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
    }] : []),
    ...(summary.libraryMatched > 0 || summary.plexNeedsSync ? [{
      label: summary.plexPending > 0 ? 'Retry Pending Plex Matches' : 'Sync to Plex',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'sync-plex');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
    }] : []),
    ...(summary.plexPending > 0 ? [
      {
        label: 'View Pending Plex Matches',
        onClick: () => {
          void handleViewPlaylistIssues(playlist, summary);
        },
        disabled: Boolean(loadingPlaylistDetails),
      },
      {
        label: 'Scan Affected Plex Folders',
        onClick: () => {
          void handleSavedPlaylistPipelineAction(playlist, 'sync-plex');
        },
        disabled: pipelineIsActive || Boolean(savedRowActionBusy),
      },
    ] : []),
    {
      label: loadingPlaylistDetails === playlist.name ? 'Loading Tracks...' : 'View Tracks',
      onClick: () => {
        void handleViewPlaylist(playlist);
      },
      disabled: Boolean(loadingPlaylistDetails),
    },
    ...(summary.hasIssueAction ? [{
      label: 'View Issues',
      onClick: () => {
        void handleViewPlaylistIssues(playlist, summary);
      },
      disabled: Boolean(loadingPlaylistDetails),
    }] : []),
    {
      label: 'Reconcile Playlist',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'reconcile-state');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
    },
    ...((summary.interrupted || summary.pipelineFailed) ? [{
      label: 'Clear Checkpoint',
      onClick: () => {
        void handleSavedPlaylistPipelineAction(playlist, 'clear');
      },
      disabled: pipelineIsActive || Boolean(savedRowActionBusy),
      danger: true,
    }] : []),
    {
      label: 'Delete Playlist',
      onClick: () => {
        void handleDeletePlaylist(playlist);
      },
      danger: true,
    },
  ];

  return (
    <div className="min-w-0 space-y-6 overflow-x-hidden">
      <div>
        <h1 className="text-xl font-semibold text-zinc-100">Playlists</h1>
        <p className="mt-0.5 text-sm text-zinc-500">
          Source playlist → saved playlist → missing-track download → Beets import → MusicBrainz release-group placement → Plex.
        </p>
      </div>

      <section className="sticky top-0 z-30 rounded border border-graphite-800 bg-graphite-900/95 px-3 py-2.5 backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
              <span className="truncate font-medium text-zinc-100">
                {savedPlaylistName || activePlaylist?.name || 'Select a saved playlist'}
              </span>
              <span className="text-zinc-600">·</span>
              <span className="text-zinc-400">{pipelineAction.replace(/_/g, ' ')}</span>
              <span className="text-zinc-600">·</span>
              <span className={pipelineStateLabel === 'interrupted' ? 'text-amber-300' : pipelineStateLabel === 'running' ? 'text-sky-300' : pipelineStateLabel === 'failed' ? 'text-rose-300' : 'text-zinc-400'}>
                {pipelineStateLabel}
              </span>
              {lastCheckpointLabel !== '-' ? (
                <>
                  <span className="text-zinc-600">·</span>
                  <span className="text-xs text-zinc-500">checkpoint {lastCheckpointLabel}</span>
                </>
              ) : null}
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {compactStat('available', matchedCount, 'text-emerald-300')}
              {compactStat('missing', missingCount, missingCount ? 'text-rose-300' : 'text-zinc-300')}
              {compactStat('waiting import', downloadedWaitingCount, downloadedWaitingCount ? 'text-sky-300' : 'text-zinc-300')}
              {compactStat('failed/review', failedReviewCount, failedReviewCount ? 'text-amber-300' : 'text-zinc-300')}
              {compactStat('removed/excluded', removedExcludedCount, removedExcludedCount ? 'text-amber-300' : 'text-zinc-300')}
              {compactStat('Plex synced', plexSyncedCount, plexSyncedCount ? 'text-red-300' : 'text-zinc-300')}
              {compactStat('pending Plex', savedDetail?.last_plex?.pending_plex_count ?? activePlaylist?.plex_pending_count ?? 0, (savedDetail?.last_plex?.pending_plex_count ?? activePlaylist?.plex_pending_count ?? 0) ? 'text-amber-300' : 'text-zinc-300')}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            {pipelineIsActive ? (
              <>
                <Button variant="outlined" size="small" onClick={() => void handlePipelineAction('pause')}>
                  Pause
                </Button>
                <Button variant="outlined" color="error" size="small" onClick={() => void handlePipelineAction('stop')}>
                  Stop
                </Button>
              </>
            ) : (
              <Button
                variant="contained"
                color={hasResumablePipeline ? 'warning' : 'primary'}
                size="small"
                disabled={!savedPlaylistName && !activePlaylist}
                onClick={() => {
                  if (savedPlaylistName) {
                    void handlePipelineAction(mainPipelineAction);
                  } else if (activePlaylist) {
                    void handleSavedPlaylistPipelineAction(activePlaylist, 'resume');
                  }
                }}
              >
                {mainPipelineLabel}
              </Button>
            )}
            <ActionMenu
              label="More Actions"
              actions={pipelineMoreActions}
              disabled={!savedPlaylistName || pipelineIsActive}
            />
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-graphite-800 pt-2 text-xs">
          <div className="min-w-0 flex-1 truncate text-zinc-400" title={latestStatusLine}>
            <span className="text-zinc-500">Now:</span> {latestStatusLine}
          </div>
          <button
            type="button"
            className="rounded border border-graphite-700 px-2 py-1 text-xs text-zinc-300 hover:border-graphite-500 hover:text-zinc-100"
            onClick={() => setLogExpanded((value) => !value)}
          >
            {logExpanded ? 'Collapse Log' : 'Expand Log'}
          </button>
        </div>

        {showLastPipelineError ? (
          <Alert severity="error" sx={{ mt: 1.5 }}>{savedDetail?.last_pipeline?.error}</Alert>
        ) : null}
        {pipelineLog.length ? (
          <div className="mt-2">
            <LogViewer
              className={`${logExpanded ? 'max-h-72' : 'max-h-24'} text-xs leading-5 text-zinc-400`}
              emptyText="No playlist pipeline log yet."
              lines={visiblePipelineLog}
              showControls={false}
            />
          </div>
        ) : null}
      </section>

      <section className="rounded border border-graphite-800 bg-graphite-900 px-3 py-2.5">
        <button
          type="button"
          className="flex w-full flex-wrap items-center justify-between gap-2 text-left"
          aria-expanded={importExpanded}
          aria-label={importExpanded ? 'Hide Add or Import Playlist' : 'Show Add or Import Playlist'}
          onClick={() => setImportExpanded((value) => !value)}
        >
          <span className="flex min-w-0 items-center gap-2">
            <span className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-400">
              Add or Import Playlist
            </span>
            <span className="rounded border border-graphite-800 px-1.5 py-0.5 text-[0.68rem] uppercase tracking-wide text-zinc-500">
              {importExpanded ? 'Hide' : 'Show'}
            </span>
          </span>
          <span className="flex min-w-0 flex-wrap items-center justify-end gap-2 text-xs text-zinc-500">
            {parseResult ? (
              <span>{matchedCount} matched · {missingCount} missing · {parseResult.total} total</span>
            ) : content ? (
              <span className="truncate">{source === 'url' ? detected.label : SOURCE_OPTIONS.find((option) => option.value === source)?.label}</span>
            ) : null}
            {parsing ? <Chip label="Matching" size="small" color="primary" /> : null}
          </span>
        </button>

        {importExpanded ? (
          <div className="mt-3">
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_13rem_13rem]">
          <TextField
            label="Playlist name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="My Playlist"
            fullWidth
          />
          <label className="flex flex-col gap-1 text-xs font-medium text-zinc-400">
            Source
            <select
              value={source}
              onChange={(event) => {
                setSource(event.target.value as PlaylistSource);
                setParseError('');
                setParseResult(null);
              }}
              className="h-9 rounded border border-graphite-700 bg-graphite-950 px-3 text-sm text-zinc-200 outline-none focus:border-red-400"
            >
              {SOURCE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-zinc-400">
            Download Source
            <select
              value={downloadMethod}
              onChange={(event) => setDownloadMethod(event.target.value)}
              className="h-9 rounded border border-graphite-700 bg-graphite-950 px-3 text-sm text-zinc-200 outline-none focus:border-red-400"
            >
              {DOWNLOAD_METHOD_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
        </div>

        {source === 'url' ? (
          <div className="mt-4 space-y-2">
            <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_9rem]">
              <TextField
                label="Playlist URL"
                value={url}
                onChange={(event) => {
                  setUrl(event.target.value);
                  setParseResult(null);
                }}
                placeholder="https://music.youtube.com/playlist?list=... or https://open.spotify.com/playlist/..."
                fullWidth
              />
              <div className={`flex h-9 items-center justify-center self-end rounded border px-3 text-sm font-semibold ${detected.tone}`}>
                {detected.label}
              </div>
            </div>
            {url.toLowerCase().includes('spotify.com') ? (
              <p className="text-xs text-zinc-500">
                Spotify uses configured API credentials when available; public playlist fallback is handled by yt-dlp.
              </p>
            ) : null}
            <p className="text-xs text-zinc-500">
              URL parsing uses the existing backend yt-dlp flow for supported playlist providers.
            </p>
          </div>
        ) : source === 'local_m3u' ? (
          <label className="mt-4 flex flex-col gap-1 text-xs font-medium text-zinc-400">
            Local M3U playlist
            <select
              value={trackText}
              onChange={(event) => {
                setTrackText(event.target.value);
                setParseResult(null);
                if (!name.trim()) setName(event.target.value);
              }}
              className="h-10 rounded border border-graphite-700 bg-graphite-950 px-3 text-sm text-zinc-200 outline-none focus:border-red-400"
            >
              <option value="">Select a playlist from the configured folder</option>
              {playlists.map((playlist) => (
                <option key={playlist.name} value={playlist.name}>{playlist.name}</option>
              ))}
            </select>
          </label>
        ) : (
          <div className="mt-4">
            <TextField
              label="Track list"
              value={trackText}
              onChange={(event) => {
                setTrackText(event.target.value);
                setParseResult(null);
              }}
              placeholder={sampleTrackList}
              fullWidth
              multiline
              minRows={8}
              spellCheck={false}
            />
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          {!parseResult ? (
            <Button variant="contained" onClick={() => setConfirmDownload(true)} disabled={!canDownload}>
              Download Missing & Sync
            </Button>
          ) : null}
          <Button variant="outlined" onClick={handleParse} disabled={parsing || !content}>
            {parsing ? 'Previewing...' : 'Preview Matches'}
          </Button>
          {parseResult ? (
            <span className="text-sm text-zinc-400">
              {matchedCount} matched · {missingCount} missing · {parseResult.total} total
            </span>
          ) : null}
        </div>

        {parsing && <LinearProgress sx={{ mt: 2, borderRadius: 1 }} />}
        {parseError && <Alert severity="error" sx={{ mt: 2 }}>{parseError}</Alert>}
          </div>
        ) : null}
      </section>

      {!parseResult && (downloadState || downloadError) ? (
        <section className="rounded border border-graphite-800 bg-graphite-900 p-4">
          <PlaylistJobProgress
            downloadState={downloadState}
            downloadError={downloadError}
            detail={downloadStatusDetail}
            onClear={clearDownloadPanel}
            onRetryMissing={handleRetryMissingFromJob}
            canRetryMissing={Boolean((downloadState?.missing?.length ?? 0) > 0 && !downloadRunning)}
          />
        </section>
      ) : null}

      {parseResult ? (
        <section className="space-y-3 rounded border border-graphite-800 bg-graphite-900 p-3">
          <div className="sticky top-0 z-20 -mx-3 -mt-3 border-b border-graphite-800 bg-graphite-900/95 px-3 py-2 backdrop-blur">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <h2 className="truncate text-sm font-semibold text-zinc-100">
                  {savedPlaylistName || name || 'Playlist Tracks'}
                </h2>
                <div className="mt-0.5 text-xs text-zinc-500">
                  {matchedCount} matched · {missingCount} missing · {selectedPlaylistTotal} total · {pipelineStateLabel}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                {viewingSavedPlaylist && !savedDetailRowsLoaded ? (
                  <Button
                    variant="outlined"
                    size="small"
                    onClick={() => {
                      void handleLoadPlaylistRows();
                    }}
                    disabled={Boolean(loadingPlaylistRows)}
                  >
                    {loadingPlaylistRows ? 'Loading Rows...' : `Load ${TRACK_GROUP_LABELS[trackGroup]} Rows`}
                  </Button>
                ) : null}
                {viewingSavedPlaylist && savedDetailRowsLoaded && missing.length ? (
                  <Button
                    variant="outlined"
                    size="small"
                    onClick={() => {
                      void handleLoadSuggestions();
                    }}
                    disabled={loadingSuggestions || applyingSuggestions || downloadRunning}
                  >
                    {loadingSuggestions ? 'Reviewing...' : 'Review Fixes'}
                  </Button>
                ) : null}
                <ActionMenu
                  label="More"
                  actions={playlistFixActions}
                  disabled={pipelineIsActive || loadingSuggestions || applyingSuggestions || saving}
                />
              </div>
            </div>
          </div>

          {notice ? <Alert severity={notice.severity} onClose={() => setNotice(null)}>{notice.message}</Alert> : null}

          <div className="flex flex-wrap gap-1.5">
            {trackGroups.map((group) => (
              <button
                key={group.id}
                type="button"
                className={`rounded-full border px-3 py-1 text-xs ${
                  trackGroup === group.id
                    ? 'border-red-500 bg-red-950/40 text-red-200'
                    : 'border-graphite-800 bg-graphite-950/50 text-zinc-400 hover:border-graphite-600 hover:text-zinc-200'
                }`}
                onClick={() => setTrackGroup(group.id)}
              >
                {group.label} <span className="tabular-nums text-zinc-500">{group.count}</span>
              </button>
            ))}
          </div>

          {viewingSavedPlaylist && savedDetailRowsLoaded && canLoadCurrentGroupRows ? (
            <div className="flex justify-end">
              <Button
                variant="outlined"
                size="small"
                onClick={() => {
                  void handleLoadPlaylistRows(trackGroup);
                }}
                disabled={Boolean(loadingPlaylistRows)}
              >
                {loadingPlaylistRows === trackGroup ? 'Loading Rows...' : currentGroupRows.length ? 'Load More Rows' : `Load ${TRACK_GROUP_LABELS[trackGroup]} Rows`}
              </Button>
            </div>
          ) : null}

          {viewingSavedPlaylist && !savedDetailRowsLoaded ? (
            <div className="rounded border border-graphite-800 bg-graphite-950/45 p-3 text-sm text-zinc-300">
              <div className="font-medium text-zinc-100">Loaded playlist summary only.</div>
              <div className="mt-1 text-xs text-zinc-500">
                Track rows load one page at a time so large playlists do not block the page.
              </div>
              <Button
                sx={{ mt: 1.5 }}
                variant="outlined"
                size="small"
                onClick={() => {
                  void handleLoadPlaylistRows();
                }}
                disabled={Boolean(loadingPlaylistRows)}
              >
                {loadingPlaylistRows ? 'Loading Rows...' : `Load ${TRACK_GROUP_LABELS[trackGroup]} Rows`}
              </Button>
            </div>
          ) : null}

          {viewingSavedPlaylist && savedDetailRowsLoaded && qualityRows.length && trackGroup === 'failed' ? (
            <div className="rounded border border-amber-900/50 bg-amber-950/20 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-[0.72rem] font-semibold uppercase tracking-wide text-amber-300">
                    Needs cleanup
                  </div>
                  <div className="mt-1 text-xs text-amber-100/70">
                    {qualityRows.length.toLocaleString()} playlist row(s) are playable but need album placement or metadata repair.
                  </div>
                </div>
                <Button
                  variant="outlined"
                  size="small"
                  onClick={() => {
                    void handleRepairQualityRows();
                  }}
                  disabled={repairingQuality || downloadRunning || qualityRepairRows.length === 0}
                >
                  {repairingQuality ? 'Queueing...' : `Repair Review Rows (${qualityRepairRows.length})`}
                </Button>
              </div>
              <div className="max-h-44 overflow-auto rounded border border-amber-900/40 bg-graphite-950/40">
                <div className="divide-y divide-graphite-800">
                  {qualityRows.map((track, idx) => (
                    <div key={`quality-${idx}:${track.id}:${track.path || track.title}`} className="px-3 py-2 text-sm">
                      <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-zinc-200">
                            {track.artist} - {track.title}
                          </div>
                          <div className="truncate text-xs text-zinc-500">
                            {track.album || 'blank album'} · {track.path || 'no path'}
                          </div>
                        </div>
                        <div className="flex flex-wrap items-start justify-start gap-1 md:justify-end">
                          <Chip
                            label={track.quality_flags?.length ? track.quality_flags.join(', ') : track.quality}
                            size="small"
                            color={qualityColor(track.quality)}
                            variant="outlined"
                          />
                          {durationLabel(track.length) ? <Chip label={durationLabel(track.length)} size="small" variant="outlined" /> : null}
                          {track.quality !== 'bad' && track.id > 0 ? (
                            <Button
                              variant="outlined"
                              size="small"
                              onClick={() => handleBeginQualityPlace(track)}
                              disabled={placingQuality}
                            >
                              Place
                            </Button>
                          ) : null}
                        </div>
                      </div>
                      {qualityPlaceDraft?.itemId === track.id ? (
                        <div className="mt-3 rounded border border-amber-900/40 bg-graphite-950/50 p-3">
                          <div className="grid gap-2 md:grid-cols-2">
                            <TextField
                              label="Artist"
                              size="small"
                              value={qualityPlaceDraft.artist}
                              onChange={(event) => updateQualityPlaceDraft('artist', event.target.value)}
                            />
                            <TextField
                              label="Title"
                              size="small"
                              value={qualityPlaceDraft.title}
                              onChange={(event) => updateQualityPlaceDraft('title', event.target.value)}
                            />
                            <TextField
                              label="Album Artist"
                              size="small"
                              value={qualityPlaceDraft.albumartist}
                              onChange={(event) => updateQualityPlaceDraft('albumartist', event.target.value)}
                            />
                            <TextField
                              label="Album"
                              size="small"
                              value={qualityPlaceDraft.album}
                              onChange={(event) => updateQualityPlaceDraft('album', event.target.value)}
                            />
                          </div>
                          <div className="mt-2 grid gap-2 sm:grid-cols-5">
                            <TextField
                              label="Year"
                              size="small"
                              value={qualityPlaceDraft.year}
                              onChange={(event) => updateQualityPlaceDraft('year', event.target.value)}
                            />
                            <TextField
                              label="Disc"
                              size="small"
                              value={qualityPlaceDraft.disc}
                              onChange={(event) => updateQualityPlaceDraft('disc', event.target.value)}
                            />
                            <TextField
                              label="Track"
                              size="small"
                              value={qualityPlaceDraft.track}
                              onChange={(event) => updateQualityPlaceDraft('track', event.target.value)}
                            />
                            <TextField
                              label="Track Total"
                              size="small"
                              value={qualityPlaceDraft.tracktotal}
                              onChange={(event) => updateQualityPlaceDraft('tracktotal', event.target.value)}
                            />
                            <TextField
                              label="Disc Total"
                              size="small"
                              value={qualityPlaceDraft.disctotal}
                              onChange={(event) => updateQualityPlaceDraft('disctotal', event.target.value)}
                            />
                          </div>
                          <div className="mt-2 grid gap-2 md:grid-cols-3">
                            <TextField
                              label="MB Recording ID"
                              size="small"
                              value={qualityPlaceDraft.mbTrackId}
                              onChange={(event) => updateQualityPlaceDraft('mbTrackId', event.target.value)}
                            />
                            <TextField
                              label="MB Release ID"
                              size="small"
                              value={qualityPlaceDraft.mbAlbumId}
                              onChange={(event) => updateQualityPlaceDraft('mbAlbumId', event.target.value)}
                            />
                            <TextField
                              label="MB Release Group ID"
                              size="small"
                              value={qualityPlaceDraft.mbReleaseGroupId}
                              onChange={(event) => updateQualityPlaceDraft('mbReleaseGroupId', event.target.value)}
                            />
                          </div>
                          <div className="mt-3 flex flex-wrap justify-end gap-2">
                            <Button
                              variant="outlined"
                              size="small"
                              onClick={() => setQualityPlaceDraft(null)}
                              disabled={placingQuality}
                            >
                              Cancel
                            </Button>
                            <Button
                              variant="contained"
                              size="small"
                              onClick={() => {
                                void handleQueueQualityPlace();
                              }}
                              disabled={placingQuality}
                            >
                              {placingQuality ? 'Queueing...' : 'Queue Placement'}
                            </Button>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
              </div>
              <div className="mt-2 text-xs text-amber-100/60">
                Repair runs in Jobs and will not delete playlist audio.
              </div>
            </div>
          ) : null}

          {savedDetailRowsLoaded && trackGroup === 'available' ? (
            <TrackList title="Available in Beets" tone="text-emerald-400" empty={!matched.length}>
              {matched.length ? (
                <div className="divide-y divide-graphite-800">
                  {matched.map((track, idx) => (
                    <div key={`available:${idx}:${track.id}:${queryLabel(track)}`} className="px-2.5 py-1.5 text-sm">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-zinc-200">
                            {track.artist} - {track.title}
                          </div>
                          <div className="truncate text-xs text-zinc-500">
                            {queryLabel(track)}
                            {track.album ? ` · ${track.album}` : ''}
                          </div>
                          {canonicalNote(track) ? (
                            <div className="truncate text-xs text-sky-300">{canonicalNote(track)}</div>
                          ) : null}
                          <div className="mt-1 flex flex-wrap gap-1 text-xs text-zinc-500">
                            {track.pipeline_status ? (
                              <Chip label={statusLabel(track.pipeline_status)} size="small" variant="outlined" />
                            ) : null}
                            {track.quality ? (
                              <Chip
                                label={track.quality_flags?.length ? `${track.quality}: ${track.quality_flags.join(', ')}` : track.quality}
                                size="small"
                                color={qualityColor(track.quality)}
                                variant="outlined"
                              />
                            ) : null}
                            {track.source ? <Chip label={track.source} size="small" variant="outlined" /> : null}
                            {durationLabel(track.length) ? <Chip label={durationLabel(track.length)} size="small" variant="outlined" /> : null}
                            {track.format ? <Chip label={track.format} size="small" variant="outlined" /> : null}
                          </div>
                        </div>
                        <div className="flex shrink-0 flex-col items-end gap-1">
                          {track.score !== undefined ? (
                            <Chip label={scoreLabel(track.score)} size="small" variant="outlined" />
                          ) : null}
                          <ActionMenu
                            actions={availableTrackActions(track)}
                            disabled={pipelineRunning || Boolean(trackActionKey)}
                          />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : 'No matches found.'}
            </TrackList>
          ) : null}

          {savedDetailRowsLoaded && (trackGroup === 'missing' || trackGroup === 'waiting' || trackGroup === 'failed' || trackGroup === 'pending_plex') ? (
            <TrackList
              title={visibleMissingTitle}
              tone={trackGroup === 'waiting' ? 'text-sky-400' : trackGroup === 'failed' || trackGroup === 'pending_plex' ? 'text-amber-300' : 'text-rose-400'}
              empty={!visibleMissingTracks.length}
            >
              {visibleMissingTracks.length ? (
                <div className="divide-y divide-graphite-800">
                  {visibleMissingTracks.map((track, idx) => {
                    const rowKey = `${idx}:${trackKey(track)}`;
                    const suggestionRow = suggestionsByTrack.get(trackKey(track));
                    const editing = resolveDraft?.key === rowKey;
                    return (
                      <div key={rowKey} className="px-2.5 py-1.5 text-sm text-zinc-300">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <div className="truncate">{[track.artist, track.title].filter(Boolean).join(' - ') || '(untitled)'}</div>
                            {canonicalNote(track) ? (
                              <div className="mt-0.5 truncate text-xs text-sky-300">{canonicalNote(track)}</div>
                            ) : null}
                            {(track.local_path || track.path) ? <div className="mt-0.5 truncate text-xs text-zinc-600">{track.local_path || track.path}</div> : null}
                            {track.translated_plex_path ? <div className="mt-0.5 truncate text-xs text-zinc-500">Plex path: {track.translated_plex_path}</div> : null}
                            <div className="mt-1 flex flex-wrap gap-1">
                              {track.pipeline_status ? <Chip label={statusLabel(track.pipeline_status)} size="small" variant="outlined" /> : null}
                              {track.pipeline_source ? <Chip label={track.pipeline_source} size="small" variant="outlined" /> : null}
                            </div>
                            {track.plex_issue || track.reason || track.failure_reason || track.pipeline_message ? (
                              <div className={`mt-1 text-xs ${isPendingPlexTrack(track) ? 'text-amber-300' : 'text-rose-300'}`}>{track.plex_issue || track.reason || track.failure_reason || track.pipeline_message}</div>
                            ) : null}
                            {identityEvidenceLabel(track) ? (
                              <div className="mt-1 text-xs text-zinc-500">{identityEvidenceLabel(track)}</div>
                            ) : null}
                            {suggestionRow?.suggestions?.length && !editing ? (
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {suggestionRow.suggestions.slice(0, 3).map((suggestion, suggestionIdx) => (
                                  <button
                                    key={`${suggestion.source}-${suggestion.artist}-${suggestion.title}-${suggestionIdx}`}
                                    type="button"
                                    className={`max-w-full truncate rounded border px-2 py-1 text-left text-xs ${
                                      suggestion.safe
                                        ? 'border-emerald-800 bg-emerald-950/40 text-emerald-200'
                                        : 'border-graphite-700 bg-graphite-950 text-zinc-300'
                                    }`}
                                    title={suggestion.reason}
                                    onClick={() => setResolveDraft({
                                      key: rowKey,
                                      track,
                                      artist: suggestion.artist || '',
                                      title: suggestion.title || '',
                                    })}
                                    disabled={downloadRunning || Boolean(resolvingKey)}
                                  >
                                    {suggestion.safe ? 'safe · ' : ''}{suggestionLabel(suggestion)}
                                  </button>
                                ))}
                              </div>
                            ) : null}
                            {editing ? (
                              <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
                                <TextField
                                  label="Artist"
                                  value={resolveDraft.artist}
                                  onChange={(event) => setResolveDraft({ ...resolveDraft, artist: event.target.value })}
                                  size="small"
                                  fullWidth
                                />
                                <TextField
                                  label="Title"
                                  value={resolveDraft.title}
                                  onChange={(event) => setResolveDraft({ ...resolveDraft, title: event.target.value })}
                                  size="small"
                                  fullWidth
                                />
                              </div>
                            ) : null}
                          </div>
                          <div className="flex shrink-0 flex-wrap justify-end gap-1">
                            {editing ? (
                              <>
                                <Button
                                  variant="contained"
                                  size="small"
                                  onClick={() => {
                                    void handleSaveResolve();
                                  }}
                                  disabled={Boolean(resolvingKey)}
                                >
                                  {resolvingKey === rowKey ? 'Saving...' : 'Save'}
                                </Button>
                                <Button
                                  variant="outlined"
                                  size="small"
                                  onClick={() => setResolveDraft(null)}
                                  disabled={Boolean(resolvingKey)}
                                >
                                  Cancel
                                </Button>
                              </>
                            ) : (
                              <ActionMenu
                                actions={missingTrackActions(track, rowKey)}
                                disabled={pipelineRunning || Boolean(trackActionKey) || Boolean(resolvingKey)}
                              />
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : <span className="text-emerald-400">
                {trackGroup === 'missing'
                  ? 'No unresolved missing tracks.'
                  : trackGroup === 'waiting'
                    ? 'No tracks are waiting for import.'
                    : trackGroup === 'pending_plex'
                      ? 'No pending Plex matches.'
                      : 'No failed or review tracks.'}
              </span>}
            </TrackList>
          ) : null}

          {savedDetailRowsLoaded && trackGroup === 'removed' ? (
            <TrackList title="Removed / Excluded" tone="text-amber-300" empty={!removedExcluded.length}>
              {removedExcluded.length ? (
              <div className="divide-y divide-graphite-800 border-t border-graphite-800">
                {removedExcluded.map((track, idx) => (
                  <div key={`removed:${idx}:${trackKey(track)}:${track.pipeline_status}`} className="flex flex-wrap items-center justify-between gap-2 px-2.5 py-1.5 text-sm">
                    <div className="min-w-0">
                      <div className="truncate text-zinc-200">{[track.artist, track.title].filter(Boolean).join(' - ')}</div>
                      <div className="text-xs text-zinc-500">
                        {statusLabel(track.pipeline_status || 'removed')}
                        {track.failure_reason ? ` · ${track.failure_reason}` : ''}
                      </div>
                    </div>
                    <ActionMenu
                      actions={removedTrackActions(track)}
                      disabled={pipelineRunning || Boolean(trackActionKey)}
                    />
                  </div>
                ))}
              </div>
              ) : 'No removed or excluded tracks.'}
            </TrackList>
          ) : null}

          {!viewingSavedPlaylist ? (
            <PlaylistJobProgress
              downloadState={downloadState}
              downloadError={downloadError}
              detail={downloadStatusDetail}
              onClear={clearDownloadPanel}
              onRetryMissing={handleRetryMissingFromJob}
              canRetryMissing={Boolean((downloadState?.missing?.length ?? 0) > 0 && !downloadRunning)}
            />
          ) : null}
        </section>
      ) : null}

      <section className="rounded border border-graphite-800 bg-graphite-900 p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">
              Saved Playlists
            </h2>
            <div className="mt-1 text-xs text-zinc-500">
              M3U files, manifests, and resumable checkpoints from <code className="text-zinc-400">/data/media/music/playlists</code>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button
              variant="outlined"
              onClick={() => {
                void loadPlaylists();
                void loadPlaylistSyncStatus();
              }}
              disabled={loadingPlaylists}
            >
              Refresh Saved Playlists
            </Button>
          </div>
        </div>

        {playlistSyncStatus ? (
          <div className="mb-3 text-xs text-zinc-500">{playlistSyncStatusLabel(playlistSyncStatus)}</div>
        ) : null}
        {playlistSyncNotice ? (
          <Alert severity={playlistSyncNotice.severity} onClose={() => setPlaylistSyncNotice(null)} sx={{ mb: 2 }}>
            {playlistSyncNotice.message}
          </Alert>
        ) : null}
        {playlistSyncStatus?.last_error ? (
          <Alert severity="error" sx={{ mb: 2 }}>{playlistSyncStatus.last_error}</Alert>
        ) : null}

        {loadingPlaylists && <LinearProgress sx={{ borderRadius: 1 }} />}
        {playlistError && <Alert severity="error">{playlistError}</Alert>}
        {playlistDetailsError ? (
          <Alert severity="error" sx={{ mt: 2 }}>{playlistDetailsError}</Alert>
        ) : null}

        {playlists.length > 0 ? (
          <div className="mb-3 space-y-3">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <SavedPlaylistDetailMetric label="Saved playlists" value={savedPlaylistMetrics.total} tone="text-zinc-100" />
              <SavedPlaylistDetailMetric label="Need action" value={savedPlaylistMetrics.needsAction} tone={savedPlaylistMetrics.needsAction ? 'text-amber-300' : 'text-emerald-300'} />
              <SavedPlaylistDetailMetric label="In Beets" value={`${savedPlaylistMetrics.available.toLocaleString()}/${savedPlaylistMetrics.tracks.toLocaleString()} (${savedPlaylistCoveragePercent}%)`} tone={savedPlaylistMetrics.tracks === savedPlaylistMetrics.available ? 'text-emerald-300' : 'text-sky-300'} />
              <SavedPlaylistDetailMetric label="Running now" value={savedPlaylistMetrics.running} tone={savedPlaylistMetrics.running ? 'text-sky-300' : 'text-zinc-300'} />
            </div>
            <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
              <TextField
                label="Find playlist"
                value={savedPlaylistSearch}
                onChange={(event) => setSavedPlaylistSearch(event.target.value)}
                size="small"
                className="lg:max-w-sm"
                fullWidth
              />
              <div className="flex flex-wrap gap-1.5">
                {SAVED_PLAYLIST_FILTERS.map((filter) => {
                  const count = savedPlaylistFilterCount(savedPlaylistMetrics, filter.key);
                  return (
                    <Button
                      key={filter.key}
                      size="small"
                      variant={savedPlaylistFilter === filter.key ? 'contained' : 'outlined'}
                      onClick={() => setSavedPlaylistFilter(filter.key)}
                    >
                      {filter.label} {count.toLocaleString()}
                    </Button>
                  );
                })}
              </div>
            </div>
            <div className="text-xs text-zinc-500">
              Showing {filteredSavedPlaylistRows.length.toLocaleString()} of {playlists.length.toLocaleString()} saved playlist(s).
            </div>
          </div>
        ) : null}

        {!loadingPlaylists && !playlistError && playlists.length === 0 ? (
          <Alert severity="info">No playlists have been saved yet.</Alert>
        ) : null}

        {!loadingPlaylists && !playlistError && playlists.length > 0 && filteredSavedPlaylistRows.length === 0 ? (
          <Alert severity="info">No saved playlists match the current filters.</Alert>
        ) : null}

        {filteredSavedPlaylistRows.length > 0 ? (
          <div className="divide-y divide-graphite-800 rounded border border-graphite-800">
            {filteredSavedPlaylistRows.map(({ playlist, summary }) => {
              const expanded = Boolean(expandedPlaylistRows[playlist.name]);
              const fileBadge = playlistFileBadge(playlist);
              return (
                <div key={playlist.name}>
                  <div className={`flex items-center gap-2 px-3 py-2.5 ${expanded ? 'bg-graphite-900/50' : 'hover:bg-graphite-900/30'}`}>
                    {/* Left: name + meta + compact stats — clickable to toggle details */}
                    <div
                      role="button"
                      tabIndex={0}
                      aria-expanded={expanded}
                      aria-label={`${expanded ? 'Collapse' : 'Expand'} details for ${playlist.name}`}
                      className="min-w-0 flex-1 cursor-pointer outline-none"
                      onClick={() => toggleSavedPlaylistDetails(playlist.name)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          toggleSavedPlaylistDetails(playlist.name);
                        }
                      }}
                    >
                      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                        <span className="truncate font-medium text-zinc-200">{playlist.name}</span>
                        {fileBadge ? <SavedBadge label={fileBadge.label} tone={fileBadge.tone} /> : null}
                        <SavedBadge label={summary.statusBadge} tone={summary.statusTone} />
                        {playlistCheckpointNote(playlist) ? (
                          <SavedBadge label={playlistCheckpointNote(playlist)} tone="warn" />
                        ) : null}
                        {playlistCoverageNote(playlist) ? (
                          <SavedBadge label={playlistCoverageNote(playlist)} tone="idle" />
                        ) : null}
                      </div>
                      <div className="mt-0.5 truncate text-[0.68rem] text-zinc-500">
                        {summary.sourceLabel} · {summary.sourceStatus} · {summary.updatedLabel}
                      </div>
                      <SavedPlaylistRowContext summary={summary} />
                      <div className="mt-1 flex flex-wrap gap-1">
                        <SavedBadge
                          label={`${summary.libraryMatched.toLocaleString()}/${summary.totalTracks.toLocaleString()} in Beets`}
                          tone={summary.missing ? 'idle' : 'ok'}
                        />
                        {summary.missing > 0 ? (
                          <SavedBadge label={`${summary.missing.toLocaleString()} missing`} tone="danger" />
                        ) : null}
                        {summary.waitingImport > 0 ? (
                          <SavedBadge label={`${summary.waitingImport.toLocaleString()} waiting import`} tone="warn" />
                        ) : null}
                        {summary.activeFailures > 0 ? (
                          <SavedBadge label={`${summary.activeFailures.toLocaleString()} unresolved`} tone="danger" />
                        ) : null}
                        {summary.plexKnown ? (
                          <SavedBadge
                            label={`Plex ${summary.plexMatched.toLocaleString()}/${summary.plexEligible.toLocaleString()}`}
                            tone={summary.plexNeedsSync ? 'warn' : 'ok'}
                            title={summary.plexIssue || undefined}
                          />
                        ) : null}
                        {summary.plexPending > 0 ? (
                          <SavedBadge label={`${summary.plexPending.toLocaleString()} pending Plex`} tone="warn" title={summary.plexIssue || undefined} />
                        ) : null}
                      </div>
                    </div>

                    {/* Right: primary action button + overflow menu — stop click propagation */}
                    <div
                      className="flex shrink-0 items-center gap-1.5"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {summary.nextStepKey === 'resume' ? (
                        <Button
                          variant="contained"
                          size="small"
                          color="warning"
                          disabled={Boolean(savedRowActionBusy)}
                          onClick={() => void handleSavedPlaylistPipelineAction(playlist, 'resume')}
                        >
                          Resume Pipeline
                        </Button>
                      ) : summary.nextStepKey === 'import-downloaded' ? (
                        <Button
                          variant="outlined"
                          size="small"
                          color="warning"
                          disabled={Boolean(savedRowActionBusy)}
                          onClick={() => void handleSavedPlaylistPipelineAction(playlist, 'import-downloaded')}
                        >
                          Import Downloaded
                        </Button>
                      ) : summary.nextStepKey === 'download-missing' ? (
                        <Button
                          variant="outlined"
                          size="small"
                          color="error"
                          disabled={Boolean(savedRowActionBusy)}
                          onClick={() => void handleSavedPlaylistPipelineAction(playlist, 'download-missing')}
                        >
                          Download Missing
                        </Button>
                      ) : summary.nextStepKey === 'review' ? (
                        <Button
                          variant="outlined"
                          size="small"
                          color="error"
                          disabled={Boolean(loadingPlaylistDetails)}
                          onClick={() => void handleViewPlaylistIssues(playlist, summary)}
                        >
                          Review
                        </Button>
                      ) : summary.nextStepKey === 'sync-plex' ? (
                        <Button
                          variant="outlined"
                          size="small"
                          color="warning"
                          disabled={Boolean(savedRowActionBusy)}
                          onClick={() => void handleSavedPlaylistPipelineAction(playlist, 'sync-plex')}
                        >
                          {summary.plexPending > 0 ? 'Retry Plex' : 'Sync to Plex'}
                        </Button>
                      ) : (
                        <Button
                          variant="outlined"
                          size="small"
                          disabled={Boolean(loadingPlaylistDetails)}
                          onClick={() => void handleViewPlaylist(playlist)}
                        >
                          {loadingPlaylistDetails === playlist.name ? 'Loading...' : 'View'}
                        </Button>
                      )}
                      <ActionMenu
                        actions={savedPlaylistRowActions(playlist, summary)}
                        disabled={Boolean(
                          (loadingPlaylistDetails && loadingPlaylistDetails !== playlist.name)
                            || savedRowActionBusy,
                        )}
                      />
                    </div>
                  </div>
                  {expanded ? <SavedPlaylistExpandedDetails summary={summary} /> : null}
                </div>
              );
            })}
          </div>
        ) : null}
      </section>

      <DownloadConfirmDialog
        open={confirmDownload}
        missing={missing.length}
        total={allTracks.length}
        needsPull={downloadNeedsPull}
        busy={startingDownload}
        onClose={() => setConfirmDownload(false)}
        onConfirm={() => {
          void handleStartDownload();
        }}
      />
    </div>
  );
}









