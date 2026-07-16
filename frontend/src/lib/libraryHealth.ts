import type { LibraryAlbum, LibraryArtist } from '../types/api';

export type ProgressColor = 'success' | 'warning' | 'error' | 'secondary';

export interface AlbumHealth {
  imported: number;
  missing: number;
  notImported: number;
  extra: number;
  trackMbMissing: number;
  trackMbMismatched: number;
  duplicateRecordingIds: number;
  mbRepairable: number;
  releaseMbMissing: boolean;
  importPercent: number;
  label: string;
  color: ProgressColor;
  needsAttention: boolean;
  canImportRepair: boolean;
  canReviewMetadata: boolean;
}

export function albumHasNoTrackRows(album?: LibraryAlbum | null) {
  if (!album) return false;
  if (album.tracks_deferred) return false;
  return Number(album.track_count || 0) > 0 && !(album.tracks ?? []).length;
}

export function albumImportedCount(album?: LibraryAlbum | null) {
  if (!album || albumHasNoTrackRows(album)) return 0;
  const rows = album.tracks ?? [];
  if (rows.length) return rows.filter((track) => track.ok && !track.missing).length;
  return Math.max(0, Number(album.track_count || 0) - Number(album.missing || 0) - Number(album.not_imported || 0));
}

export function albumExpectedTrackCount(album?: LibraryAlbum | null) {
  if (!album) return 0;
  const explicit = Number(album.expected_track_count || 0);
  if (explicit > 0) return explicit;

  const perDisc = new Map<number, number>();
  for (const track of album.tracks ?? []) {
    const total = Number(track.tracktotal || 0);
    if (total <= 0 || total >= 300) continue;
    const disc = Number(track.disc || 1);
    perDisc.set(disc, Math.max(perDisc.get(disc) ?? 0, total));
  }

  return [...perDisc.values()].reduce((sum, total) => sum + total, 0);
}

export function albumNotImportedCount(album?: LibraryAlbum | null) {
  if (!album) return 0;
  return Number(album.not_imported || 0);
}

export function albumExtraCount(album?: LibraryAlbum | null) {
  if (!album || albumHasNoTrackRows(album)) return 0;
  const explicit = Number(album.extra_track_count ?? -1);
  if (explicit >= 0) return explicit;
  const expected = albumExpectedTrackCount(album);
  if (expected <= 0) return 0;
  return Math.max(0, albumImportedCount(album) - expected);
}

export function albumMissingCount(album?: LibraryAlbum | null) {
  if (!album) return 0;
  if (albumHasNoTrackRows(album)) return Number(album.track_count || 0);

  const rows = album.tracks ?? [];
  const flagged = Number(album.missing || 0);
  const trackMissing = rows.filter((track) => track.missing).length;

  // For local (tracktotal-derived) health, only count files physically missing
  // from disk. mb_missing_count and the tracktotal-derived estimate fire false
  // positives when the library has a different edition than what MusicBrainz
  // has on record (e.g. standard vs. deluxe).
  if ((album.mb_health_source ?? 'local') === 'local') {
    return Math.max(flagged, trackMissing);
  }

  // Exact MusicBrainz completeness gaps are not the same as local missing
  // files. They can mean the selected MB release is a different edition.
  return Math.max(flagged, trackMissing);
}

export function albumLooksComplete(album?: LibraryAlbum | null) {
  if (!album) return false;
  return (
    albumImportedCount(album) > 0 &&
    albumMissingCount(album) === 0 &&
    albumNotImportedCount(album) === 0 &&
    albumExtraCount(album) === 0 &&
    Number(album.mb_trackid_missing_count ?? 0) === 0 &&
    Number(album.mb_trackid_mismatch_count ?? 0) === 0 &&
    Number(album.mb_duplicate_recording_id_count ?? 0) === 0 &&
    Number(album.mb_repairable_count ?? 0) === 0 &&
    !albumHasNoTrackRows(album)
  );
}

export function getAlbumHealth(album: LibraryAlbum): AlbumHealth {
  const tracks = album.tracks ?? [];
  const imported = albumImportedCount(album);
  const expected = albumExpectedTrackCount(album);
  const missing = albumMissingCount(album);
  const notImported = albumNotImportedCount(album);
  const extra = albumExtraCount(album);
  const localTrackMbMissing = tracks.filter(
    (track) => track.ok && !track.missing && !String(track.mb_trackid ?? '').trim(),
  ).length;
  const trackMbMissing = Number(album.mb_trackid_missing_count ?? localTrackMbMissing);
  const trackMbMismatched = Number(album.mb_trackid_mismatch_count ?? 0);
  const duplicateRecordingIds = Number(album.mb_duplicate_recording_id_count ?? 0);
  const mbRepairable = Number(album.mb_repairable_count ?? (trackMbMissing + trackMbMismatched));
  const hasFixableMbIds = mbRepairable > 0 || trackMbMismatched > 0 || duplicateRecordingIds > 0;
  const releaseMbMissing = !String(album.mb_albumid ?? '').trim();
  const total = Math.max(expected, tracks.length, album.track_count, imported + missing + notImported, 1);
  const importPercent = Math.max(0, Math.min(100, Math.round((imported / total) * 100)));

  if (missing > 0) {
    return {
      imported,
      missing,
      notImported,
      extra,
      trackMbMissing,
      trackMbMismatched,
      duplicateRecordingIds,
      mbRepairable,
      releaseMbMissing,
      importPercent,
      label: 'Missing files',
      color: 'error',
      needsAttention: true,
      canImportRepair: true,
      canReviewMetadata: false,
    };
  }

  if (notImported > 0) {
    return {
      imported,
      missing,
      notImported,
      extra,
      trackMbMissing,
      trackMbMismatched,
      duplicateRecordingIds,
      mbRepairable,
      releaseMbMissing,
      importPercent,
      label: 'Partial import',
      color: 'warning',
      needsAttention: true,
      canImportRepair: true,
      canReviewMetadata: false,
    };
  }

  if (extra > 0) {
    return {
      imported,
      missing,
      notImported,
      extra,
      trackMbMissing,
      trackMbMismatched,
      duplicateRecordingIds,
      mbRepairable,
      releaseMbMissing,
      importPercent,
      label: 'Extra tracks',
      color: 'warning',
      needsAttention: true,
      canImportRepair: false,
      canReviewMetadata: true,
    };
  }

  if (releaseMbMissing || trackMbMissing > 0 || trackMbMismatched > 0 || duplicateRecordingIds > 0 || mbRepairable > 0) {
    return {
      imported,
      missing,
      notImported,
      extra,
      trackMbMissing,
      trackMbMismatched,
      duplicateRecordingIds,
      mbRepairable,
      releaseMbMissing,
      importPercent,
      label: hasFixableMbIds ? 'Fix MB IDs' : 'Needs MB review',
      color: hasFixableMbIds ? 'warning' : 'secondary',
      needsAttention: true,
      canImportRepair: false,
      canReviewMetadata: true,
    };
  }

  return {
    imported,
    missing,
    notImported,
    extra,
    trackMbMissing,
    trackMbMismatched,
    duplicateRecordingIds,
    mbRepairable,
    releaseMbMissing,
    importPercent,
    label: 'Complete',
    color: 'success',
    needsAttention: false,
    canImportRepair: false,
    canReviewMetadata: false,
  };
}

export function artistNeedsAttention(artist: LibraryArtist) {
  return Boolean(artist.empty_artist_folder) || artist.albums.some((album) => getAlbumHealth(album).needsAttention);
}

export function albumCanImportRepair(album: LibraryAlbum) {
  return getAlbumHealth(album).canImportRepair && Boolean(album.aldir);
}

export function artistCanImportRepair(artist: LibraryArtist) {
  return artist.albums.some(albumCanImportRepair);
}

/** Albums that need MB ID discovery: in the library with a folder on disk but no release ID,
 *  and not already sitting in the Import Review queue waiting for a human decision. */
export function albumNeedsDiscovery(album: LibraryAlbum) {
  if (album.pending_review) return false;
  return Boolean(album.aldir) && !album.mb_albumid && Boolean(album.album_id);
}

/** Any album that Import All can help with: missing files, partial import, or needs MB ID.
 *  Excludes albums already waiting in Import Review (pending_review) — those need a human
 *  decision first. Also excludes albums where Beets is already complete but disk has extra
 *  files beyond the MB tracklist — those need Album Track Cleanup, not re-import. */
export function albumCanImportAllRepair(album: LibraryAlbum) {
  if (album.not_imported_is_extra) return false;
  if (album.pending_review) return false;
  return albumCanImportRepair(album) || albumNeedsDiscovery(album);
}
