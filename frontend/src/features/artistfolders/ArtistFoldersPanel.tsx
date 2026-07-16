import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getJobResult, mergeArtistFolders, scanArtistFolders, stampMbidFolders } from '../../api/client';
import type { ArtistFolderGroup, ArtistFolderScanResponse } from '../../api/types';
import { CleanActionBar, CleanEmptyState, CleanMetricGrid, CleanPanelHeader, CleanSection } from '../../components/CleanPanel';
import { JobStatusCard } from '../../components/JobStatusCard';
import { LogViewer } from '../../components/LogViewer';
import { useJobPoll } from '../../lib/hooks';

const DEFAULT_ROOT = '/data/media/music';

function jobsUrl(jobId: string | null) {
  return jobId ? `/jobs?q=${encodeURIComponent(jobId)}` : '/jobs';
}

function GroupRow({
  group,
  selected,
  onToggle,
}: {
  group: ArtistFolderGroup;
  selected: boolean;
  onToggle: () => void;
}) {
  const isMbMatch = group.match_type === 'mb_artist_id';
  const mbid = group.musicbrainz?.id ?? '';
  return (
    <label className="grid cursor-pointer grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3 border-t border-graphite-800/80 px-3 py-2.5 transition first:border-t-0 hover:bg-graphite-900/50">
      <input
        checked={selected}
        className="mt-0.5 accent-red-500"
        type="checkbox"
        onChange={onToggle}
      />
      <div className="min-w-0 space-y-0.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-zinc-200 text-sm">{group.canonical.name}</span>
          {isMbMatch && (
            <Chip color="info" label="MB ID match" size="small" />
          )}
          {group.rename_to_musicbrainz && (
            <Chip color="secondary" label="rename to MB" size="small" />
          )}
        </div>
        <div className="text-[0.72rem] text-zinc-500">
          {group.variants.map((v) => v.name).join(' · ')}
        </div>
        {mbid && (
          <div className="text-[0.65rem] text-zinc-600 font-mono">{mbid}</div>
        )}
        <div className="text-[0.68rem] text-zinc-600">
          {group.source_audio_files} file(s) to move ·{' '}
          {group.sources.map((s) => `${s.name} (${s.db_tracks} tracks)`).join(', ')}
        </div>
      </div>
      <Chip
        label={`${group.variants.reduce((s, v) => s + v.db_albums, 0)} albums`}
        size="small"
        variant="outlined"
      />
    </label>
  );
}

export function ArtistFoldersPanel() {
  const navigate = useNavigate();
  const [root, setRoot] = useState(DEFAULT_ROOT);
  const [scanStarting, setScanStarting] = useState(false);
  const [scanJobId, setScanJobId] = useState<string | null>(null);
  const [scanResult, setScanResult] = useState<ArtistFolderScanResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [mergeJobId, setMergeJobId] = useState<string | null>(null);
  const [dryRunLog, setDryRunLog] = useState<string[]>([]);
  const [error, setError] = useState('');

  // Stamp MB IDs section
  const [stampBusy, setStampBusy] = useState(false);
  const [stampLog, setStampLog] = useState<string[]>([]);
  const [stampJobId, setStampJobId] = useState<string | null>(null);
  const [stampMsg, setStampMsg] = useState('');

  const { job: scanJob } = useJobPoll(scanJobId);
  const { job: mergeJob } = useJobPoll(mergeJobId);
  const { job: stampJob } = useJobPoll(stampJobId);
  const groups = scanResult?.groups ?? [];
  const scanPending = Boolean(scanJobId && !scanJob && !scanResult);
  const scanRunning = scanJob?.status === 'running';
  const scanBusy = scanStarting || scanPending || scanRunning;
  const activeJobId =
    scanRunning ? scanJobId
      : mergeJob?.status === 'running' ? mergeJobId
        : stampJob?.status === 'running' ? stampJobId
          : scanJobId ?? mergeJobId ?? stampJobId;

  useEffect(() => {
    if (scanJob?.status !== 'success') return;
    const result = getJobResult<ArtistFolderScanResponse>(scanJob);
    if (!result) return;
    setScanResult(result);
    setSelected(new Set());
  }, [scanJob]);

  useEffect(() => {
    if (!scanJob || scanJob.status === 'running' || scanJob.status === 'success') return;
    setError('Artist-folder scan failed. Open Jobs for the full log.');
  }, [scanJob]);

  // Auto-rescan after a successful merge or stamp so the list stays current
  useEffect(() => {
    if (mergeJob?.status === 'success' && scanResult) void scan();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mergeJob?.status]);

  useEffect(() => {
    if (stampJob?.status === 'success' && scanResult) void scan();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stampJob?.status]);

  const scan = async () => {
    setScanStarting(true);
    setError('');
    setScanJobId(null);
    setScanResult(null);
    setSelected(new Set());
    setDryRunLog([]);
    try {
      const r = await scanArtistFolders(root.trim() || DEFAULT_ROOT);
      if (!r.job_id) throw new Error('Artist-folder scan did not return a job id');
      setScanJobId(r.job_id);
      window.dispatchEvent(new Event('beets:jobs-changed'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setScanStarting(false);
    }
  };

  const toggleAll = () => {
    setSelected(
      selected.size === groups.length
        ? new Set()
        : new Set(groups.map((g) => g.key)),
    );
  };

  const runMerge = async (dryRun: boolean) => {
    setError('');
    setDryRunLog([]);
    try {
      const r = await mergeArtistFolders(
        Array.from(selected),
        root.trim() || DEFAULT_ROOT,
        dryRun,
      );
      if (dryRun && r.log) {
        setDryRunLog(r.log);
      } else if (!dryRun && r.job_id) {
        setMergeJobId(r.job_id);
        window.dispatchEvent(new Event('beets:jobs-changed'));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const runStamp = async (dryRun: boolean) => {
    setStampBusy(true);
    setStampJobId(null);
    setStampLog([]);
    setStampMsg('');
    try {
      const r = await stampMbidFolders({ root: root.trim() || DEFAULT_ROOT, dryRun });
      if (r.log) setStampLog(r.log);
      if (dryRun) {
        const candidates = r.candidates ?? r.renamed ?? 0;
        const skipped = r.skipped_total ?? r.skipped ?? 0;
        setStampMsg(`Dry run: ${candidates} folder${candidates === 1 ? '' : 's'} would be renamed; ${skipped} skipped.`);
      } else if (r.job_id) {
        setStampJobId(r.job_id);
        window.dispatchEvent(new Event('beets:jobs-changed'));
      } else {
        setStampMsg('Stamp job did not return a job id.');
      }
    } catch (err) {
      setStampMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setStampBusy(false);
    }
  };

  const merging = mergeJob?.status === 'running';

  const nameGroups = groups.filter((g) => g.match_type !== 'mb_artist_id');
  const mbidGroups = groups.filter((g) => g.match_type === 'mb_artist_id');

  return (
    <div className="space-y-5">
      <CleanPanelHeader
        title="Artist Folders"
        description="Finds duplicate artist folders, MusicBrainz-ID folder variants, and folder names that can be safely normalized."
        meta={scanResult ? (
          <>
            <span>{groups.length} group(s)</span>
            <span>{nameGroups.length} name variant(s)</span>
            <span>{mbidGroups.length} MB ID group(s)</span>
          </>
        ) : scanBusy ? <span>Scan running in Jobs</span> : <span>No folder scan loaded</span>}
        actions={activeJobId ? (
          <Button size="small" variant="outlined" onClick={() => navigate(jobsUrl(activeJobId))}>
            Jobs
          </Button>
        ) : null}
      />

      <CleanActionBar>
        <TextField
          fullWidth
          label="Library root"
          size="small"
          value={root}
          onChange={(e) => setRoot(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void scan()}
          slotProps={{ input: { style: { fontFamily: 'monospace', fontSize: '0.82rem' } } }}
        />
        <Button
          disabled={scanBusy}
          variant="outlined"
          sx={{ whiteSpace: 'nowrap', minWidth: '5rem' }}
          onClick={() => void scan()}
        >
          {scanBusy ? 'Scanning…' : 'Scan'}
        </Button>
      </CleanActionBar>

      {scanBusy && <LinearProgress sx={{ borderRadius: 1 }} />}
      {error && <Alert severity="error">{error}</Alert>}
      <JobStatusCard job={scanJob} runningLabel="Scanning artist folders…" logLines={4} />

      {!scanResult && !scanBusy && (
        <CleanEmptyState title="No artist-folder scan loaded" message="Scan a library root to review duplicate artist folders and MBID-stamped variants." />
      )}

      {scanResult && (
        <div className="space-y-4">
          {/* ── Summary ─────────────────────────────── */}
          <CleanMetricGrid
            items={[
              { label: 'Groups', value: groups.length, tone: groups.length ? 'warning' : 'success' },
              { label: 'Name variants', value: nameGroups.length, tone: nameGroups.length ? 'warning' : 'success' },
              { label: 'MB ID groups', value: mbidGroups.length, tone: mbidGroups.length ? 'info' : 'success' },
              { label: 'Selected', value: selected.size, tone: selected.size ? 'warning' : 'neutral' },
            ]}
          />
          {groups.length === 0 && <CleanEmptyState title="No duplicate artist folders found" tone="success" />}

          {/* ── Merge section ────────────────────────── */}
          {groups.length > 0 && (
            <>
              <CleanActionBar sticky>
                <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-400">
                  <input
                    checked={selected.size === groups.length && groups.length > 0}
                    className="accent-red-500"
                    type="checkbox"
                    onChange={toggleAll}
                  />
                  Select all
                </label>
                <div className="flex-1" />
                {selected.size > 0 && (
                  <div className="flex gap-2">
                    <Button size="small" variant="outlined" onClick={() => void runMerge(true)}>
                      Dry run
                    </Button>
                    <Button
                      color="warning"
                      disabled={merging}
                      size="small"
                      variant="contained"
                      onClick={() => void runMerge(false)}
                    >
                      {merging ? 'Merging…' : `Merge (${selected.size})`}
                    </Button>
                  </div>
                )}
              </CleanActionBar>

              <div className="rounded border border-graphite-800 bg-graphite-950">
                {nameGroups.length > 0 && (
                  <>
                    <div className="px-3 py-1.5 text-[0.68rem] font-semibold uppercase text-zinc-500 border-b border-graphite-800">
                      Name variants
                    </div>
                    {nameGroups.map((g) => (
                      <GroupRow
                        key={g.key}
                        group={g}
                        selected={selected.has(g.key)}
                        onToggle={() =>
                          setSelected((prev) => {
                            const next = new Set(prev);
                            next.has(g.key) ? next.delete(g.key) : next.add(g.key);
                            return next;
                          })
                        }
                      />
                    ))}
                  </>
                )}
                {mbidGroups.length > 0 && (
                  <>
                    <div className="px-3 py-1.5 text-[0.68rem] font-semibold uppercase text-red-600 border-b border-graphite-800 border-t border-graphite-800">
                      Same MB artist ID — different folder names
                    </div>
                    {mbidGroups.map((g) => (
                      <GroupRow
                        key={g.key}
                        group={g}
                        selected={selected.has(g.key)}
                        onToggle={() =>
                          setSelected((prev) => {
                            const next = new Set(prev);
                            next.has(g.key) ? next.delete(g.key) : next.add(g.key);
                            return next;
                          })
                        }
                      />
                    ))}
                  </>
                )}
              </div>
            </>
          )}

          {dryRunLog.length > 0 && (
            <div className="rounded border border-graphite-700 bg-graphite-950 p-3">
              <div className="mb-1 text-[0.72rem] font-semibold uppercase text-zinc-500">Dry run output</div>
              <LogViewer className="max-h-52 text-[0.71rem] leading-5" lines={dryRunLog} />
            </div>
          )}

          <JobStatusCard job={mergeJob} runningLabel="Merging artist folders…" logLines={3} />
        </div>
      )}

      {/* ── Stamp MB IDs section ──────────────────────────────────── */}
      <CleanSection
        title="Stamp MusicBrainz IDs In Folder Names"
        description={(
          <>
            Uses the MB artist UUID as the artist identity key, then renames or merges folders into the canonical MusicBrainz artist name.
            Example: <span className="font-mono text-zinc-300">Celia Cruz</span> →{' '}
            <span className="font-mono text-zinc-300">Celia Cruz (7b8e1188-9ca4-4aa5-8393-172de6fa04de)</span>.
            Same-ID folders merge, and wrong names like <span className="font-mono text-zinc-300">BOBBYVtv (...)</span> are corrected when MusicBrainz identifies the canonical artist.
            Only acts on folders where &gt;75% of albums share the same MB artist ID.
          </>
        )}
      >
        <div className="flex gap-2">
          <Button
            disabled={stampBusy || stampJob?.status === 'running'}
            size="small"
            variant="outlined"
            onClick={() => void runStamp(true)}
          >
            Dry Run
          </Button>
          <Button
            color="warning"
            disabled={stampBusy || stampJob?.status === 'running'}
            size="small"
            variant="contained"
            onClick={() => {
              if (window.confirm('Rename artist folders to include MB artist IDs? Beets DB paths will be updated. This cannot be easily undone.')) {
                void runStamp(false);
              }
            }}
          >
            {stampJob?.status === 'running' ? 'Renaming…' : 'Stamp IDs'}
          </Button>
        </div>
        {stampMsg && <p className="text-xs text-zinc-400">{stampMsg}</p>}
        {stampLog.length > 0 && (
          <div className="rounded border border-graphite-700 bg-graphite-950 p-3">
            <LogViewer className="max-h-52 text-[0.71rem] leading-5" lines={stampLog} />
          </div>
        )}
        <JobStatusCard job={stampJob} runningLabel="Stamping MB IDs on folders…" logLines={3} />
      </CleanSection>
    </div>
  );
}
