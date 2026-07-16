import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getAlbumTracks, getLibraryArtRepairReport } from '../api/client';
import type { ArtRepairItem, ArtRepairReportResponse } from '../api/types';
import { artistNeedsAttention, getAlbumHealth } from '../lib/libraryHealth';
import { apiGet } from '../lib/api';
import type { LibraryAlbum, LibraryArtist, LibraryResponse, LibraryTrack } from '../types/api';

type StatusFilter = 'all' | 'ids' | 'missing' | 'extra' | 'art' | 'clean';
type SearchScope = 'all' | 'artists' | 'albums' | 'tracks';
type ArtistSort = 'alpha' | 'albums' | 'tracks' | 'attention';
type AlbumSort = 'artist' | 'album' | 'year' | 'issues';
type ArtistLetter = 'all' | 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H' | 'I' | 'J' | 'K' | 'L' | 'M' | 'N' | 'O' | 'P' | 'Q' | 'R' | 'S' | 'T' | 'U' | 'V' | 'W' | 'X' | 'Y' | 'Z' | '#';

type AlbumRow = {
  album: LibraryAlbum;
  artist: LibraryArtist;
};

const numberFmt = new Intl.NumberFormat();
const ARTIST_LETTERS: ArtistLetter[] = ['all', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', '#'];

const statusFilters: Array<{
  id: StatusFilter;
  label: string;
  detail: string;
}> = [
  { id: 'all', label: 'All', detail: 'Artists' },
  { id: 'ids', label: 'Needs IDs', detail: 'MusicBrainz' },
  { id: 'missing', label: 'Missing / Not Imported', detail: 'Import gaps' },
  { id: 'extra', label: 'Extra Tracks', detail: 'Review rows' },
  { id: 'art', label: 'Art Repair', detail: 'Artwork' },
  { id: 'clean', label: 'Clean', detail: 'No issues' },
];

const artistSortOptions: Array<{ value: ArtistSort; label: string }> = [
  { value: 'alpha', label: 'Alphabetical' },
  { value: 'albums', label: 'Most albums' },
  { value: 'tracks', label: 'Most tracks' },
  { value: 'attention', label: 'Needs attention first' },
];

const albumSortOptions: Array<{ value: AlbumSort; label: string }> = [
  { value: 'artist', label: 'Artist' },
  { value: 'album', label: 'Album title' },
  { value: 'year', label: 'Year' },
  { value: 'issues', label: 'Most issues' },
];

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(' ');
}

const stampSuffixRe = /\s*(?:\([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\)|\{(?:Album MbId|Track ArtistMbId|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12},?)\})\s*$/i;

function stripStamps(value: string | undefined | null) {
  let next = (value ?? '').trim();
  let previous = '';
  while (next && next !== previous) {
    previous = next;
    next = next.replace(stampSuffixRe, '').trim();
  }
  return next;
}

function displayArtistName(artist: LibraryArtist | string | undefined | null) {
  return stripStamps(typeof artist === 'string' ? artist : artist?.name) || 'Unknown artist';
}

function displayAlbumTitle(album: LibraryAlbum | string | undefined | null) {
  return stripStamps(typeof album === 'string' ? album : album?.album) || 'Untitled album';
}

function normalized(value: string | undefined | null) {
  return stripStamps(value).toLowerCase();
}

function countTracks(artist: LibraryArtist) {
  if (typeof artist.total === 'number') return artist.total;
  return artist.albums.reduce((sum, album) => sum + (album.tracks?.length ?? album.track_count ?? 0), 0);
}

function albumTrackCount(album: LibraryAlbum) {
  return Number(album.track_count || album.tracks?.length || 0);
}

function countAttentionAlbums(artist: LibraryArtist) {
  return artist.albums.filter((album) => getAlbumHealth(album).needsAttention).length;
}

function albumHasLocalArt(album: LibraryAlbum) {
  return Boolean(album.artpath || album.disk_art || album.image_url);
}

function addVersion(url: string, version?: number) {
  if (!url || !version) return url;
  return `${url}${url.includes('?') ? '&' : '?'}v=${encodeURIComponent(String(version))}`;
}

function albumArtUrl(album: LibraryAlbum, libraryVersion?: number) {
  if (album.image_url) return addVersion(album.image_url, libraryVersion);
  if (album.album_id) return addVersion(`/api/albums/${album.album_id}/art`, libraryVersion);
  const diskArt = album.disk_art || album.artpath || '';
  if (!diskArt) return '';
  return addVersion(`/api/disk-art?path=${encodeURIComponent(diskArt)}`, libraryVersion);
}

function artistArtUrl(artist: LibraryArtist, libraryVersion?: number) {
  const direct = artist.artist_image_url || artist.image_url || '';
  if (direct) return addVersion(direct, libraryVersion);
  for (const album of artist.albums) {
    const url = albumArtUrl(album, libraryVersion);
    if (url) return url;
  }
  return '';
}

function initials(name: string) {
  return name
    .replace(/^the\s+/i, '')
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('');
}

function Artwork({
  src,
  label,
  fallback,
  className,
}: {
  src: string;
  label: string;
  fallback: string;
  className: string;
}) {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [src]);

  return (
    <div className={cx('flex shrink-0 items-center justify-center overflow-hidden bg-graphite-900 text-xs font-semibold text-zinc-300', className)}>
      {src && !failed ? (
        <img
          alt={label}
          className="h-full w-full object-cover"
          loading="lazy"
          src={src}
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="max-w-full truncate px-2 text-center">{fallback}</span>
      )}
    </div>
  );
}

function artistLetter(artist: LibraryArtist): ArtistLetter {
  const first = displayArtistName(artist).trim()[0]?.toUpperCase() ?? '#';
  return /^[A-Z]$/.test(first) ? (first as ArtistLetter) : '#';
}

function trackMatchesQuery(track: LibraryTrack, needle: string) {
  return (
    normalized(track.title).includes(needle) ||
    String(track.path ?? '').toLowerCase().includes(needle) ||
    String(track.mb_trackid ?? '').toLowerCase().includes(needle)
  );
}

function albumMatchesQuery(album: LibraryAlbum, artist: LibraryArtist, query: string, scope: SearchScope) {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;

  const artistMatch = scope === 'all' || scope === 'artists'
    ? normalized(artist.name).includes(needle) || normalized(album.albumartist).includes(needle)
    : false;
  const albumMatch = scope === 'all' || scope === 'albums'
    ? normalized(album.album).includes(needle) || String(album.year || '').includes(needle)
    : false;
  const trackMatch = scope === 'all' || scope === 'tracks'
    ? (album.tracks ?? []).some((track) => trackMatchesQuery(track, needle))
    : false;

  return artistMatch || albumMatch || trackMatch;
}

function artistMatchesQuery(artist: LibraryArtist, query: string, scope: SearchScope) {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  if ((scope === 'all' || scope === 'artists') && normalized(artist.name).includes(needle)) return true;
  return artist.albums.some((album) => albumMatchesQuery(album, artist, query, scope));
}

function albumIssueScore(album: LibraryAlbum, artRepairAlbumIds: Set<number>) {
  const health = getAlbumHealth(album);
  let score = 0;
  if (health.releaseMbMissing) score += 3;
  score += health.trackMbMissing + health.trackMbMismatched + health.duplicateRecordingIds + health.mbRepairable;
  score += health.missing * 2;
  score += health.notImported * 2;
  score += health.extra;
  if (artRepairAlbumIds.has(Number(album.album_id || 0))) score += 1;
  return score;
}

function albumHasIdIssue(album: LibraryAlbum) {
  const health = getAlbumHealth(album);
  return health.releaseMbMissing || health.trackMbMissing > 0 || health.trackMbMismatched > 0 || health.mbRepairable > 0 || health.duplicateRecordingIds > 0;
}

function albumMatchesStatus(album: LibraryAlbum, status: StatusFilter, artRepairAlbumIds: Set<number>) {
  const health = getAlbumHealth(album);
  const artIssue = artRepairAlbumIds.has(Number(album.album_id || 0));

  if (status === 'all') return true;
  if (status === 'ids') return albumHasIdIssue(album);
  if (status === 'missing') return health.missing > 0 || health.notImported > 0;
  if (status === 'extra') return health.extra > 0;
  if (status === 'art') return artIssue;
  if (status === 'clean') return !health.needsAttention && !artIssue;
  return true;
}

function albumNeedsAttention(album: LibraryAlbum, artRepairAlbumIds: Set<number>) {
  return getAlbumHealth(album).needsAttention || artRepairAlbumIds.has(Number(album.album_id || 0));
}

function albumSuggestedStatus(album: LibraryAlbum, artRepairAlbumIds: Set<number>): StatusFilter {
  const health = getAlbumHealth(album);
  if (albumHasIdIssue(album)) return 'ids';
  if (health.missing > 0 || (health.notImported > 0 && !album.not_imported_is_extra)) return 'missing';
  if (health.extra > 0 || (health.notImported > 0 && album.not_imported_is_extra)) return 'extra';
  if (artRepairAlbumIds.has(Number(album.album_id || 0))) return 'art';
  return 'all';
}

function compareRepairRows(a: AlbumRow, b: AlbumRow, artRepairAlbumIds: Set<number>) {
  return albumIssueScore(b.album, artRepairAlbumIds) - albumIssueScore(a.album, artRepairAlbumIds)
    || displayArtistName(a.artist).localeCompare(displayArtistName(b.artist))
    || displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album));
}

function albumPrimaryIssue(album: LibraryAlbum, artRepairAlbumIds: Set<number>) {
  const health = getAlbumHealth(album);
  if (health.releaseMbMissing) return 'Missing album MusicBrainz ID';
  if (health.trackMbMissing > 0) return `${health.trackMbMissing} track ID(s) missing`;
  if (health.trackMbMismatched > 0) return `${health.trackMbMismatched} recording ID mismatch(es)`;
  if (health.mbRepairable > 0) return `${health.mbRepairable} recording ID repair candidate(s)`;
  if (health.missing > 0) return `${health.missing} missing file(s)`;
  if (health.notImported > 0) return `${health.notImported} not imported`;
  if (health.extra > 0) return `${health.extra} extra track row(s)`;
  if (artRepairAlbumIds.has(Number(album.album_id || 0))) return 'Artwork needs review';
  return 'Clean';
}

type AlbumAction = {
  label: string;
  path: string;
  title?: string;
  variant: 'primary' | 'warning';
};

function albumPrimaryAction(album: LibraryAlbum, artRepairItemMap: Map<number, ArtRepairItem>): AlbumAction | null {
  const health = getAlbumHealth(album);
  if (album.pending_review) return {
    label: 'In review queue',
    path: '/import?tab=review',
    title: 'Already queued in Import Review — check there for next steps',
    variant: 'primary',
  };
  if (health.releaseMbMissing) return {
    label: 'Identify in Import',
    path: '/import?tab=review&filter=library_no_mb',
    title: 'No MusicBrainz release ID — assign one in Import Review',
    variant: 'primary',
  };
  if (health.missing > 0) return {
    label: 'Acquire missing',
    path: '/import?tab=acquire',
    title: `${health.missing} track file(s) missing from disk — download replacements in Acquire`,
    variant: 'warning',
  };
  if (health.notImported > 0) {
    if (album.not_imported_is_extra) return {
      label: 'Track Cleanup',
      path: '/clean?tab=album-tracks',
      title: `${health.notImported} extra file(s) on disk beyond expected tracklist — use Album Track Cleanup`,
      variant: 'warning',
    };
    return {
      label: 'Import files',
      path: '/import?tab=review',
      title: `${health.notImported} file(s) on disk not yet imported into Beets`,
      variant: 'warning',
    };
  }
  if (health.duplicateRecordingIds > 0) return {
    label: 'Merge split album',
    path: '/clean?tab=library-db',
    title: `${health.duplicateRecordingIds} duplicate recording ID row(s) — possible split album in Library DB`,
    variant: 'warning',
  };
  if (health.mbRepairable > 0 || health.trackMbMissing > 0 || health.trackMbMismatched > 0) return {
    label: 'Repair recording IDs',
    path: '/jobs',
    title: `Recording IDs can be repaired — run MB Full Sync or MusicBrainz Repair in Jobs`,
    variant: 'primary',
  };
  const artItem = artRepairItemMap.get(Number(album.album_id || 0));
  if (artItem) return {
    label: artItem.actionable ? 'Fetch art' : 'Fix art',
    path: '/jobs',
    title: artItem.reason || 'Artwork needs repair — run Fetch Missing Art in Jobs',
    variant: 'warning',
  };
  return null;
}

function albumIssueDetails(album: LibraryAlbum, artRepairAlbumIds: Set<number>) {
  const health = getAlbumHealth(album);
  const details: string[] = [];
  if (album.tracks_deferred) details.push('Track rows are deferred in the fast library payload.');
  if (album.pending_review) details.push('Already queued in Import Review — awaiting manual decision.');
  if (health.releaseMbMissing) details.push('Album has no MusicBrainz release ID.');
  if (health.trackMbMissing > 0) details.push(`${health.trackMbMissing} imported track row(s) have no MusicBrainz recording ID.`);
  if (health.trackMbMismatched > 0) details.push(`${health.trackMbMismatched} imported track row(s) have a recording ID mismatch.`);
  if (health.duplicateRecordingIds > 0) details.push(`${health.duplicateRecordingIds} duplicate recording ID row(s) — possible split album.`);
  if (health.missing > 0) details.push(`${health.missing} Beets row(s) point to missing files.`);
  if (health.notImported > 0) {
    if (album.not_imported_is_extra) {
      details.push(`${health.notImported} extra file(s) on disk beyond the expected MB tracklist — use Album Track Cleanup.`);
    } else {
      details.push(`${health.notImported} file(s) are on disk but not yet imported into Beets.`);
    }
  }
  if (health.extra > 0) details.push(`${health.extra} local track row(s) are beyond the expected tracklist.`);
  if (artRepairAlbumIds.has(Number(album.album_id || 0))) details.push('Artwork appears in the art repair report.');
  if (!details.length) details.push('No passive issue evidence in the current library summary.');
  return details;
}

function statusChipColor(status: StatusFilter) {
  if (status === 'clean') return 'success' as const;
  if (status === 'missing' || status === 'extra' || status === 'art') return 'warning' as const;
  if (status === 'ids') return 'secondary' as const;
  return 'default' as const;
}

function AlbumBadges({
  album,
  artRepairAlbumIds,
  artRepairItemMap,
}: {
  album: LibraryAlbum;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap?: Map<number, ArtRepairItem>;
}) {
  const health = getAlbumHealth(album);
  const albumId = Number(album.album_id || 0);
  const artItem = artRepairItemMap?.get(albumId);
  const artIssue = artRepairAlbumIds.has(albumId);
  const recordingIdsOk = !health.releaseMbMissing && health.trackMbMissing === 0 && health.trackMbMismatched === 0 && health.duplicateRecordingIds === 0 && health.mbRepairable === 0;

  const artLabel = artItem
    ? artItem.issue === 'missing' ? 'Art missing'
      : artItem.issue === 'broken' ? 'Art broken'
      : 'Art unresolved'
    : 'Art repair';

  return (
    <div className="flex flex-wrap gap-1.5">
      {album.pending_review ? (
        <Chip color="info" label="In review queue" size="small" variant="outlined" />
      ) : null}
      {albumHasIdIssue(album) ? (
        <Chip color="secondary" label="Needs IDs" size="small" variant="outlined" />
      ) : null}
      {health.missing > 0 ? (
        <Chip color="error" label={`${health.missing} missing`} size="small" variant="outlined" />
      ) : null}
      {health.notImported > 0 ? (
        album.not_imported_is_extra
          ? <Chip color="warning" label={`${health.notImported} extra disk files`} size="small" variant="outlined" />
          : <Chip color="warning" label={`${health.notImported} not imported`} size="small" variant="outlined" />
      ) : null}
      {health.extra > 0 ? (
        <Chip color="warning" label={`${health.extra} extra rows`} size="small" variant="outlined" />
      ) : null}
      {health.duplicateRecordingIds > 0 ? (
        <Chip color="warning" label={`${health.duplicateRecordingIds} dup recording IDs`} size="small" variant="outlined" />
      ) : null}
      {artIssue ? (
        <Chip color="warning" label={artLabel} size="small" variant="outlined" />
      ) : albumHasLocalArt(album) ? (
        <Chip color="success" label="Art local" size="small" variant="outlined" />
      ) : null}
      {recordingIdsOk && !album.pending_review ? (
        <Chip color="success" label="Recording IDs OK" size="small" variant="outlined" />
      ) : null}
    </div>
  );
}

function NextRepairPanel({
  row,
  artRepairAlbumIds,
  artRepairItemMap,
  libraryVersion,
  onDetails,
  onFocusStatus,
  onNavigate,
}: {
  row: AlbumRow | null;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  onDetails: (row: AlbumRow) => void;
  onFocusStatus: (status: StatusFilter) => void;
  onNavigate: (path: string) => void;
}) {
  if (!row) return null;

  const { album, artist } = row;
  const title = displayAlbumTitle(album);
  const artistName = displayArtistName(artist);
  const action = albumPrimaryAction(album, artRepairItemMap);
  const focusStatus = albumSuggestedStatus(album, artRepairAlbumIds);

  return (
    <section className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 gap-3">
          <Artwork
            className="h-16 w-16 rounded-md"
            fallback="No cover"
            label={`${title} cover`}
            src={albumArtUrl(album, libraryVersion)}
          />
          <div className="min-w-0">
            <div className="text-xs font-semibold uppercase text-amber-300">Next repair</div>
            <h2 className="mt-1 line-clamp-1 text-base font-semibold text-zinc-100">{title}</h2>
            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-xs text-zinc-400">
              <span>{artistName}</span>
              {album.year ? <span>{String(album.year).slice(0, 4)}</span> : null}
              <span>{albumPrimaryIssue(album, artRepairAlbumIds)}</span>
            </div>
            <div className="mt-2">
              <AlbumBadges album={album} artRepairAlbumIds={artRepairAlbumIds} artRepairItemMap={artRepairItemMap} />
            </div>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button size="small" variant="outlined" onClick={() => onDetails(row)}>
            View details
          </Button>
          {action ? (
            <Button
              size="small"
              variant="contained"
              color={action.variant === 'warning' ? 'warning' : 'primary'}
              title={action.title}
              onClick={() => onNavigate(action.path)}
            >
              {action.label}
            </Button>
          ) : null}
          {focusStatus !== 'all' ? (
            <Button size="small" variant="text" color="warning" onClick={() => onFocusStatus(focusStatus)}>
              Show issue group
            </Button>
          ) : null}
        </div>
      </div>
    </section>
  );
}
function StatusCards({
  active,
  counts,
  onChange,
}: {
  active: StatusFilter;
  counts: Record<StatusFilter, number>;
  onChange: (status: StatusFilter) => void;
}) {
  return (
    <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
      {statusFilters.map((filter) => {
        const selected = active === filter.id;
        return (
          <button
            key={filter.id}
            className={cx(
              'min-h-[4.75rem] rounded-md border px-3 py-2 text-left transition-colors',
              selected
                ? 'border-red-400 bg-red-500/10 text-red-100'
                : 'border-graphite-800 bg-graphite-950/35 text-zinc-300 hover:border-graphite-600 hover:bg-graphite-900/60',
            )}
            type="button"
            onClick={() => onChange(filter.id)}
          >
            <span className="block text-[0.68rem] font-semibold uppercase text-zinc-500">{filter.detail}</span>
            <span className="mt-1 block text-sm font-semibold">{filter.label}</span>
            <span className="mt-1 block text-lg font-semibold tabular-nums text-zinc-100">{numberFmt.format(counts[filter.id] ?? 0)}</span>
          </button>
        );
      })}
    </div>
  );
}

function SearchRow({
  query,
  scope,
  artistSort,
  albumSort,
  albumMode,
  trackSearchLoading,
  onQueryChange,
  onScopeChange,
  onArtistSortChange,
  onAlbumSortChange,
}: {
  query: string;
  scope: SearchScope;
  artistSort: ArtistSort;
  albumSort: AlbumSort;
  albumMode: boolean;
  trackSearchLoading: boolean;
  onQueryChange: (value: string) => void;
  onScopeChange: (value: SearchScope) => void;
  onArtistSortChange: (value: ArtistSort) => void;
  onAlbumSortChange: (value: AlbumSort) => void;
}) {
  return (
    <div className="grid gap-2 rounded-md border border-graphite-800 bg-graphite-950/35 p-3 lg:grid-cols-[minmax(0,1fr)_11rem_13rem]">
      <TextField
        label="Search"
        placeholder="Artists, albums, tracks"
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
      />
      <TextField
        label="Scope"
        select
        value={scope}
        onChange={(event) => onScopeChange(event.target.value as SearchScope)}
      >
        <MenuItem value="all">All</MenuItem>
        <MenuItem value="artists">Artists</MenuItem>
        <MenuItem value="albums">Albums</MenuItem>
        <MenuItem value="tracks">Tracks</MenuItem>
      </TextField>
      <TextField
        label="Sort"
        select
        value={albumMode ? albumSort : artistSort}
        onChange={(event) => {
          if (albumMode) onAlbumSortChange(event.target.value as AlbumSort);
          else onArtistSortChange(event.target.value as ArtistSort);
        }}
      >
        {(albumMode ? albumSortOptions : artistSortOptions).map((option) => (
          <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>
        ))}
      </TextField>
      {trackSearchLoading ? (
        <div className="lg:col-span-3">
          <LinearProgress sx={{ borderRadius: 1 }} />
          <div className="mt-1 text-xs text-zinc-500">Loading track rows for track search...</div>
        </div>
      ) : null}
    </div>
  );
}

function AlphabetIndex({
  active,
  available,
  onChange,
}: {
  active: ArtistLetter;
  available: Set<ArtistLetter>;
  onChange: (letter: ArtistLetter) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1 rounded-md border border-graphite-800 bg-graphite-950/35 p-2">
      {ARTIST_LETTERS.map((letter) => {
        const enabled = letter === 'all' || available.has(letter);
        const selected = active === letter;
        return (
          <button
            key={letter}
            className={cx(
              'h-8 min-w-8 rounded border px-2 text-xs font-semibold transition',
              selected
                ? 'border-red-400 bg-red-500/15 text-red-100'
                : enabled
                  ? 'border-graphite-700 text-zinc-300 hover:border-graphite-500 hover:bg-graphite-900'
                  : 'cursor-not-allowed border-graphite-900 text-zinc-700',
            )}
            disabled={!enabled}
            type="button"
            onClick={() => onChange(letter)}
          >
            {letter === 'all' ? 'All' : letter}
          </button>
        );
      })}
    </div>
  );
}

function ArtistCard({
  artist,
  libraryVersion,
  onSelect,
}: {
  artist: LibraryArtist;
  libraryVersion?: number;
  onSelect: (artist: LibraryArtist) => void;
}) {
  const name = displayArtistName(artist);
  const needsAttention = artistNeedsAttention(artist);
  const attentionAlbums = countAttentionAlbums(artist);

  return (
    <button
      className="grid min-h-[5.5rem] grid-cols-[3.5rem_minmax(0,1fr)] items-center gap-3 rounded-md border border-graphite-800 bg-graphite-950/35 p-2 text-left transition hover:border-red-400/70 hover:bg-graphite-900/70"
      type="button"
      onClick={() => onSelect(artist)}
    >
      <Artwork
        className="h-14 w-14 rounded-md"
        fallback={initials(name) || 'A'}
        label={`${name} artwork`}
        src={artistArtUrl(artist, libraryVersion)}
      />
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold text-zinc-100">{name}</span>
        <span className="mt-1 block text-xs text-zinc-400">
          {numberFmt.format(artist.albums.length)} albums - {numberFmt.format(countTracks(artist))} tracks
        </span>
        <span className="mt-1 flex flex-wrap gap-1.5">
          {needsAttention ? (
            <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[0.68rem] font-medium text-amber-200">
              {attentionAlbums ? `${attentionAlbums} need review` : 'Needs attention'}
            </span>
          ) : (
            <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[0.68rem] font-medium text-emerald-200">Clean</span>
          )}
          {artist.empty_artist_folder ? (
            <span className="rounded bg-sky-500/10 px-1.5 py-0.5 text-[0.68rem] font-medium text-sky-200">Empty folder</span>
          ) : null}
        </span>
      </span>
    </button>
  );
}

function ArtistBrowser({
  artists,
  letter,
  libraryVersion,
  loading,
  onLetterChange,
  onSelectArtist,
}: {
  artists: LibraryArtist[];
  letter: ArtistLetter;
  libraryVersion?: number;
  loading: boolean;
  onLetterChange: (letter: ArtistLetter) => void;
  onSelectArtist: (artist: LibraryArtist) => void;
}) {
  const availableLetters = useMemo(() => new Set(artists.map(artistLetter)), [artists]);
  const visibleArtists = letter === 'all' ? artists : artists.filter((artist) => artistLetter(artist) === letter);

  return (
    <section className="space-y-3">
      <AlphabetIndex active={letter} available={availableLetters} onChange={onLetterChange} />
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-zinc-400">
          {letter === 'all' ? 'Artists' : `Artists starting with ${letter}`} - {numberFmt.format(visibleArtists.length)}
        </div>
      </div>
      <div className="max-h-[calc(100vh-21rem)] min-h-72 overflow-y-auto rounded-md border border-graphite-800 bg-graphite-950/20 p-2">
        {loading ? (
          <div className="p-4 text-sm text-zinc-400">Loading library...</div>
        ) : visibleArtists.length ? (
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {visibleArtists.map((artist, index) => (
              <ArtistCard
                key={`${artist.name}::${artist.path ?? index}`}
                artist={artist}
                libraryVersion={libraryVersion}
                onSelect={onSelectArtist}
              />
            ))}
          </div>
        ) : (
          <div className="p-4 text-sm text-zinc-400">No matching artists.</div>
        )}
      </div>
    </section>
  );
}

function AlbumCard({
  row,
  artRepairAlbumIds,
  artRepairItemMap,
  libraryVersion,
  showArtist,
  onDetails,
  onNavigate,
}: {
  row: AlbumRow;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  showArtist: boolean;
  onDetails: (row: AlbumRow) => void;
  onNavigate: (path: string) => void;
}) {
  const { album, artist } = row;
  const title = displayAlbumTitle(album);
  const artistName = displayArtistName(artist);
  const issue = albumPrimaryIssue(album, artRepairAlbumIds);
  const action = albumPrimaryAction(album, artRepairItemMap);

  return (
    <article className="grid min-h-[8.5rem] grid-cols-[5rem_minmax(0,1fr)] gap-3 rounded-md border border-graphite-800 bg-graphite-950/35 p-2">
      <Artwork
        className="h-20 w-20 rounded-md"
        fallback="No cover"
        label={`${title} cover`}
        src={albumArtUrl(album, libraryVersion)}
      />
      <div className="min-w-0">
        <div className="flex min-w-0 items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="line-clamp-2 text-sm font-semibold text-zinc-100">{title}</h3>
            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-xs text-zinc-400">
              {showArtist ? <span className="truncate">{artistName}</span> : null}
              {album.year ? <span>{String(album.year).slice(0, 4)}</span> : null}
              <span>{numberFmt.format(albumTrackCount(album))} tracks</span>
            </div>
          </div>
          <Chip label={issue} size="small" variant="outlined" />
        </div>
        <div className="mt-2">
          <AlbumBadges album={album} artRepairAlbumIds={artRepairAlbumIds} artRepairItemMap={artRepairItemMap} />
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="small" variant="outlined" onClick={() => onDetails(row)}>
            View details
          </Button>
          {action ? (
            album.pending_review ? (
              <Button
                size="small"
                variant="text"
                color="info"
                title={action.title}
                onClick={() => onNavigate(action.path)}
              >
                {action.label}
              </Button>
            ) : (
              <Button
                size="small"
                variant="text"
                color={action.variant === 'warning' ? 'warning' : 'primary'}
                title={action.title}
                onClick={() => onNavigate(action.path)}
              >
                {action.label}
              </Button>
            )
          ) : null}
        </div>
      </div>
    </article>
  );
}

function AlbumGrid({
  rows,
  title,
  subtitle,
  artRepairAlbumIds,
  artRepairItemMap,
  libraryVersion,
  showArtist,
  onDetails,
  onNavigate,
}: {
  rows: AlbumRow[];
  title: string;
  subtitle: string;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  showArtist: boolean;
  onDetails: (row: AlbumRow) => void;
  onNavigate: (path: string) => void;
}) {
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-zinc-100">{title}</h2>
          <p className="mt-0.5 text-sm text-zinc-400">{subtitle}</p>
        </div>
      </div>
      <div className="max-h-[calc(100vh-22rem)] min-h-72 overflow-y-auto rounded-md border border-graphite-800 bg-graphite-950/20 p-2">
        {rows.length ? (
          <div className="grid gap-2 xl:grid-cols-2 2xl:grid-cols-3">
            {rows.map((row) => (
              <AlbumCard
                key={`${row.artist.name}::${row.album.album_id || row.album.aldir || row.album.album}`}
                row={row}
                artRepairAlbumIds={artRepairAlbumIds}
                artRepairItemMap={artRepairItemMap}
                libraryVersion={libraryVersion}
                showArtist={showArtist}
                onDetails={onDetails}
                onNavigate={onNavigate}
              />
            ))}
          </div>
        ) : (
          <div className="p-4 text-sm text-zinc-400">No matching albums.</div>
        )}
      </div>
    </section>
  );
}

function ArtistDetail({
  artist,
  rows,
  artRepairAlbumIds,
  artRepairItemMap,
  libraryVersion,
  onBack,
  onDetails,
  onNavigate,
}: {
  artist: LibraryArtist;
  rows: AlbumRow[];
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  onBack: () => void;
  onDetails: (row: AlbumRow) => void;
  onNavigate: (path: string) => void;
}) {
  const name = displayArtistName(artist);
  const attentionAlbums = countAttentionAlbums(artist);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm text-zinc-400">
        <button className="text-red-300 hover:text-red-200" type="button" onClick={onBack}>Library</button>
        <span>/</span>
        <button className="text-red-300 hover:text-red-200" type="button" onClick={onBack}>Artists</button>
        <span>/</span>
        <span className="text-zinc-200">{name}</span>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-graphite-800 bg-graphite-950/35 p-3 sm:flex-row sm:items-center">
        <Artwork
          className="h-20 w-20 rounded-md"
          fallback={initials(name) || 'A'}
          label={`${name} artwork`}
          src={artistArtUrl(artist, libraryVersion)}
        />
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-xl font-semibold text-zinc-100">{name}</h2>
          <div className="mt-2 flex flex-wrap gap-2">
            <Chip label={`${numberFmt.format(artist.albums.length)} albums`} size="small" variant="outlined" />
            <Chip label={`${numberFmt.format(countTracks(artist))} tracks`} size="small" variant="outlined" />
            {artistNeedsAttention(artist) ? (
              <Chip color="warning" label={attentionAlbums ? `${attentionAlbums} need review` : 'Needs attention'} size="small" variant="outlined" />
            ) : (
              <Chip color="success" label="Clean" size="small" variant="outlined" />
            )}
            {artist.empty_artist_folder ? <Chip color="info" label="Empty folder" size="small" variant="outlined" /> : null}
          </div>
        </div>
      </div>

      <AlbumGrid
        rows={rows}
        title="Albums"
        subtitle={`${numberFmt.format(rows.length)} visible for ${name}`}
        artRepairAlbumIds={artRepairAlbumIds}
        artRepairItemMap={artRepairItemMap}
        libraryVersion={libraryVersion}
        showArtist={false}
        onDetails={onDetails}
        onNavigate={onNavigate}
      />
    </section>
  );
}

function trackStatus(track: LibraryTrack) {
  if (track.missing || track.status === 'missing_file') return { label: 'Missing file', color: 'text-rose-300' };
  if (track.status === 'not_imported' || track.disk_only) return { label: 'Not imported', color: 'text-amber-300' };
  if (track.status === 'other_album') return { label: 'Other album', color: 'text-amber-300' };
  if (!String(track.mb_trackid ?? '').trim()) return { label: 'No recording ID', color: 'text-violet-300' };
  return { label: 'Imported', color: 'text-emerald-300' };
}

function AlbumDetailsDialog({
  row,
  tracks,
  loading,
  error,
  artRepairAlbumIds,
  artRepairItemMap,
  libraryVersion,
  onClose,
}: {
  row: AlbumRow | null;
  tracks: LibraryTrack[];
  loading: boolean;
  error: string;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  onClose: () => void;
}) {
  const album = row?.album ?? null;
  const artist = row?.artist ?? null;
  const title = album ? displayAlbumTitle(album) : '';
  const artistName = artist ? displayArtistName(artist) : '';

  return (
    <Dialog className="relative z-50" open={Boolean(row)} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/60" />
      <div className="fixed inset-0 overflow-y-auto p-4">
        <div className="flex min-h-full items-center justify-center">
          <DialogPanel className="w-full max-w-3xl rounded-md border border-graphite-700 bg-graphite-950 p-4 shadow-xl">
            {album && artist ? (
              <div className="space-y-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 gap-3">
                    <Artwork
                      className="h-20 w-20 rounded-md"
                      fallback="No cover"
                      label={`${title} cover`}
                      src={albumArtUrl(album, libraryVersion)}
                    />
                    <div className="min-w-0">
                      <DialogTitle className="line-clamp-2 text-lg font-semibold text-zinc-100">{title}</DialogTitle>
                      <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 text-sm text-zinc-400">
                        <span>{artistName}</span>
                        {album.year ? <span>{String(album.year).slice(0, 4)}</span> : null}
                        <span>{numberFmt.format(albumTrackCount(album))} tracks</span>
                      </div>
                      <div className="mt-2">
                        <AlbumBadges album={album} artRepairAlbumIds={artRepairAlbumIds} artRepairItemMap={artRepairItemMap} />
                      </div>
                    </div>
                  </div>
                  <Button size="small" variant="outlined" onClick={onClose}>Close</Button>
                </div>

                <div className="rounded border border-graphite-800 bg-graphite-900/50 p-3">
                  <h3 className="text-sm font-semibold text-zinc-200">Review information</h3>
                  <ul className="mt-2 space-y-1 text-sm text-zinc-400">
                    {albumIssueDetails(album, artRepairAlbumIds).map((detail) => (
                      <li key={detail}>{detail}</li>
                    ))}
                  </ul>
                </div>

                <div className="rounded border border-graphite-800 bg-graphite-900/50">
                  <div className="flex items-center justify-between border-b border-graphite-800 px-3 py-2">
                    <h3 className="text-sm font-semibold text-zinc-200">Tracks</h3>
                    <span className="text-xs text-zinc-500">{loading ? 'Loading...' : `${tracks.length} rows`}</span>
                  </div>
                  {loading ? <LinearProgress /> : null}
                  {error ? <div className="p-3 text-sm text-rose-300">{error}</div> : null}
                  {!loading && !error ? (
                    tracks.length ? (
                      <div className="max-h-80 overflow-y-auto">
                        {tracks.map((track, index) => {
                          const status = trackStatus(track);
                          return (
                            <div key={`${track.id || index}-${track.title}`} className="grid grid-cols-[3rem_minmax(0,1fr)_8rem] gap-2 border-t border-graphite-800 px-3 py-2 text-sm">
                              <span className="font-mono text-xs text-zinc-500">{track.disc && track.disc > 1 ? `${track.disc}.` : ''}{track.track || index + 1}</span>
                              <span className="min-w-0 truncate text-zinc-200">{stripStamps(track.title) || 'Untitled track'}</span>
                              <span className={cx('truncate text-right text-xs', status.color)}>{status.label}</span>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="p-3 text-sm text-zinc-500">No track rows loaded for this album.</div>
                    )
                  ) : null}
                </div>
              </div>
            ) : null}
          </DialogPanel>
        </div>
      </div>
    </Dialog>
  );
}

export default function Library() {
  const navigate = useNavigate();
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<SearchScope>('all');
  const [artistSort, setArtistSort] = useState<ArtistSort>('alpha');
  const [albumSort, setAlbumSort] = useState<AlbumSort>('artist');
  const [activeStatus, setActiveStatus] = useState<StatusFilter>('all');
  const [artistLetterFilter, setArtistLetterFilter] = useState<ArtistLetter>('all');
  const [selectedArtistName, setSelectedArtistName] = useState('');
  const [artRepairReport, setArtRepairReport] = useState<ArtRepairReportResponse | null>(null);
  const [artRepairError, setArtRepairError] = useState('');
  const [trackSearchLoading, setTrackSearchLoading] = useState(false);
  const [trackSearchError, setTrackSearchError] = useState('');
  const [trackSearchTried, setTrackSearchTried] = useState(false);
  const [detailsRow, setDetailsRow] = useState<AlbumRow | null>(null);
  const [detailsTracks, setDetailsTracks] = useState<LibraryTrack[]>([]);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState('');

  const loadLibrary = useCallback(async (includeTracks = false) => {
    setLoading(true);
    setError('');
    try {
      const suffix = includeTracks ? '&include_tracks=1' : '';
      const next = await apiGet<LibraryResponse>(`/api/library?include_disk_only=1${suffix}`);
      setData(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  useEffect(() => {
    let cancelled = false;
    async function loadArtRepair() {
      try {
        const report = await getLibraryArtRepairReport();
        if (!cancelled) setArtRepairReport(report);
      } catch (err) {
        if (!cancelled) setArtRepairError(err instanceof Error ? err.message : String(err));
      }
    }
    void loadArtRepair();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const needsTrackRows = query.trim().length >= 2 && (scope === 'all' || scope === 'tracks');
    if (!needsTrackRows || data?.tracks_included || trackSearchLoading || trackSearchTried) return;

    let cancelled = false;
    async function loadTrackRows() {
      setTrackSearchLoading(true);
      setTrackSearchError('');
      setTrackSearchTried(true);
      try {
        const next = await apiGet<LibraryResponse>('/api/library?include_disk_only=1&include_tracks=1');
        if (!cancelled) setData(next);
      } catch (err) {
        if (!cancelled) setTrackSearchError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setTrackSearchLoading(false);
      }
    }

    void loadTrackRows();
    return () => {
      cancelled = true;
    };
  }, [data?.tracks_included, query, scope, trackSearchLoading, trackSearchTried]);

  useEffect(() => {
    const row = detailsRow;
    const albumId = Number(row?.album.album_id || 0);
    const fallbackTracks = row?.album.tracks ?? [];
    if (!albumId) {
      setDetailsTracks(row?.album.tracks ?? []);
      setDetailsLoading(false);
      setDetailsError('');
      return;
    }

    let cancelled = false;
    async function loadTracks() {
      setDetailsLoading(true);
      setDetailsError('');
      try {
        const result = await getAlbumTracks(albumId);
        if (!cancelled) setDetailsTracks(result.tracks ?? []);
      } catch (err) {
        if (!cancelled) {
          setDetailsError(err instanceof Error ? err.message : String(err));
          setDetailsTracks(fallbackTracks);
        }
      } finally {
        if (!cancelled) setDetailsLoading(false);
      }
    }

    void loadTracks();
    return () => {
      cancelled = true;
    };
  }, [detailsRow]);

  const artists = data?.artists ?? [];
  const libraryVersion = data?.library_version;

  const artRepairAlbumIds = useMemo(() => {
    const ids = new Set<number>();
    for (const item of artRepairReport?.items ?? []) {
      const id = Number(item.album_id || 0);
      if (id) ids.add(id);
    }
    return ids;
  }, [artRepairReport]);

  const artRepairItemMap = useMemo(() => {
    const map = new Map<number, ArtRepairItem>();
    for (const item of artRepairReport?.items ?? []) {
      const id = Number(item.album_id || 0);
      if (id) map.set(id, item);
    }
    return map;
  }, [artRepairReport]);

  const allAlbumRows = useMemo(() => {
    const rows: AlbumRow[] = [];
    for (const artist of artists) {
      for (const album of artist.albums ?? []) {
        rows.push({ artist, album });
      }
    }
    return rows;
  }, [artists]);

  const statusCounts = useMemo(() => {
    const counts: Record<StatusFilter, number> = {
      all: allAlbumRows.length,
      ids: 0,
      missing: 0,
      extra: 0,
      art: 0,
      clean: 0,
    };

    for (const { album } of allAlbumRows) {
      for (const status of ['ids', 'missing', 'extra', 'art', 'clean'] as StatusFilter[]) {
        if (albumMatchesStatus(album, status, artRepairAlbumIds)) counts[status] += 1;
      }
    }

    return counts;
  }, [allAlbumRows, artRepairAlbumIds]);

  const filteredArtists = useMemo(() => {
    const result = artists.filter((artist) => artistMatchesQuery(artist, query, scope));
    result.sort((a, b) => {
      if (artistSort === 'albums') return b.albums.length - a.albums.length || displayArtistName(a).localeCompare(displayArtistName(b));
      if (artistSort === 'tracks') return countTracks(b) - countTracks(a) || displayArtistName(a).localeCompare(displayArtistName(b));
      if (artistSort === 'attention') {
        const attentionDelta = Number(artistNeedsAttention(b)) - Number(artistNeedsAttention(a));
        return attentionDelta || countAttentionAlbums(b) - countAttentionAlbums(a) || displayArtistName(a).localeCompare(displayArtistName(b));
      }
      return displayArtistName(a).localeCompare(displayArtistName(b));
    });
    return result;
  }, [artistSort, artists, query, scope]);

  const selectedArtist = useMemo(
    () => artists.find((artist) => artist.name === selectedArtistName) ?? null,
    [artists, selectedArtistName],
  );

  useEffect(() => {
    if (selectedArtistName && !selectedArtist) setSelectedArtistName('');
  }, [selectedArtist, selectedArtistName]);

  const sortAlbumRows = useCallback((rows: AlbumRow[]) => {
    const sorted = [...rows];
    sorted.sort((a, b) => {
      if (albumSort === 'album') return displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album)) || displayArtistName(a.artist).localeCompare(displayArtistName(b.artist));
      if (albumSort === 'year') return Number(b.album.year || 0) - Number(a.album.year || 0) || displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album));
      if (albumSort === 'issues') return albumIssueScore(b.album, artRepairAlbumIds) - albumIssueScore(a.album, artRepairAlbumIds) || displayArtistName(a.artist).localeCompare(displayArtistName(b.artist));
      return displayArtistName(a.artist).localeCompare(displayArtistName(b.artist)) || displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album));
    });
    return sorted;
  }, [albumSort, artRepairAlbumIds]);

  const selectedArtistRows = useMemo(() => {
    if (!selectedArtist) return [];
    return sortAlbumRows(
      selectedArtist.albums
        .map((album) => ({ artist: selectedArtist, album }))
        .filter((row) => albumMatchesQuery(row.album, row.artist, query, scope)),
    );
  }, [query, scope, selectedArtist, sortAlbumRows]);

  const albumResults = useMemo(() => (
    sortAlbumRows(
      allAlbumRows.filter((row) => (
        activeStatus !== 'all' &&
        albumMatchesStatus(row.album, activeStatus, artRepairAlbumIds) &&
        albumMatchesQuery(row.album, row.artist, query, scope)
      )),
    )
  ), [activeStatus, allAlbumRows, artRepairAlbumIds, query, scope, sortAlbumRows]);

  const albumMode = activeStatus !== 'all' || Boolean(selectedArtist);
  const activeFilter = statusFilters.find((filter) => filter.id === activeStatus) ?? statusFilters[0];
  const attentionArtistCount = artists.filter(artistNeedsAttention).length;
  const attentionAlbumCount = allAlbumRows.filter((row) => albumNeedsAttention(row.album, artRepairAlbumIds)).length;
  const nextRepairRow = useMemo(() => {
    const rows = allAlbumRows.filter((row) => albumNeedsAttention(row.album, artRepairAlbumIds));
    rows.sort((a, b) => compareRepairRows(a, b, artRepairAlbumIds));
    return rows[0] ?? null;
  }, [allAlbumRows, artRepairAlbumIds]);

  function handleStatusChange(status: StatusFilter) {
    setActiveStatus(status);
    if (status !== 'all') setSelectedArtistName('');
  }

  function handleSelectArtist(artist: LibraryArtist) {
    setActiveStatus('all');
    setSelectedArtistName(artist.name);
  }

  const handleNavigate = useCallback((path: string) => {
    navigate(path);
  }, [navigate]);

  return (
    <div className="space-y-4">
      <section className="rounded-md border border-graphite-800 bg-graphite-950/45 p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase text-red-400">Library</p>
            <h1 className="mt-1 text-2xl font-semibold text-zinc-100">Browse music library</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Chip label={`${numberFmt.format(data?.stats.artists ?? 0)} artists`} size="small" variant="outlined" />
              <Chip label={`${numberFmt.format(data?.stats.albums ?? 0)} albums`} size="small" variant="outlined" />
              <Chip label={`${numberFmt.format(data?.stats.tracks ?? 0)} tracks`} size="small" variant="outlined" />
              {attentionAlbumCount ? (
                <Chip color="warning" label={`${numberFmt.format(attentionAlbumCount)} review albums`} size="small" variant="outlined" />
              ) : (
                <Chip color="success" label="No review issues" size="small" variant="outlined" />
              )}
            </div>
          </div>
          <div className="text-xs text-zinc-500 lg:text-right">
            {data?.tracks_included ? 'Track search loaded' : 'Fast library summary'}
          </div>
        </div>
        {loading ? <LinearProgress sx={{ mt: 2, borderRadius: 1 }} /> : null}
      </section>

      <NextRepairPanel
        row={nextRepairRow}
        artRepairAlbumIds={artRepairAlbumIds}
        artRepairItemMap={artRepairItemMap}
        libraryVersion={libraryVersion}
        onDetails={setDetailsRow}
        onFocusStatus={handleStatusChange}
        onNavigate={handleNavigate}
      />

      <StatusCards
        active={activeStatus}
        counts={statusCounts}
        onChange={handleStatusChange}
      />

      <div className="flex flex-wrap items-center gap-2 rounded-md border border-graphite-800 bg-graphite-950/35 px-3 py-2 text-sm">
        <Chip
          color={statusChipColor(activeStatus)}
          label={activeStatus === 'all' ? 'Browsing artists' : `Active filter: ${activeFilter.label}`}
          size="small"
          variant="outlined"
        />
        {selectedArtist ? <Chip label={`Artist: ${displayArtistName(selectedArtist)}`} size="small" variant="outlined" /> : null}
        {activeStatus !== 'all' ? (
          <Button size="small" variant="text" onClick={() => setActiveStatus('all')}>
            Clear filter
          </Button>
        ) : null}
        <span className="text-xs text-zinc-500">
          {activeStatus === 'all'
            ? `${numberFmt.format(filteredArtists.length)} artists visible, ${numberFmt.format(attentionArtistCount)} with review items`
            : `${numberFmt.format(albumResults.length)} matching albums`}
        </span>
      </div>

      <SearchRow
        query={query}
        scope={scope}
        artistSort={artistSort}
        albumSort={albumSort}
        albumMode={albumMode}
        trackSearchLoading={trackSearchLoading}
        onQueryChange={setQuery}
        onScopeChange={setScope}
        onArtistSortChange={setArtistSort}
        onAlbumSortChange={setAlbumSort}
      />

      {error ? <Alert severity="error">{error}</Alert> : null}
      {artRepairError ? <Alert severity="warning">Art repair status could not be loaded: {artRepairError}</Alert> : null}
      {trackSearchError ? <Alert severity="warning">Track search rows could not be loaded: {trackSearchError}</Alert> : null}

      {activeStatus !== 'all' ? (
        <AlbumGrid
          rows={albumResults}
          title={activeFilter.label}
          subtitle={`${numberFmt.format(albumResults.length)} matching albums across the library`}
          artRepairAlbumIds={artRepairAlbumIds}
          artRepairItemMap={artRepairItemMap}
          libraryVersion={libraryVersion}
          showArtist
          onDetails={setDetailsRow}
          onNavigate={handleNavigate}
        />
      ) : selectedArtist ? (
        <ArtistDetail
          artist={selectedArtist}
          rows={selectedArtistRows}
          artRepairAlbumIds={artRepairAlbumIds}
          artRepairItemMap={artRepairItemMap}
          libraryVersion={libraryVersion}
          onBack={() => setSelectedArtistName('')}
          onDetails={setDetailsRow}
          onNavigate={handleNavigate}
        />
      ) : (
        <ArtistBrowser
          artists={filteredArtists}
          letter={artistLetterFilter}
          libraryVersion={libraryVersion}
          loading={loading && !data}
          onLetterChange={setArtistLetterFilter}
          onSelectArtist={handleSelectArtist}
        />
      )}

      <AlbumDetailsDialog
        row={detailsRow}
        tracks={detailsTracks}
        loading={detailsLoading}
        error={detailsError}
        artRepairAlbumIds={artRepairAlbumIds}
        artRepairItemMap={artRepairItemMap}
        libraryVersion={libraryVersion}
        onClose={() => setDetailsRow(null)}
      />
    </div>
  );
}
