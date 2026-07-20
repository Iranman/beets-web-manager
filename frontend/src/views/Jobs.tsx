import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
  Switch,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  applyAlbumFolderCleanupIssue,
  applySafeAlbumFolderCleanup,
  fetchMissingArt,
  rebuildAlbumArt,
  fixGenres,
  getAlbumFolderCleanupReport,
  getJobResult,
  scanAlbumFolders,
  startCleanAll,
  startMbFullSync,
  startMbidStickingRepair,
  startMbsyncAll,
  syncDeleted,
} from '../api/client';
import { apiDelete, apiGet, apiPost } from '../lib/api';
import {
  JOB_FEED_PHASE_ORDER,
  buildJobFeed,
  cleanJobLogLine,
  friendlyJobTitle,
  isTechnicalLogNoise,
  summarizeJobResult,
  type JobFeedItem,
  type JobFeedStatus,
  type PathHit,
} from '../lib/jobFeed';
import { useGlobalJobs, useInterval, useJobPoll } from '../lib/hooks';
import type {
  ApiOkResponse,
  Job,
  JobListResponse,
  JobLogFeedResponse,
  JobState,
  JobStatus,
} from '../types/api';
import { AlbumTracksPanel } from '../features/albumtracks/AlbumTracksPanel';
import { ArtistAliasPanel } from '../features/artistAlias/ArtistAliasPanel';
import { ArtistFoldersPanel } from '../features/artistfolders/ArtistFoldersPanel';
import { DedupPanel } from '../features/dedup/DedupPanel';
import { LibraryHealthPanel } from '../features/libraryHealth/LibraryHealthPanel';
import { LeakedPathsPanel } from '../features/leakedPaths/LeakedPathsPanel';
import { FolderPlaceholdersPanel } from '../features/leakedPaths/FolderPlaceholdersPanel';

// ─── Types ───────────────────────────────────────────────────────────────────

type StatusFilter = 'all' | JobStatus;
type JobOpenOptions = { raw?: boolean };

type LiveJobGroup = {
  job_id: string;
  label: string;
  status: JobStatus;
  lines: string[];
  entries: JobLogFeedResponse['entries'];
  created_at?: number;
  started_at?: number;
  finished_at?: number;
};
type ConfirmAction =
  | { kind: 'kill'; job: Job }
  | { kind: 'clear' }
  | null;

interface WorkflowAction {
  value: string;
  label: string;
  description: string;
  dangerous?: boolean;
  confirm?: string;
}

type MaintenanceTaskStatus = 'pending' | 'running' | 'complete' | 'failed' | 'skipped';

interface MaintenanceTaskView {
  id: string;
  label: string;
  status: MaintenanceTaskStatus;
  detail?: string;
}

interface MaintenanceRunnerResumeSummary {
  resumable?: boolean;
  previous_status?: string;
  completed_count?: number;
  remaining_count?: number;
  next_task?: string;
  next_task_label?: string;
  updated_at?: number;
}

interface MaintenanceRunnerReportResponse extends ApiOkResponse {
  exists: boolean;
  report: Record<string, unknown>;
  resumable?: boolean;
  resume?: MaintenanceRunnerResumeSummary;
}

interface AlbumFolderScanSummary {
  [key: string]: unknown;
  total_issues?: number;
  issues_found?: number;
  safe_fixes?: number;
  needs_review?: number;
  review_needed?: number;
  blocked?: number;
  completed?: number;
  empty_folders?: number;
  duplicate_tracks?: number;
  artwork_moved?: number;
  rgid_missing_stamp?: number;
  placeholder_issues?: number;
  files_moved?: number;
  folders_deleted?: number;
  duplicate_files_quarantined?: number;
  artist_folders_scanned?: number;
  album_folders_scanned?: number;
}

interface AlbumFolderIssue {
  id?: string;
  artist?: string;
  album?: string;
  release_group_id?: string;
  current_folders?: string[];
  current_folder_names?: string[];
  canonical_folder?: string;
  proposed_canonical_folder?: string;
  proposed_action?: string;
  safety?: string;
  risk_level?: string;
  risk_reason?: string;
  status?: string;
  issue_types?: string[];
  blocking_reasons?: string[];
  audio_files_to_move?: Array<Record<string, unknown>>;
  duplicate_files_to_quarantine?: Array<Record<string, unknown>>;
  artwork_files_to_move?: Array<Record<string, unknown>>;
  unknown_files?: Array<Record<string, unknown>>;
  conflicts?: Array<Record<string, unknown>>;
  final_folder_layout?: string[];
  files_to_move?: number;
  files_to_safe_delete?: number;
  folders_to_remove?: string[];
}

interface AlbumFolderJobResult {
  summary?: AlbumFolderScanSummary;
  final_summary?: AlbumFolderScanSummary;
  issues?: AlbumFolderIssue[];
}

// ─── Constants ───────────────────────────────────────────────────────────────

const numberFmt = new Intl.NumberFormat();

const STATUS_OPTIONS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'failed', label: 'Failed' },
  { value: 'success', label: 'Completed' },
  { value: 'cancelled', label: 'Cancelled' },
];
const HISTORY_STATUS_OPTIONS = STATUS_OPTIONS.filter((option) => option.value !== 'running');

const MAINTENANCE_TASKS: MaintenanceTaskView[] = [
  { id: 'library_health', label: 'DB Health Check', status: 'pending' },
  { id: 'missing_files', label: 'Missing Files Scan', status: 'pending' },
  { id: 'artist_alias', label: 'Artist Alias Check', status: 'pending' },
  { id: 'artist_folder_merge', label: 'Artist Folder Merge', status: 'pending' },
  { id: 'release_group_merge', label: 'Release Group Merge', status: 'pending' },
  { id: 'duplicates', label: 'Duplicate Track Scan', status: 'pending' },
  { id: 'folder_scan', label: 'Folder Name Scan', status: 'pending' },
  { id: 'folder_safe_renames', label: 'Folder Safe Renames', status: 'pending' },
  { id: 'artwork', label: 'Artwork Fetch', status: 'pending' },
  { id: 'genres', label: 'Genre Tagging', status: 'pending' },
  { id: 'final_verification', label: 'Final Verification', status: 'pending' },
  { id: 'stale_jobs', label: 'Stale Job Cleanup', status: 'pending' },
  { id: 'playlist_refs', label: 'Playlist Reference Check', status: 'pending' },
];

const CLEAN_ALL_PIPELINE = [
  'Scanning',
  'Fingerprinting',
  'Matching',
  'Verifying',
  'Repairing',
  'Replacing',
  'Organizing',
  'Syncing',
];

const CLEAN_ALL_COUNT_LABELS = [
  ['scanned', 'Scanned'],
  ['verified', 'Verified'],
  ['fixed', 'Fixed'],
  ['replaced', 'Replaced'],
  ['removed', 'Removed'],
  ['needs_submission', 'Needs Submission'],
  ['needs_review', 'Needs Review'],
  ['failed', 'Failed'],
] as const;

type CleanAllCountKey = typeof CLEAN_ALL_COUNT_LABELS[number][0];


const MAX_VISIBLE_ITEMS_PER_STEP = 8;


// ─── Storage helpers ──────────────────────────────────────────────────────────

// ─── Utilities ───────────────────────────────────────────────────────────────

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(' ');
}

function formatClock(ts?: number) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDuration(job: Job) {
  if (!job.started_at) return '';
  const end = job.finished_at ?? Date.now() / 1000;
  const s = Math.max(0, end - job.started_at);
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function groupFeedEntries(feedEntries: JobLogFeedResponse['entries']): LiveJobGroup[] {
  const map = new Map<string, LiveJobGroup>();
  for (const entry of feedEntries) {
    const key = entry.job_id;
    if (!map.has(key)) {
      map.set(key, {
        job_id: key,
        label: entry.label || key,
        status: entry.status,
        lines: [],
        entries: [],
        created_at: entry.created_at,
        started_at: entry.started_at,
        finished_at: entry.finished_at,
      });
    }
    const group = map.get(key)!;
    group.status = entry.status;
    group.started_at = entry.started_at ?? group.started_at;
    group.finished_at = entry.finished_at ?? group.finished_at;
    group.lines.push(entry.message);
    group.entries.push(entry);
  }
  return [...map.values()].reverse();
}

function feedGroupForJob(job: Job | null, groups: LiveJobGroup[]) {
  if (!job) return null;
  return groups.find((group) => group.job_id === job.job_id || group.label === job.label) ?? null;
}

function jobMatches(job: Job, status: StatusFilter, query: string) {
  if (status !== 'all' && job.status !== status) return false;
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const meta = job.metadata ? Object.values(job.metadata).join(' ') : '';
  return `${job.job_id} ${job.label} ${job.status} ${meta}`.toLowerCase().includes(needle);
}

function isMaintenanceRunnerJob(job: Job) {
  return job.metadata?.type === 'maintenance-runner';
}

function maintenanceStatusLabel(job: Job | null) {
  if (!job) return 'Idle';
  const state = structuredState(job);
  const raw = String(state?.maintenance_status || '').toLowerCase();
  if (raw === 'complete') return 'Complete';
  if (raw === 'partial') return 'Partial';
  if (raw === 'completed_with_item_failures') return 'Completed with issues';
  if (raw === 'failed') return 'Failed';
  if (job.status === 'running' || raw === 'running') return 'Running';
  if (job.status === 'failed' || job.status === 'killed') return 'Failed';
  if (job.status === 'success') return 'Complete';
  return 'Idle';
}

function taskStatus(value: unknown): MaintenanceTaskStatus {
  return value === 'running' || value === 'complete' || value === 'failed' || value === 'skipped'
    ? value
    : 'pending';
}

function maintenanceTasksFromState(state: JobState | null | undefined): MaintenanceTaskView[] {
  const raw = state?.maintenance_tasks;
  if (!Array.isArray(raw)) return MAINTENANCE_TASKS;
  const byId = new Map<string, MaintenanceTaskView>();
  for (const item of raw) {
    if (!isRecord(item)) continue;
    const id = String(item.id || '');
    const label = String(item.label || '');
    if (!id || !label) continue;
    byId.set(id, {
      id,
      label,
      status: taskStatus(item.status),
      detail: typeof item.detail === 'string' ? item.detail : undefined,
    });
  }
  return MAINTENANCE_TASKS.map((task) => byId.get(task.id) ?? task);
}

function maintenanceProgress(job: Job | null) {
  if (!job) return 0;
  const state = structuredState(job);
  const raw = state?.progress_percent;
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return Math.max(0, Math.min(100, raw));
  }
  if (job.status === 'success') return 100;
  if (job.status === 'failed' || job.status === 'killed') return 100;
  return job.status === 'running' ? 5 : 0;
}

function cleanAllCountsFromState(state: JobState | null | undefined): Record<CleanAllCountKey, number> {
  const counts = Object.fromEntries(CLEAN_ALL_COUNT_LABELS.map(([key]) => [key, 0])) as Record<CleanAllCountKey, number>;
  const raw = state?.clean_all_counts;
  if (!isRecord(raw)) return counts;
  for (const [key] of CLEAN_ALL_COUNT_LABELS) {
    const value = raw[key];
    counts[key] = typeof value === 'number' && Number.isFinite(value) ? value : 0;
  }
  return counts;
}

function cleanAllPipelineFromState(state: JobState | null | undefined) {
  const raw = state?.clean_all_pipeline;
  if (!Array.isArray(raw)) return CLEAN_ALL_PIPELINE;
  const steps = raw.filter((step): step is string => typeof step === 'string' && step.trim().length > 0);
  return steps.length ? steps : CLEAN_ALL_PIPELINE;
}
function formatDateTime(ts?: number | null) {
  if (!ts) return 'Never';
  return new Date(ts * 1000).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function nestedRecord(record: Record<string, unknown> | null | undefined, key: string): Record<string, unknown> | null {
  if (!record) return null;
  const value = record[key];
  return isRecord(value) ? value : null;
}

function reportNumber(record: Record<string, unknown> | null | undefined, key: string) {
  const value = record?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function reportBoolean(record: Record<string, unknown> | null | undefined, key: string) {
  return record?.[key] === true;
}

function structuredState(job?: Job | null): JobState | null {
  return isRecord(job?.state) ? job.state as JobState : null;
}

function stateText(state: JobState | null | undefined, ...keys: string[]) {
  if (!state) return '';
  for (const key of keys) {
    const value = state[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  }
  return '';
}

function stateNumber(state: JobState | null | undefined, ...keys: string[]) {
  if (!state) return null;
  for (const key of keys) {
    const value = state[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return null;
}

function structuredProgressText(job: Job) {
  const state = structuredState(job);
  const scanned = stateNumber(state, 'scanned_count', 'scanned');
  const total = stateNumber(state, 'total_count', 'total');
  if (scanned !== null && total !== null && total > 0) return `${numberFmt.format(scanned)} / ${numberFmt.format(total)} scanned`;
  if (scanned !== null) return `${numberFmt.format(scanned)} scanned`;
  return '';
}

function latestReadableLine(job: Job, max = 130) {
  const state = structuredState(job);
  const task = stateText(state, 'current_task');
  const result = stateText(state, 'current_result');
  if (task || result) {
    const line = [task, result].filter(Boolean).join(' · ');
    return line.length > max ? `${line.slice(0, max - 3)}...` : line;
  }
  const line = (job.log ?? [])
    .map(cleanJobLogLine)
    .filter((entry) => entry && !isTechnicalLogNoise(entry))
    .at(-1);
  return line ? (line.length > max ? `${line.slice(0, max - 3)}...` : line) : '';
}

function jobProgressText(job: Job) {
  const structured = structuredProgressText(job);
  if (structured) return structured;
  const result = job.result ?? {};
  const numbers = (key: string) => {
    const value = result[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  };
  const scanned = numbers('scanned') ?? numbers('scanned_count') ?? numbers('total_scanned');
  const total = numbers('total') ?? numbers('total_count');
  if (scanned !== null && total !== null && total > 0) return `${numberFmt.format(scanned)} / ${numberFmt.format(total)} scanned`;
  const logLine = [...(job.log ?? []).map(cleanJobLogLine)].reverse().find((line) => /\b\d+\s*\/\s*\d+\b/.test(line));
  return logLine?.match(/\b\d+\s*\/\s*\d+\b/)?.[0] ?? '';
}

// ─── useWorkflowRunner ───────────────────────────────────────────────────────

function useWorkflowRunner(onStarted: () => Promise<void>) {
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [errorMap, setErrorMap] = useState<Record<string, string>>({});

  const run = useCallback(async (key: string, fn: () => Promise<unknown>) => {
    setBusyMap((m) => ({ ...m, [key]: true }));
    setErrorMap((m) => ({ ...m, [key]: '' }));
    try {
      await fn();
      await onStarted();
    } catch (err) {
      setErrorMap((m) => ({ ...m, [key]: err instanceof Error ? err.message : String(err) }));
    } finally {
      setBusyMap((m) => ({ ...m, [key]: false }));
    }
  }, [onStarted]);

  const busy = useCallback((key: string) => busyMap[key] ?? false, [busyMap]);
  const err = useCallback((key: string) => errorMap[key] ?? '', [errorMap]);
  return { run, busy, err };
}

// ─── WorkflowCard ─────────────────────────────────────────────────────────────

function WorkflowCard({
  title,
  description,
  actions,
  busy,
  error,
  onRun,
  primaryCount,
}: {
  title: string;
  description: string;
  actions: WorkflowAction[];
  busy: boolean;
  error: string;
  onRun: (action: WorkflowAction) => void;
  primaryCount?: number;
}) {
  const [sel, setSel] = useState(0);
  const [showMore, setShowMore] = useState(false);
  const visibleCount = primaryCount && !showMore ? primaryCount : actions.length;
  const visibleActions = actions.slice(0, visibleCount);
  const action = actions[sel] ?? actions[0];

  useEffect(() => {
    if (primaryCount && !showMore && sel >= primaryCount) setSel(0);
  }, [primaryCount, showMore, sel]);

  const handleRun = () => {
    if (action.dangerous && action.confirm && !window.confirm(action.confirm)) return;
    onRun(action);
  };

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-graphite-800 bg-graphite-900/60 p-4">
      <div>
        <h3 className="text-sm font-semibold text-zinc-100">{title}</h3>
        <p className="mt-0.5 text-xs leading-relaxed text-zinc-500">{description}</p>
      </div>
      {/* Mode selector */}
      <div className="flex flex-wrap gap-1.5">
        {visibleActions.map((a, idx) => (
          <button
            key={a.value}
            type="button"
            onClick={() => setSel(idx)}
            className={cx(
              'rounded px-2.5 py-1 text-xs font-medium transition-colors',
              sel === idx
                ? a.dangerous
                  ? 'bg-red-800/60 text-red-100 ring-1 ring-red-700'
                  : 'bg-red-600/70 text-white ring-1 ring-red-500'
                : 'bg-graphite-800 text-zinc-400 hover:bg-graphite-700 hover:text-zinc-200',
            )}
          >
            {a.label}
          </button>
        ))}
        {primaryCount && actions.length > primaryCount ? (
          <button
            type="button"
            className="rounded px-2.5 py-1 text-xs font-medium text-zinc-500 underline hover:text-zinc-300"
            onClick={() => setShowMore((value) => !value)}
          >
            {showMore ? 'Fewer options' : 'More options'}
          </button>
        ) : null}
      </div>
      {/* Selected action description */}
      <p className="min-h-[2.2rem] text-xs leading-relaxed text-zinc-500">{action.description}</p>
      {error ? <p className="text-xs text-red-400">{error}</p> : null}
      <div className="flex items-center gap-2">
        <Button color={action.dangerous ? 'error' : 'primary'} disabled={busy} size="small" variant="contained" onClick={handleRun}>
          {busy ? 'Starting…' : 'Run'}
        </Button>
        {action.dangerous && <span className="text-[0.68rem] font-semibold text-red-400">Destructive — confirm required</span>}
      </div>
    </div>
  );
}

// ─── Selector bar ─────────────────────────────────────────────────────────────

function SelectorBar({
  options,
  selected,
  onChange,
}: {
  options: string[];
  selected: number;
  onChange: (idx: number) => void;
}) {
  return (
    <div className="mb-4 flex flex-wrap gap-1.5">
      {options.map((label, idx) => (
        <button
          key={label}
          type="button"
          onClick={() => onChange(idx)}
          className={cx(
            'rounded px-3 py-1.5 text-xs font-medium transition-colors',
            selected === idx
              ? 'bg-red-500/15 text-zinc-100 ring-1 ring-red-400/40'
              : 'bg-graphite-900/60 text-zinc-400 hover:bg-graphite-800 hover:text-zinc-200',
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

// ─── Tab panel: MusicBrainz ───────────────────────────────────────────────────

function MusicBrainzPanel({ onStarted }: { onStarted: () => Promise<void> }) {
  const { run, busy, err } = useWorkflowRunner(onStarted);

  const actions: WorkflowAction[] = [
    { value: 'sync', label: 'Sync metadata', description: 'Run beet mbsync for albums that already have MusicBrainz compatibility IDs.' },
    { value: 'audit', label: 'Audit IDs only', description: 'Scan for stale or mismatched MusicBrainz IDs and report findings without making any changes.' },
    { value: 'repair', label: 'Repair IDs', description: 'Fix stale or mismatched MusicBrainz IDs in the Beets DB and write corrected tags to files.', dangerous: true, confirm: 'Repair stale/mismatched MusicBrainz IDs and rewrite tags? This modifies the Beets DB and audio file tags.' },
    { value: 'rebuild', label: 'Full rebuild', description: 'Run beet mbsync across the full library, repair all track IDs, and rewrite every tag.', dangerous: true, confirm: 'Run a full MusicBrainz rebuild across the entire library? This will rewrite tags on all albums.' },
  ];

  const apiMap: Record<string, () => Promise<unknown>> = {
    sync: startMbsyncAll,
    audit: () => startMbidStickingRepair({ dryRun: true, repairTracks: true, writeTags: false, limit: 100 }),
    repair: () => startMbidStickingRepair({ dryRun: false, repairTracks: true, writeTags: true, limit: 100 }),
    rebuild: () => startMbFullSync({ repairTracks: true, writeTags: true }),
  };

  return (
    <div className="max-w-xl">
      <WorkflowCard
        title="Repair Metadata"
        description="Sync MusicBrainz metadata first. Audits, ID repair, and full rebuild stay under More options."
        actions={actions}
        busy={busy('mb')}
        error={err('mb')}
        onRun={(a) => void run('mb', apiMap[a.value] ?? (() => Promise.resolve()))}
        primaryCount={1}
      />
    </div>
  );
}

// ─── Tab panel: Duplicates ────────────────────────────────────────────────────

function DuplicatesPanel() {
  const [view, setView] = useState(0);
  const views = ['Tracks & Albums', 'Artist Folders & MBID Variants'];

  return (
    <div>
      <div className="mb-1 text-xs text-zinc-500">Duplicate type</div>
      <SelectorBar options={views} selected={view} onChange={setView} />
      {view === 0 && <DedupPanel />}
      {view === 1 && <ArtistFoldersPanel />}
    </div>
  );
}

// ─── Tab panel: Genres ────────────────────────────────────────────────────────

function GenresPanel({ onStarted }: { onStarted: () => Promise<void> }) {
  const { run, busy, err } = useWorkflowRunner(onStarted);

  const actions: WorkflowAction[] = [
    { value: 'missing', label: 'Tag missing genres', description: 'Run lastgenre on all albums that currently have no genre tag.' },
    { value: 'missing-ai', label: 'Tag missing + AI fallback', description: 'Run lastgenre for missing genres, then use AI for albums still without a tag.' },
    { value: 'force', label: 'Force re-tag all', description: 'Overwrite all existing genre tags across the entire library using lastgenre.', dangerous: true, confirm: 'Overwrite all existing genre tags across the library using lastgenre? This will modify tags on all albums.' },
  ];

  const apiMap: Record<string, () => Promise<unknown>> = {
    missing: () => fixGenres({ force: false, useAi: false }),
    'missing-ai': () => fixGenres({ force: false, useAi: true }),
    force: () => fixGenres({ force: true, useAi: false }),
  };

  return (
    <div className="max-w-xl">
      <WorkflowCard
        title="Tag missing genres"
        description="Fill missing genre tags. AI fallback and force re-tag all stay under More options."
        actions={actions}
        busy={busy('genres')}
        error={err('genres')}
        onRun={(a) => void run('genres', apiMap[a.value] ?? (() => Promise.resolve()))}
        primaryCount={1}
      />
    </div>
  );
}

// ─── Tab panel: Artwork ───────────────────────────────────────────────────────

function ArtworkPanel({ onStarted }: { onStarted: () => Promise<void> }) {
  const { run, busy, err } = useWorkflowRunner(onStarted);

  const actions: WorkflowAction[] = [
    { value: 'fetch-missing', label: 'Fetch missing art', description: 'Run fetchart for albums without a local cover image, with Discogs as fallback.' },
    {
      value: 'full-rebuild',
      label: 'Full rebuild',
      description: 'Quarantine existing album art, fetch fresh covers for every album, and restore the old art if no replacement is confirmed.',
      dangerous: true,
      confirm: 'Rebuild album art for the entire library? Existing cover files will be quarantined and replaced when fresh art is confirmed.',
    },
  ];

  const apiMap: Record<string, () => Promise<unknown>> = {
    'fetch-missing': fetchMissingArt,
    'full-rebuild': rebuildAlbumArt,
  };

  return (
    <div className="max-w-xl">
      <WorkflowCard
        title="Repair Artwork"
        description="Fetch missing covers or rebuild all album art with a restore safety net. Per-album upload and replace actions stay in Library."
        actions={actions}
        busy={busy('art')}
        error={err('art')}
        onRun={(a) => void run('art', apiMap[a.value] ?? fetchMissingArt)}
      />
    </div>
  );
}

function MissingFilesSyncPanel({ onStarted }: { onStarted: () => Promise<void> }) {
  const { run, busy, err } = useWorkflowRunner(onStarted);
  const actions: WorkflowAction[] = [
    {
      value: 'preview',
      label: 'Preview sync',
      description: 'Scan Beets DB rows whose audio files no longer exist. This is read-only and does not change the database.',
    },
    {
      value: 'apply',
      label: 'Apply confirmed sync',
      description: 'Remove DB rows only for files that are still missing when the backend rescans. This does not delete files.',
      dangerous: true,
      confirm: 'Remove Beets DB rows for files that are still missing on disk? Run preview first and confirm the result before applying.',
    },
  ];
  const apiMap: Record<string, () => Promise<unknown>> = {
    preview: () => syncDeleted({ dryRun: true }),
    apply: () => syncDeleted({ dryRun: false, confirmed: true }),
  };
  return (
    <div className="max-w-xl">
      <WorkflowCard
        title="Check Database Health"
        description="Preview missing files first, then apply confirmed sync only after the backend rescans."
        actions={actions}
        busy={busy('missing-files-sync')}
        error={err('missing-files-sync')}
        onRun={(a) => void run('missing-files-sync', apiMap[a.value] ?? (() => Promise.resolve()))}
      />
    </div>
  );
}

// ─── AlbumFolderCleanupPanel ──────────────────────────────────────────────────

type AlbumIssueFilter = 'active' | 'safe' | 'review' | 'blocked' | 'completed';

function asCount(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function countSummary(summary: AlbumFolderScanSummary | null, key: keyof AlbumFolderScanSummary) {
  return asCount(summary?.[key]);
}

function issueKind(issue: AlbumFolderIssue) {
  const labels = (issue.issue_types ?? []).map((value) => value.replace(/_/g, ' '));
  return labels.length ? labels.join(', ') : 'Album folder cleanup';
}

function issueRisk(issue: AlbumFolderIssue) {
  return issue.risk_level || issue.safety || 'Needs review';
}

function issueStatus(issue: AlbumFolderIssue) {
  return issue.status || (issue.safety === 'Completed' ? 'Completed' : 'Active');
}

function issueReason(issue: AlbumFolderIssue) {
  if (issue.risk_reason) return issue.risk_reason;
  const blocker = issue.blocking_reasons?.[0];
  if (blocker) return `${issueRisk(issue)}: ${blocker}.`;
  if (issueRisk(issue) === 'Safe') return 'Safe merge: same Release Group ID, no file conflicts.';
  return `${issueRisk(issue)}.`;
}

function opText(row: Record<string, unknown>, key: 'source' | 'target') {
  return typeof row[key] === 'string' ? row[key] as string : '';
}

function AlbumFolderIssueDialog({
  issue,
  busy,
  onClose,
  onApply,
  onSkip,
  onIgnore,
}: {
  issue: AlbumFolderIssue | null;
  busy: boolean;
  onClose: () => void;
  onApply: (issue: AlbumFolderIssue) => void;
  onSkip: (issue: AlbumFolderIssue) => void;
  onIgnore: (issue: AlbumFolderIssue) => void;
}) {
  const current = issue?.current_folders ?? [];
  const canonical = issue?.canonical_folder || issue?.proposed_canonical_folder || '';
  const audioMoves = issue?.audio_files_to_move ?? [];
  const duplicateFiles = issue?.duplicate_files_to_quarantine ?? [];
  const artworkMoves = issue?.artwork_files_to_move ?? [];
  const unknownFiles = issue?.unknown_files ?? [];
  const conflicts = issue?.conflicts ?? [];
  const layout = issue?.final_folder_layout ?? [];
  const blocked = issue ? issueRisk(issue) === 'Blocked' : false;

  const ListBlock = ({ title, rows, empty }: { title: string; rows: Array<Record<string, unknown>>; empty: string }) => (
    <div className="rounded border border-graphite-800 bg-graphite-950/40 p-3">
      <div className="mb-1 text-xs font-semibold text-zinc-300">{title}</div>
      {rows.length ? (
        <div className="space-y-1">
          {rows.slice(0, 12).map((row, idx) => (
            <div key={`${title}-${idx}`} className="grid gap-1 text-[0.7rem] text-zinc-500 md:grid-cols-2">
              <div className="truncate" title={opText(row, 'source')}>{opText(row, 'source') || String(row.relative_path ?? '')}</div>
              <div className="truncate text-zinc-400" title={opText(row, 'target')}>{opText(row, 'target') || String(row.reason ?? '')}</div>
            </div>
          ))}
          {rows.length > 12 ? <div className="text-[0.68rem] text-zinc-600">+ {rows.length - 12} more</div> : null}
        </div>
      ) : (
        <div className="text-[0.7rem] text-zinc-600">{empty}</div>
      )}
    </div>
  );

  return (
    <Dialog className="relative z-50" open={Boolean(issue)} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="max-h-[88vh] w-full max-w-4xl overflow-y-auto rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">Cleanup Preview</DialogTitle>
          {issue ? (
            <div className="mt-4 space-y-4">
              <div className="grid gap-2 rounded border border-graphite-800 bg-graphite-950/40 p-3 text-xs text-zinc-400 md:grid-cols-2">
                <div><span className="text-zinc-500">Issue:</span> <span className="text-zinc-200">{issueKind(issue)}</span></div>
                <div><span className="text-zinc-500">Risk:</span> <span className="text-zinc-200">{issueRisk(issue)}</span></div>
                <div><span className="text-zinc-500">Artist:</span> <span className="text-zinc-200">{issue.artist || 'Unknown artist'}</span></div>
                <div><span className="text-zinc-500">Album:</span> <span className="text-zinc-200">{issue.album || 'Unknown album'}</span></div>
                <div className="md:col-span-2"><span className="text-zinc-500">Release Group ID:</span> <span className="font-mono text-zinc-200">{issue.release_group_id || 'Missing'}</span></div>
                <div className="md:col-span-2"><span className="text-zinc-500">Reason:</span> <span className="text-zinc-200">{issueReason(issue)}</span></div>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded border border-graphite-800 bg-graphite-950/40 p-3">
                  <div className="mb-1 text-xs font-semibold text-zinc-300">Current/source folder</div>
                  {current.map((path) => <div key={path} className="truncate text-xs text-zinc-500" title={path}>{path}</div>)}
                </div>
                <div className="rounded border border-graphite-800 bg-graphite-950/40 p-3">
                  <div className="mb-1 text-xs font-semibold text-zinc-300">Canonical target folder</div>
                  <div className="truncate text-xs text-zinc-500" title={canonical}>{canonical || 'Missing target'}</div>
                </div>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <ListBlock title="Audio files to move" rows={audioMoves} empty="No audio moves planned." />
                <ListBlock title="Duplicate files to quarantine" rows={duplicateFiles} empty="No duplicate file quarantine planned." />
                <ListBlock title="Artwork files to move" rows={artworkMoves} empty="No artwork moves planned." />
                <ListBlock title="Unknown files / blockers" rows={[...unknownFiles, ...conflicts]} empty="No blockers found." />
              </div>
              <div className="rounded border border-graphite-800 bg-graphite-950/40 p-3">
                <div className="mb-1 text-xs font-semibold text-zinc-300">Final expected folder layout</div>
                {layout.length ? (
                  <div className="grid gap-1 text-[0.7rem] text-zinc-500 md:grid-cols-2">
                    {layout.slice(0, 20).map((path) => <div key={path} className="truncate">{path}</div>)}
                  </div>
                ) : (
                  <div className="text-[0.7rem] text-zinc-600">Layout will be verified after apply.</div>
                )}
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <Button size="small" variant="outlined" onClick={() => onSkip(issue)}>Skip issue</Button>
                <Button size="small" variant="outlined" onClick={() => onIgnore(issue)}>Ignore issue</Button>
                <Button disabled={busy || blocked} size="small" variant="contained" onClick={() => onApply(issue)}>
                  {busy ? 'Applying...' : 'Apply fix'}
                </Button>
                <Button disabled={busy || blocked} size="small" variant="contained" onClick={() => onApply(issue)}>
                  Apply selected fixes
                </Button>
                <Button size="small" variant="text" onClick={onClose}>Close</Button>
              </div>
            </div>
          ) : null}
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function AlbumFolderCleanupPanel({
  onStarted,
  failedCount,
  onReviewFailed,
}: {
  onStarted: () => Promise<void>;
  failedCount: number;
  onReviewFailed: () => void;
}) {
  const [scanJobId, setScanJobId] = useState<string | null>(null);
  const [applyJobId, setApplyJobId] = useState<string | null>(null);
  const [issueApplyJobId, setIssueApplyJobId] = useState<string | null>(null);
  const [summary, setSummary] = useState<AlbumFolderScanSummary | null>(null);
  const [issues, setIssues] = useState<AlbumFolderIssue[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showDetails, setShowDetails] = useState(false);
  const [filter, setFilter] = useState<AlbumIssueFilter>('active');
  const [selectedIssue, setSelectedIssue] = useState<AlbumFolderIssue | null>(null);
  const [lastScanAt, setLastScanAt] = useState<number | null>(null);
  const prevScanStatus = useRef<string | null>(null);
  const prevApplyStatus = useRef<string | null>(null);
  const prevIssueApplyStatus = useRef<string | null>(null);

  const { job: scanJob } = useJobPoll(scanJobId);
  const { job: applyJob } = useJobPoll(applyJobId);
  const { job: issueApplyJob } = useJobPoll(issueApplyJobId);

  const doScan = useCallback(async () => {
    setError('');
    setBusy(true);
    setSummary(null);
    setIssues([]);
    prevScanStatus.current = null;
    try {
      const res = await scanAlbumFolders();
      setScanJobId(res.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, []);

  const doApply = useCallback(async () => {
    setError('');
    setBusy(true);
    prevApplyStatus.current = null;
    try {
      const selectedSafeIssues = issues.filter((issue) => issueRisk(issue) === 'Safe' && issueStatus(issue) !== 'Completed');
      const folders = selectedSafeIssues.reduce((total, issue) => total + (issue.folders_to_remove?.length ?? 0), 0);
      const filesToMove = selectedSafeIssues.reduce((total, issue) => total + asCount(issue.files_to_move), 0);
      const duplicates = selectedSafeIssues.reduce((total, issue) => total + asCount(issue.files_to_safe_delete), 0);
      const art = selectedSafeIssues.reduce((total, issue) => total + (issue.artwork_files_to_move?.length ?? 0), 0);
      const count = selectedSafeIssues.length || countSummary(summary, 'safe_fixes');
      const ok = window.confirm(
        `Auto-fix ${numberFmt.format(count)} safe issue(s)?\n\n` +
        `${numberFmt.format(folders)} folder(s) to merge/remove\n` +
        `${numberFmt.format(filesToMove)} file(s) to move\n` +
        `${numberFmt.format(duplicates)} duplicate file(s) to quarantine\n` +
        `${numberFmt.format(art)} artwork file(s) to move\n\n` +
        'Needs Review and Blocked items will not be applied.'
      );
      if (!ok) {
        setBusy(false);
        return;
      }
      const res = await applySafeAlbumFolderCleanup();
      setApplyJobId(res.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, [issues, summary]);

  const applyIssue = useCallback(async (issue: AlbumFolderIssue) => {
    if (!issue.id) return;
    if (issueRisk(issue) === 'Needs review') {
      const ok = window.confirm('Apply this reviewed cleanup issue? The backend will re-check conflicts before moving anything.');
      if (!ok) return;
    }
    setError('');
    setBusy(true);
    prevIssueApplyStatus.current = null;
    try {
      const res = await applyAlbumFolderCleanupIssue(issue.id);
      setIssueApplyJobId(res.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!scanJob || scanJob.status === prevScanStatus.current) return;
    prevScanStatus.current = scanJob.status;
    if (scanJob.status === 'success') {
      const result = getJobResult<AlbumFolderJobResult>(scanJob);
      const nextSummary = result?.summary ?? result?.final_summary ?? null;
      if (nextSummary) setSummary(nextSummary);
      setIssues(result?.issues ?? []);
      setLastScanAt(Date.now());
      setBusy(false);
      void onStarted();
    } else if (scanJob.status === 'failed' || scanJob.status === 'killed') {
      setBusy(false);
    }
  }, [scanJob?.status, onStarted]);

  useEffect(() => {
    if (!applyJob || applyJob.status === prevApplyStatus.current) return;
    prevApplyStatus.current = applyJob.status;
    if (applyJob.status === 'success' || applyJob.status === 'failed' || applyJob.status === 'killed') {
      setBusy(false);
      if (applyJob.status === 'success') {
        const result = getJobResult<AlbumFolderJobResult>(applyJob);
        const nextSummary = result?.summary ?? result?.final_summary ?? null;
        if (nextSummary) setSummary(nextSummary);
        if (result?.issues) setIssues(result.issues);
        void doScan();
      }
    }
  }, [applyJob?.status, doScan]);

  useEffect(() => {
    if (!issueApplyJob || issueApplyJob.status === prevIssueApplyStatus.current) return;
    prevIssueApplyStatus.current = issueApplyJob.status;
    if (issueApplyJob.status === 'success' || issueApplyJob.status === 'failed' || issueApplyJob.status === 'killed') {
      setBusy(false);
      if (issueApplyJob.status === 'success') {
        const result = getJobResult<AlbumFolderJobResult>(issueApplyJob);
        const nextSummary = result?.summary ?? result?.final_summary ?? null;
        if (nextSummary) setSummary(nextSummary);
        if (result?.issues) setIssues(result.issues);
        setSelectedIssue(null);
        void doScan();
      }
    }
  }, [issueApplyJob?.status, doScan]);

  const openReport = useCallback(async () => {
    setError('');
    try {
      const res = await getAlbumFolderCleanupReport();
      const report = (res.report ?? {}) as AlbumFolderJobResult;
      const nextSummary = report.summary ?? report.final_summary ?? null;
      if (nextSummary) setSummary(nextSummary);
      setIssues(report.issues ?? []);
      setShowDetails(true);
      if (!res.exists) setError('No album-folder cleanup report has been saved yet. Run a scan first.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  function count(key: keyof AlbumFolderScanSummary) {
    return countSummary(summary, key);
  }
  const totalIssues = count('total_issues') || count('issues_found');
  const safeCount = count('safe_fixes');
  const reviewCount = count('needs_review') || count('review_needed');
  const blockedCount = count('blocked');
  const completedCount = count('completed') || issues.filter((issue) => issueStatus(issue) === 'Completed').length;
  const running = scanJob?.status === 'running' || applyJob?.status === 'running' || issueApplyJob?.status === 'running';
  const currentTaskLine = running
    ? (applyJob?.status === 'running'
      ? 'Applying safe album-folder fixes...'
      : issueApplyJob?.status === 'running'
        ? 'Applying selected cleanup issue...'
        : latestReadableLine(scanJob!) || 'Scanning album folders...')
    : null;
  const stats = [
    { label: 'Safe fixes', value: safeCount, tone: safeCount ? 'text-emerald-300 border-emerald-900/50 bg-emerald-950/15' : 'text-zinc-500 border-graphite-800 bg-graphite-950/40' },
    { label: 'Needs review', value: reviewCount, tone: reviewCount ? 'text-amber-300 border-amber-900/50 bg-amber-950/15' : 'text-zinc-500 border-graphite-800 bg-graphite-950/40' },
    { label: 'Blocked', value: blockedCount, tone: blockedCount ? 'text-rose-300 border-rose-900/50 bg-rose-950/15' : 'text-zinc-500 border-graphite-800 bg-graphite-950/40' },
    { label: 'Completed', value: completedCount, tone: completedCount ? 'text-sky-300 border-sky-900/50 bg-sky-950/15' : 'text-zinc-500 border-graphite-800 bg-graphite-950/40' },
  ];
  const detailStats = [
    { label: 'Empty folders', value: count('empty_folders'), tone: 'text-sky-300 border-sky-900/50 bg-sky-950/15' },
    { label: 'Duplicate tracks', value: count('duplicate_tracks'), tone: 'text-orange-300 border-orange-900/50 bg-orange-950/15' },
    { label: 'Artwork to move', value: count('artwork_moved'), tone: 'text-red-300 border-red-900/50 bg-red-950/15' },
    { label: 'Files to move', value: count('files_moved'), tone: 'text-zinc-300 border-graphite-800 bg-graphite-950/40' },
    { label: 'Folders removed', value: count('folders_deleted'), tone: 'text-zinc-300 border-graphite-800 bg-graphite-950/40' },
  ].filter((stat) => stat.value > 0);
  const visibleIssues = issues.filter((issue) => {
    const risk = issueRisk(issue);
    const status = issueStatus(issue);
    if (filter === 'active') return status !== 'Completed' && status !== 'Ignored' && status !== 'Skipped';
    if (filter === 'safe') return risk === 'Safe';
    if (filter === 'review') return risk === 'Needs review';
    if (filter === 'blocked') return risk === 'Blocked';
    return status === 'Completed';
  });
  const markLocal = useCallback((issue: AlbumFolderIssue, status: 'Skipped' | 'Ignored') => {
    setIssues((current) => current.map((row) => row.id === issue.id ? { ...row, status, risk_reason: `${status}.` } : row));
    setSelectedIssue(null);
  }, []);

  return (
    <div id="library-cleanup" className="rounded-lg border border-graphite-800 bg-graphite-900/60 p-4 space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-zinc-100">Cleanup Report</h3>
        <p className="mt-0.5 text-xs leading-relaxed text-zinc-500">
          Review the latest folder cleanup report and apply safe folder fixes when a report exists.
        </p>
      </div>

      {error ? <Alert severity="error" sx={{ fontSize: '0.75rem', py: 0.5 }}>{error}</Alert> : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button size="small" variant="contained" disabled={busy} onClick={() => void doScan()}>
          Refresh Folder Report
        </Button>
        {summary && safeCount > 0 && (
          <Button
            size="small"
            variant="outlined"
            color="success"
            disabled={busy}
            onClick={() => void doApply()}
          >
            Auto-Fix Safe Issues
          </Button>
        )}
        {issues.length > 0 ? (
          <Button size="small" variant="outlined" onClick={() => setShowDetails((v) => !v)}>
            Review Issues
          </Button>
        ) : null}
        <Button disabled={!failedCount} size="small" variant="outlined" onClick={onReviewFailed}>
          Retry Failed Jobs
        </Button>
        <Button size="small" variant="text" onClick={() => void openReport()}>
          Cleanup Report
        </Button>
        {lastScanAt && (
          <span className="text-[0.68rem] text-zinc-600">
            Scanned {new Date(lastScanAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}
      </div>

      {busy && <LinearProgress sx={{ borderRadius: 1 }} />}
      {currentTaskLine && <div className="text-xs text-zinc-400 truncate">{currentTaskLine}</div>}

      {summary && (
        <div className="space-y-2">
          <div className={cx('inline-flex rounded border px-2.5 py-1 text-xs', totalIssues > 0 ? 'border-amber-900/60 bg-amber-950/20 text-amber-200' : 'border-emerald-900/50 bg-emerald-950/15 text-emerald-200')}>
            {numberFmt.format(totalIssues)} issue{totalIssues === 1 ? '' : 's'} found
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
            {stats.map((stat) => (
              <div key={stat.label} className={cx('rounded border px-2.5 py-1.5', stat.tone)}>
                <div className="text-sm font-semibold tabular-nums">{numberFmt.format(stat.value)}</div>
                <div className="text-[0.62rem] uppercase tracking-wide text-zinc-500">{stat.label}</div>
              </div>
            ))}
          </div>
          {detailStats.length ? (
            <details className="rounded border border-graphite-800 bg-graphite-950/30">
              <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-zinc-400">Cleanup details</summary>
              <div className="grid gap-2 border-t border-graphite-800 p-3 sm:grid-cols-3 xl:grid-cols-5">
                {detailStats.map((stat) => (
                  <div key={stat.label} className={cx('rounded border px-2.5 py-1.5', stat.tone)}>
                    <div className="text-sm font-semibold tabular-nums">{numberFmt.format(stat.value)}</div>
                    <div className="text-[0.62rem] uppercase tracking-wide text-zinc-500">{stat.label}</div>
                  </div>
                ))}
              </div>
            </details>
          ) : null}
        </div>
      )}

      {showDetails && (
        <div className="mt-2 overflow-hidden rounded-md border border-graphite-800">
          <div className="flex flex-wrap items-center gap-2 border-b border-graphite-800 bg-graphite-950/50 px-3 py-2">
            <div className="text-xs font-semibold text-zinc-300">Library Cleanup report</div>
            {(['active', 'safe', 'review', 'blocked', 'completed'] as AlbumIssueFilter[]).map((value) => (
              <button
                key={value}
                type="button"
                className={cx(
                  'rounded px-2 py-0.5 text-[0.68rem] font-medium',
                  filter === value ? 'bg-red-600/70 text-white' : 'bg-graphite-800 text-zinc-400 hover:bg-graphite-700 hover:text-zinc-200',
                )}
                onClick={() => setFilter(value)}
              >
                {value === 'review' ? 'Needs review' : value[0].toUpperCase() + value.slice(1)}
              </button>
            ))}
          </div>
          {visibleIssues.length ? (
            <div className="max-h-80 overflow-y-auto">
              {visibleIssues.slice(0, 80).map((issue) => {
                const canonical = issue.canonical_folder || issue.proposed_canonical_folder || '';
                const current = issue.current_folder_names?.length ? issue.current_folder_names : issue.current_folders;
                return (
                  <div key={issue.id ?? `${issue.artist}-${issue.album}-${canonical}`} className="grid gap-2 border-b border-graphite-800/70 px-3 py-2 text-xs last:border-b-0 xl:grid-cols-[1fr_1fr_1.1fr_1.1fr_0.8fr_0.9fr]">
                    <div className="min-w-0">
                      <div className="truncate text-[0.68rem] uppercase tracking-wide text-zinc-600">{issueKind(issue)}</div>
                      <div className="truncate font-medium text-zinc-200">{issue.artist || 'Unknown artist'}</div>
                      <div className="truncate text-zinc-500">{issue.album || 'Unknown album'}</div>
                    </div>
                    <div className="min-w-0 text-zinc-500">
                      {(current ?? []).slice(0, 3).map((folder) => <div key={folder} className="truncate">{folder}</div>)}
                    </div>
                    <div className="min-w-0">
                      {canonical ? <div className="truncate text-zinc-600">{canonical}</div> : null}
                      <div className="truncate font-mono text-[0.68rem] text-zinc-500">{issue.release_group_id || 'No RGID'}</div>
                    </div>
                    <div className="min-w-0">
                      <div className="truncate text-zinc-300">{issue.proposed_action || 'Review issue'}</div>
                      <div className="truncate text-zinc-500">
                        {numberFmt.format(asCount(issue.files_to_move))} move / {numberFmt.format(asCount(issue.files_to_safe_delete))} quarantine
                      </div>
                      <div className="truncate text-zinc-600" title={issueReason(issue)}>{issueReason(issue)}</div>
                    </div>
                    <div className="space-y-1">
                      <Chip
                        color={issueRisk(issue) === 'Safe' ? 'success' : issueRisk(issue) === 'Blocked' ? 'error' : issueRisk(issue) === 'Completed' ? 'info' : 'warning'}
                        label={issueRisk(issue)}
                        size="small"
                        variant="outlined"
                      />
                      <div className="text-[0.68rem] text-zinc-500">{issueStatus(issue)}</div>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      <Button size="small" variant="outlined" onClick={() => setSelectedIssue(issue)}>Preview</Button>
                      <Button
                        disabled={busy || issueRisk(issue) === 'Blocked' || issueStatus(issue) === 'Completed'}
                        size="small"
                        variant="contained"
                        onClick={() => void applyIssue(issue)}
                      >
                        Fix
                      </Button>
                      <Button disabled={busy || issueStatus(issue) === 'Completed'} size="small" variant="text" onClick={() => markLocal(issue, 'Skipped')}>
                        Skip
                      </Button>
                      <Button disabled={busy || issueStatus(issue) === 'Completed'} size="small" variant="text" onClick={() => markLocal(issue, 'Ignored')}>
                        Ignore
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="px-3 py-4 text-xs text-zinc-500">No matching issue rows. Run a scan or open the latest cleanup report.</div>
          )}
        </div>
      )}
      <AlbumFolderIssueDialog
        busy={busy}
        issue={selectedIssue}
        onApply={(issue) => void applyIssue(issue)}
        onClose={() => setSelectedIssue(null)}
        onIgnore={(issue) => markLocal(issue, 'Ignored')}
        onSkip={(issue) => markLocal(issue, 'Skipped')}
      />
    </div>
  );
}

function AdvancedLibraryMovePanel() {
  return (
    <div className="max-w-xl rounded-lg border border-amber-900/50 bg-amber-950/20 p-4">
      <h3 className="text-sm font-semibold text-amber-100">Move entire library to current path template</h3>
      <p className="mt-1 text-xs leading-relaxed text-amber-200/80">
        This is a library-wide beets move operation. It is intentionally not exposed as a one-click cleanup action here
        because this workflow needs a dedicated preview showing every source path, target path, conflict, and skipped item.
      </p>
      <p className="mt-2 text-xs leading-relaxed text-amber-200/80">
        Use Folder Names and Leaked DB Paths for targeted cleanup. Add a full preview/confirmation flow before enabling
        this advanced move action.
      </p>
    </div>
  );
}

function MaintenanceCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3 rounded-lg border border-graphite-800 bg-graphite-900/45 p-4">
      <div>
        <h3 className="text-sm font-semibold text-zinc-100">{title}</h3>
        <p className="mt-1 text-xs leading-relaxed text-zinc-500">{description}</p>
      </div>
      {children}
    </section>
  );
}

function MaintenanceRunnerBar({
  jobs,
  onStarted,
  onSelectJob,
}: {
  jobs: Job[];
  onStarted: () => Promise<void>;
  onSelectJob: (job: Job, options?: JobOpenOptions) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [showTasks, setShowTasks] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState('');
  const [report, setReport] = useState<MaintenanceRunnerReportResponse | null>(null);
  const maintenanceJobs = useMemo(() => jobs.filter(isMaintenanceRunnerJob), [jobs]);
  const currentJob = maintenanceJobs.find((job) => job.status === 'running') ?? maintenanceJobs[0] ?? null;
  const state = structuredState(currentJob);
  const status = maintenanceStatusLabel(currentJob);
  const running = currentJob?.status === 'running';
  const progress = maintenanceProgress(currentJob);
  const tasks = maintenanceTasksFromState(state);
  const currentTask = String(state?.current_task || (running ? 'Clean All' : 'No Clean All run active'));
  const currentPhase = String(state?.current_phase || (running ? 'Scanning' : 'Idle'));
  const lastHeartbeat = stateNumber(state, 'last_heartbeat_at');
  const cleanAllCounts = cleanAllCountsFromState(state);
  const cleanAllPipeline = cleanAllPipelineFromState(state);
  const lastRun = maintenanceJobs.find((job) => job.status !== 'running');
  const completedTaskCount = tasks.filter((task) => task.status === 'complete' || task.status === 'skipped').length;
  const nextIncompleteTask = tasks.find((task) => task.status !== 'complete' && task.status !== 'skipped');
  const hasStateCheckpoint = !running && ['Failed', 'Partial', 'Completed with issues'].includes(status) && completedTaskCount > 0 && Boolean(nextIncompleteTask);
  const reportResume = report?.resume;
  const checkpointResumable = !running && (Boolean(report?.resumable || reportResume?.resumable) || hasStateCheckpoint);
  const checkpointNextLabel = String(reportResume?.next_task_label || nextIncompleteTask?.label || 'next pending phase');

  useEffect(() => {
    let cancelled = false;
    void apiGet<MaintenanceRunnerReportResponse>('/api/jobs/maintenance-runner/report?summary=1')
      .then((data) => {
        if (!cancelled) setReport(data);
      })
      .catch(() => {
        if (!cancelled) setReport(null);
      });
    return () => {
      cancelled = true;
    };
  }, [currentJob?.job_id, currentJob?.status]);

  const runNow = async (options?: { forceFresh?: boolean }) => {
    setBusy(true);
    setError('');
    try {
      await startCleanAll(options);
      setReport(null);
      await onStarted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!currentJob || !running) return;
    setBusy(true);
    setError('');
    try {
      await apiPost<ApiOkResponse>(`/api/jobs/${currentJob.job_id}/kill`);
      await onStarted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const openReport = async () => {
    setReportOpen(true);
    setReportLoading(true);
    setReportError('');
    try {
      const data = await apiGet<MaintenanceRunnerReportResponse>('/api/jobs/maintenance-runner/report');
      setReport(data);
    } catch (err) {
      setReportError(err instanceof Error ? err.message : String(err));
    } finally {
      setReportLoading(false);
    }
  };

  const statusColor = status === 'Running'
    ? 'info'
    : status === 'Failed'
      ? 'error'
      : status === 'Partial' || status === 'Completed with issues'
        ? 'warning'
        : status === 'Complete'
          ? 'success'
          : 'default';
  const taskColor = (task: MaintenanceTaskView): 'default' | 'success' | 'warning' | 'error' | 'info' => {
    if (task.status === 'running') return 'info';
    if (task.status === 'complete') return 'success';
    if (task.status === 'failed') return 'error';
    if (task.status === 'skipped') return 'warning';
    return 'default';
  };

  return (
    <>
      <section className="rounded-md border border-graphite-800 bg-graphite-950/50 px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold text-zinc-100">Clean All</h2>
              <Chip color={statusColor} label={status} size="small" variant="outlined" />
              <span className="text-xs text-zinc-500">{currentTask}</span>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-zinc-500">
              Scan, identify, verify, repair, replace, and organize the entire music library.
            </p>
            <div className="mt-2 flex items-center gap-3">
              <LinearProgress
                className="min-w-0 flex-1"
                value={progress}
                variant="determinate"
                sx={{ borderRadius: 1, height: 8 }}
              />
              <span className="w-12 text-right text-xs tabular-nums text-zinc-400">{Math.round(progress)}%</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {cleanAllPipeline.map((step) => (
                <Chip
                  key={step}
                  color={step === currentPhase ? 'info' : 'default'}
                  label={step}
                  size="small"
                  variant={step === currentPhase ? 'filled' : 'outlined'}
                />
              ))}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-8">
              {CLEAN_ALL_COUNT_LABELS.map(([key, label]) => (
                <div key={key} className="rounded border border-graphite-800 bg-graphite-900/55 px-2 py-1.5">
                  <div className="text-sm font-semibold tabular-nums text-zinc-100">{numberFmt.format(cleanAllCounts[key])}</div>
                  <div className="text-[0.62rem] uppercase tracking-wide text-zinc-500">{label}</div>
                </div>
              ))}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[0.7rem] text-zinc-500">
              <span>Last run: {formatDateTime(lastRun?.finished_at ?? lastRun?.started_at)}</span>
              <span>Last heartbeat: {formatDateTime(lastHeartbeat)}</span>
              <span>Next scheduled run: Manual only</span>
            </div>
            {checkpointResumable ? (
              <div className="mt-2 text-xs text-amber-300">
                Resume checkpoint ready. Next phase: {checkpointNextLabel}.{' '}
                <Button
                  disabled={busy || running}
                  size="small"
                  variant="text"
                  sx={{ p: 0, minWidth: 0, verticalAlign: 'baseline', fontSize: 'inherit', textTransform: 'none' }}
                  onClick={() => {
                    if (window.confirm('Discard this checkpoint and start a completely fresh Clean All run? Progress from the incomplete run will not be reused.')) {
                      void runNow({ forceFresh: true });
                    }
                  }}
                >
                  Discard checkpoint and start fresh instead.
                </Button>
              </div>
            ) : null}
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button disabled={busy || running} size="small" variant="contained" onClick={() => void runNow()}>
              {busy && !running ? (checkpointResumable ? 'Resuming...' : 'Starting...') : checkpointResumable ? 'Resume Clean All' : 'Clean All'}
            </Button>
            {running ? (
              <Button color="error" disabled={busy} size="small" variant="outlined" onClick={() => void stop()}>
                Stop
              </Button>
            ) : null}
            <Button href="/import?tab=review" size="small" variant="outlined">
              Submission Queue
            </Button>
            <Button size="small" variant="outlined" onClick={() => void openReport()}>
              Cleanup Report
            </Button>
            <Button disabled={!currentJob} size="small" variant="outlined" onClick={() => currentJob && onSelectJob(currentJob)}>
              View Progress
            </Button>
          </div>
        </div>
        <div className="mt-3">
          <button
            type="button"
            className="text-[0.7rem] text-zinc-500 hover:text-zinc-300 underline"
            onClick={() => setShowTasks((v) => !v)}
          >
            {showTasks ? 'Hide technical checks' : `Show technical checks (${tasks.length})`}
          </button>
          {showTasks && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {tasks.map((task) => (
                <Chip
                  key={task.id}
                  color={taskColor(task)}
                  label={`${task.label}: ${task.status}`}
                  size="small"
                  title={task.detail || task.label}
                  variant={task.status === 'pending' ? 'outlined' : 'filled'}
                />
              ))}
            </div>
          )}
        </div>
        {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
      </section>
      <MaintenanceRunnerReportDialog
        error={reportError}
        loading={reportLoading}
        open={reportOpen}
        report={report}
        onClose={() => setReportOpen(false)}
      />
    </>
  );
}

function MaintenanceRunnerReportDialog({
  open,
  loading,
  error,
  report,
  onClose,
}: {
  open: boolean;
  loading: boolean;
  error: string;
  report: MaintenanceRunnerReportResponse | null;
  onClose: () => void;
}) {
  const body = report?.report ?? {};
  const duplicates = nestedRecord(body, 'duplicates');
  const duplicateSummary = nestedRecord(duplicates, 'final_summary') ?? duplicates;
  const lastRun = nestedRecord(body, 'last_run');
  const duplicateCandidates = reportNumber(duplicateSummary, 'duplicate_candidates');
  const duplicateAlbumGroups = reportNumber(duplicateSummary, 'duplicate_album_groups');
  const releaseGroupGroups = reportNumber(duplicateSummary, 'same_release_group_id_groups');
  const fileScanStarted = reportBoolean(duplicateSummary, 'file_duplicate_scan_started');
  const deletedFiles = reportNumber(duplicateSummary, 'deleted_files');
  const updatedAt = typeof body.updated_at === 'number' ? body.updated_at : null;
  const completedAt = typeof lastRun?.completed_at === 'number' ? lastRun.completed_at : null;

  return (
    <Dialog className="relative z-50" open={open} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">Cleanup Report</DialogTitle>
          {loading ? <LinearProgress sx={{ mt: 2, borderRadius: 1 }} /> : null}
          {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
          {!loading && !error && !report?.exists ? (
            <p className="mt-3 text-sm text-zinc-400">No cleanup report has been saved yet.</p>
          ) : null}
          {!loading && !error && report?.exists ? (
            <div className="mt-4 space-y-4">
              <div className="grid gap-2 sm:grid-cols-4">
                <StatTile label="Candidates" tone="sky" value={duplicateCandidates} />
                <StatTile label="Album groups" tone="slate" value={duplicateAlbumGroups} />
                <StatTile label="RGID groups" tone="slate" value={releaseGroupGroups} />
                <StatTile label="Deleted files" tone={deletedFiles ? 'red' : 'green'} value={deletedFiles} />
              </div>
              <div className="grid gap-2 rounded-md border border-graphite-800 bg-graphite-950/50 p-3 text-xs text-zinc-400 sm:grid-cols-3">
                <div>
                  <div className="text-zinc-500">Updated</div>
                  <div className="font-medium text-zinc-200">{formatDateTime(updatedAt)}</div>
                </div>
                <div>
                  <div className="text-zinc-500">Last complete run</div>
                  <div className="font-medium text-zinc-200">{formatDateTime(completedAt)}</div>
                </div>
                <div>
                  <div className="text-zinc-500">Full file scan</div>
                  <div className="font-medium text-zinc-200">{fileScanStarted ? 'Started' : 'Not started'}</div>
                </div>
              </div>
              <pre className="max-h-72 overflow-auto rounded-md border border-graphite-800 bg-graphite-950/70 p-3 text-xs leading-relaxed text-zinc-300">
                {JSON.stringify(body, null, 2)}
              </pre>
            </div>
          ) : null}
          <div className="mt-5 flex justify-end">
            <Button size="small" variant="outlined" onClick={onClose}>Close</Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function CommonActionsSection({
  jobs,
  onStarted,
  failedCount,
  onReviewFailed,
  onSelectJob,
}: {
  jobs: Job[];
  onStarted: () => Promise<void>;
  failedCount: number;
  onReviewFailed: () => void;
  onSelectJob: (job: Job, options?: JobOpenOptions) => void;
}) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-base font-semibold text-zinc-100">Library Cleanup</h2>
        <p className="mt-1 text-sm text-zinc-500">One coordinated cleanup workflow for library scans, verification, safe repairs, cleanup reports, and submission review.</p>
      </div>

      <MaintenanceRunnerBar jobs={jobs} onStarted={onStarted} onSelectJob={onSelectJob} />
      <AlbumFolderCleanupPanel failedCount={failedCount} onReviewFailed={onReviewFailed} onStarted={onStarted} />

      <section className="space-y-2" aria-label="Manual Actions">
        <details className="rounded-md border border-graphite-800 bg-graphite-950/40">
          <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-zinc-300 hover:text-zinc-100">
            Metadata & Artwork
          </summary>
          <div className="grid gap-4 border-t border-graphite-800 p-4 xl:grid-cols-3">
            <MusicBrainzPanel onStarted={onStarted} />
            <GenresPanel onStarted={onStarted} />
            <ArtworkPanel onStarted={onStarted} />
          </div>
        </details>

        <details className="rounded-md border border-graphite-800 bg-graphite-950/40">
          <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-zinc-300 hover:text-zinc-100">
            Duplicate Checks
          </summary>
          <div className="border-t border-graphite-800 p-4">
            <MaintenanceCard
              title="Duplicate Tracks & Albums"
              description="Scan duplicate tracks, duplicate album rows, and artist-folder MusicBrainz variants. Detailed candidates stay hidden until review."
            >
              <DuplicatesPanel />
            </MaintenanceCard>
          </div>
        </details>

        <details className="rounded-md border border-graphite-800 bg-graphite-950/40">
          <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-zinc-300 hover:text-zinc-100">
            Database Health
          </summary>
          <div className="grid gap-4 border-t border-graphite-800 p-4 xl:grid-cols-2">
            <MissingFilesSyncPanel onStarted={onStarted} />
            <MaintenanceCard
              title="Database health summary"
              description="Preview duplicate albums, orphaned rows, empty albums, and MusicBrainz ID coverage."
            >
              <LibraryHealthPanel active autoLoad={false} />
            </MaintenanceCard>
          </div>
        </details>
      </section>
    </section>
  );
}

function JobHistorySection({
  jobs,
  selectedJobId,
  onSelectJob,
}: {
  jobs: Job[];
  selectedJobId: string | null;
  onSelectJob: (job: Job, options?: JobOpenOptions) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const historyJobs = useMemo(() => jobs.filter((job) => job.status !== 'running'), [jobs]);
  const visibleJobs = expanded ? historyJobs : historyJobs.slice(0, 12);
  const runningCount = jobs.length - historyJobs.length;
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-zinc-100">Job History</h2>
          <p className="mt-1 text-sm text-zinc-500">
            Finished, failed, and cancelled jobs stay here. {runningCount ? `${numberFmt.format(runningCount)} running job${runningCount === 1 ? '' : 's'} shown in Current activity.` : 'No running jobs are repeated here.'}
          </p>
        </div>
        {historyJobs.length > 12 ? (
          <Button size="small" variant="outlined" onClick={() => setExpanded((value) => !value)}>
            {expanded ? 'Show recent' : 'Show more history'}
          </Button>
        ) : null}
      </div>
      <div className={cx(expanded ? 'h-[26rem]' : 'h-[14rem]', 'overflow-hidden rounded-md border border-graphite-800 bg-graphite-950/45')}>
        <ConsoleHistory jobs={visibleJobs} selectedJobId={selectedJobId} onSelectJob={onSelectJob} />
      </div>
    </section>
  );
}

function AdvancedMaintenanceSection() {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-md border border-amber-900/50 bg-amber-950/10"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-amber-100">
        Advanced Maintenance
      </summary>
      {open ? (
        <div className="space-y-4 border-t border-amber-900/40 p-4">
          <Alert severity="warning">
            Advanced repair tools for rare library/database issues. Most users should not need these.
          </Alert>
          <div className="grid gap-4">
            <MaintenanceCard
              title="Folder Name Issues"
              description="Preview and repair placeholder folders, target conflicts, and folder names that can be safely normalized."
            >
              <FolderPlaceholdersPanel />
            </MaintenanceCard>
            <MaintenanceCard
              title="Leaked DB Paths"
              description="Preview and repair Beets DB paths that still contain leaked path-template fragments."
            >
              <LeakedPathsPanel />
            </MaintenanceCard>
            <MaintenanceCard
              title="Artist Alias Repair"
              description="Merge or reject duplicate artist identities after reviewing MusicBrainz artist ID groups."
            >
              <ArtistAliasPanel active autoLoad />
            </MaintenanceCard>
            <MaintenanceCard
              title="Album Track Repair"
              description="Compare existing album rows to MusicBrainz tracklists and repair clear album-track mismatches."
            >
              <AlbumTracksPanel />
            </MaintenanceCard>
            <MaintenanceCard
              title="Advanced Library Move"
              description="Keep the library-wide move operation locked until a full source/target preview can be reviewed."
            >
              <AdvancedLibraryMovePanel />
            </MaintenanceCard>
          </div>
        </div>
      ) : null}
    </details>
  );
}

// ─── ConfirmDialog ────────────────────────────────────────────────────────────

function ConfirmDialog({
  action,
  busy,
  onClose,
  onConfirm,
}: {
  action: ConfirmAction;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const title = action?.kind === 'kill' ? 'Cancel running job?' : 'Clear finished jobs?';
  const body = action?.kind === 'kill'
    ? `Cancel "${action.job.label}". This asks the backend job to stop safely.`
    : 'Remove completed, failed, cancelled, and killed jobs from the in-memory job list.';

  return (
    <Dialog className="relative z-50" open={Boolean(action)} onClose={busy ? () => undefined : onClose}>
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-md rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">{title}</DialogTitle>
          <p className="mt-2 text-sm leading-6 text-zinc-400">{body}</p>
          <div className="mt-5 flex justify-end gap-2">
            <Button disabled={busy} variant="outlined" onClick={onClose}>Cancel</Button>
            <Button
              color={action?.kind === 'kill' || action?.kind === 'clear' ? 'error' : 'primary'}
              disabled={busy}
              variant="contained"
              onClick={onConfirm}
            >
              {busy ? 'Working...' : action?.kind === 'kill' ? 'Cancel job' : 'Clear'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

// ─── Job details ───────────────────────────────────────────────────────

function PathReference({ path }: { path: PathHit }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  async function copyPath() {
    try {
      await navigator.clipboard?.writeText(path.full);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="mt-1 rounded border border-graphite-800 bg-graphite-950/60 px-2 py-1.5 text-[0.72rem]">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-zinc-400">{expanded ? path.full : path.short}</span>
        <button className="shrink-0 text-red-300 hover:text-red-200" type="button" onClick={() => setExpanded((value) => !value)}>
          {expanded ? 'Hide' : 'Full path'}
        </button>
        <button className="shrink-0 text-zinc-500 hover:text-zinc-300" type="button" onClick={() => void copyPath()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </div>
  );
}

function copyTextWithFallback(text: string): boolean {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    return document.execCommand('copy');
  } finally {
    document.body.removeChild(textarea);
  }
}

async function writeClipboardText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall back below for non-secure origins or denied clipboard access.
  }
  return copyTextWithFallback(text);
}

function RawLogDetails({
  rawLines,
  technicalDetails,
  forceRawOpen = false,
}: {
  rawLines: string[];
  technicalDetails?: Record<string, unknown>;
  forceRawOpen?: boolean;
}) {
  const [showRaw, setShowRaw] = useState(false);
  const [showTechnical, setShowTechnical] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const rawText = rawLines.length ? rawLines.join('\n') : '(no raw log output)';

  useEffect(() => { setShowRaw(forceRawOpen); }, [forceRawOpen]);

  async function copyRawLog() {
    const ok = await writeClipboardText(rawText);
    setCopied(ok);
    setCopyFailed(!ok);
    window.setTimeout(() => {
      setCopied(false);
      setCopyFailed(false);
    }, 1200);
  }

  function downloadRawLog() {
    const blob = new Blob([rawText], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'job-log.txt';
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="border-t border-graphite-800 px-3 py-2">
      <div className="flex flex-wrap gap-2">
        <Button size="small" variant="text" onClick={() => setShowRaw((value) => !value)}>
          {showRaw ? 'Hide raw log' : 'Show raw log'}
        </Button>
        <Button size="small" variant="text" onClick={() => void copyRawLog()}>
          {copied ? 'Copied raw log' : copyFailed ? 'Copy failed' : 'Copy raw log'}
        </Button>
        <Button size="small" variant="text" onClick={downloadRawLog}>
          Download raw log
        </Button>
        {technicalDetails ? (
          <Button size="small" variant="text" onClick={() => setShowTechnical((value) => !value)}>
            {showTechnical ? 'Hide technical fields' : 'Show technical fields'}
          </Button>
        ) : null}
      </div>
      {showRaw ? (
        <pre className="mt-2 max-h-56 overflow-y-auto whitespace-pre-wrap rounded border border-graphite-800 bg-graphite-950 p-3 font-mono text-[0.7rem] leading-5 text-zinc-400">
          {rawText}
        </pre>
      ) : null}
      {showTechnical && technicalDetails ? (
        <pre className="mt-2 max-h-56 overflow-y-auto whitespace-pre-wrap rounded border border-graphite-800 bg-graphite-950 p-3 font-mono text-[0.7rem] leading-5 text-zinc-400">
          {JSON.stringify(technicalDetails, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

function toneClasses(tone: JobFeedStatus) {
  if (tone === 'success') return 'border-emerald-500/40 bg-emerald-500/5 text-emerald-200';
  if (tone === 'warning') return 'border-amber-500/40 bg-amber-500/5 text-amber-200';
  if (tone === 'error') return 'border-rose-500/40 bg-rose-500/5 text-rose-200';
  if (tone === 'cancelled') return 'border-graphite-600/50 bg-graphite-700/10 text-zinc-300';
  return 'border-sky-500/30 bg-sky-500/5 text-sky-200';
}

function statusIcon(status: JobFeedStatus) {
  if (status === 'success') return '✓';
  if (status === 'running') return '⏳';
  if (status === 'warning') return '!';
  if (status === 'error') return '×';
  if (status === 'cancelled') return '–';
  return '•';
}

function statusChipColor(status: JobFeedStatus): 'default' | 'success' | 'warning' | 'error' | 'info' {
  if (status === 'success') return 'success';
  if (status === 'warning') return 'warning';
  if (status === 'error') return 'error';
  if (status === 'running' || status === 'info') return 'info';
  return 'default';
}

function retryablePlaylistAction(job: Job) {
  if (job.metadata?.type !== 'playlist-pipeline') return '';
  const action = String(job.metadata.action || '').replace(/_/g, '-');
  const name = String(job.metadata.name || '').trim();
  return action && name ? action : '';
}
function JobSummaryHeader({
  job,
  model,
  loading,
  onCancel,
  onRetry,
}: {
  job: Job;
  model: ReturnType<typeof buildJobFeed>;
  loading?: boolean;
  onCancel?: () => void;
  onRetry?: () => void;
}) {
  const { summary } = model;
  const canCancel = job.status === 'running' && Boolean(onCancel);
  const canRetry = job.status === 'failed' && Boolean(retryablePlaylistAction(job)) && Boolean(onRetry);
  const showResult = job.status !== 'running' || summary.needsAttention;

  return (
    <div className="shrink-0 border-b border-graphite-800 px-3 py-3">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold text-zinc-100">{summary.title}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
            <Chip color={statusChipColor(summary.status)} label={summary.statusLabel} size="small" variant="outlined" />
            <span>Current phase: <span className="text-zinc-300">{summary.currentPhase}</span></span>
          </div>
        </div>
        {canCancel ? (
          <Button color="warning" size="small" variant="outlined" onClick={onCancel}>Cancel job</Button>
        ) : null}
        {canRetry ? (
          <Button color="primary" size="small" variant="contained" onClick={onRetry}>Retry</Button>
        ) : null}
      </div>

      <div className="mt-3 grid gap-2 text-xs text-zinc-500 sm:grid-cols-4">
        <div className="rounded border border-graphite-800 bg-graphite-950/40 px-2 py-1.5">
          <div className="text-[0.62rem] uppercase tracking-wide text-zinc-600">Started</div>
          <div className="mt-0.5 text-zinc-200">{formatDateTime(job.started_at ?? job.created_at)}</div>
        </div>
        <div className="rounded border border-graphite-800 bg-graphite-950/40 px-2 py-1.5">
          <div className="text-[0.62rem] uppercase tracking-wide text-zinc-600">Elapsed</div>
          <div className="mt-0.5 text-zinc-200">{formatDuration(job) || 'Working...'}</div>
        </div>
        <div className="rounded border border-graphite-800 bg-graphite-950/40 px-2 py-1.5 sm:col-span-2">
          <div className="text-[0.62rem] uppercase tracking-wide text-zinc-600">Progress</div>
          <div className="mt-0.5 text-zinc-200">{summary.progressText || (job.status === 'running' ? 'Working...' : 'No count reported')}</div>
        </div>
      </div>

      {job.status === 'running' && !summary.progressText ? <LinearProgress sx={{ borderRadius: 1, mt: 1.5 }} /> : null}
      {loading ? <LinearProgress sx={{ borderRadius: 1, mt: 1.5 }} /> : null}

      {showResult && summary.resultTitle ? (
        <div className={cx('mt-3 rounded border px-3 py-2 text-sm', toneClasses(summary.status))}>
          <div className="font-medium text-zinc-100">{summary.resultTitle}</div>
          {summary.friendlyReason ? <div className="mt-1 text-xs text-zinc-300">{summary.friendlyReason}</div> : null}
          {summary.resultBullets.length ? (
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              {summary.resultBullets.map((bullet) => (
                <span key={bullet} className="rounded border border-graphite-700/70 bg-graphite-950/40 px-2 py-1 text-zinc-300">{bullet}</span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function FeedTimeline({ items, groups, emptyText }: { items: JobFeedItem[]; groups: ReturnType<typeof buildJobFeed>['groups']; emptyText: string }) {
  if (!items.length) {
    return <div className="rounded border border-graphite-800 bg-graphite-950/50 p-4 text-sm text-zinc-500">{emptyText}</div>;
  }

  return (
    <div className="space-y-4">
      {JOB_FEED_PHASE_ORDER.map((phase) => groups.find((group) => group.phase === phase)).filter((group): group is NonNullable<typeof group> => Boolean(group)).map((group) => {
        const shownItems = group.items.slice(0, MAX_VISIBLE_ITEMS_PER_STEP);
        const hiddenCount = group.items.length - shownItems.length;
        return (
          <section key={group.phase} className="relative">
            <div className="mb-2 flex items-center gap-2">
              <span className={cx('inline-flex h-5 w-5 items-center justify-center rounded-full border text-[0.7rem]', toneClasses(group.status))}>{statusIcon(group.status)}</span>
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-400">{group.phase}</span>
            </div>
            <div className="ml-2.5 space-y-2 border-l border-graphite-800 pl-4">
              {shownItems.map((item) => (
                <article key={item.id} className={cx('rounded-md border px-3 py-2', toneClasses(item.status))}>
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 w-5 shrink-0 text-center text-xs">{statusIcon(item.status)}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        {item.time ? <span className="text-[0.68rem] tabular-nums text-zinc-500">{formatClock(item.time)}</span> : null}
                        <div className="min-w-0 flex-1 text-sm font-medium text-zinc-100">{item.title}</div>
                      </div>
                      {item.message ? <div className="mt-1 text-xs text-zinc-400">{item.message}</div> : null}
                      {item.detail ? <div className="mt-1 text-xs text-zinc-400">{item.detail}</div> : null}
                      {item.paths.map((path) => <PathReference key={`${item.id}-${path.full}`} path={path} />)}
                      {item.technical && Object.keys(item.technical).length ? (
                        <details className="mt-2 rounded border border-graphite-800 bg-graphite-950/40 px-2 py-1.5 text-[0.7rem] text-zinc-500">
                          <summary className="cursor-pointer text-zinc-400">Technical details</summary>
                          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap font-mono">{JSON.stringify(item.technical, null, 2)}</pre>
                        </details>
                      ) : null}
                    </div>
                  </div>
                </article>
              ))}
              {hiddenCount > 0 ? (
                <div className="rounded border border-graphite-800 bg-graphite-950/50 px-3 py-2 text-xs text-zinc-500">
                  {numberFmt.format(hiddenCount)} more update{hiddenCount === 1 ? '' : 's'} hidden. Show raw log for the full output.
                </div>
              ) : null}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function HumanJobFeedPanel({
  job,
  rawLines,
  loading,
  forceRawOpen = false,
  onCancel,
  onRetry,
}: {
  job: Job;
  rawLines: string[];
  loading?: boolean;
  forceRawOpen?: boolean;
  onCancel?: () => void;
  onRetry?: () => void;
}) {
  const model = useMemo(() => buildJobFeed(job, rawLines), [job, rawLines]);
  const emptyText = job.status === 'running'
    ? (rawLines.length <= 1 ? 'Waiting for the next update...' : 'Still working. No new update yet.')
    : 'No readable feed activity was recorded for this job.';

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <JobSummaryHeader job={job} loading={loading} model={model} onCancel={onCancel} onRetry={onRetry} />
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        <FeedTimeline emptyText={emptyText} groups={model.groups} items={model.items} />
      </div>
      <RawLogDetails forceRawOpen={forceRawOpen} rawLines={rawLines} technicalDetails={model.technicalDetails} />
    </div>
  );
}

function SelectedJobView({
  job,
  log,
  loading,
  forceRawOpen = false,
  onAction,
  onRetry,
}: {
  job: Job | null;
  log: string[];
  loading: boolean;
  forceRawOpen?: boolean;
  onAction: (a: ConfirmAction) => void;
  onRetry: (job: Job) => void;
}) {
  if (!job) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-zinc-600">
        Select a job to view details.
      </div>
    );
  }

  return (
    <HumanJobFeedPanel
      job={job}
      loading={loading}
      forceRawOpen={forceRawOpen}
      rawLines={log}
      onCancel={() => onAction({ kind: 'kill', job })}
      onRetry={() => onRetry(job)}
    />
  );
}

function JobDetailsDialog({
  open,
  job,
  log,
  loading,
  forceRawOpen,
  onClose,
  onAction,
  onRetry,
}: {
  open: boolean;
  job: Job | null;
  log: string[];
  loading: boolean;
  forceRawOpen: boolean;
  onClose: () => void;
  onAction: (a: ConfirmAction) => void;
  onRetry: (job: Job) => void;
}) {
  return (
    <Dialog className="relative z-50" open={open} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/65" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="flex h-[min(760px,88vh)] w-[min(920px,calc(100vw-2rem))] flex-col overflow-hidden rounded-md border border-graphite-700 bg-graphite-950 shadow-2xl shadow-black/50">
          <div className="flex shrink-0 items-center gap-3 border-b border-graphite-800 px-4 py-3">
            <DialogTitle className="min-w-0 flex-1 truncate text-sm font-semibold text-zinc-100">
              {job ? buildJobFeed(job, log).summary.title : 'Job details'}
            </DialogTitle>
            <Button size="small" variant="outlined" onClick={onClose}>Close</Button>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden">
            <SelectedJobView
              forceRawOpen={forceRawOpen}
              job={job}
              loading={loading}
              log={log}
              onAction={onAction}
              onRetry={onRetry}
            />
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}
function ConsoleHistory({
  jobs,
  selectedJobId,
  onSelectJob,
}: {
  jobs: Job[];
  selectedJobId: string | null;
  onSelectJob: (job: Job, options?: JobOpenOptions) => void;
}) {
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');

  const filtered = useMemo(
    () => jobs.filter((j) => jobMatches(j, status, query)),
    [jobs, query, status],
  );

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Filter bar */}
      <div className="flex shrink-0 items-center gap-2 border-b border-graphite-800/70 px-2.5 py-1.5">
        <input
          className="h-6 min-w-0 flex-1 bg-transparent text-xs text-zinc-300 outline-none placeholder:text-zinc-600"
          placeholder="Search jobs…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="flex gap-0.5">
          {HISTORY_STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={cx(
                'rounded px-1.5 py-0.5 text-[0.65rem] font-medium transition-colors',
                status === opt.value ? 'bg-red-600/60 text-white' : 'text-zinc-600 hover:text-zinc-400',
              )}
              onClick={() => setStatus(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>
      {/* Job list */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {filtered.map((job) => {
          const model = buildJobFeed(job, job.log ?? []);
          const summary = model.summary;
          const result = summary.resultTitle || summarizeJobResult(job, job.log ?? []);
          return (
            <div
              key={job.job_id}
              className={cx(
                'grid gap-2 border-b border-graphite-800/50 px-2.5 py-2 text-xs last:border-b-0 md:grid-cols-[minmax(0,1fr)_auto]',
                selectedJobId === job.job_id ? 'bg-red-900/25' : 'hover:bg-graphite-800/30',
              )}
            >
              <button className="min-w-0 text-left" type="button" onClick={() => onSelectJob(job)}>
                <div className="flex flex-wrap items-center gap-2">
                  <Chip color={statusChipColor(summary.status)} label={summary.statusLabel} size="small" variant="outlined" />
                  <span className="min-w-0 flex-1 truncate font-medium text-zinc-200">{summary.title}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[0.68rem] text-zinc-500">
                  <span>Started {formatDateTime(job.started_at ?? job.created_at)}</span>
                  {job.finished_at ? <span>Finished {formatDateTime(job.finished_at)}</span> : null}
                  {formatDuration(job) ? <span>{formatDuration(job)}</span> : null}
                </div>
                {result ? <div className="mt-1 truncate text-[0.72rem] text-zinc-400">{result}</div> : null}
              </button>
              <div className="flex items-center gap-1.5 md:justify-end">
                <Button size="small" variant="outlined" onClick={() => onSelectJob(job, { raw: false })}>Details</Button>
                <Button size="small" variant="text" onClick={() => onSelectJob(job, { raw: true })}>View raw log</Button>
              </div>
            </div>
          );
        })}
        {!filtered.length && <div className="p-4 text-xs text-zinc-600">No jobs match filter.</div>}
      </div>
    </div>
  );
}

// ─── Compact job status bar ──────────────────────────────────────────────────

function JobStatusBar({
  counts,

  autoRefresh,
  loading,
  hasDone,
  onAutoRefresh,
  onRefresh,
  onClearDone,
}: {
  counts: { total: number; running: number; failed: number; success: number };

  autoRefresh: boolean;
  loading: boolean;
  hasDone: boolean;
  onAutoRefresh: (value: boolean) => void;
  onRefresh: () => void;
  onClearDone: () => void;
}) {
  return (
    <section className="flex flex-wrap items-center gap-2 rounded-md border border-graphite-800 bg-graphite-950/60 px-3 py-2">
      <span className="text-sm font-semibold text-zinc-100">Job Status</span>
      <span className="rounded border border-sky-900/50 bg-sky-950/20 px-2 py-1 text-xs tabular-nums text-sky-200">
        {numberFmt.format(counts.running)} running
      </span>
      <span className="rounded border border-rose-900/50 bg-rose-950/20 px-2 py-1 text-xs tabular-nums text-rose-200">
        {numberFmt.format(counts.failed)} failed
      </span>
      <span className="rounded border border-emerald-900/50 bg-emerald-950/20 px-2 py-1 text-xs tabular-nums text-emerald-200">
        {numberFmt.format(counts.success)} completed
      </span>

      <label className="flex items-center gap-2 rounded border border-graphite-800 px-2 py-1">
        <Switch
          aria-label="Auto-refresh"
          checked={autoRefresh}
          className={cx('relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none', autoRefresh ? 'bg-sky-500' : 'bg-graphite-700')}
          onChange={onAutoRefresh}
        >
          <span className={cx('inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform', autoRefresh ? 'translate-x-4' : 'translate-x-0.5')} />
        </Switch>
        <span className="text-xs text-zinc-300">Auto-refresh</span>
      </label>
      <Button disabled={loading} size="small" variant="outlined" onClick={onRefresh}>Refresh</Button>
      {hasDone ? <Button color="error" size="small" variant="outlined" onClick={onClearDone}>Clear done</Button> : null}
    </section>
  );
}

// ─── Compact stat tile ────────────────────────────────────────────────────────

function StatTile({ label, value, tone }: { label: string; value: number; tone: 'slate' | 'sky' | 'red' | 'green' }) {
  const toneClass = { slate: 'text-zinc-100 border-graphite-800', sky: 'text-sky-200 border-sky-900', red: 'text-red-200 border-red-900', green: 'text-emerald-200 border-emerald-900' }[tone];
  return (
    <div className={cx('rounded border bg-graphite-950/50 px-3 py-2', toneClass)}>
      <div className="text-lg font-semibold tabular-nums leading-tight">{value.toLocaleString()}</div>
      <div className="mt-0.5 text-[0.68rem] uppercase tracking-wide text-zinc-500">{label}</div>
    </div>
  );
}

function RunningJobCard({
  job,
  jobGroups,
  onSelectJob,
  onAction,
  onRetry,
}: {
  job: Job;
  jobGroups: LiveJobGroup[];
  onSelectJob: (job: Job, options?: JobOpenOptions) => void;
  onAction: (a: ConfirmAction) => void;
  onRetry: (job: Job) => void;
}) {
  const group = feedGroupForJob(job, jobGroups);
  const rawLines = group?.lines.length ? group.lines : job.log ?? [];
  const model = buildJobFeed(job, rawLines, group?.entries);
  const { summary } = model;
  const latestItems = model.items.slice(-8);
  const currentStep = [...model.items].reverse().find((item) => item.status === 'running') ?? model.items.at(-1);
  const canCancel = job.status === 'running';
  const canRetry = job.status === 'failed' && Boolean(retryablePlaylistAction(job));
  const progress = summary.progressText || jobProgressText(job);

  return (
    <div className="overflow-hidden rounded-md border border-sky-900/60 bg-graphite-950/70 shadow-sm shadow-black/20">
      <div className="border-b border-graphite-800 px-4 py-3">
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-zinc-100" title={summary.title}>{summary.title}</div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500">
              <Chip color={statusChipColor(summary.status)} label={summary.statusLabel} size="small" variant="outlined" />
              <span>Phase: <span className="text-zinc-300">{summary.currentPhase}</span></span>
              {formatDuration(job) ? <span>Elapsed {formatDuration(job)}</span> : null}
              {progress ? <span>{progress}</span> : null}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            {canCancel ? (
              <Button color="warning" size="small" variant="outlined" onClick={() => onAction({ kind: 'kill', job })}>Cancel</Button>
            ) : null}
            {canRetry ? (
              <Button size="small" variant="contained" onClick={() => onRetry(job)}>Retry</Button>
            ) : null}
            <Button size="small" variant="outlined" onClick={() => onSelectJob(job, { raw: false })}>Details</Button>
          </div>
        </div>
        {job.status === 'running' && !progress ? <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} /> : null}
      </div>

      <div className="p-3">
        {currentStep ? (
          <div className={cx('mb-3 rounded-md border px-3 py-2', toneClasses(currentStep.status))}>
            <div className="flex items-start gap-2">
              <span className="mt-0.5 w-5 shrink-0 text-center text-xs">{statusIcon(currentStep.status)}</span>
              <div className="min-w-0">
                <div className="text-xs uppercase tracking-wide text-zinc-500">Now</div>
                <div className="mt-0.5 text-sm font-medium text-zinc-100">{currentStep.title}</div>
                {currentStep.detail ? <div className="mt-1 text-xs text-zinc-400">{currentStep.detail}</div> : null}
              </div>
            </div>
          </div>
        ) : null}
        <div className="space-y-1.5">
          {latestItems.length ? latestItems.map((item) => (
            <div key={item.id} className="grid grid-cols-[4.5rem_1.25rem_minmax(0,1fr)] items-start gap-2 rounded border border-graphite-800/70 bg-graphite-950/45 px-2.5 py-2 text-xs">
              <div className="pt-0.5 tabular-nums text-zinc-500">{formatClock(item.time)}</div>
              <div className={cx('flex h-5 w-5 items-center justify-center rounded-full border text-[0.65rem]', toneClasses(item.status))}>{statusIcon(item.status)}</div>
              <div className="min-w-0">
                <div className="truncate font-medium text-zinc-200" title={item.title}>{item.title}</div>
                {item.message ? <div className="mt-0.5 truncate text-zinc-500" title={item.message}>{item.message}</div> : null}
                {item.detail ? <div className="mt-0.5 truncate text-zinc-500" title={item.detail}>{item.detail}</div> : null}
              </div>
            </div>
          )) : (
            <div className="rounded border border-dashed border-graphite-800 bg-graphite-950/45 px-3 py-3 text-sm text-zinc-500">
              {job.status === 'running' ? 'Waiting for the next update...' : 'No readable feed activity was recorded.'}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CurrentJobFeedSection({
  jobs,
  jobGroups,
  latestJob,
  loading,
  onSelectJob,
  onAction,
  onRetry,
}: {
  jobs: Job[];
  jobGroups: LiveJobGroup[];
  latestJob: Job | null;
  loading: boolean;
  onSelectJob: (job: Job, options?: JobOpenOptions) => void;
  onAction: (a: ConfirmAction) => void;
  onRetry: (job: Job) => void;
}) {
  const runningJobs = jobs.filter((job) => job.status === 'running');

  if (!runningJobs.length) {
    const fallbackGroup = jobGroups[0];
    const visibleJob: Job | null = fallbackGroup ? {
      job_id: fallbackGroup.job_id,
      label: fallbackGroup.label,
      status: fallbackGroup.status,
      created_at: fallbackGroup.created_at ?? Date.now() / 1000,
      started_at: fallbackGroup.started_at,
      finished_at: fallbackGroup.finished_at,
      log_lines: fallbackGroup.lines.length,
    } : null;

    if (!visibleJob) {
      const latestSummary = latestJob ? buildJobFeed(latestJob, latestJob.log ?? []).summary : null;
      return (
        <section className="rounded-md border border-graphite-800 bg-graphite-950/55 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-base font-semibold text-zinc-100">Current activity</h2>
            {loading ? <Chip color="info" label="Loading" size="small" variant="outlined" /> : <Chip label="Idle" size="small" variant="outlined" />}
          </div>
          <div className="mt-3 rounded border border-dashed border-graphite-800 bg-graphite-950/45 px-3 py-3 text-sm text-zinc-500">
            {loading ? 'Loading job activity...' : 'No job is running.'}
          </div>
          {latestJob && latestSummary ? (
            <div className="mt-2 flex flex-wrap items-center gap-2 rounded border border-graphite-800 bg-graphite-950/50 px-3 py-2 text-xs text-zinc-500">
              <span>Last finished:</span>
              <span className="min-w-0 flex-1 truncate text-zinc-300" title={latestSummary.title}>{latestSummary.title}</span>
              <Chip color={statusChipColor(latestSummary.status)} label={latestSummary.statusLabel} size="small" variant="outlined" />
              <Button size="small" variant="text" onClick={() => onSelectJob(latestJob, { raw: false })}>Details</Button>
            </div>
          ) : null}
        </section>
      );
    }

    return (
      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold text-zinc-100">Current activity</h2>
        </div>
        <RunningJobCard job={visibleJob} jobGroups={jobGroups} onAction={onAction} onRetry={onRetry} onSelectJob={onSelectJob} />
      </section>
    );
  }

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold text-zinc-100">Current activity</h2>
        <Chip color="info" label={`${runningJobs.length} running`} size="small" variant="outlined" />
      </div>
      <div className={cx('grid gap-3', runningJobs.length > 1 ? 'lg:grid-cols-2' : '')}>
        {runningJobs.map((job) => (
          <RunningJobCard key={job.job_id} job={job} jobGroups={jobGroups} onAction={onAction} onRetry={onRetry} onSelectJob={onSelectJob} />
        ))}
      </div>
    </section>
  );
}

// ─── Main Jobs page ───────────────────────────────────────────────────────────

export default function Jobs() {
  // Job data
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  // Feed
  const [feed, setFeed] = useState<JobLogFeedResponse['entries']>([]);
  const [feedLoading, setFeedLoading] = useState(false);

  // Status bar
  const [autoRefresh, setAutoRefresh] = useState(true);

  // Job details
  const [selectedJobDialogOpen, setSelectedJobDialogOpen] = useState(false);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [selectedJobMeta, setSelectedJobMeta] = useState<Job | null>(null);
  const [selectedJobLog, setSelectedJobLog] = useState<string[]>([]);
  const [selectedJobLoading, setSelectedJobLoading] = useState(false);
  const [selectedJobRawOpen, setSelectedJobRawOpen] = useState(false);

  // Confirm dialog
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [actionBusy, setActionBusy] = useState(false);

  const { refresh: refreshBadge } = useGlobalJobs();

  // ── Data loading ───────────────────────────────────────────────────────────

  const load = useCallback(async () => {
    setError('');
    try {
      const r = await apiGet<JobListResponse>('/api/jobs');
      setJobs(r.jobs ?? []);
      refreshBadge();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [refreshBadge]);

  const loadFeed = useCallback(async () => {
    setFeedLoading(true);
    try {
      const r = await apiGet<JobLogFeedResponse>('/api/jobs/feed?limit=300&level=all');
      setFeed(r.entries ?? []);
    } catch { /* ignore feed errors silently */ }
    finally { setFeedLoading(false); }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([load(), loadFeed()]);
  }, [load, loadFeed]);

  useEffect(() => { void load(); void loadFeed(); }, [load, loadFeed]);

  const counts = useMemo(() => {
    const n = { total: jobs.length, running: 0, failed: 0, success: 0 };
    for (const j of jobs) {
      if (j.status === 'running') n.running++;
      if (j.status === 'failed' || j.status === 'killed') n.failed++;
      if (j.status === 'success') n.success++;
    }
    return n;
  }, [jobs]);
  const runningJobs = useMemo(() => jobs.filter((job) => job.status === 'running'), [jobs]);
  const failedJobs = useMemo(() => jobs.filter((job) => job.status === 'failed' || job.status === 'killed'), [jobs]);

  useInterval(load, autoRefresh ? (counts.running > 0 ? 2000 : 10000) : null);
  useInterval(loadFeed, autoRefresh ? (counts.running > 0 ? 2000 : 10000) : null);

  // ── Job selection ──────────────────────────────────────────────────────────

  const handleSelectJob = useCallback(async (job: Job, options?: JobOpenOptions) => {
    setSelectedJobId(job.job_id);
    setSelectedJobRawOpen(Boolean(options?.raw));
    setSelectedJobMeta(job);
    setSelectedJobLog([]);
    setSelectedJobDialogOpen(true);
    setSelectedJobLoading(true);
    try {
      const detail = await apiGet<Job>(`/api/jobs/${job.job_id}`);
      setSelectedJobMeta(detail);
      setSelectedJobLog(detail.log ?? []);
    } catch {
      setSelectedJobLog(['Job log is no longer available.']);
    } finally {
      setSelectedJobLoading(false);
    }
  }, []);

  const handleReviewFailed = useCallback(() => {
    const firstFailed = failedJobs[0];
    if (firstFailed) {
      void handleSelectJob(firstFailed);
      return;
    }
  }, [failedJobs, handleSelectJob]);

  const handleRetryJob = useCallback(async (job: Job) => {
    const action = retryablePlaylistAction(job);
    const name = String(job.metadata?.name || '').trim();
    if (!action || !name) return;
    setError('');
    setNotice('');
    try {
      await apiPost<ApiOkResponse>(`/api/playlists/${encodeURIComponent(name)}/pipeline/${action}`, {});
      setNotice(`Retry started for ${friendlyJobTitle(job)}.`);
      await refreshAll();
      setSelectedJobDialogOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshAll]);

  const refreshSelectedJob = useCallback(async () => {
    if (!selectedJobId) return;
    try {
      const detail = await apiGet<Job>(`/api/jobs/${selectedJobId}`);
      setSelectedJobMeta(detail);
      setSelectedJobLog(detail.log ?? []);
    } catch {
      // Keep the previous selected-job context visible.
    }
  }, [selectedJobId]);

  useInterval(refreshSelectedJob, autoRefresh && selectedJobDialogOpen && selectedJobId ? (selectedJobMeta?.status === 'running' ? 2000 : 10000) : null);

  useEffect(() => {
    if (!selectedJobId) return;
    const summaryJob = jobs.find((job) => job.job_id === selectedJobId);
    if (summaryJob) setSelectedJobMeta((current) => ({ ...(current ?? summaryJob), ...summaryJob }));
  }, [jobs, selectedJobId]);

  // ── Confirm actions ────────────────────────────────────────────────────────

  const runConfirmedAction = async () => {
    if (!confirmAction) return;
    setActionBusy(true);
    setError(''); setNotice('');
    try {
      if (confirmAction.kind === 'clear') {
        await apiDelete<ApiOkResponse>('/api/jobs');
        setNotice('Finished jobs cleared.');
      } else if (confirmAction.kind === 'kill') {
        await apiPost<ApiOkResponse>(`/api/jobs/${confirmAction.job.job_id}/kill`);
        setNotice(`Cancel requested for ${confirmAction.job.label}.`);
      }
      setConfirmAction(null);
      await load();
      await loadFeed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusy(false);
    }
  };

  const hasDone = jobs.some((j) => j.status !== 'running');
  const lastJob = jobs[0] ?? null;
  const liveJobGroups = useMemo(() => groupFeedEntries(feed), [feed]);
  return (
    <>
      <div className="space-y-3">
        <JobStatusBar
          autoRefresh={autoRefresh}
          counts={counts}
          hasDone={hasDone}
          loading={loading}
          onAutoRefresh={setAutoRefresh}
          onClearDone={() => setConfirmAction({ kind: 'clear' })}
          onRefresh={() => void refreshAll()}
        />

        {/* Alerts */}
        {notice ? <Alert severity="info" onClose={() => setNotice('')}>{notice}</Alert> : null}
        {error ? <Alert severity="error">{error}</Alert> : null}
        {loading ? <LinearProgress sx={{ borderRadius: 1 }} /> : null}

        <CurrentJobFeedSection
          jobGroups={liveJobGroups}
          jobs={runningJobs}
          latestJob={lastJob}
          loading={loading || feedLoading}
          onAction={setConfirmAction}
          onRetry={(job) => void handleRetryJob(job)}
          onSelectJob={(job, options) => void handleSelectJob(job, options)}
        />

        <CommonActionsSection jobs={jobs} failedCount={failedJobs.length} onReviewFailed={handleReviewFailed} onSelectJob={(job, options) => void handleSelectJob(job, options)} onStarted={refreshAll} />

        <AdvancedMaintenanceSection />

        <JobHistorySection
          jobs={jobs}
          selectedJobId={selectedJobId}
          onSelectJob={(job, options) => void handleSelectJob(job, options)}
        />
      </div>

      <JobDetailsDialog
        forceRawOpen={selectedJobRawOpen}
        job={selectedJobMeta}
        loading={selectedJobLoading}
        log={selectedJobLog}
        open={selectedJobDialogOpen}
        onAction={setConfirmAction}
        onClose={() => setSelectedJobDialogOpen(false)}
        onRetry={(job) => void handleRetryJob(job)}
      />

      <ConfirmDialog
        action={confirmAction}
        busy={actionBusy}
        onClose={() => setConfirmAction(null)}
        onConfirm={() => void runConfirmedAction()}
      />
    </>
  );
}






