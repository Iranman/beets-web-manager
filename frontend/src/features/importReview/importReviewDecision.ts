/**
 * Import Review action decisions (ARCH-005).
 *
 * The backend is the authority: backend/import_review_decision.py implements
 * exactly these rules, and the apply path asks it for the final verdict
 * (POST /api/import-review/decision). This module is the page's instant
 * mirror of that authority. It holds every decision rule the page used to
 * compute inline, as pure functions with no React and no I/O; the page only
 * displays what they return.
 *
 * Both implementations run against the same cases in CI
 * (frontend/tests/fixtures/import_review_decision_cases.json), so a rule changed on
 * one side only fails the build.
 */
import type { ImportTargetPreviewResponse, ReviewEvidence, ReviewItem } from '../../api/types';

export type MatchBucket = 'ready' | 'blocked' | 'audio_mismatch' | 'failed' | 'no_candidate' | 'needs_id';

export type MatchConfidenceLevel = 'high' | 'medium' | 'low' | 'blocked' | 'not_importable';

export interface TrackRow {
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

export type TargetPreviewState = {
  status: 'idle' | 'loading' | 'ready' | 'error';
  key: string;
  preview?: ImportTargetPreviewResponse;
  error?: string;
};

export type SelectedMatch = {
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
  source: 'ai' | 'candidate' | 'manual' | 'musicbrainz_acoustid' | 'musicbrainz' | string;
  ai_available?: boolean;
  ai_unavailable_reason?: string;
  matching_method?: string;
  warnings?: string[];
  action_eligibility?: unknown;
  eligibility_reason?: string;
  matching_contract?: Record<string, unknown>;
  acoustid_corroboration?: string;
  fingerprint_conflicts?: string[];
  recording_id_conflicts?: string[];
  title_mismatch_warnings?: string[];
  required_review?: boolean;
};

export const IMPORTABLE_TRACK_STATUSES = new Set<TrackRow['status']>([
  'matched',
  'fuzzy',
  'verified_match',
  'acoustid_verified',
]);

export function sameMbid(left?: string, right?: string): boolean {
  return Boolean(left && right && left.trim().toLowerCase() === right.trim().toLowerCase());
}

export function isMusicBrainzUuid(value?: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test((value ?? '').trim());
}

export function existingAlbumId(item: ReviewItem): number {
  return Number(item.existing_album_id || item.existing_album_ids?.[0] || 0);
}

export function preflightHasAudioMismatch(preflight?: ReviewEvidence['preflight']): boolean {
  return Boolean(preflight?.acoustid_mismatch);
}

export function hasAudioMismatchEvidence(item?: Pick<ReviewItem, 'evidence'>): boolean {
  return preflightHasAudioMismatch(item?.evidence?.preflight);
}

export function itemMatchBucket(item: ReviewItem): MatchBucket {
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

export function selectedImportRows(rows: TrackRow[] = []): TrackRow[] {
  return rows.filter((row) => IMPORTABLE_TRACK_STATUSES.has(row.status) && Boolean(row.source_path));
}

export function selectedImportSourceFiles(
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

export function actionLabel(item: ReviewItem, selectedMatch?: SelectedMatch, preview?: ImportTargetPreviewResponse): string {
  if (item.type === 'library_no_mb') return item.target_kind === 'item' ? 'Attach recording ID' : 'Match album';
  const selectedCount = selectedMatch ? selectedImportSourceFiles(selectedMatch, preview).length : 0;
  const previewCount = preview?.tracks_to_import_count ?? selectedCount;
  if (selectedMatch?.identity_validated === false) return 'Import blocked';
  if (selectedMatch?.is_partial_import) {
    const n = Math.max(0, Math.min(selectedCount || previewCount, previewCount));
    return existingAlbumId(item)
      ? `Repair ${n} matched track${n === 1 ? '' : 's'}`
      : `Import ${n} matched track${n === 1 ? '' : 's'}`;
  }
  if (selectedMatch?.auto_fix_eligible) {
    return existingAlbumId(item) ? 'Complete Verified Repair' : 'Complete Verified Import';
  }
  return existingAlbumId(item) ? 'Repair with ID' : 'Import with ID';
}

export function targetPreviewBlockReason(
  item: ReviewItem,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): string {
  const importLike = item.type === 'pending_ai' && !existingAlbumId(item);
  if (!importLike || !selectedMatch) return '';
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

export function applyBlockReason(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): string {
  const releaseGroupId = mbid.trim();
  if (!releaseGroupId) {
    return item.target_kind === 'item'
      ? 'Enter or select a MusicBrainz recording ID first.'
      : 'Enter or select a MusicBrainz Release Group ID first.';
  }
  const importLike = item.type === 'pending_ai' && !existingAlbumId(item);
  if (!importLike) {
    if (selectedMatch?.preflight_status === 'failed') {
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

export function storedBlockedReason(item: ReviewItem): string {
  if (item.blocked_reason) return item.blocked_reason;
  const text = [item.status, item.reason].filter(Boolean).join(' ');
  return /\bblock(?:ed|ing)?\b/i.test(text) ? item.reason || item.status || 'Import blocked.' : '';
}

export function storedBlockedNextAction(item: ReviewItem): string {
  return item.blocked_next_action || '';
}

export function actionBlockReasonForFilter(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): string {
  const selectedBlock = selectedMatch ? applyBlockReason(item, mbid, selectedMatch, targetPreviewState) : '';
  return selectedBlock || storedBlockedReason(item);
}

export function shouldShowBlockedBucket(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): boolean {
  if (item.type === 'skipped' || hasAudioMismatchEvidence(item)) return false;
  return Boolean(actionBlockReasonForFilter(item, mbid, selectedMatch, targetPreviewState));
}

export function shouldShowReadyBucket(
  item: ReviewItem,
  mbid: string,
  selectedMatch?: SelectedMatch,
  targetPreviewState?: TargetPreviewState,
): boolean {
  if (item.type === 'skipped' || hasAudioMismatchEvidence(item)) return false;
  if (shouldShowBlockedBucket(item, mbid, selectedMatch, targetPreviewState)) return false;
  if (!selectedMatch) return false;
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

export function blockedActionHint(reason: string): string {
  const value = reason.toLowerCase();
  if (value.includes('music format preferences') || value.includes('format policy')) return 'Choose another source or update Music Format Preferences before retrying.';
  if (value.includes('target path')) return 'Fix the target path conflict, then retry this item.';
  if (value.includes('out of sync') || value.includes('visible musicbrainz match')) return 'Select the visible candidate again so the ID field and comparison agree.';
  if (value.includes('release group id') || value.includes('valid musicbrainz')) return 'Use Find Match or enter a valid MusicBrainz Release or Release Group ID.';
  if (value.includes('preflight') || value.includes('tracklist') || value.includes('not importable')) return 'Choose a release that matches the files, or delete the source folder if the audio is wrong.';
  if (value.includes('no verified tracks') || value.includes('selected file count')) return 'Adjust the selected track mapping before importing.';
  return 'Resolve this block before importing; uncertain audio stays in review.';
}

/** One decision input: the item, the ID in the field, the selected match and the target preview. */
export interface ImportReviewDecisionEntry {
  item: ReviewItem;
  mbid: string;
  selected_match?: SelectedMatch | null;
  target_preview_state?: TargetPreviewState | null;
}

/** The decision payload; identical in shape to the backend's. */
export interface ImportReviewDecision {
  match_bucket: MatchBucket;
  blocked: boolean;
  ready: boolean;
  can_apply: boolean;
  apply_block_reason: string;
  block_reason: string;
  next_action: string;
  action_label: string;
  selected_source_files: string[];
}

/** The full decision for one entry (mirror of backend import_review_decision.decide). */
export function decideImportReview(entry: ImportReviewDecisionEntry): ImportReviewDecision {
  const item = entry.item;
  const mbid = entry.mbid ?? '';
  const selectedMatch = entry.selected_match ?? undefined;
  const targetPreviewState = entry.target_preview_state ?? undefined;
  const preview = targetPreviewState?.preview;
  const applyReason = applyBlockReason(item, mbid, selectedMatch, targetPreviewState);
  const blockReason = applyReason || storedBlockedReason(item);
  const nextAction = applyReason
    ? blockedActionHint(applyReason)
    : storedBlockedNextAction(item) || (blockReason ? blockedActionHint(blockReason) : '');
  return {
    match_bucket: itemMatchBucket(item),
    blocked: shouldShowBlockedBucket(item, mbid, selectedMatch, targetPreviewState),
    ready: shouldShowReadyBucket(item, mbid, selectedMatch, targetPreviewState),
    can_apply: !applyReason,
    apply_block_reason: applyReason,
    block_reason: blockReason,
    next_action: nextAction,
    action_label: actionLabel(item, selectedMatch, preview),
    selected_source_files: selectedMatch ? selectedImportSourceFiles(selectedMatch, preview) : [],
  };
}
