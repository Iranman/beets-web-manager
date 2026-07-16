import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
  Disclosure,
  DisclosureButton,
  DisclosurePanel,
  Switch,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiGet } from '../../lib/api';
import {
  albumAddMbids,
  albumMbsubmit,
  autoEnqueueImport,
  cleanupReviewFiles,
  cleanupStaleReview,
  deleteReviewFolder,
  deletePendingReview,
  getFolderStats,
  getJob,
  getReviewQueue,
  importWithId,
  matchAlbum,
  previewImportTarget,
  reconcileAutoEnqueueImport,
  revalidateImportReview,
  reconcileImportJob,
  reimportDisk,
  suggestAlbum,
  suggestFolder,
} from '../../api/client';
import type {
  AiSuggestResponse,
  AiSuggestion,
  FolderStatsResponse,
  ImportTargetPreviewResponse,
  JobStartResponse,
  ReviewCandidate,
  ReviewCounts,
  ReviewOriginCounts,
  ReviewOriginType,
  ReviewEvidence,
  ReviewItem,
  ReviewItemType,
} from '../../api/types';

/** Unified filter for the Review Queue. Type-based (pending_ai, skipped, library_no_mb)
 *  and state-based (ready, blocked, audio_mismatch, no_candidate, failed) filters live in one row. */
export type QueueFilter =
  | 'all'
  | 'pending_ai'
  | 'skipped'
  | 'library_no_mb'
  | 'ready'
  | 'blocked'
  | 'audio_mismatch'
  | 'no_candidate'
  | 'failed';

type SourceFilter = ReviewOriginType;
type ReviewBucket = Exclude<ReviewItemType, 'all'>;
type MatchBucket = 'ready' | 'blocked' | 'audio_mismatch' | 'failed' | 'no_candidate' | 'needs_id';

type ActionState = {
  status: 'idle' | 'running' | 'success' | 'warning' | 'error';
  message: string;
  jobId?: string;
};

type MatchConfidenceLevel = 'high' | 'medium' | 'low' | 'blocked' | 'not_importable';

type TargetPreviewState = {
  status: 'idle' | 'loading' | 'ready' | 'error';
  key: string;
  preview?: ImportTargetPreviewResponse;
  error?: string;
};

type SelectedMatch = {
  release_group_id: string;
  representative_release_id: string;
  artist: string;
  album: string;
  year: string;
  track_match_count: number | null;
  total_tracks: number | null;
  local_track_count: number | null;
  track_mapping: TrackRow[];
  preflight_status: 'passed' | 'failed' | 'stale' | 'not_run';
  preflight_reason: string;
  is_release_group_usable: boolean;
  is_importable: boolean;
  is_partial_import: boolean;
  confidence_score: number | null;
  confidence_level: MatchConfidenceLevel;
  auto_fix_eligible: boolean;
  auto_fix_requires_review: boolean;
  auto_fix_reason: string;
  missing_track_count: number;
  match_count: number | null;
  preflight_ok: boolean | null;
  identity_validated?: boolean;
  candidate_identity_error?: string;
  representative_release_group_id?: string;
  rejected_representative_release_id?: string;
  release_group_diagnostics?: Record<string, unknown>;
  source: 'ai' | 'candidate' | 'manual';
};

type ConfirmIntent =
  | { kind: 'apply'; item: ReviewItem; mbid: string }
  | { kind: 'dismiss'; item: ReviewItem }
  | { kind: 'delete_folder'; item: ReviewItem; folderStats?: FolderStatsResponse };

type BgJobStatus =
  | 'running'
  | 'still_running'
  | 'status_unknown'
  | 'import_job_missing'
  | 'returned_to_review'
  | 'success'
  | 'failed'
  | 'cancelled'
  | 'dismissed';

type BgJob = {
  jobId: string;
  status: BgJobStatus;
  label: string;
  message: string;
  item: ReviewItem;
  retryCount?: number;
};

function isActiveBgJob(job?: BgJob): boolean {
  return job?.status === 'running';
}

function isMissingBgJob(job?: BgJob): boolean {
  return job?.status === 'import_job_missing' || job?.status === 'returned_to_review';
}

const filters: Array<{ id: QueueFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'pending_ai', label: 'Needs AI' },
  { id: 'ready', label: 'Ready to Import' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'audio_mismatch', label: 'Audio Mismatch' },
  { id: 'library_no_mb', label: 'Needs MusicBrainz ID' },
  { id: 'no_candidate', label: 'No Candidate' },
  { id: 'failed', label: 'Failed' },
  { id: 'skipped', label: 'Skipped' },
];

const originFilters: Array<{ id: SourceFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'playlist', label: 'Playlist' },
  { id: 'batch_import', label: 'Batch' },
  { id: 'manual_import', label: 'Manual' },
  { id: 'downloads', label: 'Downloads' },
  { id: 'missing_track_acquisition', label: 'Missing Tracks' },
  { id: 'cleanup_leftover', label: 'Cleanup Leftovers' },
  { id: 'unknown', label: 'Unknown source' },
];

const blockedPanelActionButtonSx = {
  backgroundColor: '#1f2530',
  borderColor: '#7f1d1d',
  color: '#fff7ed',
  '&:hover': {
    backgroundColor: '#2d3440',
    borderColor: '#ef4444',
    color: '#fff7ed',
  },
  '&.Mui-disabled': {
    backgroundColor: '#3a424f',
    borderColor: '#9aa3b2',
    color: '#eef2f7',
    opacity: 1,
  },
  '&.Mui-focusVisible': {
    boxShadow: '0 0 0 3px rgba(239, 68, 68, 0.42)',
  },
} as const;

const blockedPanelWarningButtonSx = {
  ...blockedPanelActionButtonSx,
  backgroundColor: '#78350f',
  borderColor: '#b45309',
  '&:hover': {
    backgroundColor: '#92400e',
    borderColor: '#f59e0b',
    color: '#fff7ed',
  },
} as const;

const typeLabels: Record<ReviewBucket, string> = {
  pending_ai: 'Pending AI',
  skipped: 'Skipped',
  library_no_mb: 'Needs MB ID',
};

const typeTones: Record<ReviewBucket, string> = {
  pending_ai: 'border-amber-200 bg-amber-50 text-amber-800',
  skipped: 'border-violet-200 bg-violet-50 text-violet-800',
  library_no_mb: 'border-cyan-200 bg-cyan-50 text-cyan-800',
};

const matchLabels: Record<MatchBucket, string> = {
  ready: 'Ready match',
  blocked: 'Blocked',
  audio_mismatch: 'Audio mismatch',
  failed: 'Failed preflight',
  no_candidate: 'No MB candidate',
  needs_id: 'Needs ID',
};

const matchTones: Record<MatchBucket, string> = {
  ready: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  blocked: 'border-amber-300 bg-amber-50 text-amber-900',
  audio_mismatch: 'border-rose-300 bg-rose-50 text-rose-900',
  failed: 'border-rose-200 bg-rose-50 text-rose-800',
  no_candidate: 'border-graphite-300 bg-graphite-50 text-zinc-700',
  needs_id: 'border-cyan-200 bg-cyan-50 text-cyan-800',
};
const AUTO_IMPORT_CONFIDENCE_THRESHOLD = 0.60;
const AUTO_QUARANTINE_REJECTED_MAX_FILES = 5;

function persistSubmittedItemIds(ids: Set<string>) {
  try {
    if (ids.size) localStorage.setItem('importReview_submittedIds', JSON.stringify([...ids]));
    else localStorage.removeItem('importReview_submittedIds');
  } catch {}
}
const REVIEW_QUEUE_LIMIT = 5000;


function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: number | undefined;
  const timeout = new Promise<T>((_, reject) => {
    timer = window.setTimeout(() => reject(new Error(label)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer !== undefined) window.clearTimeout(timer);
  });
}

function isMusicLibraryPath(path?: string): boolean {
  return Boolean(path?.replace(/\\/g, '/').startsWith('/data/media/music/'));
}

function countFor(counts: ReviewCounts, filter: ReviewItemType): number {
  return counts[filter] ?? 0;
}

function originCountFor(counts: ReviewOriginCounts, filter: SourceFilter): number {
  return counts[filter] ?? 0;
}

function itemOriginType(item: ReviewItem): SourceFilter {
  return item.origin_type ?? 'unknown';
}

function originLabel(filter: SourceFilter): string {
  return originFilters.find((entry) => entry.id === filter)?.label ?? 'Unknown source';
}

function itemOriginLabel(item: ReviewItem): string {
  if (item.origin_label) return item.origin_label;
  const origin = itemOriginType(item);
  if (origin === 'playlist' && item.source_playlist_name) return `Playlist: ${item.source_playlist_name}`;
  return originLabel(origin);
}

function itemMatchesSourceFilter(item: ReviewItem, sourceFilter: SourceFilter): boolean {
  return sourceFilter === 'all' || itemOriginType(item) === sourceFilter;
}

function suggestionWithOrigin(item: ReviewItem, suggestion?: AiSuggestion): AiSuggestion {
  return {
    ...(suggestion ?? {}),
    origin_type: itemOriginType(item),
    origin_label: itemOriginLabel(item),
    origin_id: item.origin_id,
    source_playlist_id: item.source_playlist_id,
    source_playlist_name: item.source_playlist_name,
    source_batch_id: item.source_batch_id,
    source_folder: item.source_folder || item.path,
    created_by_workflow: item.created_by_workflow,
  };
}

function itemTitle(item: ReviewItem): string {
  return item.title || item.album || item.folder_name || '(unknown)';
}

function reviewItemStateKey(item: ReviewItem): string {
  const preflight = item.evidence?.preflight;
  return [
    item.id,
    item.path || '',
    item.status || '',
    item.mb_albumid || '',
    item.mb_releasegroupid || '',
    item.reason || '',
    item.sort_ts ?? '',
    preflight ? [preflight.ok, preflight.matches, preflight.expected, preflight.release_group, preflight.error].join(':') : '',
  ].join('::');
}

function preflightHasAudioMismatch(preflight?: ReviewEvidence['preflight']): boolean {
  return Boolean(preflight?.acoustid_mismatch);
}

function audioMismatchPreflightDetail(preflight?: ReviewEvidence['preflight']): string {
  if (!preflight?.acoustid_mismatch) return '';
  const selected = Number(preflight.acoustid_target_hits ?? 0);
  const top = Number(preflight.acoustid_top_hits ?? 0);
  const topRelease = preflight.acoustid_top_release ? ` Top release: ${preflight.acoustid_top_release}.` : '';
  return `AcoustID fingerprint mismatch: selected release ${selected} hit(s), strongest alternate ${top} hit(s).${topRelease}`;
}

function hasAudioMismatchEvidence(item?: Pick<ReviewItem, 'evidence'>): boolean {
  return preflightHasAudioMismatch(item?.evidence?.preflight);
}

function audioMismatchDetail(item?: Pick<ReviewItem, 'evidence'>): string {
  return audioMismatchPreflightDetail(item?.evidence?.preflight);
}

function itemMatchBucket(item: ReviewItem): MatchBucket {
  if (hasAudioMismatchEvidence(item)) return 'audio_mismatch';
  const statusKey = (item.status_key || item.status || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
  const explicitFailed = new Set([
    'auto_enqueue_failed',
    'import_failed',
    'import_failed_needs_reconcile',
    'preflight_failed',
    'failed',
  ]);
  if (explicitFailed.has(statusKey)) return 'failed';
  const explicitBlocked = new Set([
    'blocked',
    'not_importable',
    'target_conflict',
    'purge_required',
    'duplicate_only',
    'duplicate_cleanup',
    'no_verified_tracks',
    'format_policy_rejected',
  ]);
  if (item.blocked_reason || explicitBlocked.has(statusKey)) return 'blocked';
  if (item.type === 'library_no_mb') return 'needs_id';
  if (item.type !== 'pending_ai') return item.mb_valid ? 'ready' : 'no_candidate';
  if (!item.mb_valid && !item.mb_albumid) return 'no_candidate';
  const preflight = item.evidence?.preflight;
  if (preflight && preflight.ok === false) return 'failed';
  return 'ready';
}

function partialFullAlbumMismatch(item: ReviewItem): boolean {
  const preflight = item.evidence?.preflight;
  if (!preflight || preflight.ok) return false;
  const audioCount = Number(preflight.audio_count ?? 0);
  const expected = Number(preflight.expected ?? 0);
  const sourceRatio = Number(preflight.source_match_ratio ?? 0);
  const matchRatio = Number(preflight.match_ratio ?? 0);
  return audioCount > 0 && audioCount <= 2 && expected >= 8 && sourceRatio >= 0.8 && matchRatio < 0.25;
}

function wrongSourceEvidenceNote(item?: ReviewItem): string {
  if (!item) return '';
  const mismatch = audioMismatchDetail(item);
  if (mismatch) return mismatch;
  if (partialFullAlbumMismatch(item)) {
    const preflight = item.evidence?.preflight;
    return `Folder has ${preflight?.audio_count ?? 0} audio file(s), but the selected release has ${preflight?.expected ?? 0} tracks.`;
  }
  return '';
}

function deleteFolderActionLabel(item?: ReviewItem): string {
  return wrongSourceEvidenceNote(item) ? 'Delete Wrong Audio' : 'Delete Folder';
}

function matchReviewNote(item: ReviewItem): string {
  const preflight = item.evidence?.preflight;
  if (hasAudioMismatchEvidence(item)) {
    return `${audioMismatchDetail(item)} Do not import this selection; choose a matching release or delete the wrong source folder.`;
  }
  if (partialFullAlbumMismatch(item)) {
    return `This folder has ${preflight?.audio_count ?? 0} audio file(s), but the selected release has ${preflight?.expected ?? 0} tracks. Treat it as a partial or wrong-source folder until a matching release is selected.`;
  }
  if (preflight && preflight.ok === false) {
    return `The selected release failed tracklist preflight (${preflight.matches ?? 0}/${preflight.expected ?? 0}). Pick a release that matches this folder before importing.`;
  }
  if (item.type === 'pending_ai' && !item.mb_valid) {
    return 'No valid MusicBrainz release-group ID is selected yet. Use AI Suggest, paste a release-group ID, or delete the source folder if the files are wrong.';
  }
  return '';
}

function searchText(item: ReviewItem): string {
  const folderEvidence = item.evidence?.folder;
  return [
    itemTitle(item),
    matchLabels[itemMatchBucket(item)],
    item.artist,
    item.album,
    item.year,
    item.path,
    item.reason,
    item.status_key,
    storedBlockedReason(item),
    item.blocked_next_action,
    item.origin_label,
    item.source_playlist_name,
    item.source_batch_id,
    item.created_by_workflow,
    item.mb_albumid,
    item.confidence,
    audioMismatchDetail(item),
    folderEvidence?.guessed_artist,
    folderEvidence?.guessed_album,
    folderEvidence?.guessed_year,
  ].filter(Boolean).join(' ').toLowerCase();
}

function scoreValue(candidate: ReviewCandidate): number | string | undefined {
  return candidate.match_total ?? candidate.score ?? candidate.acoustid_release_hits;
}

function formatScore(value: number | string | undefined): string {
  if (typeof value === 'number') {
    return value <= 1 ? value.toFixed(2) : String(Math.round(value));
  }
  return value ? String(value) : '-';
}

function formatPercent(value: number | string | undefined): string {
  const score = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(score)) return '-';
  return `${Math.round(score * 100)}%`;
}

function normalizedScoreValue(value: number | string | undefined): number | null {
  const score = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(score) || score < 0) return null;
  if (score <= 1) return score;
  if (score <= 100) return score / 100;
  return null;
}

function scoreClass(value: number | string | undefined): string {
  const score = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(score)) return 'text-zinc-500';
  if (score >= 80) return 'text-emerald-700';
  if (score >= 60) return 'text-amber-700';
  if (score >= 0.8) return 'text-emerald-700';
  if (score >= 0.6) return 'text-amber-700';
  return 'text-zinc-500';
}

function confidenceClass(confidence?: string): string {
  if (confidence === 'high') return 'text-emerald-700';
  if (confidence === 'medium') return 'text-amber-700';
  if (confidence === 'low') return 'text-rose-700';
  return 'text-zinc-600';
}

function confidenceLevelForScore(score: number | null): MatchConfidenceLevel {
  if (score === null) return 'low';
  if (score >= 0.90) return 'high';
  if (score >= AUTO_IMPORT_CONFIDENCE_THRESHOLD) return 'medium';
  return 'low';
}

function confidenceLevelFromAi(confidence?: string): MatchConfidenceLevel {
  if (confidence === 'high') return 'high';
  if (confidence === 'medium') return 'medium';
  return 'low';
}

function confidenceLabel(level: MatchConfidenceLevel): string {
  if (level === 'high') return 'High confidence';
  if (level === 'medium') return 'Auto-import eligible';
  if (level === 'blocked') return 'Blocked';
  if (level === 'not_importable') return 'Not importable';
  return 'Low confidence';
}

function confidenceClassForLevel(level: MatchConfidenceLevel): string {
  if (level === 'high') return 'text-emerald-700';
  if (level === 'medium') return 'text-amber-700';
  if (level === 'blocked' || level === 'not_importable') return 'text-rose-700';
  return 'text-zinc-600';
}

function confidenceChipColor(level: MatchConfidenceLevel): 'success' | 'warning' | 'error' | 'default' {
  if (level === 'high') return 'success';
  if (level === 'medium') return 'warning';
  if (level === 'blocked' || level === 'not_importable') return 'error';
  return 'default';
}

function selectedMatchAutomationReason(
  confidenceLevel: MatchConfidenceLevel,
  confidenceScore: number | null,
  isImportable: boolean,
  preflightStatus: SelectedMatch['preflight_status'],
  isReleaseGroupUsable: boolean,
  hasRepresentativeRelease: boolean,
  importableTrackCount: number,
  missingTrackCount: number,
  unmatchedExtraCount: number,
  preflightReason: string,
  isPartialImport: boolean,
  matchCount: number | null,
  totalTracks: number | null,
): string {
  if (!isReleaseGroupUsable) {
    return 'Auto-import blocked because this candidate does not include a valid MusicBrainz Release Group ID.';
  }
  if (!hasRepresentativeRelease) {
    return 'Auto-import blocked because this candidate does not include a representative release for tracklist comparison.';
  }
  if (!importableTrackCount) {
    return 'Auto-import blocked until at least one local track is verified for this Release Group.';
  }
  if (preflightStatus === 'failed') {
    return preflightReason || 'Auto-import blocked because this candidate failed tracklist preflight.';
  }
  if (preflightStatus === 'not_run' || preflightStatus === 'stale') {
    return 'Auto-import blocked until preflight is refreshed for this selected candidate.';
  }
  if (!isImportable) {
    return preflightReason || 'Auto-import blocked because this selected match is not importable.';
  }
  if (confidenceScore === null || confidenceScore < AUTO_IMPORT_CONFIDENCE_THRESHOLD || confidenceLevel === 'low') {
    return 'Review required because this match is below the 60% auto-import threshold.';
  }
  if (isPartialImport && matchCount !== null && totalTracks !== null) {
    const importCount = importableTrackCount || matchCount;
    const pieces = [`Partial import ready: ${importCount} verified track${importCount === 1 ? '' : 's'} will import.`];
    if (unmatchedExtraCount > 0) {
      pieces.push(`${unmatchedExtraCount} unmatched file${unmatchedExtraCount === 1 ? '' : 's'} will stay in review.`);
    }
    if (missingTrackCount > 0) {
      pieces.push(`${missingTrackCount} album track${missingTrackCount === 1 ? '' : 's'} can be acquired later.`);
    }
    return pieces.join(' ');
  }
  const missingSuffix = missingTrackCount > 0
    ? ` ${missingTrackCount} album track${missingTrackCount === 1 ? '' : 's'} can be acquired later.`
    : '';
  return `Auto-import eligible — ${formatPercent(confidenceScore)} confidence.${missingSuffix}`;
}
function candidateName(candidate: ReviewCandidate): string {
  const base = [candidate.album || '(unknown album)', candidate.artist || '(unknown artist)'].join(' - ');
  const date = candidate.date || candidate.year;
  return date ? `${base} (${date})` : base;
}

function candidateFormat(candidate: ReviewCandidate): string {
  if (candidate.format_summary) return candidate.format_summary;
  if (candidate.mediums?.length) {
    return candidate.mediums
      .map((medium) => `${medium.format || 'Medium'} (${medium.tracks || 0})`)
      .join(' + ');
  }
  return candidate.formats?.length ? candidate.formats.join(' + ') : '';
}

function candidateLabelCatalog(candidate: ReviewCandidate): string {
  if (candidate.label_entries?.length) {
    return candidate.label_entries
      .map((entry) => [entry.label, entry.catalog_number].filter(Boolean).join(' / '))
      .filter(Boolean)
      .join('; ');
  }
  const label = candidate.label || candidate.labels?.[0] || '';
  const catalog = candidate.catalog_numbers?.join(', ') || '';
  return [label, catalog].filter(Boolean).join(' / ');
}

function candidateMeta(candidate: ReviewCandidate): string[] {
  const parts = [
    candidate.country ? `Country ${candidate.country}` : '',
    candidate.date || candidate.year ? `Date ${candidate.date || candidate.year}` : '',
    candidate.tracks ? `${candidate.tracks} tracks` : '',
    candidateFormat(candidate),
    candidateLabelCatalog(candidate),
    candidate.barcode ? `Barcode ${candidate.barcode}` : '',
    candidate.cover_art === true
      ? `Cover art ${candidate.cover_art_count || 1}`
      : candidate.cover_art === false
        ? 'No cover art'
        : 'Cover art unknown',
  ];
  return parts.filter(Boolean);
}

function sameMbid(left?: string, right?: string): boolean {
  return Boolean(left && right && left.trim().toLowerCase() === right.trim().toLowerCase());
}

function isMusicBrainzUuid(value?: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test((value ?? '').trim());
}

function musicBrainzUrl(mbAlbumId?: string): string {
  return mbAlbumId ? `https://musicbrainz.org/release/${mbAlbumId}` : '';
}

function musicBrainzReleaseGroupUrl(mbReleaseGroupId?: string): string {
  return mbReleaseGroupId ? `https://musicbrainz.org/release-group/${mbReleaseGroupId}` : '';
}

function initialMbid(item: ReviewItem): string {
  return item.mb_releasegroupid || item.mb_albumid || '';
}

function selectedReleaseGroupId(item: ReviewItem, mbid: string, response?: AiSuggestResponse): string {
  const suggestion = response?.suggestion;
  if (sameMbid(item.mb_albumid, mbid) && item.mb_releasegroupid) return item.mb_releasegroupid;
  if (sameMbid(suggestion?.mb_albumid, mbid) && suggestion?.mb_releasegroupid) {
    return suggestion.mb_releasegroupid;
  }
  const candidates = [
    ...(response?.mb_candidates ?? []),
    ...(response?.evidence?.top_candidates ?? []),
    ...(suggestion?.review_evidence?.top_candidates ?? []),
    ...(item.evidence?.top_candidates ?? []),
  ];
  const match = candidates.find((candidate) => sameMbid(candidate.mb_albumid, mbid));
  return match?.mb_releasegroupid || item.evidence?.preflight?.release_group || '';
}

function existingAlbumId(item: ReviewItem): number {
  return Number(item.existing_album_id || item.existing_album_ids?.[0] || 0);
}

function targetPreviewKey(item: ReviewItem, selectedMatch?: SelectedMatch): string {
  if (!selectedMatch || selectedMatch.source === 'manual') return '';
  const mapped = selectedMatch.track_mapping
    .map((row) => `${row.num}:${row.status}:${row.source_path || ''}:${row.mb_trackid || row.mb_title || row.local_title}`)
    .join('|');
  return [
    item.id,
    item.path || '',
    existingAlbumId(item) || '',
    selectedMatch.release_group_id,
    selectedMatch.representative_release_id,
    selectedMatch.identity_validated === false ? 'identity-invalid' : 'identity-ok',
    selectedMatch.candidate_identity_error || '',
    selectedMatch.preflight_status,
    selectedMatch.track_match_count ?? '',
    selectedMatch.total_tracks ?? '',
    mapped,
  ].join('::');
}

function actionLabel(item: ReviewItem, selectedMatch?: SelectedMatch, preview?: ImportTargetPreviewResponse): string {
  if (item.type === 'library_no_mb') return 'Match album';
  const selectedCount = selectedMatch && selectedMatch.source !== 'manual'
    ? selectedImportSourceFiles(selectedMatch, preview).length
    : 0;
  const previewCount = preview?.tracks_to_import_count ?? selectedCount;
  if (selectedMatch?.identity_validated === false) return 'Import blocked';
  if (selectedMatch?.is_partial_import && selectedMatch.source !== 'manual') {
    const n = Math.max(0, Math.min(selectedCount || previewCount, previewCount));
    return existingAlbumId(item)
      ? `Repair ${n} matched track${n === 1 ? '' : 's'}`
      : `Import ${n} matched track${n === 1 ? '' : 's'}`;
  }
  if (selectedMatch?.auto_fix_eligible && selectedMatch.source !== 'manual') {
    return existingAlbumId(item) ? 'Complete Verified Repair' : 'Complete Verified Import';
  }
  return existingAlbumId(item) ? 'Repair with ID' : 'Import with ID';
}

function currentTargetPreviewState(
  item: ReviewItem,
  selectedMatch: SelectedMatch | undefined,
  previews: Record<string, TargetPreviewState>,
): TargetPreviewState | undefined {
  const key = targetPreviewKey(item, selectedMatch);
  const state = previews[item.id];
  return key && state?.key === key ? state : undefined;
}

function targetPreviewBlockReason(
  item: ReviewItem,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): string {
  const importLike = item.type === 'pending_ai' && !existingAlbumId(item);
  if (!importLike || !selectedMatch || selectedMatch.source === 'manual') return '';
  if (!selectedMatch.is_importable) return '';
  if (!targetPreviewState || targetPreviewState.status === 'idle') {
    return 'Import blocked until the target path preview is available.';
  }
  if (targetPreviewState.status === 'loading') {
    return 'Import blocked until the target path preview finishes.';
  }
  if (targetPreviewState.status === 'error') {
    return targetPreviewState.error || 'Import blocked because the target path preview failed.';
  }
  const preview = targetPreviewState.preview;
  if (!preview) return 'Import blocked until the target path preview is available.';
  if (!preview.safe) {
    if (preview.next_action === 'verify_or_cleanup_unmatched') {
      return 'Import blocked: no verified tracks selected after automatic verification; purge/quarantine the unmatched source file or choose another match.';
    }
    return preview.blocked_reasons?.[0]
      ? `Import blocked by target path preview: ${preview.blocked_reasons[0]}.`
      : 'Import blocked because the target path preview is not safe.';
  }
  const selectedCount = selectedImportSourceFiles(selectedMatch, preview).length;
  const previewCount = preview.tracks_to_import_count ?? selectedCount;
  if (previewCount < 1 || selectedCount < 1) return 'Import blocked: no verified tracks selected for import.';
  if (previewCount !== selectedCount) return 'Import blocked: selected file count does not match target preview.';
  return '';
}

function applyBlockReason(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): string {
  const releaseGroupId = mbid.trim();
  if (!releaseGroupId) return 'Enter or select a MusicBrainz Release Group ID first.';
  const importLike = item.type === 'pending_ai' && !existingAlbumId(item);
  if (!importLike) {
    if (selectedMatch?.preflight_status === 'failed' && selectedMatch.source !== 'manual') {
      return 'Import blocked because this candidate failed tracklist preflight.';
    }
    return '';
  }
  if (!selectedMatch) {
    return 'Select the visible MusicBrainz match first so its track comparison controls the import.';
  }
  if (!sameMbid(selectedMatch.release_group_id, releaseGroupId)) {
    return 'Import blocked because the visible match and Release Group ID field are out of sync.';
  }
  if (!isMusicBrainzUuid(selectedMatch.release_group_id)) {
    return 'Import blocked because this candidate does not include a valid MusicBrainz Release Group ID.';
  }
  if (!isMusicBrainzUuid(selectedMatch.representative_release_id)) {
    return 'Import blocked because this candidate does not include a representative release for tracklist comparison.';
  }
  if (selectedMatch.identity_validated === false) {
    return selectedMatch.candidate_identity_error || 'Import blocked: representative release does not belong to selected Release Group.';
  }
  if (!selectedMatch.track_mapping.length) {
    return 'Import blocked until the visible candidate track comparison finishes.';
  }
  if (selectedMatch.preflight_status === 'not_run' || selectedMatch.preflight_status === 'stale') {
    return 'Import blocked until preflight is refreshed for the selected visible candidate.';
  }
  if (selectedMatch.preflight_status === 'failed') {
    return selectedMatch.preflight_reason || 'Import blocked because this candidate failed tracklist preflight.';
  }
  if (!selectedMatch.is_importable) {
    return selectedMatch.preflight_reason || 'Import blocked because the selected match is not importable.';
  }
  const previewBlock = targetPreviewBlockReason(item, selectedMatch, targetPreviewState);
  if (previewBlock) return previewBlock;
  return '';
}

function storedBlockedReason(item: ReviewItem): string {
  if (item.blocked_reason) return item.blocked_reason;
  const text = [item.status, item.reason].filter(Boolean).join(' ');
  return /\bblock(?:ed|ing)?\b/i.test(text) ? item.reason || item.status || 'Import blocked.' : '';
}

function storedBlockedNextAction(item: ReviewItem): string {
  return item.blocked_next_action || '';
}

function actionBlockReasonForFilter(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): string {
  const selectedBlock = selectedMatch ? applyBlockReason(item, mbid, selectedMatch, targetPreviewState) : '';
  return selectedBlock || storedBlockedReason(item);
}

function shouldShowBlockedBucket(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): boolean {
  if (item.type === 'skipped' || hasAudioMismatchEvidence(item)) return false;
  return Boolean(actionBlockReasonForFilter(item, mbid, selectedMatch, targetPreviewState));
}

function shouldShowReadyBucket(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): boolean {
  if (item.type === 'skipped' || hasAudioMismatchEvidence(item)) return false;
  if (shouldShowBlockedBucket(item, mbid, selectedMatch, targetPreviewState)) return false;
  if (!selectedMatch || selectedMatch.source === 'manual') return false;
  if (!selectedMatch.is_importable) return false;
  if (!sameMbid(selectedMatch.release_group_id, mbid)) return false;
  if (!isMusicBrainzUuid(selectedMatch.release_group_id)) return false;
  if (!isMusicBrainzUuid(selectedMatch.representative_release_id)) return false;
  if (selectedMatch.preflight_status !== 'passed') return false;
  const preview = targetPreviewState?.status === 'ready' ? targetPreviewState.preview : undefined;
  if (!preview || !preview.safe) return false;
  if ((preview.real_conflict_count ?? 0) > 0) return false;
  const selectedCount = selectedImportSourceFiles(selectedMatch, preview).length;
  const previewCount = preview.tracks_to_import_count ?? selectedCount;
  return selectedCount > 0 && previewCount > 0 && selectedCount === previewCount;
}

function blockedActionHint(reason: string): string {
  const value = reason.toLowerCase();
  if (value.includes('music format preferences') || value.includes('format policy')) return 'Choose another source or update Music Format Preferences before retrying.';
  if (value.includes('target path')) return 'Fix the target path conflict, then retry this item.';
  if (value.includes('out of sync') || value.includes('visible musicbrainz match')) return 'Select the visible candidate again so the ID field and comparison agree.';
  if (value.includes('release group id') || value.includes('valid musicbrainz')) return 'Select AI Suggest or paste a valid MusicBrainz Release Group ID.';
  if (value.includes('preflight') || value.includes('tracklist') || value.includes('not importable')) return 'Choose a release that matches the files, or delete the source folder if the audio is wrong.';
  if (value.includes('no verified tracks') || value.includes('selected file count')) return 'Adjust the selected track mapping before importing.';
  return 'Resolve this block before importing; uncertain audio stays in review.';
}

function canDeleteFolder(item: ReviewItem): boolean {
  return Boolean(item.path) && (item.type === 'pending_ai' || item.type === 'library_no_mb');
}

function actionTone(state?: ActionState): 'info' | 'success' | 'warning' | 'error' {
  if (state?.status === 'success') return 'success';
  if (state?.status === 'warning') return 'warning';
  if (state?.status === 'error') return 'error';
  return 'info';
}

function EvidenceSummary({ item, evidence: evidenceOverride }: { item?: ReviewItem; evidence?: ReviewEvidence }) {
  const evidence = evidenceOverride ?? item?.evidence;
  const top = evidence?.top_candidates ?? [];
  const preflight = evidence?.preflight;
  const best = top[0];
  const examples = preflight?.examples ?? [];

  if (!best && !preflight) {
    return null;
  }

  return (
    <div className="mt-3 space-y-2 text-xs text-zinc-600">
      <div className="flex flex-wrap items-center gap-2">
        {best ? (
          <span>
            Best match <strong className={scoreClass(scoreValue(best))}>{formatScore(scoreValue(best))}</strong>
          </span>
        ) : null}
        {best?.acoustid_hits ? <span>{best.acoustid_hits} AcoustID hit(s)</span> : null}
        {preflight ? (
          <>
            <span>
              Preflight{' '}
              <strong className={preflight.ok ? 'text-emerald-700' : 'text-rose-700'}>
                {preflight.ok ? 'OK' : 'Fail'}
              </strong>{' '}
              {preflight.matches ?? 0}/{preflight.expected ?? 0}
            </span>
            {preflight.audio_count ? <span>{preflight.audio_count} folder audio file(s)</span> : null}
            {preflight.min_required ? <span>Needs {preflight.min_required} match(es)</span> : null}
            {preflight.source_match_ratio !== undefined ? (
              <span>Folder match {formatPercent(preflight.source_match_ratio)}</span>
            ) : null}
            {preflight.acoustid_top_release ? (
              <span className={preflight.acoustid_mismatch ? 'font-semibold text-rose-700' : 'text-zinc-600'}>
                AcoustID {preflight.acoustid_mismatch ? 'mismatch' : 'checked'}: selected{' '}
                {preflight.acoustid_target_hits ?? 0}, top {preflight.acoustid_top_hits ?? 0}
              </span>
            ) : null}
          </>
        ) : null}
      </div>
      {preflight?.acoustid_mismatch ? (
        <div className="rounded border border-rose-200 bg-rose-50 px-2 py-1 text-rose-800">
          {audioMismatchPreflightDetail(preflight)} Keep this in review until a matching release is selected or the wrong source folder is deleted.
        </div>
      ) : null}
      {preflight?.error ? (
        <div className="rounded border border-rose-200 bg-rose-50 px-2 py-1 text-rose-800">
          {preflight.error}
        </div>
      ) : null}
      {examples.length ? (
        <Disclosure>
          <DisclosureButton className="text-xs font-medium text-zinc-700 underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900">
            Track match examples
          </DisclosureButton>
          <DisclosurePanel>
            <ul className="mt-1 space-y-1 rounded border border-graphite-200 bg-graphite-50 p-2 font-mono text-[11px] text-zinc-700">
              {examples.map((example) => (
                <li key={example}>{example.trim()}</li>
              ))}
            </ul>
          </DisclosurePanel>
        </Disclosure>
      ) : null}
    </div>
  );
}

interface TrackRow {
  num: number;
  local_title: string;
  mb_title: string;
  mb_trackid: string;
  status:
    | 'matched'
    | 'fuzzy'
    | 'verified_match'
    | 'acoustid_verified'
    | 'different'
    | 'conflicting'
    | 'missing'
    | 'extra'
    | 'unmatched_extra'
    | 'ignored_for_this_import';
  source_path?: string;
}

const IMPORTABLE_TRACK_STATUSES = new Set<TrackRow['status']>([
  'matched',
  'fuzzy',
  'verified_match',
  'acoustid_verified',
]);

function selectedImportRows(rows: TrackRow[] = []): TrackRow[] {
  return rows.filter((row) => IMPORTABLE_TRACK_STATUSES.has(row.status) && Boolean(row.source_path));
}

function selectedImportSourceFiles(
  match?: Pick<SelectedMatch, 'track_mapping' | 'identity_validated'>,
  preview?: ImportTargetPreviewResponse,
): string[] {
  if (match?.identity_validated === false) return [];
  const conflicted = new Set(
    (preview?.tracks ?? [])
      .filter((track) => track.target_conflict && track.source_path)
      .map((track) => track.source_path),
  );
  const files = selectedImportRows(match?.track_mapping ?? [])
    .map((row) => row.source_path || '')
    .filter((path) => path && !conflicted.has(path));
  return [...new Set(files)];
}

const CLEANUP_TRACK_STATUSES = new Set<TrackRow['status']>([
  'extra',
  'unmatched_extra',
  'ignored_for_this_import',
  'different',
  'conflicting',
]);

function cleanupTrackRows(
  match?: Pick<SelectedMatch, 'track_mapping' | 'identity_validated'>,
  preview?: ImportTargetPreviewResponse,
): Array<Pick<TrackRow, 'status' | 'source_path'>> {
  if (match?.identity_validated === false) return [];
  return [
    ...(match?.track_mapping ?? [])
      .filter((row) => CLEANUP_TRACK_STATUSES.has(row.status) && Boolean(row.source_path))
      .map((row) => ({ status: row.status, source_path: row.source_path || '' })),
    ...(preview?.tracks ?? [])
      .filter((track) => CLEANUP_TRACK_STATUSES.has(track.status as TrackRow['status']) && Boolean(track.source_path))
      .map((track) => ({ status: track.status as TrackRow['status'], source_path: track.source_path || '' })),
  ].filter((row) => Boolean(row.source_path));
}

function selectedCleanupSourceFiles(
  match?: Pick<SelectedMatch, 'track_mapping' | 'identity_validated'>,
  preview?: ImportTargetPreviewResponse,
): string[] {
  const files = cleanupTrackRows(match, preview).map((row) => row.source_path || '').filter(Boolean);
  return [...new Set(files)];
}

function hasDestructiveCleanupMismatch(
  match?: Pick<SelectedMatch, 'track_mapping' | 'identity_validated'>,
  preview?: ImportTargetPreviewResponse,
): boolean {
  return cleanupTrackRows(match, preview).some((row) => row.status === 'conflicting' || row.status === 'different');
}

function unmatchedExtraTrackCount(match?: Pick<SelectedMatch, 'track_mapping'>): number {
  return (match?.track_mapping ?? []).filter((row) => row.status === 'extra').length;
}

interface TrackDataPreflight {
  ok: boolean;
  matches: number;
  expected: number;
  audio_count: number;
  match_ratio: number;
  source_match_ratio: number;
  is_partial_import: boolean;
  extra_count: number;
  error?: string;
}

interface TrackData {
  comparison: TrackRow[];
  matched_count: number;
  fuzzy_count: number;
  extra_count: number;
  mb_track_count: number;
  local_track_count: number;
  mb_releasegroupid?: string;
  selected_release_group_id?: string;
  representative_release_id?: string;
  representative_release_group_id?: string;
  rejected_representative_release_id?: string;
  release_title?: string;
  release_artist?: string;
  date?: string;
  identity_validated?: boolean;
  candidate_identity_error?: string;
  release_group_diagnostics?: Record<string, unknown>;
  preflight?: TrackDataPreflight | null;
}

function buildCandidateSelectedMatch(
  candidate: ReviewCandidate,
  trackData: TrackData | null,
  status: SelectedMatch['preflight_status'] = trackData ? 'passed' : 'not_run',
  reason = '',
): SelectedMatch {
  const releaseGroupId = (trackData?.selected_release_group_id || trackData?.mb_releasegroupid || candidate.mb_releasegroupid || '').trim();
  const representativeReleaseId = (trackData?.representative_release_id || candidate.mb_albumid || '').trim();
  const identityValidated = trackData?.identity_validated !== false;
  const candidateIdentityError = (trackData?.candidate_identity_error || '').trim();
  const totalTracks = trackData?.mb_track_count ?? (typeof candidate.tracks === 'number' ? candidate.tracks : null);
  const localCount = trackData?.local_track_count ?? null;
  const matched = trackData?.matched_count ?? null;
  const mapping = trackData?.comparison ?? [];
  const importableRows = selectedImportRows(mapping);
  const importableTrackCount = importableRows.length;
  const isPartialImport = Boolean(
    trackData && importableTrackCount > 0
    && totalTracks !== null && importableTrackCount < totalTracks,
  );
  const backendPreflightOk = Boolean(trackData?.preflight?.ok);
  const preflightStatus: SelectedMatch['preflight_status'] = trackData
    ? identityValidated && (backendPreflightOk || importableTrackCount > 0) ? 'passed' : 'failed'
    : status;
  const missingFromRelease = totalTracks !== null ? Math.max(0, totalTracks - importableTrackCount) : 0;
  const extraCount = trackData?.extra_count ?? mapping.filter((row) => row.status === 'extra').length;
  const defaultReason = trackData
    ? !identityValidated
      ? candidateIdentityError || 'Representative Release ID rejected: it does not belong to selected Release Group.'
      : isPartialImport
        ? `Partial import ready: ${importableTrackCount} verified track${importableTrackCount === 1 ? '' : 's'} will import.${extraCount > 0 ? ` ${extraCount} unmatched file${extraCount === 1 ? '' : 's'} will stay in review.` : ''}${missingFromRelease > 0 ? ` ${missingFromRelease} album track${missingFromRelease === 1 ? '' : 's'} can be acquired later.` : ''}`
        : preflightStatus === 'passed'
          ? `${importableTrackCount}/${totalTracks ?? 0} tracks matched.`
          : trackData.preflight?.error || 'No track in selected Release Group matches cleaned local title.'
    : 'Track comparison has not finished for this candidate.';
  const isReleaseGroupUsable = isMusicBrainzUuid(releaseGroupId);
  const hasRepresentativeRelease = isMusicBrainzUuid(representativeReleaseId);
  const isImportable = (
    identityValidated &&
    isReleaseGroupUsable &&
    hasRepresentativeRelease &&
    importableTrackCount > 0 &&
    preflightStatus === 'passed'
  );
  const confidenceScore = normalizedScoreValue(scoreValue(candidate));
  let confidenceLevel = confidenceLevelForScore(confidenceScore);
  if (!identityValidated || !isReleaseGroupUsable) {
    confidenceLevel = 'blocked';
  } else if (preflightStatus === 'failed') {
    confidenceLevel = 'not_importable';
  }
  const autoFixEligible = isImportable && confidenceScore !== null && confidenceScore >= AUTO_IMPORT_CONFIDENCE_THRESHOLD;
  const autoFixRequiresReview = false;
  const autoFixReason = selectedMatchAutomationReason(
    confidenceLevel,
    confidenceScore,
    isImportable,
    preflightStatus,
    isReleaseGroupUsable,
    hasRepresentativeRelease,
    importableTrackCount,
    missingFromRelease,
    extraCount,
    reason || defaultReason,
    isPartialImport,
    importableTrackCount || matched,
    totalTracks,
  );

  return {
    release_group_id: releaseGroupId,
    representative_release_id: representativeReleaseId,
    artist: trackData?.release_artist || candidate.artist || '',
    album: trackData?.release_title || candidate.album || '',
    year: String((trackData?.date || '').slice(0, 4) || candidate.date || candidate.year || ''),
    track_match_count: matched,
    total_tracks: totalTracks,
    local_track_count: localCount,
    track_mapping: mapping,
    preflight_status: preflightStatus,
    preflight_reason: reason || defaultReason,
    is_release_group_usable: isReleaseGroupUsable,
    is_importable: isImportable,
    is_partial_import: isPartialImport,
    confidence_score: confidenceScore,
    confidence_level: confidenceLevel,
    auto_fix_eligible: autoFixEligible,
    auto_fix_requires_review: autoFixRequiresReview,
    auto_fix_reason: autoFixReason,
    missing_track_count: missingFromRelease,
    match_count: importableTrackCount || matched,
    preflight_ok: preflightStatus === 'passed' ? true : preflightStatus === 'failed' ? false : null,
    identity_validated: identityValidated,
    candidate_identity_error: candidateIdentityError,
    representative_release_group_id: trackData?.representative_release_group_id || '',
    rejected_representative_release_id: trackData?.rejected_representative_release_id || '',
    release_group_diagnostics: trackData?.release_group_diagnostics || {},
    source: 'candidate',
  };
}
function buildAiSelectedMatch(suggestion: AiSuggestion): SelectedMatch {
  const releaseGroupId = (suggestion.mb_releasegroupid || '').trim();
  const representativeReleaseId = (suggestion.representative_mb_albumid || suggestion.mb_albumid || '').trim();
  const preflight = suggestion.preflight ?? null;
  const mapping = ((suggestion.track_mapping ?? []) as TrackRow[]).filter(Boolean);
  const importableTrackCount = selectedImportRows(mapping).length;
  const extraCount = mapping.filter((row) => row.status === 'extra').length;
  const status: SelectedMatch['preflight_status'] = preflight
    ? (preflight.ok || importableTrackCount > 0) ? 'passed' : 'failed'
    : importableTrackCount > 0 ? 'passed' : 'not_run';
  const matched = suggestion.track_match_count ?? preflight?.matches ?? (importableTrackCount || null);
  const total = suggestion.mb_track_count ?? preflight?.expected ?? null;
  const localCount = suggestion.local_track_count ?? preflight?.audio_count ?? null;
  const missingTrackCount = total !== null ? Math.max(0, total - importableTrackCount) : 0;
  const isPartialImport = Boolean(importableTrackCount > 0 && total !== null && importableTrackCount < total);
  const identityValidated = suggestion.identity_validated !== false;
  const candidateIdentityError = (suggestion.candidate_identity_error || '').trim();
  const isReleaseGroupUsable = isMusicBrainzUuid(releaseGroupId);
  const hasRepresentativeRelease = isMusicBrainzUuid(representativeReleaseId);
  const isImportable = identityValidated && isReleaseGroupUsable && hasRepresentativeRelease && importableTrackCount > 0 && status === 'passed';
  const confidenceScore = normalizedScoreValue(suggestion.confidence_score ?? undefined);
  let confidenceLevel = confidenceScore !== null ? confidenceLevelForScore(confidenceScore) : confidenceLevelFromAi(suggestion.confidence);
  if (!identityValidated || !isReleaseGroupUsable) {
    confidenceLevel = 'blocked';
  } else if (status === 'failed') {
    confidenceLevel = 'not_importable';
  }
  const reason = candidateIdentityError
    || suggestion.preflight_note
    || (isPartialImport
      ? `Partial import ready: ${importableTrackCount} verified track${importableTrackCount === 1 ? '' : 's'} will import.${extraCount > 0 ? ` ${extraCount} unmatched file${extraCount === 1 ? '' : 's'} will stay in review.` : ''}${missingTrackCount > 0 ? ` ${missingTrackCount} album track${missingTrackCount === 1 ? '' : 's'} can be acquired later.` : ''}`
      : preflight
        ? `${matched ?? 0}/${total ?? 0} tracks matched.`
        : 'Track comparison has not finished for this candidate.');
  const autoFixEligible = isImportable && confidenceScore !== null && confidenceScore >= AUTO_IMPORT_CONFIDENCE_THRESHOLD;
  return {
    release_group_id: releaseGroupId,
    representative_release_id: representativeReleaseId,
    artist: suggestion.albumartist || '',
    album: suggestion.album || '',
    year: String(suggestion.year || ''),
    track_match_count: matched,
    total_tracks: total,
    local_track_count: localCount,
    track_mapping: mapping,
    preflight_status: status,
    preflight_reason: reason,
    is_release_group_usable: isReleaseGroupUsable,
    is_importable: isImportable,
    is_partial_import: isPartialImport,
    confidence_score: confidenceScore,
    confidence_level: confidenceLevel,
    auto_fix_eligible: autoFixEligible,
    auto_fix_requires_review: false,
    auto_fix_reason: selectedMatchAutomationReason(
      confidenceLevel,
      confidenceScore,
      isImportable,
      status,
      isReleaseGroupUsable,
      hasRepresentativeRelease,
      importableTrackCount,
      missingTrackCount,
      extraCount,
      reason,
      isPartialImport,
      importableTrackCount || matched,
      total,
    ),
    missing_track_count: missingTrackCount,
    match_count: importableTrackCount || matched,
    preflight_ok: status === 'passed' ? true : status === 'failed' ? false : null,
    identity_validated: identityValidated,
    candidate_identity_error: candidateIdentityError,
    representative_release_group_id: suggestion.representative_release_group_id || '',
    rejected_representative_release_id: suggestion.rejected_representative_release_id || '',
    source: 'ai',
  };
}

function savedSelectedMatch(item: ReviewItem): SelectedMatch | null {
  const suggestion = item.suggestion;
  if (!suggestion?.track_mapping?.length) return null;
  const match = buildAiSelectedMatch(suggestionWithOrigin(item, suggestion));
  return match.track_mapping.length ? match : null;
}
const TRACK_STATUS_COLORS: Record<TrackRow['status'], string> = {
  matched: 'text-emerald-700',
  fuzzy: 'text-amber-600',
  verified_match: 'text-emerald-700',
  acoustid_verified: 'text-emerald-700',
  different: 'text-red-600',
  conflicting: 'text-red-600',
  missing: 'text-zinc-400',
  extra: 'text-zinc-400',
  unmatched_extra: 'text-zinc-400',
  ignored_for_this_import: 'text-zinc-400',
};

const TRACK_STATUS_LABELS: Record<TrackRow['status'], string> = {
  matched: 'Matched',
  fuzzy: 'Fuzzy',
  verified_match: 'Verified',
  acoustid_verified: 'AcoustID verified',
  different: 'Different',
  conflicting: 'Conflict',
  missing: 'Missing',
  extra: 'Extra',
  unmatched_extra: 'Extra',
  ignored_for_this_import: 'Left in review',
};

function fileBasename(path: string): string {
  return path.replace(/\\/g, '/').split('/').pop() || path;
}

function TrackComparisonTable({ tracks }: { tracks: TrackRow[] }) {
  if (!tracks.length) return null;
  const hasSourcePaths = tracks.some((row) => row.source_path);
  return (
    <div className="mt-3 max-h-48 overflow-auto rounded border border-graphite-200">
      <table className="min-w-full text-xs">
        <thead>
          <tr className="border-b border-graphite-200 bg-graphite-50">
            <th className="px-2 py-1.5 text-left font-semibold text-zinc-500">#</th>
            <th className="px-2 py-1.5 text-left font-semibold text-zinc-600">Local Title</th>
            <th className="px-2 py-1.5 text-left font-semibold text-zinc-600">MB Title</th>
            {hasSourcePaths ? <th className="px-2 py-1.5 text-left font-semibold text-zinc-600">File</th> : null}
            <th className="px-2 py-1.5 text-left font-semibold text-zinc-600">Status</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((row) => (
            <tr key={`${row.num}-${row.mb_title}`} className="border-b border-graphite-100 last:border-b-0">
              <td className="px-2 py-1 text-zinc-400">{row.num}</td>
              <td className="max-w-[12rem] truncate px-2 py-1 text-zinc-700" title={row.local_title}>
                {row.local_title || <span className="text-zinc-400">—</span>}
              </td>
              <td className="max-w-[12rem] truncate px-2 py-1 text-zinc-700" title={row.mb_title}>
                {row.mb_title || <span className="text-zinc-400">—</span>}
              </td>
              {hasSourcePaths ? (
                <td className="max-w-[10rem] truncate px-2 py-1 font-mono text-zinc-500" title={row.source_path}>
                  {row.source_path ? fileBasename(row.source_path) : <span className="text-zinc-300">—</span>}
                </td>
              ) : null}
              <td className={`px-2 py-1 font-medium ${TRACK_STATUS_COLORS[row.status]}`}>
                {TRACK_STATUS_LABELS[row.status]}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function shortPath(path: string): string {
  if (!path) return '';
  if (path.length <= 86) return path;
  return `…${path.slice(-83)}`;
}

function TargetPathPreviewPanel({ state }: { state?: TargetPreviewState }) {
  if (!state || state.status === 'idle') return null;
  if (state.status === 'loading') {
    return (
      <div className="mt-3 rounded border border-graphite-200 bg-graphite-50 px-3 py-2 text-xs text-zinc-600">
        Target path preview loading…
      </div>
    );
  }
  if (state.status === 'error') {
    return (
      <div className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
        <div className="font-semibold">Target path preview failed</div>
        <div className="mt-0.5">{state.error || 'Preview unavailable.'}</div>
      </div>
    );
  }
  const preview = state.preview;
  if (!preview) return null;
  const blocked = !preview.safe;
  const existingFolder = Boolean(preview.existing_folder_reuse);
  const cleanupRequired = blocked && preview.next_action === 'verify_or_cleanup_unmatched';
  const borderClass = blocked
    ? cleanupRequired
      ? 'border-amber-200 bg-amber-50 text-amber-900'
      : 'border-rose-200 bg-rose-50 text-rose-900'
    : existingFolder
      ? 'border-sky-200 bg-sky-50 text-sky-900'
      : 'border-emerald-200 bg-emerald-50 text-emerald-900';
  const badgeClass = blocked
    ? cleanupRequired
      ? 'bg-amber-100 text-amber-900'
      : 'bg-rose-100 text-rose-800'
    : existingFolder
      ? 'bg-sky-100 text-sky-800'
      : 'bg-emerald-100 text-emerald-800';
  const badgeLabel = cleanupRequired ? 'Needs cleanup' : blocked ? 'Blocked' : existingFolder ? 'Existing folder' : 'Safe';
  const importCount = preview.tracks_to_import_count ?? preview.track_count;
  const missingCount = preview.missing_album_track_count ?? 0;
  const unmatchedCount = preview.unmatched_extra_count ?? 0;
  const conflictCount = preview.real_conflict_count ?? preview.conflict_count;
  return (
    <div className={`mt-3 rounded border px-3 py-2 text-xs ${borderClass}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-semibold">Target preview</div>
        <span className={`rounded px-2 py-0.5 font-semibold ${badgeClass}`}>{badgeLabel}</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-zinc-700 md:grid-cols-4">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">Import</div>
          <div className="font-semibold">{importCount}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">Missing</div>
          <div className="font-semibold">{missingCount}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">Left in review</div>
          <div className="font-semibold">{unmatchedCount}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">Conflicts</div>
          <div className="font-semibold">{conflictCount}</div>
        </div>
      </div>
      {blocked ? (
        <div className={cleanupRequired ? 'mt-2 text-amber-900' : 'mt-2 text-rose-800'}>
          {cleanupRequired ? (
            <div className="font-medium">No verified tracks are selected after automatic verification. Use guarded cleanup for the unmatched source file or choose another match.</div>
          ) : null}
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {(preview.blocked_reasons || ['Target path preview is blocked.']).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : existingFolder ? (
        <div className="mt-2 font-medium text-sky-800">
          Existing album folder found. Verified tracks will be imported into it.
        </div>
      ) : (
        <div className="mt-2 font-medium text-emerald-800">Target path preview passed.</div>
      )}
      {preview.warnings?.length ? (
        <ul className="mt-2 list-disc space-y-1 pl-4 text-amber-800">
          {preview.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
      <Disclosure>
        <DisclosureButton className="mt-2 text-xs font-medium text-zinc-700 underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900">
          Show target details
        </DisclosureButton>
        <DisclosurePanel>
          <div className="mt-2 grid gap-1 text-zinc-700 md:grid-cols-2">
            <div><span className="font-medium">Final album folder:</span> {preview.album_folder}</div>
            <div><span className="font-medium">Release Group ID:</span> <span className="font-mono">{preview.release_group_id || 'missing'}</span></div>
            <div><span className="font-medium">Target folder exists:</span> {preview.target_folder_exists ? 'yes' : 'no'}</div>
            <div><span className="font-medium">Tracks to import:</span> {importCount}</div>
            <div><span className="font-medium">Unmatched left behind:</span> {unmatchedCount}</div>
            {preview.rejected_cleanup_count ? (
              <div><span className="font-medium">Rejected cleanup:</span> {preview.rejected_cleanup_count}</div>
            ) : null}
            {preview.cleanup_required_count ? (
              <div><span className="font-medium">Cleanup candidates:</span> {preview.cleanup_required_count}</div>
            ) : null}
            <div><span className="font-medium">Missing album tracks:</span> {missingCount}</div>
            <div><span className="font-medium">Real conflicts:</span> {conflictCount}</div>
            {preview.already_imported_count ? (
              <div><span className="font-medium">Already imported:</span> {preview.already_imported_count}</div>
            ) : null}
            <div><span className="font-medium">Placeholder warnings:</span> {preview.placeholder_warning_count}</div>
            <div><span className="font-medium">Release-ID path warnings:</span> {preview.release_id_path_warning_count}</div>
          </div>
          <div className="mt-2 truncate font-mono text-[11px] text-zinc-600" title={preview.album_path}>
            {preview.album_path}
          </div>
          {preview.tracks?.length ? (
            <Disclosure>
              <DisclosureButton className="mt-2 text-xs font-medium text-zinc-700 underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900">
                Track target paths ({preview.tracks.length})
              </DisclosureButton>
              <DisclosurePanel>
                <div className="mt-2 max-h-72 overflow-auto rounded border border-graphite-200 bg-white">
                  <table className="min-w-full text-[11px]">
                    <thead>
                      <tr className="border-b border-graphite-200 bg-graphite-50 text-left text-zinc-500">
                        <th className="px-2 py-1 font-semibold">#</th>
                        <th className="px-2 py-1 font-semibold">Current source</th>
                        <th className="px-2 py-1 font-semibold">Proposed target</th>
                        <th className="px-2 py-1 font-semibold">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.tracks.map((track) => (
                        <tr key={`${track.track}-${track.target_path}`} className="border-b border-graphite-100 last:border-b-0">
                          <td className="px-2 py-1 text-zinc-500">{track.track}</td>
                          <td className="max-w-[15rem] px-2 py-1 font-mono text-zinc-600" title={track.source_path}>
                            {track.source_path ? shortPath(track.source_path) : <span className="text-zinc-400">missing source</span>}
                          </td>
                          <td className="max-w-[18rem] px-2 py-1 font-mono text-zinc-700" title={track.target_path}>
                            {shortPath(track.target_path)}
                          </td>
                          <td className={`px-2 py-1 font-medium ${
                            track.target_conflict || track.unresolved_placeholder || track.uses_release_id_in_path
                              ? 'text-rose-700'
                              : track.already_imported
                                ? 'text-sky-600'
                                : track.status === 'extra'
                                  ? 'text-amber-600'
                                  : 'text-emerald-700'
                          }`}>
                            {track.already_imported
                              ? 'already imported'
                              : track.target_conflict
                                ? 'target exists'
                                : track.unresolved_placeholder
                                  ? 'placeholder'
                                  : track.uses_release_id_in_path
                                    ? 'uses release ID'
                                    : track.status === 'extra'
                                      ? 'extra (left behind)'
                                      : track.status}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </DisclosurePanel>
            </Disclosure>
          ) : null}
        </DisclosurePanel>
      </Disclosure>
    </div>
  );
}

function CandidateCarousel({
  candidates,
  selectedMbid,
  folder,
  onUseCandidate,
  onSelectMatch,
}: {
  candidates: ReviewCandidate[];
  selectedMbid?: string;
  folder?: string;
  onUseCandidate: (mbid: string) => void;
  onSelectMatch?: (match: SelectedMatch) => void;
}) {
  const initialIndex = useMemo(() => {
    if (!selectedMbid) return 0;
    const idx = candidates.findIndex(
      (c) => sameMbid(c.mb_albumid, selectedMbid) || sameMbid(c.mb_releasegroupid, selectedMbid),
    );
    return idx >= 0 ? idx : 0;
  }, [candidates, selectedMbid]);

  const [currentIndex, setCurrentIndex] = useState(initialIndex);
  const [trackData, setTrackData] = useState<TrackData | null>(null);
  const [trackLoading, setTrackLoading] = useState(false);
  const [trackError, setTrackError] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);
  const onUseCandidateRef = useRef(onUseCandidate);
  const onSelectMatchRef = useRef(onSelectMatch);

  const total = candidates.length;
  const current = candidates[currentIndex] ?? null;

  useEffect(() => {
    onUseCandidateRef.current = onUseCandidate;
    onSelectMatchRef.current = onSelectMatch;
  }, [onSelectMatch, onUseCandidate]);

  const goTo = useCallback((index: number) => {
    const clamped = Math.max(0, Math.min(total - 1, index));
    setCurrentIndex(clamped);
    setTrackData(null);
    setTrackError('');
  }, [total]);

  useEffect(() => {
    setCurrentIndex(initialIndex);
    setTrackData(null);
    setTrackError('');
  }, [initialIndex]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase() ?? '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); goTo(currentIndex - 1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); goTo(currentIndex + 1); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [currentIndex, goTo]);

  useEffect(() => {
    const mbid = current?.mb_albumid;
    if (!mbid) { setTrackData(null); return; }
    let active = true;
    setTrackLoading(true);
    setTrackData(null);
    setTrackError('');
    const pendingMatch = buildCandidateSelectedMatch(current, null, 'not_run');
    if (pendingMatch.release_group_id) {
      onUseCandidateRef.current(pendingMatch.release_group_id);
      onSelectMatchRef.current?.(pendingMatch);
    }
    const params = new URLSearchParams();
    if (folder) params.set('folder', folder);
    const selectedRgid = current.mb_releasegroupid || pendingMatch.release_group_id;
    if (selectedRgid) params.set('release_group_id', selectedRgid);
    const qs = params.size ? `?${params.toString()}` : '';
    apiGet<TrackData & { ok: boolean }>
      (`/api/candidates/${mbid}/tracks${qs}`)
      .then((data) => {
        if (!active) return;
        if (data.ok) {
          setTrackData(data);
          const selected = buildCandidateSelectedMatch(current, data);
          if (selected.release_group_id) {
            onUseCandidateRef.current(selected.release_group_id);
          }
          onSelectMatchRef.current?.(selected);
        }
      })
      .catch((err) => {
        if (!active) return;
        const message = err instanceof Error ? err.message : String(err);
        setTrackError(message);
        onSelectMatchRef.current?.(buildCandidateSelectedMatch(current, null, 'failed', `Track comparison failed: ${message}`));
      })
      .finally(() => {
        if (active) setTrackLoading(false);
      });
    return () => { active = false; };
  }, [current, current?.mb_albumid, folder]);

  if (!candidates.length || !current) return null;

  const mbid = current.mb_albumid ?? '';
  const rgid = current.mb_releasegroupid || trackData?.mb_releasegroupid || '';
  const rgUrl = current.mb_releasegroupurl ?? (rgid ? `https://musicbrainz.org/release-group/${rgid}` : '');
  const mbUrl = current.mb_url || musicBrainzUrl(mbid);
  const score = scoreValue(current);
  const normalizedScore = normalizedScoreValue(score);
  const isAiPick = sameMbid(mbid, selectedMbid) || sameMbid(rgid, selectedMbid);

  const matchSummary = trackData
    ? `${trackData.matched_count}${trackData.fuzzy_count ? ` + ${trackData.fuzzy_count} fuzzy` : ''} / ${trackData.mb_track_count} tracks matched`
    : current.tracks
      ? `${current.tracks} tracks`
      : '';
  const visibleMatch = buildCandidateSelectedMatch(current, trackData);
  const visibleConfidence = rgid
    ? visibleMatch.confidence_level
    : 'blocked';

  return (
    <div ref={containerRef} className="mt-3 rounded border border-graphite-200 bg-white">
      <div className="flex items-center gap-2 border-b border-graphite-200 px-3 py-1.5">
        <button
          aria-label="Previous match"
          className="flex h-8 w-8 items-center justify-center rounded text-lg font-bold text-zinc-600 transition-colors hover:bg-graphite-100 disabled:cursor-not-allowed disabled:opacity-30"
          disabled={currentIndex === 0}
          onClick={() => goTo(currentIndex - 1)}
        >
          ←
        </button>
        <span className="flex-1 text-center text-xs font-semibold text-zinc-600">
          Match {currentIndex + 1} of {total}
        </span>
        <button
          aria-label="Next match"
          className="flex h-8 w-8 items-center justify-center rounded text-lg font-bold text-zinc-600 transition-colors hover:bg-graphite-100 disabled:cursor-not-allowed disabled:opacity-30"
          disabled={currentIndex === total - 1}
          onClick={() => goTo(currentIndex + 1)}
        >
          →
        </button>
      </div>

      <div className="px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`text-sm font-semibold ${scoreClass(score)}`}>{formatScore(score)}</span>
              <Chip
                size="small"
                color={confidenceChipColor(visibleConfidence)}
                label={normalizedScore !== null
                  ? `${confidenceLabel(visibleConfidence)} (${formatPercent(normalizedScore)})`
                  : confidenceLabel(visibleConfidence)}
              />
              {visibleMatch.auto_fix_eligible ? (
                <Chip
                  size="small"
                  color="success"
                  label={visibleMatch.is_partial_import ? 'Partial auto-import ready' : 'Auto-import eligible'}
                />
              ) : null}
              {isAiPick ? <Chip size="small" color="primary" label="AI pick" /> : null}
              {current.is_current ? <Chip size="small" color="success" label="current" /> : null}
              {current.is_vinyl ? <Chip size="small" color="warning" label="vinyl" /> : null}
            </div>
            <h3 className="mt-1 text-sm font-semibold text-zinc-900">{candidateName(current)}</h3>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-600">
              {candidateMeta(current).map((part) => (
                <span key={part}>{part}</span>
              ))}
            </div>
            {rgid ? (
              <div className="mt-1.5 text-xs text-zinc-500">
                <span className="font-medium">Release group:</span>{' '}
                {rgUrl ? (
                  <a className="font-mono underline decoration-zinc-400 hover:text-zinc-700" href={rgUrl} target="_blank" rel="noreferrer">
                    {rgid}
                  </a>
                ) : (
                  <span className="font-mono">{rgid}</span>
                )}
                {current.release_group_primary_type ? (
                  <span className="ml-2 rounded bg-graphite-100 px-1 py-0.5 text-zinc-500">{current.release_group_primary_type}</span>
                ) : null}
              </div>
            ) : mbid ? (
              <div className="mt-1 truncate font-mono text-xs text-zinc-500">{mbid}</div>
            ) : null}
            {matchSummary ? (
              <div className="mt-1 text-xs font-medium text-zinc-600">{matchSummary}</div>
            ) : null}
            {visibleMatch.auto_fix_reason ? (
              <div className={`mt-2 rounded border px-2 py-1 text-xs ${
                visibleMatch.auto_fix_eligible
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                  : 'border-graphite-200 bg-graphite-50 text-zinc-600'
              }`}>
                {visibleMatch.auto_fix_reason}
              </div>
            ) : null}
            {current.acoustid_hits || current.acoustid_release_hits ? (
              <span className="mt-1 inline-block rounded border border-sky-200 bg-sky-50 px-2 py-0.5 text-xs text-sky-700">
                {current.acoustid_hits || current.acoustid_release_hits} AcoustID hit(s)
              </span>
            ) : null}
          </div>

          <div className="flex shrink-0 flex-col items-end gap-2">
            {rgUrl ? (
              <Button size="small" variant="text" href={rgUrl} target="_blank" rel="noreferrer">
                Release Group
              </Button>
            ) : mbUrl ? (
              <Button size="small" variant="text" href={mbUrl} target="_blank" rel="noreferrer">
                Representative Release
              </Button>
            ) : null}
            {(() => {
              const matchRatio = trackData ? trackData.matched_count / (trackData.mb_track_count || 1) : null;
              const trackPreflightOk: boolean | null = matchRatio !== null ? matchRatio >= 0.60 : null;
              const trackPreflightFails = trackPreflightOk === false;
              const matchDesc = trackData
                ? `${trackData.matched_count}/${trackData.mb_track_count} tracks matched`
                : null;

              if (rgid) {
                if (trackPreflightFails) {
                  return (
                    <div className="flex flex-col gap-1.5 items-end">
                      <Button
                        size="small"
                        variant="outlined"
                        color="primary"
                        title="Save this Release Group ID to the field without importing. Clears the preflight block so you can choose when to import."
                        onClick={() => {
                          onUseCandidate(rgid);
                          onSelectMatch?.({
                            ...buildCandidateSelectedMatch(current, trackData, 'stale', 'Release Group ID saved; preflight must pass before import.'),
                            preflight_status: 'stale',
                            preflight_ok: null,
                            is_importable: false,
                            auto_fix_eligible: false,
                            auto_fix_requires_review: false,
                            auto_fix_reason: 'Auto-fix blocked until preflight is refreshed for this selected candidate.',
                            source: 'manual',
                          });
                        }}
                      >
                        Save ID only
                      </Button>
                      <Button
                        size="small"
                        variant="outlined"
                        color="warning"
                        disabled
                        title={matchDesc ? `Import blocked: ${matchDesc} matched (need ≥60%)` : 'Import blocked: preflight failed'}
                      >
                        Import blocked
                      </Button>
                    </div>
                  );
                }
                return (
                  <Button
                    size="small"
                    variant="contained"
                    color="primary"
                    onClick={() => {
                      onUseCandidate(rgid);
                      onSelectMatch?.(buildCandidateSelectedMatch(current, trackData));
                    }}
                  >
                    Use This Match
                  </Button>
                );
              }
              if (mbid) {
                return (
                  <Button size="small" variant="outlined" color="warning" disabled
                    title="No release-group ID — cannot use as canonical match">
                    Use This Match
                  </Button>
                );
              }
              return null;
            })()}
          </div>
        </div>

        {trackLoading ? (
          <div className="mt-3 text-xs text-zinc-400">Loading track comparison…</div>
        ) : trackError ? (
          <div className="mt-3 text-xs text-rose-500">Track fetch failed: {trackError}</div>
        ) : trackData ? (
          <Disclosure>
            <DisclosureButton className="mt-3 text-xs font-medium text-zinc-700 underline decoration-zinc-400 underline-offset-2 hover:text-zinc-900">
              Track matches ({trackData.matched_count}/{trackData.mb_track_count})
            </DisclosureButton>
            <DisclosurePanel>
              <TrackComparisonTable tracks={trackData.comparison} />
            </DisclosurePanel>
          </Disclosure>
        ) : null}
      </div>
    </div>
  );
}

function AiSuggestionPanel({
  response,
  folder,
  onUseCandidate,
  onSelectMatch,
}: {
  response?: AiSuggestResponse;
  folder?: string;
  onUseCandidate: (mbid: string) => void;
  onSelectMatch?: (match: SelectedMatch) => void;
}) {
  if (!response) return null;
  const suggestion = response.suggestion;
  const candidates = response.mb_candidates ?? [];
  const evidence = response.evidence ?? suggestion?.review_evidence;

  if (!suggestion && !candidates.length) return null;
  const hasSuggestedRelease = Boolean(suggestion?.mb_albumid || suggestion?.mb_valid);
  const suggestionPreflightOk = suggestion?.preflight?.ok;
  const suggestionPreflightFailed = suggestionPreflightOk === false;
  const hasRgid = Boolean(suggestion?.mb_releasegroupid);

  return (
    <div className="mt-3 rounded border border-red-200 bg-red-50 p-3">
      {suggestion ? (
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`text-xs font-semibold ${confidenceClass(suggestion.confidence)}`}>
              {hasSuggestedRelease ? `AI ${suggestion.confidence || 'unknown'}` : 'No MB candidate'}
            </span>
            {hasRgid && suggestion.mb_valid
              ? <Chip size="small" color="success" label="Valid Release Group ID" />
              : suggestion.mb_valid
              ? <Chip size="small" color="info" label="Valid MB release" />
              : null}
            {suggestionPreflightFailed ? (
              <>
                <Chip size="small" color="error" label="Preflight failed" />
                <Chip size="small" color="error" label="Not importable" />
              </>
            ) : suggestionPreflightOk === true ? (
              <Chip size="small" color="success" label="Preflight passed" />
            ) : null}
          </div>
          {suggestionPreflightFailed && suggestion.preflight ? (
            <p className="mt-1 text-xs font-medium text-rose-700">
              {`Tracklist check: ${suggestion.preflight.matches ?? 0} of ${suggestion.preflight.expected ?? 0} tracks matched — import blocked.`}
            </p>
          ) : null}
          {suggestion.reason ? <p className="mt-1 text-sm text-zinc-700">{suggestion.reason}</p> : null}
          <EvidenceSummary evidence={evidence} />
        </div>
      ) : null}

      {candidates.length ? (
        <CandidateCarousel
          candidates={candidates}
          folder={folder}
          selectedMbid={suggestion?.mb_releasegroupid || suggestion?.mb_albumid}
          onUseCandidate={onUseCandidate}
          onSelectMatch={onSelectMatch}
        />
      ) : null}
    </div>
  );
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function MbsubmitDialog({
  albumId,
  albumTitle,
  onClose,
}: {
  albumId: number;
  albumTitle: string;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<'loading' | 'done' | 'error'>('loading');
  const [output, setOutput] = useState('');

  useEffect(() => {
    let cancelled = false;
    albumMbsubmit(albumId)
      .then(async (started) => {
        for (let i = 0; i < 30; i++) {
          await wait(2000);
          if (cancelled) return;
          const job = await getJob(started.job_id);
          if (job.status === 'success' || job.status === 'failed') {
            if (!cancelled) {
              setOutput((job.log ?? []).join('\n'));
              setStatus(job.status === 'success' ? 'done' : 'error');
            }
            return;
          }
        }
        if (!cancelled) { setOutput('Timed out waiting for job'); setStatus('error'); }
      })
      .catch((e: unknown) => {
        if (!cancelled) { setOutput(String(e)); setStatus('error'); }
      });
    return () => { cancelled = true; };
  }, [albumId]);

  return (
    <Dialog open onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/40" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-2xl rounded-xl bg-white p-6 shadow-xl">
          <DialogTitle className="mb-3 text-base font-semibold text-zinc-900">
            MusicBrainz Submission — {albumTitle}
          </DialogTitle>
          {status === 'loading' ? (
            <LinearProgress />
          ) : (
            <pre className="max-h-96 overflow-auto rounded border border-graphite-200 bg-graphite-50 p-3 text-xs text-zinc-800 whitespace-pre-wrap">
              {output || '(no output)'}
            </pre>
          )}
          {status === 'done' ? (
            <p className="mt-3 text-xs text-zinc-500">
              Copy the text above and submit at{' '}
              <a href="https://musicbrainz.org/release/add" target="_blank" rel="noreferrer" className="underline">
                musicbrainz.org/release/add
              </a>
              . Once published, use "Add MBIDs" to attach the new IDs.
            </p>
          ) : null}
          <div className="mt-4 flex justify-end gap-2">
            {status === 'done' ? (
              <Button
                size="small"
                variant="outlined"
                onClick={() => navigator.clipboard.writeText(output).catch(() => undefined)}
              >
                Copy
              </Button>
            ) : null}
            <Button size="small" variant="contained" onClick={onClose}>Close</Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function AddMbidsDialog({
  albumId,
  albumTitle,
  onClose,
  onSuccess,
}: {
  albumId: number;
  albumTitle: string;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [artistId, setArtistId] = useState('');
  const [rgId, setRgId] = useState('');
  const [releaseId, setReleaseId] = useState('');
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [message, setMessage] = useState('');

  const valid = UUID_RE.test(artistId.trim()) && UUID_RE.test(rgId.trim()) &&
    (releaseId.trim() === '' || UUID_RE.test(releaseId.trim()));

  const handleSubmit = async () => {
    if (!valid) return;
    setStatus('running');
    setMessage('');
    try {
      const started = await albumAddMbids(albumId, {
        mb_albumartistid: artistId.trim().toLowerCase(),
        mb_releasegroupid: rgId.trim().toLowerCase(),
        mb_albumid: releaseId.trim().toLowerCase() || undefined,
      });
      for (let i = 0; i < 30; i++) {
        await wait(2000);
        const job = await getJob(started.job_id);
        if (job.status === 'success') { setStatus('done'); setMessage('MBIDs applied and album moved.'); onSuccess(); return; }
        if (job.status === 'failed') { setStatus('error'); setMessage((job.log ?? []).slice(-1)[0] || 'Job failed'); return; }
      }
      setStatus('error');
      setMessage('Timed out waiting for job');
    } catch (e: unknown) {
      setStatus('error');
      setMessage(String(e));
    }
  };

  return (
    <Dialog open onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/40" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-md rounded-xl bg-white p-6 shadow-xl">
          <DialogTitle className="mb-4 text-base font-semibold text-zinc-900">
            Add MusicBrainz IDs — {albumTitle}
          </DialogTitle>
          <div className="flex flex-col gap-3">
            <TextField
              label="Album Artist MBID (required)"
              value={artistId}
              onChange={(e) => setArtistId(e.target.value)}
              size="small"
              fullWidth
              error={artistId.trim() !== '' && !UUID_RE.test(artistId.trim())}
              helperText="mb_albumartistid — the MusicBrainz artist UUID"
              disabled={status === 'running' || status === 'done'}
            />
            <TextField
              label="Release Group MBID (required)"
              value={rgId}
              onChange={(e) => setRgId(e.target.value)}
              size="small"
              fullWidth
              error={rgId.trim() !== '' && !UUID_RE.test(rgId.trim())}
              helperText="mb_releasegroupid — the canonical album identity UUID"
              disabled={status === 'running' || status === 'done'}
            />
            <TextField
              label="Release MBID (optional)"
              value={releaseId}
              onChange={(e) => setReleaseId(e.target.value)}
              size="small"
              fullWidth
              error={releaseId.trim() !== '' && !UUID_RE.test(releaseId.trim())}
              helperText="mb_albumid — specific release UUID (leave blank to skip)"
              disabled={status === 'running' || status === 'done'}
            />
          </div>
          {message ? (
            <Alert severity={status === 'error' ? 'error' : 'success'} sx={{ mt: 2 }}>
              {message}
            </Alert>
          ) : null}
          {status === 'running' ? <LinearProgress sx={{ mt: 2 }} /> : null}
          <div className="mt-4 flex justify-end gap-2">
            <Button size="small" variant="text" color="inherit" onClick={onClose} disabled={status === 'running'}>
              Cancel
            </Button>
            {status === 'done' ? (
              <Button size="small" variant="contained" onClick={onClose}>Done</Button>
            ) : (
              <Button
                size="small"
                variant="contained"
                onClick={handleSubmit}
                disabled={!valid || status === 'running'}
              >
                Apply MBIDs
              </Button>
            )}
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function ReviewCard({
  item,
  mbid,
  suggestion,
  actionState,
  selectedMatch,
  targetPreviewState,
  onMbidChange,
  onUseCandidate,
  onSelectMatch,
  onSuggest,
  onApply,
  onDismiss,
  onDeleteFolder,
  onCleanupFiles,
  importJobActive,
}: {
  item: ReviewItem;
  mbid: string;
  suggestion?: AiSuggestResponse;
  actionState?: ActionState;
  selectedMatch?: SelectedMatch;
  targetPreviewState?: TargetPreviewState;
  onMbidChange: (value: string) => void;
  onUseCandidate: (mbid: string) => void;
  onSelectMatch?: (match: SelectedMatch) => void;
  onSuggest: () => void;
  onApply: () => void;
  onDismiss: () => void;
  onDeleteFolder: () => void;
  onCleanupFiles: () => void;
  importJobActive?: boolean;
}) {
  const selectedConfidence = selectedMatch && selectedMatch.source !== 'manual'
    ? selectedMatch.confidence_level
    : null;
  const selectedConfidenceScore = selectedMatch?.confidence_score ?? null;
  const selectedConfidenceText = selectedConfidence
    ? `${confidenceLabel(selectedConfidence)}${selectedConfidenceScore !== null ? ` (${formatPercent(selectedConfidenceScore)})` : ''}`
    : '';
  const meta = [
    item.artist,
    item.year ? String(item.year) : '',
    item.tracks ? `${item.tracks} track${item.tracks === 1 ? '' : 's'}` : '',
    selectedConfidenceText || (item.confidence ? `${item.confidence} confidence` : ''),
  ].filter(Boolean);
  const mbUrl = item.mb_url || musicBrainzUrl(item.mb_albumid);
  const itemReleaseGroupId = item.mb_releasegroupid || item.evidence?.preflight?.release_group || '';
  const itemReleaseGroupUrl = item.mb_releasegroupurl || musicBrainzReleaseGroupUrl(itemReleaseGroupId);
  const busy = actionState?.status === 'running';
  const [mbsubmitOpen, setMbsubmitOpen] = useState(false);
  const [addMbidsOpen, setAddMbidsOpen] = useState(false);
  const isLibraryNoMb = item.type === 'library_no_mb' && Boolean(item.album_id);
  const matchBucket = itemMatchBucket(item);
  const selectedMatchActive = Boolean(selectedMatch && selectedMatch.source !== 'manual');
  const audioMismatchActive = hasAudioMismatchEvidence(item);
  const applyBlockedReason = applyBlockReason(item, mbid, selectedMatch, targetPreviewState);
  const blockedActive = shouldShowBlockedBucket(item, mbid, selectedMatch, targetPreviewState);
  const visibleMatchBucket: MatchBucket = audioMismatchActive
    ? 'audio_mismatch'
    : blockedActive
      ? 'blocked'
      : selectedMatchActive
        ? selectedMatch?.preflight_status === 'passed'
          ? 'ready'
          : selectedMatch?.preflight_status === 'failed'
            ? 'failed'
            : matchBucket
        : matchBucket;
  const matchNote = matchReviewNote(item);
  const wrongSourceNote = wrongSourceEvidenceNote(item);
  const staleStoredReason = selectedMatchActive && /preflight failed|failed tracklist preflight|rejected selected musicbrainz release/i.test(item.reason || '');
  const visibleReason = blockedActive || staleStoredReason ? '' : item.reason;
  const visibleMatchNote = selectedMatchActive && !audioMismatchActive ? '' : matchNote;
  const visibleBlockReason = applyBlockedReason || storedBlockedReason(item);
  const visibleBlockHint = applyBlockedReason
    ? blockedActionHint(applyBlockedReason)
    : storedBlockedNextAction(item) || (visibleBlockReason ? blockedActionHint(visibleBlockReason) : '');
  const queueingImport = actionState?.status === 'running' && /queueing/i.test(actionState.message || '');
  const lifecycleLabel = queueingImport
    ? 'Import enqueueing'
    : item.status === 'format_policy_rejected'
      ? 'Format policy rejected'
      : item.status && item.status !== 'Pending AI'
        ? item.status.replace(/_/g, ' ')
        : typeLabels[item.type];
  const lifecycleTone = queueingImport
    ? 'border-sky-200 bg-sky-50 text-sky-800'
    : item.status === 'format_policy_rejected'
      ? 'border-amber-300 bg-amber-50 text-amber-900'
      : item.status === 'auto_enqueue_failed'
        ? 'border-rose-200 bg-rose-50 text-rose-800'
        : typeTones[item.type];
  const originDisplay = itemOriginLabel(item);
  const representativeRelease =
    selectedMatch?.representative_release_id &&
    selectedMatch.representative_release_id !== selectedMatch.release_group_id
      ? selectedMatch.representative_release_id
      : null;
  const selectedTrackReason = selectedMatch?.track_match_count !== null &&
    selectedMatch?.track_match_count !== undefined &&
    selectedMatch?.total_tracks !== null &&
    selectedMatch?.total_tracks !== undefined
      ? `${selectedMatch.track_match_count}/${selectedMatch.total_tracks} tracks matched.`
      : '';
  const selectedPreflightReason = selectedMatch?.preflight_reason === selectedTrackReason
    ? ''
    : selectedMatch?.preflight_reason || '';
  const cleanupFiles = selectedCleanupSourceFiles(selectedMatch, targetPreviewState?.preview);
  const cleanupPurgeReady = hasDestructiveCleanupMismatch(selectedMatch, targetPreviewState?.preview);
  const cleanupReady = cleanupFiles.length > 0;

  return (
    <Card variant="outlined" sx={{ borderRadius: 2, borderColor: 'rgba(15, 23, 42, 0.12)' }}>
      <CardContent>
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`rounded border px-2 py-1 text-xs font-semibold ${lifecycleTone}`}>
                {lifecycleLabel}
              </span>
              <span className={`rounded border px-2 py-1 text-xs font-semibold ${matchTones[visibleMatchBucket]}`}>
                {matchLabels[visibleMatchBucket]}
              </span>
              <span className="rounded border border-sky-200 bg-sky-50 px-2 py-1 text-xs font-semibold text-sky-800">
                {originDisplay}
              </span>
              {selectedConfidence ? (
                <span className={`text-xs font-semibold ${confidenceClassForLevel(selectedConfidence)}`}>
                  {selectedConfidenceText}
                </span>
              ) : item.confidence ? (
                <span className={`text-xs font-semibold ${confidenceClass(item.confidence)}`}>{item.confidence}</span>
              ) : null}
              {selectedMatch?.auto_fix_eligible && selectedMatch.source !== 'manual' ? (
                <Chip
                  size="small"
                  color="success"
                  label={selectedMatch.is_partial_import ? 'Partial auto-import ready' : 'Auto-import eligible'}
                />
              ) : null}
              {selectedMatchActive && selectedMatch?.is_release_group_usable && selectedMatch.identity_validated !== false ? (
                <Chip size="small" color="success" label="Release Group ID ready" />
              ) : item.mb_valid ? (
                <Chip size="small" color="success" label="MB valid" />
              ) : null}
            </div>

            <h2 className="mt-3 truncate text-lg font-semibold text-zinc-950">{itemTitle(item)}</h2>
            {meta.length ? <div className="mt-1 text-sm text-zinc-600">{meta.join(' / ')}</div> : null}

            {visibleReason ? <p className="mt-3 text-sm text-zinc-700">{visibleReason}</p> : null}
            {item.path ? <div className="mt-3 truncate font-mono text-xs text-zinc-500">{item.path}</div> : null}
            {itemReleaseGroupId ? (
              <div className="mt-2 text-xs text-zinc-500">
                <span className="font-medium">MusicBrainz Release Group ID:</span>{' '}
                {itemReleaseGroupUrl ? (
                  <a className="font-mono underline decoration-zinc-400 hover:text-zinc-700" href={itemReleaseGroupUrl} target="_blank" rel="noreferrer">
                    {itemReleaseGroupId}
                  </a>
                ) : (
                  <span className="font-mono">{itemReleaseGroupId}</span>
                )}
              </div>
            ) : null}
            {visibleMatchNote ? (
              <div className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                {visibleMatchNote}
              </div>
            ) : null}

            {/* Once a visible candidate is selected, show that candidate's state instead of stale stored evidence. */}
            {selectedMatch && selectedMatch.source !== 'manual' ? (
              <div className="mt-3 space-y-2 text-xs text-zinc-600">
                <div>
                  <span>Selected candidate: </span>
                  {selectedMatch.track_match_count !== null && selectedMatch.total_tracks !== null ? (
                    <strong className={
                      selectedMatch.preflight_status === 'passed'
                        ? 'text-emerald-700'
                        : selectedMatch.preflight_status === 'failed'
                          ? 'text-rose-700'
                          : 'text-amber-600'
                    }>
                      {selectedMatch.track_match_count}/{selectedMatch.total_tracks} tracks matched
                    </strong>
                  ) : (
                    <strong className="text-amber-600">preflight {selectedMatch.preflight_status.replace('_', ' ')}</strong>
                  )}
                  {selectedPreflightReason ? (
                    <span className="ml-1 text-zinc-500">{selectedPreflightReason}</span>
                  ) : null}
                </div>
                <div className={`rounded border px-2 py-1.5 ${
                  selectedMatch.is_partial_import
                    ? 'border-sky-200 bg-sky-50 text-sky-900'
                    : selectedMatch.auto_fix_eligible
                      ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                      : 'border-graphite-200 bg-graphite-50 text-zinc-600'
                }`}>
                  <div className="font-semibold">
                    {selectedMatch.is_partial_import
                      ? 'Partial import ready'
                      : selectedMatch.auto_fix_eligible
                        ? 'Auto-import eligible'
                        : confidenceLabel(selectedMatch.confidence_level)}
                  </div>
                  <div className="mt-0.5">{selectedMatch.auto_fix_reason}</div>
                  {!selectedMatch.is_partial_import && selectedMatch.missing_track_count > 0 ? (
                    <div className="mt-1 font-medium text-amber-800">
                      Incomplete: {selectedMatch.missing_track_count} release track{selectedMatch.missing_track_count === 1 ? '' : 's'} still need import/acquisition.
                    </div>
                  ) : null}
                </div>
              </div>
            ) : (
              <EvidenceSummary item={item} />
            )}
            {!suggestion && (item.evidence?.top_candidates ?? []).length > 0 ? (
              <CandidateCarousel
                candidates={item.evidence?.top_candidates ?? []}
                folder={item.path}
                selectedMbid={mbid}
                onUseCandidate={onUseCandidate}
                onSelectMatch={onSelectMatch}
              />
            ) : null}
            <AiSuggestionPanel
              response={suggestion}
              folder={item.path}
              onUseCandidate={onUseCandidate}
              onSelectMatch={onSelectMatch}
            />
            <TargetPathPreviewPanel state={targetPreviewState} />

            {actionState?.message ? (
              <Alert severity={actionTone(actionState)} sx={{ mt: 2 }}>
                {actionState.message}
              </Alert>
            ) : null}
          </div>

          <div className="flex w-full shrink-0 flex-col gap-2 xl:w-80">
            {mbUrl ? (
              <Button size="small" variant="outlined" href={mbUrl} target="_blank" rel="noreferrer">
                MusicBrainz
              </Button>
            ) : null}
            {item.album_id ? <Chip size="small" variant="outlined" label={`Album ${item.album_id}`} /> : null}

            <TextField
              label="MusicBrainz Release Group ID"
              value={mbid}
              onChange={(event) => onMbidChange(event.target.value)}
              size="small"
              fullWidth
              disabled={busy}
              helperText="The canonical album identity. Enter a release-group UUID."
            />

            {representativeRelease ? (
              <div className="rounded border border-graphite-200 bg-graphite-50 px-2 py-1.5 text-xs text-zinc-500">
                <div className="font-medium text-zinc-600">Representative Release ID</div>
                <div className="font-mono truncate">{representativeRelease}</div>
                <div className="mt-0.5 text-zinc-400">Used only for tracklist comparison or beets compatibility.</div>
              </div>
            ) : null}

            {blockedActive && visibleBlockReason ? (
              <div className="rounded border border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-950">
                <div className="font-semibold">Import blocked</div>
                <div className="mt-0.5">{visibleBlockReason}</div>
                {visibleBlockHint ? <div className="mt-1 font-medium">Next: {visibleBlockHint}</div> : null}
                {cleanupReady ? (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button
                      size="small"
                      variant="outlined"
                      color="warning"
                      sx={blockedPanelWarningButtonSx}
                      onClick={onCleanupFiles}
                      disabled={busy}
                    >
                      {cleanupPurgeReady ? 'Purge' : 'Quarantine'} {cleanupFiles.length} rejected file{cleanupFiles.length === 1 ? '' : 's'}
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : null}

            <div className="grid grid-cols-2 gap-2">
              <Button size="small" variant="outlined" sx={blockedPanelActionButtonSx} onClick={onSuggest} disabled={busy}>
                AI Suggest
              </Button>
              <Button
                size="small"
                variant="contained"
                onClick={onApply}
                disabled={busy || Boolean(applyBlockedReason)}
              >
                {actionLabel(item, selectedMatch, targetPreviewState?.preview)}
              </Button>
            </div>

            <Button size="small" variant="text" color="inherit" onClick={onDismiss} disabled={busy}>
              {item.type === 'pending_ai' || item.type === 'skipped' ? 'Remove from Review' : 'Hide'}
            </Button>
            {canDeleteFolder(item) ? (
              <Button
                size="small"
                variant="outlined"
                color="error"
                onClick={onDeleteFolder}
                disabled={busy || Boolean(importJobActive)}
                title={importJobActive ? 'Import job is active for this folder — wait for it to finish before deleting' : wrongSourceNote || undefined}
              >
                {deleteFolderActionLabel(item)}
              </Button>
            ) : null}

            {isLibraryNoMb ? (
              <div className="mt-2 flex flex-col gap-2 border-t border-graphite-100 pt-3">
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => setMbsubmitOpen(true)}
                  disabled={busy}
                  title="Generate MusicBrainz submission text for this album"
                >
                  Prepare MB Submission
                </Button>
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => setAddMbidsOpen(true)}
                  disabled={busy}
                  title="Attach MusicBrainz IDs once you have them and move to stamped path"
                >
                  Add MBIDs
                </Button>
              </div>
            ) : null}
          </div>
        </div>
      </CardContent>

      {mbsubmitOpen && item.album_id ? (
        <MbsubmitDialog
          albumId={item.album_id}
          albumTitle={itemTitle(item)}
          onClose={() => setMbsubmitOpen(false)}
        />
      ) : null}
      {addMbidsOpen && item.album_id ? (
        <AddMbidsDialog
          albumId={item.album_id}
          albumTitle={itemTitle(item)}
          onClose={() => setAddMbidsOpen(false)}
          onSuccess={() => { setAddMbidsOpen(false); onDismiss(); }}
        />
      ) : null}
    </Card>
  );
}

function ConfirmDialog({
  intent,
  onClose,
  onConfirm,
}: {
  intent: ConfirmIntent | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const item = intent?.item;
  const deletingLibraryFolder = intent?.kind === 'delete_folder' && isMusicLibraryPath(item?.path);
  const wrongSourceNote = intent?.kind === 'delete_folder' ? wrongSourceEvidenceNote(item) : '';
  const title = intent?.kind === 'dismiss'
    ? 'Remove Review Item'
    : intent?.kind === 'delete_folder'
      ? deletingLibraryFolder ? 'Delete Library Folder' : 'Delete Source Folder'
      : 'Start Tagging Job';
  const detail = intent?.kind === 'dismiss'
    ? 'This removes the folder from Pending Review only. Audio files are not deleted.'
    : intent?.kind === 'delete_folder'
      ? deletingLibraryFolder
        ? 'This permanently deletes this folder from the music library, removes matching Beets DB rows, and removes it from Pending Review. Use this only when the tracks are confirmed wrong.'
        : 'This permanently deletes the source folder from disk and removes it from Pending Review. Use this only when the tracks are confirmed wrong.'
      : 'This starts a backend Beets job that can write tags and move files according to the configured path template.';

  return (
    <Dialog open={Boolean(intent)} onClose={onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/50" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-lg rounded bg-white p-5 shadow-xl">
          <DialogTitle className="text-lg font-semibold text-zinc-950">{title}</DialogTitle>
          <p className="mt-2 text-sm leading-6 text-zinc-600">{detail}</p>
          {item ? (
            <div className="mt-4 rounded border border-graphite-200 bg-graphite-50 p-3">
              <div className="truncate text-sm font-semibold text-zinc-900">{itemTitle(item)}</div>
              {intent?.kind === 'apply' ? (
                <div className="mt-1 truncate font-mono text-xs text-zinc-600">{intent.mbid}</div>
              ) : null}
              {intent?.kind === 'delete_folder' && item.path ? (
                <div className="mt-2 space-y-2">
                  <div className="truncate font-mono text-xs text-zinc-600" title={item.path}>{item.path}</div>
                  {wrongSourceNote ? (
                    <div className="rounded border border-rose-200 bg-rose-50 px-2 py-1 text-xs text-rose-800">
                      <div className="font-semibold">Wrong audio evidence</div>
                      <div>{wrongSourceNote}</div>
                    </div>
                  ) : null}
                  {intent.folderStats?.exists ? (
                    <div className="flex flex-wrap gap-3 text-xs text-zinc-500">
                      <span>{intent.folderStats.audio_count} audio file{intent.folderStats.audio_count !== 1 ? 's' : ''}</span>
                      {intent.folderStats.art_count > 0 ? (
                        <span>{intent.folderStats.art_count} artwork file{intent.folderStats.art_count !== 1 ? 's' : ''}</span>
                      ) : null}
                      {intent.folderStats.other_count > 0 ? (
                        <span>{intent.folderStats.other_count} other file{intent.folderStats.other_count !== 1 ? 's' : ''}</span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="text" onClick={onClose}>Cancel</Button>
            <Button color={intent?.kind === 'delete_folder' ? 'error' : 'primary'} variant="contained" onClick={onConfirm}>
              {intent?.kind === 'delete_folder' ? deleteFolderActionLabel(item) : 'Confirm'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function BackgroundJobsStrip({
  jobs,
  onClearQueue,
  onReconcileAll,
  onDismissJob,
  onRetryJob,
}: {
  jobs: Record<string, BgJob>;
  onClearQueue: () => void;
  onReconcileAll: () => void;
  onDismissJob: (itemId: string) => void;
  onRetryJob: (itemId: string) => void;
}) {
  const list = Object.values(jobs).filter((job) => job.status !== 'dismissed');
  const running = list.filter(isActiveBgJob).length;
  const statusUnknown = list.filter((j) => j.status === 'status_unknown' || j.status === 'still_running').length;
  const missing = list.filter(isMissingBgJob).length;
  const failed = list.filter((j) => j.status === 'failed').length;
  const done = list.filter((j) => j.status === 'success').length;
  const clearable = list.filter((j) => !isActiveBgJob(j)).length;
  const reconcilable = list.filter((j) => j.status === 'status_unknown' || j.status === 'still_running').length;

  const statusTone = (job: BgJob) => {
    if (isActiveBgJob(job)) return 'text-amber-400';
    if (job.status === 'failed') return 'text-rose-400';
    if (isMissingBgJob(job) || job.status === 'status_unknown' || job.status === 'still_running') return 'text-sky-400';
    if (job.status === 'cancelled') return 'text-zinc-500';
    return 'text-emerald-400';
  };
  const statusIcon = (job: BgJob) => {
    if (isActiveBgJob(job)) return '⟳';
    if (job.status === 'failed') return '✗';
    if (isMissingBgJob(job) || job.status === 'status_unknown' || job.status === 'still_running') return '•';
    if (job.status === 'cancelled') return '×';
    return '✓';
  };

  return (
    <div className="rounded border border-graphite-700 bg-graphite-800/40 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-3 text-xs font-medium">
          {running > 0 ? (
            <span className="flex items-center gap-1.5 text-amber-400">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
              {running} importing…
            </span>
          ) : null}
          {statusUnknown > 0 ? <span className="text-sky-400">{statusUnknown} status unknown</span> : null}
          {missing > 0 ? <span className="text-sky-400">{missing} returned to review</span> : null}
          {failed > 0 ? <span className="text-rose-400">{failed} failed</span> : null}
          {done > 0 ? <span className="text-emerald-400">{done} complete</span> : null}
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="small"
            variant="text"
            color="inherit"
            onClick={onReconcileAll}
            disabled={reconcilable === 0}
            title="Run one backend status reconciliation for unknown rows"
          >
            Reconcile all
          </Button>
          <Button
            size="small"
            variant="text"
            color="inherit"
            onClick={onClearQueue}
            disabled={clearable === 0}
            title="Clear completed and inactive queue entries"
          >
            Clear queue
          </Button>
        </div>
      </div>
      <div className="mt-2 space-y-1.5">
        {list.map((job) => {
          const canRetryStatus = job.status === 'status_unknown' || job.status === 'still_running';
          const canReturnToReview = job.status === 'failed' || isMissingBgJob(job) || job.status === 'cancelled';
          return (
            <div key={job.item.id} className="flex items-start gap-2 text-xs">
              <span className={`mt-px shrink-0 ${statusTone(job)}`}>
                {statusIcon(job)}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium text-zinc-300">{itemTitle(job.item)}</div>
                <div className="mt-0.5 truncate text-zinc-500">{job.message}</div>
              </div>
              {!isActiveBgJob(job) ? (
                <div className="flex shrink-0 items-center gap-2">
                  {canRetryStatus || canReturnToReview ? (
                    <button
                      className="text-sky-500 hover:text-sky-300 text-xs"
                      title={canRetryStatus ? 'Run one backend status reconciliation' : 'Return this item to the review queue'}
                      onClick={() => onRetryJob(job.item.id)}
                    >
                      {canRetryStatus ? 'Retry status' : 'Return to review'}
                    </button>
                  ) : null}
                  <button
                    className="text-zinc-600 hover:text-zinc-400"
                    title="Dismiss"
                    onClick={() => onDismissJob(job.item.id)}
                  >
                    ×
                  </button>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function ImportReviewPage({
  initialFilter = 'all',
  onFilterChange,
  active = true,
}: {
  initialFilter?: QueueFilter;
  onFilterChange?: (filter: QueueFilter) => void;
  active?: boolean;
}) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [counts, setCounts] = useState<ReviewCounts>({});
  const [originCounts, setOriginCounts] = useState<ReviewOriginCounts>({});
  const [filter, setFilter] = useState<QueueFilter>(initialFilter);
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>('all');
  const [query, setQuery] = useState('');
  const [evidenceOnly, setEvidenceOnly] = useState(false);
  const [mbids, setMbids] = useState<Record<string, string>>({});
  const [suggestions, setSuggestions] = useState<Record<string, AiSuggestResponse>>({});
  const [actions, setActions] = useState<Record<string, ActionState>>({});
  const [selectedMatches, setSelectedMatches] = useState<Record<string, SelectedMatch>>({});
  const [targetPreviews, setTargetPreviews] = useState<Record<string, TargetPreviewState>>({});
  const targetPreviewKeysRef = useRef<Record<string, string>>({});
  const autoEnqueueKeysRef = useRef<Set<string>>(new Set());
  const autoCleanupKeysRef = useRef<Set<string>>(new Set());
  const reviewItemStateKeysRef = useRef<Record<string, string>>({});
  const [confirmIntent, setConfirmIntent] = useState<ConfirmIntent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [cleanupMsg, setCleanupMsg] = useState('');
  const [revalidateBusy, setRevalidateBusy] = useState(false);
  const [revalidateMsg, setRevalidateMsg] = useState('');
  const [cursor, setCursor] = useState(0);
  // Jobs that were 'running' at last page close; populated by the lazy initializer below
  // and drained by the reconnect effect once pollJobBackground is available.
  const reconnectJobsRef = useRef<BgJob[]>([]);
  const [backgroundJobs, setBackgroundJobs] = useState<Record<string, BgJob>>(() => {
    try {
      const saved = localStorage.getItem('importReview_backgroundJobs');
      if (!saved) return {};
      const parsed = JSON.parse(saved) as Record<string, BgJob>;
      const result: Record<string, BgJob> = {};
      for (const [id, job] of Object.entries(parsed)) {
        if (job.status === 'running' && job.jobId) {
          reconnectJobsRef.current.push(job);
          result[id] = { ...job, message: 'Reconnecting…' };
        } else if (job.status === 'running' || job.status === 'still_running') {
          result[id] = {
            ...job,
            status: 'status_unknown',
            retryCount: job.retryCount ?? 3,
            message: job.message || 'Status unknown — reconciliation needed.',
          };
        } else {
          result[id] = job;
        }
      }
      return result;
    } catch {
      return {};
    }
  });
  const submittedItemIdsRef = useRef<Set<string>>(
    new Set(JSON.parse(localStorage.getItem('importReview_submittedIds') ?? '[]') as string[])
  );
  const runningJobItemIdsRef = useRef<Set<string>>(new Set());
  const backgroundJobsRef = useRef<Record<string, BgJob>>(backgroundJobs);
  const [activeJobHiddenCount, setActiveJobHiddenCount] = useState(0);

  const setAction = useCallback((itemId: string, state: ActionState) => {
    setActions((current) => ({ ...current, [itemId]: state }));
  }, []);

  const removeLocalItem = useCallback((item: ReviewItem) => {
    submittedItemIdsRef.current.add(item.id);
    persistSubmittedItemIds(submittedItemIdsRef.current);
    setItems((current) => current.filter((row) => row.id !== item.id));
    setCounts((current) => ({
      ...current,
      all: Math.max(0, (current.all ?? 0) - 1),
      [item.type]: Math.max(0, (current[item.type] ?? 0) - 1),
    }));
    setOriginCounts((current) => {
      const origin = itemOriginType(item);
      return {
        ...current,
        all: Math.max(0, (current.all ?? 0) - 1),
        [origin]: Math.max(0, (current[origin] ?? 0) - 1),
      };
    });
  }, []);

  const loadQueue = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError('');
    try {
      const response = await getReviewQueue({ limit: REVIEW_QUEUE_LIMIT, origin_type: sourceFilter });
      const allItems = response.items ?? [];
      let hiddenByActiveJob = 0;
      let restoredStaleSubmittedIds = false;
      const nextItems = allItems.filter((it) => {
        if (!submittedItemIdsRef.current.has(it.id)) return true;
        const backgroundJob = backgroundJobsRef.current[it.id];
        if (isActiveBgJob(backgroundJob)) {
          hiddenByActiveJob += 1;
          return false;
        }
        // If the backend still returns the review item and no live job owns it,
        // the local submitted marker is stale. Show it again.
        submittedItemIdsRef.current.delete(it.id);
        restoredStaleSubmittedIds = true;
        return true;
      });
      if (restoredStaleSubmittedIds) persistSubmittedItemIds(submittedItemIdsRef.current);
      setActiveJobHiddenCount(hiddenByActiveJob);
      const liveIds = new Set(nextItems.map((item) => item.id));
      setBackgroundJobs((current) => {
        let changed = false;
        const next = { ...current };
        for (const [id, job] of Object.entries(current)) {
          if (job.status === 'returned_to_review' && liveIds.has(id)) {
            delete next[id];
            changed = true;
          }
        }
        return changed ? next : current;
      });
      const changedIds = new Set<string>();
      const nextStateKeys: Record<string, string> = {};
      for (const item of nextItems) {
        const key = reviewItemStateKey(item);
        nextStateKeys[item.id] = key;
        if (reviewItemStateKeysRef.current[item.id] && reviewItemStateKeysRef.current[item.id] !== key) {
          changedIds.add(item.id);
        }
      }
      for (const id of Object.keys(reviewItemStateKeysRef.current)) {
        if (!liveIds.has(id)) changedIds.add(id);
      }
      reviewItemStateKeysRef.current = nextStateKeys;
      for (const id of changedIds) delete targetPreviewKeysRef.current[id];
      setSelectedMatches((current) => {
        const next = { ...current };
        for (const id of changedIds) delete next[id];
        for (const id of Object.keys(next)) if (!liveIds.has(id)) delete next[id];
        for (const item of nextItems) {
          if (!next[item.id]) {
            const saved = savedSelectedMatch(item);
            if (saved) next[item.id] = saved;
          }
        }
        return next;
      });
      setTargetPreviews((current) => {
        const next = { ...current };
        for (const id of changedIds) delete next[id];
        for (const id of Object.keys(next)) if (!liveIds.has(id)) delete next[id];
        return next;
      });
      setActions((current) => {
        const next = { ...current };
        for (const id of changedIds) delete next[id];
        for (const id of Object.keys(next)) if (!liveIds.has(id)) delete next[id];
        return next;
      });
      setItems(nextItems);
      setCounts(response.counts ?? {});
      setOriginCounts(response.origin_counts ?? {});
      setMbids((current) => {
        const next = { ...current };
        for (const id of changedIds) delete next[id];
        for (const id of Object.keys(next)) if (!liveIds.has(id)) delete next[id];
        for (const item of nextItems) {
          if (!next[item.id]) next[item.id] = initialMbid(item);
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [sourceFilter]);

  useEffect(() => {
    if (active) void loadQueue();
  }, [active, loadQueue]);
  useEffect(() => { setFilter(initialFilter); }, [initialFilter]);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => {
      if (!document.hidden) void loadQueue(true);
    }, 20000);
    return () => window.clearInterval(id);
  }, [active, loadQueue]);

  const visibleItems = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((item) => {
      if (!itemMatchesSourceFilter(item, sourceFilter)) return false;
      if (filter !== 'all') {
        if (filter === 'pending_ai' || filter === 'skipped' || filter === 'library_no_mb') {
          if (item.type !== filter) return false;
        } else if (filter === 'ready') {
          const selectedMatch = selectedMatches[item.id];
          const previewState = currentTargetPreviewState(item, selectedMatch, targetPreviews);
          const candidateMbid = mbids[item.id] ?? initialMbid(item);
          if (!shouldShowReadyBucket(item, candidateMbid, selectedMatch, previewState)) return false;
        } else if (filter === 'blocked') {
          const selectedMatch = selectedMatches[item.id];
          const previewState = currentTargetPreviewState(item, selectedMatch, targetPreviews);
          const candidateMbid = mbids[item.id] ?? initialMbid(item);
          if (!shouldShowBlockedBucket(item, candidateMbid, selectedMatch, previewState)) return false;
        } else {
          // audio_mismatch, no_candidate, failed are evidence-derived; skipped items belong only in 'skipped'
          if (item.type === 'skipped') return false;
          if (itemMatchBucket(item) !== filter) return false;
        }
      }
      if (evidenceOnly && !item.evidence?.top_candidates?.length && !item.evidence?.preflight) return false;
      if (q && !searchText(item).includes(q)) return false;
      return true;
    });
  }, [evidenceOnly, filter, items, mbids, query, selectedMatches, sourceFilter, targetPreviews]);

  // Reset cursor when filters/search change; clamp when list shrinks
  useEffect(() => { setCursor(0); }, [filter, sourceFilter, query, evidenceOnly]);
  useEffect(() => {
    if (visibleItems.length > 0) {
      setCursor((c) => Math.min(c, visibleItems.length - 1));
    }
  }, [visibleItems.length]);

  const activeItem = visibleItems[cursor] ?? null;
  const revalidatePendingCount = visibleItems.filter((item) => item.type === 'pending_ai').length;

  const handleRevalidateQueue = useCallback(async () => {
    const ids = visibleItems.filter((item) => item.type === 'pending_ai').map((item) => item.id);
    if (ids.length < 1) {
      setRevalidateMsg('No pending review items are visible to revalidate.');
      return;
    }
    setRevalidateBusy(true);
    setRevalidateMsg(`Revalidating ${ids.length} review item${ids.length === 1 ? '' : 's'}...`);
    try {
      const result = await revalidateImportReview({ review_item_ids: ids, auto_enqueue: true, limit: ids.length });
      await loadQueue(true);
      const hydrated = (result.items ?? []).filter((row) => row.ok && !row.queued && row.selected_match);
      if (hydrated.length) {
        setSelectedMatches((current) => {
          const next = { ...current };
          for (const row of hydrated) {
            if (row.review_item_id && row.selected_match) next[row.review_item_id] = row.selected_match as unknown as SelectedMatch;
          }
          return next;
        });
        setMbids((current) => {
          const next = { ...current };
          for (const row of hydrated) {
            if (row.review_item_id && row.selected_match?.release_group_id) {
              next[row.review_item_id] = row.selected_match.release_group_id;
            }
          }
          return next;
        });
        setTargetPreviews((current) => {
          const next = { ...current };
          for (const row of hydrated) {
            if (row.review_item_id) {
              delete next[row.review_item_id];
              delete targetPreviewKeysRef.current[row.review_item_id];
            }
          }
          return next;
        });
      }
      setRevalidateMsg(
        `Revalidated ${result.reviewed_count ?? 0}; queued ${result.queued_count ?? 0}; updated ${result.updated_count ?? 0}; failed ${result.failed_count ?? 0}.`,
      );
    } catch (err) {
      setRevalidateMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setRevalidateBusy(false);
    }
  }, [loadQueue, visibleItems]);

  // Target path preview — only for the active item
  useEffect(() => {
    if (!active) return;
    if (!activeItem) return;
    const item = activeItem;
    const selectedMatch = selectedMatches[item.id];
    if (!selectedMatch || selectedMatch.source === 'manual') return;
    const key = targetPreviewKey(item, selectedMatch);
    if (!key || targetPreviewKeysRef.current[item.id] === key) return;
    targetPreviewKeysRef.current[item.id] = key;
    setTargetPreviews((prev) => ({ ...prev, [item.id]: { status: 'loading', key } }));
    void previewImportTarget({
      path: item.path || '',
      release_group_id: selectedMatch.release_group_id,
      representative_release_id: selectedMatch.representative_release_id,
      artist: selectedMatch.artist || item.artist || '',
      album: selectedMatch.album || item.album || itemTitle(item),
      year: selectedMatch.year || item.year || '',
      existing_album_id: existingAlbumId(item) || undefined,
      track_mapping: selectedMatch.track_mapping,
      selected_source_files: selectedImportSourceFiles(selectedMatch),
      identity_validated: selectedMatch.identity_validated !== false,
      candidate_identity_error: selectedMatch.candidate_identity_error || '',
    })
      .then((preview) => {
        if (targetPreviewKeysRef.current[item.id] !== key) return;
        setTargetPreviews((prev) => ({ ...prev, [item.id]: { status: 'ready', key, preview } }));
      })
      .catch((err) => {
        if (targetPreviewKeysRef.current[item.id] !== key) return;
        setTargetPreviews((prev) => ({
          ...prev,
          [item.id]: { status: 'error', key, error: err instanceof Error ? err.message : String(err) },
        }));
      });
  }, [activeItem, selectedMatches]);

  // Auto-start AI Suggest only while the Review tab is active and the item needs identification.
  // Fires only when the cursor moves to a new item; checks current state synchronously
  // so it won't re-fire after the suggestion arrives.
  useEffect(() => {
    if (!active) return;
    if (!activeItem) return;
    const item = activeItem;
    if (item.type !== 'pending_ai' && itemMatchBucket(item) !== 'no_candidate') return;
    if (suggestions[item.id]) return;
    if (mbids[item.id]) return;
    if (actions[item.id]?.status === 'running') return;
    if (submittedItemIdsRef.current.has(item.id)) return;
    void handleSuggest(item);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, activeItem?.id]);

  const handleCleanupStale = useCallback(async () => {
    setCleanupBusy(true);
    setCleanupMsg('');
    try {
      const res = await cleanupStaleReview();
      if (res.removed_total > 0) {
        setCleanupMsg(
          `Removed ${res.removed_total} stale entr${res.removed_total === 1 ? 'y' : 'ies'} ` +
          `(${res.removed_folder_gone} folder gone, ${res.removed_mb_in_library} already in library). ` +
          `${res.remaining} remaining.`
        );
        void loadQueue(true);
      } else {
        setCleanupMsg('No stale entries found.');
      }
    } catch (err) {
      setCleanupMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setCleanupBusy(false);
    }
  }, [loadQueue]);

  // Background job polling: only real running jobs keep polling.
  // Missing jobs and unknown status resolve into terminal/recoverable rows.
  const pollJobBackground = useCallback(async (jobId: string, item: ReviewItem, label: string, keepReviewAfterSuccess = false) => {
    runningJobItemIdsRef.current.add(item.id);
    const updateBg = (patch: Partial<BgJob>) =>
      setBackgroundJobs((prev) => ({ ...prev, [item.id]: { ...prev[item.id], ...patch } as BgJob }));

    const handleSuccess = async () => {
      if (!keepReviewAfterSuccess && item.type === 'pending_ai' && item.path) {
        await deletePendingReview(item.path).catch(() => undefined);
      }
      submittedItemIdsRef.current.delete(item.id);
      persistSubmittedItemIds(submittedItemIdsRef.current);
      updateBg({ status: 'success', message: `${label} complete.`, retryCount: 0 });
      void loadQueue(true);
    };

    const markMissing = (message: string, returnedToReview = false) => {
      submittedItemIdsRef.current.delete(item.id);
      persistSubmittedItemIds(submittedItemIdsRef.current);
      updateBg({
        status: returnedToReview ? 'returned_to_review' : 'import_job_missing',
        message,
        retryCount: 0,
      });
      void loadQueue(true);
    };

    const markPolicyHandled = (message: string, pendingReviewExists?: boolean) => {
      submittedItemIdsRef.current.delete(item.id);
      persistSubmittedItemIds(submittedItemIdsRef.current);
      updateBg({
        status: pendingReviewExists ? 'returned_to_review' : 'success',
        message: message || 'Audio policy handled.',
        retryCount: 0,
      });
      void loadQueue(true);
    };

    try {
      if (!jobId) {
        markMissing('Job missing — returned to review.', true);
        return;
      }
      let currentJobId = jobId;
      let consecutiveErrors = 0;
      for (;;) {
        await wait(2500);
        let job: Awaited<ReturnType<typeof getJob>>;
        try {
          job = await getJob(currentJobId);
          consecutiveErrors = 0;
        } catch {
          consecutiveErrors += 1;
          if (consecutiveErrors < 3) {
            updateBg({
              status: 'running',
              retryCount: consecutiveErrors,
              message: `Connection lost — retrying ${consecutiveErrors}/3…`,
            });
            continue;
          }

          updateBg({ status: 'status_unknown', retryCount: consecutiveErrors, message: 'Status unknown — reconciliation needed.' });
          try {
            const rec = await reconcileImportJob(currentJobId, item.path || '', item.id);
            if (rec.handled) {
              markPolicyHandled(rec.note || 'Audio policy handled.', rec.pending_review_exists);
              return;
            }
            if (rec.status === 'success' || rec.status === 'likely_completed') {
              await handleSuccess();
              return;
            }
            if (rec.status === 'running' && rec.job_id) {
              currentJobId = rec.job_id;
              updateBg({ jobId: currentJobId, status: 'running', retryCount: 0, message: rec.note || 'Import job is running.' });
              consecutiveErrors = 0;
              continue;
            }
            if (rec.status === 'failed') {
              throw new Error(rec.note || 'Import job failed.');
            }
            if (rec.status === 'returned_to_review' || rec.status === 'not_found' || rec.status === 'import_job_missing' || rec.retryable) {
              markMissing(rec.note || 'No active import job was found; returned to review.', rec.status === 'returned_to_review' || rec.pending_review_exists === true);
              return;
            }
          } catch (recErr) {
            updateBg({
              status: 'status_unknown',
              retryCount: consecutiveErrors,
              message: recErr instanceof Error
                ? `Status unknown — ${recErr.message}`
                : 'Status unknown — reconciliation failed. Retry status or clear queue.',
            });
            return;
          }
          updateBg({ status: 'status_unknown', retryCount: consecutiveErrors, message: 'Status unknown — reconciliation needed.' });
          return;
        }
        const lastLine = (job.log ?? []).filter(Boolean).slice(-1)[0] || 'Running…';
        if (job.status === 'running') {
          updateBg({ status: 'running', retryCount: 0, message: lastLine.slice(0, 160) });
          continue;
        }
        if (job.status === 'success') {
          await handleSuccess();
          return;
        }
        if (job.status === 'cancelled') {
          updateBg({ status: 'cancelled', message: 'Import cancelled.', retryCount: 0 });
          return;
        }
        if (job.status === 'failed') {
          const rec = await reconcileImportJob(currentJobId, item.path || '', item.id).catch(() => undefined);
          if (rec?.handled) {
            markPolicyHandled(rec.note || lastLine || 'Audio policy handled.', rec.pending_review_exists);
            return;
          }
        }
        throw new Error(lastLine || `Job ${job.status}`);
      }
    } catch (err) {
      updateBg({ status: 'failed', message: err instanceof Error ? err.message : String(err), retryCount: 0 });
    } finally {
      runningJobItemIdsRef.current.delete(item.id);
    }
  }, [loadQueue]);

  // Backend-owned auto-enqueue: the browser only submits the current verified selection.
  useEffect(() => {
    if (!active) return;
    if (!activeItem) return;
    const item = activeItem;
    const itemStateKey = reviewItemStateKey(item);
    const statusKey = (item.status_key || item.status || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
    if (['auto_enqueue_failed', 'format_policy_rejected', 'import_failed', 'failed'].includes(statusKey)) return;
    if (item.type !== 'pending_ai' || existingAlbumId(item)) return;
    if (submittedItemIdsRef.current.has(item.id)) return;
    if (runningJobItemIdsRef.current.has(item.id)) return;
    const sm = selectedMatches[item.id];
    if (!sm || sm.source === 'manual' || !sm.auto_fix_eligible || !sm.is_importable) return;
    const previewState = currentTargetPreviewState(item, sm, targetPreviews);
    if (!previewState || previewState.status !== 'ready' || !previewState.preview) return;
    const preview = previewState.preview;
    if (!preview.safe || (preview.real_conflict_count ?? 0) > 0) return;
    const blockReason = applyBlockReason(item, mbids[item.id] || sm.release_group_id, sm, previewState);
    if (blockReason) return;
    const selectedSourceFiles = selectedImportSourceFiles(sm, preview);
    const previewCount = preview.tracks_to_import_count ?? selectedSourceFiles.length;
    if (selectedSourceFiles.length < 1 || previewCount < 1) return;
    if (selectedSourceFiles.length !== previewCount) {
      setAction(item.id, {
        status: 'error',
        message: `Import selection changed for ${itemTitle(item)}. Refresh target preview before queueing.`,
      });
      return;
    }
    const representativeId = sm.representative_release_id && sm.representative_release_id !== sm.release_group_id
      ? sm.representative_release_id
      : (mbids[item.id] || sm.representative_release_id);
    const enqueueKey = [
      item.id,
      itemStateKey,
      previewState.key,
      sm.release_group_id,
      representativeId,
      ...selectedSourceFiles.slice().sort(),
    ].join('::');
    if (autoEnqueueKeysRef.current.has(enqueueKey)) return;
    autoEnqueueKeysRef.current.add(enqueueKey);

    const unfilteredSelectedFiles = selectedImportSourceFiles(sm);
    const keepReviewAfterSuccess = Boolean(
      (sm.is_partial_import && unmatchedExtraTrackCount(sm) > 0)
      || selectedSourceFiles.length < unfilteredSelectedFiles.length,
    );
    const albumLabel = itemTitle(item);
    const label = `Import queued: ${albumLabel} - ${selectedSourceFiles.length} track${selectedSourceFiles.length === 1 ? '' : 's'}`;
    const payload = {
      path: item.path || '',
      review_item_id: item.id,
      mb_albumid: representativeId,
      mb_releasegroupid: sm.release_group_id,
      confidence_score: sm.confidence_score,
      selected_source_files: selectedSourceFiles,
      track_mapping: sm.track_mapping,
      ai_suggestion: suggestionWithOrigin(item, suggestions[item.id]?.suggestion as AiSuggestion | undefined),
      selected_match: {
        release_group_id: sm.release_group_id,
        representative_release_id: representativeId,
        artist: sm.artist || item.artist || '',
        album: sm.album || item.album || albumLabel,
        year: sm.year || item.year || '',
        confidence_score: sm.confidence_score,
        preflight_status: sm.preflight_status,
        source: sm.source,
      },
      trigger_plex: false,
    };
    const setSameItemAction = (state: ActionState) => {
      if (reviewItemStateKeysRef.current[item.id] === itemStateKey) setAction(item.id, state);
    };
    const attachQueuedJob = (jobId: string, message = `Job ${jobId} queued.`) => {
      setBackgroundJobs((prev) => ({
        ...prev,
        [item.id]: { jobId, status: 'running', label, message, item },
      }));
      removeLocalItem(item);
      void pollJobBackground(jobId, item, label, keepReviewAfterSuccess);
    };
    setSameItemAction({
      status: 'running',
      message: `Submitting import for ${albumLabel}: ${selectedSourceFiles.length} verified track${selectedSourceFiles.length === 1 ? '' : 's'}...`,
    });

    void withTimeout(autoEnqueueImport(payload), 15000, 'enqueue acknowledgement timed out')
      .then((started) => {
        if (started.handled) {
          autoEnqueueKeysRef.current.delete(enqueueKey);
          setSameItemAction({
            status: 'warning',
            message: started.note || started.eligibility?.blocking_reasons?.join('; ') || 'Audio was rejected by Music Format Preferences.',
          });
          if (started.pending_review_exists === false) removeLocalItem(item);
          void loadQueue();
          return;
        }
        if (!started.queued || !started.job_id) {
          const reasons = started.eligibility?.blocking_reasons?.join('; ') || 'Backend eligibility rejected auto-import.';
          autoEnqueueKeysRef.current.delete(enqueueKey);
          setSameItemAction({ status: 'error', message: reasons });
          return;
        }
        attachQueuedJob(started.job_id, `Queued ${selectedSourceFiles.length} track${selectedSourceFiles.length === 1 ? '' : 's'} for ${albumLabel}.`);
      })
      .catch((err) => {
        setSameItemAction({ status: 'running', message: `Checking queued import for ${albumLabel}...` });
        void reconcileAutoEnqueueImport(payload)
          .then((result) => {
            if (result.queued && result.job_id) {
              attachQueuedJob(result.job_id, `Reconnected to queued import for ${albumLabel}.`);
              return;
            }
            if (result.handled) {
              autoEnqueueKeysRef.current.delete(enqueueKey);
              setSameItemAction({
                status: 'warning',
                message: result.note || result.eligibility?.blocking_reasons?.join('; ') || 'Audio was rejected by Music Format Preferences.',
              });
              if (result.pending_review_exists === false) removeLocalItem(item);
              void loadQueue();
              return;
            }
            autoEnqueueKeysRef.current.delete(enqueueKey);
            setSameItemAction({
              status: 'error',
              message: result.retryable
                ? `Import was not queued for ${albumLabel}. Retry enqueue.`
                : (result.eligibility?.blocking_reasons?.join('; ') || (err instanceof Error ? err.message : String(err))),
            });
          })
          .catch((reconcileErr) => {
            autoEnqueueKeysRef.current.delete(enqueueKey);
            setSameItemAction({ status: 'error', message: reconcileErr instanceof Error ? reconcileErr.message : String(reconcileErr) });
          });
      });
  }, [active, activeItem, mbids, pollJobBackground, removeLocalItem, selectedMatches, setAction, suggestions, targetPreviews]);
  // Keep ref in sync so callbacks can read current backgroundJobs without stale closures.
  useEffect(() => { backgroundJobsRef.current = backgroundJobs; }, [backgroundJobs]);

  // Persist backgroundJobs to localStorage so in-flight jobs survive a page refresh.
  useEffect(() => {
    try {
      if (Object.keys(backgroundJobs).length === 0) {
        localStorage.removeItem('importReview_backgroundJobs');
      } else {
        localStorage.setItem('importReview_backgroundJobs', JSON.stringify(backgroundJobs));
      }
    } catch {}
  }, [backgroundJobs]);

  // Resume polling for jobs that were in-flight when the page was last closed.
  // Fires once when pollJobBackground first becomes available (after loadQueue is stable).
  useEffect(() => {
    const toReconnect = reconnectJobsRef.current;
    if (toReconnect.length === 0) return;
    reconnectJobsRef.current = [];
    for (const job of toReconnect) {
      void pollJobBackground(job.jobId, job.item, job.label);
    }
  }, [pollJobBackground]);

  const handleRetryJob = useCallback((itemId: string) => {
    const existing = backgroundJobsRef.current[itemId];
    if (!existing) return;
    if (existing.status === 'status_unknown' || existing.status === 'still_running') {
      if (runningJobItemIdsRef.current.has(itemId)) return;
      runningJobItemIdsRef.current.add(itemId);
      setBackgroundJobs((prev) => ({
        ...prev,
        [itemId]: { ...prev[itemId], status: 'status_unknown' as const, message: 'Reconciling status…' },
      }));
      void reconcileImportJob(existing.jobId, existing.item.path || '', existing.item.id)
        .then((rec) => {
          if (rec.handled) {
            submittedItemIdsRef.current.delete(itemId);
            persistSubmittedItemIds(submittedItemIdsRef.current);
            setBackgroundJobs((prev) => ({
              ...prev,
              [itemId]: {
                ...prev[itemId],
                status: (rec.pending_review_exists ? 'returned_to_review' : 'success') as BgJobStatus,
                message: rec.note || 'Audio policy handled.',
              },
            }));
            void loadQueue(true);
            return;
          }
          if (rec.status === 'success' || rec.status === 'likely_completed') {
            submittedItemIdsRef.current.delete(itemId);
            persistSubmittedItemIds(submittedItemIdsRef.current);
            setBackgroundJobs((prev) => ({
              ...prev,
              [itemId]: { ...prev[itemId], status: 'success' as const, message: rec.note || 'Completed — target verified.' },
            }));
            void loadQueue(true);
            return;
          }
          if (rec.status === 'running' && rec.job_id) {
            setBackgroundJobs((prev) => ({
              ...prev,
              [itemId]: { ...prev[itemId], jobId: rec.job_id, status: 'running' as const, message: rec.note || 'Import job is running.' },
            }));
            runningJobItemIdsRef.current.delete(itemId);
            void pollJobBackground(rec.job_id, existing.item, existing.label);
            return;
          }
          if (rec.status === 'failed') {
            setBackgroundJobs((prev) => ({
              ...prev,
              [itemId]: { ...prev[itemId], status: 'failed' as const, message: rec.note || 'Import job failed.' },
            }));
            return;
          }
          submittedItemIdsRef.current.delete(itemId);
          persistSubmittedItemIds(submittedItemIdsRef.current);
          setBackgroundJobs((prev) => ({
            ...prev,
            [itemId]: {
              ...prev[itemId],
              status: (rec.status === 'returned_to_review' || rec.pending_review_exists ? 'returned_to_review' : 'import_job_missing') as BgJobStatus,
              message: rec.note || 'Job missing — returned to review.',
            },
          }));
          void loadQueue(true);
        })
        .catch((err) => {
          setBackgroundJobs((prev) => ({
            ...prev,
            [itemId]: {
              ...prev[itemId],
              status: 'status_unknown' as const,
              message: err instanceof Error ? `Status unknown — ${err.message}` : 'Status unknown — reconciliation failed.',
            },
          }));
        })
        .finally(() => {
          runningJobItemIdsRef.current.delete(itemId);
        });
      return;
    }

    // Missing/failed/cancelled job: restore item to review queue for explicit re-submission.
    submittedItemIdsRef.current.delete(itemId);
    persistSubmittedItemIds(submittedItemIdsRef.current);
    setBackgroundJobs((prev) => { const next = { ...prev }; delete next[itemId]; return next; });
    void loadQueue(true);
  }, [loadQueue, pollJobBackground]);

  const handleSuggest = useCallback(async (item: ReviewItem) => {
    setAction(item.id, { status: 'running', message: 'Asking AI...' });
    try {
      const response = item.album_id
        ? await suggestAlbum(item.album_id)
        : await suggestFolder(item.path || '');
      const suggestion = response.suggestion;
      setSuggestions((current) => ({ ...current, [item.id]: response }));
      if (suggestion?.mb_valid && suggestion.mb_albumid) {
        const rgid = suggestion.mb_releasegroupid || suggestion.mb_albumid || '';
        setMbids((current) => ({ ...current, [item.id]: rgid }));
        setSelectedMatches((current) => ({ ...current, [item.id]: buildAiSelectedMatch(suggestion) }));
        delete targetPreviewKeysRef.current[item.id];
        setTargetPreviews((current) => { const next = { ...current }; delete next[item.id]; return next; });
      }
      setAction(item.id, {
        status: 'success',
        message: suggestion?.reason || 'AI suggestion ready. Verify the release-group ID before applying.',
      });
    } catch (err) {
      setAction(item.id, { status: 'error', message: err instanceof Error ? err.message : String(err) });
    }
  }, [setAction]);

  const startApply = useCallback((item: ReviewItem) => {
    const mbid = (mbids[item.id] || '').trim();
    const sm = selectedMatches[item.id];
    const previewState = currentTargetPreviewState(item, sm, targetPreviews);
    const blockReason = applyBlockReason(item, mbid, sm, previewState);
    if (blockReason) { setAction(item.id, { status: 'error', message: blockReason }); return; }
    setConfirmIntent({ kind: 'apply', item, mbid });
  }, [mbids, selectedMatches, setAction, targetPreviews]);

  // Non-blocking: submit job, remove from queue immediately, poll in background
  const runApply = useCallback(async (item: ReviewItem, mbid: string) => {
    if (runningJobItemIdsRef.current.has(item.id)) return;
    setAction(item.id, { status: 'running', message: 'Starting backend job...' });
    try {
      let started: JobStartResponse;
      const existingId = existingAlbumId(item);
      const sm = selectedMatches[item.id];
      const releaseGroupId = sm?.release_group_id || selectedReleaseGroupId(item, mbid, suggestions[item.id]);
      const representativeId = sm?.representative_release_id && sm.representative_release_id !== sm.release_group_id
        ? sm.representative_release_id
        : mbid;
      const preview = currentTargetPreviewState(item, sm, targetPreviews)?.preview;
      const unfilteredSelectedFiles = selectedImportSourceFiles(sm);
      const selectedSourceFiles = selectedImportSourceFiles(sm, preview);
      const keepReviewAfterSuccess = Boolean(
        (sm?.is_partial_import && unmatchedExtraTrackCount(sm) > 0)
        || selectedSourceFiles.length < unfilteredSelectedFiles.length,
      );

      if (item.type === 'library_no_mb' && item.album_id) {
        started = await matchAlbum(item.album_id, representativeId);
      } else if (existingId) {
        started = await reimportDisk({
          aldir: item.path || '',
          mb_albumid: representativeId,
          existing_album_id: existingId,
          albumartist: item.artist || '',
        });
      } else {
        started = await importWithId({
          path: item.path || '',
          mb_albumid: representativeId,
          mb_releasegroupid: releaseGroupId || undefined,
          selected_source_files: selectedSourceFiles,
          track_mapping: sm?.track_mapping,
          ai_suggestion: suggestionWithOrigin(item, suggestions[item.id]?.suggestion as AiSuggestion | undefined),
        });
      }

      if (!started.job_id) throw new Error('Backend did not return a job id');

      const label = actionLabel(item, sm, preview);
      setBackgroundJobs((prev) => ({
        ...prev,
        [item.id]: { jobId: started.job_id!, status: 'running', label, message: `Job ${started.job_id} queued.`, item },
      }));
      removeLocalItem(item);                          // advance cursor immediately
      void pollJobBackground(started.job_id, item, label, keepReviewAfterSuccess);
    } catch (err) {
      setAction(item.id, { status: 'error', message: err instanceof Error ? err.message : String(err) });
    }
  }, [pollJobBackground, removeLocalItem, selectedMatches, setAction, suggestions, targetPreviews]);

  const requestDismiss = useCallback((item: ReviewItem) => {
    if (item.type === 'pending_ai' || item.type === 'skipped') { setConfirmIntent({ kind: 'dismiss', item }); return; }
    removeLocalItem(item);
  }, [removeLocalItem]);

  const requestDeleteFolder = useCallback(async (item: ReviewItem) => {
    if (!item.path) {
      setAction(item.id, { status: 'error', message: 'No source folder path is available for this review item.' });
      return;
    }
    let folderStats: FolderStatsResponse | undefined;
    try {
      folderStats = await getFolderStats(item.path);
    } catch {
      // best-effort — show dialog without stats
    }
    setConfirmIntent({ kind: 'delete_folder', item, folderStats });
  }, [setAction]);

  const runDismiss = useCallback(async (item: ReviewItem) => {
    setAction(item.id, { status: 'running', message: 'Removing review item...' });
    try {
      if ((item.type === 'pending_ai' || item.type === 'skipped') && item.path) await deletePendingReview(item.path);
      removeLocalItem(item);
    } catch (err) {
      setAction(item.id, { status: 'error', message: err instanceof Error ? err.message : String(err) });
    }
  }, [removeLocalItem, setAction]);

  const runCleanupFiles = useCallback(async (item: ReviewItem) => {
    if (!item.path) {
      setAction(item.id, { status: 'error', message: 'No source folder path is available for this review item.' });
      return;
    }
    const sm = selectedMatches[item.id];
    const preview = currentTargetPreviewState(item, sm, targetPreviews)?.preview;
    const files = selectedCleanupSourceFiles(sm, preview);
    const destructive = hasDestructiveCleanupMismatch(sm, preview);
    const cleanupAction = destructive ? 'delete_rejected' : 'quarantine_rejected';
    if (files.length < 1) {
      setAction(item.id, { status: 'error', message: 'No rejected or extra source files are selected for cleanup.' });
      return;
    }
    setAction(item.id, { status: 'running', message: `${destructive ? 'Purging' : 'Quarantining'} ${files.length} rejected file${files.length === 1 ? '' : 's'}...` });
    try {
      const result = await cleanupReviewFiles({
        path: item.path,
        review_item_id: item.id,
        files,
        action: cleanupAction,
        allow_delete: destructive,
      });
      const handled = (result.quarantined_count ?? 0) + (result.deleted_count ?? 0);
      setAction(item.id, {
        status: 'success',
        message: destructive
          ? `Purged ${result.deleted_count ?? 0} file${result.deleted_count === 1 ? '' : 's'}; ${result.remaining_audio_count ?? 0} audio file${result.remaining_audio_count === 1 ? '' : 's'} remain.`
          : `Quarantined ${result.quarantined_count ?? 0} file${result.quarantined_count === 1 ? '' : 's'}; ${result.remaining_audio_count ?? 0} audio file${result.remaining_audio_count === 1 ? '' : 's'} remain.`,
      });
      if (result.pending_review_removed || result.remaining_audio_count === 0) {
        removeLocalItem(item);
      } else if (handled > 0) {
        void loadQueue(true);
      }
    } catch (err) {
      setAction(item.id, { status: 'error', message: err instanceof Error ? err.message : String(err) });
    }
  }, [loadQueue, removeLocalItem, selectedMatches, setAction, targetPreviews]);

  useEffect(() => {
    if (!active || !activeItem?.path) return;
    const item = activeItem;
    const itemPath = item.path || '';
    if (!itemPath) return;
    if (actions[item.id]?.status === 'running') return;
    if (backgroundJobs[item.id]?.status === 'running' || backgroundJobs[item.id]?.status === 'still_running') return;
    const selectedMatch = selectedMatches[item.id];
    if (!selectedMatch || selectedMatch.source === 'manual') return;
    const previewState = currentTargetPreviewState(item, selectedMatch, targetPreviews);
    const preview = previewState?.status === 'ready' ? previewState.preview : undefined;
    const files = selectedCleanupSourceFiles(selectedMatch, preview);
    const destructive = hasDestructiveCleanupMismatch(selectedMatch, preview);
    const cleanupAction = destructive ? 'delete_rejected' : 'quarantine_rejected';
    if (files.length < 1 || files.length >= AUTO_QUARANTINE_REJECTED_MAX_FILES) return;
    const key = [
      item.id,
      itemPath,
      targetPreviewKey(item, selectedMatch),
      cleanupAction,
      [...files].sort().join('|'),
    ].join('::');
    if (autoCleanupKeysRef.current.has(key)) return;
    autoCleanupKeysRef.current.add(key);
    setAction(item.id, {
      status: 'running',
      message: `${destructive ? 'Auto-purging' : 'Auto-quarantining'} ${files.length} rejected file${files.length === 1 ? '' : 's'}...`,
    });
    void cleanupReviewFiles({
      path: itemPath,
      review_item_id: item.id,
      files,
      action: cleanupAction,
      allow_delete: destructive,
    })
      .then((result) => {
        const handled = (result.quarantined_count ?? 0) + (result.deleted_count ?? 0);
        setAction(item.id, {
          status: 'success',
          message: destructive
            ? `Auto-purged ${result.deleted_count ?? 0} rejected file${result.deleted_count === 1 ? '' : 's'}; ${result.remaining_audio_count ?? 0} audio file${result.remaining_audio_count === 1 ? '' : 's'} remain.`
            : `Auto-quarantined ${result.quarantined_count ?? 0} rejected file${result.quarantined_count === 1 ? '' : 's'}; ${result.remaining_audio_count ?? 0} audio file${result.remaining_audio_count === 1 ? '' : 's'} remain.`,
        });
        if (result.pending_review_removed || result.remaining_audio_count === 0) {
          removeLocalItem(item);
        } else if (handled > 0) {
          void loadQueue(true);
        }
      })
      .catch((err) => {
        autoCleanupKeysRef.current.delete(key);
        setAction(item.id, { status: 'error', message: err instanceof Error ? err.message : String(err) });
      });
  }, [active, activeItem, actions, backgroundJobs, loadQueue, removeLocalItem, selectedMatches, setAction, targetPreviews]);

  const runDeleteFolder = useCallback(async (item: ReviewItem) => {
    if (!item.path) {
      setAction(item.id, { status: 'error', message: 'No source folder path is available for this review item.' });
      return;
    }
    setAction(item.id, { status: 'running', message: 'Deleting source folder from disk...' });
    try {
      const result = await deleteReviewFolder(item.path, isMusicLibraryPath(item.path), item.album_id);
      removeLocalItem(item);
      setAction(item.id, {
        status: 'success',
        message: `Deleted ${result.audio_files_removed} audio file(s) and ${result.files_removed} total file(s).`,
      });
      void loadQueue(true);
    } catch (err) {
      setAction(item.id, { status: 'error', message: err instanceof Error ? err.message : String(err) });
    }
  }, [loadQueue, removeLocalItem, setAction]);

  const confirm = useCallback(() => {
    const intent = confirmIntent;
    setConfirmIntent(null);
    if (!intent) return;
    if (intent.kind === 'apply') void runApply(intent.item, intent.mbid);
    else if (intent.kind === 'dismiss') void runDismiss(intent.item);
    else void runDeleteFolder(intent.item);
  }, [confirmIntent, runApply, runDeleteFolder, runDismiss]);

  const selectFilter = useCallback((nextFilter: QueueFilter) => {
    setFilter(nextFilter);
    onFilterChange?.(nextFilter);
  }, [onFilterChange]);

  const selectSourceFilter = useCallback((nextFilter: SourceFilter) => {
    setSourceFilter(nextFilter);
  }, []);

  const clearFilters = useCallback(() => {
    setFilter('all');
    setSourceFilter('all');
    setQuery('');
    setEvidenceOnly(false);
    onFilterChange?.('all');
  }, [onFilterChange]);

  const filterCounts = useMemo((): Record<QueueFilter, number> => {
    const nonSkipped = items.filter((i) => i.type !== 'skipped');
    const ready = nonSkipped.filter((i) => {
      const selectedMatch = selectedMatches[i.id];
      const previewState = currentTargetPreviewState(i, selectedMatch, targetPreviews);
      return shouldShowReadyBucket(i, mbids[i.id] ?? initialMbid(i), selectedMatch, previewState);
    }).length;
    const blocked = nonSkipped.filter((i) => {
      const selectedMatch = selectedMatches[i.id];
      const previewState = currentTargetPreviewState(i, selectedMatch, targetPreviews);
      return shouldShowBlockedBucket(i, mbids[i.id] ?? initialMbid(i), selectedMatch, previewState);
    }).length;
    const audioMismatch = nonSkipped.filter((i) => itemMatchBucket(i) === 'audio_mismatch').length;
    const failed = nonSkipped.filter((i) => itemMatchBucket(i) === 'failed').length;
    const noCand = nonSkipped.filter((i) => itemMatchBucket(i) === 'no_candidate').length;
    return {
      all: counts.all ?? items.length,
      pending_ai: counts.pending_ai ?? 0,
      skipped: counts.skipped ?? 0,
      library_no_mb: counts.library_no_mb ?? 0,
      ready,
      blocked,
      audio_mismatch: audioMismatch,
      no_candidate: noCand,
      failed,
    };
  }, [counts, items, mbids, selectedMatches, targetPreviews]);

  const sourceFilterCounts = useMemo((): Record<SourceFilter, number> => {
    const result = Object.fromEntries(originFilters.map((entry) => [entry.id, originCountFor(originCounts, entry.id)])) as Record<SourceFilter, number>;
    if (!result.all) result.all = originCounts.all ?? items.length;
    return result;
  }, [items.length, originCounts]);
  const unloadedReviewCount = Math.max(0, (counts.all ?? items.length) - items.length - activeJobHiddenCount);

  const statusFilterLabel = filters.find((entry) => entry.id === filter)?.label ?? 'All';
  const sourceFilterLabel = originLabel(sourceFilter);
  const activeFilterSummary = [sourceFilter !== 'all' ? sourceFilterLabel : '', filter !== 'all' ? statusFilterLabel : '']
    .filter(Boolean)
    .join(' + ');
  const hasExtraFilters = filter !== 'all' || sourceFilter !== 'all' || Boolean(query.trim()) || evidenceOnly;

  const handlePrev = useCallback(() => setCursor((c) => Math.max(0, c - 1)), []);
  const handleNext = useCallback(() => setCursor((c) => Math.min(visibleItems.length - 1, c + 1)), [visibleItems.length]);
  const handleSkip = useCallback(() => setCursor((c) => Math.min(visibleItems.length - 1, c + 1)), [visibleItems.length]);

  // Stable per-item callbacks (avoid inline closures in the card render)
  const handleMbidChange = useCallback((value: string) => {
    if (!activeItem) return;
    const item = activeItem;
    setMbids((c) => ({ ...c, [item.id]: value }));
    setSelectedMatches((current) => {
      const prev = current[item.id];
      return {
        ...current,
        [item.id]: {
          release_group_id: value.trim(),
          representative_release_id: '',
          artist: prev?.artist || item.artist || '',
          album: prev?.album || item.album || itemTitle(item),
          year: prev?.year || String(item.year || ''),
          track_match_count: null, total_tracks: null, local_track_count: null, track_mapping: [],
          preflight_status: 'stale',
          preflight_reason: 'Release Group ID was edited manually; select a visible match to refresh preflight before import.',
          is_release_group_usable: isMusicBrainzUuid(value),
          is_importable: false,
          is_partial_import: false,
          confidence_score: null,
          confidence_level: isMusicBrainzUuid(value) ? 'low' : 'blocked',
          auto_fix_eligible: false, auto_fix_requires_review: false,
          auto_fix_reason: isMusicBrainzUuid(value)
            ? 'Auto-fix blocked until a visible match refreshes track comparison and preflight.'
            : 'Auto-fix blocked because this value is not a valid MusicBrainz Release Group ID.',
          missing_track_count: 0, match_count: null, preflight_ok: null, source: 'manual',
        } as SelectedMatch,
      };
    });
    setTargetPreviews((c) => { const n = { ...c }; delete n[item.id]; return n; });
    delete targetPreviewKeysRef.current[item.id];
  }, [activeItem]);

  const handleUseCandidate = useCallback((value: string) => {
    if (activeItem) setMbids((c) => ({ ...c, [activeItem.id]: value }));
  }, [activeItem]);

  const handleSelectMatch = useCallback((match: SelectedMatch) => {
    if (!activeItem) return;
    setSelectedMatches((c) => ({ ...c, [activeItem.id]: match }));
    setMbids((c) => ({ ...c, [activeItem.id]: match.release_group_id }));
    setTargetPreviews((c) => { const n = { ...c }; delete n[activeItem.id]; return n; });
    delete targetPreviewKeysRef.current[activeItem.id];
  }, [activeItem]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase() ?? '';
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (e.key === '[') { e.preventDefault(); handlePrev(); }
      if (e.key === ']') { e.preventDefault(); handleNext(); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [handlePrev, handleNext]);

  return (
    <section className="flex flex-col gap-5">
      {/* Counts + action bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
          <span className="text-zinc-400">{countFor(counts, 'all')} total</span>
          {activeJobHiddenCount ? <span className="text-zinc-400">{activeJobHiddenCount} active job hidden</span> : null}
          {unloadedReviewCount ? <span className="text-zinc-400">{unloadedReviewCount} not loaded</span> : null}
          {(
            [
              { id: 'pending_ai', label: 'pending AI' },
              { id: 'skipped', label: 'skipped' },
              { id: 'library_no_mb', label: 'missing MB ID' },
            ] as Array<{ id: ReviewItemType; label: string }>
          ).map(({ id, label }) => {
            const n = countFor(counts, id);
            return (
              <span key={id} className="text-zinc-400">
                {n} {label}
              </span>
            );
          })}
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <div className="flex gap-2">
            <Button
              disabled={revalidateBusy || loading || revalidatePendingCount < 1}
              size="small"
              variant="outlined"
              onClick={() => void handleRevalidateQueue()}
            >
              {revalidateBusy ? 'Revalidating…' : 'Revalidate'}
            </Button>
            <Button disabled={cleanupBusy || loading} size="small" variant="outlined" onClick={() => void handleCleanupStale()}>
              {cleanupBusy ? 'Cleaning…' : 'Clean Stale'}
            </Button>
            <Button variant="contained" onClick={() => void loadQueue()} disabled={loading}>
              Refresh
            </Button>
          </div>
          {revalidateMsg ? <span className="text-xs text-zinc-400">{revalidateMsg}</span> : null}
          {cleanupMsg ? <span className="text-xs text-zinc-400">{cleanupMsg}</span> : null}
        </div>
      </div>

      {/* Filter / search card */}
      <Card variant="outlined" sx={{ borderRadius: 2, borderColor: 'rgba(148, 163, 184, 0.22)' }}>
        <CardContent>
          <div className="grid gap-4 lg:grid-cols-[1fr_auto] lg:items-center">
            <TextField
              label="Search review queue"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              size="small"
              fullWidth
            />
            <div className="flex items-center justify-between gap-3 rounded border border-graphite-200 bg-graphite-50 px-3 py-2">
              <span className="text-sm font-medium text-zinc-700">Evidence only</span>
              <Switch
                checked={evidenceOnly}
                onChange={setEvidenceOnly}
                className="group inline-flex h-7 w-12 items-center rounded-full bg-graphite-300 transition data-[checked]:bg-sky-600"
              >
                <span className="size-5 translate-x-1 rounded-full bg-white shadow transition group-data-[checked]:translate-x-6" />
              </Switch>
            </div>
          </div>
          {activeFilterSummary ? (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
              <span>Active: {activeFilterSummary}</span>
              {hasExtraFilters ? (
                <Button size="small" variant="text" onClick={clearFilters}>Clear filters</Button>
              ) : null}
            </div>
          ) : hasExtraFilters ? (
            <div className="mt-3 flex justify-end">
              <Button size="small" variant="text" onClick={clearFilters}>Clear filters</Button>
            </div>
          ) : null}
          <div className="mt-4 flex flex-wrap gap-2">
            {filters.map((entry) => {
              const count = filterCounts[entry.id];
              const active = filter === entry.id;
              if (count === 0 && !active) return null;
              return (
                <Button
                  key={entry.id}
                  size="small"
                  color={entry.id === 'failed' || entry.id === 'audio_mismatch' ? 'error' : entry.id === 'blocked' ? 'warning' : 'primary'}
                  variant={active ? 'contained' : 'outlined'}
                  onClick={() => selectFilter(entry.id)}
                >
                  {entry.label} ({count})
                </Button>
              );
            })}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Source</span>
            {originFilters.map((entry) => {
              const count = sourceFilterCounts[entry.id];
              const active = sourceFilter === entry.id;
              if (entry.id !== 'all' && count === 0 && !active) return null;
              return (
                <Button
                  key={entry.id}
                  size="small"
                  color={active ? 'primary' : 'inherit'}
                  variant={active ? 'contained' : 'outlined'}
                  onClick={() => selectSourceFilter(entry.id)}
                >
                  {entry.label} ({count})
                </Button>
              );
            })}
          </div>
        </CardContent>
        {loading ? <LinearProgress /> : null}
      </Card>

      {error ? <Alert severity="error">{error}</Alert> : null}

      {/* Background import status strip */}
      {Object.keys(backgroundJobs).length > 0 ? (
        <BackgroundJobsStrip
          jobs={backgroundJobs}
          onClearQueue={() => {
            setBackgroundJobs((prev) => {
              const next: Record<string, BgJob> = {};
              for (const [itemId, job] of Object.entries(prev)) {
                if (isActiveBgJob(job)) next[itemId] = job;
                else submittedItemIdsRef.current.delete(itemId);
              }
              persistSubmittedItemIds(submittedItemIdsRef.current);
              return next;
            });
            void loadQueue(true);
          }}
          onReconcileAll={() => {
            for (const [itemId, job] of Object.entries(backgroundJobsRef.current)) {
              if (job.status === 'status_unknown' || job.status === 'still_running') handleRetryJob(itemId);
            }
          }}
          onDismissJob={(itemId) =>
            setBackgroundJobs((prev) => { const n = { ...prev }; delete n[itemId]; return n; })
          }
          onRetryJob={handleRetryJob}
        />
      ) : null}

      {/* One-at-a-time queue */}
      {!loading && !error && visibleItems.length === 0 ? (
        <Alert severity="success">No more albums need review.</Alert>
      ) : visibleItems.length > 0 ? (
        <>
          {/* Queue position navigator */}
          <div className="flex items-center gap-2 rounded border border-graphite-700 bg-graphite-800/40 px-3 py-2">
            <button
              aria-label="Previous item"
              title="Previous [[]"
              className="flex h-8 w-8 items-center justify-center rounded text-zinc-300 transition hover:bg-graphite-700 disabled:cursor-not-allowed disabled:opacity-30"
              disabled={cursor === 0}
              onClick={handlePrev}
            >
              ←
            </button>
            <div className="min-w-0 flex-1 text-center">
              <div className="text-[0.7rem] text-zinc-500">
                {cursor + 1} of {visibleItems.length} shown · {filterCounts[filter]} in filter · {items.length} loaded of {filterCounts.all} total
                {activeJobHiddenCount ? ` · ${activeJobHiddenCount} hidden by active jobs` : ''}
                {unloadedReviewCount ? ` · ${unloadedReviewCount} beyond page limit` : ''}
              </div>
              {activeItem ? (
                <div className="truncate text-sm font-semibold text-zinc-200" title={itemTitle(activeItem)}>
                  {itemTitle(activeItem)}
                </div>
              ) : null}
            </div>
            <button
              aria-label="Next item"
              title="Next []]"
              className="flex h-8 w-8 items-center justify-center rounded text-zinc-300 transition hover:bg-graphite-700 disabled:cursor-not-allowed disabled:opacity-30"
              disabled={cursor >= visibleItems.length - 1}
              onClick={handleNext}
            >
              →
            </button>
            <button
              className="rounded border border-graphite-600 px-3 py-1 text-xs font-medium text-zinc-400 transition hover:border-graphite-500 hover:text-zinc-200 disabled:opacity-30"
              disabled={cursor >= visibleItems.length - 1}
              onClick={handleSkip}
            >
              Skip
            </button>
          </div>

          {/* Active review card — single item only */}
          {activeItem ? (
            <ReviewCard
              key={activeItem.id}
              item={activeItem}
              mbid={mbids[activeItem.id] ?? initialMbid(activeItem)}
              suggestion={suggestions[activeItem.id]}
              actionState={actions[activeItem.id]}
              selectedMatch={selectedMatches[activeItem.id]}
              targetPreviewState={currentTargetPreviewState(activeItem, selectedMatches[activeItem.id], targetPreviews)}
              onMbidChange={handleMbidChange}
              onUseCandidate={handleUseCandidate}
              onSelectMatch={handleSelectMatch}
              onSuggest={() => void handleSuggest(activeItem)}
              onApply={() => startApply(activeItem)}
              onDismiss={() => requestDismiss(activeItem)}
              onDeleteFolder={() => void requestDeleteFolder(activeItem)}
              onCleanupFiles={() => void runCleanupFiles(activeItem)}
              importJobActive={
                backgroundJobs[activeItem.id]?.status === 'running' ||
                backgroundJobs[activeItem.id]?.status === 'still_running'
              }
            />
          ) : null}
        </>
      ) : null}

      <ConfirmDialog
        intent={confirmIntent}
        onClose={() => setConfirmIntent(null)}
        onConfirm={confirm}
      />
    </section>
  );
}



















