import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getAlbumMbCompleteness, getArtistDiscography, getReleaseArt, getYtdlpStatus, startAlbumDownload } from '../../api/client';
import {
  albumHasNoTrackRows,
  albumLooksComplete,
  albumMissingCount,
  albumNotImportedCount,
  getAlbumHealth,
} from '../../lib/libraryHealth';
import {
  anyDirectDownloadMethodEnabled,
  DIRECT_DOWNLOAD_METHODS,
  directDownloadMethodEnabled,
  directDownloadMethodTitle,
} from '../../lib/downloadMethods';
import { useJobPoll } from '../../lib/hooks';
import type { AlbumMbCompletenessResponse, DiscographyAlbum, DownloadMethod, YtdlpStatusResponse } from '../../api/types';
import type { LibraryAlbum } from '../../types/api';

type DiscFilter = 'all' | 'have' | 'missing';
type DiscographyRow = {
  kind: 'have' | 'missing';
  release: DiscographyAlbum | null;
  local: LibraryAlbum | null;
  sectionKey: string;
  sectionLabel: string;
  sectionOrder: number;
  sortAlbum: string;
  sortYear: string;
};

const PRIMARY_TYPE_ORDER = ['Album', 'EP', 'Single', 'Broadcast', 'Other'];
function normaliseTitle(value: string) {
  return value
    .toLowerCase()
    .replace(/\s*[\(\[]\d{4}[\)\]]\s*$/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function releaseYear(value?: string | number) {
  return value ? String(value).slice(0, 4) : '';
}

function localAlbumKey(album: LibraryAlbum) {
  return String(album.album_id || album.aldir || `${album.albumartist || ''}::${album.album || ''}::${album.year || ''}`);
}

function formatTypePart(value: string) {
  const clean = value
    .replace(/^[\s"'[\]]+|[\s"'[\]]+$/g, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!clean) return '';
  const lower = clean.toLowerCase();
  if (lower === 'ep') return 'EP';
  if (lower === 'dj mix' || lower === 'dj-mix') return 'DJ-mix';
  if (lower === 'mixtape/street' || lower === 'mixtape street') return 'Mixtape/Street';
  return clean
    .split(/(\s+|\/)/)
    .map((part) => (/^\s+$|^\/$/.test(part) ? part : part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()))
    .join('');
}

function splitReleaseTypeParts(value?: string | string[] | null) {
  const raw = Array.isArray(value) ? value.join(';') : value || '';
  return raw
    .split(/\s*(?:,|;|\+|\|)\s*/g)
    .map(formatTypePart)
    .filter(Boolean);
}

function releaseTypeInfo(primary?: string | null, subtypes?: string | string[] | null) {
  const primaryParts = splitReleaseTypeParts(primary);
  const subtypeParts = splitReleaseTypeParts(subtypes);
  const primaryType = primaryParts[0] || subtypeParts[0] || 'Album';
  const primaryOrder = PRIMARY_TYPE_ORDER.findIndex((part) => part.toLowerCase() === primaryType.toLowerCase());
  return {
    key: primaryType.toLowerCase(),
    label: primaryType,
    order: (primaryOrder >= 0 ? primaryOrder : 50) * 100,
  };
}

function shouldOfferDownload(release?: DiscographyAlbum | null, local?: LibraryAlbum | null) {
  if (albumMissingCount(local) > 0 || albumNotImportedCount(local) > 0 || albumHasNoTrackRows(local)) return true;
  if (!release) return false;
  return !release.on_disk || !local;
}

function DownloadControls({
  artistName,
  release,
  localAlbum,
  ytdlpEnabled,
  ytdlpMessage,
  ytdlpStatus,
  onLibraryChanged,
}: {
  artistName: string;
  release?: DiscographyAlbum | null;
  localAlbum?: LibraryAlbum | null;
  ytdlpEnabled: boolean;
  ytdlpMessage: string;
  ytdlpStatus: YtdlpStatusResponse | null;
  onLibraryChanged?: () => void;
}) {
  const [activeMethod, setActiveMethod] = useState<DownloadMethod | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [dlError, setDlError] = useState('');
  const [completeness, setCompleteness] = useState<AlbumMbCompletenessResponse | null>(null);
  const [completenessError, setCompletenessError] = useState('');
  const [checkingCompleteness, setCheckingCompleteness] = useState(false);
  const successHandledRef = useRef(false);
  const { job } = useJobPoll(jobId);
  const dlDone = job?.status === 'success';
  const dlFailed = job?.status === 'failed' || job?.status === 'killed';
  const dlRunning = Boolean(jobId && (!job || job.status === 'running'));
  const busy = Boolean(activeMethod) || dlRunning;
  const mbid = localAlbum?.mb_albumid || release?.mbid || '';
  const mbMissingCount = Number(completeness?.missing_count || 0);
  const localMissingCount = albumMissingCount(localAlbum);
  const localNotImportedCount = albumNotImportedCount(localAlbum);
  const localHasNoRows = albumHasNoTrackRows(localAlbum);
  const missingTrackMode = Boolean(localAlbum?.album_id && (
    localMissingCount > 0 ||
    localNotImportedCount > 0 ||
    localHasNoRows
  ));
  const mbEditionGap = Boolean(localAlbum?.album_id && !missingTrackMode && mbMissingCount > 0);
  const canDownload = missingTrackMode || shouldOfferDownload(release, localAlbum);
  const canCheckCompleteness = Boolean(localAlbum?.album_id && mbid && !completeness && !albumLooksComplete(localAlbum));

  useEffect(() => {
    if (job?.status !== 'success' || successHandledRef.current) return;
    successHandledRef.current = true;
    onLibraryChanged?.();
  }, [job?.status, onLibraryChanged]);

  async function checkCompleteness() {
    if (!localAlbum?.album_id || !mbid) return;
    setCheckingCompleteness(true);
    setCompletenessError('');
    try {
      const data = await getAlbumMbCompleteness(localAlbum.album_id, mbid);
      setCompleteness(data);
    } catch (err) {
      setCompletenessError(err instanceof Error ? err.message : String(err));
    } finally {
      setCheckingCompleteness(false);
    }
  }

  async function download(method: DownloadMethod) {
    successHandledRef.current = false;
    setActiveMethod(method);
    setJobId(null);
    setDlError('');
    const albumName = release?.album || localAlbum?.album || '';
    const year = release?.year || localAlbum?.year || '';
    try {
      const sourceFallback = method === 'slskd' && missingTrackMode && ytdlpEnabled;
      const { job_id } = await startAlbumDownload({
        artist: artistName,
        albumartist: artistName,
        album: albumName,
        year: year ? String(year).slice(0, 4) : '',
        track_count: localAlbum?.track_count || undefined,
        mb_albumid: mbid,
        existing_album_id: missingTrackMode ? localAlbum?.album_id : undefined,
        method,
        auto_import: true,
        fallback_method: sourceFallback ? 'spotiflac' : undefined,
        try_ytdlp_fallback: sourceFallback,
        try_source_fallback: sourceFallback,
      });
      setJobId(job_id);
    } catch (e) {
      setDlError(e instanceof Error ? e.message : String(e));
    } finally {
      setActiveMethod(null);
    }
  }

  if (!canDownload && !dlRunning && !dlDone && !dlFailed && !dlError && !canCheckCompleteness && !completenessError && !mbEditionGap) {
    return null;
  }

  return (
    <div className="mt-3 space-y-2">
      {canDownload && !dlDone ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            disabled={busy}
            size="small"
            title={missingTrackMode && ytdlpEnabled ? 'Try Soulseek first, then SpotiFLAC, YouTube, and SoundCloud if Soulseek has no usable candidate' : undefined}
            variant="outlined"
            onClick={() => void download('slskd')}
          >
            {activeMethod === 'slskd' || (dlRunning && !activeMethod)
              ? 'Starting...'
              : missingTrackMode
                ? ytdlpEnabled ? 'SLSKD + sources' : 'SLSKD missing'
                : 'SLSKD'}
          </Button>
          {DIRECT_DOWNLOAD_METHODS.map((source) => (
            <Button
              key={source.method}
              disabled={busy || !directDownloadMethodEnabled(source.method, ytdlpStatus)}
              size="small"
              variant="outlined"
              title={directDownloadMethodTitle(source.method, ytdlpStatus, ytdlpMessage)}
              onClick={() => void download(source.method)}
            >
              {activeMethod === source.method
                ? 'Starting...'
                : missingTrackMode
                  ? `${source.label} missing`
                  : source.label}
            </Button>
          ))}
        </div>
      ) : null}
      {!canDownload && canCheckCompleteness ? (
        <Button
          disabled={checkingCompleteness}
          size="small"
          variant="outlined"
          onClick={() => void checkCompleteness()}
        >
          {checkingCompleteness ? 'Checking...' : 'Check missing'}
        </Button>
      ) : null}
      {missingTrackMode && canDownload && !dlDone ? (
        <p className="text-xs text-amber-300">
          {localMissingCount || localNotImportedCount || localAlbum?.track_count || 'Missing'} local track(s) need import repair.
          {ytdlpEnabled ? ' SLSKD can fall back to SpotiFLAC, YouTube, and SoundCloud if no usable Soulseek source works.' : ''}
        </p>
      ) : null}
      {mbEditionGap ? (
        <p className="text-xs text-amber-300">
          MusicBrainz lists {mbMissingCount} unmatched track(s) for the selected release. Local files are not marked missing; verify the edition or link a different MusicBrainz release.
        </p>
      ) : null}
      {completeness?.missing?.length ? (
        <div className="space-y-1 rounded border border-amber-500/40 bg-amber-500/5 p-2 text-xs text-amber-100">
          {completeness.missing.slice(0, 5).map((track) => (
            <div key={`${track.disc}-${track.track}-${track.title}`} className="truncate">
              {track.disc}.{String(track.track).padStart(2, '0')} {track.title || 'Untitled track'}
            </div>
          ))}
          {completeness.missing.length > 5 ? (
            <div className="text-amber-300">+{completeness.missing.length - 5} more</div>
          ) : null}
        </div>
      ) : completeness ? (
        <p className="text-xs text-emerald-300">MusicBrainz tracklist is complete.</p>
      ) : null}
      {completenessError && localAlbum?.album_id && mbid ? (
        <p className="text-xs text-zinc-500">Could not check MusicBrainz completeness: {completenessError}</p>
      ) : null}
      {dlError && <p className="text-xs text-red-400">{dlError}</p>}
      {dlRunning && (
        <p className="line-clamp-2 text-xs text-zinc-500">
          {job?.log?.filter(Boolean).slice(-1)[0] ?? 'downloading...'}
        </p>
      )}
      {dlDone ? <p className="text-xs text-emerald-300">Download/import job finished.</p> : null}
      {dlFailed ? <p className="text-xs text-red-400">Download/import job failed. Check Jobs for details.</p> : null}
    </div>
  );
}

function MissingReleaseCard({
  album,
  artistName,
  localAlbum = null,
  ytdlpEnabled,
  ytdlpMessage,
  ytdlpStatus,
  onLibraryChanged,
}: {
  album: DiscographyAlbum;
  artistName: string;
  localAlbum?: LibraryAlbum | null;
  ytdlpEnabled: boolean;
  ytdlpMessage: string;
  ytdlpStatus: YtdlpStatusResponse | null;
  onLibraryChanged?: () => void;
}) {
  const [artFailed, setArtFailed] = useState(false);
  const [artUrl, setArtUrl] = useState('');

  useEffect(() => {
    let cancelled = false;
    setArtFailed(false);
    setArtUrl('');
    if (!album.mbid) return undefined;
    getReleaseArt(album.mbid, artistName, album.album)
      .then((res) => {
        if (!cancelled) setArtUrl(res.ok && res.url ? res.url : '');
      })
      .catch(() => {
        if (!cancelled) setArtUrl('');
      });
    return () => {
      cancelled = true;
    };
  }, [album.mbid, artistName, album.album]);

  return (
    <div className="flex min-h-full flex-col overflow-hidden rounded-md border border-graphite-800 bg-graphite-900">
      <div className="flex aspect-square w-full items-center justify-center overflow-hidden border-2 border-rose-500 bg-graphite-950 text-sm font-semibold text-zinc-500">
        {artUrl && !artFailed ? (
          <img
            alt={`${album.album || 'Missing release'} cover`}
            className="h-full w-full object-cover"
            loading="lazy"
            src={artUrl}
            onError={() => setArtFailed(true)}
          />
        ) : (
          <span className="px-3 text-center">Missing art</span>
        )}
      </div>
      <div className="flex flex-1 flex-col p-3">
        <div className="min-w-0 flex-1">
          <div className={`line-clamp-2 text-sm font-semibold ${album.on_disk ? 'text-emerald-200' : 'text-zinc-100'}`}>
            {album.album || 'Untitled release'}
          </div>
          {album.year ? (
            <span className="mt-1 text-xs text-zinc-500">{album.year}</span>
          ) : null}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {album.mb_url ? (
            <Button size="small" variant="outlined" href={album.mb_url} target="_blank" rel="noreferrer">
              MusicBrainz
            </Button>
          ) : null}
        </div>
        <DownloadControls
          artistName={artistName}
          release={album}
          localAlbum={localAlbum}
          ytdlpEnabled={ytdlpEnabled}
          ytdlpMessage={ytdlpMessage}
          ytdlpStatus={ytdlpStatus}
          onLibraryChanged={onLibraryChanged}
        />
      </div>
    </div>
  );
}

function FilterBtn({
  active,
  count,
  label,
  onClick,
}: {
  active: boolean;
  count: number;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className={`rounded border px-2.5 py-1 text-xs font-semibold transition ${
        active
          ? 'border-red-400 bg-red-400/10 text-red-300'
          : 'border-graphite-700 text-zinc-400 hover:border-graphite-600 hover:text-zinc-300'
      }`}
      onClick={onClick}
    >
      {label} <span className="opacity-70">{count}</span>
    </button>
  );
}

export function DiscographyPanel({
  artistName,
  localAlbums = [],
  compact = false,
  focusAttentionOnly = false,
  renderLocalAlbum,
  onLibraryChanged,
}: {
  artistName: string;
  localAlbums?: LibraryAlbum[];
  compact?: boolean;
  focusAttentionOnly?: boolean;
  renderLocalAlbum?: (album: LibraryAlbum) => ReactNode;
  onLibraryChanged?: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [have, setHave] = useState<DiscographyAlbum[]>([]);
  const [missing, setMissing] = useState<DiscographyAlbum[]>([]);
  const [mbArtist, setMbArtist] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [filter, setFilter] = useState<DiscFilter>('all');
  const [ytdlpStatus, setYtdlpStatus] = useState<YtdlpStatusResponse | null>(null);

  const { job } = useJobPoll(jobId);

  const fetchDiscography = useCallback(async () => {
    setLoading(true);
    setError('');
    let waitingForJob = false;
    try {
      const res = await getArtistDiscography(artistName);
      if (res.status === 'running' && res.job_id) {
        waitingForJob = true;
        setJobId(res.job_id);
        return;
      }
      if (!res.ok) throw new Error(res.error ?? 'Discography lookup failed');
      setHave(res.have ?? []);
      setMissing(res.missing ?? []);
      setMbArtist(res.mb_artist ?? artistName);
      setLoaded(true);
      setFilter('all');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!waitingForJob) setLoading(false);
    }
  }, [artistName]);

  useEffect(() => {
    void getYtdlpStatus()
      .then(setYtdlpStatus)
      .catch((err) => {
        setYtdlpStatus({
          ok: false,
          ready: false,
          enabled: false,
          cookie_file: '',
          cookie_candidates: [],
          message: err instanceof Error ? err.message : String(err),
        });
      });
  }, []);

  useEffect(() => {
    setHave([]);
    setMissing([]);
    setMbArtist('');
    setLoaded(false);
    setFilter('all');
    setJobId(null);
    setError('');
    void fetchDiscography();
  }, [fetchDiscography]);

  // When job completes re-fetch the now-cached discography.
  useEffect(() => {
    if (job?.status === 'success') {
      setJobId(null);
      void fetchDiscography();
    } else if (job?.status === 'failed' || job?.status === 'killed') {
      setJobId(null);
      setLoading(false);
      setError('Discography lookup failed. Try again.');
    }
  }, [fetchDiscography, job?.status]);

  const merged = useMemo(() => {
    const allDiscography = [...have, ...missing];
    const usedLocalIds = new Set<number>();

    function findLocal(release: DiscographyAlbum) {
      const releaseMbid = (release.mbid || '').trim().toLowerCase();
      if (releaseMbid) {
        const byMbid = localAlbums.find((album) => (
          (album.mb_releasegroupid || '').trim().toLowerCase() === releaseMbid ||
          (album.mb_albumid || '').trim().toLowerCase() === releaseMbid
        ));
        if (byMbid) return byMbid;
      }

      const releaseTitle = normaliseTitle(release.album || '');
      const year = releaseYear(release.year);
      return localAlbums.find((album) => {
        if (usedLocalIds.has(album.album_id)) return false;
        if (normaliseTitle(album.album || '') !== releaseTitle) return false;
        const localYear = releaseYear(album.year);
        return !year || !localYear || year === localYear;
      }) ?? null;
    }

    const rows: DiscographyRow[] = allDiscography.map((release) => {
      const local = findLocal(release);
      if (local) usedLocalIds.add(local.album_id);
      const section = releaseTypeInfo(
        release.type || local?.albumtype || '',
        release.subtypes?.length ? release.subtypes : local?.albumtypes || '',
      );
      return {
        kind: local || release.on_disk ? 'have' as const : 'missing' as const,
        release,
        local,
        sectionKey: section.key,
        sectionLabel: section.label,
        sectionOrder: section.order,
        sortAlbum: normaliseTitle(local?.album || release.album || ''),
        sortYear: releaseYear(local?.year || release.year) || '9999',
      };
    });

    for (const album of localAlbums) {
      if (usedLocalIds.has(album.album_id)) continue;
      const section = releaseTypeInfo(album.albumtype, album.albumtypes);
      rows.push({
        kind: 'have',
        release: null,
        local: album,
        sectionKey: section.key,
        sectionLabel: section.label,
        sectionOrder: section.order,
        sortAlbum: normaliseTitle(album.album || ''),
        sortYear: releaseYear(album.year) || '9999',
      });
    }

    rows.sort((a, b) => a.sortYear.localeCompare(b.sortYear) || a.sortAlbum.localeCompare(b.sortAlbum));
    return rows;
  }, [have, localAlbums, missing]);

  const haveCount = merged.filter((item) => item.kind === 'have').length;
  const missingCount = merged.filter((item) => item.kind === 'missing').length;
  const visible = filter === 'have'
    ? merged.filter((item) => item.kind === 'have')
    : filter === 'missing'
      ? merged.filter((item) => item.kind === 'missing')
      : merged;
  const focusedVisible = focusAttentionOnly
    ? visible.filter((item) => item.local ? getAlbumHealth(item.local).needsAttention : false)
    : visible;
  const groupedVisible = Array.from(focusedVisible.reduce((groups, item) => {
    const existing = groups.get(item.sectionKey) ?? {
      key: item.sectionKey,
      label: item.sectionLabel,
      order: item.sectionOrder,
      items: [] as DiscographyRow[],
    };
    existing.items.push(item);
    groups.set(item.sectionKey, existing);
    return groups;
  }, new Map<string, { key: string; label: string; order: number; items: DiscographyRow[] }>()))
    .map(([, group]) => group)
    .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));

  const jobRunning = Boolean(jobId && (!job || job.status === 'running'));
  const jobLog = job?.log?.filter(Boolean).slice(-1)[0] ?? '';
  const ytdlpEnabled = anyDirectDownloadMethodEnabled(ytdlpStatus);
  const ytdlpMessage = ytdlpStatus?.message || 'Checking yt-dlp status...';

  return (
    <div className="rounded-md border border-graphite-800 bg-graphite-950/40">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <div>
          <span className="text-sm font-semibold text-zinc-200">
            MusicBrainz Discography
          </span>
          {mbArtist && mbArtist !== artistName && (
            <span className="ml-2 text-xs text-zinc-500">matched as "{mbArtist}"</span>
          )}
          {ytdlpStatus && !ytdlpEnabled ? (
            <div className="mt-1 text-xs text-amber-300">Direct sources unavailable until yt-dlp is ready.</div>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {loaded && (
            <>
              <FilterBtn active={filter === 'all'}     count={merged.length}  label="All"     onClick={() => setFilter('all')} />
              <FilterBtn active={filter === 'have'}    count={haveCount}      label="Have"    onClick={() => setFilter('have')} />
              <FilterBtn active={filter === 'missing'} count={missingCount}   label="Missing" onClick={() => setFilter('missing')} />
            </>
          )}
          <Button
            disabled={loading || jobRunning}
            size="small"
            variant="outlined"
            onClick={() => void fetchDiscography()}
          >
            Refresh
          </Button>
        </div>
      </div>

      {(loading || jobRunning) && (
        <div className="px-4 pb-2 space-y-1">
          <LinearProgress />
          {jobRunning && jobLog && (
            <p className="text-xs text-zinc-500">{jobLog}</p>
          )}
        </div>
      )}

      {error && (
        <div className="px-4 pb-3">
          <Alert severity="error">{error}</Alert>
        </div>
      )}

      {!loading && (loaded || localAlbums.length > 0) && (
        <div className="border-t border-graphite-800 p-3">
          {focusedVisible.length === 0 ? (
            <p className="py-4 text-sm text-zinc-500">
              {focusAttentionOnly ? 'No albums need attention.' : filter === 'missing' ? 'You have everything!' : 'No releases found.'}
            </p>
          ) : (
            <div className="space-y-5">
              {groupedVisible.map((section) => (
                <section key={section.key}>
                  <div className="mb-2 flex items-center gap-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-300">{section.label}</h3>
                    <span className="rounded bg-graphite-900 px-1.5 py-0.5 text-[0.68rem] tabular-nums text-zinc-400">
                      {section.items.length}
                    </span>
                  </div>
                  <div className={compact ? 'flex flex-col gap-1' : 'grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4'}>
                    {section.items.map((item) => (
                      item.local && renderLocalAlbum ? (
                        <div key={`local-${localAlbumKey(item.local)}`} className="space-y-2">
                          {renderLocalAlbum(item.local)}
                          <DownloadControls
                            artistName={artistName}
                            release={item.release}
                            localAlbum={item.local}
                            ytdlpEnabled={ytdlpEnabled}
                            ytdlpMessage={ytdlpMessage}
                            ytdlpStatus={ytdlpStatus}
                            onLibraryChanged={onLibraryChanged}
                          />
                        </div>
                      ) : item.release ? (
                        <MissingReleaseCard
                          key={item.release.mbid || `${item.release.year}-${item.release.album}`}
                          album={item.release}
                          artistName={artistName}
                          ytdlpEnabled={ytdlpEnabled}
                          ytdlpMessage={ytdlpMessage}
                          ytdlpStatus={ytdlpStatus}
                          onLibraryChanged={onLibraryChanged}
                        />
                      ) : null
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
