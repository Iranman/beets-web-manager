import type { Job, JobLogFeedItem, JobStatus } from '../types/api';

export type JobFeedPhase =
  | 'Preparing'
  | 'Scanning'
  | 'Matching'
  | 'Downloading'
  | 'Importing'
  | 'Tagging'
  | 'Artwork'
  | 'Plex sync'
  | 'Cleanup'
  | 'Finished'
  | 'Needs attention';

export type JobFeedStatus = 'info' | 'running' | 'success' | 'warning' | 'error' | 'cancelled';

export interface PathHit {
  full: string;
  short: string;
}

export interface JobFeedItem {
  id: string;
  phase: JobFeedPhase;
  status: JobFeedStatus;
  title: string;
  message?: string;
  detail?: string;
  time?: number;
  raw?: string;
  source?: string;
  paths: PathHit[];
  technical?: Record<string, unknown>;
}

export interface JobFeedGroup {
  phase: JobFeedPhase;
  status: JobFeedStatus;
  items: JobFeedItem[];
}

export interface JobFeedSummary {
  title: string;
  status: JobFeedStatus;
  statusLabel: string;
  currentPhase: JobFeedPhase;
  progressText: string;
  resultTitle: string;
  resultBullets: string[];
  friendlyReason: string;
  needsAttention: boolean;
}

export interface JobFeedModel {
  summary: JobFeedSummary;
  items: JobFeedItem[];
  groups: JobFeedGroup[];
  technicalDetails: Record<string, unknown>;
}

const numberFmt = new Intl.NumberFormat();

export const JOB_FEED_PHASE_ORDER: JobFeedPhase[] = [
  'Preparing',
  'Scanning',
  'Matching',
  'Downloading',
  'Importing',
  'Tagging',
  'Artwork',
  'Plex sync',
  'Cleanup',
  'Finished',
  'Needs attention',
];

const PLAYLIST_ACTION_LABELS: Record<string, string> = {
  sync_plex: 'Syncing playlist to Plex',
  sync_sources: 'Syncing playlist sources',
  reconcile_state: 'Reconciling playlist state',
  import_downloaded: 'Importing downloaded playlist tracks',
  download_missing: 'Downloading missing playlist tracks',
  run_full: 'Running playlist pipeline',
  resume: 'Resuming playlist pipeline',
};

const RESULT_LABELS: Record<string, string> = {
  checked: 'tracks checked',
  tracks_checked: 'tracks checked',
  matched: 'tracks matched',
  found: 'tracks found',
  imported: 'tracks imported',
  plex_added: 'tracks added to Plex',
  local_added: 'tracks added locally',
  tracks_unmatched: 'tracks not found in Plex',
  missing: 'missing tracks',
  missing_after_import: 'tracks still missing',
  failed: 'failed tracks',
  failed_count: 'failed items',
  needs_review: 'items needing review',
  review_required: 'items needing review',
  skipped: 'skipped items',
  removed: 'removed items',
  updated: 'updated items',
  changed: 'changed items',
  total: 'total items',
  total_count: 'total items',
  scanned: 'items scanned',
  scanned_count: 'items scanned',
};

export function cleanJobLogLine(raw: string) {
  return raw
    .replace(/\u001b\[[0-9;]*m/g, '')
    .replace(/^[[({]?(?:INFO|DEBUG|WARN|WARNING|ERROR)[\])]?[:\s-]*/i, '')
    .trim();
}

export function isTechnicalLogNoise(line: string) {
  return (
    /^\s*File ".*", line \d+/i.test(line) ||
    /^\s*\^+\s*$/.test(line) ||
    /^\s*[+-]\s+/.test(line) ||
    /^\s*(albumartist|albumstatus|artist|genres|length|mb_albumartistids|mb_artistids):/i.test(line) ||
    /^\s*(self|subcommand|album|item|util|os|sys)\./i.test(line) ||
    /^\s*with\s+open\(/i.test(line) ||
    /^\s*\[\d+\/\d+\]\s+moving album_id=/i.test(line) ||
    /^\s*(raise|return|namespace\s*=|module\s*=|for name in|import_module|\w+\s*=)/i.test(line) ||
    /^\s*[a-zA-Z_][\w.]+\([^)]*\)\s*$/.test(line) ||
    /^Traceback \(most recent call last\):/i.test(line) ||
    /during handling of the above exception/i.test(line) ||
    /the above exception was the direct cause/i.test(line)
  );
}

export function formatJobStatus(status: JobStatus | string, needsAttention = false) {
  if (needsAttention) return 'Needs attention';
  if (status === 'success') return 'Completed';
  if (status === 'failed') return 'Failed';
  if (status === 'cancelled' || status === 'killed') return 'Cancelled';
  if (status === 'running') return 'Running';
  return labelize(String(status || 'Unknown'));
}

export function friendlyJobTitle(job: Job) {
  const action = String(job.metadata?.action ?? '').trim();
  const playlist = String(job.metadata?.name ?? '').trim();
  const type = String(job.metadata?.type ?? '').trim();
  if (type === 'playlist-pipeline' && action) {
    const base = PLAYLIST_ACTION_LABELS[action] ?? labelize(action);
    return playlist ? `${base}: ${playlist}` : base;
  }
  const label = String(job.label || '').trim();
  const playlistMatch = label.match(/^Playlist Plex sync:\s*(.+)$/i);
  if (playlistMatch) return `Syncing playlist to Plex: ${playlistMatch[1].trim()}`;
  return label || job.job_id;
}

export function collectJobCounts(lines: string[], result?: Record<string, unknown>) {
  const counts = new Map<string, number>();
  const add = (key: string, value: number) => {
    if (!Number.isFinite(value)) return;
    counts.set(humanKey(key), value);
  };

  for (const [key, value] of flattenNumbers(result ?? {})) add(key, value);

  for (const raw of lines) {
    const line = cleanJobLogLine(raw);
    if (isTechnicalLogNoise(line)) continue;
    for (const match of line.matchAll(/\b(rows|tracks|albums|missing|removed|unimported|entries|audio|moved|updated|skipped|merged|deleted|created|issues|files|folders):\s*(\d+)/gi)) {
      add(match[1], Number(match[2]));
    }
    for (const match of line.matchAll(/\b(found|checked|matched|updated|moved|removed|merged|skipped|deleted|created|scanned)\s+(\d+)\s+([a-z][a-z -]+)/gi)) {
      add(match[3], Number(match[2]));
    }
  }
  return counts;
}

export function buildJobFeed(job: Job, rawLines?: string[], feedEntries?: JobLogFeedItem[]): JobFeedModel {
  const lines = rawLines ?? job.log ?? [];
  const items = dedupeFeedItems([
    metadataFeedItem(job),
    ...lines.map((line, index) => feedItemFromLine(line, index, job, feedEntries?.[index])),
    ...stateFeedItems(job),
    finalStatusItem(job, lines),
  ].filter((item): item is JobFeedItem => Boolean(item)));
  const groups = groupFeedItems(items);
  const summary = buildFeedSummary(job, lines, groups);
  return {
    summary,
    items,
    groups,
    technicalDetails: {
      job_id: job.job_id,
      label: job.label,
      status: job.status,
      returncode: job.returncode,
      metadata: job.metadata ?? {},
      state: job.state ?? {},
      result: job.result ?? {},
    },
  };
}

export function summarizeJobResult(job: Job | null | undefined, rawLines: string[]) {
  if (!job && !rawLines.length) return '';
  const counts = collectJobCounts(rawLines, job?.result);
  const preferred = [
    'tracks checked',
    'tracks matched',
    'tracks found',
    'tracks imported',
    'tracks added to Plex',
    'tracks not found in Plex',
    'missing tracks',
    'failed tracks',
    'items needing review',
    'duplicate albums',
    'files',
    'folders',
    'database rows',
    'tracks',
    'albums',
    'moved',
    'updated',
    'removed',
    'skipped',
    'disk entries',
    'audio files',
  ];
  const parts: string[] = [];
  for (const key of preferred) {
    const value = counts.get(key);
    if (value === undefined) continue;
    if (value === 0 && !/skipped|missing|removed|not found|failed|review/.test(key)) continue;
    parts.push(`${numberFmt.format(value)} ${key}`);
  }
  return parts.slice(0, 5).join(', ');
}

function buildFeedSummary(job: Job, rawLines: string[], groups: JobFeedGroup[]): JobFeedSummary {
  const attention = jobNeedsAttention(job, rawLines);
  const status = statusTone(job, attention);
  const currentPhase = currentJobPhase(job, groups, attention);
  const progressText = progressFromState(job);
  const resultBullets = resultBulletsForJob(job, rawLines);
  const resultTitle = resultTitleForJob(job, attention);
  const friendlyReason = friendlyFailureReason(job, rawLines);
  return {
    title: friendlyJobTitle(job),
    status,
    statusLabel: formatJobStatus(job.status, attention),
    currentPhase,
    progressText,
    resultTitle,
    resultBullets,
    friendlyReason,
    needsAttention: attention,
  };
}

function metadataFeedItem(job: Job): JobFeedItem | null {
  const type = String(job.metadata?.type ?? '');
  const action = String(job.metadata?.action ?? '');
  const playlist = String(job.metadata?.name ?? '');
  if (type !== 'playlist-pipeline' || !action) return null;
  const title = action === 'sync_plex'
    ? `Syncing the ${playlist || 'selected'} playlist with Plex.`
    : `${PLAYLIST_ACTION_LABELS[action] ?? labelize(action)}${playlist ? `: ${playlist}` : ''}.`;
  return {
    id: `metadata-${job.job_id}`,
    phase: action === 'sync_plex' ? 'Plex sync' : 'Preparing',
    status: job.status === 'running' ? 'running' : statusTone(job, false),
    title,
    detail: playlist ? `Playlist: ${playlist}` : undefined,
    time: job.started_at ?? job.created_at,
    paths: [],
    technical: {
      action,
      playlist,
      type,
    },
  };
}

function stateFeedItems(job: Job): JobFeedItem[] {
  const state = job.state;
  if (!state) return [];
  const task = textValue(state.current_task);
  const result = textValue(state.current_result);
  const out: JobFeedItem[] = [];
  if (task) {
    out.push({
      id: `state-task-${job.job_id}`,
      phase: phaseFromText(task, job),
      status: job.status === 'running' ? 'running' : statusTone(job, false),
      title: sentence(friendlyMessage(task, job)),
      detail: result ? sentence(friendlyMessage(result, job)) : undefined,
      time: job.started_at ?? job.created_at,
      raw: task,
      paths: extractPaths(task),
      technical: { current_task: task, current_result: result || undefined },
    });
  }
  return out;
}

function finalStatusItem(job: Job, rawLines: string[]): JobFeedItem | null {
  if (job.status === 'running') return null;
  if (job.status === 'success') {
    return {
      id: `final-${job.job_id}`,
      phase: jobNeedsAttention(job, rawLines) ? 'Needs attention' : 'Finished',
      status: jobNeedsAttention(job, rawLines) ? 'warning' : 'success',
      title: jobNeedsAttention(job, rawLines)
        ? `${friendlyDoneSubject(job)} finished, but some items need attention.`
        : `${friendlyDoneSubject(job)} completed successfully.`,
      time: job.finished_at,
      paths: [],
    };
  }
  if (job.status === 'failed' || job.status === 'killed' || job.status === 'cancelled') {
    return {
      id: `final-${job.job_id}`,
      phase: job.status === 'failed' ? 'Needs attention' : 'Finished',
      status: job.status === 'failed' ? 'error' : 'cancelled',
      title: job.status === 'failed'
        ? `${friendlyDoneSubject(job)} failed.`
        : `${friendlyDoneSubject(job)} was cancelled.`,
      detail: friendlyFailureReason(job, rawLines),
      time: job.finished_at,
      paths: [],
    };
  }
  return null;
}

function feedItemFromLine(raw: string, index: number, job: Job, entry?: JobLogFeedItem): JobFeedItem | null {
  const line = cleanJobLogLine(raw);
  if (!line || isTechnicalLogNoise(line)) return null;
  const paths = extractPaths(line);
  const mapped = friendlyLine(line, job, paths);
  if (!mapped?.title) return null;
  const phase = mapped.phase ?? phaseFromText(line, job);
  const status = mapped.status ?? statusFromLine(line, entry?.level, job.status);
  return {
    id: `${entry?.job_id ?? job.job_id}-${entry?.line ?? index}-${line.slice(0, 32)}`,
    phase,
    status,
    title: mapped.title,
    message: mapped.message,
    detail: mapped.detail,
    time: entry?.created_at ?? job.started_at ?? job.created_at,
    raw,
    source: job.label,
    paths,
    technical: mapped.technical,
  };
}

function friendlyLine(line: string, job: Job, paths: PathHit[]): Partial<JobFeedItem> | null {
  const playlist = String(job.metadata?.name ?? '').trim();
  const pathDetail = paths[0] ? `Path: ${paths[0].short}` : undefined;
  const visible = stripPathsForDisplay(line, paths);
  const lower = line.toLowerCase();
  let match = line.match(/^ACTION:\s*(\S+)\s+NAME:\s*(.*?)\s+TYPE:\s*(.+)$/i);
  if (match) {
    const action = match[1] ?? '';
    const name = match[2] ?? playlist;
    const type = match[3] ?? '';
    return {
      phase: action === 'sync_plex' ? 'Plex sync' : 'Preparing',
      status: 'running',
      title: action === 'sync_plex'
        ? `Syncing the ${name} playlist with Plex.`
        : `${PLAYLIST_ACTION_LABELS[action] ?? labelize(action)}${name ? `: ${name}` : ''}.`,
      technical: { action, playlist: name, type },
    };
  }
  if (/^Preparing Plex sync from (?:current Beets library paths|saved final library paths)/i.test(line)) {
    return {
      phase: 'Preparing',
      status: 'success',
      title: 'Checking the current Beets library paths before syncing to Plex.',
      technical: { original: line },
    };
  }
  if (/^Saved M3U had no final library paths; checking Beets detail/i.test(line)) {
    return {
      phase: 'Scanning',
      status: 'running',
      title: 'Checking Beets for playlist tracks because the saved playlist paths were incomplete.',
    };
  }
  if (/^scan_started$/i.test(line)) return { phase: 'Scanning', status: 'running', title: 'Scanning the playlist items.' };
  if (/^plex_sync_started$/i.test(line)) return { phase: 'Plex sync', status: 'running', title: 'Sending playlist updates to Plex.' };
  if (/^job_completed$/i.test(line)) return { phase: 'Finished', status: 'success', title: 'Playlist sync completed successfully.' };

  match = line.match(/^track_matched(?::|\s+)(.+)$/i);
  if (match) return { phase: 'Matching', status: 'success', title: `Matched track: ${match[1].trim()}.` };

  match = line.match(/^Plex sync issue:\s*(.+)$/i);
  if (match) return { phase: 'Needs attention', status: 'warning', title: 'Plex sync finished with missing tracks.', detail: sentence(match[1]) };

  match = line.match(/^Plex missing examples:\s*(.+)$/i);
  if (match) return { phase: 'Needs attention', status: 'warning', title: 'Some tracks were not found in Plex.', detail: match[1] };

  match = line.match(/Indexed\s+(\d+)\s+Plex track/i);
  if (match) return { phase: 'Scanning', status: 'success', title: `Indexed ${numberFmt.format(Number(match[1]))} Plex tracks for matching.` };

  match = line.match(/(?:matched|found)\s+(\d+)\s*(?:\/\s*(\d+))?\s+track/i);
  if (match) {
    const matched = numberFmt.format(Number(match[1]));
    const total = match[2] ? ` of ${numberFmt.format(Number(match[2]))}` : '';
    return { phase: 'Matching', status: 'success', title: `Matched ${matched}${total} tracks.` };
  }

  match = line.match(/\b(\d+)\s+track\(s\)\s+already in the library/i);
  if (match) return { phase: 'Matching', status: 'success', title: `${numberFmt.format(Number(match[1]))} tracks already found in the library.` };

  match = line.match(/\b(\d+)\s+missing track/i);
  if (match) return { phase: 'Downloading', status: 'warning', title: `${numberFmt.format(Number(match[1]))} tracks need download or review.` };

  if (/beet import|importing/i.test(line)) return { phase: 'Importing', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/write tags|writing tags|tagging|mbsync|musicbrainz/i.test(line)) return { phase: 'Tagging', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/fetchart|embedart|artwork|cover/i.test(line)) return { phase: 'Artwork', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/download|slskd|spotiflac|yt-dlp|soundcloud/i.test(line)) return { phase: 'Downloading', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/cleanup|clean|moved|move|merge|merged|removed|delete|repair/i.test(line)) return { phase: 'Cleanup', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/scan|scanning|read-db|check-missing|loading|lookup|index/i.test(line)) return { phase: 'Scanning', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/match|matched|available in beets/i.test(line)) return { phase: 'Matching', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/plex/i.test(line)) return { phase: 'Plex sync', status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/finished|complete|completed|done|status:ok|elapsed/i.test(line)) return { phase: 'Finished', status: 'success', title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
  if (/error|failed|exception|not found|no such file/i.test(line)) return { phase: 'Needs attention', status: 'error', title: sentence(friendlyMessage(visible, job)), detail: pathDetail };

  if (/^\s*[A-Za-z_][\w-]*:\s*/.test(line)) return null;
  return { phase: phaseFromText(line, job), status: statusFromText(lower), title: sentence(friendlyMessage(visible, job)), detail: pathDetail };
}

function groupFeedItems(items: JobFeedItem[]) {
  return JOB_FEED_PHASE_ORDER.map((phase) => {
    const phaseItems = items.filter((item) => item.phase === phase);
    return {
      phase,
      status: phaseStatus(phaseItems),
      items: phaseItems,
    };
  }).filter((group) => group.items.length > 0);
}

function phaseStatus(items: JobFeedItem[]): JobFeedStatus {
  if (items.some((item) => item.status === 'error')) return 'error';
  if (items.some((item) => item.status === 'warning')) return 'warning';
  if (items.some((item) => item.status === 'running')) return 'running';
  if (items.some((item) => item.status === 'cancelled')) return 'cancelled';
  if (items.every((item) => item.status === 'success')) return 'success';
  return 'info';
}

function currentJobPhase(job: Job, groups: JobFeedGroup[], attention: boolean): JobFeedPhase {
  if (attention) return 'Needs attention';
  if (job.status === 'success') return 'Finished';
  if (job.status === 'failed') return 'Needs attention';
  if (job.status === 'cancelled' || job.status === 'killed') return 'Finished';
  const running = [...groups].reverse().find((group) => group.items.some((item) => item.status === 'running'));
  if (running) return running.phase;
  const latest = [...groups].reverse().find((group) => group.phase !== 'Finished');
  return latest?.phase ?? 'Preparing';
}

function progressFromState(job: Job) {
  const state = job.state;
  const scanned = numberValue(state?.scanned_count ?? state?.scanned ?? state?.done);
  const total = numberValue(state?.total_count ?? state?.total);
  if (scanned !== null && total !== null && total > 0) {
    return `${numberFmt.format(scanned)} of ${numberFmt.format(total)} items processed`;
  }
  const matched = numberValue(job.result?.matched_count ?? job.result?.matched);
  const resultTotal = numberValue(job.result?.total_count ?? job.result?.total);
  if (matched !== null && resultTotal !== null && resultTotal > 0) {
    return `${numberFmt.format(matched)} of ${numberFmt.format(resultTotal)} tracks matched`;
  }
  return '';
}

function resultTitleForJob(job: Job, attention: boolean) {
  const playlist = String(job.metadata?.name ?? '').trim();
  const action = String(job.metadata?.action ?? '').trim();
  if (job.status === 'running') return 'Still working. No new update yet.';
  if (attention) return playlist ? `Playlist sync finished, but ${playlist} needs attention.` : 'Job finished, but some items need attention.';
  if (job.status === 'success' && action === 'sync_plex') return playlist ? `${playlist} playlist synced to Plex.` : 'Playlist synced to Plex.';
  if (job.status === 'success') return `${friendlyDoneSubject(job)} completed.`;
  if (job.status === 'failed') return `${friendlyDoneSubject(job)} failed.`;
  if (job.status === 'killed' || job.status === 'cancelled') return `${friendlyDoneSubject(job)} was cancelled.`;
  return '';
}

function resultBulletsForJob(job: Job, rawLines: string[]) {
  const counts = collectJobCounts(rawLines, job.result);
  const bullets: string[] = [];
  for (const key of Object.keys(RESULT_LABELS)) {
    const value = counts.get(RESULT_LABELS[key]) ?? counts.get(humanKey(key));
    if (value === undefined) continue;
    if (value === 0 && !/missing|failed|review|unmatched/.test(key)) continue;
    bullets.push(`${numberFmt.format(value)} ${RESULT_LABELS[key]}`);
  }
  const summary = summarizeJobResult(job, rawLines);
  if (!bullets.length && summary) bullets.push(...summary.split(', '));
  if (job.status === 'success' && String(job.metadata?.action ?? '') === 'sync_plex') bullets.push('Plex playlist updated');
  return [...new Set(bullets)].slice(0, 5);
}

function friendlyFailureReason(job: Job, rawLines: string[]) {
  const stateError = textValue(job.state?.error_summary) || textValue(job.state?.error);
  if (stateError) return sentence(friendlyMessage(stateError, job));
  const errorLine = [...rawLines].reverse()
    .map(cleanJobLogLine)
    .find((line) => /error|failed|exception|not found|no such file/i.test(line));
  if (!errorLine) return job.status === 'failed' ? 'The backend reported a failure. Open raw log for the technical details.' : '';
  if (/No Beets library tracks are available to sync to Plex/i.test(errorLine)) return 'No matching Beets library tracks were available for the Plex playlist sync.';
  if (/connecting to Plex|Plex.*connect|connection/i.test(errorLine)) return 'Plex sync failed while connecting to Plex.';
  return sentence(friendlyMessage(errorLine.replace(/^ERROR:\s*/i, ''), job));
}

function jobNeedsAttention(job: Job, rawLines: string[]) {
  if (job.status === 'failed') return false;
  const counts = collectJobCounts(rawLines, job.result);
  const attentionKeys = ['tracks not found in Plex', 'missing tracks', 'failed tracks', 'items needing review'];
  return attentionKeys.some((key) => (counts.get(key) ?? 0) > 0) || rawLines.some((line) => /needs attention|Plex sync issue|missing in Plex|review required/i.test(line));
}

function friendlyDoneSubject(job: Job) {
  const playlist = String(job.metadata?.name ?? '').trim();
  const action = String(job.metadata?.action ?? '').trim();
  if (playlist && action === 'sync_plex') return `${playlist} playlist sync`;
  if (playlist) return `${playlist} playlist job`;
  return job.label || 'Job';
}

function statusTone(job: Job, attention: boolean): JobFeedStatus {
  if (attention) return 'warning';
  if (job.status === 'success') return 'success';
  if (job.status === 'failed') return 'error';
  if (job.status === 'cancelled' || job.status === 'killed') return 'cancelled';
  if (job.status === 'running') return 'running';
  return 'info';
}

function statusFromLine(line: string, level?: string, jobStatus?: JobStatus): JobFeedStatus {
  if (level === 'error' || /error|failed|exception|not found|no such file/i.test(line)) return 'error';
  if (level === 'warn' || /warn|skipped|missing|needs attention|issue/i.test(line)) return 'warning';
  if (/finished|complete|completed|done|matched|found|updated|created|synced/i.test(line)) return 'success';
  if (jobStatus === 'running') return 'running';
  return 'info';
}

function statusFromText(lower: string): JobFeedStatus {
  if (/error|failed|exception|not found|no such file/i.test(lower)) return 'error';
  if (/warn|skipped|missing|needs attention|issue|unsafe/i.test(lower)) return 'warning';
  if (/finished|complete|completed|done|matched|found|updated|created|synced/i.test(lower)) return 'success';
  return 'running';
}

function phaseFromText(text: string, job: Job): JobFeedPhase {
  const lower = text.toLowerCase();
  if (/error|failed|exception|not found|needs attention|review/.test(lower)) return 'Needs attention';
  if (/finished|complete|completed|done|status:ok|elapsed/.test(lower)) return 'Finished';
  if (/plex|sync_plex/.test(lower) || job.metadata?.action === 'sync_plex') return 'Plex sync';
  if (/artwork|cover|fetchart|embedart/.test(lower)) return 'Artwork';
  if (/tag|write|mbsync|musicbrainz/.test(lower)) return 'Tagging';
  if (/import|beet import/.test(lower)) return 'Importing';
  if (/download|slskd|spotiflac|yt-dlp|soundcloud/.test(lower)) return 'Downloading';
  if (/match|matched|lookup/.test(lower)) return 'Matching';
  if (/scan|scanning|read-db|check|loading|index/.test(lower)) return 'Scanning';
  if (/cleanup|clean|move|moved|merge|merged|removed|delete|repair/.test(lower)) return 'Cleanup';
  return 'Preparing';
}

function friendlyMessage(value: string, job: Job) {
  const playlist = String(job.metadata?.name ?? '').trim();
  return value
    .replace(/^ERROR:\s*/i, '')
    .replace(/^Preparing Plex sync from saved final library paths$/i, 'Checking the current Beets library paths before syncing to Plex')
    .replace(/^Preparing Plex sync from current Beets library paths$/i, 'Checking the current Beets library paths before syncing to Plex')
    .replace(/\bsync_plex\b/g, playlist ? `sync ${playlist} to Plex` : 'sync to Plex')
    .replace(/\bplaylist-pipeline\b/g, 'playlist job')
    .replace(/_/g, ' ');
}

function dedupeFeedItems(items: JobFeedItem[]) {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = [item.phase, item.status, item.title, item.detail ?? ''].join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function extractPaths(raw: string): PathHit[] {
  const paths = new Map<string, PathHit>();
  const labeled = /(?:^|\b)(?:MISSING|Path|Source|Destination|From|To):\s*((?:[A-Za-z]:\\|\\\\|\/).+)$/gi;
  const quoted = /(['"])((?:[A-Za-z]:\\|\\\\|\/)[\s\S]*?)\1/g;
  const bare = /((?:[A-Za-z]:\\|\\\\|\/)[^\s'"<>]+)/g;
  const addPath = (value: string) => {
    const full = value.trim().replace(/[),.;]+$/g, '');
    if (full.length > 8) paths.set(full, { full, short: shortPath(full) });
  };
  for (const match of raw.matchAll(labeled)) addPath(match[1] ?? '');
  for (const match of raw.matchAll(quoted)) addPath(match[2] ?? '');
  for (const match of raw.matchAll(bare)) addPath(match[1] ?? '');
  const hits = [...paths.values()];
  return hits.filter((hit) => !hits.some((other) => (
    other.full !== hit.full &&
    other.full.length > hit.full.length + 8 &&
    other.full.includes(hit.full)
  )));
}

function stripPathsForDisplay(raw: string, paths: PathHit[]) {
  let display = raw;
  for (const path of paths) display = display.split(path.full).join(path.short);
  return display;
}

function shortPath(value: string) {
  const path = value.trim();
  if (path.length <= 76) return path;
  const normalized = path.replace(/\\/g, '/');
  const parts = normalized.split('/').filter(Boolean);
  const tail = parts.slice(-3).join('/');
  return tail ? `.../${tail}` : `${path.slice(0, 32)}...${path.slice(-28)}`;
}

function sentence(value: string) {
  const trimmed = value.trim();
  if (!trimmed) return '';
  const capped = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capped) ? capped : `${capped}.`;
}

function labelize(value: string) {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\bplex\b/gi, 'Plex')
    .replace(/\bmb\b/gi, 'MusicBrainz')
    .replace(/\bid\b/gi, 'ID')
    .replace(/\b\w/g, (m) => m.toUpperCase());
}

function humanKey(key: string) {
  const normalized = key.replace(/[_-]+/g, ' ').trim().toLowerCase();
  if (RESULT_LABELS[key]) return RESULT_LABELS[key];
  if (normalized === 'rows') return 'database rows';
  if (normalized === 'entries') return 'disk entries';
  if (normalized === 'audio') return 'audio files';
  if (normalized === 'unimported') return 'files not imported';
  if (normalized === 'elapsed seconds') return 'seconds';
  return normalized;
}

function flattenNumbers(record: Record<string, unknown>, prefix = ''): Array<[string, number]> {
  const out: Array<[string, number]> = [];
  for (const [key, value] of Object.entries(record)) {
    const fullKey = prefix ? `${prefix}_${key}` : key;
    if (typeof value === 'number' && Number.isFinite(value)) out.push([fullKey, value]);
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      out.push(...flattenNumbers(value as Record<string, unknown>, fullKey));
    }
  }
  return out;
}

function numberValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function textValue(value: unknown) {
  return typeof value === 'string' && value.trim() ? value.trim() : '';
}
