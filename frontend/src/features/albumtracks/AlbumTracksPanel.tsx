import { Dialog, DialogBackdrop, DialogPanel, DialogTitle, Disclosure, DisclosureButton, DisclosurePanel, Switch } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getJobResult, removeAlbumTracks, removeAlbumTracksBatch, scanAlbumTracks } from '../../api/client';
import type { AlbumTrackProblem, AlbumTrackScanResult } from '../../api/types';
import { CleanActionBar, CleanEmptyState, CleanMetricGrid, CleanPanelHeader } from '../../components/CleanPanel';
import { LogViewer } from '../../components/LogViewer';
import { useJobPoll } from '../../lib/hooks';

function jobsUrl(jobId: string | null) {
  return jobId ? `/jobs?q=${encodeURIComponent(jobId)}` : '/jobs';
}

// ── Problem album card ────────────────────────────────────────────────────────

function ProblemAlbum({ problem }: { problem: AlbumTrackProblem }) {
  const navigate = useNavigate();
  const [removing, setRemoving] = useState(false);
  const [removeJobId, setRemoveJobId] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const { job } = useJobPoll(removeJobId);

  useEffect(() => {
    if (job?.status === 'success') {
      setDone(true);
      setRemoving(false);
    } else if (job?.status === 'failed' || job?.status === 'killed' || job?.status === 'cancelled') {
      setRemoving(false);
    }
  }, [job?.status]);

  const handleRemove = async () => {
    const ids = problem.remove_candidates.map((c) => c.id);
    setRemoving(true);
    try {
      const r = await removeAlbumTracks(problem.album_id, ids, false);
      setRemoveJobId(r.job_id);
    } catch {
      setRemoving(false);
    }
  };

  return (
    <div className="overflow-hidden rounded-md border border-graphite-800/90 bg-graphite-950/60 shadow-sm shadow-black/20">
      <div className="flex items-start justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <div className="font-medium text-zinc-200 text-sm">
            {problem.artist} — {problem.album}
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            {problem.keep_count ?? 0}/{problem.actual_count ?? 0} local track(s) match selected MB release
            {problem.expected_count ? ` · ${problem.expected_count} MB track(s)` : ''}
          </div>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {problem.low_album_match && (
              <Chip color="error" label="Low album match" size="small" />
            )}
            {problem.remove_candidates.length > 0 && (
              <Chip color="error" label={`${problem.remove_candidates.length} to remove`} size="small" variant="outlined" />
            )}
            {problem.review_candidates.length > 0 && (
              <Chip color="warning" label={`${problem.review_candidates.length} to review`} size="small" variant="outlined" />
            )}
          </div>
        </div>
        <div className="flex shrink-0 gap-1.5">
          <Button
            size="small"
            variant="outlined"
            sx={{ fontSize: '0.72rem', py: 0.25 }}
            onClick={() => navigate(`/library?artist=${encodeURIComponent(problem.artist)}`)}
          >
            Open
          </Button>
          {problem.remove_candidates.length > 0 && !done && (
            <Button
              color="error"
              disabled={removing}
              size="small"
              variant="outlined"
              onClick={() => void handleRemove()}
            >
              {removing ? 'Removing…' : 'Remove'}
            </Button>
          )}
          {removeJobId ? (
            <Button
              size="small"
              variant="text"
              sx={{ fontSize: '0.72rem', py: 0.25 }}
              onClick={() => navigate(jobsUrl(removeJobId))}
            >
              Jobs
            </Button>
          ) : null}
          {done && <Chip color="success" label="Removed" size="small" />}
        </div>
      </div>

      {removeJobId && job && (
          <div className="border-t border-graphite-800/80 bg-graphite-950/80 px-3 py-2">
          <LogViewer className="max-h-40 text-[0.7rem] leading-5 text-zinc-400" emptyText="..." lines={job.log ?? []} />
        </div>
      )}

      {/* Track detail */}
      {(problem.remove_candidates.length > 0 || problem.review_candidates.length > 0) && (
        <Disclosure>
          {({ open }) => (
            <div className="border-t border-graphite-800/80">
              <DisclosureButton className="flex w-full items-center justify-between px-4 py-2 text-left text-xs text-zinc-400 transition hover:bg-graphite-900/50">
                <span>Track details</span>
                <span>{open ? '▲' : '▼'}</span>
              </DisclosureButton>
              <DisclosurePanel>
                {[
                  { label: 'Remove', items: problem.remove_candidates, color: 'error' as const },
                  { label: 'Review', items: problem.review_candidates, color: 'warning' as const },
                ].map(({ label, items, color }) =>
                  items.map((item) => (
                    <div
                      key={item.id}
                      className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-2 border-t border-graphite-800/80 px-4 py-2 text-xs first:border-t-0"
                    >
                      <Chip color={color} label={label} size="small" variant="outlined" />
                      <div className="min-w-0">
                        <div className="truncate text-zinc-300">{item.title}</div>
                        <div className="truncate font-mono text-zinc-600">{item.path}</div>
                        {item.reason && <div className="italic text-zinc-600">{item.reason}</div>}
                      </div>
                    </div>
                  )),
                )}
              </DisclosurePanel>
            </div>
          )}
        </Disclosure>
      )}
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export function AlbumTracksPanel() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const extraDiskCount = Math.max(0, parseInt(searchParams.get('extraDisk') ?? '0', 10) || 0);
  const [limit, setLimit] = useState('250');
  const [useAi, setUseAi] = useState(true);
  const [useFingerprint, setUseFingerprint] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [batchJobId, setBatchJobId] = useState<string | null>(null);
  const [batchRemoving, setBatchRemoving] = useState(false);
  const [removeAllConfirm, setRemoveAllConfirm] = useState(false);
  const [result, setResult] = useState<AlbumTrackScanResult | null>(null);
  const [error, setError] = useState('');

  const { job } = useJobPoll(jobId);
  const { job: batchJob } = useJobPoll(batchJobId);

  useEffect(() => {
    if (job?.status !== 'success' || result) return;
    const r = getJobResult<AlbumTrackScanResult>(job);
    if (r) setResult(r);
  }, [job, job?.status, result]);

  useEffect(() => {
    if (!batchRemoving) return;
    if (batchJob?.status === 'success' || batchJob?.status === 'failed' || batchJob?.status === 'killed' || batchJob?.status === 'cancelled') {
      setBatchRemoving(false);
    }
  }, [batchJob?.status, batchRemoving]);

  const startScan = async () => {
    setError('');
    setResult(null);
    try {
      const r = await scanAlbumTracks({
        limit: Math.max(1, Math.min(500, parseInt(limit, 10) || 75)),
        useAi,
        useFingerprint,
      });
      setJobId(r.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const removalGroups = (result?.problem_albums ?? [])
    .map((problem) => ({
      album_id: problem.album_id,
      item_ids: problem.remove_candidates.map((candidate) => candidate.id),
    }))
    .filter((group) => group.item_ids.length > 0);
  const removalTrackCount = removalGroups.reduce((sum, group) => sum + group.item_ids.length, 0);

  const removeAll = async () => {
    if (!removalGroups.length) return;
    setError('');
    setBatchRemoving(true);
    try {
      const r = await removeAlbumTracksBatch(removalGroups, false);
      setBatchJobId(r.job_id);
    } catch (err) {
      setBatchRemoving(false);
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const running = job?.status === 'running';
  const batchRunning = batchJob?.status === 'running' || batchRemoving;
  const activeJobId = batchJobId ?? jobId;

  return (
    <div className="space-y-4">
      <CleanPanelHeader
        title="Album Track Cleanup"
        description="Compares local album rows with the selected MusicBrainz release and separates clear removals from review cases."
        meta={result ? (
          <>
            <span>{result.albums_scanned} album(s) scanned</span>
            <span>{result.problem_count} problem album(s)</span>
          </>
        ) : <span>No scan result loaded</span>}
        actions={activeJobId ? (
          <Button size="small" variant="outlined" onClick={() => navigate(jobsUrl(activeJobId))}>
            Jobs
          </Button>
        ) : null}
      />

      {extraDiskCount > 0 && (
        <Alert
          severity="info"
          action={
            <Button color="inherit" size="small" onClick={() => navigate('/library?lane=extra')}>
              Library Extra lane
            </Button>
          }
        >
          <strong>{extraDiskCount} album(s)</strong> have extra audio files on disk not imported into Beets.
          These don&apos;t appear in the scan below (which checks imported rows against the MB tracklist).
          Use the Library <strong>Extra</strong> lane to review them individually.
        </Alert>
      )}

      {/* Controls */}
      <CleanActionBar>
        <TextField
          label="Albums to scan"
          size="small"
          type="number"
          value={limit}
          sx={{ width: '9rem' }}
          onChange={(e) => setLimit(e.target.value)}
        />
        <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
          <Switch
            checked={useAi}
            onChange={setUseAi}
            className="group inline-flex h-5 w-9 items-center rounded-full bg-graphite-700 transition data-[checked]:bg-red-500"
          >
            <span className="size-3.5 translate-x-0.5 rounded-full bg-white transition group-data-[checked]:translate-x-4" />
          </Switch>
          AI review
        </label>
        <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
          <Switch
            checked={useFingerprint}
            onChange={setUseFingerprint}
            className="group inline-flex h-5 w-9 items-center rounded-full bg-graphite-700 transition data-[checked]:bg-red-500"
          >
            <span className="size-3.5 translate-x-0.5 rounded-full bg-white transition group-data-[checked]:translate-x-4" />
          </Switch>
          Fingerprint
        </label>
        <Button disabled={running} variant="outlined" onClick={() => void startScan()}>
          {running ? 'Scanning…' : 'Scan Albums'}
        </Button>
      </CleanActionBar>

      {running && <LinearProgress sx={{ borderRadius: 1 }} />}
      {batchRunning && <LinearProgress color="error" sx={{ borderRadius: 1 }} />}
      {error && <Alert severity="error">{error}</Alert>}
      {job?.status === 'failed' && (
        <Alert
          action={jobId ? (
            <Button color="inherit" size="small" onClick={() => navigate(jobsUrl(jobId))}>
              Jobs
            </Button>
          ) : null}
          severity="error"
        >
          Scan failed. Check the log in the Jobs panel.
        </Alert>
      )}
      {batchJob?.status === 'failed' && (
        <Alert
          action={batchJobId ? (
            <Button color="inherit" size="small" onClick={() => navigate(jobsUrl(batchJobId))}>
              Jobs
            </Button>
          ) : null}
          severity="error"
        >
          Batch remove failed. Check the log below or in the Jobs panel.
        </Alert>
      )}
      {batchJobId && batchJob && (
        <div className="rounded border border-graphite-800 bg-graphite-950 px-3 py-2">
          <div className="mb-2 flex items-center justify-between gap-2 text-xs text-zinc-500">
            <span>Batch remove job: {batchJob.status}</span>
            <Button size="small" variant="text" onClick={() => navigate(jobsUrl(batchJobId))}>
              Jobs
            </Button>
          </div>
          <LogViewer className="max-h-48 text-[0.7rem] leading-5 text-zinc-400" emptyText="..." lines={batchJob.log ?? []} />
        </div>
      )}

      {!result && !running && (
        <CleanEmptyState
          title="No album-track scan loaded"
          message="Run a scan to find extra local rows, low-confidence album matches, and review cases."
        />
      )}

      {/* Results */}
      {result && (
        <div className="space-y-3">
          <CleanActionBar sticky={removalTrackCount > 0}>
            <div className="min-w-0 flex-1">
              <CleanMetricGrid
                items={[
                  { label: 'Albums scanned', value: result.albums_scanned, tone: 'info' },
                  { label: 'Remove', value: result.remove_count, tone: result.remove_count ? 'danger' : 'success' },
                  { label: 'Review', value: result.review_count, tone: result.review_count ? 'warning' : 'success' },
                  { label: 'Problems', value: result.problem_count, tone: result.problem_count ? 'warning' : 'success' },
                ]}
              />
            </div>
            {removalTrackCount > 0 && (
              <Button color="error" disabled={batchRunning} size="small" variant="contained" onClick={() => setRemoveAllConfirm(true)}>
                {batchRunning ? 'Removing…' : `Remove all clear removals (${removalTrackCount})`}
              </Button>
            )}
          </CleanActionBar>

          {result.problem_count === 0 && (
            <CleanEmptyState title="All scanned albums are clean" tone="success" />
          )}

          {result.problem_albums.length > 0 && (
            <div className="space-y-2">
              {result.problem_albums.map((p) => (
                <ProblemAlbum key={p.album_id} problem={p} />
              ))}
            </div>
          )}
        </div>
      )}

      <Dialog open={removeAllConfirm} onClose={() => setRemoveAllConfirm(false)} className="relative z-50">
        <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <DialogPanel className="w-full max-w-sm rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
            <DialogTitle className="text-base font-semibold text-zinc-100">
              Remove {removalTrackCount} track{removalTrackCount !== 1 ? 's' : ''}?
            </DialogTitle>
            <p className="mt-2 text-sm text-zinc-400">
              Remove all tracks flagged as clear removals from the beets database.
              Audio files are not deleted. This cannot be undone without a database restore.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outlined" size="small" onClick={() => setRemoveAllConfirm(false)}>Cancel</Button>
              <Button
                color="error"
                size="small"
                variant="contained"
                onClick={() => { setRemoveAllConfirm(false); void removeAll(); }}
              >
                Remove All
              </Button>
            </div>
          </DialogPanel>
        </div>
      </Dialog>
    </div>
  );
}
