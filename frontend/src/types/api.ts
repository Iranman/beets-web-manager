// ── Jobs ─────────────────────────────────────────────────────────────────────

export type JobStatus = 'running' | 'success' | 'failed' | 'cancelled' | 'killed' | 'missing';

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
  scan_scope?: string | null;
  scan_path?: string | null;
  scanned_count?: number | null;
  total_count?: number | null;
  remaining_count?: number | null;
  found_count?: number | null;
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

// ── Generic ───────────────────────────────────────────────────────────────────

export interface ApiOkResponse {
  ok: boolean;
  error?: string;
  job_id?: string;
}

// ── Import Review ─────────────────────────────────────────────────────────────

export type ReviewItemType = 'pending_ai' | 'library_no_mb' | 'skipped';

export interface ReviewEvidenceCandidate {
  mb_albumid: string;
  album: string;
  artist: string;
  year: string;
  date?: string;
  country: string;
  label?: string;
  labels?: string[];
  catalog_numbers?: string[];
  barcode?: string;
  formats?: string[];
  mediums?: Array<{ position?: number; format?: string; tracks?: number }>;
  format_summary?: string;
  cover_art?: boolean | null;
  front_art?: boolean | null;
  cover_art_count?: number;
  edition_count?: number;
  edition_alternates?: ReviewEvidenceCandidate[];
  match_total: number;
  acoustid_hits: number;
}

export interface ReviewEvidence {
  top_candidates: ReviewEvidenceCandidate[];
  preflight: {
    ok: boolean;
    matches: number;
    expected: number;
    audio_count?: number;
    min_required?: number;
    match_ratio?: number;
    source_match_ratio?: number;
    artist_ok?: boolean;
    acoustid_mismatch?: boolean;
    error: string;
  } | null;
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
  type: ReviewItemType;
  status: string;
  title: string;
  artist: string;
  album: string;
  year: number;
  path: string;
  folder: string;
  folder_name: string;
  confidence: string;
  reason: string;
  mb_albumid: string;
  mb_url: string;
  mb_valid: boolean;
  existing_album_ids: number[];
  existing_album_id: number;
  tracks: number;
  sort_ts: number;
  evidence: ReviewEvidence;
  album_id?: number;
  first_item_id?: number;
}

export interface ReviewQueueResponse {
  ok: boolean;
  items: ReviewItem[];
  total: number;
  counts: Record<string, number>;
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
  stats: { artists: number; albums: number; tracks: number };
  library_version: number;
  tracks_included?: boolean;
}

// ── AI suggest ────────────────────────────────────────────────────────────────

export interface AiSuggestion {
  mb_albumid: string;
  album: string;
  albumartist: string;
  year: number | null;
  label: string;
  country: string;
  confidence: 'high' | 'medium' | 'low';
  reason: string;
  mb_valid: boolean;
  mb_url?: string;
  candidate_index: number;
  preflight?: ReviewEvidence['preflight'];
  preflight_note?: string;
  review_evidence?: ReviewEvidence;
}

export interface AiSuggestResponse {
  ok: boolean;
  suggestion?: AiSuggestion;
  mb_candidates?: MbCandidate[];
  acoustid_candidates?: unknown[];
  acoustid_release_hits?: Record<string, number>;
  evidence?: ReviewEvidence;
  error?: string;
}

export interface MbCandidate {
  score: number;
  mb_albumid: string;
  mb_url?: string;
  album: string;
  artist: string;
  year: string;
  date?: string;
  tracks: number;
  country: string;
  formats: string[];
  mediums?: Array<{ position?: number; format?: string; tracks?: number }>;
  format_summary?: string;
  is_vinyl: boolean;
  status?: string;
  packaging?: string;
  label: string;
  labels?: string[];
  catalog_numbers?: string[];
  label_entries?: Array<{ label?: string; catalog_number?: string }>;
  barcode?: string;
  cover_art?: boolean | null;
  front_art?: boolean | null;
  cover_art_count?: number;
  edition_count?: number;
  edition_alternates?: MbCandidate[];
  is_current?: boolean;
  acoustid_release_hits?: number;
  _match_score?: { total: number; [k: string]: number };
}
