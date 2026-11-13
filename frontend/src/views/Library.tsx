import { errorMessage, panelError } from '../lib/errors';
import {
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
} from '@headlessui/react';
import Alert from '@mui/material/Alert';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { getAlbumTracks, getArtistImageUrl } from '../api/client';
import { apiGet } from '../lib/api';
import { useLocation, useNavigate, useSearchParams } from 'react-router';
import { useInterval } from '../lib/hooks';
import type { LibraryAlbum, LibraryArtist, LibraryResponse, LibraryTrack } from '../types/api';
import { useEngineOffline, useReloadOnEngineReturn } from '../components/EngineOfflineBanner';
import CoverFlow, { type CoverFlowItem } from '../components/CoverFlow';

type SearchScope = 'all' | 'artists' | 'albums' | 'tracks';
type ArtistSort = 'alpha' | 'albums' | 'tracks';
type AlbumSort = 'artist' | 'album' | 'year';
type ArtistLetter = 'all' | 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H' | 'I' | 'J' | 'K' | 'L' | 'M' | 'N' | 'O' | 'P' | 'Q' | 'R' | 'S' | 'T' | 'U' | 'V' | 'W' | 'X' | 'Y' | 'Z' | '#';

type LibraryView = 'coverflow' | 'grid';
export const LIBRARY_VIEW_KEY = 'library.view';

function readLibraryView(): LibraryView {
  try {
    return localStorage.getItem(LIBRARY_VIEW_KEY) === 'grid' ? 'grid' : 'coverflow';
  } catch {
    return 'coverflow';
  }
}

function saveLibraryView(view: LibraryView) {
  try {
    localStorage.setItem(LIBRARY_VIEW_KEY, view);
  } catch {
    /* storage unavailable: the choice lasts this visit only */
  }
}

type AlbumRow = {
  album: LibraryAlbum;
  artist: LibraryArtist;
};

const numberFmt = new Intl.NumberFormat();
const countLabel = (n: number, noun: string) => `${numberFmt.format(n)} ${noun}${n === 1 ? '' : 's'}`;
const ARTIST_LETTERS: ArtistLetter[] = ['all', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', '#'];

const artistSortOptions: Array<{ value: ArtistSort; label: string }> = [
  { value: 'alpha', label: 'Alphabetical' },
  { value: 'albums', label: 'Most albums' },
  { value: 'tracks', label: 'Most tracks' },
];

const albumSortOptions: Array<{ value: AlbumSort; label: string }> = [
  { value: 'artist', label: 'Artist' },
  { value: 'album', label: 'Album title' },
  { value: 'year', label: 'Year' },
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

// Folder image the library scan found on disk, tried when the album art route fails.
function albumDiskArtFallbackUrl(album: LibraryAlbum, libraryVersion?: number) {
  if (!album.album_id || !album.disk_art) return '';
  return addVersion(`/api/disk-art?path=${encodeURIComponent(album.disk_art)}`, libraryVersion);
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
  fallbackSrc = '',
  label,
  fallback,
  className,
}: {
  src: string;
  fallbackSrc?: string;
  label: string;
  fallback: ReactNode;
  className: string;
}) {
  const [failures, setFailures] = useState(0);

  useEffect(() => {
    setFailures(0);
  }, [src, fallbackSrc]);

  const current = failures === 0 ? src : failures === 1 ? fallbackSrc : '';

  return (
    <div className={cx('flex shrink-0 items-center justify-center overflow-hidden bg-graphite-900 text-xs font-semibold text-zinc-300', className)}>
      {current ? (
        <img
          alt={label}
          className="h-full w-full object-cover"
          loading="lazy"
          src={current}
          onError={() => setFailures((n) => n + 1)}
        />
      ) : (
        // No portrait: initials printed on a record label, grooves around it (authored CSS, no image).
        typeof fallback === 'string' ? (
          // Initials keep the image's accessible name, so a card named by its art reads the same either way.
          <span aria-label={label} className="lib-groove flex h-full w-full items-center justify-center" role="img">
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
  // Crate divider tabs standing on the bin's edge, one per initial: one scrolling row on a phone, wrapping from sm up.
  return (
    <div
      aria-label="Artists by initial"
      className="crate-tabs flex gap-1 overflow-x-auto sm:flex-wrap sm:overflow-x-visible"
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
            {countLabel(artist.albums.length, 'album')} - {countLabel(countTracks(artist), 'track')}
          </span>
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
                  key={`${artist.name}::${artist.path || `${groupIndex}-${index}`}`}
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
  libraryVersion,
  showArtist,
  onDetails,
}: {
  row: AlbumRow;
  libraryVersion?: number;
  showArtist: boolean;
  onDetails: (row: AlbumRow) => void;
}) {
  const { album, artist } = row;
  const title = displayAlbumTitle(album);
  const artistName = displayArtistName(artist);

  return (
    // Open tile: the sleeve leads, the words sit under it on the shelf, no box around either.
    // Library is browse-only: the sleeve itself opens the album, and there are no actions here.
    <article className="flex min-w-0 flex-col">
      {/* A sleeve standing in the crate; pulling it out flips it over to its back cover. */}
      <button
        aria-haspopup="dialog"
        aria-label={`Open ${title}`}
        className="sleeve block aspect-square w-full cursor-pointer rounded-sm motion-safe:transition-transform motion-safe:hover:-translate-y-0.5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brass-300"
        type="button"
        onClick={() => onDetails(row)}
      >
        <Artwork
          className="sleeve-face h-full w-full"
          fallback={<SleevePlaceholder />}
          label={`${title} cover`}
          fallbackSrc={albumDiskArtFallbackUrl(album, libraryVersion)}
          src={albumArtUrl(album, libraryVersion)}
        />
      </button>
      <div className="mt-2 min-w-0 flex-1">
        <h3 className="mt-1 line-clamp-2 text-sm font-semibold text-zinc-100" title={title}>{title}</h3>
        {showArtist ? <div className="truncate text-sm text-zinc-300" title={artistName}>{artistName}</div> : null}
        <div className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-zinc-400">
          {album.year ? <span>{String(album.year).slice(0, 4)}</span> : null}
          <span>{numberFmt.format(albumTrackCount(album))} tracks</span>
        </div>
      </div>
    </article>
  );
}

function AlbumGrid({
  rows,
  title,
  subtitle,
  libraryVersion,
  showArtist,
  onDetails,
}: {
  rows: AlbumRow[];
  title: string;
  subtitle: string;
  libraryVersion?: number;
  showArtist: boolean;
  onDetails: (row: AlbumRow) => void;
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
                libraryVersion={libraryVersion}
                showArtist={showArtist}
                onDetails={onDetails}
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

// Cover Flow and Grid are two views of the same list; the toggle only changes how it is laid out.
function ViewToggle({ view, onChange }: { view: LibraryView; onChange: (view: LibraryView) => void }) {
  return (
    <div aria-label="Library view" className="crate-tabs ml-auto flex gap-1" role="group">
      {(['coverflow', 'grid'] as const).map((option) => (
        <button
          key={option}
          aria-pressed={view === option}
          className={cx('crate-tab h-8 px-3 text-xs', view === option && 'font-semibold')}
          type="button"
          onClick={() => onChange(option)}
        >
          {option === 'coverflow' ? 'Cover Flow' : 'Grid'}
        </button>
      ))}
    </div>
  );
}

// An artist's sleeve in the flow: the artist image or first album cover the library payload already
// carries (no extra request), else a lettered placard on sleeve paper.
function ArtistFlowArt({ artist, libraryVersion }: { artist: LibraryArtist; libraryVersion?: number }) {
  const name = displayArtistName(artist);
  return (
    <Artwork
      className="h-full w-full"
      fallback={<span className="cf-placard h-full w-full"><span>{name}</span></span>}
      label=""
      src={artistArtUrl(artist, libraryVersion)}
    />
  );
}

function ArtistFlow({
  artists,
  centerKey,
  libraryVersion,
  loading,
  autoFocus,
  onCenterKeyChange,
  onSelectArtist,
}: {
  artists: LibraryArtist[];
  centerKey: string;
  libraryVersion?: number;
  loading: boolean;
  autoFocus: boolean;
  onCenterKeyChange: (key: string) => void;
  onSelectArtist: (artist: LibraryArtist) => void;
}) {
  const availableLetters = useMemo(() => new Set(artists.map(artistLetter)), [artists]);
  const index = Math.max(0, artists.findIndex((artist) => artist.name === centerKey));
  const current = artists[index];
  const n = artists.length;
  const item = (i: number): CoverFlowItem => {
    const artist = artists[i];
    const name = displayArtistName(artist);
    return {
      key: artist.name,
      label: `Artist ${name}, ${i + 1} of ${n}`,
      title: name,
      subtitle: `${countLabel(artist.albums.length, 'album')} - ${countLabel(countTracks(artist), 'track')}`,
      art: <ArtistFlowArt artist={artist} libraryVersion={libraryVersion} />,
    };
  };

  return (
    <section className="space-y-2">
      {/* In the flow a letter jumps to its first artist instead of filtering; the centred artist's letter shows pressed. */}
      <AlphabetIndex
        active={current ? artistLetter(current) : 'all'}
        available={availableLetters}
        onChange={(letter) => {
          const target = letter === 'all' ? artists[0] : artists.find((artist) => artistLetter(artist) === letter);
          if (target) onCenterKeyChange(target.name);
        }}
      />
      <h2 className="border-b border-graphite-800 pb-2 text-sm font-semibold text-zinc-300">
        Artists - {numberFmt.format(n)}
      </h2>
      {loading ? (
        <div className="crate rounded-sm p-4 text-sm text-zinc-300">Loading library…</div>
      ) : n ? (
        <CoverFlow
          count={n}
          index={index}
          item={item}
          autoFocus={autoFocus}
          label="Artists"
          onActivate={(i) => onSelectArtist(artists[i])}
          onIndexChange={(i) => onCenterKeyChange(artists[i].name)}
        />
      ) : (
        <div className="crate rounded-sm p-4 text-sm text-zinc-300">No matching artists.</div>
      )}
    </section>
  );
}

function albumRowKey(row: AlbumRow) {
  return String(row.album.album_id || row.album.aldir || row.album.album);
}

function AlbumFlow({
  rows,
  centerKey,
  libraryVersion,
  autoFocus,
  onCenterKeyChange,
  onDetails,
  onBack,
}: {
  rows: AlbumRow[];
  centerKey: string;
  libraryVersion?: number;
  autoFocus: boolean;
  onCenterKeyChange: (key: string) => void;
  onDetails: (row: AlbumRow) => void;
  onBack: () => void;
}) {
  const index = Math.max(0, rows.findIndex((row) => albumRowKey(row) === centerKey));
  const n = rows.length;
  const item = (i: number): CoverFlowItem => {
    const { album, artist } = rows[i];
    const title = displayAlbumTitle(album);
    return {
      key: albumRowKey(rows[i]),
      label: `Album ${title} by ${displayArtistName(artist)}, ${i + 1} of ${n}`,
      title,
      subtitle: [album.year ? String(album.year).slice(0, 4) : '', countLabel(albumTrackCount(album), 'track')].filter(Boolean).join(' - '),
      art: (
        <Artwork
          className="h-full w-full"
          fallback={<SleevePlaceholder />}
          fallbackSrc={albumDiskArtFallbackUrl(album, libraryVersion)}
          label=""
          src={albumArtUrl(album, libraryVersion)}
        />
      ),
    };
  };

  return (
    <section className="space-y-3">
      {/* Navigation only: back to the artist flow, centred on this artist. Escape or Backspace on the flow does the same. */}
      <button className="cf-back" type="button" onClick={onBack}>
        <span aria-hidden="true">←</span> All artists
      </button>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-graphite-800 pb-2">
        <h2 className="font-display text-lg font-semibold text-zinc-100">Albums</h2>
        <p className="text-sm text-zinc-400">{countLabel(n, 'album')}</p>
      </div>
      {n ? (
        <CoverFlow
          autoFocus={autoFocus}
          count={n}
          index={index}
          item={item}
          label="Albums"
          onActivate={(i) => onDetails(rows[i])}
          onBack={onBack}
          onIndexChange={(i) => onCenterKeyChange(albumRowKey(rows[i]))}
        />
      ) : (
        <div className="crate rounded-sm p-4 text-sm text-zinc-400">No matching albums.</div>
      )}
    </section>
  );
}

function ArtistDetail({
  artist,
  rows,
  libraryVersion,
  view,
  albumKey,
  autoFocus,
  onAlbumKeyChange,
  onBack,
  onDetails,
}: {
  artist: LibraryArtist;
  rows: AlbumRow[];
  libraryVersion?: number;
  view: LibraryView;
  autoFocus: boolean;
  albumKey: string;
  onAlbumKeyChange: (key: string) => void;
  onBack: () => void;
  onDetails: (row: AlbumRow) => void;
}) {
  const name = displayArtistName(artist);
  const artUrl = useArtistArtUrl(artist, libraryVersion);
  const flow = view === 'coverflow';

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
      {/* In Cover Flow the header is one compact row, so the flow sits high enough on a phone. */}
      <div
        className={cx(
          'relative isolate -mx-3 flex overflow-hidden px-3 sm:-mx-4 sm:px-4',
          flow ? 'flex-row items-center gap-3 py-3' : 'flex-col gap-4 py-6 sm:flex-row sm:items-end sm:py-8',
        )}
      >
        <CoverBackdrop src={artUrl} />
        <Artwork
          className={cx('rounded-sm ring-1 ring-black/40', flow ? 'h-14 w-14 sm:h-20 sm:w-20' : 'h-28 w-28 sm:h-36 sm:w-36')}
          fallback={initials(name) || 'A'}
          label={`${name} artwork`}
          src={artUrl}
        />
        <div className="min-w-0 flex-1">
          <h2
            className={cx(
              'font-display font-semibold text-zinc-50 [overflow-wrap:anywhere]',
              flow ? 'text-xl sm:text-3xl' : 'text-3xl sm:text-4xl',
            )}
          >
            {name}
          </h2>
          {/* The same catalogue line as the page header, scoped to this artist. */}
          <p className="lib-catalog">
            <span>{countLabel(artist.albums.length, 'album')}</span>
            <span>{countLabel(countTracks(artist), 'track')}</span>
          </p>
        </div>
      </div>

      {view === 'coverflow' ? (
        <AlbumFlow
          autoFocus={autoFocus}
          centerKey={albumKey}
          libraryVersion={libraryVersion}
          rows={rows}
          onBack={onBack}
          onCenterKeyChange={onAlbumKeyChange}
          onDetails={onDetails}
        />
      ) : (
        <AlbumGrid
          rows={rows}
          title="Albums"
          subtitle={`${numberFmt.format(rows.length)} visible for ${name}`}
          libraryVersion={libraryVersion}
          showArtist={false}
          onDetails={onDetails}
        />
      )}
    </section>
  );
}

// The one issue the Library shows: a track whose file is missing prints faded and struck through.
function trackIsMissing(track: LibraryTrack) {
  return Boolean(track.missing || track.status === 'missing_file');
}

function catalogNumber(album: LibraryAlbum) {
  const rg = String(album.mb_releasegroupid || '').replace(/-/g, '').trim();
  return rg.length >= 8 ? `Cat. No. ${rg.slice(0, 4).toUpperCase()}-${rg.slice(4, 8).toUpperCase()}` : '';
}

function prefersReducedMotion() {
  return typeof window !== 'undefined' && Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
}

// The flip back takes as long as the flip over (CSS: sleeve-flip / sleeve-fade); then the dialog closes.
const FLIP_MS = 620;
const FADE_MS = 180;

function SleeveSide({ label, tracks, startIndex, multiDisc }: { label: string; tracks: LibraryTrack[]; startIndex: number; multiDisc: boolean }) {
  if (!tracks.length) return null;
  return (
    <section className="sleeve-side">
      <h3 className="sleeve-side-title">{label}</h3>
      <ol>
        {tracks.map((track, offset) => {
          const index = startIndex + offset;
          const missing = trackIsMissing(track);
          return (
            <li key={`${track.id || index}-${track.title}`} className={cx('sleeve-track', missing && 'is-missing')}>
              <span className="sleeve-track-no">{multiDisc && track.disc ? `${track.disc}.` : ''}{track.track || index + 1}</span>
              <span className="sleeve-track-title">
                {stripStamps(track.title) || 'Untitled track'}
                {missing ? <span className="sleeve-track-note">missing</span> : null}
              </span>
              <span className="sleeve-track-time">{formatLength(track.length)}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function AlbumDetailsDialog({
  row,
  tracks,
  loading,
  error,
  libraryVersion,
  onClose,
  onCleanAlbum,
  onReplaceTrack,
}: {
  row: AlbumRow | null;
  tracks: LibraryTrack[];
  loading: boolean;
  error: string;
  libraryVersion?: number;
  onClose: () => void;
  onCleanAlbum?: (row: AlbumRow) => void;
  onReplaceTrack?: (track: LibraryTrack) => void;
}) {
  const album = row?.album ?? null;
  const artist = row?.artist ?? null;
  const title = album ? displayAlbumTitle(album) : '';
  const artistName = artist ? displayArtistName(artist) : '';
  const [closing, setClosing] = useState(false);
  // The back cover is printed over the album's own art (the same URL as the front, so no new
  // fetch); when there is no art, or it fails, it falls back to plain aged paper.
  const [backArtFailures, setBackArtFailures] = useState(0);
  const titleRef = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    setClosing(false);
    setBackArtFailures(0);
  }, [row]);

  // Esc, the Close control and a click on the table all turn the sleeve back over, then close.
  function requestClose(after?: () => void) {
    if (closing) return;
    setClosing(true);
    window.setTimeout(() => {
      onClose();
      after?.();
    }, prefersReducedMotion() ? FADE_MS : FLIP_MS);
  }

  const coverUrl = album ? albumArtUrl(album, libraryVersion) : '';
  const coverFallbackUrl = album ? albumDiskArtFallbackUrl(album, libraryVersion) : '';
  const backArt = backArtFailures === 0 ? coverUrl : backArtFailures === 1 ? coverFallbackUrl : '';
  const multiDisc = tracks.some((track) => Number(track.disc || 1) > 1);
  const half = Math.ceil(tracks.length / 2);
  const totalSeconds = tracks.reduce((sum, track) => sum + (Number.isFinite(track.length) ? Number(track.length) : 0), 0);
  const catalog = album ? catalogNumber(album) : '';
  const replaceable = onReplaceTrack ? tracks.filter((track) => track.id && track.path && !track.missing) : [];

  // Opening an album pulls its sleeve from the crate and turns it over: the front (the same cover as
  // in the crate) rotates away and the back cover, printed with the tracklist, faces the listener.
  return (
    <Dialog className="relative z-50" initialFocus={titleRef} open={Boolean(row)} onClose={() => requestClose()}>
      <DialogBackdrop className="sleeve-table fixed inset-0" />
      <div className="fixed inset-0 overflow-y-auto p-3 sm:p-6">
        <div className="flex min-h-full items-center justify-center">
          <DialogPanel className="flip-card">
            {album && artist ? (
              <div className={cx('flip-inner', closing && 'is-closing')}>
                <div aria-hidden="true" className="flip-face flip-front" inert>
                  <Artwork
                    className="h-full w-full"
                    fallback={<SleevePlaceholder />}
                    label=""
                    fallbackSrc={albumDiskArtFallbackUrl(album, libraryVersion)}
                    src={coverUrl}
                  />
                </div>
                <div className={cx('flip-face flip-back', backArt && 'has-art')}>
                  {backArt ? (
                    <img
                      alt=""
                      aria-hidden="true"
                      className="sleeve-back-art"
                      src={backArt}
                      onError={() => setBackArtFailures((n) => n + 1)}
                    />
                  ) : null}
                  <header className="sleeve-head">
                    <div className="min-w-0">
                      <p className="sleeve-artist">{artistName}</p>
                      <DialogTitle ref={titleRef} className="sleeve-title focus:outline-none" tabIndex={-1}>
                        {title}
                      </DialogTitle>
                    </div>
                    <div className="flex shrink-0 items-start gap-2">
                      {album.album_id && onCleanAlbum ? (
                        <button
                          className="sleeve-print-button"
                          type="button"
                          onClick={() => requestClose(() => onCleanAlbum(row!))}
                        >
                          Remove from Library…
                        </button>
                      ) : null}
                      <button className="sleeve-print-button" type="button" onClick={() => requestClose()}>Close</button>
                    </div>
                  </header>

                  {/* The paper area scrolls inside the square when a tracklist runs long. */}
                  <div aria-label="Tracklist" className="sleeve-body" role="region" tabIndex={0}>
                    {loading ? <p className="sleeve-small" aria-live="polite">Loading the tracklist…</p> : null}
                    {error ? <p className="sleeve-error" role="alert">{`Tracklist could not be loaded: ${error}`}</p> : null}
                    {!loading && !error && !tracks.length ? <p className="sleeve-small">No track rows loaded for this album.</p> : null}
                    {!loading && tracks.length ? (
                      <div className="sleeve-sides">
                        <SleeveSide label="Side A" tracks={tracks.slice(0, half)} startIndex={0} multiDisc={multiDisc} />
                        <SleeveSide label="Side B" tracks={tracks.slice(half)} startIndex={half} multiDisc={multiDisc} />
                      </div>
                    ) : null}

                    {replaceable.length ? (
                      <ul className="sleeve-small mt-3 flex flex-wrap gap-x-3">
                        {replaceable.map((track) => (
                          <li key={track.id}>
                            <button className="sleeve-print-button" type="button" onClick={() => onReplaceTrack?.(track)}>
                              {`Replace ${stripStamps(track.title) || 'track'}…`}
                            </button>
                          </li>
                        ))}
                      </ul>
                    ) : null}

                    {/* The small print along the foot of the sleeve. */}
                    <footer className="sleeve-foot">
                      <p className="sleeve-small">
                        <span>{artistName}</span>
                        <span>{title}</span>
                        {album.year ? <span className="tabular-nums">{String(album.year).slice(0, 4)}</span> : null}
                        <span>{countLabel(albumTrackCount(album), 'track')}</span>
                        {totalSeconds ? <span className="tabular-nums">{`Total time ${formatLength(totalSeconds)}`}</span> : null}
                        {catalog ? <span className="tabular-nums">{catalog}</span> : null}
                      </p>
                    </footer>
                  </div>
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
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<SearchScope>('all');
  const [artistSort, setArtistSort] = useState<ArtistSort>('alpha');
  const [albumSort, setAlbumSort] = useState<AlbumSort>('artist');
  const [artistLetterFilter, setArtistLetterFilter] = useState<ArtistLetter>('all');
  // The open artist lives in the URL (?artist=<name>): drilling in pushes a history entry, so browser
  // Back and phone swipe-back return to the artists, and a link to an artist opens it.
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedArtistName = searchParams.get('artist') ?? '';
  // An in-page drill-in marks its history entry, so the in-page back can pop it instead of pushing a
  // second entry (which would make browser Back re-open the artist).
  const location = useLocation();
  const navigate = useNavigate();
  const setSelectedArtistName = useCallback((name: string, replace = false) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (name) next.set('artist', name);
      else next.delete('artist');
      return next;
    }, { replace, state: name && !replace ? { libraryDrillIn: true } : undefined });
  }, [setSearchParams]);
  const [trackSearchLoading, setTrackSearchLoading] = useState(false);
  const [trackSearchError, setTrackSearchError] = useState('');
  const [trackSearchTried, setTrackSearchTried] = useState(false);
  const [detailsRow, setDetailsRow] = useState<AlbumRow | null>(null);
  const [detailsTracks, setDetailsTracks] = useState<LibraryTrack[]>([]);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState('');
  const [view, setView] = useState<LibraryView>(readLibraryView);
  // The flows remember their centred sleeve by key, so a background reload never yanks them back.
  // Seeded from the URL, so leaving a deep-linked artist (button or browser Back) centres that artist.
  const [artistKey, setArtistKey] = useState(() => searchParams.get('artist') ?? '');
  // Focus follows an in-page drill-in or back, so the keyboard keeps working; a page load does not grab focus.
  const [flowFocus, setFlowFocus] = useState(false);
  const [albumKey, setAlbumKey] = useState('');
  // A new search or sort sends both flows to the first match.
  function resetFlows() {
    setArtistKey('');
    setAlbumKey('');
  }
  function changeView(next: LibraryView) {
    setView(next);
    saveLibraryView(next);
  }

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
  useReloadOnEngineReturn(useCallback(() => {
    void loadLibrary();
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
  // would otherwise poll forever; instead it stops at the budget, and the next visit polls again.
  // The Library shows no status for this (owner rule: browse-only, no issue indicators).
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

  useInterval(() => {
    setReconcilePolls((n) => n + 1);
    void loadLibrary(tracksIncludedForPoll, true);
  }, reconcilePollDelay);

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

  const allAlbumRows = useMemo(() => {
    const rows: AlbumRow[] = [];
    for (const artist of artists) {
      for (const album of artist.albums ?? []) {
        rows.push({ artist, album });
      }
    }
    return rows;
  }, [artists]);

  const filteredArtists = useMemo(() => {
    const result = artists.filter((artist) => artistMatchesQuery(artist, query, scope));
    result.sort((a, b) => {
      if (artistSort === 'albums') return b.albums.length - a.albums.length || displayArtistName(a).localeCompare(displayArtistName(b));
      if (artistSort === 'tracks') return countTracks(b) - countTracks(a) || displayArtistName(a).localeCompare(displayArtistName(b));
      return displayArtistName(a).localeCompare(displayArtistName(b));
    });
    return result;
  }, [artistSort, artists, query, scope]);

  const selectedArtist = useMemo(
    () => artists.find((artist) => artist.name === selectedArtistName) ?? null,
    [artists, selectedArtistName],
  );

  useEffect(() => {
    // Only once the library has loaded: before that every artist is "missing".
    if (data && selectedArtistName && !selectedArtist) setSelectedArtistName('', true);
  }, [data, selectedArtist, selectedArtistName, setSelectedArtistName]);

  const sortAlbumRows = useCallback((rows: AlbumRow[]) => {
    const sorted = [...rows];
    sorted.sort((a, b) => {
      if (albumSort === 'album') return displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album)) || displayArtistName(a.artist).localeCompare(displayArtistName(b.artist));
      if (albumSort === 'year') return Number(b.album.year || 0) - Number(a.album.year || 0) || displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album));
      return displayArtistName(a.artist).localeCompare(displayArtistName(b.artist)) || displayAlbumTitle(a.album).localeCompare(displayAlbumTitle(b.album));
    });
    return sorted;
  }, [albumSort]);

  const selectedArtistRows = useMemo(() => {
    if (!selectedArtist) return [];
    return sortAlbumRows(
      selectedArtist.albums
        .map((album) => ({ artist: selectedArtist, album }))
        .filter((row) => albumMatchesQuery(row.album, row.artist, query, scope)),
    );
  }, [query, scope, selectedArtist, sortAlbumRows]);

  const albumMode = Boolean(selectedArtist);
  function handleSelectArtist(artist: LibraryArtist) {
    setSelectedArtistName(artist.name);
    setArtistKey(artist.name);
    setAlbumKey('');
    setFlowFocus(true);
  }
  function backToArtists() {
    if (selectedArtistName) setArtistKey(selectedArtistName);
    setFlowFocus(true);
    if ((location.state as { libraryDrillIn?: boolean } | null)?.libraryDrillIn) navigate(-1);
    else setSelectedArtistName('', true); // deep link: nothing of ours to pop
  }

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
              <span>{data ? countLabel(data.stats.artists, 'artist') : '— artists'}</span>
              <span>{data ? countLabel(data.stats.albums, 'album') : '— albums'}</span>
              <span>{data ? countLabel(data.stats.tracks, 'track') : '— tracks'}</span>
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-0.5 text-xs text-muted">
              <span>{data?.tracks_included ? 'Track search loaded' : 'Fast library summary'}</span>
            </div>
          </div>
          {/* Inside an artist the shelf steps aside so the artist's own header leads. */}
          {selectedArtist ? null : <CoverShelf urls={shelfCovers} />}
        </div>
        {loading ? <LinearProgress sx={{ mt: 2, borderRadius: 1 }} /> : null}
      </div>

      {/* What the list below is showing, as one plain line over the search fields. */}
      {loadFailed ? null : <div className="flex min-h-8 flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="font-semibold text-zinc-100">Browsing artists</span>
        {selectedArtist ? <span className="text-zinc-200">{`Artist: ${displayArtistName(selectedArtist)}`}</span> : null}
        <span className="text-muted">
          {`${numberFmt.format(filteredArtists.length)} artists visible`}
        </span>
        <ViewToggle view={view} onChange={changeView} />
      </div>}

      <SearchRow
        query={query}
        scope={scope}
        artistSort={artistSort}
        albumSort={albumSort}
        albumMode={albumMode}
        trackSearchLoading={trackSearchLoading}
        onQueryChange={(value) => { setQuery(value); resetFlows(); }}
        onScopeChange={(value) => { setScope(value); resetFlows(); }}
        onArtistSortChange={(value) => { setArtistSort(value); resetFlows(); }}
        onAlbumSortChange={(value) => { setAlbumSort(value); setAlbumKey(''); }}
      />

      {shownError ? <Alert severity="error">{shownError}</Alert> : null}
      {trackSearchError ? <Alert severity="warning">Track search rows could not be loaded: {trackSearchError}</Alert> : null}

      {/* Under a load error there is no list: "No matching artists" would claim an empty library. */}
      {loadFailed ? null : selectedArtist ? (
        <ArtistDetail
          artist={selectedArtist}
          rows={selectedArtistRows}
          libraryVersion={libraryVersion}
          view={view}
          albumKey={albumKey}
          autoFocus={flowFocus}
          onAlbumKeyChange={setAlbumKey}
          onBack={backToArtists}
          onDetails={setDetailsRow}
        />
      ) : view === 'coverflow' ? (
        <ArtistFlow
          artists={filteredArtists}
          centerKey={artistKey}
          libraryVersion={libraryVersion}
          loading={loading && !data}
          autoFocus={flowFocus}
          onCenterKeyChange={setArtistKey}
          onSelectArtist={handleSelectArtist}
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
        libraryVersion={libraryVersion}
        onClose={() => setDetailsRow(null)}
      />
      {/* Remove from Library and per-track Replace (AlbumCleanupModal, TrackReplacementModal and the
          dialog's onCleanAlbum/onReplaceTrack props) are deliberately not wired here: Library is
          browse-only, and where they move is still undecided. */}
    </div>
  );
}
