// Helpers shared by the three homes of playlist work: Playlists (make and sync),
// the Workbench Playlist Downloads tab (download, import, run the pipeline) and
// the Jobs & Cleanup playlist review-row drawer (repair and place).
import Button from '@mui/material/Button';
import Menu from '@mui/material/Menu';
import MenuItem from '@mui/material/MenuItem';
import { useState } from 'react';
import type {
  PlaylistCreateResponse,
  PlaylistDetailResponse,
  PlaylistEntry,
  PlaylistMatchedTrack,
  PlaylistTrack,
} from '../../api/types';

export type Notice = {
  severity: 'info' | 'success' | 'warning' | 'error';
  message: string;
};

export type MenuAction = {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
};

export type PlaylistPipelineAction =
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

export const PLAYLIST_HIDDEN_POLL_MS = 15_000;

export function playlistPollDelay(ms: number): number {
  return typeof document !== 'undefined' && document.hidden ? Math.max(ms, PLAYLIST_HIDDEN_POLL_MS) : ms;
}

export function playlistStatusMessage(result: PlaylistCreateResponse | null | undefined): string {
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

export function playlistStatusSeverity(result: PlaylistCreateResponse | null | undefined): Notice['severity'] {
  const plex = result?.plex;
  if (!plex) return 'success';
  if (plex.error || plex.status === 'failed') return 'error';
  if (plex.status === 'partial_success' || plex.status === 'partial' || (plex.pending_plex_count ?? plex.tracks_unmatched ?? 0) > 0) return 'warning';
  if (plex.status === 'not_configured' || (!plex.created && !plex.error)) return 'info';
  return 'success';
}

export function canonicalNote(track: PlaylistTrack | PlaylistMatchedTrack): string {
  if (!track.canonicalized) return '';
  const source = [track.source_artist, track.source_title].filter(Boolean).join(' - ');
  const current = [track.artist, track.title].filter(Boolean).join(' - ');
  if (!source || source === current) return '';
  return `corrected from ${source}`;
}

export function durationLabel(seconds: number | undefined): string {
  if (!seconds || Number.isNaN(seconds)) return '';
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60).toString().padStart(2, '0');
  return `${minutes}:${rest}`;
}

export function qualityColor(quality: string | undefined): 'success' | 'warning' | 'error' | 'default' {
  if (quality === 'bad') return 'error';
  if (quality === 'review') return 'warning';
  if (quality === 'ok') return 'success';
  return 'default';
}

export function statusLabel(status: string): string {
  return status.replace(/_/g, ' ');
}

export function isWaitingImportTrack(track: PlaylistTrack): boolean {
  const status = (track.pipeline_status || '').toString().toLowerCase();
  return Boolean(track.staged_path) || status === 'downloaded' || status === 'waiting_import' || status === 'importing';
}

export function isFailedReviewTrack(track: PlaylistTrack): boolean {
  const status = (track.pipeline_status || '').toString().toLowerCase();
  return status === 'failed' || status === 'source_failed' || status === 'review_required' || Boolean(track.failure_reason);
}

export function trackKey(track: PlaylistTrack): string {
  return `${(track.artist || '').toLowerCase()}|${(track.title || '').toLowerCase()}|${track.path || ''}`;
}

export function matchedAsTracks(items: PlaylistMatchedTrack[]): PlaylistTrack[] {
  return items.map((item) => ({
    artist: item.query_artist || item.artist || '',
    title: item.query_title || item.title || '',
    source_artist: item.source_artist,
    source_title: item.source_title,
    canonicalized: item.canonicalized,
    canonical_source: item.canonical_source,
  }));
}

const RESUMABLE_PLAYLIST_STATUSES = new Set(['interrupted', 'paused', 'stopped', 'failed', 'error']);
const DIRECT_RESUME_PLAYLIST_STATUSES = new Set(['interrupted', 'paused', 'stopped']);
const FAILED_RESUME_PLAYLIST_STATUSES = new Set(['failed', 'error']);

export function normalizedPlaylistStatus(value?: string | null): string {
  return String(value || '').trim().toLowerCase();
}

export function playlistHasResumableCheckpoint(
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

export function ActionMenu({
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
