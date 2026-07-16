import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
  Disclosure,
  DisclosureButton,
  DisclosurePanel,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  getLidarrArtistAlbumsByName,
  getWantedLidarr,
  getYtdlpStatus,
  reimportDisk,
  runLidarrAlbumSearch,
  startAlbumDownload,
} from '../../api/client';
import type { DownloadAlbumPayload, DownloadMethod, LidarrArtistAlbum, LidarrWantedAlbum, YtdlpStatusResponse } from '../../api/types';
import {
  anyDirectDownloadMethodEnabled,
  DIRECT_DOWNLOAD_METHODS,
  directDownloadMethodEnabled,
  directDownloadMethodTitle,
  downloadMethodLabel,
} from '../../lib/downloadMethods';
import { useJobPoll } from '../../lib/hooks';

type LookupState = {
  albums: LidarrArtistAlbum[] | null;
  artistPath: string;
  error: string;
  found: boolean;
  lidarrArtist: string;
  loading: boolean;
};

type WantedActionKind = DownloadMethod | 'import';

type WantedAction = {
  album: LidarrWantedAlbum;
  kind: WantedActionKind;
  status?: LidarrArtistAlbum;
};

const EMPTY_LOOKUP: LookupState = {
  albums: null,
  artistPath: '',
  error: '',
  found: false,
  lidarrArtist: '',
  loading: false,
};

function titleKey(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function groupByArtist(albums: LidarrWantedAlbum[]) {
  const groups = new Map<string, LidarrWantedAlbum[]>();
  for (const album of albums) {
    const artist = album.artist || '(unknown artist)';
    groups.set(artist, [...(groups.get(artist) ?? []), album]);
  }
  return Array.from(groups.entries())
    .map(([artist, items]) => ({
      artist,
      items: [...items].sort((a, b) => `${a.year} ${a.album}`.localeCompare(`${b.year} ${b.album}`)),
    }))
    .sort((a, b) => a.artist.localeCompare(b.artist));
}

function matchLidarrAlbum(wanted: LidarrWantedAlbum, lookup: LidarrArtistAlbum[] | null) {
  if (!lookup?.length) return undefined;
  const wantedMbid = wanted.mb_albumid?.trim().toLowerCase();
  const wantedTitle = titleKey(wanted.album);

  return (
    lookup.find((album) => album.lidarr_id === wanted.lidarr_id) ??
    lookup.find((album) => wantedMbid && album.mb_albumid?.trim().toLowerCase() === wantedMbid) ??
    lookup.find((album) => titleKey(album.title) === wantedTitle) ??
    lookup.find((album) => {
      const key = titleKey(album.title);
      return Boolean(key && wantedTitle && (key.includes(wantedTitle) || wantedTitle.includes(key)));
    })
  );
}

function statusChip(status?: LidarrArtistAlbum) {
  if (!status) return <Chip label="status not loaded" size="small" variant="outlined" />;
  if (status.percent >= 100 && (status.aldir || status.disk_path)) {
    return <Chip color="secondary" label="ready to import" size="small" variant="outlined" />;
  }
  if (status.percent >= 100) {
    return <Chip color="warning" label="downloaded, no path" size="small" variant="outlined" />;
  }
  if (status.percent > 0) {
    return <Chip color="warning" label={`${status.percent}% downloaded`} size="small" variant="outlined" />;
  }
  return <Chip color="error" label="not downloaded" size="small" variant="outlined" />;
}

function actionTitle(action: WantedAction | null) {
  if (!action) return '';
  if (action.kind === 'import') return 'Import and tag album?';
  if (action.kind === 'slskd') return 'Download from Soulseek and tag?';
  return `Download from ${downloadMethodLabel(action.kind)} and tag?`;
}

function actionBody(action: WantedAction | null) {
  if (!action) return '';
  const target = `${action.album.artist} - ${action.status?.title || action.album.album}`;
  if (action.kind === 'import') {
    return `${target} will be imported into Beets from the Lidarr album folder using the selected MusicBrainz ID.`;
  }
  const source = action.kind === 'slskd' ? 'slskd' : downloadMethodLabel(action.kind);
  return `${target} will be searched with ${source}, downloaded, then imported and tagged by Beets.`;
}

function lastLogLine(log?: string[]) {
  return (log ?? []).filter(Boolean).slice(-1)[0] ?? '';
}

function WantedActionDialog({
  action,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  action: WantedAction | null;
  busy: boolean;
  error: string;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={Boolean(action)} onClose={busy ? () => undefined : onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/70" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-md rounded-md border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">{actionTitle(action)}</DialogTitle>
          <p className="mt-2 text-sm leading-6 text-zinc-400">{actionBody(action)}</p>
          {error ? <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert> : null}
          <div className="mt-5 flex justify-end gap-2">
            <Button disabled={busy} variant="outlined" onClick={onClose}>
              Cancel
            </Button>
            <Button disabled={busy} variant="contained" onClick={onConfirm}>
              {busy ? 'Starting...' : 'Confirm'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

function WantedRow({
  album,
  lookupLoaded,
  onLoadStatus,
  onReloadStatus,
  status,
  ytdlpEnabled,
  ytdlpMessage,
  ytdlpStatus,
}: {
  album: LidarrWantedAlbum;
  lookupLoaded: boolean;
  onLoadStatus: () => void;
  onReloadStatus: () => void;
  status?: LidarrArtistAlbum;
  ytdlpEnabled: boolean;
  ytdlpMessage: string;
  ytdlpStatus: YtdlpStatusResponse | null;
}) {
  const [action, setAction] = useState<WantedAction | null>(null);
  const [starting, setStarting] = useState(false);
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [searching, setSearching] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const { job, error: jobError } = useJobPoll(jobId);

  const running = Boolean(jobId && (!job || job.status === 'running'));
  const mbid = status?.mb_albumid || album.mb_albumid || '';
  const path = status?.aldir || status?.disk_path || '';
  const readyToImport = Boolean(status && status.percent >= 100 && path && mbid);
  const latest = lastLogLine(job?.log);

  useEffect(() => {
    if (job?.status === 'success') {
      onReloadStatus();
    }
  }, [job?.status, onReloadStatus]);

  const runDownload = async (method: DownloadMethod) => {
    setStarting(true);
    setActionError('');
    setNotice('');
    try {
      const actionMbid = status?.mb_albumid || album.mb_albumid || '';
      const payload: DownloadAlbumPayload = {
        artist: album.artist,
        albumartist: album.artist,
        album: status?.title || album.album,
        year: status?.year || album.year || '',
        track_count: status?.total_track_count || 0,
        mb_albumid: actionMbid,
        method,
        auto_import: true,
      };
      const started = await startAlbumDownload(payload);
      setJobId(started.job_id);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  };

  const runAction = async () => {
    if (!action) return;
    setStarting(true);
    setActionError('');
    setNotice('');
    try {
      const actionStatus = action.status;
      const actionMbid = actionStatus?.mb_albumid || action.album.mb_albumid || '';
      const aldir = actionStatus?.aldir || actionStatus?.disk_path || '';
      if (!aldir) throw new Error('Lidarr did not provide a disk path for this album.');
      if (!actionMbid) throw new Error('A MusicBrainz release ID is required before importing.');
      const started = await reimportDisk({
        aldir,
        mb_albumid: actionMbid,
        albumartist: action.album.artist,
      });
      setJobId(started.job_id);
      setAction(null);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  };

  const searchLidarr = async () => {
    setSearching(true);
    setNotice('');
    setActionError('');
    try {
      const result = await runLidarrAlbumSearch(album.lidarr_id);
      setNotice(result.command_id ? `Lidarr search queued: command ${result.command_id}` : 'Lidarr search queued.');
      onReloadStatus();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="border-t border-graphite-800 px-4 py-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-zinc-200">{status?.title || album.album}</span>
            {status?.year || album.year ? <span className="text-sm text-zinc-500">{status?.year || album.year}</span> : null}
            {status?.album_type || album.type ? (
              <Chip label={status?.album_type || album.type} size="small" variant="outlined" sx={{ fontSize: '0.65rem' }} />
            ) : null}
            {!album.monitored ? <Chip color="warning" label="unmonitored" size="small" variant="outlined" /> : null}
            {statusChip(status)}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-500">
            {status ? <span>{status.track_file_count}/{status.total_track_count || 0} tracks</span> : null}
            {mbid ? <span className="font-mono">{mbid}</span> : <span>No MusicBrainz ID</span>}
            {path ? <span className="max-w-xl truncate font-mono">{path}</span> : null}
          </div>
          {album.mb_url ? (
            <a className="text-xs text-red-400 hover:text-red-300" href={album.mb_url} rel="noreferrer" target="_blank">
              MusicBrainz
            </a>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-wrap justify-start gap-2 lg:justify-end">
          {!lookupLoaded ? (
            <Button disabled={running} size="small" variant="outlined" onClick={onLoadStatus}>
              Load Status
            </Button>
          ) : null}
          <Button disabled={running || searching} size="small" variant="outlined" onClick={() => void searchLidarr()}>
            {searching ? 'Searching...' : 'Search Lidarr'}
          </Button>
          <Button
            disabled={running || starting}
            size="small"
            variant="outlined"
            onClick={() => void runDownload('slskd')}
          >
            {starting ? '…' : 'slskd'}
          </Button>
          {DIRECT_DOWNLOAD_METHODS.map((source) => (
            <Button
              key={source.method}
              disabled={running || starting || !directDownloadMethodEnabled(source.method, ytdlpStatus)}
              size="small"
              variant="outlined"
              title={directDownloadMethodTitle(source.method, ytdlpStatus, ytdlpMessage)}
              onClick={() => void runDownload(source.method)}
            >
              {starting ? '...' : source.shortLabel}
            </Button>
          ))}
          <Button
            disabled={running || !readyToImport}
            size="small"
            variant="contained"
            onClick={() => setAction({ album, kind: 'import', status })}
          >
            Import & Tag
          </Button>
        </div>
      </div>

      {lookupLoaded && !status ? (
        <div className="mt-2 text-xs text-amber-300">No matching Lidarr album status was returned for this wanted item.</div>
      ) : null}
      {!ytdlpEnabled ? (
        <div className="mt-2 text-xs text-zinc-500">Direct downloads disabled: {ytdlpMessage}</div>
      ) : null}
      {actionError ? <Alert severity="error" sx={{ mt: 2 }}>{actionError}</Alert> : null}
      {notice ? <Alert severity="info" sx={{ mt: 2 }} onClose={() => setNotice('')}>{notice}</Alert> : null}
      {jobId || jobError ? (
        <div className="mt-3 rounded border border-graphite-800 bg-graphite-950/70 p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {running ? <span className="inline-block size-2 animate-pulse rounded-full bg-sky-400" /> : null}
            <span className="font-medium text-zinc-200">
              {job?.status === 'success' ? 'Job complete' : job?.status === 'failed' ? 'Job failed' : 'Job running'}
            </span>
            {job?.status ? (
              <Chip
                color={job.status === 'success' ? 'success' : job.status === 'failed' ? 'error' : 'info'}
                label={job.status}
                size="small"
                variant="outlined"
              />
            ) : null}
            {jobId ? <span className="font-mono text-xs text-zinc-500">{jobId}</span> : null}
          </div>
          {running ? <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} /> : null}
          {jobError ? <div className="mt-2 text-xs text-red-300">{jobError}</div> : null}
          {latest ? <div className="mt-2 text-xs text-zinc-400">{latest}</div> : null}
        </div>
      ) : null}

      <WantedActionDialog
        action={action}
        busy={starting}
        error={actionError}
        onClose={() => {
          setAction(null);
          setActionError('');
        }}
        onConfirm={() => void runAction()}
      />
    </div>
  );
}

function ArtistWantedGroup({
  albums,
  artist,
  ytdlpEnabled,
  ytdlpMessage,
  ytdlpStatus,
}: {
  artist: string;
  albums: LidarrWantedAlbum[];
  ytdlpEnabled: boolean;
  ytdlpMessage: string;
  ytdlpStatus: YtdlpStatusResponse | null;
}) {
  const [lookup, setLookup] = useState<LookupState>(EMPTY_LOOKUP);

  const loadStatus = useCallback(async () => {
    setLookup((current) => ({ ...current, error: '', loading: true }));
    try {
      const response = await getLidarrArtistAlbumsByName(artist);
      setLookup({
        albums: response.albums ?? [],
        artistPath: response.artist_path ?? '',
        error: '',
        found: Boolean(response.found),
        lidarrArtist: response.lidarr_artist ?? '',
        loading: false,
      });
    } catch (err) {
      setLookup((current) => ({
        ...current,
        error: err instanceof Error ? err.message : String(err),
        loading: false,
      }));
    }
  }, [artist]);

  const lookupLoaded = lookup.albums !== null;

  return (
    <Disclosure defaultOpen>
      {({ open }) => (
        <div className="overflow-hidden rounded border border-graphite-800 bg-graphite-950">
          <div className="flex flex-col gap-3 bg-graphite-950/60 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <DisclosureButton className="min-w-0 flex-1 text-left">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-zinc-100">{artist}</span>
                  <Chip color="error" label={`${albums.length} wanted`} size="small" variant="outlined" />
                  {lookupLoaded ? <Chip label={`${lookup.albums?.length ?? 0} Lidarr albums`} size="small" variant="outlined" /> : null}
                  <span className="text-xs text-zinc-500">{open ? 'Hide' : 'Show'}</span>
                </div>
                {lookup.lidarrArtist || lookup.artistPath ? (
                  <div className="mt-1 truncate text-xs text-zinc-500">
                    {lookup.lidarrArtist || artist}
                    {lookup.artistPath ? ` - ${lookup.artistPath}` : ''}
                  </div>
                ) : null}
              </div>
            </DisclosureButton>
            <div className="flex items-center gap-2">
              <Button
                disabled={lookup.loading}
                size="small"
                variant="outlined"
                onClick={() => void loadStatus()}
              >
                {lookup.loading ? 'Loading...' : lookupLoaded ? 'Reload Status' : 'Load Status'}
              </Button>
            </div>
          </div>

          <DisclosurePanel>
            {lookup.loading ? <LinearProgress sx={{ borderRadius: 0 }} /> : null}
            {lookup.error ? <Alert severity="error" sx={{ m: 2 }}>{lookup.error}</Alert> : null}
            {lookupLoaded && !lookup.found ? (
              <Alert severity="warning" sx={{ m: 2 }}>
                Lidarr did not return an artist match for {artist}.
              </Alert>
            ) : null}
            {albums.map((album) => (
              <WantedRow
                key={`${album.lidarr_id}-${album.mb_albumid || album.album}`}
                album={album}
                lookupLoaded={lookupLoaded}
                onLoadStatus={loadStatus}
                onReloadStatus={loadStatus}
                status={matchLidarrAlbum(album, lookup.albums)}
                ytdlpEnabled={ytdlpEnabled}
                ytdlpMessage={ytdlpMessage}
                ytdlpStatus={ytdlpStatus}
              />
            ))}
          </DisclosurePanel>
        </div>
      )}
    </Disclosure>
  );
}

export function WantedPanel() {
  const [albums, setAlbums] = useState<LidarrWantedAlbum[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [showUnmonitored, setShowUnmonitored] = useState(false);
  const [ytdlpStatus, setYtdlpStatus] = useState<YtdlpStatusResponse | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await getWantedLidarr();
      setAlbums(response.missing ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
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

  const visibleAlbums = useMemo(() => {
    const q = query.trim().toLowerCase();
    return albums.filter((album) => {
      if (!showUnmonitored && !album.monitored) return false;
      if (!q) return true;
      return (
        album.artist.toLowerCase().includes(q) ||
        album.album.toLowerCase().includes(q) ||
        String(album.year || '').includes(q)
      );
    });
  }, [albums, query, showUnmonitored]);

  const groups = useMemo(() => groupByArtist(visibleAlbums), [visibleAlbums]);
  const monitored = albums.filter((album) => album.monitored).length;
  const unmonitored = albums.length - monitored;
  const ytdlpEnabled = anyDirectDownloadMethodEnabled(ytdlpStatus);
  const ytdlpMessage = ytdlpStatus?.message || 'Checking yt-dlp status...';

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h2 className="text-base font-medium text-zinc-200">Lidarr wanted albums</h2>
          {!loading && !error ? (
            <p className="mt-0.5 text-sm text-zinc-500">
              {monitored} monitored
              {unmonitored > 0 ? `, ${unmonitored} unmonitored` : ''}
            </p>
          ) : null}
          {ytdlpStatus && !ytdlpEnabled ? (
            <p className="mt-1 text-xs text-amber-300">Direct sources unavailable until yt-dlp is ready.</p>
          ) : null}
        </div>
        <Button size="small" variant="outlined" disabled={loading} onClick={() => void load()}>
          Refresh
        </Button>
      </div>

      {loading ? <LinearProgress sx={{ borderRadius: 1 }} /> : null}
      {error ? (
        <Alert severity={error.includes('not configured') ? 'warning' : 'error'}>
          {error.includes('not configured')
            ? 'Lidarr API key is not configured. Set LIDARR_API_KEY and LIDARR_URL in the container environment.'
            : error}
        </Alert>
      ) : null}

      {!loading && !error ? (
        <>
          <div className="flex flex-wrap gap-3">
            <TextField
              label="Filter"
              placeholder="artist, album, or year"
              size="small"
              value={query}
              sx={{ minWidth: '16rem' }}
              onChange={(event) => setQuery(event.target.value)}
            />
            <label className="flex cursor-pointer items-center gap-2 rounded border border-graphite-700 px-3 text-sm text-zinc-400">
              <input
                checked={showUnmonitored}
                className="accent-red-500"
                type="checkbox"
                onChange={(event) => setShowUnmonitored(event.target.checked)}
              />
              Show unmonitored
            </label>
          </div>

          {albums.length === 0 ? (
            <Alert severity="success">No missing albums. Lidarr wanted list is empty.</Alert>
          ) : groups.length === 0 ? (
            <p className="text-sm text-zinc-500">No albums match the filter.</p>
          ) : (
            <div className="space-y-3">
              {groups.map((group) => (
                <ArtistWantedGroup
                  key={group.artist}
                  artist={group.artist}
                  albums={group.items}
                  ytdlpEnabled={ytdlpEnabled}
                  ytdlpMessage={ytdlpMessage}
                  ytdlpStatus={ytdlpStatus}
                />
              ))}
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}
