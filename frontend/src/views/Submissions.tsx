import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import Menu from '@mui/material/Menu';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  addSubmissionReferenceUrl,
  albumAcoustidSubmit,
  albumMbsubmit,
  attachSubmissionMbids,
  getJob,
  getReviewQueue,
  getSubmissionTarget,
  itemAcoustidSubmit,
  itemMbsubmit,
  resetSubmissionDraft,
  saveSubmissionDraft,
  validateSubmissionMusicBrainzRelease,
} from '../api/client';
import type {
  JobResponse,
  JobStartResponse,
  ReferenceUrlEntry,
  ReviewItem,
  SubmissionMusicBrainzMapping,
  SubmissionMusicBrainzValidationResponse,
  SubmissionTargetResponse,
  SubmissionTrack,
} from '../api/types';

const REVIEW_LIMIT = 5000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FILTERS = ['All', 'Needs metadata', 'Ready for MusicBrainz', 'Waiting for MBIDs', 'Ready for AcoustID', 'Complete', 'Failed'];
const APPLY_MBIDS_LABEL = 'Apply MBIDs';
const SUBMIT_FINGERPRINTS_LABEL = 'Submit Fingerprints';

const STEPS = ['Identify', 'Review', 'MusicBrainz', 'Attach IDs', 'AcoustID', 'Complete'];

const RESOLVED_STATE_LABEL: Record<string, string> = {
  imported_album: 'Imported Beets album',
  imported_singleton: 'Imported Beets singleton',
  imported_singletons: 'Imported Beets singletons (no shared album)',
  unimported_album: 'Unimported album folder',
  loose_tracks: 'Loose-track folder',
  empty: 'No audio files found',
  inaccessible: 'Folder not found or unreadable',
};

const PRIMARY_METADATA_FIELDS: Array<[string, string]> = [
  ['title', 'Release title'],
  ['albumartist', 'Release artist credit'],
  ['release_type', 'Primary release type'],
  ['release_date', 'Release date'],
  ['country', 'Country'],
  ['format', 'Media format'],
];
const ADDITIONAL_METADATA_FIELDS: Array<[string, string]> = [
  ['secondary_type', 'Secondary type'],
  ['release_status', 'Status'],
  ['label', 'Label'],
  ['catalog_number', 'Catalog number'],
  ['barcode', 'Barcode'],
  ['annotation', 'Annotation or note'],
];
const OPTIONAL_SUMMARY_KEYS = new Set(['release_status', 'country', 'label', 'catalog_number', 'barcode']);
const REVIEW_SUMMARY_KEYS = new Set(['release_type', 'format']);

type RunState = { status: 'idle' | 'running' | 'success' | 'error'; message: string; output: string; jobId?: string };
const IDLE_RUN: RunState = { status: 'idle', message: '', output: '' };
type SaveState = 'idle' | 'saving' | 'saved' | 'error';

type DraftState = {
  metadata: Record<string, string>;
  trackEdits: Record<string, Partial<SubmissionTrack>>;
  stage?: string;
  mbsubmit_output?: string;
  published?: { input?: string; artistId?: string; releaseGroupId?: string; releaseId?: string };
  reference_urls?: ReferenceUrlEntry[];
};

function wait(ms: number) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }
function positiveInt(value: string | number | null | undefined): number {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}
function reviewItemId(item: ReviewItem): string { return item.id || (item.path ? `pending:${item.path}` : item.album || item.title || 'review-item'); }
function itemTitle(item?: ReviewItem | null): string { return item?.album || item?.title || item?.folder_name || item?.path || 'Review item'; }
function itemArtist(item?: ReviewItem | null): string { return item?.artist || item?.suggestion?.albumartist || ''; }
function itemSourcePath(item?: ReviewItem | null): string { return item?.source_folder || item?.path || item?.folder || ''; }
function statusTone(status: RunState['status']): 'success' | 'info' | 'error' { return status === 'success' ? 'success' : status === 'error' ? 'error' : 'info'; }
function runOutput(job: JobResponse): string { return (typeof job.result?.output === 'string' ? job.result.output : '') || (job.log ?? []).join('\n'); }

async function pollJob(started: JobStartResponse): Promise<{ job: JobResponse; output: string }> {
  for (let i = 0; i < 120; i += 1) {
    await wait(2000);
    const job = await getJob(started.job_id);
    if (job.status === 'success' || job.status === 'failed' || job.status === 'cancelled' || job.status === 'killed') {
      return { job, output: runOutput(job) };
    }
  }
  throw new Error('Timed out waiting for the submission job');
}

function musicBrainzAddUrl(target: SubmissionTargetResponse | null, selected: ReviewItem | null): string {
  const params = new URLSearchParams();
  const summary = target?.summary;
  const artist = summary?.albumartist || itemArtist(selected);
  const title = summary?.title || itemTitle(selected);
  const date = summary?.release_date || selected?.year || '';
  if (artist) params.set('artist', String(artist));
  if (title && title !== 'Review item') params.set('title', String(title));
  if (date) params.set('date', String(date));
  const qs = params.toString();
  return `https://musicbrainz.org/release/add${qs ? `?${qs}` : ''}`;
}

function trackParserText(tracks: SubmissionTrack[], edits: DraftState['trackEdits']): string {
  return tracks.map((track) => {
    const edit = edits[String(track.item_id)] || {};
    const title = String(edit.title ?? track.title ?? '(unknown)');
    const artist = String(edit.artist ?? track.artist ?? '');
    const no = Number(edit.track ?? track.track ?? track.index);
    const duration = track.duration_display ? ` (${track.duration_display})` : '';
    return `${no}. ${title}${artist ? ` - ${artist}` : ''}${duration}`;
  }).join('\n');
}

function queueStatus(item: ReviewItem, selected: boolean, target: SubmissionTargetResponse | null): string {
  if (selected && target?.summary.workflow_stage) return target.summary.workflow_stage;
  if (item.status_key === 'failed' || item.status === 'failed') return 'Failed';
  if (item.mb_albumid || item.mb_releasegroupid) return 'Ready for AcoustID';
  if (item.album_id || item.first_item_id) return 'Ready for MusicBrainz';
  return 'Needs metadata';
}

function stepIndex(stage?: string): number {
  const text = (stage || '').toLowerCase();
  if (text.includes('complete')) return 5;
  if (text.includes('acoustid')) return 4;
  if (text.includes('waiting') || text.includes('ids')) return 3;
  if (text.includes('prepared')) return 3;
  if (text.includes('ready for musicbrainz')) return 2;
  return 0;
}

function fieldSourceLabel(source: string): string {
  const labels: Record<string, string> = {
    youtube_metadata: 'YouTube metadata', youtube_title: 'YouTube title', youtube_channel: 'YouTube channel',
    youtube_playlist: 'YouTube playlist', youtube_upload_date: 'YouTube upload date', web_page_title: 'Web page title',
  };
  return labels[source] || source;
}

function referenceMetaKey(field: string): string {
  if (field === 'artist') return 'albumartist';
  if (field === 'year') return 'release_date';
  return field;
}

function JobLink({ jobId }: { jobId?: string }) {
  if (!jobId) return null;
  return <Button component={Link} to={`/jobs?job=${encodeURIComponent(jobId)}`} size="small" variant="text">View Job</Button>;
}

export default function Submissions() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [filter, setFilter] = useState('All');
  const [selectedId, setSelectedId] = useState(searchParams.get('review_item_id') || '');
  const [albumId, setAlbumId] = useState(searchParams.get('album_id') || '');
  const [itemId, setItemId] = useState(searchParams.get('item_id') || '');
  const [target, setTarget] = useState<SubmissionTargetResponse | null>(null);
  const [targetError, setTargetError] = useState('');
  const [draft, setDraft] = useState<DraftState>({ metadata: {}, trackEdits: {} });
  const [dirty, setDirty] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [loading, setLoading] = useState(true);
  const [targetLoading, setTargetLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [mbRun, setMbRun] = useState<RunState>(IDLE_RUN);
  const [acoustidRun, setAcoustidRun] = useState<RunState>(IDLE_RUN);
  const [applyRun, setApplyRun] = useState<RunState>(IDLE_RUN);
  const [mbInput, setMbInput] = useState('');
  const [validation, setValidation] = useState<SubmissionMusicBrainzValidationResponse | null>(null);
  const [refUrlInput, setRefUrlInput] = useState('');
  const [refUrlBusy, setRefUrlBusy] = useState(false);
  const [refUrlError, setRefUrlError] = useState('');
  const [moreMenuAnchor, setMoreMenuAnchor] = useState<HTMLElement | null>(null);
  const saveTimer = useRef<number | null>(null);

  const activeAlbumId = positiveInt(albumId);
  const activeItemId = positiveInt(itemId);
  const selectedItem = useMemo(() => items.find((item) => reviewItemId(item) === selectedId || item.id === selectedId) ?? null, [items, selectedId]);
  const sourcePath = itemSourcePath(selectedItem);
  const visibleItems = useMemo(() => items.filter((item) => filter === 'All' || queueStatus(item, reviewItemId(item) === selectedId, target) === filter), [items, filter, selectedId, target]);
  const stage = target?.summary.workflow_stage || draft.stage || 'Needs metadata';
  const activeStep = stepIndex(stage);
  const tracks = target?.tracks ?? [];
  const resolvedState = target?.summary.resolved_state || '';
  const isBeetsTarget = target?.target_type === 'album' || target?.target_type === 'item';
  const isBlockedState = resolvedState === 'empty' || resolvedState === 'inaccessible';
  const parserText = trackParserText(tracks, draft.trackEdits || {});
  const mbUrl = musicBrainzAddUrl(target, selectedItem);
  const selectedCandidateCount = selectedItem?.evidence?.top_candidates?.length ?? 0;
  const readyFingerprints = tracks.filter((track) => track.mb_trackid && track.file_available).length;
  const release = validation?.release;
  const canApply = Boolean(activeAlbumId && release?.mb_albumartistid && release?.mb_releasegroupid && release?.mb_albumid && (!validation?.needs_confirmation || window.confirm));
  const references = draft.reference_urls || [];

  const loadQueue = useCallback(async () => {
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

  useEffect(() => { void loadQueue(); }, [loadQueue]);

  useEffect(() => {
    setSelectedId(searchParams.get('review_item_id') || '');
    setAlbumId(searchParams.get('album_id') || '');
    setItemId(searchParams.get('item_id') || '');
  }, [searchParams]);

  useEffect(() => {
    if (!selectedItem) return;
    setAlbumId(selectedItem.album_id ? String(selectedItem.album_id) : '');
    setItemId(selectedItem.first_item_id ? String(selectedItem.first_item_id) : '');
  }, [selectedItem]);

  useEffect(() => {
    let cancelled = false;
    if (!activeAlbumId && !activeItemId && !sourcePath) {
      setTarget(null);
      setTargetError('');
      return undefined;
    }
    setTargetLoading(true);
    setTargetError('');
    getSubmissionTarget({
      albumId: activeAlbumId || undefined,
      itemId: activeItemId || undefined,
      path: (!activeAlbumId && !activeItemId) ? sourcePath : undefined,
    })
      .then((data) => {
        if (cancelled) return;
        setTarget(data);
        const remoteDraft = (data.draft || {}) as Partial<DraftState>;
        setDraft({ metadata: remoteDraft.metadata || {}, trackEdits: remoteDraft.trackEdits || {}, stage: remoteDraft.stage, mbsubmit_output: remoteDraft.mbsubmit_output, published: remoteDraft.published, reference_urls: remoteDraft.reference_urls || [] });
        setMbInput(remoteDraft.published?.input || '');
        setDirty(false);
        setSaveState('idle');
      })
      .catch((err) => { if (!cancelled) { setTarget(null); setTargetError(err instanceof Error ? err.message : String(err)); } })
      .finally(() => { if (!cancelled) setTargetLoading(false); });
    return () => { cancelled = true; };
  }, [activeAlbumId, activeItemId, sourcePath]);

  useEffect(() => {
    if (!dirty || !target) return undefined;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    setSaveState('saving');
    saveTimer.current = window.setTimeout(() => {
      void saveSubmissionDraft({ target_type: target.target_type, target_id: target.target_id, draft })
        .then(() => { setDirty(false); setSaveState('saved'); })
        .catch((err) => { setSaveState('error'); setMessage(err instanceof Error ? err.message : String(err)); });
    }, 800);
    return () => { if (saveTimer.current) window.clearTimeout(saveTimer.current); };
  }, [dirty, draft, target]);

  function updateDraft(next: DraftState) { setDraft(next); setDirty(true); }
  function updateMeta(key: string, value: string) { updateDraft({ ...draft, metadata: { ...draft.metadata, [key]: value } }); }
  function updateTrack(itemIdValue: number, key: keyof SubmissionTrack, value: string | number) { updateDraft({ ...draft, trackEdits: { ...draft.trackEdits, [String(itemIdValue)]: { ...(draft.trackEdits[String(itemIdValue)] || {}), [key]: value } } }); }

  function selectItem(item: ReviewItem) {
    if (dirty && !window.confirm('Save is still pending. Switch items anyway?')) return;
    const next = new URLSearchParams(searchParams);
    next.set('review_item_id', reviewItemId(item));
    if (item.album_id) next.set('album_id', String(item.album_id)); else next.delete('album_id');
    if (item.first_item_id) next.set('item_id', String(item.first_item_id)); else next.delete('item_id');
    setValidation(null);
    setRefUrlError('');
    setSearchParams(next, { replace: false });
  }

  async function copyText(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setMessage(`${label} copied.`); }
    catch { setMessage('Clipboard write failed.'); }
  }

  async function addReferenceUrl() {
    if (!target || !refUrlInput.trim()) return;
    setRefUrlBusy(true);
    setRefUrlError('');
    try {
      const result = await addSubmissionReferenceUrl({
        albumId: activeAlbumId || undefined,
        itemId: activeItemId || undefined,
        path: (!activeAlbumId && !activeItemId) ? sourcePath : undefined,
        url: refUrlInput.trim(),
      });
      const remoteDraft = (result.draft || {}) as Partial<DraftState>;
      setDraft((current) => ({ ...current, reference_urls: remoteDraft.reference_urls || current.reference_urls || [] }));
      setDirty(false);
      setSaveState('saved');
      if (result.reference.status === 'error') setRefUrlError(result.reference.error || 'Could not parse that URL.');
      else setRefUrlInput('');
    } catch (err) {
      setRefUrlError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefUrlBusy(false);
    }
  }

  function removeReferenceUrl(id: string) {
    if (!target) return;
    const next = { ...draft, reference_urls: references.filter((ref) => ref.id !== id) };
    updateDraft(next);
  }

  function reprocessReferenceUrl(url: string) {
    setRefUrlInput(url);
    void addReferenceUrl();
  }

  function applyReferenceField(field: string, value: string) {
    updateMeta(referenceMetaKey(field), value);
    setMessage(`Applied ${field} from reference URL.`);
  }

  async function runMusicBrainzPrepare() {
    if (!target) return;
    setMbRun({ status: 'running', message: 'Preparing MusicBrainz track-parser text...', output: '' });
    try {
      const started = activeAlbumId ? await albumMbsubmit(activeAlbumId) : await itemMbsubmit(activeItemId);
      setMbRun((current) => ({ ...current, jobId: started.job_id }));
      const { job, output } = await pollJob(started);
      const ok = job.status === 'success';
      setMbRun({ status: ok ? 'success' : 'error', message: ok ? 'MusicBrainz preparation is ready. Complete the release on MusicBrainz, then paste the published URL here.' : 'MusicBrainz preparation failed.', output, jobId: started.job_id });
      if (ok) {
        const next = { ...draft, stage: 'Waiting for published MBIDs', mbsubmit_output: output };
        setDraft(next);
        await saveSubmissionDraft({ target_type: target.target_type, target_id: target.target_id, draft: next });
        setDirty(false);
        setSaveState('saved');
      }
    } catch (err) {
      setMbRun({ status: 'error', message: err instanceof Error ? err.message : String(err), output: '' });
    }
  }

  async function validatePublishedIds() {
    if (!target || !mbInput.trim()) return;
    try {
      const data = await validateSubmissionMusicBrainzRelease({ input: mbInput.trim(), album_id: activeAlbumId || undefined, item_id: activeItemId || undefined });
      setValidation(data);
      if (data.release) {
        const next = { ...draft, stage: 'IDs validated', published: { input: mbInput.trim(), artistId: data.release.mb_albumartistid || '', releaseGroupId: data.release.mb_releasegroupid || '', releaseId: data.release.mb_albumid || '' } };
        setDraft(next);
        await saveSubmissionDraft({ target_type: target.target_type, target_id: target.target_id, draft: next });
        setDirty(false);
        setSaveState('saved');
      }
    } catch (err) {
      setValidation({ ok: false, error: err instanceof Error ? err.message : String(err) });
    }
  }

  async function applyPublishedIds() {
    if (!target || !activeAlbumId || !release?.mb_albumartistid || !release.mb_releasegroupid || !release.mb_albumid) return;
    if (validation?.needs_confirmation && !window.confirm('MusicBrainz mismatches were found. Attach these IDs anyway?')) return;
    const recordings = (validation?.mapping || [])
      .filter((row): row is SubmissionMusicBrainzMapping & { item_id: number; recording_mbid: string } => Boolean(row.item_id && row.recording_mbid && UUID_RE.test(row.recording_mbid)))
      .map((row) => ({ item_id: row.item_id, mb_trackid: row.recording_mbid }));
    setApplyRun({ status: 'running', message: 'Applying MusicBrainz IDs without moving files...', output: '' });
    try {
      const started = await attachSubmissionMbids(activeAlbumId, { mb_albumartistid: release.mb_albumartistid, mb_releasegroupid: release.mb_releasegroupid, mb_albumid: release.mb_albumid, recordings });
      setApplyRun((current) => ({ ...current, jobId: started.job_id }));
      const { job, output } = await pollJob(started);
      const ok = job.status === 'success';
      setApplyRun({ status: ok ? 'success' : 'error', message: ok ? 'MusicBrainz IDs attached and verified.' : 'MusicBrainz ID attach failed.', output, jobId: started.job_id });
      if (ok) {
        const next = { ...draft, stage: 'Ready for AcoustID' };
        setDraft(next);
        await saveSubmissionDraft({ target_type: target.target_type, target_id: target.target_id, draft: next });
        setDirty(false);
        setSaveState('saved');
        void getSubmissionTarget({ albumId: activeAlbumId }).then(setTarget);
      }
    } catch (err) {
      setApplyRun({ status: 'error', message: err instanceof Error ? err.message : String(err), output: '' });
    }
  }

  async function runAcoustidSubmit() {
    if (!target) return;
    setAcoustidRun({ status: 'running', message: `Submitting ${readyFingerprints} fingerprints to AcoustID...`, output: '' });
    try {
      const started = activeAlbumId ? await albumAcoustidSubmit(activeAlbumId) : await itemAcoustidSubmit(activeItemId);
      setAcoustidRun((current) => ({ ...current, jobId: started.job_id }));
      const { job, output } = await pollJob(started);
      const ok = job.status === 'success';
      setAcoustidRun({ status: ok ? 'success' : 'error', message: ok ? 'AcoustID submit job completed. Review the output for per-track provider results.' : 'AcoustID submit job failed.', output, jobId: started.job_id });
      if (ok) {
        const next = { ...draft, stage: 'Complete' };
        setDraft(next);
        await saveSubmissionDraft({ target_type: target.target_type, target_id: target.target_id, draft: next });
        setDirty(false);
        setSaveState('saved');
      }
    } catch (err) {
      setAcoustidRun({ status: 'error', message: err instanceof Error ? err.message : String(err), output: '' });
    }
  }

  async function resetDraft() {
    if (!target || !window.confirm('Reset the saved submission draft for this target?')) return;
    await resetSubmissionDraft(target.target_type, target.target_id);
    setDraft({ metadata: {}, trackEdits: {} });
    setValidation(null);
    setMbInput('');
    setDirty(false);
    setSaveState('idle');
    setMessage('Draft reset.');
  }

  const primary = (() => {
    if (!selectedItem) return { label: 'Select a review item', disabled: true, action: () => undefined };
    if (targetLoading) return { label: 'Resolving local files…', disabled: true, action: () => undefined };
    if (!target || isBlockedState) return { label: 'Rescan folder', disabled: true, action: () => undefined };
    if (activeStep <= 1) return { label: 'Prepare MusicBrainz submission', disabled: !target.preflight.musicbrainz_ready || mbRun.status === 'running', action: runMusicBrainzPrepare };
    if (activeStep === 2) return { label: 'Open prepared submission in MusicBrainz', disabled: false, action: () => window.open(mbUrl, '_blank', 'noopener,noreferrer') };
    if (!validation?.release) return { label: 'Validate published IDs', disabled: !mbInput.trim(), action: validatePublishedIds };
    if (activeStep <= 3) return { label: APPLY_MBIDS_LABEL, disabled: !canApply || applyRun.status === 'running', action: applyPublishedIds };
    return { label: `Submit ${readyFingerprints} fingerprints`, disabled: !target.preflight.acoustid_ready || acoustidRun.status === 'running' || readyFingerprints === 0, action: runAcoustidSubmit };
  })();

  const footerBlockerCount = target ? target.preflight.checks.filter((c) => c.blocking && c.status === 'fail').length : 0;
  const footerText = (() => {
    if (!selectedItem) return 'Select a review item to start.';
    if (targetLoading) return 'Resolving local tracks for this review item…';
    if (targetError) return `Could not resolve this item: ${targetError}`;
    if (!target) return 'Could not resolve this item — see Preflight Checklist.';
    return `${target.summary.albumartist || 'Unknown artist'} - ${target.summary.title || 'Untitled'} / ${stage}`;
  })();

  const knownFields = PRIMARY_METADATA_FIELDS.filter(([key]) => key !== 'title' && key !== 'albumartist' && !!(target?.summary as Record<string, unknown> | undefined)?.[key] && !REVIEW_SUMMARY_KEYS.has(key));
  const needsReviewFields = [...PRIMARY_METADATA_FIELDS, ...ADDITIONAL_METADATA_FIELDS].filter(([key]) => REVIEW_SUMMARY_KEYS.has(key) && !(target?.summary as Record<string, unknown> | undefined)?.[key]);
  const optionalMissingFields = ADDITIONAL_METADATA_FIELDS.filter(([key]) => OPTIONAL_SUMMARY_KEYS.has(key) && !(target?.summary as Record<string, unknown> | undefined)?.[key]);

  const submissionSentence = (() => {
    if (!target) return '';
    const noun = target.summary.release_type === 'Singleton' || target.summary.track_count === 1 && target.target_type !== 'album' ? 'one track' : `one album containing ${tracks.length} local audio file${tracks.length === 1 ? '' : 's'}`;
    const mbPart = target.summary.mb_albumid ? 'An existing MusicBrainz release has been attached.' : 'No existing MusicBrainz release has been selected yet.';
    const acoustidPart = target.preflight.acoustid_ready ? `AcoustID submission is available now (${readyFingerprints} fingerprint(s) ready).` : 'AcoustID submission will become available after recording IDs are attached.';
    return `Preparing ${noun} for MusicBrainz review. ${mbPart} ${acoustidPart}`;
  })();

  return (
    <section className="pb-24">
      <div className="rounded-md border border-graphite-800 bg-graphite-950/45 px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-red-400">Submissions</p>
            <h1 className="mt-1 text-2xl font-semibold text-zinc-100">MusicBrainz and AcoustID</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Chip label={stage} color={stage === 'Complete' ? 'success' : stage.includes('Ready') ? 'info' : 'default'} size="small" variant="outlined" />
              {activeAlbumId ? <Chip label={`Album ${activeAlbumId}`} size="small" variant="outlined" /> : null}
              {activeItemId && !activeAlbumId ? <Chip label={`Item ${activeItemId}`} size="small" variant="outlined" /> : null}
              <Chip label={`${targetLoading || !target ? (selectedItem?.tracks || 0) : tracks.length} tracks`} size="small" variant="outlined" />
              {resolvedState ? <Chip label={RESOLVED_STATE_LABEL[resolvedState] || resolvedState} size="small" variant="outlined" color={isBlockedState ? 'error' : 'default'} /> : null}
            </div>
          </div>
          <Button component={Link} to="/import?tab=review" size="small" variant="outlined">Back to Review</Button>
        </div>
        {(loading || targetLoading) ? <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} /> : null}
        {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
        {targetError ? <Alert severity="error" sx={{ mt: 2 }}>{targetError}</Alert> : null}
        {message ? <Alert severity="info" sx={{ mt: 2 }} onClose={() => setMessage('')}>{message}</Alert> : null}
      </div>

      {/* Compact workflow stepper */}
      <div className="mt-3 flex items-center gap-1 rounded-md border border-graphite-800 bg-graphite-900/70 px-3 py-2">
        {STEPS.map((label, index) => (
          <div key={label} className="flex flex-1 items-center gap-1">
            <div className={['flex h-6 min-w-6 items-center justify-center rounded-full px-1.5 text-[0.68rem] font-semibold', index === activeStep ? 'bg-red-600 text-white' : index < activeStep ? 'bg-emerald-900 text-emerald-200' : 'bg-graphite-800 text-zinc-500'].join(' ')}>{index + 1}</div>
            <span className={['truncate text-[0.7rem]', index === activeStep ? 'text-zinc-100' : 'text-zinc-500'].join(' ')}>{label}</span>
            {index < STEPS.length - 1 ? <div className={['mx-1 h-px flex-1', index < activeStep ? 'bg-emerald-800' : 'bg-graphite-800'].join(' ')} /> : null}
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
        <aside className="space-y-3 rounded-md border border-graphite-800 bg-graphite-900/70 p-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-zinc-100">Review Queue</h2>
            <Button size="small" variant="text" onClick={() => void loadQueue()}>Refresh</Button>
          </div>
          <div className="flex flex-wrap gap-1">
            {FILTERS.map((name) => <Button key={name} size="small" variant={filter === name ? 'contained' : 'outlined'} onClick={() => setFilter(name)}>{name}</Button>)}
          </div>
          <div className="max-h-[38rem] space-y-2 overflow-auto pr-1">
            {visibleItems.map((item) => {
              const id = reviewItemId(item);
              const selected = id === selectedId || item.id === selectedId;
              const status = queueStatus(item, selected, target);
              const missing = item.blocked_reason ? 1 : 0;
              const trackCount = selected && target && !targetLoading ? tracks.length : (item.tracks || item.evidence?.folder?.track_count || 0);
              return (
                <button key={id} type="button" onClick={() => selectItem(item)} className={['w-full rounded border px-3 py-2 text-left transition-colors', selected ? 'border-red-500 bg-red-950/35 text-zinc-100' : 'border-graphite-800 bg-graphite-950/40 text-zinc-300 hover:border-graphite-700 hover:bg-graphite-850'].join(' ')}>
                  <div className="truncate text-sm font-semibold">{itemTitle(item)}</div>
                  <div className="mt-1 truncate text-xs text-zinc-500">{itemArtist(item) || 'unknown artist'}{item.year ? ` / ${item.year}` : ''}</div>
                  <div className="mt-2 grid grid-cols-2 gap-1 text-[0.68rem] text-zinc-400">
                    <span>{trackCount} tracks</span>
                    <span>{item.album_id ? 'album' : 'singleton/folder'}</span>
                    <span className="truncate">{status}</span>
                    <span>{missing} missing</span>
                  </div>
                  <div className="mt-1 truncate font-mono text-[0.65rem] text-zinc-600">{item.source_folder || item.path || item.folder || ''}</div>
                </button>
              );
            })}
            {!visibleItems.length ? <div className="rounded border border-graphite-800 bg-graphite-950/35 p-3 text-sm text-zinc-400">No items match this filter.</div> : null}
          </div>
        </aside>

        <div className="space-y-4">
          {/* A. Selected item header + submission summary */}
          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
              <div className="min-w-0">
                <h2 className="truncate text-xl font-semibold text-zinc-100">{target?.summary.title || itemTitle(selectedItem)}</h2>
                <div className="mt-1 text-sm text-zinc-400">{target?.summary.albumartist || itemArtist(selectedItem) || 'Unknown artist'}</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {isBeetsTarget ? <Chip size="small" variant="outlined" label="Beets" /> : null}
                  {draft.metadata && Object.keys(draft.metadata).length ? <Chip size="small" variant="outlined" label="Draft edits" /> : null}
                  {selectedCandidateCount ? <Chip size="small" variant="outlined" label={`${selectedCandidateCount} MB candidates`} /> : null}
                </div>
              </div>
              {target?.summary.cover_art_url ? <img alt="Cover art" src={target.summary.cover_art_url} className="h-24 w-24 rounded border border-graphite-800 object-cover" /> : null}
            </div>

            {target ? <p className="mt-3 rounded border border-graphite-800 bg-graphite-950/40 p-3 text-sm text-zinc-300">{submissionSentence}</p> : null}

            {target ? (
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                <div>
                  <div className="text-[0.68rem] font-semibold uppercase tracking-wide text-emerald-400">Known</div>
                  <ul className="mt-1 space-y-1 text-xs text-zinc-300">
                    <li>Artist: {target.summary.albumartist || '—'}</li>
                    <li>Title: {target.summary.title || '—'}</li>
                    {target.summary.release_date ? <li>Year: {target.summary.release_date}</li> : null}
                    <li>Local tracks: {tracks.length}</li>
                    <li>Runtime: {target.summary.runtime_display || '—'}</li>
                    {knownFields.map(([key, label]) => <li key={key}>{label}: {String((target.summary as unknown as Record<string, unknown>)[key])}</li>)}
                  </ul>
                </div>
                <div>
                  <div className="text-[0.68rem] font-semibold uppercase tracking-wide text-amber-400">Needs review</div>
                  {needsReviewFields.length ? (
                    <ul className="mt-1 space-y-1 text-xs text-zinc-300">{needsReviewFields.map(([key, label]) => <li key={key}>{label}</li>)}</ul>
                  ) : <p className="mt-1 text-xs text-zinc-500">Nothing outstanding.</p>}
                </div>
                <div>
                  <div className="text-[0.68rem] font-semibold uppercase tracking-wide text-zinc-500">Optional</div>
                  {optionalMissingFields.length ? (
                    <ul className="mt-1 space-y-1 text-xs text-zinc-500">{optionalMissingFields.map(([key, label]) => <li key={key}>{label}</li>)}</ul>
                  ) : <p className="mt-1 text-xs text-zinc-500">Nothing to add.</p>}
                </div>
              </div>
            ) : null}

            <details className="mt-3 rounded border border-graphite-800 bg-graphite-950/35 p-3 text-xs text-zinc-400">
              <summary className="cursor-pointer text-zinc-200">What will be submitted?</summary>
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                <div><div className="font-semibold text-zinc-300">MusicBrainz</div><ul className="mt-1 list-disc pl-4"><li>Release metadata</li><li>{tracks.length}-track listing</li><li>Artist credit</li><li>Release date</li><li>Format</li><li>Artwork reference</li></ul></div>
                <div><div className="font-semibold text-zinc-300">AcoustID</div><ul className="mt-1 list-disc pl-4"><li>{tracks.filter((t) => t.file_available).length} Chromaprint fingerprints</li><li>{tracks.filter((t) => t.mb_trackid).length} MusicBrainz recording IDs</li></ul></div>
                <div><div className="font-semibold text-zinc-300">Local changes</div><ul className="mt-1 list-disc pl-4"><li>Attach MusicBrainz IDs to Beets</li><li>Write verified IDs to file tags</li><li>Save selected artwork</li></ul></div>
              </div>
              <p className="mt-3 text-zinc-500">Nothing above has been submitted yet — these are the actions the workflow below will take once you run them.</p>
            </details>

            <details className="mt-3 rounded border border-graphite-800 bg-graphite-950/35 p-3 text-xs text-zinc-400">
              <summary className="cursor-pointer text-zinc-200">Advanced lookup</summary>
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                <TextField label="Beets Album ID" size="small" value={albumId} onChange={(event) => setAlbumId(event.target.value.replace(/\D/g, ''))} />
                <TextField label="Beets Item ID" size="small" value={itemId} onChange={(event) => setItemId(event.target.value.replace(/\D/g, ''))} />
                <TextField label="Existing release MBID" size="small" value={target?.summary.mb_albumid || ''} slotProps={{ input: { readOnly: true } }} />
              </div>
            </details>
          </section>

          {/* B. Reference URLs */}
          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <h2 className="text-sm font-semibold text-zinc-100">Reference URLs</h2>
            <p className="mt-1 text-xs text-zinc-400">Paste a YouTube, MusicBrainz, Discogs, Bandcamp, or SoundCloud link to help identify this release. Metadata is retrieved without downloading any audio or video.</p>
            <div className="mt-2 flex gap-2">
              <TextField fullWidth size="small" placeholder="https://www.youtube.com/watch?v=..." value={refUrlInput} onChange={(event) => setRefUrlInput(event.target.value)} disabled={!target} />
              <Button size="small" variant="contained" disabled={!target || !refUrlInput.trim() || refUrlBusy} onClick={() => void addReferenceUrl()}>{refUrlBusy ? 'Parsing…' : 'Add reference'}</Button>
            </div>
            {refUrlError ? <Alert severity="error" sx={{ mt: 2 }}>{refUrlError}</Alert> : null}
            <div className="mt-3 space-y-3">
              {references.map((ref) => (
                <div key={ref.id} className="rounded border border-graphite-800 bg-graphite-950/35 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Chip size="small" variant="outlined" label={ref.source} />
                        <Chip size="small" color={ref.status === 'ok' ? 'success' : 'error'} label={ref.status === 'ok' ? 'Parsed' : 'Failed'} />
                      </div>
                      <a href={ref.url} target="_blank" rel="noopener noreferrer" className="mt-1 block truncate text-xs text-zinc-400 hover:text-zinc-200">{ref.url}</a>
                    </div>
                    {(ref.artwork_candidates?.[0]?.url) ? <img src={ref.artwork_candidates[0].url} alt="Thumbnail" className="h-14 w-14 rounded border border-graphite-800 object-cover" /> : null}
                  </div>
                  {ref.status === 'error' ? <Alert severity="error" sx={{ mt: 1.5 }}>{ref.error}</Alert> : null}
                  {ref.raw?.title ? <div className="mt-2 text-xs text-zinc-500">Raw title: <span className="text-zinc-300">{String(ref.raw.title)}</span></div> : null}
                  {ref.fields && ref.fields.length ? (
                    <div className="mt-2 overflow-auto rounded border border-graphite-800">
                      <table className="min-w-full text-left text-[0.7rem]">
                        <thead className="bg-graphite-950 text-zinc-500"><tr><th className="px-2 py-1">Field</th><th className="px-2 py-1">Suggested value</th><th className="px-2 py-1">Source</th><th className="px-2 py-1">Confidence</th><th className="px-2 py-1" /></tr></thead>
                        <tbody>{ref.fields.map((f) => (
                          <tr key={f.field} className="border-t border-graphite-800">
                            <td className="px-2 py-1 capitalize text-zinc-300">{f.field}</td>
                            <td className="px-2 py-1 text-zinc-200">{f.value}</td>
                            <td className="px-2 py-1 text-zinc-500">{fieldSourceLabel(f.source)}</td>
                            <td className="px-2 py-1"><Chip size="small" label={f.confidence} color={f.confidence === 'high' ? 'success' : f.confidence === 'medium' ? 'warning' : 'default'} /></td>
                            <td className="px-2 py-1"><Button size="small" variant="outlined" onClick={() => applyReferenceField(f.field, f.value)}>Apply</Button></td>
                          </tr>
                        ))}</tbody>
                      </table>
                    </div>
                  ) : null}
                  {ref.normalized?.is_topic_channel ? <Alert severity="info" sx={{ mt: 1.5 }}>This channel name ends in "- Topic" (an auto-generated YouTube Music channel), which usually is the real artist name.</Alert> : null}
                  {ref.normalized?.likely_label_channel ? <Alert severity="warning" sx={{ mt: 1.5 }}>This channel looks like a label/distributor, not the recording artist. Review before applying the artist suggestion.</Alert> : null}
                  {(ref.mb_links?.length || ref.discogs_links?.length) ? (
                    <div className="mt-2 text-xs text-zinc-400">
                      <div className="font-semibold text-zinc-300">Links found in the description</div>
                      <div className="mt-1 flex flex-wrap gap-2">
                        {(ref.mb_links || []).map((link) => (
                          <Button key={link} size="small" variant="outlined" onClick={() => { setMbInput(link); setValidation(null); }}>Use as MusicBrainz release</Button>
                        ))}
                        {(ref.discogs_links || []).map((link) => (
                          <Button key={link} size="small" variant="outlined" onClick={() => { setRefUrlInput(link); void addReferenceUrl(); }}>Add Discogs link</Button>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  <div className="mt-2 flex gap-2">
                    <Button size="small" variant="text" onClick={() => reprocessReferenceUrl(ref.url)}>Reprocess</Button>
                    <Button size="small" variant="text" color="error" onClick={() => removeReferenceUrl(ref.id)}>Remove</Button>
                  </div>
                </div>
              ))}
              {!references.length ? <p className="text-xs text-zinc-500">No reference URLs added yet.</p> : null}
            </div>
          </section>

          {/* D. Preflight problems */}
          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <h2 className="text-sm font-semibold text-zinc-100">Preflight Checklist</h2>
            {!target ? <p className="mt-2 text-xs text-zinc-500">Select a resolvable review item to see preflight checks.</p> : (
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                {(target.preflight.checks || []).map((check) => <div key={check.label} className="rounded border border-graphite-800 bg-graphite-950/35 p-3"><div className="flex items-center justify-between gap-2"><span className="text-sm text-zinc-200">{check.label}</span><Chip size="small" label={check.status} color={check.status === 'pass' ? 'success' : check.status === 'warning' ? 'warning' : 'error'} /></div>{check.explanation ? <p className="mt-2 text-xs text-zinc-400">{check.explanation}</p> : null}{check.action ? <p className="mt-1 text-xs text-red-300">{check.action}</p> : null}{check.affected?.length ? <p className="mt-1 truncate text-xs text-zinc-500">{check.affected.slice(0, 4).join(', ')}</p> : null}</div>)}
              </div>
            )}
          </section>

          {/* E. Track list, moved above the metadata editor */}
          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between"><h2 className="text-sm font-semibold text-zinc-100">Track Submission Preview</h2><div className="flex flex-wrap gap-2"><Button size="small" variant="outlined" disabled={!tracks.length} onClick={() => void copyText(parserText, 'Track-parser text')}>Copy track-parser text</Button><Button size="small" variant="outlined" disabled={!tracks.length} onClick={() => void copyText(`${target?.summary.albumartist || ''} - ${target?.summary.title || ''}\n\n${parserText}`, 'Submission summary')}>Copy complete summary</Button></div></div>
            {!tracks.length ? <p className="mt-3 text-sm text-zinc-500">{target ? 'No tracks were found for this review item.' : 'Select a review item to see its tracks.'}</p> : (
              <div className="mt-3 overflow-auto rounded border border-graphite-800">
                <table className="min-w-full text-left text-xs"><thead className="bg-graphite-950 text-zinc-400"><tr><th className="px-2 py-2">Disc</th><th className="px-2 py-2">Track</th><th className="px-2 py-2">Title</th><th className="px-2 py-2">Artist credit</th><th className="px-2 py-2">Duration</th><th className="px-2 py-2">File</th><th className="px-2 py-2">AcoustID</th><th className="px-2 py-2">Recording MBID</th><th className="px-2 py-2">Validation</th></tr></thead>
                  <tbody>{tracks.map((track) => { const edit = draft.trackEdits[String(track.item_id)] || {}; return <tr key={track.item_id || track.file_path || track.index} className="border-t border-graphite-800"><td className="px-2 py-2"><TextField size="small" value={edit.disc ?? track.disc} onChange={(event) => updateTrack(track.item_id, 'disc', Number(event.target.value) || 1)} sx={{ width: 72 }} /></td><td className="px-2 py-2"><TextField size="small" value={edit.track ?? track.track} onChange={(event) => updateTrack(track.item_id, 'track', Number(event.target.value) || track.track)} sx={{ width: 72 }} /></td><td className="px-2 py-2 min-w-56"><TextField fullWidth size="small" value={edit.title ?? track.title} onChange={(event) => updateTrack(track.item_id, 'title', event.target.value)} /></td><td className="px-2 py-2 min-w-48"><TextField fullWidth size="small" value={edit.artist ?? track.artist} onChange={(event) => updateTrack(track.item_id, 'artist', event.target.value)} /></td><td className="px-2 py-2 text-zinc-300">{track.duration_display || 'Missing'}</td><td className="max-w-56 truncate px-2 py-2 text-zinc-400">{track.file_name}<div className={track.file_available ? 'text-emerald-400' : 'text-red-300'}>{track.file_available ? 'available' : 'missing'}</div></td><td className="px-2 py-2 text-zinc-300">{track.fingerprint_status}</td><td className="max-w-44 truncate px-2 py-2 font-mono text-zinc-400">{track.mb_trackid || 'Missing'}</td><td className="px-2 py-2 text-zinc-300">{track.validation_status}</td></tr>; })}</tbody></table>
              </div>
            )}
            <details className="mt-3 rounded border border-graphite-800 bg-graphite-950/35 p-3"><summary className="cursor-pointer text-sm text-zinc-200">Exact MusicBrainz track-parser text</summary><pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap text-xs text-zinc-300">{draft.mbsubmit_output || mbRun.output || parserText || 'No track text available yet.'}</pre></details>
          </section>

          {/* F. Metadata editor: primary fields first, everything else collapsed */}
          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-sm font-semibold text-zinc-100">Editable Release Metadata</h2>
              {saveState === 'saving' ? <Chip size="small" label="Saving…" color="warning" variant="outlined" />
                : saveState === 'saved' ? <Chip size="small" label="Saved" color="success" variant="outlined" />
                : saveState === 'error' ? <Chip size="small" label="Save failed" color="error" variant="outlined" />
                : dirty ? <Chip size="small" label="Unsaved changes" color="warning" variant="outlined" /> : null}
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-3">
              {PRIMARY_METADATA_FIELDS.map(([key, label]) => {
                const backendValue = String((target?.summary as Record<string, unknown> | undefined)?.[key] ?? '');
                const draftValue = draft.metadata[key];
                return <TextField key={key} label={label} size="small" value={draftValue ?? backendValue} onChange={(event) => updateMeta(key, event.target.value)} helperText={draftValue ? 'User-edited draft' : (backendValue ? 'Read from Beets/file tags' : undefined)} />;
              })}
            </div>
            <details className="mt-3 rounded border border-graphite-800 bg-graphite-950/35 p-3">
              <summary className="cursor-pointer text-sm text-zinc-200">Additional release details</summary>
              <div className="mt-3 grid gap-3 md:grid-cols-3">
                {ADDITIONAL_METADATA_FIELDS.map(([key, label]) => {
                  const backendValue = String((target?.summary as Record<string, unknown> | undefined)?.[key] ?? '');
                  const draftValue = draft.metadata[key];
                  return <TextField key={key} label={label} size="small" value={draftValue ?? backendValue} onChange={(event) => updateMeta(key, event.target.value)} helperText={draftValue ? 'User-edited draft' : (backendValue ? 'Read from Beets/file tags' : undefined)} />;
                })}
              </div>
            </details>
          </section>

          <section className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4"><h2 className="text-sm font-semibold text-zinc-100">Duplicate Detection</h2><p className="mt-1 text-sm text-zinc-400">Likely existing releases from the current review evidence.</p><div className="mt-3 space-y-2">{(selectedItem?.evidence?.top_candidates || []).slice(0, 6).map((candidate, index) => <button key={`${candidate.mb_albumid || index}`} type="button" className="w-full rounded border border-graphite-800 bg-graphite-950/35 p-3 text-left hover:border-red-600" onClick={() => { setMbInput(candidate.mb_url || candidate.mb_albumid || ''); setValidation(null); }}><div className="text-sm text-zinc-100">{candidate.album || candidate.artist || 'MusicBrainz candidate'}</div><div className="mt-1 text-xs text-zinc-400">{candidate.artist} / {candidate.date || candidate.year || 'date unknown'} / {candidate.country || 'country unknown'} / {candidate.format_summary || candidate.formats?.join(', ') || 'format unknown'}</div><div className="mt-1 font-mono text-[0.68rem] text-zinc-500">{candidate.mb_albumid || candidate.mb_releasegroupid}</div></button>)}{!selectedCandidateCount ? <Alert severity="info">No duplicate candidates are attached to this review item yet. Prepare the release only after checking MusicBrainz.</Alert> : null}</div></div>
            <div className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4"><h2 className="text-sm font-semibold text-zinc-100">MusicBrainz Handoff</h2><p className="mt-1 text-sm text-zinc-400">The app prepares track-parser text. MusicBrainz publication stays on the MusicBrainz website in a separate tab.</p>{!isBeetsTarget && target ? <Alert severity="warning" sx={{ mt: 2 }}>This folder is not imported into Beets yet, so track-parser preparation and ID attachment are unavailable until it is imported. You can still add reference URLs and edit metadata below.</Alert> : null}<div className="mt-3 flex flex-wrap gap-2"><Button size="small" variant="contained" disabled={!target?.preflight.musicbrainz_ready || mbRun.status === 'running'} onClick={() => void runMusicBrainzPrepare()}>Prepare Submission</Button><Button size="small" variant="outlined" onClick={() => window.open(mbUrl, '_blank', 'noopener,noreferrer')}>Open MusicBrainz separately</Button></div>{mbRun.message ? <Alert severity={statusTone(mbRun.status)} sx={{ mt: 2 }}>{mbRun.message}</Alert> : null}<JobLink jobId={mbRun.jobId} /><TextField sx={{ mt: 3 }} fullWidth size="small" label="Published MusicBrainz release URL or MBID" value={mbInput} onChange={(event) => { setMbInput(event.target.value); setValidation(null); updateDraft({ ...draft, published: { ...(draft.published || {}), input: event.target.value } }); }} helperText="Paste the resulting MusicBrainz release URL here after the website confirms it exists." /><div className="mt-2 flex gap-2"><Button size="small" variant="outlined" disabled={!mbInput.trim()} onClick={() => void validatePublishedIds()}>Validate published IDs</Button>{release ? <Button size="small" variant="contained" disabled={!canApply || applyRun.status === 'running'} onClick={() => void applyPublishedIds()}>{APPLY_MBIDS_LABEL}</Button> : null}</div>{validation?.error ? <Alert severity="error" sx={{ mt: 2 }}>{validation.error}</Alert> : null}{validation?.message ? <Alert severity="info" sx={{ mt: 2 }}>{validation.message}</Alert> : null}{release ? <Alert severity={validation?.needs_confirmation ? 'warning' : 'success'} sx={{ mt: 2 }}>{release.title} / {release.albumartist} / {release.track_count} tracks. {validation?.mismatches?.length ? `${validation.mismatches.length} mismatch warning(s).` : 'Track mapping matched by position.'}</Alert> : null}{applyRun.message ? <Alert severity={statusTone(applyRun.status)} sx={{ mt: 2 }}>{applyRun.message}</Alert> : null}<JobLink jobId={applyRun.jobId} /></div>
          </section>

          <section className="rounded-md border border-graphite-800 bg-graphite-900/70 p-4"><h2 className="text-sm font-semibold text-zinc-100">AcoustID Submission</h2><p className="mt-1 text-sm text-zinc-400">Submit {readyFingerprints} fingerprints for {tracks.length} tracks linked to {tracks.filter((track) => track.mb_trackid).length} MusicBrainz recordings.</p><div className="mt-3 grid gap-2 md:grid-cols-2">{tracks.map((track) => <div key={`fp-${track.item_id || track.file_path}`} className="rounded border border-graphite-800 bg-graphite-950/35 p-2 text-xs"><div className="truncate text-zinc-200">{track.track}. {track.title}</div><div className={track.mb_trackid && track.file_available ? 'text-emerald-300' : 'text-red-300'}>{track.fingerprint_status}</div></div>)}</div><div className="mt-3 flex flex-wrap gap-2"><Button size="small" variant="contained" disabled={!target?.preflight.acoustid_ready || acoustidRun.status === 'running' || readyFingerprints === 0} onClick={() => void runAcoustidSubmit()}>{SUBMIT_FINGERPRINTS_LABEL}</Button><Button size="small" variant="outlined" disabled={!acoustidRun.output} onClick={() => void copyText(acoustidRun.output, 'AcoustID output')}>Copy output</Button></div>{acoustidRun.message ? <Alert severity={statusTone(acoustidRun.status)} sx={{ mt: 2 }}>{acoustidRun.message}</Alert> : null}<JobLink jobId={acoustidRun.jobId} />{acoustidRun.output ? <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-graphite-800 bg-graphite-950/60 p-3 text-xs text-zinc-300">{acoustidRun.output}</pre> : null}</section>
        </div>
      </div>

      <div className="fixed inset-x-0 bottom-0 z-20 border-t border-graphite-800 bg-graphite-950/95 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-screen-2xl flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div className="min-w-0 truncate text-sm text-zinc-400">
            {footerText}
            {footerBlockerCount ? <span className="ml-2 text-amber-400">{footerBlockerCount} required field{footerBlockerCount === 1 ? '' : 's'} need review.</span> : null}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button component={Link} to="/import?tab=review" size="small" variant="outlined">Back</Button>
            <Button size="small" variant="outlined" disabled={!target} onClick={() => { if (!target) return; setSaveState('saving'); void saveSubmissionDraft({ target_type: target.target_type, target_id: target.target_id, draft }).then(() => { setDirty(false); setSaveState('saved'); }).catch((err) => { setSaveState('error'); setMessage(err instanceof Error ? err.message : String(err)); }); }}>Save Draft</Button>
            <Button size="small" variant="outlined" disabled={!target} onClick={(event) => setMoreMenuAnchor(event.currentTarget)}>More actions</Button>
            <Menu anchorEl={moreMenuAnchor} open={Boolean(moreMenuAnchor)} onClose={() => setMoreMenuAnchor(null)}>
              <MenuItem onClick={() => { setMoreMenuAnchor(null); void resetDraft(); }}>Reset draft…</MenuItem>
            </Menu>
            <Button size="small" variant="contained" disabled={primary.disabled} onClick={() => void primary.action()}>{primary.label}</Button>
          </div>
        </div>
      </div>
    </section>
  );
}
