// Jobs & Cleanup > Playlist Review Rows: the one home for repairing and placing
// playlist tracks that play but need album placement or metadata repair. Both
// actions queue jobs; nothing changes until the job runs.
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { cleanupPlaylistQuality, getPlaylistRows, getPlaylists, placePlaylistQuality } from '../../api/client';
import type { PlaylistEntry, PlaylistMatchedTrack } from '../../api/types';
import { errorMessage } from '../../lib/errors';
import { durationLabel, qualityColor, type Notice } from './playlistShared';

const PLAYLIST_ROW_PAGE_SIZE = 100;

type QualityPlaceDraft = {
  itemId: number;
  artist: string;
  title: string;
  albumartist: string;
  album: string;
  year: string;
  track: string;
  disc: string;
  tracktotal: string;
  disctotal: string;
  mbTrackId: string;
  mbAlbumId: string;
  mbReleaseGroupId: string;
};

function optionalNumber(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function yearDraft(value: number | undefined): string {
  if (!value || value < 1000 || value > 9999) return '';
  return String(value);
}

function qualityPlaceDraftForTrack(track: PlaylistMatchedTrack): QualityPlaceDraft {
  return {
    itemId: track.id,
    artist: track.artist || track.query_artist || '',
    title: track.title || track.query_title || '',
    albumartist: track.albumartist || track.artist || track.query_artist || '',
    album: track.album && track.album !== 'Unknown' ? track.album : '',
    year: yearDraft(track.year),
    track: track.track && track.track > 0 ? String(track.track) : '',
    disc: track.disc && track.disc > 0 ? String(track.disc) : '1',
    tracktotal: '',
    disctotal: '1',
    mbTrackId: '',
    mbAlbumId: '',
    mbReleaseGroupId: '',
  };
}

function reviewRowCount(playlist: PlaylistEntry): number {
  return Math.max(0, Number(playlist.quality_review || 0)) + Math.max(0, Number(playlist.quality_bad || 0));
}

const PLACE_FIELDS: Array<{ field: keyof Omit<QualityPlaceDraft, 'itemId'>; label: string }> = [
  { field: 'artist', label: 'Artist' },
  { field: 'title', label: 'Title' },
  { field: 'albumartist', label: 'Album Artist' },
  { field: 'album', label: 'Album' },
];
const PLACE_NUMBER_FIELDS: Array<{ field: keyof Omit<QualityPlaceDraft, 'itemId'>; label: string }> = [
  { field: 'year', label: 'Year' },
  { field: 'disc', label: 'Disc' },
  { field: 'track', label: 'Track' },
  { field: 'tracktotal', label: 'Track Total' },
  { field: 'disctotal', label: 'Disc Total' },
];
const PLACE_ID_FIELDS: Array<{ field: keyof Omit<QualityPlaceDraft, 'itemId'>; label: string }> = [
  { field: 'mbTrackId', label: 'MB Recording ID' },
  { field: 'mbAlbumId', label: 'MB Release ID' },
  { field: 'mbReleaseGroupId', label: 'MB Release Group ID' },
];

export function PlaylistQualityPanel({
  initialPlaylist = '',
  onStarted,
}: {
  /** A ?playlist= deep link from Playlists: select that playlist. */
  initialPlaylist?: string;
  onStarted?: () => Promise<void> | void;
}) {
  const [playlists, setPlaylists] = useState<PlaylistEntry[]>([]);
  const [loadingPlaylists, setLoadingPlaylists] = useState(true);
  const [playlistError, setPlaylistError] = useState('');
  const [selectedName, setSelectedName] = useState(initialPlaylist);
  const [rows, setRows] = useState<PlaylistMatchedTrack[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingRows, setLoadingRows] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [repairingQuality, setRepairingQuality] = useState(false);
  const [qualityPlaceDraft, setQualityPlaceDraft] = useState<QualityPlaceDraft | null>(null);
  const [placingQuality, setPlacingQuality] = useState(false);

  const loadPlaylists = useCallback(async () => {
    setLoadingPlaylists(true);
    setPlaylistError('');
    try {
      const response = await getPlaylists();
      setPlaylists(response.playlists ?? []);
    } catch (err) {
      setPlaylistError(errorMessage(err));
    } finally {
      setLoadingPlaylists(false);
    }
  }, []);

  useEffect(() => {
    void loadPlaylists();
  }, [loadPlaylists]);

  // With no deep link, start on the first playlist that has review rows.
  useEffect(() => {
    if (selectedName || !playlists.length) return;
    const first = playlists.find((playlist) => reviewRowCount(playlist) > 0);
    if (first) setSelectedName(first.name);
  }, [playlists, selectedName]);

  const loadRows = useCallback(async (name: string, offset: number) => {
    if (!name) return;
    setLoadingRows(true);
    setNotice(null);
    try {
      const result = await getPlaylistRows(name, { group: 'available', offset, limit: PLAYLIST_ROW_PAGE_SIZE });
      const page = (result.rows ?? []) as PlaylistMatchedTrack[];
      setRows((current) => (offset ? [...(current ?? []), ...page] : page));
      setHasMore(Boolean(result.has_more));
    } catch (err) {
      setNotice({ severity: 'error', message: `Rows for ${name} could not be loaded: ${errorMessage(err)}` });
    } finally {
      setLoadingRows(false);
    }
  }, []);

  useEffect(() => {
    setRows(null);
    setQualityPlaceDraft(null);
    if (selectedName) void loadRows(selectedName, 0);
  }, [selectedName, loadRows]);

  const selectedPlaylist = playlists.find((playlist) => playlist.name === selectedName);
  // Same gate as before the move: no repair while this playlist's pipeline runs.
  const downloadRunning = selectedPlaylist?.last_pipeline?.status === 'running';
  const qualityRows = useMemo(
    () => (rows ?? []).filter((track) => track.quality && track.quality !== 'ok'),
    [rows],
  );
  const qualityRepairRows = useMemo(
    () => qualityRows.filter((track) => track.quality !== 'bad' && track.id > 0),
    [qualityRows],
  );

  const handleRepairQualityRows = async () => {
    const itemIds = qualityRepairRows.map((track) => track.id).filter((id) => id > 0);
    if (!itemIds.length) {
      setNotice({ severity: 'info', message: 'No repairable playlist quality rows are selected.' });
      return;
    }
    const ok = window.confirm(
      `Queue playlist quality repair for ${itemIds.length} review row(s)? The job will write tags and move repaired tracks into normal album folders.`,
    );
    if (!ok) return;
    setRepairingQuality(true);
    setNotice(null);
    try {
      const result = await cleanupPlaylistQuality({
        dry_run: false,
        action: 'repair',
        filter: 'repair',
        item_ids: itemIds,
        limit: Math.max(50, itemIds.length),
      });
      if (result.queued && result.job_id) {
        setNotice({
          severity: 'success',
          message: `Queued playlist quality repair for ${itemIds.length.toLocaleString()} row(s). Track progress on Jobs: ${result.job_id}`,
        });
      } else {
        setNotice({
          severity: 'info',
          message: 'No matching repair rows were queued. Refresh the playlist after current jobs finish.',
        });
      }
      await loadPlaylists();
      await onStarted?.();
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setRepairingQuality(false);
    }
  };

  const updateQualityPlaceDraft = (field: keyof Omit<QualityPlaceDraft, 'itemId'>, value: string) => {
    setQualityPlaceDraft((draft) => (draft ? { ...draft, [field]: value } : draft));
  };

  const handleQueueQualityPlace = async () => {
    if (!qualityPlaceDraft) return;
    if (!qualityPlaceDraft.artist.trim() || !qualityPlaceDraft.title.trim() || !qualityPlaceDraft.albumartist.trim() || !qualityPlaceDraft.album.trim()) {
      setNotice({ severity: 'error', message: 'Artist, title, album artist, and album are required to place a review row.' });
      return;
    }
    setPlacingQuality(true);
    setNotice(null);
    try {
      const result = await placePlaylistQuality({
        item_id: qualityPlaceDraft.itemId,
        playlist: selectedName || undefined,
        placement: {
          artist: qualityPlaceDraft.artist.trim(),
          title: qualityPlaceDraft.title.trim(),
          albumartist: qualityPlaceDraft.albumartist.trim(),
          album: qualityPlaceDraft.album.trim(),
          year: optionalNumber(qualityPlaceDraft.year),
          track: optionalNumber(qualityPlaceDraft.track),
          disc: optionalNumber(qualityPlaceDraft.disc),
          tracktotal: optionalNumber(qualityPlaceDraft.tracktotal),
          disctotal: optionalNumber(qualityPlaceDraft.disctotal),
          mb_trackid: qualityPlaceDraft.mbTrackId.trim() || undefined,
          mb_albumid: qualityPlaceDraft.mbAlbumId.trim() || undefined,
          mb_releasegroupid: qualityPlaceDraft.mbReleaseGroupId.trim() || undefined,
        },
      });
      setQualityPlaceDraft(null);
      setNotice({
        severity: 'success',
        message: `Queued manual placement job ${result.job_id}. Track progress on Jobs; the playlist will sync after the file moves.`,
      });
      await loadPlaylists();
      await onStarted?.();
    } catch (err) {
      setNotice({ severity: 'error', message: errorMessage(err) });
    } finally {
      setPlacingQuality(false);
    }
  };

  const draftField = (field: keyof Omit<QualityPlaceDraft, 'itemId'>, label: string) => (
    <TextField
      key={field}
      label={label}
      size="small"
      value={qualityPlaceDraft?.[field] ?? ''}
      onChange={(event) => updateQualityPlaceDraft(field, event.target.value)}
    />
  );

  return (
    <div className="min-w-0 space-y-4">
      <p className="max-w-prose text-sm text-muted">
        Playlist tracks that play but need album placement or metadata repair. Repair and placement run as jobs and never delete playlist audio.
      </p>
      <label className="flex max-w-md flex-col gap-1 text-sm text-zinc-300">
        Playlist
        <select
          className="ops-field w-full min-w-0"
          value={selectedName}
          onChange={(event) => setSelectedName(event.target.value)}
          disabled={loadingPlaylists}
        >
          <option value="">{loadingPlaylists ? 'Loading playlists...' : playlists.length ? 'Pick a saved playlist' : 'No saved playlists yet'}</option>
          {playlists.map((playlist) => {
            const count = reviewRowCount(playlist);
            return (
              <option key={playlist.name} value={playlist.name}>
                {count ? `${playlist.name} (${count.toLocaleString()} to review)` : playlist.name}
              </option>
            );
          })}
        </select>
      </label>
      {playlistError ? (
        <Alert severity="error">Saved playlists could not be loaded: {playlistError}. Use Refresh at the top of the page to try again.</Alert>
      ) : null}
      {notice ? <Alert severity={notice.severity} onClose={() => setNotice(null)}>{notice.message}</Alert> : null}

      {selectedName && rows ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-zinc-300">
              {qualityRows.length
                ? `${qualityRows.length.toLocaleString()} review row(s) in the ${rows.length.toLocaleString()} loaded track(s).`
                : `No review rows in the ${rows.length.toLocaleString()} loaded track(s).`}
            </p>
            <Button
              variant="outlined"
              size="small"
              onClick={() => {
                void handleRepairQualityRows();
              }}
              disabled={repairingQuality || downloadRunning || qualityRepairRows.length === 0}
            >
              {repairingQuality ? 'Queueing...' : `Repair Review Rows (${qualityRepairRows.length})`}
            </Button>
          </div>
          {downloadRunning ? (
            <p className="text-sm text-info">This playlist&apos;s download pipeline is running. Repair is available when it finishes.</p>
          ) : null}
          {qualityRows.length ? (
            <ul className="ops-track-list max-h-[32rem] overflow-auto border-y border-graphite-800">
              {qualityRows.map((track, idx) => (
                <li key={`quality-${idx}:${track.id}:${track.path || track.title}`} className="py-2 text-sm">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1 basis-48">
                      <div className="break-words font-medium text-zinc-200">{track.artist} - {track.title}</div>
                      <div className="break-all text-xs text-muted">{track.album || 'blank album'} · {track.path || 'no path'}</div>
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      <Chip
                        label={track.quality_flags?.length ? track.quality_flags.join(', ') : track.quality}
                        size="small"
                        color={qualityColor(track.quality)}
                        variant="outlined"
                      />
                      {durationLabel(track.length) ? <Chip label={durationLabel(track.length)} size="small" variant="outlined" /> : null}
                      {track.quality !== 'bad' && track.id > 0 ? (
                        <Button
                          variant="outlined"
                          size="small"
                          onClick={() => {
                            setQualityPlaceDraft(qualityPlaceDraftForTrack(track));
                            setNotice(null);
                          }}
                          disabled={placingQuality}
                        >
                          Place
                        </Button>
                      ) : null}
                    </div>
                  </div>
                  {qualityPlaceDraft?.itemId === track.id ? (
                    <div className="mt-3 border-t border-graphite-800 pt-3">
                      <div className="grid gap-2 md:grid-cols-2">{PLACE_FIELDS.map(({ field, label }) => draftField(field, label))}</div>
                      <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">{PLACE_NUMBER_FIELDS.map(({ field, label }) => draftField(field, label))}</div>
                      <div className="mt-2 grid gap-2 md:grid-cols-3">{PLACE_ID_FIELDS.map(({ field, label }) => draftField(field, label))}</div>
                      <div className="mt-3 flex flex-wrap justify-end gap-2">
                        <Button variant="outlined" size="small" onClick={() => setQualityPlaceDraft(null)} disabled={placingQuality}>
                          Cancel
                        </Button>
                        <Button
                          variant="contained"
                          size="small"
                          onClick={() => {
                            void handleQueueQualityPlace();
                          }}
                          disabled={placingQuality}
                        >
                          {placingQuality ? 'Queueing...' : 'Queue Placement'}
                        </Button>
                      </div>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
          {hasMore ? (
            <Button variant="outlined" size="small" disabled={loadingRows} onClick={() => void loadRows(selectedName, rows.length)}>
              {loadingRows ? 'Loading Rows...' : 'Load More Rows'}
            </Button>
          ) : null}
        </div>
      ) : selectedName && loadingRows ? (
        <p className="text-sm text-muted">Loading tracks...</p>
      ) : null}
    </div>
  );
}
