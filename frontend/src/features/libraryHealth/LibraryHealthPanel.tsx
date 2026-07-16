import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  apiJson, getMbidStatus, getJobResult, startMbidStickingRepair, startTemplateTokenCleanup,
  getRgidGroupDetail, mergeRgidGroup, keepRgidGroupSeparate, undoRgidGroupResolution,
  assignRgidRepresentativeRelease, relinkRgidAlbum, sendRgidAlbumToRepair,
} from '../../api/client';
import { getJob } from '../../api/client';
import type {
  MbidStatusResponse, MbidStickingRepairResult, RgidGroupDetailResponse,
} from '../../api/types';
import { CleanEmptyState, CleanMetricGrid, CleanPanelHeader, CleanSection } from '../../components/CleanPanel';
import { JobStatusCard } from '../../components/JobStatusCard';
import { useJobPoll } from '../../lib/hooks';

interface TemplateTokenItem {
  path: string;
  new_path: string;
  filename: string;
  new_filename: string;
}

interface TemplateTokenResult {
  candidates: number;
  renamed: number;
  db_updates: number;
  skipped: number;
  dry_run: boolean;
  items: TemplateTokenItem[];
}

interface DupAlbum {
  album_id: number;
  track_count: number;
  mb_albumid: string;
  mb_releasegroupid: string;
  year: number;
  aldir: string;
}

interface DupGroup {
  albumartist: string;
  album: string;
  count: number;
  albums: DupAlbum[];
  merge_safe?: boolean;
  merge_target_album_id?: number;
  merge_source_album_ids?: number[];
  merge_reason?: string;
  merge_blockers?: string[];
}

interface RgidDupAlbum {
  album_id: number;
  albumartist: string;
  album: string;
  year: number;
  track_count: number;
  mb_albumid: string;
  aldir: string;
}

interface RgidDupGroup {
  mb_releasegroupid: string;
  count: number;
  albums: RgidDupAlbum[];
}

interface OrphanSample {
  id: number;
  title: string;
  artist: string;
  album: string;
  path: string;
}

interface EmptyAlbum {
  album_id: number;
  albumartist: string;
  album: string;
  year: number;
}

interface LibraryHealthResponse {
  ok: boolean;
  duplicate_albums: DupGroup[];
  duplicate_album_count: number;
  rgid_duplicate_groups: RgidDupGroup[];
  rgid_duplicate_group_count: number;
  orphaned_items: OrphanSample[];
  orphaned_item_count: number;
  orphaned_item_ids: number[];
  empty_albums: EmptyAlbum[];
  empty_album_count: number;
}

interface JobStartResponse { ok: boolean; job_id: string }

interface LibraryHealthPanelProps {
  active?: boolean;
  autoLoad?: boolean;
}

function jobsUrl(jobId: string | null) {
  return jobId ? `/jobs?q=${encodeURIComponent(jobId)}` : '/jobs';
}

function jsonPost(path: string, body?: unknown) {
  return apiJson<JobStartResponse>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function pollJob(jobId: string): Promise<'success' | 'failed'> {
  for (let i = 0; i < 180; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const j = await getJob(jobId);
      if (j.status === 'success') return 'success';
      if (j.status === 'failed' || j.status === 'killed') return 'failed';
    } catch {
      // keep polling
    }
  }
  return 'failed';
}

function formatScanTime(timestamp: number | null) {
  if (!timestamp) return 'Not scanned this session';
  return new Date(timestamp).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function duplicateGroupKey(group: DupGroup) {
  return `${group.albumartist}|${group.album}`;
}

function displayMergeReason(value?: string) {
  return (value || '')
    .replace(/MusicBrainz release IDs/g, 'MusicBrainz Release Group IDs')
    .replace(/release IDs/g, 'Release Group IDs')
    .replace(/release ID/g, 'Release Group ID');
}

// ── Duplicate group row ───────────────────────────────────────────────────────

function DupGroupRow({
  group,
  onMerged,
}: {
  group: DupGroup;
  onMerged: () => void;
}) {
  const navigate = useNavigate();
  const [merging, setMerging] = useState(false);
  const [mergeJobId, setMergeJobId] = useState<string | null>(null);
  const [msg, setMsg] = useState('');
  const { job: mergeJob } = useJobPoll(mergeJobId);

  // Pick the album with the most tracks as the merge target (keep it).
  const sorted = [...group.albums].sort((a, b) => b.track_count - a.track_count);
  const target = sorted.find((al) => al.album_id === group.merge_target_album_id) ?? sorted[0];
  const sourceIds = new Set(group.merge_source_album_ids ?? sorted.slice(1).map((al) => al.album_id));
  const sources = sorted.filter((al) => sourceIds.has(al.album_id));
  const mergeSafe = Boolean(group.merge_safe && target && sources.length);

  const handleMerge = async () => {
    if (!mergeSafe) {
      setMsg(group.merge_reason || 'This duplicate group needs manual review before merging.');
      return;
    }
    if (!window.confirm(
      `Merge ${sources.length} duplicate row(s) into album_id ${target.album_id} (${target.track_count} tracks)?\n\nThis updates the database only — audio files are not touched.`,
    )) return;
    setMerging(true);
    setMsg('');
    setMergeJobId(null);
    try {
      for (const src of sources) {
        const r = await jsonPost('/api/clean/merge-duplicate-album', {
          target_album_id: target.album_id,
          source_album_id: src.album_id,
        });
        const outcome = await pollJob(r.job_id);
        if (outcome !== 'success') {
          setMsg(`Merge of album_id ${src.album_id} failed — check Jobs.`);
          setMerging(false);
          return;
        }
      }
      setMsg(`Merged ${sources.length} duplicate row(s) into album_id ${target.album_id}.`);
      onMerged();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setMerging(false);
    }
  };

  return (
    <div className="rounded border border-amber-800/40 bg-amber-950/20 p-2 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <div className="font-medium text-amber-200">{group.albumartist} — {group.album}</div>
            <Chip
              color={mergeSafe ? 'success' : 'default'}
              label={mergeSafe ? 'Safe merge' : 'Review'}
              size="small"
            />
          </div>
          <div className="mt-1 flex flex-col gap-y-0.5">
            {sorted.map((al) => (
              <span key={al.album_id} className="text-xs text-zinc-400">
                {al.album_id === target.album_id ? (
                  <span className="text-emerald-400">keep </span>
                ) : (
                  <span className="text-rose-400">merge </span>
                )}
                id={al.album_id} · {al.track_count} track(s)
                {al.mb_releasegroupid ? ` · Release Group ${al.mb_releasegroupid.slice(0, 8)}…` : ' · no Release Group ID'}
                {al.mb_albumid ? ` · representative release ${al.mb_albumid.slice(0, 8)}…` : ''}
                {al.year ? ` · ${al.year}` : ''}
                {al.aldir ? <span className="block pl-12 font-mono text-[0.65rem] text-zinc-600">{al.aldir}</span> : null}
              </span>
            ))}
          </div>
          {group.merge_reason && <p className="mt-1 text-xs text-zinc-400">{displayMergeReason(group.merge_reason)}</p>}
          {!mergeSafe && (
            <p className="mt-1 text-xs text-amber-300">
              Merge disabled: {displayMergeReason(group.merge_reason) || 'this group needs review before any database rows are merged.'}
            </p>
          )}
          {msg && <p className="mt-1 text-xs text-zinc-400">{msg}</p>}
        </div>
        <div className="flex shrink-0 gap-1.5">
          <Button
            size="small"
            variant="outlined"
            sx={{ fontSize: '0.72rem', py: 0.25 }}
            onClick={() => navigate(`/library?artist=${encodeURIComponent(group.albumartist)}`)}
          >
            Review details
          </Button>
          <Button
            color="warning"
            disabled={merging || !mergeSafe}
            size="small"
            variant="outlined"
            sx={{ fontSize: '0.72rem', py: 0.25 }}
            onClick={() => void handleMerge()}
            title={!mergeSafe ? (displayMergeReason(group.merge_reason) || 'Merge disabled because this group is not safe to merge automatically.') : undefined}
          >
            {merging ? 'Merging…' : 'Merge'}
          </Button>
        </div>
      </div>
      {mergeJob?.status === 'running' && (
        <div className="mt-1 text-xs text-zinc-500">
          {mergeJob.log?.filter(Boolean).slice(-1)[0] ?? 'Merging…'}
        </div>
      )}
    </div>
  );
}

// ── RGID duplicate group row ──────────────────────────────────────────────────

function RgidDetailPanel({
  rgid,
  fallbackAlbums,
  onResolved,
}: {
  rgid: string;
  fallbackAlbums: RgidDupAlbum[];
  onResolved: () => void;
}) {
  const [detail, setDetail] = useState<RgidGroupDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState('');
  const [reasonInput, setReasonInput] = useState('');
  const [relinkInput, setRelinkInput] = useState<Record<number, string>>({});
  const [releaseChoice, setReleaseChoice] = useState<Record<number, string>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const res = await getRgidGroupDetail(rgid);
      setDetail(res);
      if (!res.ok) setLoadError(res.error || 'Could not load this cluster.');
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [rgid]);

  useEffect(() => { void refresh(); }, [refresh]);

  // Used by per-row actions (assign representative release, relink, repair
  // partial import) that fix one row but don't necessarily resolve the whole
  // cluster -- refresh the panel in place rather than dismissing the card.
  const runJob = async (promise: Promise<JobStartResponse>, successMsg: string) => {
    try {
      const res = await promise;
      const outcome = await pollJob(res.job_id);
      if (outcome === 'success') {
        setActionMsg(successMsg);
        await refresh();
      } else {
        setActionMsg('Job failed — check Jobs for details.');
      }
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    }
  };

  const albums = detail?.albums?.length ? detail.albums : fallbackAlbums;

  const handleMerge = async () => {
    if (!detail?.merge_safe || !detail.merge_target_album_id || !detail.merge_source_album_ids?.length) return;
    const targetId = detail.merge_target_album_id;
    const sources = detail.merge_source_album_ids;
    if (!window.confirm(
      `Merge ${sources.length} row(s) into album_id ${targetId}?\n\nThis updates the database only — audio files are not touched.`,
    )) return;
    setBusyAction('merge');
    setActionMsg('');
    try {
      for (const sourceId of sources) {
        const res = await mergeRgidGroup({ mbReleaseGroupId: rgid, targetAlbumId: targetId, sourceAlbumId: sourceId });
        const outcome = await pollJob(res.job_id);
        if (outcome !== 'success') {
          setActionMsg(`Merge of album_id ${sourceId} failed — check Jobs.`);
          setBusyAction(null);
          return;
        }
      }
      setActionMsg(`Merged ${sources.length} row(s) into album_id ${targetId}.`);
      await refresh();
      onResolved();
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyAction(null);
    }
  };

  const handleKeepSeparate = async () => {
    setBusyAction('keep-separate');
    setActionMsg('');
    try {
      const res = await keepRgidGroupSeparate({ mbReleaseGroupId: rgid, reason: reasonInput || undefined });
      if (res.ok) {
        setActionMsg('Marked as separate editions — this cluster will not be re-flagged.');
        onResolved();
      } else {
        setActionMsg(res.error || 'Failed to save decision.');
      }
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyAction(null);
    }
  };

  const handleUndoResolution = async () => {
    setBusyAction('undo-resolution');
    setActionMsg('');
    try {
      await undoRgidGroupResolution(rgid);
      setActionMsg('Resolution cleared — this cluster may be re-flagged on the next scan.');
      await refresh();
    } catch (err) {
      setActionMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyAction(null);
    }
  };

  const handleAssign = async (albumId: number) => {
    const mbAlbumId = releaseChoice[albumId];
    if (!mbAlbumId) return;
    setBusyAction(`assign-${albumId}`);
    setActionMsg('');
    await runJob(
      assignRgidRepresentativeRelease({ albumId, mbAlbumId }),
      `Assigned representative release to album_id ${albumId}.`,
    );
    setBusyAction(null);
  };

  const handleRelink = async (albumId: number) => {
    const raw = (relinkInput[albumId] || '').trim();
    const uuid = raw.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i)?.[0]?.toLowerCase();
    if (raw && !uuid) {
      setActionMsg('Paste a MusicBrainz release URL or UUID, or leave blank to search by artist/album.');
      return;
    }
    setBusyAction(`relink-${albumId}`);
    setActionMsg('');
    await runJob(
      relinkRgidAlbum({ albumId, mbAlbumId: uuid }),
      `Relinked album_id ${albumId}.`,
    );
    setBusyAction(null);
  };

  const handleRepairPartial = async (albumId: number) => {
    setBusyAction(`repair-${albumId}`);
    setActionMsg('');
    await runJob(sendRgidAlbumToRepair(albumId), `Repaired partial import for album_id ${albumId}.`);
    setBusyAction(null);
  };

  return (
    <div className="mt-2 space-y-3 rounded border border-blue-900/40 bg-blue-950/10 p-2">
      {loading && <div className="text-xs text-zinc-500">Loading cluster details…</div>}
      {loadError && <Alert severity="error" sx={{ fontSize: '0.72rem' }}>{loadError}</Alert>}
      {actionMsg && <p className="text-xs text-zinc-400">{actionMsg}</p>}

      {detail?.resolution && (
        <div className="rounded border border-emerald-900/50 bg-emerald-950/20 p-2 text-xs text-emerald-300">
          Resolved: kept as separate editions
          {detail.resolution.reason ? ` — ${detail.resolution.reason}` : ''}.
          <Button
            color="inherit"
            disabled={busyAction === 'undo-resolution'}
            size="small"
            variant="text"
            sx={{ fontSize: '0.65rem', ml: 1, py: 0, minWidth: 0 }}
            onClick={() => void handleUndoResolution()}
          >
            Undo
          </Button>
        </div>
      )}

      {/* Merge duplicate rows */}
      <div className="space-y-1">
        <div className="text-[0.68rem] font-semibold uppercase text-blue-400">Merge duplicate rows</div>
        {detail?.merge_safe ? (
          <Button
            color="warning"
            disabled={busyAction === 'merge'}
            size="small"
            variant="outlined"
            sx={{ fontSize: '0.72rem', py: 0.25 }}
            onClick={() => void handleMerge()}
          >
            {busyAction === 'merge' ? 'Merging…' : `Merge into album_id ${detail.merge_target_album_id}`}
          </Button>
        ) : (
          <p className="text-xs text-amber-300">
            Merge disabled: {displayMergeReason(detail?.merge_reason) || 'this cluster needs review before any database rows are merged.'}
            {detail?.merge_blockers?.length ? ` (${detail.merge_blockers.join('; ')})` : ''}
          </p>
        )}
      </div>

      {/* Keep separate / mark valid editions */}
      {!detail?.resolution && (
        <div className="space-y-1">
          <div className="text-[0.68rem] font-semibold uppercase text-blue-400">Keep separate / valid editions</div>
          <div className="flex flex-wrap items-center gap-1.5">
            <input
              type="text"
              placeholder="Optional reason (e.g. deluxe vs. standard edition)"
              value={reasonInput}
              onChange={(e) => setReasonInput(e.target.value)}
              className="min-w-[220px] flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
            />
            <Button
              size="small"
              variant="outlined"
              disabled={busyAction === 'keep-separate'}
              sx={{ fontSize: '0.72rem', py: 0.25 }}
              onClick={() => void handleKeepSeparate()}
            >
              {busyAction === 'keep-separate' ? 'Saving…' : 'Keep separate'}
            </Button>
          </div>
        </div>
      )}

      {/* Choose representative release / Relink / Repair partial import, per album row */}
      <div className="space-y-2">
        <div className="text-[0.68rem] font-semibold uppercase text-blue-400">Per-row actions</div>
        {albums.map((al) => {
          const isPartial = al.track_count > 0 && al.track_count <= 2 && !al.mb_albumid;
          return (
            <div key={al.album_id} className="rounded border border-zinc-800 bg-zinc-900/40 p-1.5 text-xs">
              <div className="text-zinc-300">
                id={al.album_id} · {al.albumartist} — {al.album}
                {al.year ? ` (${al.year})` : ''} · {al.track_count} track(s)
                {al.mb_albumid ? ` · release ${al.mb_albumid.slice(0, 8)}…` : ' · no representative release'}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {(detail?.candidate_releases?.length ?? 0) > 0 && (
                  <>
                    <select
                      value={releaseChoice[al.album_id] ?? ''}
                      onChange={(e) => setReleaseChoice((prev) => ({ ...prev, [al.album_id]: e.target.value }))}
                      className="rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 text-[0.68rem] text-zinc-200"
                    >
                      <option value="">Choose representative release…</option>
                      {detail!.candidate_releases.map((c) => (
                        <option key={c.mb_albumid} value={c.mb_albumid}>
                          {c.date || '????'} · {c.country || '??'} · {c.track_count} tr · {c.status || 'unknown'}
                        </option>
                      ))}
                    </select>
                    <Button
                      size="small"
                      variant="text"
                      disabled={!releaseChoice[al.album_id] || busyAction === `assign-${al.album_id}`}
                      sx={{ fontSize: '0.65rem', py: 0.1, minWidth: 0 }}
                      onClick={() => void handleAssign(al.album_id)}
                    >
                      Assign
                    </Button>
                  </>
                )}
                <input
                  type="text"
                  placeholder="Relink: paste MB release URL/UUID (blank = search)"
                  value={relinkInput[al.album_id] ?? ''}
                  onChange={(e) => setRelinkInput((prev) => ({ ...prev, [al.album_id]: e.target.value }))}
                  className="min-w-[220px] flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[0.68rem] text-zinc-200"
                />
                <Button
                  size="small"
                  variant="text"
                  disabled={busyAction === `relink-${al.album_id}`}
                  sx={{ fontSize: '0.65rem', py: 0.1, minWidth: 0 }}
                  onClick={() => void handleRelink(al.album_id)}
                >
                  {busyAction === `relink-${al.album_id}` ? 'Relinking…' : 'Relink'}
                </Button>
                {isPartial && (
                  <Button
                    size="small"
                    variant="text"
                    color="warning"
                    disabled={busyAction === `repair-${al.album_id}`}
                    sx={{ fontSize: '0.65rem', py: 0.1, minWidth: 0 }}
                    onClick={() => void handleRepairPartial(al.album_id)}
                    title="Route this low-track-count partial import through MBID repair"
                  >
                    {busyAction === `repair-${al.album_id}` ? 'Repairing…' : 'Repair partial import'}
                  </Button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RgidDupGroupRow({
  group,
  onResolved,
}: {
  group: RgidDupGroup;
  onResolved: () => void;
}) {
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const mbUrl = `https://musicbrainz.org/release-group/${group.mb_releasegroupid}`;
  return (
    <div className="rounded border border-blue-800/40 bg-blue-950/20 p-2 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-blue-300">{group.mb_releasegroupid}</span>
            <Chip color="info" label={`${group.count} rows`} size="small" />
          </div>
          <div className="flex flex-col gap-y-0.5">
            {group.albums.map((al) => (
              <span key={al.album_id} className="text-xs text-zinc-400">
                id={al.album_id} · <span className="text-zinc-300">{al.albumartist} — {al.album}</span>
                {al.year ? ` (${al.year})` : ''}
                {` · ${al.track_count} track(s)`}
                {group.mb_releasegroupid ? ` · Release Group ${group.mb_releasegroupid.slice(0, 8)}…` : ' · no Release Group ID'}
                {al.mb_albumid ? ` · representative release ${al.mb_albumid.slice(0, 8)}…` : ''}
                {al.aldir ? <span className="block pl-8 font-mono text-[0.65rem] text-zinc-600">{al.aldir}</span> : null}
              </span>
            ))}
          </div>
          <p className="text-[0.68rem] text-zinc-500">
            Same MusicBrainz Release Group ID — likely the same album identity, but different representative releases, formats, or split imports still need review before merging.
          </p>
        </div>
        <div className="flex shrink-0 flex-col gap-1 items-end">
          <Button
            size="small"
            variant="outlined"
            sx={{ fontSize: '0.72rem', py: 0.25 }}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? 'Hide actions' : 'Resolve cluster'}
          </Button>
          <a
            href={mbUrl}
            target="_blank"
            rel="noreferrer"
            className="text-[0.65rem] text-blue-400 underline decoration-blue-800 hover:text-blue-300"
          >
            MusicBrainz
          </a>
          <Button
            color="inherit"
            size="small"
            variant="text"
            sx={{ fontSize: '0.65rem', py: 0.25, color: 'text.secondary' }}
            onClick={() => navigate(`/library?artist=${encodeURIComponent(group.albums[0]?.albumartist ?? '')}`)}
          >
            Open in Library
          </Button>
        </div>
      </div>
      {expanded && (
        <RgidDetailPanel
          rgid={group.mb_releasegroupid}
          fallbackAlbums={group.albums}
          onResolved={onResolved}
        />
      )}
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export function LibraryHealthPanel({ active = true, autoLoad = true }: LibraryHealthPanelProps) {
  const navigate = useNavigate();
  const [data, setData] = useState<LibraryHealthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [lastScanAt, setLastScanAt] = useState<number | null>(null);
  const [scanJobId, setScanJobId] = useState<string | null>(null);

  const [orphanBusy, setOrphanBusy] = useState(false);
  const [orphanMsg, setOrphanMsg] = useState('');
  const [orphanJobId, setOrphanJobId] = useState<string | null>(null);
  const [emptyBusy, setEmptyBusy] = useState(false);
  const [emptyMsg, setEmptyMsg] = useState('');
  const [emptyJobId, setEmptyJobId] = useState<string | null>(null);
  const { job: scanJob } = useJobPoll(scanJobId);
  const { job: orphanJob } = useJobPoll(orphanJobId);
  const { job: emptyJob } = useJobPoll(emptyJobId);
  const scanPending = Boolean(scanJobId && !scanJob);
  const scanRunning = scanJob?.status === 'running';
  const scanBusy = loading || scanPending || scanRunning;

  useEffect(() => {
    if (scanJob?.status !== 'success') return;
    const result = getJobResult<LibraryHealthResponse>(scanJob);
    if (!result) return;
    setData(result);
    setLastScanAt(Date.now());
  }, [scanJob]);

  useEffect(() => {
    if (!scanJob || scanJob.status === 'running' || scanJob.status === 'success') return;
    setError('Library database health scan failed. Open Jobs for the full log.');
  }, [scanJob]);

  // Template token state
  const [tokenJobId, setTokenJobId] = useState<string | null>(null);
  const [tokenResult, setTokenResult] = useState<TemplateTokenResult | null>(null);
  const [tokenBusy, setTokenBusy] = useState(false);
  const [tokenMsg, setTokenMsg] = useState('');
  const { job: tokenJob } = useJobPoll(tokenJobId);

  useEffect(() => {
    if (!tokenJob || tokenJob.status === 'running') return;
    const result = getJobResult<TemplateTokenResult>(tokenJob);
    if (result) setTokenResult(result);
    if (tokenJob.status !== 'success') setTokenMsg('Template token job failed — check Jobs.');
    setTokenJobId(null);
  }, [tokenJob]);

  // Merge all duplicates state
  const [mergeAllBusy, setMergeAllBusy] = useState(false);
  const [mergeAllMsg, setMergeAllMsg] = useState('');

  // RGID groups dismissed as "keep separate"
  const [dismissedRgids, setDismissedRgids] = useState<Set<string>>(new Set());

  // MB coverage state
  const [mbStatus, setMbStatus] = useState<MbidStatusResponse | null>(null);
  const [mbLoading, setMbLoading] = useState(false);
  const [mbError, setMbError] = useState('');
  const [mbFixJobId, setMbFixJobId] = useState<string | null>(null);
  const [mbFixMsg, setMbFixMsg] = useState('');
  const { job: mbFixJob } = useJobPoll(mbFixJobId);

  useEffect(() => {
    if (mbFixJob?.status === 'success') {
      const r = (mbFixJob.result ?? {}) as MbidStickingRepairResult;
      const changed = r.albums_changed ?? 0;
      const resolved = r.resolved_album_rows ?? 0;
      const unresolved = r.unresolved_count ?? 0;
      const failed = r.failed_count ?? 0;
      const alreadyFixed = r.skipped_already_fixed ?? 0;
      if (!changed && !resolved && !unresolved && !failed) {
        setMbFixMsg('No changes made — nothing to fix.');
      } else {
        setMbFixMsg(
          `Fixed ${changed} album(s) (${resolved} newly linked to a MusicBrainz release). ` +
          `${alreadyFixed} already fixed. ` +
          (unresolved ? `${unresolved} need manual review. ` : '') +
          (failed ? `${failed} failed to write tags. ` : '') +
          'Rescanning…'
        );
      }
      setMbFixJobId(null);
      void handleMbCheck();
    } else if (mbFixJob?.status === 'failed' || mbFixJob?.status === 'killed') {
      setMbFixMsg('Fix job failed — check Jobs for details.');
      setMbFixJobId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mbFixJob?.status]);

  const _SESSION_DATA_KEY = 'beets.health.data';
  const _SESSION_TS_KEY   = 'beets.health.ts';
  const _SESSION_DONE_KEY = 'beets.health.scanned';

  // Persist scan results across re-mounts (tab navigation resets React state).
  useEffect(() => {
    if (!data) return;
    try {
      sessionStorage.setItem(_SESSION_DATA_KEY, JSON.stringify(data));
      sessionStorage.setItem(_SESSION_TS_KEY, String(Date.now()));
    } catch { /* storage full or private mode */ }
  }, [data]);

  // Restore cached data on mount so the panel isn't blank after tab navigation.
  useEffect(() => {
    if (data) return;
    try {
      const raw = sessionStorage.getItem(_SESSION_DATA_KEY);
      const ts  = Number(sessionStorage.getItem(_SESSION_TS_KEY) || 0);
      if (raw) {
        setData(JSON.parse(raw) as LibraryHealthResponse);
        setLastScanAt(ts || null);
      }
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    setScanJobId(null);
    try {
      const res = await jsonPost('/api/clean/library-health/scan');
      if (!res.job_id) throw new Error('Library database health scan did not return a job id');
      setScanJobId(res.job_id);
      sessionStorage.setItem(_SESSION_DONE_KEY, '1');
      window.dispatchEvent(new Event('beets:jobs-changed'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  // Auto-scan once per browser session. sessionStorage key prevents re-firing on every
  // tab navigation (component unmounts/remounts reset React state but not sessionStorage).
  useEffect(() => {
    if (!autoLoad || !active || loading || scanJobId) return;
    if (sessionStorage.getItem(_SESSION_DONE_KEY)) return;
    void load();
  }, [active, autoLoad, loading, load, scanJobId]);

  async function handleOrphanDryRun() {
    setOrphanBusy(true);
    setOrphanMsg('');
    try {
      const res = await jsonPost('/api/clean/remove-orphaned-items', {
        dry_run: true,
        item_ids: data?.orphaned_item_ids ?? [],
      });
      const outcome = await pollJob(res.job_id);
      if (outcome === 'success') {
        setOrphanMsg(`Dry run: would remove ${data?.orphaned_item_count ?? 0} orphaned item(s). Run "Remove" to apply.`);
      } else {
        setOrphanMsg('Dry run job failed — check Jobs for details.');
      }
    } catch (err) {
      setOrphanMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setOrphanBusy(false);
    }
  }

  async function handleOrphanRemove() {
    if (!window.confirm(`Remove ${data?.orphaned_item_count ?? 0} orphaned item(s) from the beets database? This cannot be undone.`)) return;
    setOrphanBusy(true);
    setOrphanMsg('');
    setOrphanJobId(null);
    try {
      const res = await jsonPost('/api/clean/remove-orphaned-items', {
        dry_run: false,
        item_ids: data?.orphaned_item_ids ?? [],
      });
      setOrphanJobId(res.job_id);
      const outcome = await pollJob(res.job_id);
      if (outcome === 'success') void load();
    } catch (err) {
      setOrphanMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setOrphanBusy(false);
    }
  }

  async function handleEmptyDryRun() {
    setEmptyBusy(true);
    setEmptyMsg('');
    try {
      const ids = data?.empty_albums.map((a) => a.album_id) ?? [];
      const res = await jsonPost('/api/clean/remove-empty-albums', { dry_run: true, album_ids: ids });
      const outcome = await pollJob(res.job_id);
      setEmptyMsg(outcome === 'success'
        ? `Dry run: would remove ${ids.length} empty album row(s). Run "Remove" to apply.`
        : 'Dry run failed — check Jobs.');
    } catch (err) {
      setEmptyMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setEmptyBusy(false);
    }
  }

  async function handleEmptyRemove() {
    const ids = data?.empty_albums.map((a) => a.album_id) ?? [];
    if (!window.confirm(`Remove ${ids.length} empty album row(s) from the beets database?`)) return;
    setEmptyBusy(true);
    setEmptyMsg('');
    setEmptyJobId(null);
    try {
      const res = await jsonPost('/api/clean/remove-empty-albums', { dry_run: false, album_ids: ids });
      setEmptyJobId(res.job_id);
      const outcome = await pollJob(res.job_id);
      if (outcome === 'success') void load();
    } catch (err) {
      setEmptyMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setEmptyBusy(false);
    }
  }

  async function handleTokenScan() {
    setTokenBusy(true);
    setTokenMsg('');
    setTokenResult(null);
    setTokenJobId(null);
    try {
      const r = await startTemplateTokenCleanup({ dryRun: true });
      setTokenJobId(r.job_id);
    } catch (err) {
      setTokenMsg(err instanceof Error ? err.message : String(err));
      setTokenBusy(false);
    } finally {
      setTokenBusy(false);
    }
  }

  async function handleTokenFix() {
    const count = tokenResult?.candidates ?? 0;
    if (!window.confirm(`Rename ${count} file(s) with unresolved path-template tokens? Beets DB paths will also be updated.`)) return;
    setTokenBusy(true);
    setTokenMsg('');
    setTokenJobId(null);
    setTokenResult(null);
    try {
      const r = await startTemplateTokenCleanup({ dryRun: false });
      setTokenJobId(r.job_id);
    } catch (err) {
      setTokenMsg(err instanceof Error ? err.message : String(err));
      setTokenBusy(false);
    } finally {
      setTokenBusy(false);
    }
  }

  async function handleMbCheck() {
    setMbLoading(true);
    setMbError('');
    try {
      const res = await getMbidStatus();
      setMbStatus(res);
    } catch (err) {
      setMbError(err instanceof Error ? err.message : String(err));
    } finally {
      setMbLoading(false);
    }
  }

  async function handleMbFix() {
    const releaseGaps = mbStatus?.item_release_gap_rows ?? 0;
    const trackGaps = mbStatus?.track_recording_gap_rows ?? 0;
    const inferred = mbStatus?.inferred_album_mbid_rows ?? 0;
    const total = releaseGaps + trackGaps + inferred;
    if (!window.confirm(
      `Fix ${total} MB ID gap row(s)?\n\n` +
      `• ${inferred} album(s) with inferrable Beets representative release ID\n` +
      `• ${releaseGaps} item representative-release gap row(s)\n` +
      `• ${trackGaps} item track recording gap row(s)\n\n` +
      `Tags will be written after fixing DB rows.`
    )) return;
    setMbFixMsg('');
    setMbFixJobId(null);
    try {
      const r = await startMbidStickingRepair({ dryRun: false, limit: 500 });
      setMbFixJobId(r.job_id);
    } catch (err) {
      setMbFixMsg(err instanceof Error ? err.message : String(err));
    }
  }

  const safeDuplicateGroups = data?.duplicate_albums.filter((grp) => grp.merge_safe) ?? [];
  const safeDuplicatePairCount = safeDuplicateGroups.reduce(
    (total, grp) => total + (grp.merge_source_album_ids?.length ?? Math.max(0, grp.albums.length - 1)),
    0,
  );

  async function handleMergeAll() {
    const groups = safeDuplicateGroups;
    if (!groups.length || safeDuplicatePairCount <= 0) {
      setMergeAllMsg('No duplicate album groups are safe to merge automatically.');
      return;
    }
    if (!window.confirm(`Merge ${safeDuplicatePairCount} safe duplicate album row(s) across ${groups.length} group(s)? Database only — audio files are not moved.`)) return;
    setMergeAllBusy(true);
    setMergeAllMsg('');
    let merged = 0;
    let failed = 0;
    const mergedGroupKeys = new Set<string>();
    try {
      for (const grp of groups) {
        const sorted = [...grp.albums].sort((a, b) => b.track_count - a.track_count);
        const target = sorted.find((al) => al.album_id === grp.merge_target_album_id) ?? sorted[0];
        const sourceIds = new Set(grp.merge_source_album_ids ?? sorted.slice(1).map((al) => al.album_id));
        const sources = sorted.filter((al) => sourceIds.has(al.album_id));
        let groupFailed = false;
        for (const src of sources) {
          try {
            const r = await jsonPost('/api/clean/merge-duplicate-album', {
              target_album_id: target.album_id,
              source_album_id: src.album_id,
            });
            const outcome = await pollJob(r.job_id);
            if (outcome === 'success') merged += 1;
            else {
              failed += 1;
              groupFailed = true;
            }
          } catch {
            failed += 1;
            groupFailed = true;
          }
        }
        if (!groupFailed) mergedGroupKeys.add(duplicateGroupKey(grp));
      }
      setMergeAllMsg(
        failed
          ? `Merged ${merged} pair(s), ${failed} failed — check Jobs for details.`
          : `Merged ${merged} safe duplicate pair(s) successfully.`
      );
      if (!failed && mergedGroupKeys.size > 0) {
        setData(d => d ? {
          ...d,
          duplicate_albums: d.duplicate_albums.filter(g => !mergedGroupKeys.has(duplicateGroupKey(g))),
          duplicate_album_count: Math.max(0, d.duplicate_album_count - mergedGroupKeys.size),
        } : d);
      }
    } catch (err) {
      setMergeAllMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setMergeAllBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <CleanPanelHeader
        title="Library Database Health"
        description="Database rows that can make albums look broken even when the audio files are fine."
        meta={(
          <>
            <span>Last scan: {formatScanTime(lastScanAt)}</span>
            {scanBusy ? <span>Scan running in Jobs</span> : null}
          </>
        )}
        actions={(
          <>
            {scanJobId ? (
              <Button size="small" variant="outlined" onClick={() => navigate(jobsUrl(scanJobId))}>
                Jobs
              </Button>
            ) : null}
            <Button disabled={scanBusy} size="small" variant="outlined" onClick={() => {
              sessionStorage.removeItem(_SESSION_DONE_KEY);
              void load();
            }}>
              {scanBusy ? 'Scanning...' : 'Refresh'}
            </Button>
          </>
        )}
      />

      {scanBusy && <LinearProgress sx={{ borderRadius: 1 }} />}
      {error && <Alert severity="error">{error}</Alert>}
      <JobStatusCard job={scanJob} runningLabel="Scanning library database health..." logLines={4} />

      {data && (
        <div className="space-y-6">
          <CleanMetricGrid
            items={[
              {
                label: 'Duplicate albums',
                value: data.duplicate_album_count,
                tone: data.duplicate_album_count ? 'warning' : 'success',
              },
              {
                label: 'Same Release Group ID groups',
                value: data.rgid_duplicate_group_count ?? 0,
                tone: (data.rgid_duplicate_group_count ?? 0) ? 'info' : 'success',
              },
              {
                label: 'Orphaned items',
                value: data.orphaned_item_count,
                tone: data.orphaned_item_count ? 'danger' : 'success',
              },
              {
                label: 'Empty albums',
                value: data.empty_album_count,
                tone: data.empty_album_count ? 'warning' : 'success',
              },
            ]}
          />

          {/* ── Duplicate albums ─────────────────────────────────────── */}
          <CleanSection
            title="Duplicate Album Entries"
            description="Same artist and album title appearing more than once in the database. Rows with the same MusicBrainz Release Group ID are likely duplicate album rows; different Release Group IDs may be separate albums and require review."
            tone={data.duplicate_album_count > 0 ? 'warning' : 'success'}
            count={(
              <Chip
                color={data.duplicate_album_count > 0 ? 'warning' : 'success'}
                label={data.duplicate_album_count}
                size="small"
              />
            )}
            actions={data.duplicate_album_count > 1 ? (
              <Button
                color="warning"
                disabled={mergeAllBusy || safeDuplicatePairCount <= 0}
                size="small"
                variant="outlined"
                onClick={() => void handleMergeAll()}
              >
                {mergeAllBusy ? 'Merging safe…' : `Merge safe (${safeDuplicatePairCount})`}
              </Button>
            ) : null}
          >
            {mergeAllMsg && <p className="mt-1 mb-2 text-xs text-zinc-400">{mergeAllMsg}</p>}
            {data.duplicate_album_count > 0 && (
              <p className="mt-1 mb-2 text-xs text-zinc-400">
                {safeDuplicatePairCount > 0
                  ? `${safeDuplicatePairCount} row(s) can be merged automatically; ${data.duplicate_album_count - safeDuplicateGroups.length} group(s) need review.`
                  : 'No duplicate album groups are safe to merge automatically.'}
              </p>
            )}
            {data.duplicate_albums.length > 0 && (
              <div className="space-y-2">
                {data.duplicate_albums.map((grp) => (
                  <DupGroupRow
                    key={`${grp.albumartist}|${grp.album}`}
                    group={grp}
                    onMerged={() => setData(d => d ? {
                      ...d,
                      duplicate_albums: d.duplicate_albums.filter(g => g !== grp),
                      duplicate_album_count: Math.max(0, d.duplicate_album_count - 1),
                    } : d)}
                  />
                ))}
              </div>
            )}
            {data.duplicate_album_count === 0 && (
              <CleanEmptyState title="No duplicate album entries found" tone="success" />
            )}
          </CleanSection>

          {/* ── RGID duplicate groups ────────────────────────────────── */}
          {(() => {
            const rgidGroups = (data.rgid_duplicate_groups ?? []).filter(
              (g) => !dismissedRgids.has(g.mb_releasegroupid)
            );
            const count = rgidGroups.length;
            return (
              <CleanSection
                title="Same Release Group ID — Multiple Rows"
                description="Multiple database album rows share the same MusicBrainz Release Group ID. Representative release IDs may differ for tracklist compatibility, but the album identity is the release group."
                tone={count > 0 ? 'info' : 'success'}
                count={(
                  <Chip
                    color={count > 0 ? 'info' : 'success'}
                    label={count}
                    size="small"
                  />
                )}
              >
                {count === 0 && (
                  <CleanEmptyState title="No same-RGID duplicate rows found" tone="success" />
                )}
                {count > 0 && (
                  <div className="space-y-2">
                    {rgidGroups.map((grp) => (
                      <RgidDupGroupRow
                        key={grp.mb_releasegroupid}
                        group={grp}
                        onResolved={() =>
                          setDismissedRgids((prev) => new Set([...prev, grp.mb_releasegroupid]))
                        }
                      />
                    ))}
                  </div>
                )}
              </CleanSection>
            );
          })()}

          {/* ── Orphaned items ───────────────────────────────────────── */}
          <CleanSection
            title="Orphaned Items"
            description="Beets item rows whose audio file no longer exists on disk."
            tone={data.orphaned_item_count > 0 ? 'danger' : 'success'}
            count={(
              <Chip
                color={data.orphaned_item_count > 0 ? 'error' : 'success'}
                label={data.orphaned_item_count}
                size="small"
              />
            )}
            actions={data.orphaned_item_count > 0 ? (
                <div className="flex gap-2">
                  <Button disabled={orphanBusy} size="small" variant="outlined" onClick={() => void handleOrphanDryRun()}>
                    Dry Run
                  </Button>
                  <Button color="error" disabled={orphanBusy} size="small" variant="contained" onClick={() => void handleOrphanRemove()}>
                    {orphanBusy ? 'Removing…' : `Remove ${data.orphaned_item_count}`}
                  </Button>
                </div>
              ) : null}
          >
            {orphanMsg && <p className="mt-2 text-xs text-zinc-400">{orphanMsg}</p>}
            <JobStatusCard job={orphanJob} runningLabel="Removing orphaned items…" logLines={2} className="mt-2" />
            {data.orphaned_item_count === 0 && (
              <CleanEmptyState title="All item rows have matching files on disk" tone="success" />
            )}
            {data.orphaned_items.length > 0 && (
              <div className="mt-3 space-y-1">
                {data.orphaned_items.slice(0, 8).map((item) => (
                  <div key={item.id} className="text-xs text-zinc-500">
                    {item.artist} — {item.title} <span className="text-rose-400">({item.path})</span>
                  </div>
                ))}
                {data.orphaned_item_count > 8 && (
                  <div className="text-xs text-zinc-600">…and {data.orphaned_item_count - 8} more</div>
                )}
              </div>
            )}
          </CleanSection>

          {/* ── Empty albums ─────────────────────────────────────────── */}
          <CleanSection
            title="Empty Album Rows"
            description="Album records in the beets database with zero track rows."
            tone={data.empty_album_count > 0 ? 'warning' : 'success'}
            count={(
              <Chip
                color={data.empty_album_count > 0 ? 'warning' : 'success'}
                label={data.empty_album_count}
                size="small"
              />
            )}
            actions={data.empty_album_count > 0 ? (
                <div className="flex gap-2">
                  <Button disabled={emptyBusy} size="small" variant="outlined" onClick={() => void handleEmptyDryRun()}>
                    Dry Run
                  </Button>
                  <Button color="warning" disabled={emptyBusy} size="small" variant="contained" onClick={() => void handleEmptyRemove()}>
                    {emptyBusy ? 'Removing…' : `Remove ${data.empty_album_count}`}
                  </Button>
                </div>
              ) : null}
          >
            {emptyMsg && <p className="mt-2 text-xs text-zinc-400">{emptyMsg}</p>}
            <JobStatusCard job={emptyJob} runningLabel="Removing empty album rows…" logLines={2} className="mt-2" />
            {data.empty_album_count === 0 && (
              <CleanEmptyState title="No empty album rows found" tone="success" />
            )}
            {data.empty_albums.length > 0 && (
              <div className="mt-3 space-y-1">
                {data.empty_albums.slice(0, 8).map((al) => (
                  <div key={al.album_id} className="text-xs text-zinc-500">
                    {al.albumartist} — {al.album} {al.year ? `(${al.year})` : ''} <span className="text-zinc-600">id={al.album_id}</span>
                  </div>
                ))}
                {data.empty_album_count > 8 && (
                  <div className="text-xs text-zinc-600">…and {data.empty_album_count - 8} more</div>
                )}
              </div>
            )}
          </CleanSection>

          {/* ── MB ID coverage ───────────────────────────────────────── */}
          <CleanSection
            title="MusicBrainz ID Coverage"
            description="Albums missing MusicBrainz Release Group ID coverage, representative release IDs needed for Beets compatibility, or track recording IDs. Scans the full library."
            tone={
              mbStatus && (mbStatus.missing_album_mb > 0 || (mbStatus.item_release_gap_rows ?? 0) > 0 || (mbStatus.track_recording_gap_rows ?? 0) > 0)
                ? 'warning' : mbStatus ? 'success' : 'neutral'
            }
            actions={(
              <div className="flex gap-2">
                <Button
                  disabled={mbLoading || mbFixJob?.status === 'running'}
                  size="small"
                  variant="outlined"
                  onClick={() => void handleMbCheck()}
                >
                  {mbLoading ? 'Scanning…' : mbStatus ? 'Re-check' : 'Check Coverage'}
                </Button>
                {mbStatus && ((mbStatus.item_release_gap_rows ?? 0) + (mbStatus.track_recording_gap_rows ?? 0) + (mbStatus.inferred_album_mbid_rows ?? 0)) > 0 && (
                  <Button
                    color="warning"
                    disabled={mbFixJob?.status === 'running'}
                    size="small"
                    variant="contained"
                    onClick={() => void handleMbFix()}
                  >
                    Fix MB ID Gaps
                  </Button>
                )}
              </div>
            )}
          >
            {mbLoading && <LinearProgress sx={{ borderRadius: 1, mt: 1 }} />}
            {mbError && <Alert severity="error" sx={{ mt: 1 }}>{mbError}</Alert>}
            {mbFixMsg && <p className="mt-1 text-xs text-zinc-400">{mbFixMsg}</p>}
            <JobStatusCard job={mbFixJob} runningLabel="Fixing MB ID gaps…" logLines={3} className="mt-2" />
            {mbFixJob?.status === 'success' && ((mbFixJob.result as MbidStickingRepairResult | undefined)?.unresolved_albums?.length ?? 0) > 0 && (
              <div className="mt-2 space-y-1 rounded border border-amber-900/50 bg-amber-950/20 p-2">
                <div className="text-[0.68rem] font-semibold uppercase text-amber-500">Needs manual review</div>
                {(mbFixJob.result as MbidStickingRepairResult).unresolved_albums!.slice(0, 8).map((u) => (
                  <div key={u.album_id} className="text-xs text-zinc-400">
                    {u.label} <span className="text-zinc-600">id={u.album_id}</span> — {u.reason}
                  </div>
                ))}
              </div>
            )}
            {mbStatus && (
              <div className="mt-3 space-y-3">
                <CleanMetricGrid
                  items={[
                    { label: 'Albums total', value: mbStatus.total_albums, tone: 'info' },
                    { label: 'Albums missing MB compatibility ID', value: mbStatus.missing_album_mb, tone: mbStatus.missing_album_mb ? 'warning' : 'success' },
                    { label: 'Tracks missing recording ID', value: mbStatus.missing_track_mb, tone: mbStatus.missing_track_mb ? 'warning' : 'success' },
                    { label: 'Representative release gap rows', value: mbStatus.item_release_gap_rows ?? 0, tone: (mbStatus.item_release_gap_rows ?? 0) ? 'warning' : 'success' },
                  ]}
                />
                {(mbStatus.inferred_album_mbid_rows ?? 0) > 0 && (
                  <p className="text-xs text-amber-400">
                    {mbStatus.inferred_album_mbid_rows} album(s) can have the Beets representative release ID inferred from item tags — Fix MB ID Gaps will restore them.
                  </p>
                )}
                {mbStatus.missing_album_mb > 0 && (mbStatus.examples?.length ?? 0) > 0 && (
                  <div className="space-y-1">
                    <div className="text-[0.68rem] font-semibold uppercase text-zinc-500">Albums missing Beets compatibility ID (first {mbStatus.examples.length})</div>
                    {mbStatus.examples.slice(0, 6).map((ex) => (
                      <div key={ex.album_id} className="text-xs text-zinc-500">
                        {ex.artist} — {ex.album} {ex.year ? `(${ex.year})` : ''} <span className="text-zinc-600">id={ex.album_id}</span>
                      </div>
                    ))}
                    {mbStatus.missing_album_mb > 6 && (
                      <div className="text-xs text-zinc-600">…and {mbStatus.missing_album_mb - 6} more</div>
                    )}
                  </div>
                )}
                {(mbStatus.item_release_gap_rows ?? 0) === 0 && mbStatus.missing_album_mb === 0 && (
                  <CleanEmptyState title="All library albums have matching MusicBrainz compatibility IDs" tone="success" />
                )}
              </div>
            )}
            {!mbStatus && !mbLoading && (
              <p className="text-xs text-zinc-600">Click "Check Coverage" to scan the library.</p>
            )}
          </CleanSection>

          {/* ── Template token files ─────────────────────────────────── */}
          <CleanSection
            title="Unresolved Path-Template Tokens"
            description="Audio files whose filenames still contain Beets path-template tokens like $disc_subfolder or %if{…}. These are renamed in place — no files are moved."
            tone={tokenResult && tokenResult.candidates > 0 ? 'warning' : 'neutral'}
            count={tokenResult ? (
              <Chip
                color={tokenResult.candidates > 0 ? 'warning' : 'success'}
                label={tokenResult.candidates}
                size="small"
              />
            ) : null}
            actions={(
              <div className="flex gap-2">
                <Button
                  disabled={tokenBusy || tokenJob?.status === 'running'}
                  size="small"
                  variant="outlined"
                  onClick={() => void handleTokenScan()}
                >
                  {tokenJob?.status === 'running' ? 'Scanning…' : 'Scan'}
                </Button>
                {tokenResult && tokenResult.candidates > 0 && (
                  <Button
                    color="warning"
                    disabled={tokenBusy || tokenJob?.status === 'running'}
                    size="small"
                    variant="contained"
                    onClick={() => void handleTokenFix()}
                  >
                    Fix {tokenResult.candidates} file{tokenResult.candidates !== 1 ? 's' : ''}
                  </Button>
                )}
              </div>
            )}
          >
            {tokenMsg && <p className="text-xs text-zinc-400">{tokenMsg}</p>}
            {tokenJob?.status === 'running' && <LinearProgress sx={{ borderRadius: 1, mt: 1 }} />}
            <JobStatusCard job={tokenJob} runningLabel="Scanning for template-token files…" logLines={2} className="mt-2" />
            {tokenResult && tokenResult.candidates === 0 && !tokenResult.renamed && (
              <CleanEmptyState title="No template-token filenames found" tone="success" />
            )}
            {tokenResult && tokenResult.renamed > 0 && (
              <p className="mt-2 text-xs text-emerald-400">
                Renamed {tokenResult.renamed} file{tokenResult.renamed !== 1 ? 's' : ''} · {tokenResult.db_updates} DB path update{tokenResult.db_updates !== 1 ? 's' : ''}
                {tokenResult.skipped > 0 ? ` · ${tokenResult.skipped} skipped` : ''}
              </p>
            )}
            {tokenResult && tokenResult.items.length > 0 && (
              <div className="mt-3 space-y-1">
                {tokenResult.items.slice(0, 10).map((item) => (
                  <div key={item.path} className="text-xs text-zinc-500">
                    <span className="text-amber-400">{item.filename}</span>
                    <span className="text-zinc-600"> → </span>
                    <span className="text-zinc-300">{item.new_filename}</span>
                  </div>
                ))}
                {tokenResult.items.length > 10 && (
                  <div className="text-xs text-zinc-600">…and {tokenResult.items.length - 10} more</div>
                )}
              </div>
            )}
          </CleanSection>

        </div>
      )}
    </div>
  );
}
