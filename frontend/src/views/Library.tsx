import { errorMessage, panelError } from '../lib/errors';
import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { getAlbumTracks, getArtistImageUrl, getLibraryArtRepairReport } from '../api/client';
import type { ArtRepairItem, ArtRepairReportResponse } from '../api/types';
import { artistNeedsAttention, getAlbumHealth } from '../lib/libraryHealth';
import { apiGet } from '../lib/api';
import { useInterval } from '../lib/hooks';
import type { LibraryAlbum, LibraryArtist, LibraryResponse, LibraryTrack } from '../types/api';
import { AlbumCleanupModal } from '../components/AlbumCleanupModal';
import { TrackReplacementModal } from '../components/TrackReplacementModal';
import { useEngineOffline, useReloadOnEngineReturn } from '../components/EngineOfflineBanner';

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

// Every ArtistCard/detail header mounts its own useArtistArtUrl instance, and
// a large library can have hundreds of artists with no cached image at once
// (773 artists, 328 needing a fetch, confirmed live 2026-07-20). Without a
// shared cap, all of them fired /api/artist-image-url simultaneously on
// mount -- flooding both the browser's per-origin connection limit and the
// backend's Waitress thread pool (WEBCONTROL_THREADS, default 8), badly
// enough that even the /api/library request (which serves the fast engine-only
// tree immediately and reconciles disk state in a background job) got stuck
// queued behind hundreds of rivals for 20+ seconds. This queue
// caps how many of these run at once; everything past the cap waits its
// turn instead of piling on all at once.
const ARTIST_IMAGE_FETCH_CONCURRENCY = 4;
let activeArtistImageFetches = 0;
const artistImageFetchQueue: Array<() => void> = [];

function runArtistImageFetchThrottled<T>(task: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const run = () => {
      activeArtistImageFetches += 1;
      task()
        .then(resolve, reject)
        .finally(() => {
          activeArtistImageFetches -= 1;
          const next = artistImageFetchQueue.shift();
          if (next) next();
        });
    };
    if (activeArtistImageFetches < ARTIST_IMAGE_FETCH_CONCURRENCY) {
      run();
    } else {
      artistImageFetchQueue.push(run);
    }
  });
}

// Falls back to an album cover immediately, then swaps in a real locally-cached
// artist photo (fetched from Discogs and stored under /api/artist-image-cache)
// once available, so artists aren't stuck showing a borrowed album cover forever.
function useArtistArtUrl(artist: LibraryArtist, libraryVersion?: number) {
  const [fetchedUrl, setFetchedUrl] = useState('');
  const hasCachedImage = Boolean(artist.artist_image_url || artist.image_url);
  const artistName = artist.name;

  useEffect(() => {
    setFetchedUrl('');
    if (hasCachedImage || !artistName) return undefined;
    let cancelled = false;
    runArtistImageFetchThrottled(() => getArtistImageUrl(artistName))
      .then((res) => {
        if (!cancelled && res.ok && res.url) setFetchedUrl(res.url);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [artistName, hasCachedImage]);

  if (fetchedUrl) return addVersion(fetchedUrl, libraryVersion);
  return artistArtUrl(artist, libraryVersion);
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

// Tile actions may wrap onto two lines so two cover columns fit on phones (the theme keeps
// button labels on one line everywhere else, #607).
const tileButtonSx = { whiteSpace: 'normal', lineHeight: 1.25, textAlign: 'center' } as const;

// Placeholder sleeve for albums without art: a record half out of a plain card sleeve.
function SleevePlaceholder() {
  return (
    <span className="relative flex h-full w-full items-center justify-center bg-graphite-850" data-testid="sleeve-placeholder">
      <svg aria-hidden="true" className="h-3/4 w-3/4" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="62" cy="50" r="36" className="fill-zinc-950" />
        <circle cx="62" cy="50" r="29" className="stroke-graphite-800" strokeWidth="1" />
        <circle cx="62" cy="50" r="22" className="stroke-graphite-800" strokeWidth="1" />
        <circle cx="62" cy="50" r="11" className="fill-brass-700" />
        <circle cx="62" cy="50" r="2" className="fill-graphite-950" />
        <rect x="8" y="12" width="56" height="76" rx="2" className="fill-graphite-800 stroke-graphite-700" />
        <circle cx="36" cy="50" r="10" className="stroke-graphite-700" strokeWidth="1.5" />
      </svg>
      <span className="sr-only">No cover art</span>
    </span>
  );
}

function Artwork({
  src,
  label,
  fallback,
  className,
}: {
  src: string;
  label: string;
  fallback: ReactNode;
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
        // No portrait: initials printed on a record label, grooves around it (authored CSS, no image).
        typeof fallback === 'string' ? (
          <span className="lib-groove flex h-full w-full items-center justify-center">
            <span className="lib-label">{fallback}</span>
          </span>
        ) : fallback
      )}
    </div>
  );
}

// The header's shelf: a few sleeves leaning on the ledge, the library's own cover art. Decorative
// (the albums are listed below), so hidden from assistive tech; a cover that fails to load just drops out.
function ShelfCover({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <img
      alt=""
      className="lib-shelf-cover"
      decoding="async"
      height={112}
      loading="lazy"
      src={src}
      width={112}
      onError={() => setFailed(true)}
    />
  );
}

function CoverShelf({ urls }: { urls: string[] }) {
  if (!urls.length) return null;
  return (
    <div aria-hidden="true" className="lib-shelf">
      {urls.map((url) => <ShelfCover key={url} src={url} />)}
    </div>
  );
}

// A sleeve blown up, blurred and tinted to sit behind a header. Decorative: the same cover is shown
// sharp beside it. Without art (or when it fails) the walnut ground shows through.
function CoverBackdrop({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setFailed(false);
  }, [src]);
  return (
    <div aria-hidden="true" className="lib-backdrop">
      {src && !failed ? (
        <img alt="" decoding="async" height={64} loading="lazy" src={src} width={64} onError={() => setFailed(true)} />
      ) : null}
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

export function albumPrimaryAction(album: LibraryAlbum, artRepairItemMap: Map<number, ArtRepairItem>): AlbumAction | null {
  const health = getAlbumHealth(album);
  if (album.pending_review) return {
    label: 'In review queue',
    path: '/import?tab=review',
    title: 'Already queued in Import Review — check there for next steps',
    variant: 'primary',
  };
  if (health.releaseMbMissing) return {
    label: 'Fix missing IDs',
    path: '/library/missing-ids',
    title: 'No MusicBrainz release ID — assign one in Library > Missing IDs',
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
      path: '/jobs?tab=album-tracks',
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
    path: '/jobs?tab=library-db',
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

// Library is browse-only: repair actions live on their owning page, so these render as links that
// name where they lead, never as action buttons.
function ActionLinkLabel({ action }: { action: AlbumAction }) {
  const page = action.path.startsWith('/jobs') ? 'Jobs' : action.path.startsWith('/library/missing-ids') ? 'Missing IDs' : 'Import';
  return (
    // One span: MUI buttons are inline-flex, which would drop the spaces between bare text nodes.
    <span>
      {action.label} <span className="sr-only">on</span><span aria-hidden="true">→</span> {page}
    </span>
  );
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
  const coverUrl = albumArtUrl(album, libraryVersion);

  return (
    // The page's focal point: the record to fix next, its own sleeve blown up and tinted behind it,
    // bleeding to the edges of the content frame. No box: the backdrop and the rules set it apart.
    <section aria-label="Next repair" className="relative isolate -mx-3 overflow-hidden border-y border-graphite-800 px-3 py-5 sm:-mx-4 sm:px-4 sm:py-6">
      <CoverBackdrop src={coverUrl} />
      <div className="grid gap-4 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center lg:grid-cols-[auto_minmax(0,1fr)_auto]">
        <Artwork
          className="h-28 w-28 rounded-sm ring-1 ring-black/40 sm:h-36 sm:w-36"
          fallback={<SleevePlaceholder />}
          label={`${title} cover`}
          src={coverUrl}
        />
        <div className="min-w-0">
          <h2 className="line-clamp-2 font-display text-2xl font-semibold text-zinc-100 sm:text-3xl">{title}</h2>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-sm text-zinc-300">
            <span className="font-semibold text-brass-300">Next repair</span>
            <span>{artistName}</span>
            {album.year ? <span className="tabular-nums">{String(album.year).slice(0, 4)}</span> : null}
            <span>{albumPrimaryIssue(album, artRepairAlbumIds)}</span>
          </div>
          <div className="mt-3">
            <AlbumBadges album={album} artRepairAlbumIds={artRepairAlbumIds} artRepairItemMap={artRepairItemMap} />
          </div>
        </div>
        <div className="flex flex-wrap gap-2 sm:col-span-2 lg:col-span-1 lg:flex-col lg:items-stretch">
          {action ? (
            <Button
              size="small"
              variant="text"
              color={action.variant === 'warning' ? 'warning' : 'primary'}
              title={action.title}
              sx={{ textDecoration: 'underline', textUnderlineOffset: '0.2em' }}
              onClick={() => onNavigate(action.path)}
            >
              <ActionLinkLabel action={action} />
            </Button>
          ) : null}
          <Button aria-haspopup="dialog" size="small" variant="outlined" onClick={() => onDetails(row)}>
            View details
          </Button>
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
  // Six toggles set on one rule, like index tabs on a bin; the selected one carries a brass underline.
  return (
    <div
      aria-label="Filter the library by issue"
      className="grid grid-cols-2 gap-x-4 sm:grid-cols-3 xl:grid-cols-6"
      role="group"
    >
      {statusFilters.map((filter) => {
        const selected = active === filter.id;
        const count = counts[filter.id] ?? 0;
        return (
          <button
            key={filter.id}
            aria-pressed={selected}
            className={cx(
              'min-h-12 border-b-2 py-1.5 text-left transition-colors',
              selected
                ? 'border-brass-400 text-brass-200'
                : 'border-graphite-800 text-zinc-200 hover:border-graphite-600 hover:text-zinc-100 active:border-brass-700',
            )}
            type="button"
            onClick={() => onChange(filter.id)}
          >
            <span className="block text-xs text-muted">{filter.detail}</span>{' '}
            <span className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-semibold">{filter.label}</span>{' '}
              <span className={cx('text-base font-semibold tabular-nums', count ? 'text-zinc-100' : 'text-muted')}>
                {numberFmt.format(count)}
              </span>
            </span>
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
    <div className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_11rem_13rem]">
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
          <div className="mt-1 text-xs text-muted">Loading track rows for track search…</div>
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
  // Crate divider tabs standing on the bin's edge, one per initial; they wrap onto more rows on a phone.
  return (
    <div
      aria-label="Artists by initial"
      className="crate-tabs flex flex-wrap gap-1"
      role="group"
    >
      {ARTIST_LETTERS.map((letter) => {
        const enabled = letter === 'all' || available.has(letter);
        const selected = active === letter;
        return (
          <button
            key={letter}
            aria-pressed={selected}
            className={cx(
              'crate-tab h-10 min-w-10 shrink-0 px-2 text-sm transition-colors',
              selected ? 'font-semibold' : null,
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
  const artUrl = useArtistArtUrl(artist, libraryVersion);

  // A record in the crate: the artist's art as a sleeve, the name and one line of counts under it.
  return (
    <button
      className="group flex w-[calc(50%-1.5rem)] min-w-0 flex-col gap-2 rounded-sm p-1 text-left transition-colors hover:bg-walnut-800 active:bg-walnut-700 sm:w-40"
      type="button"
      onClick={() => onSelect(artist)}
    >
      <span className="sleeve block aspect-square w-full">
        <Artwork
          className="sleeve-face h-full w-full"
          fallback={initials(name) || 'A'}
          label={`${name} artwork`}
          src={artUrl}
        />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold text-zinc-100 group-hover:text-brass-200">{name}</span>
        <span className="flex flex-wrap items-baseline gap-x-2 text-xs text-zinc-400">
          <span className="tabular-nums">
            {numberFmt.format(artist.albums.length)} albums - {numberFmt.format(countTracks(artist))} tracks
          </span>
          {needsAttention ? (
            <span className="text-brass-300">
              {attentionAlbums ? `${attentionAlbums} need review` : 'Needs attention'}
            </span>
          ) : (
            <span className="text-success">Clean</span>
          )}
          {artist.empty_artist_folder ? (
            <span className="text-info">Empty folder</span>
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
  grouped,
  onLetterChange,
  onSelectArtist,
}: {
  artists: LibraryArtist[];
  grouped: boolean;
  letter: ArtistLetter;
  libraryVersion?: number;
  loading: boolean;
  onLetterChange: (letter: ArtistLetter) => void;
  onSelectArtist: (artist: LibraryArtist) => void;
}) {
  const availableLetters = useMemo(() => new Set(artists.map(artistLetter)), [artists]);
  const visibleArtists = letter === 'all' ? artists : artists.filter((artist) => artistLetter(artist) === letter);
  // Sorted A to Z, the crate gets a divider tab at each new initial; other sorts stay one run.
  const groups = useMemo(() => {
    const runs: Array<{ letter: ArtistLetter | null; artists: LibraryArtist[] }> = [];
    for (const artist of visibleArtists) {
      const key = grouped ? artistLetter(artist) : null;
      const last = runs[runs.length - 1];
      if (last && last.letter === key) last.artists.push(artist);
      else runs.push({ letter: key, artists: [artist] });
    }
    return runs;
  }, [grouped, visibleArtists]);

  return (
    <section className="space-y-2">
      <AlphabetIndex active={letter} available={availableLetters} onChange={onLetterChange} />
      <h2 className="border-b border-graphite-800 pb-2 text-sm font-semibold text-zinc-300">
        {letter === 'all' ? 'Artists' : `Artists starting with ${letter}`} - {numberFmt.format(visibleArtists.length)}
      </h2>
      <div className="crate max-h-[calc(100vh-21rem)] min-h-72 overflow-y-auto rounded-sm px-3 py-4">
        {loading ? (
          <div className="p-4 text-sm text-zinc-300">Loading library…</div>
        ) : visibleArtists.length ? (
          // One run of sleeves; when sorted A to Z a walnut divider card stands before each initial,
          // the way dividers stand between records in a crate.
          <div className="flex flex-wrap gap-x-3 gap-y-5">
            {groups.flatMap((group, groupIndex) => [
              group.letter ? (
                <div key={`divider-${group.letter}-${groupIndex}`} className="crate-divider-card">
                  <h3 className="crate-divider">{group.letter}</h3>
                </div>
              ) : null,
              ...group.artists.map((artist, index) => (
                <ArtistCard
                  key={`${artist.name}::${artist.path ?? `${groupIndex}-${index}`}`}
                  artist={artist}
                  libraryVersion={libraryVersion}
                  onSelect={onSelectArtist}
                />
              )),
            ])}
          </div>
        ) : (
          <div className="p-4 text-sm text-zinc-300">No matching artists.</div>
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
  onCleanAlbum,
}: {
  row: AlbumRow;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  showArtist: boolean;
  onDetails: (row: AlbumRow) => void;
  onNavigate: (path: string) => void;
  onCleanAlbum?: (row: AlbumRow) => void;
}) {
  const { album, artist } = row;
  const title = displayAlbumTitle(album);
  const artistName = displayArtistName(artist);
  const issue = albumPrimaryIssue(album, artRepairAlbumIds);
  const action = albumPrimaryAction(album, artRepairItemMap);

  return (
    // Open tile: the sleeve leads, the words sit under it on the shelf, no box around either.
    <article className="flex min-w-0 flex-col">
      {/* A sleeve standing in the crate, the sleeves behind it showing at its edge. */}
      <div className="sleeve aspect-square w-full">
        <Artwork
          className="sleeve-face h-full w-full"
          fallback={<SleevePlaceholder />}
          label={`${title} cover`}
          src={albumArtUrl(album, libraryVersion)}
        />
      </div>
      <div className="mt-2 min-w-0 flex-1">
        <h3 className="mt-1 line-clamp-2 text-sm font-semibold text-zinc-100" title={title}>{title}</h3>
        {showArtist ? <div className="truncate text-sm text-zinc-300" title={artistName}>{artistName}</div> : null}
        <div className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-zinc-400">
          {album.year ? <span>{String(album.year).slice(0, 4)}</span> : null}
          <span>{numberFmt.format(albumTrackCount(album))} tracks</span>
        </div>
        <div className="mt-2 space-y-1.5">
          <Chip label={issue} size="small" sx={{ maxWidth: '100%' }} variant="outlined" />
          <AlbumBadges album={album} artRepairAlbumIds={artRepairAlbumIds} artRepairItemMap={artRepairItemMap} />
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Button aria-haspopup="dialog" size="small" sx={tileButtonSx} variant="outlined" onClick={() => onDetails(row)}>
            View details
          </Button>
          {album.album_id ? (
            <Button
              size="small"
              sx={tileButtonSx}
              variant="outlined"
              color="warning"
              onClick={() => onCleanAlbum?.(row)}
            >
              Remove from Library…
            </Button>
          ) : null}
          {action ? (
            album.pending_review ? (
              <Button
                size="small"
                sx={tileButtonSx}
                variant="text"
                color="info"
                title={action.title}
                onClick={() => onNavigate(action.path)}
              >
                <ActionLinkLabel action={action} />
              </Button>
            ) : (
              <Button
                size="small"
                sx={tileButtonSx}
                variant="text"
                color={action.variant === 'warning' ? 'warning' : 'primary'}
                title={action.title}
                onClick={() => onNavigate(action.path)}
              >
                <ActionLinkLabel action={action} />
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
  onCleanAlbum,
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
  onCleanAlbum?: (row: AlbumRow) => void;
}) {
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-graphite-800 pb-2">
        <h2 className="font-display text-lg font-semibold text-zinc-100">{title}</h2>
        <p className="text-sm text-zinc-400">{subtitle}</p>
      </div>
      <div className="crate max-h-[calc(100vh-22rem)] min-h-72 overflow-y-auto rounded-sm px-3 py-4">
        {rows.length ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,8rem),1fr))] gap-3 sm:grid-cols-[repeat(auto-fill,minmax(min(100%,12rem),1fr))]">
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
                onCleanAlbum={onCleanAlbum}
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
  onCleanAlbum,
}: {
  artist: LibraryArtist;
  rows: AlbumRow[];
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  onBack: () => void;
  onDetails: (row: AlbumRow) => void;
  onNavigate: (path: string) => void;
  onCleanAlbum?: (row: AlbumRow) => void;
}) {
  const name = displayArtistName(artist);
  const attentionAlbums = countAttentionAlbums(artist);
  const artUrl = useArtistArtUrl(artist, libraryVersion);

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm text-zinc-400">
        <button className="inline-flex min-h-8 items-center text-brass-300 hover:text-brass-200 active:text-brass-400" type="button" onClick={onBack}>Library</button>
        <span aria-hidden="true">/</span>
        <button className="inline-flex min-h-8 items-center text-brass-300 hover:text-brass-200 active:text-brass-400" type="button" onClick={onBack}>Artists</button>
        <span aria-hidden="true">/</span>
        <span className="text-zinc-200">{name}</span>
      </div>

      {/* The artist's own art, blown up behind the header and bleeding to the frame's edges. */}
      <div className="relative isolate -mx-3 flex flex-col gap-4 overflow-hidden px-3 py-6 sm:-mx-4 sm:flex-row sm:items-end sm:px-4 sm:py-8">
        <CoverBackdrop src={artUrl} />
        <Artwork
          className="h-28 w-28 rounded-sm ring-1 ring-black/40 sm:h-36 sm:w-36"
          fallback={initials(name) || 'A'}
          label={`${name} artwork`}
          src={artUrl}
        />
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-3xl font-semibold text-zinc-50 [overflow-wrap:anywhere] sm:text-4xl">{name}</h2>
          {/* The same catalogue line as the page header, scoped to this artist. */}
          <p className="lib-catalog">
            <span>{`${numberFmt.format(artist.albums.length)} albums`}</span>
            <span>{`${numberFmt.format(countTracks(artist))} tracks`}</span>
            {artistNeedsAttention(artist) ? (
              <span className="lib-review is-flag">{attentionAlbums ? `${attentionAlbums} need review` : 'Needs attention'}</span>
            ) : (
              <span className="lib-review is-ok">Clean</span>
            )}
            {artist.empty_artist_folder ? <span className="lib-review">Empty folder</span> : null}
          </p>
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
        onCleanAlbum={onCleanAlbum}
      />
    </section>
  );
}

function trackStatus(track: LibraryTrack) {
  if (track.missing || track.status === 'missing_file') return { label: 'Missing file', color: 'text-danger-text' };
  if (track.status === 'not_imported' || track.disk_only) return { label: 'Not imported', color: 'text-warning' };
  if (track.status === 'other_album') return { label: 'Other album', color: 'text-warning' };
  if (!String(track.mb_trackid ?? '').trim()) return { label: 'No recording ID', color: 'text-violet-300' };
  return { label: 'Imported', color: 'text-success' };
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
  onCleanAlbum,
  onNavigate,
  onReplaceTrack,
}: {
  row: AlbumRow | null;
  tracks: LibraryTrack[];
  loading: boolean;
  error: string;
  artRepairAlbumIds: Set<number>;
  artRepairItemMap: Map<number, ArtRepairItem>;
  libraryVersion?: number;
  onClose: () => void;
  onCleanAlbum?: (row: AlbumRow) => void;
  onNavigate?: (path: string) => void;
  onReplaceTrack?: (track: LibraryTrack) => void;
}) {
  const album = row?.album ?? null;
  const artist = row?.artist ?? null;
  const title = album ? displayAlbumTitle(album) : '';
  const artistName = artist ? displayArtistName(artist) : '';

  const coverUrl = album ? albumArtUrl(album, libraryVersion) : '';
  const multiDisc = tracks.some((track) => Number(track.disc || 1) > 1);

  // Opening an album flips its sleeve over: the front (the cover) turns away and the back cover,
  // printed with the tracklist like an LP, faces the listener. Only this one panel animates.
  return (
    <Dialog className="relative z-50" open={Boolean(row)} onClose={onClose}>
      <DialogBackdrop className="fixed inset-0 bg-black/70" />
      <div className="fixed inset-0 overflow-y-auto p-4">
        <div className="flex min-h-full items-center justify-center">
          <DialogPanel className="flip-card w-full max-w-3xl">
            {album && artist ? (
              <div className="flip-inner">
                <div aria-hidden="true" className="flip-face flip-front" inert>
                  <Artwork
                    className="aspect-square h-full max-h-full w-auto max-w-full"
                    fallback={<SleevePlaceholder />}
                    label=""
                    src={coverUrl}
                  />
                </div>
                <div className="flip-face flip-back space-y-5 p-4 sm:p-6">
                  <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 gap-4">
                      <Artwork
                        className="h-20 w-20 rounded-sm"
                        fallback={<SleevePlaceholder />}
                        label={`${title} cover`}
                        src={coverUrl}
                      />
                      <div className="min-w-0">
                        <DialogTitle
                          className="line-clamp-2 font-display text-2xl font-semibold text-zinc-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brass-300"
                          data-autofocus
                          tabIndex={-1}
                        >
                          {title}
                        </DialogTitle>
                        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-sm text-zinc-300">
                          <span>{artistName}</span>
                          {album.year ? <span className="tabular-nums">{String(album.year).slice(0, 4)}</span> : null}
                          <span>{numberFmt.format(albumTrackCount(album))} tracks</span>
                        </div>
                        <div className="mt-2">
                          <AlbumBadges album={album} artRepairAlbumIds={artRepairAlbumIds} artRepairItemMap={artRepairItemMap} />
                        </div>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {album.album_id ? (
                        <Button
                          size="small"
                          variant="outlined"
                          color="warning"
                          onClick={() => {
                            onClose();
                            if (row) onCleanAlbum?.(row);
                          }}
                        >
                          Remove from Library…
                        </Button>
                      ) : null}
                      <Button size="small" variant="outlined" onClick={onClose}>Close</Button>
                    </div>
                  </div>

                  {/* The back cover's print: the tracklist first, then the review notes. */}
                  <section>
                    <div className="flex items-baseline justify-between border-b border-zinc-700 pb-1.5">
                      <h3 className="font-display text-lg font-semibold text-zinc-100">Tracks</h3>
                      <span className="text-xs text-muted tabular-nums">{loading ? 'Loading…' : `${tracks.length} rows`}</span>
                    </div>
                    {loading ? <LinearProgress /> : null}
                    {error ? <div className="py-3 text-sm text-danger-text">{error}</div> : null}
                    {!loading && !error ? (
                      tracks.length ? (
                        <ol className="max-h-80 overflow-y-auto">
                          {tracks.map((track, index) => {
                            const status = trackStatus(track);
                            const canReplace = Boolean(track.id && track.path && !track.missing);
                            const disc = Number(track.disc || 1);
                            const discStart = multiDisc && (index === 0 || Number(tracks[index - 1].disc || 1) !== disc);
                            return (
                              <li key={`${track.id || index}-${track.title}`}>
                                {discStart ? <div className="pb-1 pt-3 text-sm font-semibold text-brass-300">{`Disc ${disc}`}</div> : null}
                                <div className="grid grid-cols-[2.5rem_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b border-zinc-800 py-2 text-sm sm:grid-cols-[2.5rem_minmax(0,1fr)_7rem_3.5rem_auto]">
                                  <span className="text-xs text-muted tabular-nums">{track.disc && track.disc > 1 ? `${track.disc}.` : ''}{track.track || index + 1}</span>
                                  <span className="min-w-0 truncate text-zinc-100">{stripStamps(track.title) || 'Untitled track'}</span>
                                  <span className={cx('truncate text-xs sm:text-right', status.color)}>{status.label}</span>
                                  <span className="hidden text-right text-xs text-muted tabular-nums sm:block">{formatLength(track.length)}</span>
                                  {canReplace ? (
                                    <Button size="small" variant="outlined" onClick={() => onReplaceTrack?.(track)}>
                                      Replace…
                                    </Button>
                                  ) : <span />}
                                </div>
                              </li>
                            );
                          })}
                        </ol>
                      ) : (
                        <div className="py-3 text-sm text-muted">No track rows loaded for this album.</div>
                      )
                    ) : null}
                  </section>

                  <section>
                    <h3 className="border-b border-zinc-700 pb-1.5 font-display text-lg font-semibold text-zinc-100">Review information</h3>
                    <ul className="mt-2 space-y-1 text-sm text-zinc-300">
                      {albumIssueDetails(album, artRepairAlbumIds).map((detail) => (
                        <li key={detail}>{detail}</li>
                      ))}
                    </ul>
                    {/* Library is for browsing; changes to an album are made on the Import workbench. */}
                    {onNavigate ? (
                      <button
                        className="mt-3 inline-flex min-h-8 items-center text-sm text-brass-300 underline underline-offset-2 hover:text-brass-200 active:text-brass-400"
                        type="button"
                        onClick={() => {
                          onClose();
                          onNavigate('/import');
                        }}
                      >
                        Manage on Import
                      </button>
                    ) : null}
                  </section>
                </div>
              </div>
            ) : null}
          </DialogPanel>
        </div>
      </div>
    </Dialog>
  );
}

function formatLength(seconds?: number) {
  if (!seconds || !Number.isFinite(seconds)) return '';
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
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
  const [cleanupTarget, setCleanupTarget] = useState<AlbumRow | null>(null);
  const [replacementTarget, setReplacementTarget] = useState<LibraryTrack | null>(null);

  // `background` fetches (disk-reconciliation polling) must not toggle the
  // blocking spinner or clear errors -- they silently swap in the richer tree.
  const loadLibrary = useCallback(async (includeTracks = false, background = false) => {
    if (!background) {
      setLoading(true);
      setError('');
    }
    try {
      const suffix = includeTracks ? '&include_tracks=1' : '';
      const next = await apiGet<LibraryResponse>(`/api/library?include_disk_only=1${suffix}`);
      setData(next);
    } catch (err) {
      if (!background) setError(errorMessage(err));
    } finally {
      if (!background) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);
  const engineOffline = useEngineOffline();
  // Bumped when the engine comes back, to reload the art repair status too.
  const [engineReturns, setEngineReturns] = useState(0);
  useReloadOnEngineReturn(useCallback(() => {
    void loadLibrary();
    setEngineReturns((n) => n + 1);
  }, [loadLibrary]));

  // The page paints from whatever /api/library returns first, then polls in the
  // background (bounded, backing off) while that payload is not yet fresh:
  //   - the fast engine-only tree arrives as `reconciled:false` on a cold cache;
  //   - a cache served past its TTL arrives with `reconciled:true` AND
  //     `reconciling:true` -- it looks reconciled but still shows STALE disk
  //     state while the backend refreshes it in a background job.
  // Either case means the visible disk state is provisional, so keep polling
  // until the payload is fresh (reconciled && !reconciling) -- at which point an
  // advanced engine tree has landed -- then stop. Polls are background fetches so
  // they never show the blocking spinner. The budget is NOT reset when
  // library_version changes: a backend that churned the version on every poll
  // would otherwise poll forever; instead it terminates at the stall/retry UI.
  const RECONCILE_POLL_BASE_MS = 2_000;
  const RECONCILE_POLL_MAX_MS = 15_000;
  const MAX_RECONCILE_POLLS = 15;

  const needsReconcile = Boolean(data && (data.reconciled === false || data.reconciling === true));
  const tracksIncludedForPoll = Boolean(data?.tracks_included);
  const [reconcilePolls, setReconcilePolls] = useState(0);

  const reconcilePollDelay =
    needsReconcile && reconcilePolls < MAX_RECONCILE_POLLS
      ? Math.min(RECONCILE_POLL_BASE_MS * 2 ** reconcilePolls, RECONCILE_POLL_MAX_MS)
      : null;
  const reconcilePollActive = reconcilePollDelay !== null;
  const reconcileStalled = needsReconcile && !reconcilePollActive;

  useInterval(() => {
    setReconcilePolls((n) => n + 1);
    void loadLibrary(tracksIncludedForPoll, true);
  }, reconcilePollDelay);

  useEffect(() => {
    let cancelled = false;
    async function loadArtRepair() {
      try {
        const report = await getLibraryArtRepairReport();
        if (!cancelled) {
          setArtRepairReport(report);
          setArtRepairError('');
        }
      } catch (err) {
        if (!cancelled) setArtRepairError(errorMessage(err));
      }
    }
    void loadArtRepair();
    return () => {
      cancelled = true;
    };
  }, [engineReturns]);

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
        if (!cancelled) setTrackSearchError(errorMessage(err));
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
          setDetailsError(errorMessage(err));
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
  const loadFailed = !data && Boolean(error);
  // Up to eight sleeves for the header shelf, taken from the albums already loaded (no extra request).
  const shelfCovers = useMemo(() => {
    const urls: string[] = [];
    for (const row of allAlbumRows) {
      const url = albumArtUrl(row.album, libraryVersion);
      if (url && !urls.includes(url)) urls.push(url);
      if (urls.length >= 8) break;
    }
    return urls;
  }, [allAlbumRows, libraryVersion]);
  const shownError = panelError(error, engineOffline);

  return (
    <div className="space-y-4">
      {/* Page header straight on the shelf: no panel around the page's own title. */}
      <div className="border-b border-graphite-800 pb-4">
        <div className="flex flex-col-reverse gap-4 md:flex-row md:items-end md:justify-between">
          <div className="min-w-0">
            <h1 className="lib-title">Browse music library</h1>
            <p className="lib-catalog">
              {/* No library loaded yet, or the load failed: counts are unknown, not zero. */}
              <span>{`${data ? numberFmt.format(data.stats.artists) : '—'} artists`}</span>
              <span>{`${data ? numberFmt.format(data.stats.albums) : '—'} albums`}</span>
              <span>{`${data ? numberFmt.format(data.stats.tracks) : '—'} tracks`}</span>
              {!data ? null : attentionAlbumCount ? (
                <span className="lib-review is-flag">{`${numberFmt.format(attentionAlbumCount)} albums to review`}</span>
              ) : (
                <span className="lib-review is-ok">No review issues</span>
              )}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-0.5 text-xs text-muted">
              <span>{data?.tracks_included ? 'Track search loaded' : 'Fast library summary'}</span>
              {reconcilePollActive ? (
                <span className="flex items-center gap-1 text-warning" aria-live="polite">
                  <CircularProgress size={12} color="inherit" />
                  Checking disk for missing &amp; not-imported files…
                </span>
              ) : reconcileStalled ? (
                <span className="flex items-center gap-1 text-zinc-400" aria-live="polite">
                  Disk check incomplete
                  <button
                    type="button"
                    className="inline-flex min-h-8 items-center px-1 text-zinc-300 underline hover:text-zinc-100 active:text-brass-300"
                    onClick={() => setReconcilePolls(0)}
                  >
                    retry
                  </button>
                </span>
              ) : null}
              <button
                type="button"
                className="inline-flex min-h-8 items-center text-sm text-zinc-300 underline underline-offset-2 hover:text-zinc-100 active:text-brass-300"
                onClick={() => handleNavigate('/library/missing-ids')}
              >
                Missing MusicBrainz IDs
              </button>
            </div>
          </div>
          {/* Inside an artist the shelf steps aside so the artist's own header leads. */}
          {selectedArtist ? null : <CoverShelf urls={shelfCovers} />}
        </div>
        {loading ? <LinearProgress sx={{ mt: 2, borderRadius: 1 }} /> : null}
      </div>

      <NextRepairPanel
        row={selectedArtist ? null : nextRepairRow}
        artRepairAlbumIds={artRepairAlbumIds}
        artRepairItemMap={artRepairItemMap}
        libraryVersion={libraryVersion}
        onDetails={setDetailsRow}
        onFocusStatus={handleStatusChange}
        onNavigate={handleNavigate}
      />

      {loadFailed ? null : (
        <StatusCards
          active={activeStatus}
          counts={statusCounts}
          onChange={handleStatusChange}
        />
      )}

      {/* What the list below is showing, as one plain line over the search fields. */}
      {loadFailed ? null : <div className="flex min-h-8 flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className={activeStatus === 'all' ? 'font-semibold text-zinc-100' : 'font-semibold text-brass-300'}>
          {activeStatus === 'all' ? 'Browsing artists' : `Active filter: ${activeFilter.label}`}
        </span>
        {selectedArtist ? <span className="text-zinc-200">{`Artist: ${displayArtistName(selectedArtist)}`}</span> : null}
        <span className="text-muted">
          {activeStatus === 'all'
            ? `${numberFmt.format(filteredArtists.length)} artists visible, ${numberFmt.format(attentionArtistCount)} with review items`
            : `${numberFmt.format(albumResults.length)} matching albums`}
        </span>
        {activeStatus !== 'all' ? (
          <Button size="small" variant="text" onClick={() => setActiveStatus('all')}>
            Clear filter
          </Button>
        ) : null}
      </div>}

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

      {shownError ? <Alert severity="error">{shownError}</Alert> : null}
      {artRepairError ? <Alert severity="warning">Art repair status could not be loaded: {artRepairError}</Alert> : null}
      {trackSearchError ? <Alert severity="warning">Track search rows could not be loaded: {trackSearchError}</Alert> : null}

      {/* Under a load error there is no list: "No matching artists" would claim an empty library. */}
      {loadFailed ? null : activeStatus !== 'all' ? (
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
          onCleanAlbum={setCleanupTarget}
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
          onCleanAlbum={setCleanupTarget}
        />
      ) : (
        <ArtistBrowser
          artists={filteredArtists}
          letter={artistLetterFilter}
          libraryVersion={libraryVersion}
          loading={loading && !data}
          grouped={artistSort === 'alpha'}
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
        onCleanAlbum={setCleanupTarget}
        onNavigate={handleNavigate}
        onReplaceTrack={setReplacementTarget}
      />

      <AlbumCleanupModal
        open={Boolean(cleanupTarget)}
        albumId={Number(cleanupTarget?.album.album_id || 0)}
        albumTitle={cleanupTarget ? displayAlbumTitle(cleanupTarget.album) : ''}
        artistName={cleanupTarget ? displayArtistName(cleanupTarget.artist) : ''}
        mbReleaseGroupId={cleanupTarget?.album.mb_releasegroupid}
        onClose={() => setCleanupTarget(null)}
        onSuccess={() => void loadLibrary()}
      />

      <TrackReplacementModal
        open={Boolean(replacementTarget)}
        itemId={Number(replacementTarget?.id || 0)}
        trackTitle={replacementTarget ? (stripStamps(replacementTarget.title) || 'Untitled track') : ''}
        originalPath={replacementTarget?.path || ''}
        onClose={() => setReplacementTarget(null)}
        onSuccess={() => void loadLibrary()}
      />
    </div>
  );
}
