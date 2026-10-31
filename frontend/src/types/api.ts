// ── Jobs ─────────────────────────────────────────────────────────────────────

export type JobStatus = 'running' | 'success' | 'failed' | 'cancelled' | 'recovery_required' | 'killed' | 'missing';

export interface JobMetadata {
  [key: string]: string | number | boolean | undefined;
  type?: string;
  aldir?: string;
  path?: string;
  mb_albumid?: string;
  mb_releasegroupid?: string;
  existing_album_id?: number;
  artist?: string;
  album?: string;
  albumartist?: string;
  year?: string | number;
  track_count?: number;
  method?: string;
}

export interface JobState {
  [key: string]: unknown;
  job_id?: string;
  job_name?: string;
  category?: string;
  status?: JobStatus | string;
  current_task?: string | null;
  current_item?: string | null;
  current_path?: string | null;
  current_file?: string | null;
  scan_scope?: string | null;
  scan_path?: string | null;
  scanned_count?: number | null;
  total_count?: number | null;
  remaining_count?: number | null;
  found_count?: number | null;
  progress_percent?: number | null;
  affected_count?: number | null;
  placeholder_count?: number | null;
  target_exists_count?: number | null;
  db_tracked_count?: number | null;
  empty_folder_count?: number | null;
  source_missing_count?: number | null;
  safe_count?: number | null;
  needs_review_count?: number | null;
  skipped_count?: number | null;
  changed_count?: number | null;
  error_count?: number | null;
  current_result?: string | null;
  duplicate_type?: string | null;
  issue_reason?: string | null;
  final_summary?: Record<string, unknown> | null;
  error_summary?: string | null;
  started_at?: number | string | null;
  finished_at?: number | string | null;
  duration_seconds?: number | null;
}

export interface JobResultSummary {
  type?: string;
  count?: number;
  key_count?: number;
  keys?: string[];
  scalars?: Record<string, unknown>;
  sizes?: Record<string, number>;
  value?: string;
}

export interface Job {
  job_id: string;
  label: string;
  status: JobStatus;
  log?: string[];
  log_lines?: number;
  returncode?: number | null;
  created_at: number;
  started_at?: number;
  finished_at?: number;
  metadata?: JobMetadata;
  result?: Record<string, unknown>;
  result_summary?: JobResultSummary;
  state?: JobState;
  /** Set when a restart stopped this job; it is never re-run (status
   * recovery_required for mutating jobs, failed for read-only ones). */
  interrupted?: boolean;
  recovery?: { interrupted_at?: number; last_heartbeat_at?: number; checkpoint?: unknown } | null;
}

export interface JobListResponse {
  ok?: boolean;
  jobs: Job[];
  count?: number;
  duration_ms?: number;
}

export interface JobLogFeedItem {
  job_id: string;
  label: string;
  status: JobStatus;
  level: 'debug' | 'info' | 'warn' | 'error' | string;
  line: number;
  message: string;
  created_at?: number;
  started_at?: number;
  finished_at?: number;
}

export interface JobLogFeedResponse {
  ok?: boolean;
  entries: JobLogFeedItem[];
  total: number;
}

// ── Library ───────────────────────────────────────────────────────────────────

export interface LibraryTrack {
  id: number;
  album_id?: number;
  title: string;
  track: number;
  disc: number;
  tracktotal?: number;
  path: string;
  mb_trackid: string;
  length?: number;
  ok: boolean;
  missing: boolean;
  imported?: boolean;
  disk_only?: boolean;
  other_album_id?: number;
  status?: 'imported' | 'missing_file' | 'not_imported' | 'other_album' | 'expected_missing' | string;
}

export interface LibraryAlbum {
  album_id: number;
  album: string;
  albumartist: string;
  albumartist_credit?: string;
  albumartists?: string[];
  albumartists_credit?: string[];
  albumtype?: string;
  albumtypes?: string[];
  year: number;
  mb_albumid: string;
  mb_albumartistid?: string;
  mb_albumartistids?: string[];
  mb_releasegroupid: string;
  aldir: string;
  artpath?: string;
  disk_art?: string;
  tracks?: LibraryTrack[];
  tracks_deferred?: boolean;
  track_count: number;
  expected_track_count?: number;
  mb_missing_count?: number;
  extra_track_count?: number;
  mb_trackid_missing_count?: number;
  mb_trackid_mismatch_count?: number;
  mb_duplicate_recording_id_count?: number;
  mb_repairable_count?: number;
  mb_health_source?: string;
  not_imported: number;
  not_imported_is_extra?: boolean;
  pending_review?: boolean;
  missing: number;
  image_url?: string;
}

export interface LibraryArtist {
  name: string;
  albums: LibraryAlbum[];
  imported?: number;
  missing?: number;
  not_imported?: number;
  total?: number;
  empty_artist_folder?: boolean;
  path?: string;
  image_url?: string;
  artist_image_url?: string;
  image_source?: string;
}

export interface LibraryResponse {
  ok: boolean;
  artists: LibraryArtist[];
  stats: {
    artists: number;
    /** Count of unique Beets album_ids -- not disk-folder cards. A real album
     * split across multiple disk folders is counted once, not once per folder. */
    albums: number;
    /** Imported tracks only: album tracks plus singletons. Excludes disk-only content. */
    tracks: number;
    /** Imported tracks with no Beets album row (singletons). Already included in `tracks`. */
    singleton_tracks?: number;
    /** Disk folders with no Beets album row and at least one never-imported file. */
    disk_only_albums?: number;
    /** Never-imported track files inside disk_only_albums. Not included in `tracks`. */
    disk_only_tracks?: number;
  };
  library_version: number;
  tracks_included?: boolean;
  /** True once the payload includes disk state: missing-on-disk flags plus
   * disk-only/not-imported items. False when only the fast engine-only tree is
   * served (cold cache) while a background disk reconciliation runs. */
  reconciled?: boolean;
  /** True when a background disk reconciliation was just scheduled; the client
   * should keep polling /api/library until `reconciled` becomes true. */
  reconciling?: boolean;
}
