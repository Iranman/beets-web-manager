export type ReviewItemType = 'all' | 'pending_ai' | 'skipped' | 'library_no_mb';

export type ReviewOriginType =
  | 'all'
  | 'playlist'
  | 'batch_import'
  | 'manual_import'
  | 'downloads'
  | 'missing_track_acquisition'
  | 'cleanup_leftover'
  | 'unknown';

export type ReviewCounts = Partial<Record<ReviewItemType, number>>;
export type ReviewOriginCounts = Partial<Record<ReviewOriginType, number>>;

export interface ReviewQueueParams {
  limit?: number;
  status?: string;
  origin_type?: ReviewOriginType;
  search?: string;
  evidence_only?: boolean;
}

export interface ReviewCandidate {
  artist?: string;
  album?: string;
  year?: number | string;
  date?: number | string;
  mb_albumid?: string;
  mb_url?: string;
  mb_releasegroupid?: string;
  mb_releasegroupurl?: string;
  release_group_primary_type?: string;
  country?: string;
  formats?: string[];
  mediums?: Array<{ position?: number; format?: string; tracks?: number }>;
  format_summary?: string;
  is_vinyl?: boolean;
  status?: string;
  packaging?: string;
  label?: string;
  labels?: string[];
  catalog_numbers?: string[];
  label_entries?: Array<{ label?: string; catalog_number?: string }>;
  barcode?: string;
  cover_art?: boolean | null;
  front_art?: boolean | null;
  cover_art_count?: number;
  edition_count?: number;
  edition_alternates?: ReviewCandidate[];
  is_current?: boolean;
  tracks?: number;
  match_total?: number | string;
  acoustid_hits?: number;
  acoustid_release_hits?: number;
  score?: number | string;
}

export interface RecordingLocalEvidence {
  filename?: string;
  source_path?: string;
  title?: string;
  artist?: string;
  album?: string;
  albumartist?: string;
  year?: number | string;
  track?: number | string;
  disc?: number | string;
  duration?: string;
  duration_seconds?: number;
  mb_trackid?: string;
  mb_albumid?: string;
  mb_releasegroupid?: string;
  fingerprint_status?: string;
}

export interface RecordingLinkedRelease {
  mb_albumid?: string;
  mb_url?: string;
  album?: string;
  artist?: string;
  date?: string;
  year?: string;
  country?: string;
  status?: string;
  label?: string;
  mb_releasegroupid?: string;
  mb_releasegroupurl?: string;
  release_group_primary_type?: string;
  release_group_secondary_types?: string[];
  disc?: string;
  medium_position?: number | null;
  medium_format?: string;
  media_format?: string;
  track?: string;
  track_number?: string;
  track_position?: number | null;
  tracktotal?: string;
  tracks?: number;
  track_count?: number;
  duration_ms?: number | null;
  local_match?: {
    album_score?: number;
    artist_score?: number;
    year_score?: number;
    year_match?: boolean;
    year_delta?: number | null;
    total?: number;
  };
}

export interface RecordingMatchField {
  status?: 'yes' | 'no' | 'fuzzy' | 'conflict' | 'unknown' | 'tolerance';
  score?: number;
  local?: string;
  suggested?: string;
  delta_seconds?: number | null;
}

export interface RecordingMatchDecision {
  title_match?: RecordingMatchField;
  artist_match?: RecordingMatchField;
  album_match?: RecordingMatchField;
  year_match?: RecordingMatchField;
  duration_match?: RecordingMatchField;
  release_group_match?: RecordingMatchField;
  confidence_score?: number;
  safety_result?: string;
}

export interface ReviewRecordingCandidate {
  candidate_index?: number;
  candidate_type?: 'recording' | 'release' | 'release_group';
  mb_trackid?: string;
  mb_url?: string;
  musicbrainz_url?: string;
  title?: string;
  artist?: string;
  recording_title?: string;
  recording_artist?: string;
  album?: string;
  year?: string | number;
  release_title?: string;
  release_artist?: string;
  release_date?: string;
  release_year?: string | number;
  mb_albumid?: string;
  mb_albumids?: string[];
  mb_releasegroupid?: string;
  mb_releasegroupurl?: string;
  track_number?: string;
  medium_position?: number | null;
  country?: string;
  medium_format?: string;
  media_format?: string;
  duration?: string;
  source?: string;
  match_method?: string;
  score?: number | string;
  match_total?: number | string;
  confidence?: string;
  confidence_score?: number | null;
  acoustid_score?: number | null;
  score_breakdown?: Record<string, number | string>;
  selected_release?: RecordingLinkedRelease;
  linked_releases?: RecordingLinkedRelease[];
  same_recording_release_count?: number;
  matching_local_release_found?: boolean;
  decision?: RecordingMatchDecision;
  conflicts?: string[];
  recommended_action?: string;
  requires_confirmation?: boolean;
  safety_result?: string;
  safety_key?: 'safe' | 'review' | 'conflict' | 'none' | string;
  reason?: string;
}
export interface ReviewPreflight {
  ok?: boolean;
  matches?: number;
  expected?: number;
  audio_count?: number;
  min_required?: number;
  match_ratio?: number;
  source_match_ratio?: number;
  artist_ok?: boolean;
  artist_score?: number;
  too_many_extras?: boolean;
  oversized_subset_complete?: boolean;
  acoustid_mismatch?: boolean;
  acoustid_target_hits?: number;
  acoustid_top_release?: string;
  acoustid_top_hits?: number;
  acoustid_release_hits?: Record<string, number>;
  release_title?: string;
  release_artist?: string;
  release_group?: string;
  error?: string;
  examples?: string[];
  reason?: string;
}

export interface ReviewEvidence {
  top_candidates?: ReviewCandidate[];
  current?: RecordingLocalEvidence;
  recording_candidates?: ReviewRecordingCandidate[];
  selected_recording_candidate?: ReviewRecordingCandidate;
  missing_id_type?: string;
  fingerprint?: {
    status?: string;
    acoustid_status?: string;
    score?: number;
    acoustid_id?: string;
    mb_trackid?: string;
    mb_releasegroupid?: string;
  };
  preflight?: ReviewPreflight | null;
  folder?: {
    path?: string;
    guessed_artist?: string;
    guessed_album?: string;
    guessed_year?: string;
    track_count?: number;
    nested_audio_count?: number;
    track_titles?: string[];
    filenames?: string[];
  };
}

export interface ReviewItem {
  id: string;
  type: Exclude<ReviewItemType, 'all'>;
  status?: string;
  status_key?: string;
  blocked_reason?: string;
  blocked_next_action?: string;
  title?: string;
  artist?: string;
  album?: string;
  albumartist?: string;
  year?: number | string;
  track?: number | string;
  disc?: number | string;
  duration?: string;
  duration_seconds?: number;
  album_id?: number;
  first_item_id?: number;
  item_id?: number;
  /** For type "library_no_mb": "item" means this is a singleton (no album
   * row) needing a recording ID, not a release ID like the album case. */
  target_kind?: 'album' | 'item';
  tracks?: number;
  path?: string;
  folder?: string;
  folder_name?: string;
  confidence?: string;
  reason?: string;
  mb_albumid?: string;
  mb_trackid?: string;
  mb_releasegroupid?: string;
  mb_releasegroupurl?: string;
  mb_url?: string;
  mb_valid?: boolean;
  missing_id_type?: string;
  existing_album_ids?: number[];
  existing_album_id?: number;
  sort_ts?: number;
  evidence?: ReviewEvidence;
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
  suggestion?: AiSuggestion;
  origin_type?: ReviewOriginType;
  origin_label?: string;
  origin_id?: string;
  source_playlist_id?: string;
  source_playlist_name?: string;
  source_batch_id?: string;
  source_folder?: string;
  created_by_workflow?: string;
}

export interface ReviewQueueResponse {
  ok: true;
  items: ReviewItem[];
  total: number;
  counts: ReviewCounts;
  origin_counts?: ReviewOriginCounts;
}

export interface ApiOkResponse {
  ok: boolean;
  error?: string | null;
}
export type TransactionStatus =
  | 'Pending'
  | 'Preview'
  | 'Approved'
  | 'Running'
  | 'Completed'
  | 'Cancelled'
  | 'Failed'
  | 'Rolled Back'
  | 'Partially Rolled Back';

export interface TransactionConfidence {
  overall?: number | null;
  ai?: number | null;
  acoustid?: number | null;
  musicbrainz?: number | null;
  artwork?: number | null;
}

export interface TransactionRollback {
  available: boolean;
  reason?: string;
}

export interface TransactionCounts {
  items?: number;
  files?: number;
  changes?: number;
  warnings?: number;
  errors?: number;
  [key: string]: number | undefined;
}

export interface TransactionSummary {
  id: string;
  created_at: number;
  updated_at: number;
  initiating_user?: string;
  originating_job?: string | null;
  operation_type: string;
  status: TransactionStatus | string;
  dry_run: boolean;
  summary: string;
  reason?: string;
  source?: string;
  confidence: TransactionConfidence;
  counts: TransactionCounts;
  rollback: TransactionRollback;
  metadata?: Record<string, unknown>;
}

export interface TransactionMetadataDiffRow {
  field: string;
  old?: unknown;
  new?: unknown;
  changed?: boolean;
}

export interface TransactionFilesystemChange {
  operation?: string;
  old?: string;
  new?: string;
}

export interface TransactionChange {
  id?: string;
  operation?: string;
  artist?: string;
  album?: string;
  track?: string;
  current_metadata?: Record<string, unknown>;
  new_metadata?: Record<string, unknown>;
  metadata_diff?: TransactionMetadataDiffRow[];
  filesystem?: TransactionFilesystemChange[];
  artwork?: Record<string, unknown>;
  confidence?: TransactionConfidence;
  reason?: string;
  source?: string;
  warnings?: string[];
  errors?: string[];
}

export interface TransactionDetail extends TransactionSummary {
  changes: TransactionChange[];
  changes_total: number;
  changes_offset?: number;
  changes_limit?: number;
  logs?: string[];
  backup?: Record<string, unknown>;
  result_summary?: Record<string, unknown>;
}

export interface TransactionListResponse {
  ok: boolean;
  transactions: TransactionSummary[];
  total: number;
}

export interface TransactionDetailResponse {
  ok: boolean;
  transaction: TransactionDetail;
}

export interface TransactionSettings {
  enabled: boolean;
  backups_enabled: boolean;
  rollback_enabled: boolean;
  backup_retention_days: number;
  automatic_approval_threshold: number;
  require_review_below_threshold: boolean;
  maximum_undo_history: number;
  dry_run_by_default: boolean;
}

export interface TransactionSettingsResponse {
  ok: boolean;
  settings: TransactionSettings;
}

export interface SetupPathCheck {
  path: string;
  exists: boolean;
  writable?: boolean;
  error?: string;
}

export interface SetupStatusResponse {
  ok: boolean;
  status: 'ready' | 'warning' | string;
  version: string;
  demo_mode: boolean;
  setup_complete: boolean;
  blocking_reasons: string[];
  paths: {
    config: SetupPathCheck;
    music_library: SetupPathCheck;
    downloads: SetupPathCheck;
    beets_config: Pick<SetupPathCheck, 'path' | 'exists'>;
  };
  fpcalc: {
    available: boolean;
    path: string;
  };
  beets?: {
    available: boolean;
    path?: string;
    version?: string;
    configured_plugins?: string[];
    pluginpath?: string[];
    plugin_failures?: string[];
    /** Exit code of the supported plugin-loader probe (`beet -c <config>
     * -vv version`) -- kept under its original name for compatibility, but
     * it no longer reflects an unsupported `beet plugins` command. */
    plugins_returncode?: number | null;
    plugin_loader_returncode?: number | null;
    plugin_loader_ok?: boolean;
    plugin_loader_timed_out?: boolean;
    plugin_loader_error?: string;
    replaygain_backend?: string;
    replaygain_command?: string;
    discogs_token_configured?: boolean;
    listenbrainz_token_configured?: boolean;
  };
  auth: {
    token_configured: boolean;
    token_auto_generated: boolean;
    password_configured: boolean;
  };
  integrations: Record<string, {
    configured: boolean;
    required: boolean;
    state?: 'configured' | 'not_configured' | 'installed_but_disabled' | 'dependency_plugin_missing' | 'plugin_loader_failed' | 'connection_test_failed' | 'connected' | string;
    note?: string;
    detail?: string;
  }>;
  settings: Record<string, unknown>;
}

/** Response from POST /api/setup/auth-token/regenerate. `token` is the
 * plaintext value and is returned exactly once, here -- every other read of
 * BEETS_WEB_AUTH_TOKEN (GET /api/setup/env) is masked, so the caller must
 * capture and display it immediately and never expect to fetch it again. */
export interface SetupAuthTokenRegenerateResponse {
  ok: boolean;
  error?: string;
  token?: string;
  warning?: string;
  backup_path?: string;
}

/** Live connectivity test result from /api/setup/test/{ai,musicbrainz,acoustid,plex}.
 * Each integration is tested and reported independently -- one failing must
 * never block or hide the others. */
export interface SetupIntegrationTestResponse {
  ok: boolean;
  status: 'ready' | 'failed' | 'not_configured' | string;
  error?: string;
  model?: string;
  fpcalc_available?: boolean;
  fpcalc_version?: string;
  music_libraries?: string[];
}

export interface SetupEnvVariable {
  name: string;
  section: string;
  secret: boolean;
  has_value: boolean;
  value: string;
  source: 'file' | 'process' | 'example' | string;
  runtime_has_value: boolean;
  runtime_value: string;
}

export interface SetupEnvResponse {
  ok: boolean;
  error?: string;
  env_file: string;
  exists: boolean;
  example_file: string;
  restart_required_after_save: boolean;
  variables: SetupEnvVariable[];
  saved?: string[];
  backup_path?: string;
  process_applied?: boolean;
}

export interface SetupEnvSavePayload {
  variables: Record<string, string>;
  clear?: string[];
}

export interface AlbumMbFormatResponse extends ApiOkResponse {
  track_text: string;
  mb_url: string;
  album: string;
  albumartist: string;
  year: number | string;
  track_count: number;
}


export interface SubmissionReadiness {
  plugins: Record<string, boolean>;
  fpcalc_available: boolean;
  fpcalc_path?: string;
  pyacoustid_available: boolean;
  acoustid_key_configured: boolean;
  beet_available: boolean;
}

export interface SubmissionSummary {
  target_type: 'album' | 'item' | string;
  album_id?: number;
  item_id?: number;
  title: string;
  albumartist: string;
  release_type?: string;
  secondary_type?: string;
  release_status?: string;
  release_date?: string | number;
  country?: string;
  label?: string;
  catalog_number?: string;
  barcode?: string;
  format?: string;
  disc_count?: number;
  track_count?: number;
  runtime?: number;
  runtime_display?: string;
  source_path?: string;
  mb_albumartistid?: string;
  mb_albumartistids?: string;
  mb_releasegroupid?: string;
  mb_albumid?: string;
  cover_art_url?: string;
  workflow_stage?: string;
  resolved_state?:
    | 'imported_album'
    | 'imported_singleton'
    | 'imported_singletons'
    | 'unimported_album'
    | 'loose_tracks'
    | 'empty'
    | 'inaccessible'
    | string;
}

export interface ReferenceUrlField {
  field: string;
  value: string;
  source: string;
  confidence: 'high' | 'medium' | 'low' | string;
}

export interface ReferenceUrlArtworkCandidate {
  url: string;
  width?: number;
  height?: number;
}

export interface ReferenceUrlEntry {
  id: string;
  url: string;
  source: 'youtube' | 'musicbrainz' | 'discogs' | 'bandcamp' | 'soundcloud' | 'web' | string;
  added_at?: number;
  status: 'ok' | 'error' | string;
  error?: string;
  raw?: Record<string, unknown>;
  normalized?: Record<string, unknown>;
  fields?: ReferenceUrlField[];
  artwork_candidates?: ReferenceUrlArtworkCandidate[];
  playlist_entries?: Array<{ title: string; duration?: number; url?: string }>;
  mb_links?: string[];
  discogs_links?: string[];
  mb_entity_type?: string;
  mb_mbid?: string;
}

export interface SubmissionTrack {
  index: number;
  item_id: number;
  album_id?: number;
  disc: number;
  track: number;
  title: string;
  artist: string;
  album?: string;
  albumartist?: string;
  duration?: number;
  duration_display?: string;
  file_name?: string;
  file_path?: string;
  file_available?: boolean;
  format?: string;
  mb_trackid?: string;
  mb_albumid?: string;
  fingerprint_status?: string;
  validation_status?: string;
}

export type SubmissionCheckSeverity = 'blocked' | 'needs_attention' | 'ready' | string;
export type SubmissionCheckStage = 'artist' | 'identify' | 'musicbrainz_prep' | 'attach_ids' | 'acoustid' | 'complete' | string;
export type SubmissionCheckGroup = 'local_files' | 'metadata' | 'musicbrainz' | 'acoustid' | 'system' | string;
export type SubmissionActionType =
  | 'rescan' | 'open_import_review' | 'edit_metadata' | 'edit_tracks' | 'resolve_artist'
  | 'review_duplicates' | 'open_mb_handoff' | 'open_settings' | 'view_setup_details' | '';

export interface SubmissionArtistMatch {
  id: string;
  name: string;
  score: number;
  disambiguation?: string;
}

export interface SubmissionPreflightCheck {
  id: string;
  label: string;
  /** @deprecated raw backend status; use severity for display */
  status: 'pass' | 'fail' | 'warning' | string;
  severity: SubmissionCheckSeverity;
  stage: SubmissionCheckStage;
  group: SubmissionCheckGroup;
  explanation?: string;
  action?: string;
  action_type?: SubmissionActionType;
  action_target?: string;
  affected?: string[];
  blocking?: boolean;
  current_stage_relevant?: boolean;
}

export interface SubmissionPreflight {
  checks: SubmissionPreflightCheck[];
  missing_count: number;
  warning_count: number;
  musicbrainz_ready: boolean;
  acoustid_ready: boolean;
  current_stage?: SubmissionCheckStage;
  current_stage_label?: string;
}

export interface SubmissionTargetResponse extends ApiOkResponse {
  target_type: 'album' | 'item' | 'folder' | string;
  target_id: number | string;
  summary: SubmissionSummary;
  tracks: SubmissionTrack[];
  preflight: SubmissionPreflight;
  readiness: SubmissionReadiness;
  draft?: Record<string, unknown>;
  artist_match?: SubmissionArtistMatch | Record<string, never>;
}

export interface SubmissionDraftResponse extends ApiOkResponse {
  draft: Record<string, unknown>;
  removed?: boolean;
}

export interface SubmissionReferenceUrlResponse extends ApiOkResponse {
  reference: ReferenceUrlEntry;
  draft: Record<string, unknown>;
}

export interface SubmissionMusicBrainzMapping {
  item_id?: number;
  disc?: number;
  track?: number;
  local_title?: string;
  musicbrainz_title?: string;
  recording_mbid?: string;
  duration_delta_ms?: number;
  status?: string;
  issues?: string[];
}

export interface SubmissionMusicBrainzValidationResponse extends ApiOkResponse {
  entity_type?: string;
  release_group_mbid?: string;
  requires_release_mbid?: boolean;
  message?: string;
  release?: {
    mb_albumid?: string;
    title?: string;
    albumartist?: string;
    mb_albumartistid?: string;
    mb_albumartistids?: string;
    mb_releasegroupid?: string;
    date?: string;
    country?: string;
    track_count?: number;
  };
  local?: SubmissionSummary;
  mapping?: SubmissionMusicBrainzMapping[];
  mismatches?: SubmissionMusicBrainzMapping[];
  needs_confirmation?: boolean;
}

export interface SubmissionAttachMbidsPayload {
  mb_albumartistid: string;
  mb_releasegroupid: string;
  mb_albumid?: string;
  recordings?: Array<{ item_id: number; mb_trackid: string }>;
}

export interface JobStartResponse extends ApiOkResponse {
  job_id: string;
  batch_job_id?: string;
  state?: AiBatchState;
  reconnected?: boolean;
}


export type AiBatchFolderStatus =
  | 'queued'
  | 'claimed'
  | 'scanning'
  | 'ai_queued'
  | 'ai_running'
  | 'ai_completed'
  | 'importing'
  | 'retrying'
  | 'completed'
  | 'imported'
  | 'review_created'
  | 'review_required'
  | 'skipped'
  | 'policy_rejected'
  | 'replacement_queued'
  | 'replacement_unavailable'
  | 'completed_with_fallback'
  | 'handled_warning'
  | 'failed'
  | 'import_failed'
  | 'ai_failed'
  | 'timed_out'
  | 'canceled';

export interface AiBatchFolderState {
  folder_id: string;
  batch_job_id: string;
  source_folder: string;
  status: AiBatchFolderStatus | string;
  current_step?: string;
  ai_suggest_status?: string;
  ai_suggest_started_at?: number | null;
  ai_suggest_completed_at?: number | null;
  ai_suggest_error?: string;
  review_item_id?: string;
  detected_artist?: string;
  detected_album?: string;
  suggested_release_group_id?: string;
  failure_reason?: string;
  retry_count?: number;
  retry_exhausted?: boolean;
  max_retries?: number;
  manual_review_required?: boolean;
}

export interface AiBatchState {
  batch_job_id: string;
  job_id?: string;
  source_path: string;
  status: string;
  current_step?: string;
  total_folders_found: number;
  folders_processed: number;
  folders_queued: number;
  folders_running: number;
  folders_completed: number;
  folders_review?: number;
  folders_warning?: number;
  folders_replacement_queued?: number;
  folders_replacement_unavailable?: number;
  folders_failed: number;
  folders_skipped: number;
  folders_unfinished?: number;
  folders_retryable?: number;
  folders_attention?: number;
  folder_status_counts?: Record<string, number>;
  batch_summary?: string;
  recovery_state?: string;
  heartbeat_at?: number | null;
  started_at?: number | null;
  updated_at?: number | null;
  completed_at?: number | null;
  current_folder_names?: string[];
  last_completed_folder?: string;
  last_failed_folder?: string;
  last_failed_reason?: string;
  last_error?: string;
  retry_count?: number;
  ai_max_parallel?: number;
  ai_timeout_seconds?: number;
  heartbeat_age_seconds?: number | null;
  worker_alive?: boolean;
  folders?: AiBatchFolderState[];
}

export interface AiBatchStatusResponse extends ApiOkResponse {
  state: AiBatchState;
}

export interface QbitStatusResponse extends ApiOkResponse {
  configured: boolean;
  url?: string;
  username_configured?: boolean;
  password_configured?: boolean;
  category?: string;
  filter?: string;
  path_aliases?: string;
  repair_allowed_roots?: string[];
  torrent_source_roots?: string[];
}

export interface QbitHardlinkRepairResult {
  dry_run: boolean;
  torrents: number;
  category?: string;
  filter?: string;
  search?: string;
  checked: number;
  already_present: number;
  linked: number;
  would_link: number;
  size_mismatch?: number;
  skipped: number;
  errors: number;
  rechecked_hashes?: number;
  actions?: Array<Record<string, unknown>>;
}

export interface QbitHardlinkRepairResponse extends ApiOkResponse {
  dry_run: boolean;
  job_id?: string;
  result?: QbitHardlinkRepairResult;
  log?: string[];
}

export interface LibraryImportAllLastResponse extends ApiOkResponse {
  updated_at?: number;
  album_count?: number;
  repaired_count?: number;
  queued_count?: number;
  failed_count?: number;
  failures?: Array<{
    label?: string;
    message?: string;
    child_job_id?: string;
    wanted_track_count?: number;
  }>;
  failed_albums?: Array<Record<string, unknown>>;
}

export type JobStatus = 'running' | 'success' | 'failed' | 'cancelled' | 'killed' | 'missing';

export interface JobResponse extends ApiOkResponse {
  job_id?: string;
  status: JobStatus;
  log?: string[];
  // PythonJob return value — present only when status === 'success'
  result?: Record<string, unknown>;
}

export interface AlbumArtCandidate {
  name: string;
  path: string;
  size: number;
  usable?: boolean;
}

export interface AlbumArtStatusResponse extends ApiOkResponse {
  album_id: number;
  album: string;
  albumartist: string;
  album_dir: string;
  art_url: string;
  artpath: string;
  art_exists: boolean;
  local_art_path: string;
  has_local_art: boolean;
  has_removable_art?: boolean;
  broken_art_count?: number;
  candidates: AlbumArtCandidate[];
}

export interface AlbumArtDeleteResponse extends ApiOkResponse {
  removed: string[];
  removed_count: number;
}

export type ArtRepairIssue = 'missing' | 'broken' | 'unresolved';

export interface ArtRepairItem {
  album_id: number;
  albumartist: string;
  album: string;
  year?: number;
  mb_albumid?: string;
  mb_releasegroupid?: string;
  album_dir?: string;
  artpath?: string;
  local_art_path?: string;
  has_local_art?: boolean;
  has_removable_art?: boolean;
  broken_art_count?: number;
  candidate_count?: number;
  track_count?: number;
  outside_library_count?: number;
  missing_file_count?: number;
  first_track_path?: string;
  path_issue?: string;
  can_move_to_library?: boolean;
  repair_action?: string;
  issue: ArtRepairIssue;
  reason?: string;
  actionable?: boolean;
  status?: string;
  source?: string;
  error?: string;
  saved_path?: string;
  last_status?: string;
  last_error?: string;
  last_source?: string;
}

export interface ArtRepairLastRun {
  ok?: boolean;
  started_at?: number;
  finished_at?: number;
  missing?: number;
  saved?: number;
  fetchart_saved?: number;
  fallback_saved?: number;
  failed?: number;
  skipped?: number;
  unresolved?: number;
  saved_items?: ArtRepairItem[];
  failed_items?: ArtRepairItem[];
  unresolved_items?: ArtRepairItem[];
  remaining_items?: ArtRepairItem[];
  counts?: Record<string, number>;
}

export interface ArtRepairReportResponse extends ApiOkResponse {
  generated_at: number;
  total_albums: number;
  counts: {
    total: number;
    missing: number;
    broken: number;
    unresolved: number;
  };
  items: ArtRepairItem[];
  last_run?: ArtRepairLastRun;
}

// ── Clean — Dedup ─────────────────────────────────────────────────────────────

export interface DedupDuplicate {
  source_path: string;
  source_filename: string;
  source_artist: string;
  source_title: string;
  lib_path: string;
  lib_artist: string;
  lib_title: string;
  confidence: 'high' | 'medium';
  reason: string;
}

export interface DedupScanState {
  status: 'running' | 'done';
  job_id?: string;
  job_status?: JobStatus;
  kind?: 'scan' | 'ai_review' | string;
  scan_path?: string;
  log: string[];
  scanned: number;
  total: number;
  found: number;
  duplicates: DedupDuplicate[];
  folders: unknown[];
}

export interface DedupStartResponse extends ApiOkResponse {
  job_id: string;
}

export interface DedupCleanupResponse extends ApiOkResponse {
  deleted?: number;
  skipped?: number;
  dry_run?: boolean;
}

// ── Clean — Album Tracks ──────────────────────────────────────────────────────

export interface AlbumTrackCandidate {
  id: number;
  title: string;
  path: string;
  decision: 'remove' | 'review' | 'keep';
  confidence: string;
  reason: string;
}

export interface AlbumTrackProblem {
  album_id: number;
  artist: string;
  album: string;
  mb_albumid: string;
  expected_count?: number;
  actual_count?: number;
  keep_count?: number;
  local_match_ratio?: number;
  low_album_match?: boolean;
  remove_candidates: AlbumTrackCandidate[];
  review_candidates: AlbumTrackCandidate[];
}

export interface AlbumTrackScanResult {
  ok: boolean;
  albums_scanned: number;
  problem_count: number;
  remove_count: number;
  review_count: number;
  problem_albums: AlbumTrackProblem[];
}

// ── Clean — Artist Folders ────────────────────────────────────────────────────

export interface ArtistFolderEntry {
  name: string;
  path: string;
  audio_files: number;
  subfolders: number;
  db_albums: number;
  db_tracks: number;
  mb_artistid?: string;
  musicbrainz_synthetic?: boolean;
}

export interface ArtistFolderGroup {
  key: string;
  canonical: ArtistFolderEntry;
  sources: ArtistFolderEntry[];
  variants: ArtistFolderEntry[];
  musicbrainz?: {
    id: string;
    name: string;
    score: number;
    matched_entries: string[];
    disambiguation: string;
  };
  rename_to_musicbrainz: boolean;
  source_audio_files: number;
  source_folders: number;
  match_type?: 'name' | 'mb_artist_id';
  /** @deprecated use canonical.name */
  album_count?: number;
}

export interface ArtistFolderScanResponse extends ApiOkResponse {
  root: string;
  count: number;
  name_group_count?: number;
  mbid_group_count?: number;
  groups: ArtistFolderGroup[];
}

// ── Playlists ─────────────────────────────────────────────────────────────────

export interface PlaylistEntry {
  name: string;
  tracks: number;
  available?: number;
  missing?: number;
  quality_bad?: number;
  quality_review?: number;
  m3u_tracks?: number;
  desired_source?: string;
  has_m3u?: boolean;
  has_manifest?: boolean;
  has_checkpoint?: boolean;
  playlist_id?: string;
  manifest_path?: string;
  m3u_path?: string;
  checkpoint_job_id?: string;
  checkpoint_status?: string;
  checkpoint_phase?: string;
  checkpoint_current?: string;
  checkpoint_interrupted?: boolean;
  checkpoint_tracks?: number;
  checkpoint_missing?: number;
  checkpoint_waiting_for_import?: number;
  checkpoint_updated_at?: number;
  plex_tracks?: number;
  plex_tracks_matched?: number;
  plex_tracks_unmatched?: number;
  plex_pending_count?: number;
  plex_synced?: boolean | null;
  plex_synced_count?: number;
  last_plex?: PlaylistPlexResult;
  downloaded?: number;
  imported?: number;
  failed?: number;
  review_required?: number;
  removed?: number;
  excluded?: number;
  source?: string;
  last_sync_status?: string;
  last_sync_error?: string;
  last_pipeline?: PlaylistPipelineSummary;
  sync_status?: string;
}

export interface PlaylistsResponse {
  playlists: PlaylistEntry[];
  supported_sources?: PlaylistSource[];
  download_sources?: string[];
  diagnostics?: string[];
  duration_ms?: number;
}

export type PlaylistSource = 'local_m3u' | 'url' | 'text';

export type PlaylistTrackPipelineStatus =
  | 'pending'
  | 'available'
  | 'searching'
  | 'downloaded'
  | 'waiting_import'
  | 'importing'
  | 'imported'
  | 'plex_synced'
  | 'plex_pending'
  | 'failed'
  | 'missing'
  | 'review_required'
  | 'removed'
  | 'excluded';

export interface PlaylistPipelineSummary {
  action?: string;
  status?: string;
  jobs_job_id?: string;
  playlist_job_id?: string;
  error?: string;
  updated_at?: number;
}

export interface PlaylistTrack {
  artist: string;
  title: string;
  album?: string;
  path?: string;
  local_track_id?: string;
  local_path?: string;
  translated_plex_path?: string;
  source_artist?: string;
  source_title?: string;
  canonicalized?: boolean;
  canonical_source?: string;
  source?: string;
  mb_trackid?: string;
  mb_albumid?: string;
  mb_releasegroupid?: string;
  identity_status?: string;
  identity_reason?: string;
  identity_mb_trackid?: string;
  identity_mb_releasegroupid?: string;
  fingerprint_status?: string;
  acoustid_status?: string;
  acoustid_score?: number;
  pipeline_status?: PlaylistTrackPipelineStatus | string;
  pipeline_source?: string;
  pipeline_message?: string;
  failure_reason?: string;
  reason?: string;
  staged_path?: string;
  plex_issue?: string;
  retry_action?: string;
  acoustid?: string;
  pipeline_updated_at?: number;
}

export interface PlaylistMatchedTrack extends PlaylistTrack {
  id: number;
  query_artist?: string;
  query_title?: string;
  album?: string;
  path?: string;
  score?: number;
  quality?: 'ok' | 'review' | 'bad' | string;
  quality_flags?: string[];
  length?: number;
  format?: string;
  bitrate?: number;
  albumartist?: string;
  source?: string;
  year?: number;
  track?: number;
  disc?: number;
}

export interface PlaylistParseResponse extends ApiOkResponse {
  tracks: PlaylistTrack[];
  matched: PlaylistMatchedTrack[];
  missing: PlaylistTrack[];
  total: number;
}

export interface PlaylistDetailResponse extends PlaylistParseResponse {
  name: string;
  m3u?: string;
  manifest?: string;
  manifest_tracks?: number;
  m3u_tracks?: number;
  desired_source?: string;
  has_m3u?: boolean;
  has_manifest?: boolean;
  has_checkpoint?: boolean;
  playlist_id?: string;
  manifest_path?: string;
  m3u_path?: string;
  checkpoint_job_id?: string;
  checkpoint_status?: string;
  checkpoint_phase?: string;
  checkpoint_current?: string;
  checkpoint_interrupted?: boolean;
  checkpoint_tracks?: number;
  checkpoint_missing?: number;
  checkpoint_waiting_for_import?: number;
  checkpoint_updated_at?: number;
  available?: number;
  missing_count?: number;
  detail_mode?: 'summary' | 'full' | 'rows' | string;
  tracks_loaded?: boolean;
  partial_tracks_loaded?: boolean;
  duration_ms?: number;
  removed_excluded?: PlaylistTrack[];
  counts?: Partial<Record<PlaylistTrackPipelineStatus, number>>;
  source?: string;
  source_content?: string;
  last_sync_status?: string;
  last_plex?: PlaylistPlexResult & {
    status?: string;
    verified_count?: number;
    existing_playlist_count?: number;
    tracks_added?: number;
    tracks_matched?: number;
    tracks_unmatched?: number;
    pending_plex_count?: number;
    pending_tracks?: PlaylistTrack[];
    matched_track_ids?: string[];
    summary_message?: string;
    synced_at?: number;
  };
  last_pipeline?: PlaylistPipelineSummary;
}

export interface PlaylistResolveTrackResponse extends PlaylistDetailResponse {
  updated: boolean;
  resolved?: PlaylistTrack | PlaylistTrack[];
  resolved_count?: number;
  not_found?: PlaylistTrack[];
  errors?: Array<{ track?: PlaylistTrack; error?: string }>;
}

export interface PlaylistTrackSuggestion {
  artist: string;
  title: string;
  source: 'beets' | 'beets-title' | 'musicbrainz' | string;
  confidence: number;
  safe: boolean;
  reason: string;
  item_id?: number;
  mb_trackid?: string;
  mb_url?: string;
  album?: string;
  year?: string;
  title_score?: number;
  artist_score?: number;
}

export interface PlaylistSuggestionRow {
  track: PlaylistTrack;
  suggestions: PlaylistTrackSuggestion[];
  best?: PlaylistTrackSuggestion | null;
}

export interface PlaylistSuggestionsResponse extends ApiOkResponse {
  name: string;
  total_missing: number;
  safe_count: number;
  rows: PlaylistSuggestionRow[];
}

export interface PlaylistApplySuggestionsResponse extends PlaylistResolveTrackResponse {
  suggested?: Array<{ track: PlaylistTrack; best?: PlaylistTrackSuggestion | null }>;
  safe_count?: number;
}

export interface PlaylistPlexResult {
  created?: boolean;
  tracks_added?: number;
  tracks_matched?: number;
  tracks_unmatched?: number;
  tracks_requested?: number;
  pending_plex_count?: number;
  pending_tracks?: PlaylistTrack[];
  matched_track_ids?: string[];
  summary_message?: string;
  complete?: boolean;
  status?: 'success' | 'partial_success' | 'synced' | 'partial' | 'failed' | 'matching' | 'not_configured' | string;
  error?: string | null;
  issue_reason?: string;
  action_needed?: string;
  path_mapping_used?: string;
  path_mapping_verified?: boolean;
  plex_library_locations?: string[];
  sample_beets_path?: string;
  sample_mapped_plex_path?: string;
  sample_mapped_exists?: boolean;
  verified_count?: number;
  existing_playlist_count?: number;
  replaced?: number;
}

export interface PlaylistCreateResponse extends ApiOkResponse {
  m3u?: string;
  manifest?: string;
  plex?: PlaylistPlexResult;
  tracks_in_m3u?: number;
  desired_tracks?: number;
  missing_tracks?: number;
}

export interface PlaylistDeleteResponse extends ApiOkResponse {
  name: string;
  m3u: string;
  deleted_m3u: boolean;
  deleted_manifest?: boolean;
  plex_deleted: number;
  plex_error?: string;
  library_tracks_deleted: number;
}

export interface PlaylistDownloadStartResponse extends ApiOkResponse {
  job_id: string;
  jobs_job_id?: string;
  resumed?: boolean;
}

export interface PlaylistTrackStatus {
  id: string;
  artist: string;
  title: string;
  status: string;
  method?: string;
  message?: string;
  path?: string;
  updated_at?: number;
  source_artist?: string;
  source_title?: string;
  canonicalized?: boolean;
  canonical_source?: string;
}

export interface PlaylistDownloadStatusResponse extends ApiOkResponse {
  status: 'running' | 'done' | 'error' | 'paused' | 'stopped';
  log?: string[];
  done?: number;
  failed?: number;
  total?: number;
  round?: number;
  max_rounds?: number;
  current?: string;
  phase?: string;
  playlist?: PlaylistCreateResponse | null;
  tracks?: PlaylistTrack[];
  matched?: PlaylistMatchedTrack[];
  matched_initial?: number;
  missing_initial?: number;
  matched_after_import?: number;
  missing_after_import?: number;
  missing?: PlaylistTrack[];
  download_methods?: string[];
  track_status_list?: PlaylistTrackStatus[];
  resumed?: boolean;
  interrupted?: boolean;
}

export interface PlaylistPipelineStartResponse extends ApiOkResponse {
  action: string;
  job_id?: string;
  jobs_job_id?: string;
  resumed?: boolean;
}

export interface PlaylistRowsResponse extends ApiOkResponse {
  name: string;
  detail_mode: 'rows' | string;
  tracks_loaded?: boolean;
  partial_tracks_loaded?: boolean;
  group: 'available' | 'missing' | 'waiting' | 'failed' | 'removed' | string;
  rows: Array<PlaylistTrack | PlaylistMatchedTrack>;
  offset: number;
  limit: number;
  row_count: number;
  known_total?: number;
  has_more?: boolean;
  scanned?: number;
  duration_ms?: number;
  summary?: PlaylistDetailResponse;
}
export interface PlaylistTrackActionResponse extends ApiOkResponse {
  action: string;
  track: PlaylistTrack;
  deleted?: boolean;
  path?: string;
  playlist: PlaylistDetailResponse;
  job?: PlaylistPipelineStartResponse;
  retry_error?: string;
}

export interface PlaylistQualityCleanupCandidate extends PlaylistMatchedTrack {
  albumartist?: string;
  recommended_action?: 'repair' | 'delete_preview' | 'keep' | string;
  repairable?: boolean;
}

export interface PlaylistQualityCleanupResponse extends ApiOkResponse {
  dry_run: boolean;
  action?: string;
  filter?: string;
  queued?: boolean;
  summary: {
    candidates: number;
    bad: number;
    review: number;
    repair?: number;
    delete_preview?: number;
    move_singletons?: number;
  };
  candidates?: PlaylistQualityCleanupCandidate[];
  job_id?: string;
  backup?: string;
  rows_deleted?: number;
  files_deleted?: number;
  rows_repaired?: number;
  rows_moved?: number;
  repaired?: Array<{
    id: number;
    repaired: boolean;
    reason?: string;
    old_path?: string;
    new_path?: string;
    artist?: string;
    title?: string;
    album?: string;
  }>;
  deleted?: Array<{ id: number; path: string; file_deleted: boolean }>;
  moved?: Array<{ id: number; old_path?: string; new_path?: string; moved?: boolean; reason?: string }>;
}

export interface PlaylistQualityPlacePayload {
  item_id: number;
  playlist?: string;
  placement: {
    artist: string;
    title: string;
    albumartist: string;
    album: string;
    year?: number;
    track?: number;
    disc?: number;
    tracktotal?: number;
    disctotal?: number;
    mb_trackid?: string;
    mb_albumid?: string;
    mb_releasegroupid?: string;
  };
}

export interface PlaylistQualityPlaceResponse extends ApiOkResponse {
  queued: boolean;
  job_id: string;
  candidate?: PlaylistQualityCleanupCandidate;
  placement?: PlaylistQualityPlacePayload['placement'] & { ok?: boolean };
}

export interface PlaylistSyncStatusResponse extends ApiOkResponse {
  enabled: boolean;
  interval: number;
  running: boolean;
  last_run?: number;
  last_error?: string;
  last_log?: string[];
  last_result?: {
    playlists_seen?: number;
    playlists_updated?: number;
    tracks_written?: number;
    local_added?: number;
    plex_added?: number;
    skipped?: number;
  } | null;
}

export interface PlaylistSyncStartResponse extends ApiOkResponse {
  job_id: string;
}

// ── Lidarr Wanted ─────────────────────────────────────────────────────────────

export interface LidarrWantedAlbum {
  artist: string;
  album: string;
  year: string;
  type: string;
  lidarr_id: number;
  mb_albumid: string;
  mb_url: string;
  monitored: boolean;
}

export interface WantedResponse extends ApiOkResponse {
  missing: LidarrWantedAlbum[];
  total: number;
}

export type AcquisitionQueueSource = 'beets' | 'lidarr';

export interface AcquisitionQueueHealth {
  imported: number;
  missing: number;
  not_imported: number;
  expected: number;
  percent: number;
  label: string;
  color: 'success' | 'warning' | 'error' | 'secondary' | 'info';
  release_mb_missing: boolean;
  track_mb_missing: number;
  track_mb_mismatch: number;
  duplicate_recording_ids: number;
  mb_repairable: number;
}

export interface AcquisitionQueueLocal {
  album_id: number;
  aldir: string;
  track_count: number;
  expected_track_count: number;
  albumtype: string;
  mb_albumid: string;
  health: AcquisitionQueueHealth;
}

export interface AcquisitionQueueActions {
  can_download: boolean;
  can_ytdlp: boolean;
  can_import_disk: boolean;
  can_search_lidarr: boolean;
  recommended: 'import_disk' | 'slskd' | 'review';
}

export interface AcquisitionQueueItem {
  key: string;
  sort_key: string;
  artist: string;
  album: string;
  year: string;
  mbid: string;
  issue: string;
  local: AcquisitionQueueLocal | null;
  wanted: LidarrWantedAlbum | null;
  sources: AcquisitionQueueSource[];
  actions: AcquisitionQueueActions;
}

export interface AcquisitionQueueResponse extends ApiOkResponse {
  items: AcquisitionQueueItem[];
  total: number;
  counts: {
    beets: number;
    lidarr: number;
    merged: number;
    unmonitored: number;
  };
  library_error?: string;
  wanted_error?: string;
  library_version?: number;
}

export type DownloadMethod = 'slskd' | 'spotiflac' | 'ytdlp' | 'soundcloud';

export interface AcquisitionDownloadAllPayload {
  keys?: string[];
  method?: DownloadMethod;
  include_unmonitored?: boolean;
  try_ytdlp_fallback?: boolean;
  try_source_fallback?: boolean;
  prioritize?: 'queue' | 'beets_first';
  limit?: number;
}

export interface AcquisitionDownloadAllStartResponse extends JobStartResponse {
  count: number;
  skipped: number;
}

export interface AcquisitionDownloadAllFailure {
  key: string;
  artist: string;
  album: string;
  error: string;
}

export interface AcquisitionDownloadAllResult {
  total?: number;
  success?: number;
  failed?: number;
  skipped?: number;
  failures?: AcquisitionDownloadAllFailure[];
  ytdlp_fallback_disabled?: boolean;
  aborted?: boolean;
}

export interface AcquisitionDownloadAllLastJob {
  job_id?: string;
  label?: string;
  status: JobStatus;
  created_at?: number;
  finished_at?: number;
  log?: string[];
  log_lines?: number;
  result?: AcquisitionDownloadAllResult;
  error?: string;
}

export interface AcquisitionDownloadAllActiveResponse extends ApiOkResponse {
  active: boolean;
  job: JobResponse | null;
  last_job?: AcquisitionDownloadAllLastJob | null;
}

export interface YtdlpCookieRejection {
  cookie_file?: string;
  size?: number;
  mtime_ns?: number;
  rejected_at?: number;
  reason?: string;
}

export interface YtdlpAuthSmoke {
  key: string;
  label: string;
  mode: string;
  ok: boolean;
  cached?: boolean;
  rejected?: boolean;
  checked_at?: number;
  cookie_count?: number;
  smoke_client?: string;
  smoke_title?: string;
  smoke_url?: string;
  smoke_warning?: string;
  error?: string;
  rejection?: YtdlpCookieRejection | null;
}

export interface YtdlpInstallStatus {
  python?: string;
  pip_cmd?: string[];
  package?: string;
  fallback_package?: string;
  pre?: boolean;
  version?: string;
  ejs_version?: string;
  curl_cffi_version?: string;
}

export interface BinaryStatus {
  available: boolean;
  path: string;
  version?: string;
  returncode?: number;
  error?: string;
}

export interface YtdlpNetrcStatus {
  enabled: boolean;
  file?: string;
  machines?: string[];
}

export interface SpotiflacStatus {
  available: boolean;
  enabled: boolean;
  command?: string;
  auto_install?: boolean;
  package?: string;
  version?: string;
  input_sources?: string[];
  services?: string[];
}

export interface LidarrArtistAlbum {
  lidarr_id: number;
  title: string;
  year: string;
  album_type: string;
  monitored: boolean;
  track_file_count: number;
  total_track_count: number;
  percent: number;
  mb_albumid: string;
  cover_url: string;
  disk_path: string;
  aldir: string;
}

export interface LidarrArtistAlbumsResponse extends ApiOkResponse {
  albums: LidarrArtistAlbum[];
  found?: boolean;
  lidarr_artist?: string;
  artist_path?: string;
}

export interface LidarrCommandResponse extends ApiOkResponse {
  command_id?: number;
  status?: string;
}

export interface DownloadAlbumPayload {
  artist: string;
  album: string;
  year?: string;
  track_count?: number;
  mb_albumid?: string;
  existing_album_id?: number;
  album_id?: number;
  method?: DownloadMethod;
  auto_import?: boolean;
  albumartist?: string;
  fallback_method?: Exclude<DownloadMethod, 'slskd'>;
  try_ytdlp_fallback?: boolean;
  try_source_fallback?: boolean;
  ytdlp_fallback?: boolean;
}

export interface YtdlpStatusResponse extends ApiOkResponse {
  ready: boolean;
  enabled: boolean;
  cookie_file: string;
  cookies_from_browser?: string;
  browser_cookies_enabled?: boolean;
  cookie_auth_mode?: string;
  cookie_auth_label?: string;
  cookie_auth_candidates?: string[];
  cookie_auth_rejections?: Array<{
    key: string;
    label?: string;
    rejection?: YtdlpCookieRejection | null;
  }>;
  cookie_auth_smoke?: YtdlpAuthSmoke[];
  cookie_candidates: string[];
  install?: YtdlpInstallStatus;
  ffmpeg?: {
    ffmpeg?: BinaryStatus;
    ffprobe?: BinaryStatus;
  };
  netrc?: YtdlpNetrcStatus;
  spotiflac?: SpotiflacStatus;
  youtube?: Record<string, unknown>;
  js_runtime?: Record<string, unknown>;
  js_runtimes?: string[];
  remote_components?: string[];
  cookie_rejected?: boolean;
  cookie_rejection?: YtdlpCookieRejection | null;
  message: string;
}

// ── Genre stats ───────────────────────────────────────────────────────────────

export interface GenreStatsAlbum {
  id: number;
  album: string;
  albumartist: string;
  year: number | string;
}

export interface GenreStatsResponse extends ApiOkResponse {
  total: number;
  with_genre: number;
  without_genre: number;
  missing: GenreStatsAlbum[];
}

// ── Plex ─────────────────────────────────────────────────────────────────────

export interface PlexStatusResponse extends ApiOkResponse {
  configured: boolean;
  connected: boolean;
  url: string;
  section_preference: string;
  machine_id: string;
  section_key: number | null;
  section_title: string | null;
  error: string | null;
}

// ── Plugin controls ──────────────────────────────────────────────────────────

export type PluginCommandName = 'ytimport' | 'ytupdate' | 'wlg' | 'aisauce';

export interface PluginCommandStatus {
  installed: boolean;
  enabled: boolean;
  runnable: boolean;
}

export interface PluginStatusResponse extends ApiOkResponse {
  installed: Partial<Record<PluginCommandName, boolean>>;
  enabled?: Partial<Record<PluginCommandName, boolean>>;
  status?: Partial<Record<PluginCommandName, PluginCommandStatus>>;
}

export interface PluginInstallLogResponse extends ApiOkResponse {
  log: string[];
}

export interface AiSuggestion {
  candidate_index?: number;
  candidate_type?: 'recording' | 'release' | 'release_group';
  mb_trackid?: string;
  match_method?: string;
  candidate_evidence?: ReviewRecordingCandidate;
  selected_recording_candidate?: ReviewRecordingCandidate;
  recording_candidates?: ReviewRecordingCandidate[];
  selected_release?: RecordingLinkedRelease;
  linked_releases?: RecordingLinkedRelease[];
  conflicts?: string[];
  recommended_action?: string;
  requires_confirmation?: boolean;
  safety_result?: string;
  confidence_score?: number | null;
  missing_id_type?: string;
  mb_albumid?: string;
  representative_mb_albumid?: string;
  mb_releasegroupid?: string;
  mb_releasegroupurl?: string;
  release_group_primary_type?: string;
  album?: string;
  albumartist?: string;
  year?: number | null;
  label?: string;
  country?: string;
  confidence?: 'high' | 'medium' | 'low';
  reason?: string;
  mb_valid?: boolean;
  mb_url?: string;
  preflight?: ReviewPreflight | null;
  preflight_note?: string;
  track_mapping?: ImportWithIdPayload['track_mapping'];
  track_match_count?: number | null;
  mb_track_count?: number | null;
  local_track_count?: number | null;
  identity_validated?: boolean;
  candidate_identity_error?: string;
  representative_release_group_id?: string;
  rejected_representative_release_id?: string;
  review_evidence?: ReviewEvidence;
  origin_type?: ReviewOriginType;
  origin_label?: string;
  origin_id?: string;
  source_playlist_id?: string;
  source_playlist_name?: string;
  source_batch_id?: string;
  source_folder?: string;
  created_by_workflow?: string;
  /** Fallback Discogs match, set only when MusicBrainz search found nothing. */
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
  discogs_candidate?: {
    discogs_id?: number | string;
    discogs_url?: string;
    artist?: string;
    album?: string;
    year?: string;
    country?: string;
    format?: string;
    genre?: string;
  } | null;
}

export interface AiSuggestResponse extends ApiOkResponse {
  suggestion?: AiSuggestion;
  /** /api/items/<iid>/ai-suggest uses this key (plural) instead of
   * `suggestion` -- a pre-existing backend inconsistency, not replicated
   * here on purpose, just typed as-is so item-level callers can read it. */
  suggestions?: AiSuggestion & { mb_trackid?: string };
  mb_candidates?: ReviewCandidate[];
  selected_candidate?: ReviewCandidate | ReviewRecordingCandidate;
  selected_match?: ImportReviewSelectedMatch;
  recording_candidates?: ReviewRecordingCandidate[];
  acoustid_candidates?: unknown[];
  evidence?: ReviewEvidence;
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
}

export interface ImportReviewSelectedMatch {
  release_group_id: string;
  representative_release_id: string;
  artist: string;
  album: string;
  year: string;
  track_match_count: number | null;
  total_tracks: number | null;
  local_track_count: number | null;
  track_mapping: ImportWithIdPayload['track_mapping'];
  preflight_status: 'passed' | 'failed' | 'stale' | 'not_run';
  preflight_reason: string;
  is_release_group_usable: boolean;
  is_importable: boolean;
  is_partial_import: boolean;
  confidence_score: number | null;
  confidence_level: 'high' | 'medium' | 'low' | 'blocked' | 'not_importable';
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
}

export interface ImportReviewManualIdPayload {
  review_item_id: string;
  musicbrainz_id: string;
  target_kind?: string;
  path?: string;
  album_id?: number;
  existing_album_id?: number;
  item_id?: number;
}

export interface ImportReviewManualIdResponse extends ApiOkResponse {
  entity_type: 'release' | 'release-group' | 'recording' | 'unknown';
  input_mbid?: string;
  release_group_id?: string;
  representative_release_id?: string;
  mb_trackid?: string;
  musicbrainz_url?: string;
  selected_match?: ImportReviewSelectedMatch;
  selected_recording_candidate?: ReviewRecordingCandidate;
  recording_candidates?: ReviewRecordingCandidate[];
  release_group_candidates?: Array<{
    mb_albumid: string;
    title: string;
    date: string;
    country: string;
    status: string;
    track_count: number;
  }>;
  status_lines?: string[];
  message?: string;
  error?: string;
}

export interface ReimportDiskPayload {
  aldir: string;
  mb_albumid: string;
  existing_album_id?: number;
  albumartist?: string;
}

export interface ImportWithIdPayload {
  path: string;
  mb_albumid: string;
  mb_releasegroupid?: string;
  existing_album_id?: number;
  selected_source_files?: string[];
  track_mapping?: Array<{
    num: number;
    local_title: string;
    mb_title: string;
    mb_trackid: string;
    status: string;
    source_path?: string;
  }>;
  ai_suggestion?: AiSuggestion;
}

export interface AutoEnqueueImportPayload extends ImportWithIdPayload {
  review_item_id?: string;
  confidence_score?: number | null;
  selected_match?: Record<string, unknown>;
  trigger_plex?: boolean;
}

export interface AutoEnqueueImportResponse extends JobStartResponse {
  queued: boolean;
  handled?: boolean;
  existing_job?: boolean;
  reconciled?: boolean;
  retryable?: boolean;
  status?: string;
  job_status?: string;
  pending_review_exists?: boolean;
  note?: string;
  eligibility?: {
    eligible: boolean;
    selected_track_count?: number;
    unmatched_file_count?: number;
    missing_release_track_count?: number;
    blocking_reasons?: string[];
  };
}

export interface ImportReviewRevalidatePayload {
  review_item_id?: string;
  review_item_ids?: string[];
  auto_enqueue?: boolean;
  limit?: number;
}

export interface ImportReviewRevalidatedMatch {
  release_group_id: string;
  representative_release_id: string;
  artist: string;
  album: string;
  year: string;
  track_match_count: number | null;
  total_tracks: number | null;
  local_track_count: number | null;
  track_mapping: ImportWithIdPayload['track_mapping'];
  preflight_status: 'passed' | 'failed' | 'stale' | 'not_run';
  preflight_reason: string;
  is_release_group_usable: boolean;
  is_importable: boolean;
  is_partial_import: boolean;
  confidence_score: number | null;
  confidence_level: 'high' | 'medium' | 'low' | 'blocked' | 'not_importable';
  auto_fix_eligible: boolean;
  auto_fix_requires_review: boolean;
  auto_fix_reason: string;
  missing_track_count: number;
  match_count: number | null;
  preflight_ok: boolean | null;
  source: 'candidate';
}

export interface ImportReviewRevalidateItem {
  ok: boolean;
  review_item_id: string;
  path: string;
  selected_match?: ImportReviewRevalidatedMatch;
  eligibility?: AutoEnqueueImportResponse['eligibility'];
  queued?: boolean;
  job_id?: string;
  existing_job?: boolean;
  error?: string;
}

export interface ImportReviewRevalidateResponse extends ApiOkResponse {
  reviewed_count: number;
  updated_count: number;
  queued_count: number;
  failed_count: number;
  items: ImportReviewRevalidateItem[];
}
export interface ImportTargetPreviewTrack {
  track: number;
  status: string;
  source_path: string;
  target_filename: string;
  target_path: string;
  target_exists: boolean;
  target_conflict: boolean;
  already_imported: boolean;
  same_as_source: boolean;
  unresolved_placeholder: boolean;
  uses_release_id_in_path: boolean;
}

export interface ImportTargetPreviewResponse extends ApiOkResponse {
  safe: boolean;
  status: 'safe' | 'blocked' | 'existing_folder';
  next_action?: 'import' | 'verify_or_cleanup_unmatched' | 'resolve_conflict' | 'blocked';
  cleanup_required_count?: number;
  blocked_reasons: string[];
  warnings: string[];
  path_template: string;
  release_group_id: string;
  representative_release_id: string;
  artist_folder: string;
  album_folder: string;
  album_path: string;
  album_folder_uses_release_group_id: boolean;
  target_folder_exists: boolean;
  target_folder_conflict: boolean;
  existing_folder_reuse: boolean;
  already_imported_count: number;
  conflict_count: number;
  real_conflict_count?: number;
  placeholder_warning_count: number;
  release_id_path_warning_count: number;
  source_file_count: number;
  track_count: number;
  tracks_to_import_count?: number;
  unmatched_extra_count?: number;
  rejected_cleanup_count?: number;
  missing_album_track_count?: number;
  tracks: ImportTargetPreviewTrack[];
}

export interface ImportTargetPreviewPayload {
  path: string;
  release_group_id: string;
  representative_release_id?: string;
  artist?: string;
  album?: string;
  year?: string | number;
  existing_album_id?: number;
  track_mapping: Array<{
    num: number;
    local_title: string;
    mb_title: string;
    mb_trackid: string;
    status: string;
    source_path?: string;
  }>;
  selected_source_files?: string[];
  identity_validated?: boolean;
  candidate_identity_error?: string;
}

export interface DeleteReviewFolderResponse extends ApiOkResponse {
  path: string;
  files_removed: number;
  audio_files_removed: number;
  bytes_removed: number;
  db_rows_removed?: number;
  library_folder_deleted?: boolean;
  pending_review_removed: boolean;
  log?: string[];
}

export type ImportReviewFileCleanupAction =
  | 'quarantine_rejected'
  | 'quarantine_duplicate'
  | 'delete_rejected'
  | 'delete_duplicate';

export interface ImportReviewFileCleanupPayload {
  path: string;
  review_item_id?: string;
  files: string[];
  action: ImportReviewFileCleanupAction;
  allow_delete?: boolean;
}

export interface ImportReviewFileCleanupResponse extends ApiOkResponse {
  action: ImportReviewFileCleanupAction;
  quarantined: Array<{ source: string; quarantined: string }>;
  deleted: string[];
  skipped: Array<{ path: string; reason: string }>;
  quarantined_count: number;
  deleted_count: number;
  skipped_count: number;
  remaining_audio_count: number;
  pending_review_removed: boolean;
  pending_review_updated: boolean;
  log?: string[];
}
export interface FolderStatsResponse extends ApiOkResponse {
  path: string;
  exists: boolean;
  audio_count: number;
  art_count: number;
  other_count: number;
  total_count: number;
}

// ── Config / health ───────────────────────────────────────────────────────────

export interface HealthChecks {
  library_path: boolean;
  beet_bin: boolean;
  music_root: boolean;
  lidarr_key: boolean;
  discogs_token: boolean;
  slskd_key: boolean;
  openai_key: boolean;
}

export interface HealthResponse extends ApiOkResponse {
  checks: HealthChecks;
}

export interface StatsResponse {
  tracks: number;
  albums: number;
  artists: number;
}

export interface ConfigFileResponse extends ApiOkResponse {
  content: string;
  has_backup: boolean;
  backup_ts: number | null;
}

export interface ConfigSaveResponse extends ApiOkResponse {
  backed_up?: boolean;
}
export type MusicFormatLayout = 'mono' | 'stereo' | '2.1' | '5.1' | '7.1' | 'atmos';
export type MusicFormatKey = 'flac' | 'mp3' | 'aac' | 'alac' | 'opus' | 'wav' | 'eac3' | 'truehd';

export interface MusicFormatPreferences {
  allowed_layouts: Record<MusicFormatLayout, boolean>;
  allow_atmos: boolean;
  custom_max_channels: number | null;
  preferred_formats: MusicFormatKey[];
  rejected_download_handling: 'quarantine' | 'delete';
  replacement_fallback: {
    keep_current: boolean;
    mark_needs_replacement: boolean;
    queue_retry: boolean;
    try_lower_ranked: boolean;
    try_alternate_source: boolean;
    allow_temporary_exception: boolean;
  };
}

export interface MusicFormatPreferencesResponse extends ApiOkResponse {
  preferences: MusicFormatPreferences;
}

export interface MusicFormatReplacementTrack {
  item_id?: number;
  path: string;
  artist?: string;
  album?: string;
  title?: string;
  status: string;
  replacement_status?: string;
  reason?: string;
  attempt_count?: number;
  failure_stage?: string;
  failure_reason?: string;
  retryable?: boolean;
  next_retry_at?: number;
  replacement_identity_key?: string;
  mb_trackid?: string;
  mb_releasegroupid?: string;
  queued_retry?: boolean;
  updated_at?: number;
}

export interface MusicFormatReplacementStatusResponse extends ApiOkResponse {
  tracks: MusicFormatReplacementTrack[];
  updated_at?: number;
}


// ── Import Intake ─────────────────────────────────────────────────────────────

export interface PreflightFolder {
  path: string;
  name: string;
  audio_files: number;
  already_in_library: boolean;
}

export interface PreflightResponse extends ApiOkResponse {
  path: string;
  audio_files: number;
  audio_folders: number;
  already_in_library_folders: number;
  pending_review: number;
  unsupported_files: number;
  empty_dirs: number;
  artist_folder_groups: number;
  folders: PreflightFolder[];
}

// ── Import history ────────────────────────────────────────────────────────────

export interface RecentImport {
  matched_at?: number | string;
  imported_at?: number | string;
  original_path?: string;
  original_folder?: string;
  aldir?: string;
  artist: string;
  album: string;
  year: string | number;
  mb_albumid: string;
  mb_url?: string;
  confidence?: string;
  reason?: string;
  tracks?: number;
}

export interface RecentImportsResponse {
  imports: RecentImport[];
}

export interface AiMatchHistoryResponse extends ApiOkResponse {
  history: RecentImport[];
}

// ── Artist alias review ───────────────────────────────────────────────────────

export interface ArtistIdGroupName {
  name: string;
  album_count: number;
  track_count: number;
  credits: Array<{ name: string; count: number }>;
}

export interface ArtistIdGroup {
  mb_artistid: string;
  reject_key?: string;
  canonical: string;
  album_count: number;
  names: ArtistIdGroupName[];
}

export interface ArtistIdGroupsResponse extends ApiOkResponse {
  groups: ArtistIdGroup[];
  hidden_count?: number;
}

// ── Artist discography (MusicBrainz) ─────────────────────────────────────────

export interface DiscographyAlbum {
  album: string;
  year: string;
  type: string;
  subtypes: string[];
  mbid: string;
  mb_url: string;
  on_disk: boolean;
  match_reason: string;
}

export interface DiscographyResponse extends ApiOkResponse {
  mb_artist?: string;
  mbid?: string;
  have?: DiscographyAlbum[];
  missing?: DiscographyAlbum[];
  total?: number;
  status?: 'running';
  job_id?: string;
}

export interface AlbumMbCompletenessTrack {
  disc: number;
  track: number;
  title: string;
  mb_trackid: string;
  duration_ms?: number;
  ok: boolean;
  missing: boolean;
  item?: Record<string, unknown>;
}

export interface AlbumMbCompletenessExtraItem {
  id: number;
  title: string;
  track: number;
  disc: number;
  path: string;
  mb_trackid: string;
  length?: number;
  score?: number;
}

export interface AlbumMbDuplicateRecordingItem {
  id: number;
  album_id?: number;
  album?: string;
  albumartist?: string;
  title: string;
  track: number;
  disc: number;
  path: string;
  filename?: string;
  mb_trackid: string;
  mb_albumid?: string;
  length?: number;
  in_selected_album?: boolean;
  selected_release?: boolean;
  matched_to_selected_release?: boolean;
}

export interface AlbumMbDuplicateRecordingExpectedTrack {
  disc: number;
  track: number;
  title: string;
  mb_trackid: string;
}

export interface AlbumMbDuplicateRecordingGroup {
  mb_trackid: string;
  count: number;
  expected_count: number;
  duplicate_count: number;
  items: AlbumMbDuplicateRecordingItem[];
  expected_tracks: AlbumMbDuplicateRecordingExpectedTrack[];
}

export interface AlbumMbCompletenessResponse extends ApiOkResponse {
  album_id: number;
  album: string;
  artist: string;
  year: number;
  mb_albumid: string;
  mb_url: string;
  expected_count: number;
  actual_count: number;
  extra_count: number;
  extra_track_count?: number;
  in_library: number;
  missing_count: number;
  mb_missing_count?: number;
  mb_trackid_missing_count?: number;
  mb_trackid_mismatch_count?: number;
  mb_duplicate_recording_id_count?: number;
  duplicate_recording_groups?: AlbumMbDuplicateRecordingGroup[];
  mb_repairable_count?: number;
  mb_health_source?: string;
  percent: number;
  tracks: AlbumMbCompletenessTrack[];
  missing: AlbumMbCompletenessTrack[];
  extra_items?: AlbumMbCompletenessExtraItem[];
  release_title: string;
}

export interface AlbumDuplicateResolverTrack {
  disc: number;
  track: number;
  title: string;
  mb_trackid: string;
  score?: number;
}

export interface AlbumDuplicateResolverItem extends AlbumMbDuplicateRecordingItem {
  default_action?: 'skip' | 'delete' | 'retag';
  default_target?: AlbumDuplicateResolverTrack | null;
  retag_candidates?: AlbumDuplicateResolverTrack[];
}

export interface AlbumDuplicateResolverGroup {
  key: string;
  mb_trackid: string;
  count: number;
  duplicate_count: number;
  expected_count: number;
  expected_tracks: AlbumDuplicateResolverTrack[];
  keep_items: AlbumDuplicateResolverItem[];
  action_items: AlbumDuplicateResolverItem[];
}

export interface AlbumDuplicateResolverPlan extends ApiOkResponse {
  album_id: number;
  mb_albumid: string;
  album: string;
  artist: string;
  expected_count: number;
  actual_count: number;
  missing_count: number;
  missing_tracks: AlbumDuplicateResolverTrack[];
  groups: AlbumDuplicateResolverGroup[];
  group_count: number;
  action_item_count: number;
  message?: string;
}

export interface AlbumDuplicateResolverAction {
  action: 'skip' | 'delete' | 'retag';
  item_id: number;
  target_mb_trackid?: string;
}

export interface MbidStatusExample {
  album_id: number;
  item_id?: number;
  artist: string;
  album: string;
  title?: string;
  tracks?: number;
  year?: number;
  track?: number;
  disc?: number;
  path?: string;
  album_mb_albumid?: string;
  item_mb_albumid?: string;
}

export interface MbidStatusResponse extends ApiOkResponse {
  root: string;
  total_albums: number;
  missing_album_mb: number;
  total_tracks: number;
  missing_track_mb: number;
  item_release_gap_rows?: number;
  albums_with_item_release_gaps?: number;
  track_recording_gap_rows?: number;
  albums_with_track_recording_gaps?: number;
  inferred_album_mbid_rows?: number;
  template_token_rows?: number;
  albums_with_template_tokens?: number;
  examples: MbidStatusExample[];
  release_gap_examples?: MbidStatusExample[];
  track_gap_examples?: MbidStatusExample[];
  template_token_examples?: MbidStatusExample[];
}

export interface MbidUnresolvedAlbum {
  album_id: number;
  label: string;
  reason: string;
}

export interface MbidStickingRepairResult {
  albums_checked?: number;
  albums_changed?: number;
  release_item_rows?: number;
  track_rows?: number;
  inferred_album_rows?: number;
  unlinked_track_gap_albums?: number;
  resolved_album_rows?: number;
  unresolved_albums?: MbidUnresolvedAlbum[];
  unresolved_count?: number;
  skipped_already_fixed?: number;
  failed_count?: number;
  dry_run?: boolean;
}

export interface RgidGroupAlbum {
  album_id: number;
  albumartist: string;
  album: string;
  year: number;
  track_count: number;
  mb_albumid: string;
  aldir: string;
}

export interface RgidCandidateRelease {
  mb_albumid: string;
  title: string;
  date: string;
  country: string;
  status: string;
  track_count: number;
}

export interface RgidResolution {
  decision: string;
  reason: string;
  album_ids: number[];
  updated_at: number;
}

export interface RgidGroupDetailResponse {
  ok: boolean;
  error?: string;
  mb_releasegroupid: string;
  albums: RgidGroupAlbum[];
  merge_safe?: boolean;
  merge_target_album_id?: number;
  merge_source_album_ids?: number[];
  merge_reason?: string;
  merge_blockers?: string[];
  resolution?: RgidResolution | null;
  candidate_releases: RgidCandidateRelease[];
}

export interface LeakedDbPathRow {
  item_id: number;
  album_id: number;
  db_path: string;
  abs_path: string;
  resolved_path: string | null;
  file_exists_at_db_path: boolean;
  file_exists_at_resolved: boolean;
  safe: boolean;
  skip_reason: string;
  error?: string;
}

export interface LeakedDbPathsResponse extends ApiOkResponse {
  total: number;
  safe_count: number;
  unsafe_count: number;
  rows: LeakedDbPathRow[];
}

export interface LeakedDbPathsFixResult {
  fixed: number;
  skipped: number;
  errors: number;
  dry_run: boolean;
  total_scanned: number;
  safe_found: number;
  rows: LeakedDbPathRow[];
}

export interface FolderPlaceholderRow {
  folder: string;
  artist: string;
  name: string;
  clean_name: string | null;
  proposed_folder: string | null;
  placeholder_type: 'literal_placeholder' | 'template_token';
  placeholder_desc: string;
  is_empty: boolean;
  file_count: number;
  audio_count: number;
  db_album_ids: number[];
  db_item_count: number;
  target_exists: boolean;
  missing_rgid?: boolean;
  safe: boolean;
  skip_reason: string;
}

export interface FolderPlaceholderScanResult extends ApiOkResponse {
  total: number;
  safe_count: number;
  unsafe_count: number;
  rows: FolderPlaceholderRow[];
}

export interface FolderPlaceholderReview {
  ok?: boolean;
  source_path: string;
  target_path?: string | null;
  proposed_path: string | null;
  source_exists: boolean;
  target_exists: boolean;
  source_empty?: boolean;
  source_is_empty: boolean;
  target_is_empty: boolean;
  source_file_count: number;
  source_audio_count: number;
  target_file_count: number;
  target_audio_count: number;
  source_only_files: string[];
  target_only_files: string[];
  matching_files: string[];
  conflicting_files: string[];
  source_db_items: number;
  source_db_album_ids: number[];
  source_known_rgid: string | null;
  source_has_placeholder: boolean;
  source_has_unresolved_token: boolean;
  source_folder_rgid: string | null;
  target_folder_rgid: string | null;
  safety_status?: string;
  safe?: boolean;
  blocking_reasons?: string[];
  action: string;
  suggested_actions: string[];
  reasons_blocked: string[];
  preview_token?: string;
  error?: string | null;
}

export interface FolderPlaceholderMergeConflict {
  filename: string;
  source_path: string;
  target_path: string;
  source_size: number;
  target_size: number;
  same_size: boolean;
}

export interface FolderPlaceholderMergePreview {
  ok?: boolean;
  source_path: string;
  target_path: string;
  moves: Array<{ from: string; to: string; source?: string; target?: string; relative_path?: string; size?: number }>;
  conflicts: FolderPlaceholderMergeConflict[];
  matching_files?: Array<{ source: string; target: string; relative_path: string; size: number }>;
  db_items_in_source: number;
  db_path_updates_needed: Array<{ item_id: number; old_path: string; new_path: string }> | boolean;
  safe: boolean;
  blocking_reasons: string[];
  source_will_be_empty_after_move: boolean;
  folders_to_remove_if_empty: string[];
  preview_token?: string;
  error?: string;
}

export interface FolderPlaceholderApplyResult {
  ok: boolean;
  action?: string;
  removed?: string;
  renamed_from?: string;
  renamed_to?: string;
  moved?: Array<{ source: string; target: string }>;
  removed_folders?: string[];
  changed_count?: number;
  error?: string;
}

export interface FolderPlaceholderApplySafeRenamesResult {
  renamed: number;
  skipped: number;
  failed: number;
  total: number;
  summary: string;
}




