import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  albumAcoustidSubmit,
  albumAddMbids,
  albumMbsubmit,
  getAlbumMbFormat,
  getJob,
  getReviewQueue,
  itemAcoustidSubmit,
  itemMbsubmit,
} from '../api/client';
import type { AlbumMbFormatResponse, JobResponse, JobStartResponse, ReviewItem } from '../api/types';

const REVIEW_LIMIT = 5000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type RunState = {
  status: 'idle' | 'running' | 'success' | 'error';
  message: string;
  output: string;
  jobId?: string;
};

const IDLE_RUN: RunState = { status: 'idle', message: '', output: '' };

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function positiveInt(value: string | number | null | undefined): number {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function itemTitle(item?: ReviewItem | null): string {
  if (!item) return 'Manual submission';
  return item.album || item.title || item.folder_name || item.path || 'Review item';
}

function itemArtist(item?: ReviewItem | null): string {
  return item?.artist || item?.suggestion?.albumartist || '';
}

function itemYear(item?: ReviewItem | null): string {
  return item?.year ? String(item.year) : '';
}

function reviewItemId(item: ReviewItem): string {
  return item.id || (item.path ? `pending:${item.path}` : itemTitle(item));
}

function musicBrainzAddUrl(item: ReviewItem | null, format: AlbumMbFormatResponse | null): string {
  if (format?.mb_url) return format.mb_url;
  const params = new URLSearchParams();
  const artist = itemArtist(item);
  const title = itemTitle(item);
  const year = itemYear(item);
  if (artist) params.set('artist', artist);
  if (title && title !== 'Manual submission') params.set('title', title);
  if (year) params.set('year', year);
  const qs = params.toString();
  return `https://musicbrainz.org/release/add${qs ? `?${qs}` : ''}`;
}

function reviewTrackText(item: ReviewItem | null): string {
  const titles = item?.evidence?.folder?.track_titles ?? [];
  if (titles.length) {
    return titles.map((title, index) => `${index + 1}. ${title}`).join('\n');
  }
  const files = item?.evidence?.folder?.filenames ?? [];
  if (files.length) {
    return files.map((file, index) => `${index + 1}. ${file.replace(/\.[^.]+$/, '')}`).join('\n');
  }
  return '';
}

function runOutput(job: JobResponse): string {
  const resultOutput = typeof job.result?.output === 'string' ? job.result.output : '';
  return resultOutput || (job.log ?? []).join('\n');
}

async function pollJob(started: JobStartResponse): Promise<{ job: JobResponse; output: string }> {
  for (let i = 0; i < 90; i += 1) {
    await wait(2000);
    const job = await getJob(started.job_id);
    if (job.status === 'success' || job.status === 'failed' || job.status === 'cancelled' || job.status === 'killed') {
      return { job, output: runOutput(job) };
    }
  }
  throw new Error('Timed out waiting for the submission job');
}

function statusTone(status: RunState['status']): 'success' | 'info' | 'error' {
  if (status === 'success') return 'success';
  if (status === 'error') return 'error';
  return 'info';
}

function JobLink({ jobId }: { jobId?: string }) {
  if (!jobId) return null;
  return (
    <Button component={Link} to={`/jobs?job=${encodeURIComponent(jobId)}`} size="small" variant="text">
      View Job
    </Button>
  );
}

export default function Submissions() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [selectedId, setSelectedId] = useState(searchParams.get('review_item_id') || '');
  const [albumId, setAlbumId] = useState(searchParams.get('album_id') || '');
  const [itemId, setItemId] = useState(searchParams.get('item_id') || '');
  const [format, setFormat] = useState<AlbumMbFormatResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [formatLoading, setFormatLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [mbRun, setMbRun] = useState<RunState>(IDLE_RUN);
  const [acoustidRun, setAcoustidRun] = useState<RunState>(IDLE_RUN);
  const [applyRun, setApplyRun] = useState<RunState>(IDLE_RUN);
  const [artistId, setArtistId] = useState('');
  const [releaseGroupId, setReleaseGroupId] = useState('');
  const [releaseId, setReleaseId] = useState('');

  const activeAlbumId = positiveInt(albumId);
  const activeItemId = positiveInt(itemId);
  const selectedItem = useMemo(() => {
    if (!selectedId) return null;
    return items.find((item) => reviewItemId(item) === selectedId || item.id === selectedId) ?? null;
  }, [items, selectedId]);

  const trackText = format?.track_text || reviewTrackText(selectedItem);
  const mbUrl = musicBrainzAddUrl(selectedItem, format);
  const hasLibraryTarget = activeAlbumId > 0 || activeItemId > 0;
  const canApplyMbids =
    activeAlbumId > 0 &&
    UUID_RE.test(artistId.trim()) &&
    UUID_RE.test(releaseGroupId.trim()) &&
    (!releaseId.trim() || UUID_RE.test(releaseId.trim()));

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getReviewQueue({ limit: REVIEW_LIMIT });
      setItems(data.items ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const nextSelected = searchParams.get('review_item_id') || '';
    const nextAlbum = searchParams.get('album_id') || '';
    const nextItem = searchParams.get('item_id') || '';
    setSelectedId(nextSelected);
    setAlbumId(nextAlbum);
    setItemId(nextItem);
  }, [searchParams]);

  useEffect(() => {
    if (!selectedItem) return;
    setAlbumId(selectedItem.album_id ? String(selectedItem.album_id) : '');
    setItemId(selectedItem.first_item_id ? String(selectedItem.first_item_id) : '');
  }, [selectedItem]);

  useEffect(() => {
    let cancelled = false;
    if (!activeAlbumId) {
      setFormat(null);
      return undefined;
    }
    setFormatLoading(true);
    getAlbumMbFormat(activeAlbumId)
      .then((data) => {
        if (!cancelled) setFormat(data);
      })
      .catch(() => {
        if (!cancelled) setFormat(null);
      })
      .finally(() => {
        if (!cancelled) setFormatLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeAlbumId]);

  function selectItem(item: ReviewItem) {
    const next = new URLSearchParams(searchParams);
    next.set('review_item_id', reviewItemId(item));
    if (item.album_id) next.set('album_id', String(item.album_id));
    else next.delete('album_id');
    if (item.first_item_id) next.set('item_id', String(item.first_item_id));
    else next.delete('item_id');
    if (item.path) next.set('path', item.path);
    else next.delete('path');
    setSearchParams(next, { replace: false });
  }

  async function copyText(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      setMessage(`${label} copied.`);
    } catch {
      setMessage('Clipboard write failed.');
    }
  }

  async function runMusicBrainzPrepare() {
    if (!hasLibraryTarget) return;
    setMbRun({ status: 'running', message: 'Preparing MusicBrainz submission text...', output: '' });
    try {
      const started = activeAlbumId ? await albumMbsubmit(activeAlbumId) : await itemMbsubmit(activeItemId);
      setMbRun((current) => ({ ...current, jobId: started.job_id }));
      const { job, output } = await pollJob(started);
      setMbRun({
        status: job.status === 'success' ? 'success' : 'error',
        message: job.status === 'success' ? 'MusicBrainz submission text is ready.' : 'MusicBrainz prepare job failed.',
        output,
        jobId: started.job_id,
      });
    } catch (err) {
      setMbRun({ status: 'error', message: err instanceof Error ? err.message : String(err), output: '' });
    }
  }

  async function runAcoustidSubmit() {
    if (!hasLibraryTarget) return;
    setAcoustidRun({ status: 'running', message: 'Submitting fingerprints to AcoustID...', output: '' });
    try {
      const started = activeAlbumId ? await albumAcoustidSubmit(activeAlbumId) : await itemAcoustidSubmit(activeItemId);
      setAcoustidRun((current) => ({ ...current, jobId: started.job_id }));
      const { job, output } = await pollJob(started);
      setAcoustidRun({
        status: job.status === 'success' ? 'success' : 'error',
        message: job.status === 'success' ? 'AcoustID submit job completed.' : 'AcoustID submit job failed.',
        output,
        jobId: started.job_id,
      });
    } catch (err) {
      setAcoustidRun({ status: 'error', message: err instanceof Error ? err.message : String(err), output: '' });
    }
  }

  async function applyMbids() {
    if (!canApplyMbids || !activeAlbumId) return;
    setApplyRun({ status: 'running', message: 'Applying MusicBrainz IDs...', output: '' });
    try {
      const started = await albumAddMbids(activeAlbumId, {
        mb_albumartistid: artistId.trim().toLowerCase(),
        mb_releasegroupid: releaseGroupId.trim().toLowerCase(),
        mb_albumid: releaseId.trim().toLowerCase() || undefined,
      });
      setApplyRun((current) => ({ ...current, jobId: started.job_id }));
      const { job, output } = await pollJob(started);
      setApplyRun({
        status: job.status === 'success' ? 'success' : 'error',
        message: job.status === 'success' ? 'MusicBrainz IDs applied.' : 'Add MBIDs job failed.',
        output,
        jobId: started.job_id,
      });
    } catch (err) {
      setApplyRun({ status: 'error', message: err instanceof Error ? err.message : String(err), output: '' });
    }
  }

  return (
    <section className="space-y-4">
      <div className="rounded-md border border-graphite-800 bg-graphite-950/45 px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-red-400">Submissions</p>
            <h1 className="mt-1 text-2xl font-semibold text-zinc-100">MusicBrainz and AcoustID</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Chip label={selectedItem ? 'Review handoff' : 'Manual target'} size="small" variant="outlined" />
              {activeAlbumId ? <Chip color="info" label={`Album ${activeAlbumId}`} size="small" variant="outlined" /> : null}
              {activeItemId && !activeAlbumId ? <Chip color="info" label={`Item ${activeItemId}`} size="small" variant="outlined" /> : null}
              {items.length ? <Chip label={`${items.length} review items`} size="small" variant="outlined" /> : null}
            </div>
          </div>
          <Button component={Link} to="/import?tab=review" size="small" variant="outlined">
            Back to Review
          </Button>
        </div>
        {loading ? <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} /> : null}
        {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
        {message ? <Alert severity="info" sx={{ mt: 2 }} onClose={() => setMessage('')}>{message}</Alert> : null}
      </div>

      <div className="grid gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <aside className="space-y-3 rounded-md border border-graphite-800 bg-graphite-900/70 p-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-zinc-100">Review Queue</h2>
            <Button size="small" variant="text" onClick={() => void load()}>Refresh</Button>
          </div>
          <div className="max-h-[32rem] space-y-2 overflow-auto pr-1">
            {items.length ? items.map((item) => {
              const id = reviewItemId(item);
              const selected = id === selectedId || item.id === selectedId;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => selectItem(item)}
                  className={[
                    'w-full rounded border px-3 py-2 text-left transition-colors',
                    selected
                      ? 'border-red-500 bg-red-950/35 text-zinc-100'
                      : 'border-graphite-800 bg-graphite-950/40 text-zinc-300 hover:border-graphite-700 hover:bg-graphite-850',
                  ].join(' ')}
                >
                  <div className="truncate text-sm font-semibold">{itemTitle(item)}</div>
                  <div className="mt-1 truncate text-xs text-zinc-500">{itemArtist(item) || item.path || 'unknown artist'}</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    <span className="rounded bg-graphite-800 px-1.5 py-0.5 text-[0.65rem] text-zinc-400">{item.type}</span>
                    {item.album_id ? <span className="rounded bg-sky-950/55 px-1.5 py-0.5 text-[0.65rem] text-sky-300">album {item.album_id}</span> : null}
                  </div>
                </button>
              );
            }) : (
              <div className="rounded border border-graphite-800 bg-graphite-950/35 p-3 text-sm text-zinc-400">
                No review items loaded.
              </div>
            )}
          </div>
        </aside>

        <div className="space-y-4">
          <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <h2 className="truncate text-lg font-semibold text-zinc-100">{itemTitle(selectedItem)}</h2>
                <div className="mt-1 text-sm text-zinc-400">
                  {[itemArtist(selectedItem), itemYear(selectedItem), selectedItem?.tracks ? `${selectedItem.tracks} tracks` : ''].filter(Boolean).join(' / ') || 'No review item selected'}
                </div>
                {selectedItem?.path ? (
                  <div className="mt-2 truncate font-mono text-xs text-zinc-500">{selectedItem.path}</div>
                ) : null}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="small" variant="outlined" href={mbUrl} target="_blank" rel="noreferrer">
                  Open MusicBrainz
                </Button>
                {trackText ? (
                  <Button size="small" variant="outlined" onClick={() => void copyText(trackText, 'Track list')}>
                    Copy Track List
                  </Button>
                ) : null}
              </div>
            </div>

            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <TextField
                label="Album ID"
                value={albumId}
                onChange={(event) => setAlbumId(event.target.value.replace(/[^\d]/g, ''))}
                size="small"
                helperText="Preferred for album-level MusicBrainz and AcoustID submission."
              />
              <TextField
                label="Item ID"
                value={itemId}
                onChange={(event) => setItemId(event.target.value.replace(/[^\d]/g, ''))}
                size="small"
                helperText="Fallback for singleton or item-level submission."
              />
            </div>

            {formatLoading ? <LinearProgress sx={{ mt: 2, borderRadius: 1 }} /> : null}
            {trackText ? (
              <pre className="mt-4 max-h-64 overflow-auto rounded border border-graphite-800 bg-graphite-950/60 p-3 text-xs text-zinc-300 whitespace-pre-wrap">
                {trackText}
              </pre>
            ) : (
              <Alert severity="warning" sx={{ mt: 3 }}>
                No local track list is available yet. Select a review item with evidence or enter an imported album ID.
              </Alert>
            )}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
              <h2 className="text-sm font-semibold text-zinc-100">MusicBrainz</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Prepare Beets submission text for imported library targets, then open the MusicBrainz release editor.
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Button size="small" variant="contained" onClick={() => void runMusicBrainzPrepare()} disabled={!hasLibraryTarget || mbRun.status === 'running'}>
                  Prepare Submission
                </Button>
                <Button size="small" variant="outlined" href={mbUrl} target="_blank" rel="noreferrer">
                  Add Release
                </Button>
                {mbRun.output ? (
                  <Button size="small" variant="outlined" onClick={() => void copyText(mbRun.output, 'MusicBrainz submission')}>
                    Copy Output
                  </Button>
                ) : null}
              </div>
              {!hasLibraryTarget ? (
                <Alert severity="info" sx={{ mt: 3 }}>
                  Import or select a library-backed review item to run Beets mbsubmit. You can still copy the track list and use MusicBrainz manually.
                </Alert>
              ) : null}
              {mbRun.message ? (
                <Alert severity={statusTone(mbRun.status)} sx={{ mt: 3 }}>{mbRun.message}</Alert>
              ) : null}
              <JobLink jobId={mbRun.jobId} />
              {mbRun.output ? (
                <pre className="mt-3 max-h-72 overflow-auto rounded border border-graphite-800 bg-graphite-950/60 p-3 text-xs text-zinc-300 whitespace-pre-wrap">
                  {mbRun.output}
                </pre>
              ) : null}
            </section>

            <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
              <h2 className="text-sm font-semibold text-zinc-100">AcoustID</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Submit Chromaprint fingerprints after the tracks are imported and tagged with MusicBrainz recording IDs.
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Button size="small" variant="contained" onClick={() => void runAcoustidSubmit()} disabled={!hasLibraryTarget || acoustidRun.status === 'running'}>
                  Submit Fingerprints
                </Button>
                {acoustidRun.output ? (
                  <Button size="small" variant="outlined" onClick={() => void copyText(acoustidRun.output, 'AcoustID output')}>
                    Copy Output
                  </Button>
                ) : null}
              </div>
              {!hasLibraryTarget ? (
                <Alert severity="warning" sx={{ mt: 3 }}>
                  AcoustID submission needs an imported Beets album or item so fingerprints can be tied to MusicBrainz IDs.
                </Alert>
              ) : null}
              {acoustidRun.message ? (
                <Alert severity={statusTone(acoustidRun.status)} sx={{ mt: 3 }}>{acoustidRun.message}</Alert>
              ) : null}
              <JobLink jobId={acoustidRun.jobId} />
              {acoustidRun.output ? (
                <pre className="mt-3 max-h-72 overflow-auto rounded border border-graphite-800 bg-graphite-950/60 p-3 text-xs text-zinc-300 whitespace-pre-wrap">
                  {acoustidRun.output}
                </pre>
              ) : null}
            </section>
          </div>

          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
              <div>
                <h2 className="text-sm font-semibold text-zinc-100">Attach Published MusicBrainz IDs</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  After publishing or finding the release, attach the artist and release-group IDs to the imported album.
                </p>
              </div>
              {activeAlbumId ? <Chip label={`Album ${activeAlbumId}`} size="small" variant="outlined" /> : null}
            </div>
            <div className="mt-4 grid gap-3 lg:grid-cols-3">
              <TextField
                label="Album Artist MBID"
                value={artistId}
                onChange={(event) => setArtistId(event.target.value)}
                size="small"
                error={artistId.trim() !== '' && !UUID_RE.test(artistId.trim())}
              />
              <TextField
                label="Release Group MBID"
                value={releaseGroupId}
                onChange={(event) => setReleaseGroupId(event.target.value)}
                size="small"
                error={releaseGroupId.trim() !== '' && !UUID_RE.test(releaseGroupId.trim())}
              />
              <TextField
                label="Release MBID"
                value={releaseId}
                onChange={(event) => setReleaseId(event.target.value)}
                size="small"
                error={releaseId.trim() !== '' && !UUID_RE.test(releaseId.trim())}
                helperText="Optional"
              />
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <Button size="small" variant="contained" onClick={() => void applyMbids()} disabled={!canApplyMbids || applyRun.status === 'running'}>
                Apply MBIDs
              </Button>
              {!activeAlbumId ? <span className="text-xs text-zinc-500">Album ID is required.</span> : null}
            </div>
            {applyRun.message ? (
              <Alert severity={statusTone(applyRun.status)} sx={{ mt: 3 }}>{applyRun.message}</Alert>
            ) : null}
            <JobLink jobId={applyRun.jobId} />
            {applyRun.output ? (
              <pre className="mt-3 max-h-64 overflow-auto rounded border border-graphite-800 bg-graphite-950/60 p-3 text-xs text-zinc-300 whitespace-pre-wrap">
                {applyRun.output}
              </pre>
            ) : null}
          </section>
        </div>
      </div>
    </section>
  );
}
