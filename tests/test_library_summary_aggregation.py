"""Tests for _library_stats_for_artists() -- the library summary-count fix.

Root cause (found via live read-only inspection of production, 2026-07-28):
_build_library_payload() walks /data/media/music and represents each disk
folder as an "album" card, including folders that are actually just a
single already-imported singleton track (album_id == 0, not_imported == 0,
track_count == 1) -- these are NOT fake extra albums, but the old stats
function counted every non-virtual card as "1 album" regardless. It also
summed track_count per card without deduplicating by album_id, so the same
real album split across two disk folders (e.g. a reissue folder alongside
the original) was counted as 2 albums instead of 1. Confirmed live: a
production instance reporting "413 albums" via the raw database reported
"987 albums" via this stats function, and manual inspection of the raw
payload showed exactly this pattern (595 album_id==0 cards, each
track_count=1, not_imported=0 -- singleton pseudo-albums -- plus 56 album_ids
appearing on more than one card).

These tests build synthetic artist/album card lists (the same shape
_build_library_payload() produces) and assert the corrected counting
behavior, without needing a real Beets database or Flask app.
"""

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_ast_cache import get_app_ast  # noqa: E402

import ast  # noqa: E402


def _load_namespace() -> Dict[str, Any]:
    tree = get_app_ast()
    names = {
        "_library_stats_for_artists",
        "_library_album_is_disk_only",
        "_library_recompute_artist_counts",
        "_library_payload_for_response",
    }
    ns: Dict[str, Any] = {"List": List, "Dict": Dict, "Any": Any}
    for node in tree.body:
        node_name = getattr(node, "name", None)
        if node_name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, "app.py", "exec"), ns)
    return ns


def _album(album_id=0, track_count=1, not_imported=0, missing=0, tracks=None, virtual=False):
    d = {
        "album_id": album_id,
        "track_count": track_count,
        "not_imported": not_imported,
        "missing": missing,
    }
    if tracks is not None:
        d["tracks"] = tracks
    if virtual:
        d["virtual_appearance"] = True
    return d


def _artist(name, albums):
    return {"name": name, "albums": albums}


class LibraryStatsForArtistsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_namespace()
        cls.stats = staticmethod(cls.ns["_library_stats_for_artists"])

    # 1. Raw track total equals unique remote item count (real albums only, no singletons here).
    def test_normal_albums_count_tracks_and_albums_correctly(self):
        artists = [
            _artist("Artist A", [_album(album_id=1, track_count=10)]),
            _artist("Artist B", [_album(album_id=2, track_count=5)]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 2)
        self.assertEqual(result["tracks"], 15)
        self.assertEqual(result["artists"], 2)

    # 4. Singleton tracks do not create fake albums.
    def test_singleton_tracks_do_not_create_fake_albums(self):
        artists = [
            _artist("Solo Artist", [_album(album_id=0, track_count=1, not_imported=0)]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 0)
        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["singleton_tracks"], 1)

    def test_multiple_singleton_cards_add_up_as_tracks_not_albums(self):
        artists = [
            _artist("1TakeJay", [
                _album(album_id=0, track_count=1, not_imported=0),
                _album(album_id=0, track_count=1, not_imported=0),
            ]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 0)
        self.assertEqual(result["tracks"], 2)
        self.assertEqual(result["singleton_tracks"], 2)

    # 5. Disk-only folders are counted separately.
    def test_disk_only_folder_counted_separately_not_as_album_or_track(self):
        artists = [
            _artist("New Artist", [_album(album_id=0, track_count=8, not_imported=8)]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 0)
        self.assertEqual(result["tracks"], 0)
        self.assertEqual(result["disk_only_albums"], 1)
        self.assertEqual(result["disk_only_tracks"], 8)

    # 6. Imported and disk-only entries are not double-counted.
    def test_mixed_imported_and_disk_only_albums_not_double_counted(self):
        artists = [
            _artist("Mixed Artist", [
                _album(album_id=1, track_count=10),
                _album(album_id=0, track_count=3, not_imported=3),
                _album(album_id=0, track_count=1, not_imported=0),
            ]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 1)
        self.assertEqual(result["tracks"], 11)  # 10 real album tracks + 1 singleton
        self.assertEqual(result["disk_only_albums"], 1)
        self.assertEqual(result["disk_only_tracks"], 3)
        self.assertEqual(result["singleton_tracks"], 1)

    # 7. Multiple pagination/disk-folder pages do not duplicate the same album.
    def test_same_album_split_across_two_disk_folders_counts_once(self):
        artists = [
            _artist("Reissue Artist", [
                _album(album_id=42, track_count=8),   # original folder
                _album(album_id=42, track_count=4),   # deluxe reissue folder, same album_id
            ]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 1)
        self.assertEqual(result["tracks"], 12)

    def test_duplicate_album_ids_across_different_artists_each_count_once(self):
        # Shouldn't normally happen (album_id implies one owning artist), but
        # the counting logic keys purely on album_id set membership globally,
        # so this documents that behavior explicitly.
        artists = [
            _artist("Artist A", [_album(album_id=5, track_count=3)]),
            _artist("Artist B", [_album(album_id=5, track_count=3)]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 1)

    # Virtual appearance rows must not be counted at all.
    def test_virtual_appearance_albums_are_excluded(self):
        artists = [
            _artist("Artist A", [_album(album_id=1, track_count=10, virtual=True)]),
        ]
        result = self.stats(artists)
        self.assertEqual(result["albums"], 0)
        self.assertEqual(result["tracks"], 0)

    # 10. The exact production-shaped fixture (sanitized) returns tracks=3144, albums=413.
    def test_production_shaped_fixture_reconciles_to_real_totals(self):
        artists = []
        # 413 real albums, 10 tracks apiece = 4130 tracks under real albums... but
        # production is 3144 total with 413 albums and 934 singletons, so model
        # album track counts to sum to (3144 - 934) = 2210 across 413 albums.
        album_tracks_total = 3144 - 934
        per_album = album_tracks_total // 413
        remainder = album_tracks_total - per_album * 413
        albums = []
        for i in range(413):
            tc = per_album + (1 if i < remainder else 0)
            albums.append(_album(album_id=i + 1, track_count=tc))
        artists.append(_artist("Real Artist", albums))

        # 934 singleton pseudo-album cards (album_id=0, not_imported=0, 1 track each)
        singleton_albums = [_album(album_id=0, track_count=1, not_imported=0) for _ in range(934)]
        artists.append(_artist("Singleton Artist", singleton_albums))

        result = self.stats(artists)
        self.assertEqual(result["albums"], 413)
        self.assertEqual(result["tracks"], 3144)
        self.assertEqual(result["singleton_tracks"], 934)
        self.assertEqual(result["disk_only_albums"], 0)


class LibraryPayloadForResponseIncludeDiskOnlyTests(unittest.TestCase):
    """11/12: include_disk_only=0 excludes disk-only entries; =1 includes
    them, but only in the documented disk_only_* fields, never folded into
    the main albums/tracks counts."""

    @classmethod
    def setUpClass(cls):
        cls.ns = _load_namespace()
        cls.build = cls.ns["_library_payload_for_response"]

    def _payload(self):
        return {
            "artists": [
                _artist("Mixed Artist", [
                    _album(album_id=1, track_count=10),
                    _album(album_id=0, track_count=3, not_imported=3),  # disk-only
                    _album(album_id=0, track_count=1, not_imported=0),  # singleton
                ]),
            ],
            "library_version": 1,
        }

    def test_include_disk_only_false_excludes_disk_only_album(self):
        out = self.ns["_library_payload_for_response"](self._payload(), include_tracks=False, include_disk_only=False)
        albums = out["artists"][0]["albums"]
        album_ids = [a.get("album_id") for a in albums]
        not_imported_counts = [a.get("not_imported") for a in albums]
        # The genuinely disk-only card (not_imported=3) must not survive.
        self.assertNotIn(3, [a.get("track_count") for a in albums if a.get("not_imported")])
        self.assertEqual(out["stats"]["disk_only_albums"], 0)

    def test_include_disk_only_true_reports_disk_only_separately(self):
        out = self.ns["_library_payload_for_response"](self._payload(), include_tracks=False, include_disk_only=True)
        self.assertEqual(out["stats"]["albums"], 1)
        self.assertEqual(out["stats"]["disk_only_albums"], 1)
        self.assertEqual(out["stats"]["disk_only_tracks"], 3)


if __name__ == "__main__":
    unittest.main()
