import { formatShortTime } from '../lib/dates';
import { useInterval } from '../lib/hooks';
import { errorMessage, panelError } from '../lib/errors';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import {
  applySafePlaylistSuggestions,
  createPlaylist,
  deletePlaylist,
  applyPlaylistTrackAction,
  getJob,
  getPlaylistDetails,
  getPlaylistRows,
  getPlaylistSyncStatus,
  getPlaylists,
  getPlaylistSuggestions,
  parsePlaylist,
  resolvePlaylistTrack,
  runPlaylistPipelineAction,
  syncPlaylists,
  apiErrorBody,
} from '../api/client';
import type {
  PlaylistCreateResponse,
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
import { useEngineOffline } from '../components/EngineOfflineBanner';
import { MusicBrainzUnavailableNotice, fingerprintNotCheckedLabel } from '../components/MusicBrainzUnavailableNotice';
import { PlaylistSyncSummary, playlistSyncStatusLabel } from '../features/playlists/PlaylistSyncSummary';
import {
  ActionMenu,
  canonicalNote,
  durationLabel,
  isFailedReviewTrack,
  isWaitingImportTrack,
  matchedAsTracks,
  normalizedPlaylistStatus,
  playlistHasResumableCheckpoint,
  playlistPollDelay,
  playlistStatusMessage,
  playlistStatusSeverity,
  qualityColor,
  statusLabel,
  trackKey,
  type MenuAction,
  type Notice,
  type PlaylistPipelineAction,
} from '../features/playlists/playlistShared';

type ResolveDraft = {
  key: string;
  track: PlaylistTrack;
  artist: string;
  title: string;
};

type TrackGroupId = 'available' | 'missing' | 'waiting' | 'failed' | 'pending_plex' | 'removed';

const PLAYLIST_ROW_PAGE_SIZE = 100;
const PLAYLIST_SYNC_STATUS_POLL_MS = 60_000;
const PLAYLIST_PIPELINE_JOB_POLL_MS = 5_000;

// Downloads, imports and pipeline runs live on the Workbench; review-row repair and
// placement live on Jobs & Cleanup. This page links there and does neither itself.
function workbenchPath(playlistName: string): string {
  return `/import?tab=playlists&playlist=${encodeURIComponent(playlistName)}`;
}

function reviewRowsPath(playlistName: string): string {
  return `/jobs?tab=playlist-repair&playlist=${encodeURIComponent(playlistName)}`;
}

const TRACK_GROUP_LABELS: Record<TrackGroupId, string> = {
  available: 'Available',
  missing: 'Missing',
  waiting: 'Waiting Import',
  failed: 'Failed/Review',
  pending_plex: 'Pending Plex Match',
  removed: 'Removed/Excluded',
};

// Download and import retries (retry_download, retry_import, delete_staged) live on the
// Workbench's Playlist Downloads tab; this page keeps the playlist's own membership.
type TrackAction =
  | 'remove'
  | 'exclude'
  | 'restore';

const SOURCE_OPTIONS: Array<{ value: PlaylistSource; label: string }> = [
  { value: 'local_m3u', label: 'Local M3U' },
  { value: 'url', label: 'Playlist URL' },
  { value: 'text', label: 'Track list' },
];

const sampleTrackList = `Pink Floyd - Breathe
Radiohead - Creep
The Beatles - Come Together`;

// SEC-002 CodeQL repository-wide closure finding (alerts #4-#8,
// js/incomplete-url-substring-sanitization): platformLabel() only ever
// returns fixed literal label/tone strings (never derived from `value`
// itself), rendered as plain React text/className -- so this was a
// false positive for security purposes (substring matching here can
// only mislabel a badge, it never drives injection or a trust/
// navigation decision; the actual playlist URL is validated server-side
// by the existing backend yt-dlp flow). Fixed anyway to use real
// hostname parsing, matching the mission's stated preference for strict
// parsing over substring matching and closing the false-match edge case
// (e.g. "https://evil.example/spotify.com" previously matched Spotify).
function platformHost(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return '';
  for (const candidate of [trimmed, `https://${trimmed}`]) {
    try {
      return new URL(candidate).hostname.toLowerCase();
    } catch {
      // try the next candidate
    }
  }
  return '';
}

function hostMatches(host: string, domain: string): boolean {
  return !!host && (host === domain || host.endsWith(`.${domain}`));
}

function platformLabel(value: string): { label: string; tone: string } {
  if (!value.trim()) return { label: 'Paste a URL', tone: 'border-graphite-700 text-muted' };
  const host = platformHost(value);
  if (hostMatches(host, 'music.youtube.com') || hostMatches(host, 'youtube.com') || hostMatches(host, 'youtu.be')) {
    return { label: 'YouTube', tone: 'border-graphite-700 text-danger-text' };
  }
  if (hostMatches(host, 'spotify.com')) {
    return { label: 'Spotify', tone: 'border-graphite-700 text-success' };
  }
  if (hostMatches(host, 'soundcloud.com')) {
    return { label: 'SoundCloud', tone: 'border-graphite-700 text-warning' };
  }
  if (hostMatches(host, 'music.apple.com')) {
    return { label: 'Apple Music', tone: 'border-graphite-700 text-danger-text' };
  }
  return { label: 'URL', tone: 'border-graphite-700 text-zinc-300' };
}

function queryLabel(track: PlaylistMatchedTrack): string {
  const artist = track.query_artist || track.artist || '';
  const title = track.query_title || track.title || '';
  return [artist, title].filter(Boolean).join(' - ');
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
  else if (fingerprintNotCheckedLabel(fingerprint)) parts.push(`Fingerprint: ${fingerprintNotCheckedLabel(fingerprint)}`);

  if (typeof track.acoustid_score === 'number' && track.acoustid_score > 0) {
    parts.push(`AcoustID: ${track.acoustid_score.toFixed(2)}`);
  } else if (fingerprintNotCheckedLabel(track.acoustid_status)) {
    if (!fingerprintNotCheckedLabel(fingerprint)) parts.push(`AcoustID: ${fingerprintNotCheckedLabel(track.acoustid_status)}`);
  } else if (track.acoustid_status) {
    parts.push(`AcoustID: ${statusLabel(track.acoustid_status)}`);
  }
  if (track.identity_mb_trackid || track.mb_trackid) parts.push('Recording: MusicBrainz ID');
  if (track.identity_mb_releasegroupid || track.mb_releasegroupid) parts.push('Release group: Resolved');
  return parts.join(' · ');
}

function suggestionRowsByTrack(rows: PlaylistSuggestionRow[]): Map<string, PlaylistSuggestionRow> {
  const map = new Map<string, PlaylistSuggestionRow>();
  rows.forEach((row) => {
    map.set(trackKey(row.track), row);
  });
  return map;
}

function allTracksFromResult(result: PlaylistParseResponse | null): PlaylistTrack[] {
  if (!result) return [];
  if (result.tracks?.length) return result.tracks;
  return [...matchedAsTracks(result.matched ?? []), ...(result.missing ?? [])];
}

function playlistCoverageNote(playlist: PlaylistEntry): string {
  if (playlist.m3u_tracks === undefined || playlist.m3u_tracks === playlist.tracks) return '';
  const source = playlist.desired_source === 'checkpoint' ? 'checkpoint' : 'desired';
  return `M3U ${playlist.m3u_tracks.toLocaleString()} · ${source} ${playlist.tracks.toLocaleString()}`;
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
  return `updated ${formatShortTime(value)}`;
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
  const plexFailed = plexStatus === 'failed' || lastPlexStatus === 'failed' || normalizedPlaylistStatus(playlist.sync_status) === 'failed';
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
  } else if (plexFailed) {
    statusBadge = 'Plex Sync Failed';
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

// A mixtape J-card: the playlist's name hand-lettered on sleeve paper under two strips of tape colour.
// Authored CSS only (playlists carry no album ids to draw art from); always decorative.
function MixtapeSleeve({ name, small = false, className = '' }: { name: string; small?: boolean; className?: string }) {
  const label = name.trim() || 'Untitled mix';
  let hash = 0;
  for (const ch of label) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const initials = label.split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]?.toUpperCase()).join('');
  return (
    <span aria-hidden="true" className={`mixtape sleeve ${small ? 'is-small' : ''} ${className}`} data-tone={hash % 4}>
      <span className="mixtape-title">{small ? initials : label}</span>
    </span>
  );
}

function SavedBadge({ label, tone = 'idle', title }: { label: string; tone?: SavedBadgeTone; title?: string }) {
  // Words carry the state; the tone only reinforces it. No pill: the badges sit in a run of text.
  return (
    <span title={title} className={`inline-flex items-center text-xs font-medium ${savedTextTone(tone)}`}>
      {label}
    </span>
  );
}

function savedTextTone(tone: SavedBadgeTone): string {
  if (tone === 'ok') return 'text-success';
  if (tone === 'warn') return 'text-warning';
  if (tone === 'danger') return 'text-danger-text';
  if (tone === 'info') return 'text-info';
  return 'text-zinc-300';
}

function SavedPlaylistRowContext({ summary }: { summary: SavedPlaylistSummary }) {
  const activity = summary.lastPipelineLabel !== 'No pipeline run recorded'
    ? summary.lastPipelineLabel
    : summary.lastCheckpointLabel !== 'No checkpoint'
      ? summary.lastCheckpointLabel
      : 'No recent activity';
  const activityTone = summary.running
    ? 'text-info'
    : summary.pipelineFailed || summary.lastError
      ? 'text-danger-text'
      : summary.interrupted
        ? 'text-warning'
        : 'text-zinc-400';

  return (
    <div className="mt-1 min-w-0 space-y-0.5 text-xs">
      <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-center sm:gap-2">
        <span className="min-w-0 truncate">
          <span className="text-muted">Next step: </span>
          <span className={`font-medium ${savedTextTone(summary.nextStepTone)}`}>{summary.nextStep}</span>
        </span>
        <span className="hidden text-muted sm:inline">/</span>
        <span className="min-w-0 truncate">
          <span className="text-muted">Last activity: </span>
          <span className={activityTone}>{activity}</span>
        </span>
      </div>
      {summary.lastError ? (
        <div className="truncate text-danger-text" title={summary.lastError}>
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
    <div className="min-w-0 border-t border-graphite-800 pt-1.5">
      <div className={`text-sm font-semibold tabular-nums ${tone}`}>{typeof value === 'number' ? value.toLocaleString() : value}</div>
      <div className="mt-1 text-xs text-muted">{label}</div>
    </div>
  );
}

function SavedPlaylistExpandedDetails({ summary }: { summary: SavedPlaylistSummary }) {
  return (
    <div className="border-t border-walnut-950 px-3 py-3">
      <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-6">
        <SavedPlaylistDetailMetric label="Total playlist tracks" value={summary.totalTracks} />
        <SavedPlaylistDetailMetric label="In Beets library" value={summary.libraryMatched} tone="text-success" />
        <SavedPlaylistDetailMetric label="Missing from Beets" value={summary.missing} tone={summary.missing ? 'text-danger-text' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Downloaded total" value={summary.downloadedTotal} tone={summary.downloadedTotal ? 'text-info' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Downloaded waiting import" value={summary.waitingImport} tone={summary.waitingImport ? 'text-warning' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Imported from downloads" value={summary.importedFromDownloads} tone={summary.importedFromDownloads ? 'text-success' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Active failures" value={summary.activeFailures} tone={summary.activeFailures ? 'text-danger-text' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Historical failures" value={summary.historicalFailures} tone={summary.historicalFailures ? 'text-zinc-400' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Review needed" value={summary.review} tone={summary.review ? 'text-warning' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Removed" value={summary.removed} tone={summary.removed ? 'text-warning' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Excluded" value={summary.excluded} tone={summary.excluded ? 'text-warning' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Plex matched" value={summary.plexKnown ? `${summary.plexMatched.toLocaleString()}/${summary.plexEligible.toLocaleString()}` : 'Not synced yet'} tone={summary.plexNeedsSync ? 'text-warning' : 'text-success'} />
        <SavedPlaylistDetailMetric label="Pending Plex match" value={summary.plexKnown ? summary.plexPending : '-'} tone={summary.plexPending ? 'text-warning' : 'text-zinc-300'} />
        <SavedPlaylistDetailMetric label="Last pipeline run" value={summary.lastPipelineLabel} tone="text-zinc-300" />
        <SavedPlaylistDetailMetric label="Last checkpoint" value={summary.lastCheckpointLabel} tone="text-zinc-300" />
      </div>
      {summary.lastError ? (
        <div className="mt-3 border-t border-graphite-800 pt-2">
          <div className="text-xs font-semibold text-danger-text">Last error</div>
          <div className="mt-1 whitespace-normal break-words text-xs text-zinc-100">{summary.lastError}</div>
        </div>
      ) : null}
      {summary.plexIssue && summary.plexIssue !== summary.lastError ? (
        <div className="mt-3 border-t border-graphite-800 pt-2">
          <div className="text-xs font-semibold text-warning">Plex issue</div>
          <div className="mt-1 whitespace-normal break-words text-xs text-zinc-100">{summary.plexIssue}</div>
        </div>
      ) : null}
    </div>
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
      <div className={`mb-2 text-xs font-semibold ${tone}`}>
        {title}
      </div>
      <div className="max-h-80 overflow-auto border-y border-graphite-800">
        {empty ? <div className="px-4 py-5 text-sm text-muted">{children}</div> : children}
      </div>
    </div>
  );
}

export default function Playlists() {
  // Sync and pipeline runs need the Beets engine; disabled while it is down.
  const engineOffline = useEngineOffline();
  const navigate = useNavigate();
  const [playlists, setPlaylists] = useState<PlaylistEntry[]>([]);
  const [loadingPlaylists, setLoadingPlaylists] = useState(true);
  const [playlistError, setPlaylistError] = useState('');
  const [playlistSyncStatus, setPlaylistSyncStatus] = useState<PlaylistSyncStatusResponse | null>(null);
  const [playlistSyncNotice, setPlaylistSyncNotice] = useState<Notice | null>(null);
  const [playlistSyncJobId, setPlaylistSyncJobId] = useState('');
  const [startingPlaylistSync, setStartingPlaylistSync] = useState(false);
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
  const [providerStatus, setProviderStatus] = useState<{ musicbrainz_unavailable?: boolean; acoustid_status?: string } | null>(null);
  const [resolveDraft, setResolveDraft] = useState<ResolveDraft | null>(null);
  const [resolvingKey, setResolvingKey] = useState('');
  const [suggestionRows, setSuggestionRows] = useState<PlaylistSuggestionRow[]>([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [applyingSuggestions, setApplyingSuggestions] = useState(false);
  const [pipelineJobId, setPipelineJobId] = useState('');
  const [pipelineJob, setPipelineJob] = useState<JobResponse | null>(null);
  const [pipelineActionBusy, setPipelineActionBusy] = useState('');
  const [savedRowActionBusy, setSavedRowActionBusy] = useState('');
  // Notices from saved-playlist row actions; the Sync to Plex band shows only Sync All.
  const [savedRowNotice, setSavedRowNotice] = useState<Notice | null>(null);
  const [expandedPlaylistRows, setExpandedPlaylistRows] = useState<Record<string, boolean>>({});
  const [savedPlaylistFilter, setSavedPlaylistFilter] = useState<SavedPlaylistFilter>('all');
  const [savedPlaylistSearch, setSavedPlaylistSearch] = useState('');
  const [trackActionKey, setTrackActionKey] = useState('');
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
  const activePlaylist = selectedPlaylist;
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
  // A download or import run (started on the Workbench) shows here as the playlist's last pipeline status.
  const downloadRunning = savedDetail?.last_pipeline?.status === 'running';
  const pipelineRunning = downloadRunning || pipelineJob?.status === 'running' || Boolean(pipelineActionBusy);
  const canSave = Boolean(name.trim() && allTracks.length && !saving);
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
      setPlaylistError(errorMessage(err));
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

  useInterval(() => { void loadPlaylistSyncStatus(); }, PLAYLIST_SYNC_STATUS_POLL_MS);

  // Follow the sync job this page started (or joined) until it ends.
  useEffect(() => {
    if (!playlistSyncJobId) return undefined;
    let cancelled = false;
    const check = async () => {
      try {
        const job = await getJob(playlistSyncJobId);
        if (cancelled || job.status === 'running') return;
        setPlaylistSyncJobId('');
        void loadPlaylistSyncStatus();
        void loadPlaylists();
        setPlaylistSyncNotice(job.status === 'success'
          ? { severity: 'success', message: 'Playlist sync finished. Any pending tracks are listed below.' }
          : {
              severity: 'error',
              message: `Playlist sync ${job.status === 'cancelled' || job.status === 'killed' ? 'was cancelled' : 'failed'}${typeof job.result?.error === 'string' ? `: ${job.result.error}` : ''}. Lists that did not sync are marked below; open Jobs for the log.`,
            });
      } catch {
        // Transient poll failure: try again on the next tick.
      }
    };
    const timer = window.setInterval(() => { void check(); }, 3_000);
    void check();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [playlistSyncJobId, loadPlaylistSyncStatus, loadPlaylists]);

  const handleSyncAllPlaylists = async () => {
    setStartingPlaylistSync(true);
    setPlaylistSyncNotice(null);
    try {
      const started = await syncPlaylists();
      setPlaylistSyncJobId(started.job_id);
      setPlaylistSyncNotice({
        severity: 'info',
        message: started.already_running
          ? 'A playlist sync was already running, so no second one was started. Following that run.'
          : 'Playlist sync started.',
      });
    } catch (err) {
      setPlaylistSyncNotice({ severity: 'error', message: `Playlist sync did not start: ${errorMessage(err)}` });
    } finally {
      setStartingPlaylistSync(false);
    }
  };

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
        if (!cancelled) setPlaylistDetailsError(errorMessage(err));
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [pipelineJobId, savedPlaylistName, loadPlaylists]);

  const handleParse = async () => {
    if (!content) {
      setParseError(source === 'url' ? 'Paste a playlist URL first.' : 'Paste at least one track first.');
      return;
    }
    setParsing(true);
    setParseError('');
    setNotice(null);
    setSuggestionRows([]);
    setProviderStatus(null);
    try {
      const result = await parsePlaylist(source, content);
      setParseResult(result);
      if (!name.trim() && source === 'url') {
        const fallback = detected.label === 'Paste a URL' || detected.label === 'URL' ? 'Playlist' : `${detected.label} Playlist`;
        setName(fallback);
      }
    } catch (err) {
      setParseError(errorMessage(err));
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
      const noTracks = apiErrorBody(err)?.error_code === 'no_tracks';
      setNotice({
        severity: 'error',
        message: noTracks
          ? 'Nothing was saved: this playlist has no tracks. Add or paste at least one track, then save again.'
          : errorMessage(err),
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDeletePlaylist = async (playlist: PlaylistEntry) => {
    const ok = window.confirm(
      `Delete the saved playlist "${playlist.name}" from M3U/Plex only? Songs in Beets will not be deleted.`,
    );
    if (!ok) return;
    setSavedRowNotice(null);
    setPlaylistDetailsError('');
    try {
      const result = await deletePlaylist(playlist.name, true);
      const plexNote = result.plex_error
        ? ` Plex delete error: ${result.plex_error}`
        : result.plex_deleted
          ? ` Deleted ${result.plex_deleted} Plex playlist(s).`
          : '';
      setSavedRowNotice({
        severity: 'success',
        message: `Deleted playlist ${playlist.name}. Library songs were not deleted.${plexNote}`,
      });
      if (name === playlist.name) {
        setParseResult(null);
      }
      await loadPlaylists();
    } catch (err) {
      setPlaylistDetailsError(errorMessage(err));
    }
  };

  const handleViewPlaylist = async (playlist: PlaylistEntry) => {
    setLoadingPlaylistDetails(playlist.name);
    setPlaylistDetailsError('');
    setNotice(null);
    setSuggestionRows([]);
    setProviderStatus(null);
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
      setPlaylistDetailsError(errorMessage(err));
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
      setPlaylistDetailsError(errorMessage(err));
    } finally {
      setLoadingPlaylistRows('');
    }
  };

  // Run, resume, pause, stop, clear and the download/import steps are on the Workbench.
  const handlePipelineAction = async (action: Extract<PlaylistPipelineAction, 'sync-sources' | 'sync-plex' | 'reconcile-state'>) => {
    if (!savedPlaylistName) return;
    setPipelineActionBusy(action);
    setNotice(null);
    setPlaylistDetailsError('');
    try {
      const started = await runPlaylistPipelineAction(savedPlaylistName, action);
      if (started.jobs_job_id || started.job_id) {
        const jobId = started.jobs_job_id || started.job_id || '';
        setPipelineJobId(jobId);
        setPipelineJob({ ok: true, job_id: jobId, status: 'running', log: ['(starting...)'] });
      }
      setNotice({ severity: 'info', message: `${action.replace(/-/g, ' ')} started for ${savedPlaylistName}.` });
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setPipelineActionBusy('');
    }
  };

  const handleTrackAction = async (
    action: TrackAction,
    track: PlaylistTrack,
  ) => {
    if (!savedPlaylistName) return;
    const key = `${action}:${trackKey(track)}`;
    setTrackActionKey(key);
    setNotice(null);
    try {
      const result = await applyPlaylistTrackAction(savedPlaylistName, action, track);
      setParseResult(result.playlist);
      if (result.job?.jobs_job_id) {
        setPipelineJobId(result.job.jobs_job_id);
      }
      setNotice({
        severity: result.retry_error ? 'error' : 'success',
        message: result.retry_error || `${action.replace(/_/g, ' ')} completed.`,
      });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setTrackActionKey('');
    }
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
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setResolvingKey('');
    }
  };

  const handleLoadSuggestions = async () => {
    if (!savedPlaylistName) return;
    setLoadingSuggestions(true);
    setNotice(null);
    setProviderStatus(null);
    try {
      const result = await getPlaylistSuggestions(savedPlaylistName);
      setSuggestionRows(result.rows ?? []);
      setProviderStatus(result);
      setNotice({
        severity: 'info',
        message: `${result.safe_count.toLocaleString()} safe suggestion(s) found for ${result.total_missing.toLocaleString()} missing track(s).`,
      });
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setLoadingSuggestions(false);
    }
  };

  const handleApplySafeSuggestions = async () => {
    if (!savedPlaylistName) return;
    setApplyingSuggestions(true);
    setNotice(null);
    setProviderStatus(null);
    try {
      const result = await applySafePlaylistSuggestions(savedPlaylistName);
      setParseResult(result);
      setProviderStatus(result);
      setSuggestionRows([]);
      setResolveDraft(null);
      setNotice({
        severity: 'success',
        message: `Applied ${(result.resolved_count ?? 0).toLocaleString()} safe suggestion(s). ${result.matched.length.toLocaleString()} available, ${result.missing.length.toLocaleString()} missing.`,
      });
      await loadPlaylists();
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setApplyingSuggestions(false);
    }
  };

  const pipelineStatus = pipelineJob?.status
    || savedDetail?.last_pipeline?.status
    || activePlaylist?.last_pipeline?.status
    || 'idle';
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
  const hasResumablePipeline = playlistHasResumableCheckpoint(savedDetail, activePlaylist);
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
    ...(viewingSavedPlaylist ? [
      { label: 'Sync Sources', onClick: () => void handlePipelineAction('sync-sources'), disabled: pipelineIsActive },
      { label: 'Sync to Plex', onClick: () => void handlePipelineAction('sync-plex'), disabled: pipelineIsActive || !matchedCount },
      {
        label: 'Reconcile Playlist State',
        onClick: () => void handlePipelineAction('reconcile-state'),
        disabled: pipelineIsActive,
      },
    ] : []),
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
        { label: 'Ignore', onClick: () => handleIgnoreMissing(track), disabled: downloadRunning },
      ];
    }
    const status = trackStatus(track, 'missing');
    const hasStagedFile = Boolean(track.staged_path);
    // Import Now, Delete Staged Download and the download retries are on the Workbench.
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
        { label: 'Resolve Match', onClick: () => handleBeginResolve(track, rowKey) },
        { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
      ];
    }
    if (failed) {
      return [
        { label: 'View Error', onClick: () => showTrackError(track) },
        { label: 'Resolve Manually', onClick: () => handleBeginResolve(track, rowKey) },
        { label: 'Exclude from Future Sync', onClick: () => void handleTrackAction('exclude', track) },
      ];
    }
    return [
      { label: 'Resolve Match', onClick: () => handleBeginResolve(track, rowKey) },
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
    action: Extract<PlaylistPipelineAction, 'sync-plex' | 'reconcile-state'>,
  ) => {
    const busyKey = `${playlist.name}:${action}`;
    setSavedRowActionBusy(busyKey);
    setSavedRowNotice(null);
    setPlaylistDetailsError('');
    try {
      const started = await runPlaylistPipelineAction(playlist.name, action);
      if (started.jobs_job_id || started.job_id) {
        const jobId = started.jobs_job_id || started.job_id || '';
        setPipelineJobId(jobId);
        setPipelineJob({ ok: true, job_id: jobId, status: 'running', log: ['(starting...)'] });
      }
      setSavedRowNotice({
        severity: 'info',
        message: `${action.replace(/-/g, ' ')} started for ${playlist.name}.`,
      });
      if (savedPlaylistName === playlist.name) {
        const detail = await getPlaylistDetails(playlist.name, { mode: 'summary' });
        setParseResult(detail);
      }
      await loadPlaylists();
    } catch (err) {
      setSavedRowNotice({ severity: 'error', message: errorMessage(err) });
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
      setSavedRowNotice({ severity: summary.plexPending > 0 ? 'warning' : 'error', message: summary.plexIssue });
    }
  };

  const savedPlaylistRowActions = (playlist: PlaylistEntry, summary: SavedPlaylistSummary): MenuAction[] => [
    // Running, resuming, downloading, importing and clearing a checkpoint are on the Workbench.
    ...(summary.interrupted || summary.missing > 0 || summary.waitingImport > 0 || summary.pipelineFailed ? [{
      label: 'Open in Workbench',
      onClick: () => navigate(workbenchPath(playlist.name)),
    }] : []),
    ...(summary.qualityReview > 0 ? [{
      label: 'Open Review Rows in Jobs',
      onClick: () => navigate(reviewRowsPath(playlist.name)),
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
    {
      label: 'Delete Playlist',
      onClick: () => {
        void handleDeletePlaylist(playlist);
      },
      danger: true,
    },
  ];

  return (
    <div className="min-w-0 space-y-6 overflow-x-clip">
      <div className="border-b border-graphite-800 pb-4">
        <h1 className="lib-title">Playlists</h1>
        <p className="lib-catalog">
          <span>{`${savedPlaylistMetrics.total.toLocaleString()} saved`}</span>
          <span>{`${savedPlaylistMetrics.available.toLocaleString()} of ${savedPlaylistMetrics.tracks.toLocaleString()} tracks in Beets`}</span>
          {savedPlaylistMetrics.needsAction ? (
            <span className="lib-review is-flag">{`${savedPlaylistMetrics.needsAction.toLocaleString()} need action`}</span>
          ) : null}
        </p>
        <p className="mt-2 max-w-[65ch] text-sm text-muted">
          Make a playlist from a link, an M3U file or a typed track list, then keep it in sync with Plex.
        </p>
      </div>

      {/* The focal point: the bench where a playlist is made. Its J-card takes the name as it is typed. */}
      <section aria-label="Make a playlist" className="grid grid-cols-[6rem_minmax(0,1fr)] gap-4 sm:grid-cols-[8rem_minmax(0,1fr)] md:gap-6 lg:grid-cols-[11rem_minmax(0,1fr)]">
        <MixtapeSleeve name={name || savedPlaylistName} className="w-full self-start" />
        <div className="min-w-0">
        <button
          type="button"
          className="flex w-full flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-sm border-b border-graphite-800 pb-2 text-left focus-visible:ring-2 focus-visible:ring-brass-300"
          aria-expanded={importExpanded}
          aria-label={importExpanded ? 'Hide Add or Import Playlist' : 'Show Add or Import Playlist'}
          onClick={() => setImportExpanded((value) => !value)}
        >
          <span className="flex min-w-0 flex-wrap items-baseline gap-x-3">
            <span className="font-display text-xl font-semibold text-zinc-100 sm:text-2xl">
              Add or import playlist
            </span>
            <span className="text-sm text-brass-300 underline underline-offset-4">
              {importExpanded ? 'Hide' : 'Show'}
            </span>
          </span>
          <span className="flex min-w-0 flex-wrap items-baseline justify-end gap-x-3 text-xs text-muted">
            {parseResult ? (
              <>
                <span>{matchedCount} matched</span>
                <span>{missingCount} missing</span>
                <span>{parseResult.total} total</span>
              </>
            ) : content ? (
              <span className="truncate">{source === 'url' ? detected.label : SOURCE_OPTIONS.find((option) => option.value === source)?.label}</span>
            ) : null}
            {parsing ? <Chip label="Matching" size="small" color="primary" /> : null}
          </span>
        </button>

        {!importExpanded ? (
          <p className="mt-3 max-w-[60ch] text-sm text-zinc-300">
            Paste a playlist link, pick an M3U from the playlists folder, or type one track per line. Preview the matches against Beets before you save it.
          </p>
        ) : null}

        {importExpanded ? (
          <div className="mt-3">
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_13rem]">
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
              className="h-9 rounded border border-graphite-700 bg-graphite-950 px-3 text-sm text-zinc-200 outline-none focus:border-brass-400"
            >
              {SOURCE_OPTIONS.map((option) => (
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
            {hostMatches(platformHost(url), 'spotify.com') ? (
              <p className="text-xs text-muted">
                Spotify uses configured API credentials when available; public playlist fallback is handled by yt-dlp.
              </p>
            ) : null}
            <p className="text-xs text-muted">
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
              className="h-10 rounded border border-graphite-700 bg-graphite-950 px-3 text-sm text-zinc-200 outline-none focus:border-brass-400"
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
          <Button variant="outlined" onClick={handleParse} disabled={parsing || !content}>
            {parsing ? 'Previewing...' : 'Preview Matches'}
          </Button>
          {parseResult ? (
            <span className="flex flex-wrap gap-x-3 text-sm text-zinc-400">
              <span>{matchedCount} matched</span>
              <span>{missingCount} missing</span>
              <span>{parseResult.total} total</span>
            </span>
          ) : null}
        </div>

        {parsing && <LinearProgress sx={{ mt: 2, borderRadius: 1 }} />}
        {parseError && <Alert severity="error" sx={{ mt: 2 }}>{parseError}</Alert>}
          </div>
        ) : null}
        </div>
      </section>

      {parseResult ? (
        // The open playlist: its tracklist, filed by state, set on rules like a back cover.
        <section aria-label="Playlist tracks" className="space-y-3">
          <div className="sticky top-[var(--bw-header-h)] z-10 -mx-3 border-b border-graphite-800 bg-graphite-950 px-3 py-2 sm:-mx-4 sm:px-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <h2 className="truncate font-display text-xl font-semibold text-zinc-100">
                  {savedPlaylistName || name || 'Playlist Tracks'}
                </h2>
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
                  <span>{matchedCount} matched</span>
                  <span>{missingCount} missing</span>
                  <span>{selectedPlaylistTotal} total</span>
                  <span>{pipelineStateLabel}</span>
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
          <MusicBrainzUnavailableNotice show={providerStatus?.musicbrainz_unavailable === true} acoustidStatus={providerStatus?.acoustid_status} />

          <div role="group" aria-label="Tracks by state" className="crate-tabs flex flex-wrap gap-1">
            {trackGroups.map((group) => (
              <button
                key={group.id}
                type="button"
                className="crate-tab min-h-9 px-3 text-xs transition-colors"
                aria-pressed={trackGroup === group.id}
                onClick={() => setTrackGroup(group.id)}
              >
                {group.label} <span className="tabular-nums">{group.count}</span>
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
            <div className="border-y border-graphite-800 py-3 text-sm text-zinc-300">
              <div className="font-medium text-zinc-100">Loaded playlist summary only.</div>
              <div className="mt-1 text-xs text-muted">
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
            <p className="border-y border-graphite-800 py-3 text-sm text-zinc-300">
              <span className="font-semibold text-warning">Needs cleanup: </span>
              {qualityRows.length.toLocaleString()} playlist row(s) are playable but need album placement or metadata repair.{' '}
              <Link className="text-zinc-100 underline underline-offset-4 hover:text-brass-200" to={reviewRowsPath(savedPlaylistName)}>
                Repair them in Jobs &amp; Cleanup
              </Link>
            </p>
          ) : null}

          {savedDetailRowsLoaded && trackGroup === 'available' ? (
            <TrackList title="Available in Beets" tone="text-success" empty={!matched.length}>
              {matched.length ? (
                <div className="divide-y divide-graphite-800">
                  {matched.map((track, idx) => (
                    <div key={`available:${idx}:${track.id}:${queryLabel(track)}`} className="px-2.5 py-1.5 text-sm">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-zinc-200">
                            {track.artist} - {track.title}
                          </div>
                          <div className="truncate text-xs text-muted">
                            {queryLabel(track)}
                            {track.album ? ` · ${track.album}` : ''}
                          </div>
                          {canonicalNote(track) ? (
                            <div className="truncate text-xs text-info">{canonicalNote(track)}</div>
                          ) : null}
                          <div className="mt-1 flex flex-wrap gap-1 text-xs text-muted">
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
              tone={trackGroup === 'waiting' ? 'text-info' : trackGroup === 'failed' || trackGroup === 'pending_plex' ? 'text-warning' : 'text-danger-text'}
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
                              <div className="mt-0.5 truncate text-xs text-info">{canonicalNote(track)}</div>
                            ) : null}
                            {(track.local_path || track.path) ? <div className="mt-0.5 truncate text-xs text-muted">{track.local_path || track.path}</div> : null}
                            {track.translated_plex_path ? <div className="mt-0.5 truncate text-xs text-muted">Plex path: {track.translated_plex_path}</div> : null}
                            <div className="mt-1 flex flex-wrap gap-1">
                              {track.pipeline_status ? <Chip label={statusLabel(track.pipeline_status)} size="small" variant="outlined" /> : null}
                              {track.pipeline_source ? <Chip label={track.pipeline_source} size="small" variant="outlined" /> : null}
                            </div>
                            {track.plex_issue || track.reason || track.failure_reason || track.pipeline_message ? (
                              <div className={`mt-1 text-xs ${isPendingPlexTrack(track) ? 'text-warning' : 'text-danger-text'}`}>{track.plex_issue || track.reason || track.failure_reason || track.pipeline_message}</div>
                            ) : null}
                            {identityEvidenceLabel(track) ? (
                              <div className="mt-1 text-xs text-muted">{identityEvidenceLabel(track)}</div>
                            ) : null}
                            {suggestionRow?.suggestions?.length && !editing ? (
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {suggestionRow.suggestions.slice(0, 3).map((suggestion, suggestionIdx) => (
                                  <button
                                    key={`${suggestion.source}-${suggestion.artist}-${suggestion.title}-${suggestionIdx}`}
                                    type="button"
                                    className={`max-w-full min-h-8 truncate rounded border px-2 py-1 text-left text-xs hover:border-brass-400 active:bg-graphite-800 disabled:cursor-not-allowed disabled:opacity-60 ${
                                      suggestion.safe
                                        ? 'border-success/60 text-success'
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
              ) : <span className="text-success">
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
            <TrackList title="Removed / Excluded" tone="text-warning" empty={!removedExcluded.length}>
              {removedExcluded.length ? (
              <div className="divide-y divide-graphite-800 border-t border-graphite-800">
                {removedExcluded.map((track, idx) => (
                  <div key={`removed:${idx}:${trackKey(track)}:${track.pipeline_status}`} className="flex flex-wrap items-center justify-between gap-2 px-2.5 py-1.5 text-sm">
                    <div className="min-w-0">
                      <div className="truncate text-zinc-200">{[track.artist, track.title].filter(Boolean).join(' - ')}</div>
                      <div className="text-xs text-muted">
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

        </section>
      ) : null}

      {/* The second job of the page: keeping saved playlists in step with Plex. */}
      <section aria-labelledby="playlist-sync-heading" className="grid gap-x-8 gap-y-3 border-y border-graphite-800 py-5 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
        <div className="min-w-0">
          <h2 id="playlist-sync-heading" className="font-display text-xl font-semibold text-zinc-100">
            Sync to Plex
          </h2>
          {playlistSyncStatus ? (
            <p className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm text-zinc-300">
              <span>{playlistSyncStatusLabel(playlistSyncStatus)}</span>
              {playlistSyncStatus.last_job_id ? (
                <Link className="text-zinc-300 underline underline-offset-4 hover:text-zinc-50" to={`/jobs?job=${encodeURIComponent(playlistSyncStatus.last_job_id)}`}>
                  View last sync job
                </Link>
              ) : null}
            </p>
          ) : (
            <p className="mt-1 text-sm text-muted">Sync status loads with the saved playlists.</p>
          )}
        </div>
        <Button
          variant="contained"
          onClick={() => { void handleSyncAllPlaylists(); }}
          disabled={startingPlaylistSync || Boolean(playlistSyncJobId) || Boolean(playlistSyncStatus?.running) || engineOffline}
        >
          {playlistSyncJobId || playlistSyncStatus?.running ? 'Syncing to Plex...' : 'Sync All to Plex'}
        </Button>
        <div className="min-w-0 md:col-span-2">
          {playlistSyncNotice ? (
            <Alert severity={playlistSyncNotice.severity} onClose={() => setPlaylistSyncNotice(null)} sx={{ mb: 2 }}>
              {playlistSyncNotice.message}
            </Alert>
          ) : null}
          {playlistSyncStatus?.last_error ? (
            <Alert severity="error" sx={{ mb: 2 }}>{playlistSyncStatus.last_error}</Alert>
          ) : null}
          <PlaylistSyncSummary status={playlistSyncStatus} />
        </div>
      </section>

      <section aria-labelledby="saved-playlists-heading">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h2 id="saved-playlists-heading" className="font-display text-xl font-semibold text-zinc-100">
              Saved playlists
            </h2>
            <div className="mt-1 text-xs text-muted">
              M3U files, manifests, and resumable checkpoints from <code className="text-zinc-300">&lt;music root&gt;/playlists</code>
            </div>
          </div>
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

        {loadingPlaylists && <LinearProgress sx={{ borderRadius: 1 }} />}
        {panelError(playlistError, engineOffline) && <Alert severity="error">{playlistError}</Alert>}
        {playlistDetailsError ? (
          <Alert severity="error" sx={{ mt: 2 }}>{playlistDetailsError}</Alert>
        ) : null}
        {savedRowNotice ? (
          <Alert severity={savedRowNotice.severity} onClose={() => setSavedRowNotice(null)} sx={{ mb: 2 }}>
            {savedRowNotice.message}
          </Alert>
        ) : null}

        {playlists.length > 0 ? (
          <div className="mb-3 space-y-3">
            <p className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-zinc-300">
              <span>{`${savedPlaylistCoveragePercent}% of playlist tracks in Beets`}</span>
              {savedPlaylistMetrics.running ? <span className="text-info">{`${savedPlaylistMetrics.running.toLocaleString()} running now`}</span> : null}
            </p>
            <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
              <TextField
                label="Find playlist"
                value={savedPlaylistSearch}
                onChange={(event) => setSavedPlaylistSearch(event.target.value)}
                size="small"
                className="lg:max-w-sm"
                fullWidth
              />
              <div role="group" aria-label="Filter saved playlists" className="crate-tabs flex flex-wrap gap-1">
                {SAVED_PLAYLIST_FILTERS.map((filter) => {
                  const count = savedPlaylistFilterCount(savedPlaylistMetrics, filter.key);
                  return (
                    <button
                      key={filter.key}
                      type="button"
                      className="crate-tab min-h-9 px-3 text-xs transition-colors"
                      aria-pressed={savedPlaylistFilter === filter.key}
                      onClick={() => setSavedPlaylistFilter(filter.key)}
                    >
                      {filter.label} <span className="tabular-nums">{count.toLocaleString()}</span>
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="text-xs text-muted">
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
          <div className="crate divide-y divide-walnut-950 rounded-sm">
            {filteredSavedPlaylistRows.map(({ playlist, summary }) => {
              const expanded = Boolean(expandedPlaylistRows[playlist.name]);
              const fileBadge = playlistFileBadge(playlist);
              return (
                <div key={playlist.name}>
                  <div className={`flex flex-wrap items-center gap-3 px-3 py-3 sm:flex-nowrap ${expanded ? 'bg-walnut-800' : 'hover:bg-walnut-800'}`}>
                    <MixtapeSleeve name={playlist.name} small className="w-12 shrink-0 self-start sm:w-14" />
                    {/* Left: name + meta + compact stats — clickable to toggle details */}
                    <div
                      role="button"
                      tabIndex={0}
                      aria-expanded={expanded}
                      aria-label={`${expanded ? 'Collapse' : 'Expand'} details for ${playlist.name}`}
                      className="min-w-0 flex-1 cursor-pointer rounded-sm focus-visible:ring-2 focus-visible:ring-brass-300"
                      onClick={() => toggleSavedPlaylistDetails(playlist.name)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          toggleSavedPlaylistDetails(playlist.name);
                        }
                      }}
                    >
                      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5">
                        <span className="truncate font-display text-base font-semibold text-zinc-100">{playlist.name}</span>
                        {fileBadge ? <SavedBadge label={fileBadge.label} tone={fileBadge.tone} /> : null}
                        <SavedBadge label={summary.statusBadge} tone={summary.statusTone} />
                        {playlistCheckpointNote(playlist) ? (
                          <SavedBadge label={playlistCheckpointNote(playlist)} tone="warn" />
                        ) : null}
                        {playlistCoverageNote(playlist) ? (
                          <SavedBadge label={playlistCoverageNote(playlist)} tone="idle" />
                        ) : null}
                      </div>
                      <div className="mt-0.5 flex min-w-0 flex-wrap gap-x-3 text-xs text-zinc-400">
                        <span>{summary.sourceLabel}</span>
                        <span>{summary.sourceStatus}</span>
                        <span>{summary.updatedLabel}</span>
                      </div>
                      <SavedPlaylistRowContext summary={summary} />
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
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
                      className="flex w-full shrink-0 items-center justify-end gap-1.5 sm:w-auto"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {summary.nextStepKey === 'resume' || summary.nextStepKey === 'import-downloaded' || summary.nextStepKey === 'download-missing' ? (
                        <Button
                          component={Link}
                          to={workbenchPath(playlist.name)}
                          variant="outlined"
                          size="small"
                          color={summary.nextStepKey === 'download-missing' ? 'error' : 'warning'}
                        >
                          Open in Workbench
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

    </div>
  );
}









