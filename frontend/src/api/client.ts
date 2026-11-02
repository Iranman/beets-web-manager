import { ENGINE_AUTH_MESSAGE, ENGINE_OFFLINE_MESSAGE, SESSION_EXPIRED_MESSAGE, classifyHttpFailure, fetchFailure, prepareRequestHeaders } from '../lib/api';
import type {
  UntrackedCandidatesResponse,
  UntrackedInventorySummaryResponse,
  UntrackedRecoveryPlanResponse,
  RecoverProbeResponse,
  RecoverRunsResponse,
  RecoverFetchResponse,
  RecoverRetryResponse,
  RecoverImportResponse,
  RecoverAuthorizationInput,
} from './types';
import type {
  AcquisitionDownloadAllActiveResponse,
  AcquisitionDownloadAllPayload,
  AcquisitionDownloadAllStartResponse,
  AcquisitionQueueParams,
  AcquisitionQueueRefreshResponse,
  AcquisitionQueueResponse,
  AiMatchHistoryResponse,
  AutoEnqueueImportPayload,
  ImportReviewManualIdPayload,
  ImportReviewManualIdResponse,
  ImportReviewRevalidatePayload,
  AutoEnqueueImportResponse,
  ImportReviewRevalidateResponse,
  AiSuggestResponse,
  ArtRepairReportResponse,
  AiBatchStatusResponse,
  ApiOkResponse,
  ArtistIdGroupsResponse,
  ConfigFileResponse,
  ConfigSaveResponse,
  DedupCleanupResponse,
  DedupScanState,
  DedupStartResponse,
  DeleteReviewFolderResponse,
  ImportReviewFileCleanupPayload,
  ImportReviewFileCleanupResponse,
  HealthResponse,
  ImportWithIdPayload,
  JobResponse,
  JobStartResponse,
  UnattendedCleanupStatus,
  AttachRecordingResponse,
  DownloadAlbumPayload,
  LidarrCommandResponse,
  MbidStatusResponse,
  MusicFormatPreferences,
  MusicFormatPreferencesResponse,
  MusicFormatReplacementStatusResponse,
  PlaylistCreateResponse,
  PlaylistDeleteResponse,
  PlaylistDetailResponse,
  PlaylistDownloadStartResponse,
  PlaylistDownloadStatusResponse,
  PlaylistPipelineStartResponse,
  PlaylistTrackActionResponse,
  PlaylistQualityCleanupResponse,
  PlaylistQualityPlacePayload,
  PlaylistQualityPlaceResponse,
  PlaylistResolveTrackResponse,
  PlaylistSyncStartResponse,
  PlaylistSyncStatusResponse,
  PlexStatusResponse,
  PluginInstallLogResponse,
  PluginStatusResponse,
  PlaylistMatchedTrack,
  PlaylistParseResponse,
  PlaylistApplySuggestionsResponse,
  PlaylistsResponse,
  PlaylistSuggestionsResponse,
  PlaylistSource,
  PlaylistTrack,
  PlaylistRowsResponse,
  PreflightResponse,
  RecentImportsResponse,
  ReimportDiskPayload,
  ReviewQueueParams,
  ReviewQueueResponse,
  LibraryMissingMbParams,
  LibraryMissingMbResponse,
  MissingMbBatchPreviewPayload,
  MissingMbBatchStartResponse,
  RgidGroupDetailResponse,
  FolderPlaceholderReview,
  FolderPlaceholderMergePreview,
  FolderPlaceholderApplyResult,
  FolderStatsResponse,
  ImportTargetPreviewPayload,
  ImportReviewDecisionResponse,
  ImportTargetPreviewResponse,
  StatsResponse,
  SetupAuthTokenRegenerateResponse,
  SetupEnvResponse,
  SetupEnvRevealAuthResponse,
  SetupEnvRevealResponse,
  SetupEnvSavePayload,
  SetupFirstRunRequest,
  SetupFirstRunResponse,
  SetupIntegrationTestResponse,
  SetupStatusResponse,
  SetupTestBeetsResponse,
  BeetsPluginsReport,
  AuthMeResponse,
  LoginRequest,
  LoginResponse,
  LogoutResponse,
  YtdlpStatusResponse,
  TransactionDetailResponse,
  TransactionListResponse,
  TransactionSettings,
  SubmissionTargetResponse,
  SubmissionDraftResponse,
  SubmissionReferenceUrlResponse,
  SubmissionMusicBrainzValidationResponse,
  SubmissionAttachMbidsPayload,
  TransactionSettingsResponse,
  ReconciliationChoice,
  ReconciliationReviewsResponse,
} from './types';
import type { LibraryTrack } from '../types/api';
import { followedJobStart, idempotencyErrorMessage } from '../lib/jobStart';

type ApiErrorBody = {
  ok?: boolean;
  error?: string;
  code?: string;
  error_code?: string;
  candidate?: unknown;
  /** Set by POST /api/setup/env/<name>/reveal when the request was rejected
   * specifically for lacking a reveal authorization window -- distinct from
   * other rejection reasons so the caller knows to prompt for the
   * administrator password and retry, rather than just showing an error. */
  reauth_required?: boolean;
  /** Set on a 503 from the provider-outage handler (e.g. MusicBrainz down);
   * its `error` is fixed, user-safe text and is not a Beets engine outage. */
  unavailable?: boolean;
};

/** Reads the structured error body (code, sanitized candidate, ...) a
 * backend rejection attached to its thrown Error -- e.g. distinguishing
 * `review_confirmation_required` from other failures without parsing
 * `err.message` text. Returns undefined for non-API errors or plain
 * network failures. */
export function apiErrorBody(err: unknown): ApiErrorBody | undefined {
  return (err as { body?: ApiErrorBody } | null | undefined)?.body;
}

export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase();
  // Every mutating request must carry X-Beets-CSRF, regardless of which call
  // site built the RequestInit. Job starts are deduplicated by the key (#344).
  const { headers, settle } = prepareRequestHeaders(method, path, init?.body, init?.headers);

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (err: unknown) {
    const { message, isTimeout } = fetchFailure(err);
    const netErr = new Error(message) as Error & { body?: ApiErrorBody; isNetworkError?: boolean; isTimeout?: boolean; httpStatus?: number };
    netErr.isNetworkError = !isTimeout;
    netErr.isTimeout = isTimeout;
    netErr.httpStatus = 0;
    throw netErr;
  }

  if (response.status < 500) {
    settle();
  }
  const body = (await response.json().catch(() => null)) as ApiErrorBody | null;

  // Same job already running, or this key already started it: follow it.
  const followed = followedJobStart(response.status, body);
  if (followed) return followed as T;

  if (!response.ok) {
    let errorCode = body?.error_code || body?.code || '';
    const { isEngineOffline, isEngineAuthError, isAuthError } = classifyHttpFailure(response.status, errorCode, body?.unavailable === true);
    let message = idempotencyErrorMessage(errorCode) || body?.error || `HTTP ${response.status}`;

    if (isAuthError) {
      message = SESSION_EXPIRED_MESSAGE;
    } else if (isEngineOffline) {
      message = ENGINE_OFFLINE_MESSAGE;
    } else if (isEngineAuthError) {
      message = ENGINE_AUTH_MESSAGE;
    }

    const err = new Error(message) as Error & { body?: ApiErrorBody; isAuthError?: boolean; isEngineOffline?: boolean; isEngineAuthError?: boolean; httpStatus?: number };
    err.body = body ?? undefined;
    err.httpStatus = response.status;
    err.isAuthError = isAuthError;
    err.isEngineOffline = isEngineOffline;
    err.isEngineAuthError = isEngineAuthError;
    throw err;
  }

  if (body && body.ok === false) {
    let message = body.error || 'Request failed';
    let isEngineOffline = body.error_code === 'ENGINE_OFFLINE';
    let isEngineAuthError = body.error_code === 'ENGINE_AUTH_FAILED';
    if (isEngineOffline) {
      message = ENGINE_OFFLINE_MESSAGE;
    } else if (isEngineAuthError) {
      message = ENGINE_AUTH_MESSAGE;
    }

    const err = new Error(message) as Error & { body?: ApiErrorBody; isEngineOffline?: boolean; isEngineAuthError?: boolean };
    err.body = body;
    err.isEngineOffline = isEngineOffline;
    err.isEngineAuthError = isEngineAuthError;
    throw err;
  }

  return body as T;
}

function jsonRequest(method: 'POST' | 'DELETE', body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

export function getReviewQueue(options: number | ReviewQueueParams = 500): Promise<ReviewQueueResponse> {
  const params = new URLSearchParams();
  if (typeof options === 'number') {
    params.set('limit', String(options));
  } else {
    params.set('limit', String(options.limit ?? 500));
    if (options.offset) params.set('offset', String(options.offset));
    if (options.status && options.status !== 'all') params.set('status', options.status);
    if (options.origin_type && options.origin_type !== 'all') params.set('origin_type', options.origin_type);
    if (options.search?.trim()) params.set('search', options.search.trim());
    if (options.evidence_only) params.set('evidence_only', 'true');
  }
  return apiJson<ReviewQueueResponse>(`/api/import/review-queue?${params.toString()}`);
}

export function getLibraryMissingMbIds(options: LibraryMissingMbParams = {}): Promise<LibraryMissingMbResponse> {
  const params = new URLSearchParams();
  params.set('limit', String(options.limit ?? 100));
  if (options.offset) params.set('offset', String(options.offset));
  if (options.kind && options.kind !== 'all') params.set('kind', options.kind);
  if (options.search?.trim()) params.set('search', options.search.trim());
  if (options.sort) params.set('sort', options.sort);
  return apiJson<LibraryMissingMbResponse>(`/api/library/missing-mb-ids?${params.toString()}`);
}

/** Start the read-only proof job for up to 50 Missing IDs rows. */
export function previewMissingMbBatch(payload: MissingMbBatchPreviewPayload): Promise<MissingMbBatchStartResponse> {
  return apiJson<MissingMbBatchStartResponse>('/api/library/missing-mb-ids/batch/preview', jsonRequest('POST', payload));
}

/** Start writing the proven rows of one preview. confirm_token goes only here. */
export function applyMissingMbBatch(transactionId: string, confirmToken: string): Promise<MissingMbBatchStartResponse> {
  return apiJson<MissingMbBatchStartResponse>(
    '/api/library/missing-mb-ids/batch/apply',
    jsonRequest('POST', { transaction_id: transactionId, confirm_token: confirmToken }),
  );
}

export interface CleanupStaleResponse {
  ok: boolean;
  removed_folder_gone: number;
  /** Entries whose album is already resolved in Beets. */
  removed_resolved: number;
  removed_total: number;
  remaining: number;
}

export function cleanupStaleReview(): Promise<CleanupStaleResponse> {
  return apiJson<CleanupStaleResponse>('/api/import/cleanup-stale', jsonRequest('POST'));
}

export function getHealth(): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>('/api/health');
}

export function getHealthDetail(): Promise<HealthResponse> {
  return apiJson<HealthResponse>('/api/health/detail');
}
export function getTransactionSettings(): Promise<TransactionSettingsResponse> {
  return apiJson<TransactionSettingsResponse>('/api/transactions/settings');
}

export function saveTransactionSettings(payload: Partial<TransactionSettings>): Promise<TransactionSettingsResponse> {
  return apiJson<TransactionSettingsResponse>('/api/transactions/settings', jsonRequest('POST', payload));
}

export function getTransactions(params: {
  status?: string;
  operation?: string;
  q?: string;
  offset?: number;
  limit?: number;
} = {}): Promise<TransactionListResponse> {
  const query = new URLSearchParams();
  if (params.status && params.status !== 'all') query.set('status', params.status);
  if (params.operation && params.operation !== 'all') query.set('operation', params.operation);
  if (params.q?.trim()) query.set('q', params.q.trim());
  if (params.offset) query.set('offset', String(params.offset));
  if (params.limit) query.set('limit', String(params.limit));
  const suffix = query.toString();
  return apiJson<TransactionListResponse>(`/api/transactions${suffix ? `?${suffix}` : ''}`);
}

export function getTransaction(transactionId: string, params: { offset?: number; limit?: number } = {}): Promise<TransactionDetailResponse> {
  const query = new URLSearchParams();
  if (params.offset) query.set('offset', String(params.offset));
  if (params.limit) query.set('limit', String(params.limit));
  const suffix = query.toString();
  return apiJson<TransactionDetailResponse>(`/api/transactions/${encodeURIComponent(transactionId)}${suffix ? `?${suffix}` : ''}`);
}

/** `confirmDeleteFiles` is the phrase the user typed; the backend requires it
 * (and checks it) for album-cleanup plans that delete files. */
export function approveTransaction(transactionId: string, confirmDeleteFiles?: string): Promise<TransactionDetailResponse> {
  return apiJson<TransactionDetailResponse>(
    `/api/transactions/${encodeURIComponent(transactionId)}/approve`,
    jsonRequest('POST', confirmDeleteFiles === undefined ? undefined : { confirm_delete_files: confirmDeleteFiles }),
  );
}

export function cancelTransaction(transactionId: string): Promise<TransactionDetailResponse> {
  return apiJson<TransactionDetailResponse>(`/api/transactions/${encodeURIComponent(transactionId)}/cancel`, jsonRequest('POST'));
}
export function applyTransaction(transactionId: string): Promise<TransactionDetailResponse & { job_id?: string }> {
  return apiJson<TransactionDetailResponse & { job_id?: string }>(`/api/transactions/${encodeURIComponent(transactionId)}/apply`, jsonRequest('POST'));
}

export function rollbackTransaction(transactionId: string): Promise<TransactionDetailResponse> {
  return apiJson<TransactionDetailResponse>(`/api/transactions/${encodeURIComponent(transactionId)}/rollback`, jsonRequest('POST'));
}

// ── Untracked files (ARCH-021) ──────────────────────────────────────────────

export function getUntrackedInventorySummary(): Promise<UntrackedInventorySummaryResponse> {
  return apiJson<UntrackedInventorySummaryResponse>('/api/library/untracked-inventory');
}

export function startUntrackedInventory(): Promise<{ ok: boolean; job_id?: string; error?: string }> {
  return apiJson<{ ok: boolean; job_id?: string; error?: string }>('/api/library/untracked-inventory', jsonRequest('POST', {}));
}

export function getUntrackedCandidates(category: string, limit = 50, offset = 0): Promise<UntrackedCandidatesResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (category) params.set('category', category);
  return apiJson<UntrackedCandidatesResponse>(`/api/library/untracked-recovery/candidates?${params.toString()}`);
}

export function planUntrackedRecovery(action: string, paths: string[]): Promise<UntrackedRecoveryPlanResponse> {
  return apiJson<UntrackedRecoveryPlanResponse>('/api/library/untracked-recovery/plan', jsonRequest('POST', { action, paths }));
}

// ── Recover My Uploads ──────────────────────────────────────────────────────
// Re-download a user's own YouTube/SoundCloud uploads. The backend owns every
// decision; these just carry the request and return the backend's verdict.

export function recoverProbe(url: string): Promise<RecoverProbeResponse> {
  return apiJson<RecoverProbeResponse>('/api/recover/probe', jsonRequest('POST', { url }));
}

export function getRecoverRuns(limit = 50): Promise<RecoverRunsResponse> {
  return apiJson<RecoverRunsResponse>(`/api/recover/runs?limit=${encodeURIComponent(limit)}`);
}

/** Start a fetch+stage job for one or more URLs. The backend REQUIRES an
 * authorization basis; without one it returns {ok:false, error:"authorization
 * basis required (...)"}. */
export function startRecoverFetch(urls: string[], authorization: RecoverAuthorizationInput): Promise<RecoverFetchResponse> {
  return apiJson<RecoverFetchResponse>('/api/recover/fetch', jsonRequest('POST', { urls, authorization }));
}

export function retryRecoverRun(runId: string): Promise<RecoverRetryResponse> {
  return apiJson<RecoverRetryResponse>('/api/recover/retry', jsonRequest('POST', { run_id: runId }));
}

/** Import a staged run. The backend REQUIRES the same authorization basis as
 * fetch; an asserted (unverified) basis routes items to review, not auto-import. */
export function importRecoverRun(runId: string, authorization: RecoverAuthorizationInput): Promise<RecoverImportResponse> {
  return apiJson<RecoverImportResponse>('/api/recover/import', jsonRequest('POST', { run_id: runId, authorization }));
}

export function transactionExportUrl(transactionId: string, format: 'json' | 'csv' | 'markdown'): string {
  return `/api/transactions/${encodeURIComponent(transactionId)}/export?format=${encodeURIComponent(format)}`;
}

export function getSetupStatus(): Promise<SetupStatusResponse> {
  return apiJson<SetupStatusResponse>('/api/setup/status');
}

export function getAuthMe(): Promise<AuthMeResponse> {
  return apiJson<AuthMeResponse>('/api/auth/me');
}

export function login(payload: LoginRequest): Promise<LoginResponse> {
  return apiJson<LoginResponse>('/api/login', jsonRequest('POST', payload));
}

export function logout(): Promise<LogoutResponse> {
  return apiJson<LogoutResponse>('/api/logout', jsonRequest('POST'));
}

export function submitFirstRunSetup(payload: SetupFirstRunRequest): Promise<SetupFirstRunResponse> {
  return apiJson<SetupFirstRunResponse>('/api/setup/first-run', jsonRequest('POST', payload));
}

export function getSetupEnv(): Promise<SetupEnvResponse> {
  return apiJson<SetupEnvResponse>('/api/setup/env');
}

export function saveSetupEnv(payload: SetupEnvSavePayload): Promise<SetupEnvResponse> {
  return apiJson<SetupEnvResponse>('/api/setup/env', jsonRequest('POST', payload));
}

export function completeSetup(): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>('/api/setup/complete', jsonRequest('POST'));
}

export function getPluginsStatus(): Promise<BeetsPluginsReport> {
  return apiJson<BeetsPluginsReport>('/api/plugins/status');
}


export function verifyPlugins(): Promise<BeetsPluginsReport> {
  return apiJson<BeetsPluginsReport>('/api/plugins/verify', jsonRequest('POST'));
}

export function testSetupBeets(): Promise<SetupTestBeetsResponse> {
  return apiJson<SetupTestBeetsResponse>('/api/setup/test/beets', jsonRequest('POST'));
}

export function testSetupAi(payload?: { api_key?: string; base_url?: string; model?: string }): Promise<SetupIntegrationTestResponse> {
  return apiJson<SetupIntegrationTestResponse>('/api/setup/test/ai', jsonRequest('POST', payload));
}

export function testSetupMusicBrainz(): Promise<SetupIntegrationTestResponse> {
  return apiJson<SetupIntegrationTestResponse>('/api/setup/test/musicbrainz', jsonRequest('POST'));
}

export function testSetupAcoustid(payload?: { api_key?: string }): Promise<SetupIntegrationTestResponse> {
  return apiJson<SetupIntegrationTestResponse>('/api/setup/test/acoustid', jsonRequest('POST', payload));
}

export function testSetupPlex(payload?: { url?: string; token?: string }): Promise<SetupIntegrationTestResponse> {
  return apiJson<SetupIntegrationTestResponse>('/api/setup/test/plex', jsonRequest('POST', payload));
}

export function regenerateAuthToken(): Promise<SetupAuthTokenRegenerateResponse> {
  return apiJson<SetupAuthTokenRegenerateResponse>('/api/setup/auth-token/regenerate', jsonRequest('POST'));
}

/** Fetches the single effective plaintext value for one revealable secret
 * setting. Never call this speculatively for every field -- only in direct
 * response to the operator clicking Show on that one field. */
export function revealSetupEnvValue(name: string): Promise<SetupEnvRevealResponse> {
  return apiJson<SetupEnvRevealResponse>(`/api/setup/env/${encodeURIComponent(name)}/reveal`, jsonRequest('POST'));
}

/** Confirms the current administrator password to open a short reveal
 * authorization window (only meaningful on an install with a browser
 * password configured -- trivially succeeds otherwise). */
export function authorizeSetupEnvReveal(password: string): Promise<SetupEnvRevealAuthResponse> {
  return apiJson<SetupEnvRevealAuthResponse>('/api/setup/env/reveal-auth', jsonRequest('POST', { password }));
}

export function getStats(): Promise<StatsResponse> {
  return apiJson<StatsResponse>('/api/stats');
}

export function getMbidStatus(): Promise<MbidStatusResponse> {
  return apiJson<MbidStatusResponse>('/api/library/mbid-status');
}

export function startMbidStickingRepair(opts: {
  dryRun: boolean;
  repairTracks?: boolean;
  writeTags?: boolean;
  limit?: number;
}): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/library/mbid-sticking-repair',
    jsonRequest('POST', {
      dry_run: opts.dryRun,
      repair_tracks: opts.repairTracks ?? true,
      write_tags: opts.writeTags ?? !opts.dryRun,
      limit: opts.limit ?? 100,
    }),
  );
}

export function startMbFullSync(opts?: {
  repairTracks?: boolean;
  writeTags?: boolean;
}): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/library/mb-full-sync',
    jsonRequest('POST', {
      repair_tracks: opts?.repairTracks ?? true,
      write_tags: opts?.writeTags ?? true,
    }),
  );
}

export function startTemplateTokenCleanup(opts: {
  dryRun: boolean;
  albumId?: number;
}): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/library/template-token-cleanup',
    jsonRequest('POST', {
      dry_run: opts.dryRun,
      album_id: opts.albumId,
    }),
  );
}

export function getRgidGroupDetail(rgid: string): Promise<RgidGroupDetailResponse> {
  return apiJson<RgidGroupDetailResponse>(`/api/clean/rgid-group/${encodeURIComponent(rgid)}`);
}

export function mergeRgidGroup(opts: {
  mbReleaseGroupId: string;
  targetAlbumId: number;
  sourceAlbumId: number;
}): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/clean/rgid-group/merge',
    jsonRequest('POST', {
      mb_releasegroupid: opts.mbReleaseGroupId,
      target_album_id: opts.targetAlbumId,
      source_album_id: opts.sourceAlbumId,
    }),
  );
}

export function keepRgidGroupSeparate(opts: { mbReleaseGroupId: string; reason?: string }): Promise<{ ok: boolean; error?: string }> {
  return apiJson('/api/clean/rgid-group/keep-separate', jsonRequest('POST', {
    mb_releasegroupid: opts.mbReleaseGroupId,
    reason: opts.reason,
  }));
}

export function undoRgidGroupResolution(mbReleaseGroupId: string): Promise<{ ok: boolean; error?: string }> {
  return apiJson('/api/clean/rgid-group/undo-resolution', jsonRequest('POST', {
    mb_releasegroupid: mbReleaseGroupId,
  }));
}

export function assignRgidRepresentativeRelease(opts: { albumId: number; mbAlbumId: string }): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/rgid-group/assign-representative-release', jsonRequest('POST', {
    album_id: opts.albumId,
    mb_albumid: opts.mbAlbumId,
  }));
}

export function relinkRgidAlbum(opts: { albumId: number; mbAlbumId?: string; mbReleaseGroupId?: string }): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/rgid-group/relink', jsonRequest('POST', {
    album_id: opts.albumId,
    mb_albumid: opts.mbAlbumId,
    mb_releasegroupid: opts.mbReleaseGroupId,
  }));
}

export function sendRgidAlbumToRepair(albumId: number): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/rgid-group/send-to-repair', jsonRequest('POST', {
    album_id: albumId,
  }));
}

export function restartApp(): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>('/api/restart', { method: 'POST' });
}

export function runPreflight(path: string): Promise<PreflightResponse> {
  return apiJson<PreflightResponse>('/api/import/preflight', jsonRequest('POST', { path }));
}

export function startAiBatchImport(path: string, recoverBatchJobId?: string): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/ai-batch-import',
    jsonRequest('POST', { path, recover_batch_job_id: recoverBatchJobId || undefined }),
  );
}

export function getAiBatchStatus(jobId?: string): Promise<AiBatchStatusResponse> {
  const query = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';
  return apiJson<AiBatchStatusResponse>(`/api/ai-batch-import/status${query}`);
}

export function pauseAiBatch(jobId?: string): Promise<AiBatchStatusResponse> {
  return apiJson<AiBatchStatusResponse>('/api/ai-batch-pause', jsonRequest('POST', { job_id: jobId || undefined }));
}

export function recoverAiBatch(jobId?: string, retryFailed = false): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/ai-batch-import/recover', jsonRequest('POST', { job_id: jobId || undefined, retry_failed: retryFailed || undefined }));
}
export function stopAiBatch(jobId?: string): Promise<AiBatchStatusResponse> {
  return apiJson<AiBatchStatusResponse>('/api/ai-batch-stop', jsonRequest('POST', { job_id: jobId || undefined }));
}

export function skipAiBatch(jobId?: string, folderId?: string, skipStale = false): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>(
    '/api/ai-batch-skip',
    jsonRequest('POST', { job_id: jobId || undefined, folder_id: folderId || undefined, skip_stale: skipStale }),
  );
}

export function killJob(jobId: string): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>(`/api/jobs/${jobId}/kill`, { method: 'POST' });
}

export function getRecentImports(): Promise<RecentImportsResponse> {
  return apiJson<RecentImportsResponse>('/api/recent-imports');
}

export function clearRecentImports(): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>('/api/recent-imports/clear', { method: 'POST' });
}

export function getAiMatchHistory(limit = 50): Promise<AiMatchHistoryResponse> {
  return apiJson<AiMatchHistoryResponse>(`/api/ai-match-history?limit=${limit}`);
}

// ── Dedup ─────────────────────────────────────────────────────────────────────

export function startDedupScan(path: string): Promise<DedupStartResponse> {
  return apiJson<DedupStartResponse>('/api/dedup/scan', jsonRequest('POST', { path }));
}

export function getDedupScan(jid: string): Promise<DedupScanState & { ok: boolean }> {
  return apiJson(`/api/dedup/scan/${jid}`);
}

export function startDedupAiReview(scanJid: string, scanPath: string): Promise<DedupStartResponse> {
  return apiJson<DedupStartResponse>(
    '/api/dedup/ai-review',
    jsonRequest('POST', { scan_jid: scanJid, scan_path: scanPath }),
  );
}

export function runDedupCleanup(
  paths: string[],
  root: string,
  dryRun = true,
): Promise<DedupCleanupResponse> {
  return apiJson<DedupCleanupResponse>(
    '/api/dedup/cleanup',
    jsonRequest('POST', { paths, root, dry_run: dryRun }),
  );
}

// ── Album Tracks ──────────────────────────────────────────────────────────────

export function scanAlbumTracks(opts?: {
  albumId?: number;
  limit?: number;
  useAi?: boolean;
  useFingerprint?: boolean;
}): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/album-tracks/scan', jsonRequest('POST', {
    album_id: opts?.albumId ?? 0,
    limit: opts?.limit ?? 75,
    use_ai: opts?.useAi ?? true,
    use_fingerprint: opts?.useFingerprint ?? true,
  }));
}

export function removeAlbumTracks(
  albumId: number,
  itemIds: number[],
  dryRun = false,
): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/clean/album-tracks/remove',
    jsonRequest('POST', {
      album_id: albumId,
      item_ids: itemIds,
      dry_run: dryRun,
      delete_files: true,
      clean_empty_folders: false,
    }),
  );
}

export function removeAlbumTracksBatch(
  groups: Array<{ album_id: number; item_ids: number[] }>,
  dryRun = false,
): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/clean/album-tracks/remove-batch',
    jsonRequest('POST', {
      groups,
      dry_run: dryRun,
      delete_files: true,
      clean_empty_folders: false,
    }),
  );
}

// ── Artist Folders ────────────────────────────────────────────────────────────

export function scanArtistFolders(root?: string): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/clean/artist-folders/scan',
    jsonRequest('POST', root ? { root } : {}),  // server defaults to its MUSIC_ROOT
  );
}

export function mergeArtistFolders(
  keys: string[],
  root?: string,
  dryRun = true,
): Promise<JobStartResponse & { dry_run?: boolean; summary?: unknown; log?: string[] }> {
  return apiJson(
    '/api/clean/artist-folders/merge',
    jsonRequest('POST', { keys, ...(root ? { root } : {}), dry_run: dryRun }),
  );
}

export type StampMbidFoldersResponse = ApiOkResponse & {
  dry_run?: boolean;
  job_id?: string;
  log?: string[];
  renamed?: number;
  skipped?: number;
  candidates?: number;
  skipped_total?: number;
};

export function stampMbidFolders(opts: {
  root?: string;
  dryRun?: boolean;
  folders?: string[];
}): Promise<StampMbidFoldersResponse> {
  return apiJson(
    '/api/clean/artist-folders/stamp-mbid',
    jsonRequest('POST', {
      ...(opts.root ? { root: opts.root } : {}),
      dry_run: opts.dryRun ?? true,
      folders: opts.folders ?? [],
    }),
  );
}

// ── Playlists ─────────────────────────────────────────────────────────────────

export function getPlaylists(): Promise<PlaylistsResponse> {
  return apiJson<PlaylistsResponse>('/api/playlists');
}

export function getPlaylistDetails(
  name: string,
  options?: { mode?: 'summary' | 'full' },
): Promise<PlaylistDetailResponse> {
  const qs = options?.mode ? `?mode=${encodeURIComponent(options.mode)}` : '';
  return apiJson<PlaylistDetailResponse>(`/api/playlists/${encodeURIComponent(name)}/tracks${qs}`);
}

export function getPlaylistRows(
  name: string,
  options: { group?: string; offset?: number; limit?: number } = {},
): Promise<PlaylistRowsResponse> {
  const qs = new URLSearchParams({ mode: 'rows' });
  if (options.group) qs.set('group', options.group);
  if (options.offset !== undefined) qs.set('offset', String(options.offset));
  if (options.limit !== undefined) qs.set('limit', String(options.limit));
  return apiJson<PlaylistRowsResponse>(`/api/playlists/${encodeURIComponent(name)}/tracks?${qs.toString()}`);
}
export function resolvePlaylistTrack(
  name: string,
  track: PlaylistTrack,
  replacement: Pick<PlaylistTrack, 'artist' | 'title'>,
): Promise<PlaylistResolveTrackResponse> {
  return apiJson<PlaylistResolveTrackResponse>(
    `/api/playlists/${encodeURIComponent(name)}/resolve-track`,
    jsonRequest('POST', { track, replacement }),
  );
}

export function getPlaylistSuggestions(name: string): Promise<PlaylistSuggestionsResponse> {
  return apiJson<PlaylistSuggestionsResponse>(`/api/playlists/${encodeURIComponent(name)}/suggestions`);
}

export function applySafePlaylistSuggestions(name: string): Promise<PlaylistApplySuggestionsResponse> {
  return apiJson<PlaylistApplySuggestionsResponse>(
    `/api/playlists/${encodeURIComponent(name)}/apply-safe-suggestions`,
    jsonRequest('POST', { musicbrainz: true }),
  );
}

export function parsePlaylist(source: PlaylistSource, content: string): Promise<PlaylistParseResponse> {
  return apiJson<PlaylistParseResponse>('/api/playlist/parse', jsonRequest('POST', { source, content }));
}

export function createPlaylist(
  name: string,
  items: PlaylistMatchedTrack[],
  desiredTracks?: PlaylistTrack[],
  missingTracks?: PlaylistTrack[],
  source?: PlaylistSource,
  content?: string,
): Promise<PlaylistCreateResponse> {
  return apiJson<PlaylistCreateResponse>(
    '/api/playlist/create',
    jsonRequest('POST', {
      name,
      items,
      desired_tracks: desiredTracks?.length ? desiredTracks : items,
      missing_tracks: missingTracks ?? [],
      source: source ?? 'text',
      content: content ?? '',
    }),
  );
}

export function deletePlaylist(name: string, deletePlex = true): Promise<PlaylistDeleteResponse> {
  return apiJson<PlaylistDeleteResponse>(
    `/api/playlists/${encodeURIComponent(name)}`,
    jsonRequest('DELETE', { delete_plex: deletePlex }),
  );
}

export function startPlaylistDownload(payload: {
  name: string;
  tracks?: PlaylistTrack[];
  all_tracks?: PlaylistTrack[];
  source?: PlaylistSource;
  content?: string;
  methods?: string[];
  sync_after_import: boolean;
}): Promise<PlaylistDownloadStartResponse> {
  return apiJson<PlaylistDownloadStartResponse>('/api/playlist/download', jsonRequest('POST', payload));
}

export function getPlaylistDownloadStatus(jobId: string): Promise<PlaylistDownloadStatusResponse> {
  return apiJson<PlaylistDownloadStatusResponse>(`/api/playlist/download/${jobId}`);
}

export function getPlaylistSyncStatus(): Promise<PlaylistSyncStatusResponse> {
  return apiJson<PlaylistSyncStatusResponse>('/api/playlists/sync/status');
}

export function syncPlaylists(names?: string[]): Promise<PlaylistSyncStartResponse> {
  return apiJson<PlaylistSyncStartResponse>('/api/playlists/sync', jsonRequest('POST', { names: names ?? [] }));
}

export function runPlaylistPipelineAction(
  name: string,
  action:
    | 'sync-sources'
    | 'download-missing'
    | 'import-downloaded'
    | 'sync-plex'
    | 'reconcile-state'
    | 'run-full'
    | 'resume'
    | 'pause'
    | 'stop'
    | 'clear',
): Promise<PlaylistPipelineStartResponse> {
  return apiJson<PlaylistPipelineStartResponse>(
    `/api/playlists/${encodeURIComponent(name)}/pipeline/${action}`,
    jsonRequest('POST', {}),
  );
}

export function applyPlaylistTrackAction(
  name: string,
  action: 'remove' | 'exclude' | 'restore' | 'delete_staged' | 'retry_download' | 'retry_import',
  track: PlaylistTrack,
): Promise<PlaylistTrackActionResponse> {
  return apiJson<PlaylistTrackActionResponse>(
    `/api/playlists/${encodeURIComponent(name)}/tracks/action`,
    jsonRequest('POST', { action, track, path: track.staged_path }),
  );
}

export function cleanupPlaylistQuality(payload: {
  dry_run: boolean;
  action?: 'scan' | 'repair' | 'delete_preview' | string;
  filter?: 'all' | 'repair' | 'preview' | string;
  item_ids?: number[];
  limit?: number;
  all_matching?: boolean;
}): Promise<PlaylistQualityCleanupResponse> {
  return apiJson<PlaylistQualityCleanupResponse>('/api/playlists/quality-cleanup', jsonRequest('POST', payload));
}

export function placePlaylistQuality(payload: PlaylistQualityPlacePayload): Promise<PlaylistQualityPlaceResponse> {
  return apiJson<PlaylistQualityPlaceResponse>('/api/playlists/quality-place', jsonRequest('POST', payload));
}

// ── Job result helper (type-narrow job.result from PythonJob) ─────────────────

export function getJobResult<T>(jobResponse: { result?: unknown }): T | null {
  return (jobResponse.result as T) ?? null;
}

export function getArtistImageUrl(artist: string): Promise<{ ok: boolean; url?: string; source?: string }> {
  return apiJson(`/api/artist-image-url?artist=${encodeURIComponent(artist)}`);
}

export function getAlbumTracks(albumId: number): Promise<{ ok: boolean; album_id: number; tracks: LibraryTrack[] }> {
  return apiJson<{ ok: boolean; album_id: number; tracks: LibraryTrack[] }>(`/api/albums/${albumId}/tracks`);
}

export function mergeSplitAlbum(
  targetAlbumId: number,
  sourceAlbumId: number,
  itemIds: number[],
): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    `/api/albums/${targetAlbumId}/merge-split-album`,
    jsonRequest('POST', { source_album_id: sourceAlbumId, item_ids: itemIds }),
  );
}

/** One page of the cached Find Music list. Never asks the server to rebuild it. */
export function getAcquisitionQueue(options: AcquisitionQueueParams = {}): Promise<AcquisitionQueueResponse> {
  const params = new URLSearchParams();
  if (options.offset) params.set('offset', String(options.offset));
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  if (options.search?.trim()) params.set('search', options.search.trim());
  if (options.source && options.source !== 'all') params.set('source', options.source);
  if (options.album_type?.length) params.set('album_type', options.album_type.join(','));
  if (options.sort && options.sort !== 'sort_key') params.set('sort', options.sort);
  if (options.include_unmonitored) params.set('include_unmonitored', '1');
  const query = params.toString();
  return apiJson<AcquisitionQueueResponse>(`/api/acquisition/queue${query ? `?${query}` : ''}`);
}

/** Start (or join) the background Find Music list build. */
export function refreshAcquisitionQueue(includeUnmonitored = false, afterDownload = false): Promise<AcquisitionQueueRefreshResponse> {
  // after_download lets the server reuse a list that is still current; only the automatic
  // refresh after a finished download sends it, never the user's Refresh button.
  return apiJson<AcquisitionQueueRefreshResponse>(
    '/api/acquisition/queue/refresh',
    jsonRequest('POST', {
      ...(includeUnmonitored ? { include_unmonitored: true } : {}),
      ...(afterDownload ? { after_download: true } : {}),
    }),
  );
}

export function startAcquisitionDownloadAll(
  payload: AcquisitionDownloadAllPayload,
): Promise<AcquisitionDownloadAllStartResponse> {
  return apiJson<AcquisitionDownloadAllStartResponse>('/api/acquisition/download-all', jsonRequest('POST', payload));
}

export function getAcquisitionDownloadAllActive(): Promise<AcquisitionDownloadAllActiveResponse> {
  return apiJson<AcquisitionDownloadAllActiveResponse>('/api/acquisition/download-all/active');
}

export function runLidarrAlbumSearch(albumId: number): Promise<LidarrCommandResponse> {
  return apiJson<LidarrCommandResponse>(
    '/api/lidarr/command',
    jsonRequest('POST', { name: 'AlbumSearch', albumIds: [albumId] }),
  );
}

export function startAlbumDownload(payload: DownloadAlbumPayload): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/download/album', jsonRequest('POST', payload));
}

export function getYtdlpStatus(): Promise<YtdlpStatusResponse> {
  return apiJson<YtdlpStatusResponse>('/api/ytdlp/status');
}

// ── Artist alias review ───────────────────────────────────────────────────────

export function getArtistIdGroups(): Promise<ArtistIdGroupsResponse> {
  return apiJson<ArtistIdGroupsResponse>('/api/library/artist-id-groups');
}

export function mergeArtistId(mb_artistid: string, canonical: string): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/merge-artist-id', jsonRequest('POST', { mb_artistid, canonical }));
}

export function rejectArtistIdGroup(mb_artistid: string, reject_key?: string): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>(
    '/api/library/artist-id-groups/reject',
    jsonRequest('POST', { mb_artistid, reject_key }),
  );
}

export function confirmArtistAlias(
  source_artist: string,
  canonical_artist: string,
  mb_artistid?: string,
): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/library/confirm-artist-alias',
    jsonRequest('POST', { source_artist, canonical_artist, mb_artistid: mb_artistid || undefined }),
  );
}

// ── Plugin runner ─────────────────────────────────────────────────────────────

export function runPlugin(args: string[], label: string): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/plugins/run', jsonRequest('POST', { args, label }));
}

export function getPluginStatus(): Promise<PluginStatusResponse> {
  return apiJson<PluginStatusResponse>('/api/plugins/installed');
}

export function getPluginInstallLog(): Promise<PluginInstallLogResponse> {
  return apiJson<PluginInstallLogResponse>('/api/plugins/install-log');
}

// ── Library actions ───────────────────────────────────────────────────────────

export function syncDeleted(opts: { dryRun?: boolean; confirmed?: boolean } = {}): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/sync-deleted', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: JSON.stringify({
      dry_run: opts.dryRun !== false,
      confirmed: opts.confirmed === true,
    }),
  });
}

export function startMbsyncAll(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/mbsync-all', { method: 'POST' });
}

export function scanLeakedDbPaths(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/leaked-db-paths/scan', { method: 'POST' });
}

export function fixLeakedDbPaths(opts: { dry_run: boolean; item_ids?: number[]; confirmed?: boolean }): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/leaked-db-paths/fix', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: JSON.stringify(opts),
  });
}

export function scanFolderPlaceholders(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/folder-placeholders/scan', { method: 'POST' });
}

export function reviewFolderPlaceholder(
  sourcePath: string,
  proposedPath: string | null,
): Promise<FolderPlaceholderReview> {
  const params = new URLSearchParams({ source_path: sourcePath });
  if (proposedPath) params.set('proposed_path', proposedPath);
  return apiJson<FolderPlaceholderReview>(`/api/clean/folder-placeholder/review?${params}`);
}

export function previewFolderPlaceholderMerge(
  sourcePath: string,
  targetPath: string,
): Promise<FolderPlaceholderMergePreview> {
  return apiJson<FolderPlaceholderMergePreview>('/api/clean/folder-placeholder/preview-merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: JSON.stringify({ source_path: sourcePath, target_path: targetPath }),
  });
}

export function applyFolderPlaceholderAction(
  action: string,
  sourcePath: string,
  opts: { targetPath?: string | null; previewToken?: string | null } = {},
): Promise<FolderPlaceholderApplyResult> {
  return apiJson<FolderPlaceholderApplyResult>('/api/clean/folder-placeholder/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: JSON.stringify({
      action,
      source_path: sourcePath,
      target_path: opts.targetPath,
      preview_token: opts.previewToken,
      confirmed: true,
    }),
  });
}

export function applySafeFolderPlaceholderRenames(
  sourcePaths?: string[],
): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/folder-placeholder/apply-safe-renames', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: JSON.stringify(sourcePaths !== undefined ? { source_paths: sourcePaths } : {}),
  });
}

export function scanAlbumFolders(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/album-folders/scan', { method: 'POST' });
}

export function applySafeAlbumFolderCleanup(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/album-folders/apply-safe', { method: 'POST' });
}

export function applyAlbumFolderCleanupIssue(issueId: string): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/clean/album-folders/apply-issue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Beets-CSRF': '1' },
    body: JSON.stringify({ issue_id: issueId, confirmed: true }),
  });
}

export function getAlbumFolderCleanupReport(): Promise<ApiOkResponse & { exists: boolean; report: Record<string, unknown> }> {
  return apiJson<ApiOkResponse & { exists: boolean; report: Record<string, unknown> }>('/api/clean/album-folders/report');
}

export function fetchMissingArt(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/fetch-missing-art', { method: 'POST' });
}

export function rebuildAlbumArt(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/rebuild-album-art', jsonRequest('POST', { confirmed: true }));
}

export function getLibraryArtRepairReport(): Promise<ArtRepairReportResponse> {
  return apiJson<ArtRepairReportResponse>('/api/library/art-repair');
}

export function fetchAlbumArt(albumId: number): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/albums/${albumId}/fetch-art`, { method: 'POST' });
}

export function reconcileArtwork(batchJobId: string, folderId: string, artworkJobId: string): Promise<AiBatchStatusResponse> {
  return apiJson<AiBatchStatusResponse>('/api/ai-batch/reconcile-artwork', jsonRequest('POST', { batch_job_id: batchJobId, folder_id: folderId, artwork_job_id: artworkJobId }));
}

// ── Genre ─────────────────────────────────────────────────────────────────────

export function fixGenres(opts: { force?: boolean; useAi?: boolean }): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(
    '/api/library/fix-genres',
    jsonRequest('POST', { force: opts.force ?? false, use_ai: opts.useAi ?? false }),
  );
}

// ── Plex ──────────────────────────────────────────────────────────────────────

export function getPlexStatus(): Promise<PlexStatusResponse> {
  return apiJson<PlexStatusResponse>('/api/plex/status');
}

export function refreshPlex(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/plex/refresh', { method: 'POST' });
}

// ── Config editor ─────────────────────────────────────────────────────────────

export function getConfigFile(): Promise<ConfigFileResponse> {
  return apiJson<ConfigFileResponse>('/api/config');
}

export function saveConfigFile(content: string, expectedRevision: string): Promise<ConfigSaveResponse> {
  return apiJson<ConfigSaveResponse>('/api/config', jsonRequest('POST', { content, expected_revision: expectedRevision }));
}

export function revertConfigFile(expectedRevision: string): Promise<ConfigSaveResponse> {
  return apiJson<ConfigSaveResponse>('/api/config/revert', jsonRequest('POST', { expected_revision: expectedRevision }));
}
export function getMusicFormatPreferences(): Promise<MusicFormatPreferencesResponse> {
  return apiJson<MusicFormatPreferencesResponse>('/api/settings/music-format');
}

export function saveMusicFormatPreferences(preferences: MusicFormatPreferences): Promise<MusicFormatPreferencesResponse> {
  return apiJson<MusicFormatPreferencesResponse>('/api/settings/music-format', jsonRequest('POST', { preferences }));
}

export function getMusicFormatReplacementStatuses(): Promise<MusicFormatReplacementStatusResponse> {
  return apiJson<MusicFormatReplacementStatusResponse>('/api/music-format/replacements');
}

export function startMusicFormatScan(limit = 0): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/music-format/scan', jsonRequest('POST', { limit }));
}

export function startMusicFormatReplacement(limit = 0, method = 'slskd'): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/library/music-format/replace', jsonRequest('POST', { limit, method }));
}

export function suggestAlbum(albumId: number): Promise<AiSuggestResponse> {
  return apiJson<AiSuggestResponse>(`/api/albums/${albumId}/ai-suggest`, { method: 'POST' });
}

export function suggestFolder(path: string): Promise<AiSuggestResponse> {
  return apiJson<AiSuggestResponse>('/api/folders/ai-suggest', jsonRequest('POST', { path }));
}

export function matchAlbum(albumId: number, mbId: string): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/albums/${albumId}/match`, jsonRequest('POST', { mb_id: mbId }));
}

export function suggestItem(itemId: number): Promise<AiSuggestResponse> {
  return apiJson<AiSuggestResponse>(`/api/items/${itemId}/ai-suggest`, { method: 'POST' });
}

export function validateManualMusicBrainzId(payload: ImportReviewManualIdPayload): Promise<ImportReviewManualIdResponse> {
  return apiJson<ImportReviewManualIdResponse>('/api/import-review/manual-id/validate', jsonRequest('POST', payload));
}

export function attachRecording(
  itemId: number,
  mbTrackId: string,
): Promise<AttachRecordingResponse> {
  // Always "safe" mode: the backend independently re-derives eligibility
  // from its own trusted candidate set and rejects anything that actually
  // needs review (see confirmAttachRecording) -- this call never asserts
  // safety/confidence/conflicts itself.
  return apiJson<AttachRecordingResponse>(
    `/api/items/${itemId}/attach-recording`,
    jsonRequest('POST', { mb_trackid: mbTrackId, mode: 'safe' }),
  );
}

export function confirmAttachRecording(
  itemId: number,
  mbTrackId: string,
  decisionVersion: string,
  confirmationReason: string,
): Promise<AttachRecordingResponse> {
  return apiJson<AttachRecordingResponse>(
    `/api/items/${itemId}/attach-recording`,
    jsonRequest('POST', {
      mb_trackid: mbTrackId,
      mode: 'confirmed_review',
      confirm: true,
      confirmation_reason: confirmationReason,
      decision_version: decisionVersion,
    }),
  );
}

export function reimportDisk(payload: ReimportDiskPayload): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/albums/reimport-disk', jsonRequest('POST', payload));
}

export function importWithId(payload: ImportWithIdPayload): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/folders/import-with-id', jsonRequest('POST', payload));
}

export function autoEnqueueImport(payload: AutoEnqueueImportPayload): Promise<AutoEnqueueImportResponse> {
  return apiJson<AutoEnqueueImportResponse>('/api/import-review/auto-enqueue', jsonRequest('POST', payload));
}

export function revalidateImportReview(payload: ImportReviewRevalidatePayload): Promise<ImportReviewRevalidateResponse> {
  return apiJson<ImportReviewRevalidateResponse>('/api/import-review/revalidate', jsonRequest('POST', payload));
}

/** The backend's authoritative Import Review action decisions (ARCH-005). */
export function decideImportReviewRemote(entries: unknown[]): Promise<ImportReviewDecisionResponse> {
  return apiJson<ImportReviewDecisionResponse>('/api/import-review/decision', jsonRequest('POST', { entries }));
}

export function previewImportTarget(payload: ImportTargetPreviewPayload): Promise<ImportTargetPreviewResponse> {
  return apiJson<ImportTargetPreviewResponse>('/api/folders/import-target-preview', jsonRequest('POST', payload));
}

export function deleteReviewFolder(
  path: string,
  confirmedWrongLibraryFolder = false,
  albumId?: number,
): Promise<DeleteReviewFolderResponse> {
  return apiJson<DeleteReviewFolderResponse>(
    '/api/import/review-folder/delete',
    jsonRequest('POST', {
      path,
      confirmed_wrong_library_folder: confirmedWrongLibraryFolder,
      album_id: albumId || undefined,
    }),
  );
}

export function cleanupReviewFiles(
  payload: ImportReviewFileCleanupPayload,
): Promise<ImportReviewFileCleanupResponse> {
  return apiJson<ImportReviewFileCleanupResponse>(
    '/api/import/review-files/cleanup',
    jsonRequest('POST', payload),
  );
}
export function getFolderStats(path: string): Promise<FolderStatsResponse> {
  return apiJson<FolderStatsResponse>(`/api/import/folder-stats?path=${encodeURIComponent(path)}`);
}

export function deletePendingReview(path: string): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>('/api/ai-pending-review', jsonRequest('DELETE', { path }));
}

export function getJob(jobId: string): Promise<JobResponse> {
  return apiJson<JobResponse>(`/api/jobs/${jobId}`);
}

export interface ReconcileImportJobResponse {
  ok: boolean;
  job_id: string;
  status: string;
  reconciled: boolean;
  handled?: boolean;
  reconciled_from?: string;
  step?: string;
  heartbeat_at?: number;
  note?: string;
  log?: string[];
  artist?: string;
  album?: string;
  imported_at?: number;
  retryable?: boolean;
  pending_review_exists?: boolean;
  source_exists?: boolean;
}

export function reconcileImportJob(
  jobId: string,
  sourcePath?: string,
  reviewItemId?: string,
): Promise<ReconcileImportJobResponse> {
  return apiJson<ReconcileImportJobResponse>(
    '/api/import/reconcile-job',
    jsonRequest('POST', { job_id: jobId, source_path: sourcePath || '', review_item_id: reviewItemId || '' }),
  );
}

export function startMaintenanceRunner(options?: { forceFresh?: boolean }): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/jobs/maintenance-runner', jsonRequest('POST', options?.forceFresh ? { force_fresh: true } : undefined));
}

export function startCleanAll(options?: { forceFresh?: boolean }): Promise<JobStartResponse> {
  return startMaintenanceRunner(options);
}

// ── Unmatched releases ────────────────────────────────────────────────────────


export function getSubmissionTarget(params: {
  albumId?: number;
  itemId?: number;
  path?: string;
  singleton?: boolean;
}): Promise<SubmissionTargetResponse> {
  const qs = new URLSearchParams();
  if (params.albumId) qs.set('album_id', String(params.albumId));
  if (params.itemId) qs.set('item_id', String(params.itemId));
  if (params.path) qs.set('path', params.path);
  if (params.singleton) qs.set('singleton', '1');
  return apiJson<SubmissionTargetResponse>(`/api/submissions/target?${qs.toString()}`);
}

export function saveSubmissionDraft(payload: {
  target_type: string;
  target_id: number | string;
  draft: Record<string, unknown>;
}): Promise<SubmissionDraftResponse> {
  const body = payload.target_type === 'folder'
    ? { target_type: payload.target_type, target_path: String(payload.target_id), draft: payload.draft }
    : payload;
  return apiJson<SubmissionDraftResponse>('/api/submissions/draft', jsonRequest('POST', body));
}

export function resetSubmissionDraft(targetType: string, targetId: number | string): Promise<SubmissionDraftResponse> {
  const qs = new URLSearchParams({ target_type: targetType });
  qs.set(targetType === 'folder' ? 'target_path' : 'target_id', String(targetId));
  return apiJson<SubmissionDraftResponse>(`/api/submissions/draft?${qs.toString()}`, { method: 'DELETE' });
}

export function addSubmissionReferenceUrl(payload: {
  albumId?: number;
  itemId?: number;
  path?: string;
  url: string;
}): Promise<SubmissionReferenceUrlResponse> {
  return apiJson<SubmissionReferenceUrlResponse>(
    '/api/submissions/reference-url',
    jsonRequest('POST', { album_id: payload.albumId || undefined, item_id: payload.itemId || undefined, path: payload.path || undefined, url: payload.url }),
  );
}

export function validateSubmissionMusicBrainzRelease(payload: {
  input: string;
  album_id?: number;
  item_id?: number;
}): Promise<SubmissionMusicBrainzValidationResponse> {
  return apiJson<SubmissionMusicBrainzValidationResponse>(
    '/api/submissions/musicbrainz-release/validate',
    jsonRequest('POST', payload),
  );
}

export function attachSubmissionMbids(albumId: number, payload: SubmissionAttachMbidsPayload): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/submissions/albums/${albumId}/attach-mbids`, jsonRequest('POST', payload));
}

export function albumMbsubmit(albumId: number): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/albums/${albumId}/mbsubmit`, jsonRequest('POST'));
}

export function itemMbsubmit(itemId: number): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/items/${itemId}/mbsubmit`, jsonRequest('POST'));
}

export function albumAcoustidSubmit(albumId: number): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/albums/${albumId}/acoustid-submit`, jsonRequest('POST'));
}

export function itemAcoustidSubmit(itemId: number): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/items/${itemId}/acoustid-submit`, jsonRequest('POST'));
}

export function albumAddMbids(
  albumId: number,
  payload: { mb_albumartistid: string; mb_releasegroupid: string; mb_albumid?: string },
): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>(`/api/albums/${albumId}/add-mbids`, jsonRequest('POST', payload));
}

/** Beets album/item rows as the Beets web API returns them (the plan
 * snapshots them verbatim); only the fields the UI reads are typed. */
export interface AlbumCleanupPlanAlbum {
  id?: number;
  album?: string;
  albumartist?: string;
  mb_releasegroupid?: string;
}

export interface AlbumCleanupPlanItem {
  id?: number;
  title?: string;
  artist?: string;
  track?: number;
  disc?: number;
  path?: string;
}

/** Shape of plan_album_cleanup (backend/composite_workflows.py). */
export interface AlbumCleanupPlanResponse extends ApiOkResponse {
  operation_id?: string;
  token?: string;
  status?: string;
  requires_approval?: boolean;
  /** False for a row-only plan: library rows are removed, files stay on disk. */
  delete_files?: boolean;
  album?: AlbumCleanupPlanAlbum;
  items?: AlbumCleanupPlanItem[];
  error?: string;
}

/** Authoritative failure classification from the Web Manager route -- see
 * _classify_album_cleanup_apply_failure in app.py. Only "stale_plan" may
 * ever be presented as "nothing was changed"; "partial_mutation" means at
 * least one file was already deleted before the failure and must be shown
 * distinctly from a plain stale-plan refusal. */
export type AlbumCleanupApplyErrorKind = 'stale_plan' | 'partial_mutation' | 'other';

export interface AlbumCleanupApplyResponse extends ApiOkResponse {
  operation_id?: string;
  status?: string;
  delete_files?: boolean;
  /** Beets item ids whose files were deleted; empty for a row-only plan. */
  deleted?: number[];
  /** Beets item ids whose library rows were removed. */
  removed_item_ids?: number[];
  log?: string[];
  error?: string;
  error_kind?: AlbumCleanupApplyErrorKind;
  mutated?: boolean;
}

export function planAlbumCleanup(albumId: number): Promise<AlbumCleanupPlanResponse> {
  return apiJson<AlbumCleanupPlanResponse>(`/api/albums/${albumId}/cleanup/plan`, jsonRequest('POST'));
}

export function applyAlbumCleanup(operationId: string): Promise<AlbumCleanupApplyResponse> {
  return apiJson<AlbumCleanupApplyResponse>('/api/albums/cleanup/apply', jsonRequest('POST', { operation_id: operationId }));
}

// --- Track Replacement (SEC-002 Wave 17) ---

export interface TrackReplacementMatchingContract {
  identity_source?: string;
  original_recording_id?: string;
  replacement_recording_id?: string;
  original_release_group_id?: string;
  replacement_release_group_id?: string;
  decision_reason?: string;
}

export interface TrackReplacementTransaction {
  id?: string;
  status?: string;
  metadata?: {
    original_path?: string;
    original_stat?: Record<string, unknown>;
    replacement_path?: string;
    replacement_stat?: Record<string, unknown>;
    matching_contract?: TrackReplacementMatchingContract;
    reversibility?: string;
    rollback_available?: boolean;
    quarantine_target?: string;
  };
}

export interface TrackReplacementPlanResponse extends ApiOkResponse {
  operation_id?: string;
  plan?: TrackReplacementTransaction;
  quarantine_target?: string;
  reversibility?: string;
  error?: string;
  code?: string;
}

export interface TrackReplacementApplyResponse extends ApiOkResponse {
  operation_id?: string;
  status?: string;
  quarantined_to?: string;
  replacement_path?: string;
  already_completed?: boolean;
  error?: string;
  code?: string;
  mutated?: boolean;
}

export function planTrackReplacement(
  itemId: number,
  candidatePath: string,
  reason?: string,
): Promise<TrackReplacementPlanResponse> {
  return apiJson<TrackReplacementPlanResponse>(
    `/api/items/${itemId}/replacement/plan`,
    jsonRequest('POST', { candidate_path: candidatePath, reason }),
  );
}

export function applyTrackReplacement(
  itemId: number,
  operationId: string,
): Promise<TrackReplacementApplyResponse> {
  return apiJson<TrackReplacementApplyResponse>(
    `/api/items/${itemId}/replacement/apply`,
    jsonRequest('POST', { operation_id: operationId }),
  );
}

// Track Replacement is genuinely recoverable (server-derived quarantine of
// the original, not an irreversible delete) -- unlike Album Cleanup, this
// can and does return ok:true when the engine's own rollback_available
// metadata says so. Routed through the same generic
// /api/transactions/<id>/rollback dispatcher, which resolves the
// transaction's real mutation_family before picking an executor.
export function rollbackTrackReplacement(operationId: string): Promise<TrackReplacementApplyResponse> {
  return apiJson<TrackReplacementApplyResponse>(`/api/transactions/${operationId}/rollback`, jsonRequest('POST'));
}

export function getReconciliationReviews(): Promise<ReconciliationReviewsResponse> {
  return apiJson<ReconciliationReviewsResponse>('/api/import-reconciliation/reviews');
}

export function resolveReconciliationReview(reviewId: string, choice: ReconciliationChoice): Promise<ApiOkResponse> {
  return apiJson<ApiOkResponse>(
    `/api/import-reconciliation/reviews/${encodeURIComponent(reviewId)}/resolve`,
    jsonRequest('POST', { choice }),
  );
}

// ── Unattended duplicate deletion ─────────────────────────────────────────────

export function getUnattendedCleanup(): Promise<UnattendedCleanupStatus> {
  return apiJson<UnattendedCleanupStatus>('/api/dedup/unattended-cleanup');
}

export function setUnattendedCleanup(enabled: boolean, confirm = '', reason = ''): Promise<UnattendedCleanupStatus> {
  return apiJson<UnattendedCleanupStatus>(
    '/api/dedup/unattended-cleanup',
    jsonRequest('POST', { enabled, confirm, reason }),
  );
}

export function runDuplicateMaintenance(): Promise<JobStartResponse> {
  return apiJson<JobStartResponse>('/api/dedup/maintenance-run', jsonRequest('POST', {}));
}
