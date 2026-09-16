"""ARCH-007 Milestone 1 Adversarial Challenge Test Harness (Challenger 2).

Empirically tests edge-case parameters against all 18 new BeetsClient methods
and Control Agent HTTP endpoints on branch fix/arch007-structured-read-closure.

Covers:
1. Boundary limits (limit=0, limit=-1, limit=9999999, offset=-1, min_albums boundary, etc.)
2. Empty/whitespace strings, unicode characters, control characters (\x00, \n, \r)
3. Malformed UUIDs, non-existent IDs
4. Bounded responses and timeout behaviors
5. Engine-offline / closed socket simulation across all methods
6. Direct HTTP Control Agent adversarial input verification (no unhandled 500s)
7. SQLite database integrity (zero corruption, schema consistency, PRAGMA integrity_check)
"""

import http.client
import io
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import backend.beets_control_agent as agent
from backend.beets_client import (
    BeetsAuthError,
    BeetsBadRequestError,
    BeetsClient,
    BeetsError,
    BeetsNotFoundError,
    BeetsUnavailableError,
)


def _get_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestArch007ClientBoundaryLimits(unittest.TestCase):
    """Challenge 1: Boundary limits against BeetsClient semantic methods."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok" * 10)

    # 1. get_unmatched_review_items
    def test_unmatched_review_items_boundary_limits(self):
        invalid_limits = [0, -1, -999, 1001, 9999999, "200", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_unmatched_review_items(limit=lim)

        invalid_offsets = [-1, -999, "0", None]
        for off in invalid_offsets:
            with self.subTest(offset=off):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_unmatched_review_items(offset=off)

    # 2. get_genre_stats
    def test_genre_stats_boundary_limits(self):
        invalid_missing_limits = [-1, -999, 2001, 9999999, "200", None]
        for mlim in invalid_missing_limits:
            with self.subTest(missing_limit=mlim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_genre_stats(missing_limit=mlim)

    # 3. get_rgid_groups
    def test_rgid_groups_boundary_limits(self):
        invalid_limits = [0, -1, 501, 9999999, "100", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_rgid_groups(limit=lim)

        invalid_offsets = [-1, -999, "0", None]
        for off in invalid_offsets:
            with self.subTest(offset=off):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_rgid_groups(offset=off)

        invalid_min_albums = [0, 1, -1, 51, 9999, "2", None]
        for ma in invalid_min_albums:
            with self.subTest(min_albums=ma):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_rgid_groups(min_albums=ma)

    # 4. merge_rgid_group
    def test_merge_rgid_group_boundary_limits(self):
        # Target album id <= 0
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(0, [2, 3])
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(-1, [2, 3])
        # Source album ids empty or non-positive
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [])
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [0])
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [-5])
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [2, 0])
        # Target in source
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [1, 2])

    # 5. clean_orphaned_items
    def test_clean_orphaned_items_boundary_limits(self):
        invalid_ids = [[0], [-1], [1, 0], [1, -5], ["bad"], [None]]
        for ids in invalid_ids:
            with self.subTest(ids=ids):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.clean_orphaned_items(ids)

    # 6. clean_empty_albums
    def test_clean_empty_albums_boundary_limits(self):
        invalid_ids = [[0], [-1], [2, -3], ["abc"], [None]]
        for ids in invalid_ids:
            with self.subTest(ids=ids):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.clean_empty_albums(ids)

    # 7. get_mbid_sticking_candidates
    def test_mbid_sticking_candidates_boundary_limits(self):
        invalid_limits = [0, -1, 1001, 9999999, "100", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_mbid_sticking_candidates(limit=lim)

        invalid_offsets = [-1, -999, "0", None]
        for off in invalid_offsets:
            with self.subTest(offset=off):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_mbid_sticking_candidates(offset=off)

        invalid_phases = [0, -1, 4, 99, "1"]
        for ph in invalid_phases:
            with self.subTest(phase=ph):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_mbid_sticking_candidates(phase=ph)

    # 8. get_album_mb_completeness
    def test_album_mb_completeness_boundary_limits(self):
        invalid_aids = [0, -1, -9999, "123", None, 1.5]
        for aid in invalid_aids:
            with self.subTest(album_id=aid):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_album_mb_completeness(aid)

    # 9. sync_deleted_files
    def test_sync_deleted_files_boundary_limits(self):
        invalid_limits = [0, -1, 50001, 9999999, "1000", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.sync_deleted_files(limit=lim)

    # 10. find_files_for_hardlink
    def test_find_files_for_hardlink_boundary_limits(self):
        invalid_limits = [0, -1, 201, 9999999, "50", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_files_for_hardlink(filename="test.mp3", limit=lim)

    # 11. get_format_upgrade_candidates
    def test_format_upgrade_candidates_boundary_limits(self):
        invalid_limits = [0, -1, 1001, 9999999, "100", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_format_upgrade_candidates(limit=lim)

        invalid_offsets = [-1, -999, "0", None]
        for off in invalid_offsets:
            with self.subTest(offset=off):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_format_upgrade_candidates(offset=off)

    # 12. find_recording_replacement
    def test_find_recording_replacement_boundary_limits(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        invalid_limits = [0, -1, 51, 9999999, "20", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_recording_replacement(uuid_str, limit=lim)

        invalid_exclude = [0, -1, -999, "10", None]
        for exc_id in invalid_exclude:
            if exc_id is None:
                continue
            with self.subTest(exclude_item_id=exc_id):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_recording_replacement(uuid_str, exclude_item_id=exc_id)

    # 13. merge_imported_album
    def test_merge_imported_album_boundary_limits(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(0, 2)
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(-1, 2)
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(1, 0)
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(1, -1)
        # Identical target and source
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(10, 10)

    # 14. resolve_folder_to_albums
    def test_resolve_folder_boundary_limits(self):
        invalid_since = [-1, -0.001, -999, "123", []]
        for s in invalid_since:
            with self.subTest(since=s):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.resolve_folder_to_albums("/music/album", since=s)


class TestArch007ClientStringsAndCharacters(unittest.TestCase):
    """Challenge 2: Empty/whitespace strings, unicode, and control characters."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok" * 10)

    def test_resolve_folder_empty_and_control_chars(self):
        invalid_paths = ["", "   ", "\t\n", None, 123, "/music/\x00/album"]
        for p in invalid_paths:
            with self.subTest(folder_path=p):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.resolve_folder_to_albums(p)

    def test_find_files_for_hardlink_empty_filename(self):
        invalid_fnames = ["", "   ", "\t\r\n", None, 123]
        for fn in invalid_fnames:
            with self.subTest(filename=fn):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_files_for_hardlink(filename=fn)

    def test_format_upgrade_candidates_format_filter(self):
        invalid_filters = [
            "", "   ", "A", "TOOLONGFORMATNAME", "MP3\x00", "MP3\n",
            "MP3; DROP TABLE items;", "' OR 1=1 --", None, 123
        ]
        for fmt in invalid_filters:
            with self.subTest(format_filter=fmt):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_format_upgrade_candidates(format_filter=fmt)

    def test_mbid_sticking_candidates_invalid_mode(self):
        invalid_modes = ["", "   ", "UNKNOWN", "all\x00", "mode\n", 123, None]
        for m in invalid_modes:
            with self.subTest(mode=m):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_mbid_sticking_candidates(mode=m)

    def test_stamp_artist_folder_mbids_invalid_strings(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(folder_path="")
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(folder_path="   ")
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(mbid="")
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(mbid="   ")
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(artist_folders="not-a-list")


class TestArch007ClientMalformedUUIDs(unittest.TestCase):
    """Challenge 3: Malformed UUIDs and ID validation across all relevant methods."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok" * 10)

    malformed_uuids = [
        "", "   ", "not-a-uuid", "12345",
        "3a0b3d68-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "3a0b3d68-3a0b-3a0b-3a0b-3a0b3d68",  # Too short
        "3a0b3d68-3a0b-3a0b-3a0b-3a0b3d68xxxxxx",  # Too long
        "' OR '1'='1", "UUID\x00INJECTION", None, 12345,
    ]

    def test_get_rgid_group_detail_malformed_uuid(self):
        for u in self.malformed_uuids:
            with self.subTest(rgid=u):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_rgid_group_detail(u)

    def test_merge_rgid_group_malformed_uuid(self):
        for u in self.malformed_uuids:
            if not u or not isinstance(u, str):
                continue
            with self.subTest(rgid=u):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.merge_rgid_group(1, [2], rgid=u)

    def test_find_recording_replacement_malformed_uuid(self):
        for u in self.malformed_uuids:
            with self.subTest(mb_trackid=u):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_recording_replacement(u)

    def test_stamp_artist_folder_mbids_malformed_uuid(self):
        for u in self.malformed_uuids:
            if not u or not isinstance(u, str):
                continue
            with self.subTest(mbid=u):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.stamp_artist_folder_mbids(mbid=u)


class TestArch007ClientEngineOfflineAndSocketFailures(unittest.TestCase):
    """Challenge 4: Closed socket simulation and offline fail-closed behavior across all 18 methods."""

    @classmethod
    def setUpClass(cls):
        # Pick a free port and never bind a server to it
        cls.dead_port = _get_free_port()
        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.dead_port}",
        })
        cls.env_patcher.start()
        cls.client = BeetsClient(base_url=f"http://127.0.0.1:{cls.dead_port}", token="tok" * 10, timeout=1.0)

    @classmethod
    def tearDownClass(cls):
        cls.env_patcher.stop()

    def test_all_18_methods_fail_closed_on_dead_engine(self):
        """Verify that every new BeetsClient method raises BeetsUnavailableError when engine is dead."""
        valid_uuid = "11111111-1111-1111-1111-111111111111"
        calls = [
            ("resolve_folder_to_albums", (self.client.resolve_folder_to_albums, ("/music/path",))),
            ("resolve_folder", (self.client.resolve_folder, ("/music/path",))),
            ("get_unmatched_review_items", (self.client.get_unmatched_review_items, ())),
            ("get_unmatched_review_queue", (self.client.get_unmatched_review_queue, ())),
            ("get_library_stats", (self.client.get_library_stats, ())),
            ("get_genre_stats", (self.client.get_genre_stats, ())),
            ("get_rgid_groups", (self.client.get_rgid_groups, ())),
            ("get_rgid_group_detail", (self.client.get_rgid_group_detail, (valid_uuid,))),
            ("merge_rgid_group", (self.client.merge_rgid_group, (1, [2, 3]))),
            ("clean_orphaned_items", (self.client.clean_orphaned_items, ())),
            ("clean_empty_albums", (self.client.clean_empty_albums, ())),
            ("get_mbid_sticking_candidates", (self.client.get_mbid_sticking_candidates, ())),
            ("get_album_mb_completeness", (self.client.get_album_mb_completeness, (1,))),
            ("sync_deleted_files", (self.client.sync_deleted_files, ())),
            ("scan_library_integrity", (self.client.scan_library_integrity, ())),
            ("get_artist_alias_groups", (self.client.get_artist_alias_groups, ())),
            ("list_artist_alias_groups", (self.client.list_artist_alias_groups, ())),
            ("stamp_artist_folder_mbids", (self.client.stamp_artist_folder_mbids, ())),
            ("find_files_for_hardlink", (self.client.find_files_for_hardlink, ("song.mp3",))),
            ("find_hardlink_candidates", (self.client.find_hardlink_candidates, ("song.mp3",))),
            ("get_format_upgrade_candidates", (self.client.get_format_upgrade_candidates, ())),
            ("find_recording_replacement", (self.client.find_recording_replacement, (valid_uuid,))),
            ("find_recording_replacements", (self.client.find_recording_replacements, (valid_uuid,))),
            ("merge_imported_album", (self.client.merge_imported_album, (1, 2))),
        ]

        for name, (fn, args) in calls:
            with self.subTest(method=name):
                with self.assertRaises(BeetsUnavailableError) as ctx:
                    fn(*args)
                self.assertIn("unavailable", str(ctx.exception).lower())

    def test_unresolvable_host_raises_unavailable(self):
        client = BeetsClient(base_url="http://non-existent-agent-domain-xyz-404.local:8338", token="tok", timeout=1.0)
        with self.assertRaises(BeetsUnavailableError):
            client.get_library_stats()


class TestArch007LiveControlAgentAdversarialHarness(unittest.TestCase):
    """Challenge 5 & 6: Live HTTP Control Agent verification against adversarial inputs and SQLite integrity."""

    @classmethod
    def setUpClass(cls):
        cls.token = "arch007_challenger2_secret_token_1234567890_ok"
        cls.token_patch = mock.patch.object(agent, "BEETS_API_TOKEN", cls.token)
        cls.token_patch.start()

        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmpdir.name) / "musiclibrary.blb"
        cls.lib_patch = mock.patch.object(agent, "LIB_PATH", str(cls.db_path))
        cls.lib_patch.start()

        cls.music_dir = Path(cls.tmpdir.name) / "music"
        cls.music_dir.mkdir(parents=True, exist_ok=True)
        cls.music_patch = mock.patch.object(agent, "MUSIC_LIBRARY_PATH", str(cls.music_dir))
        cls.music_patch.start()

        cls._seed_database()

        cls.port = _get_free_port()
        cls.httpd = agent.ThreadingHTTPServer(("127.0.0.1", cls.port), agent.ControlAgentHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.port}",
        })
        cls.env_patcher.start()

        cls.client = BeetsClient(base_url=f"http://127.0.0.1:{cls.port}", token=cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.env_patcher.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.music_patch.stop()
        cls.lib_patch.stop()
        cls.token_patch.stop()
        cls.tmpdir.cleanup()

    @classmethod
    def _seed_database(cls):
        con = sqlite3.connect(cls.db_path)
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS albums ("
            "  id INTEGER PRIMARY KEY, "
            "  album TEXT, "
            "  albumartist TEXT, "
            "  albumartist_credit TEXT, "
            "  artist TEXT, "
            "  genre TEXT, "
            "  year INTEGER, "
            "  mb_albumid TEXT, "
            "  mb_releasegroupid TEXT, "
            "  mb_albumartistid TEXT"
            ")"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "  id INTEGER PRIMARY KEY, "
            "  album_id INTEGER, "
            "  path TEXT, "
            "  title TEXT, "
            "  artist TEXT, "
            "  album TEXT, "
            "  albumartist TEXT, "
            "  genre TEXT, "
            "  year INTEGER, "
            "  track INTEGER, "
            "  disc INTEGER, "
            "  length REAL, "
            "  format TEXT, "
            "  bitrate INTEGER, "
            "  samplerate INTEGER, "
            "  bitdepth INTEGER, "
            "  mb_trackid TEXT, "
            "  mb_albumid TEXT, "
            "  added REAL"
            ")"
        )

        # Album 1: Complete with MBIDs
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, genre, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (1, 'OK Computer', 'Radiohead', 'Rock', 1997, "
            "  '8b556b6b-d366-3d23-bf7b-232185a5a1f2', '3a0b3d68-07e0-3165-b1a8-8b01bb363a0b', 'a74b1b7f-71a5-4011-9441-d0b5e4122711')"
        )
        # Create physical files for Album 1
        a1_dir = cls.music_dir / "Radiohead" / "OK Computer"
        a1_dir.mkdir(parents=True, exist_ok=True)
        t1_path = a1_dir / "01 Airbag.flac"
        t1_path.write_bytes(b"dummy flac content")
        t2_path = a1_dir / "02 Paranoid Android.flac"
        t2_path.write_bytes(b"dummy flac content")

        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (101, 1, ?, 'Airbag', 'Radiohead', 'OK Computer', 'Radiohead', 'Rock', 1997, 1, 1, 284.0, 'FLAC', 850000, 44100, 16, "
            "  '4b1a134a-921c-4b6e-8212-8414cb2f57b8', '8b556b6b-d366-3d23-bf7b-232185a5a1f2', 1710000001.0)",
            (str(t1_path),)
        )
        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (102, 1, ?, 'Paranoid Android', 'Radiohead', 'OK Computer', 'Radiohead', 'Rock', 1997, 2, 1, 383.0, 'FLAC', 890000, 44100, 16, "
            "  '5c2b245b-932d-4c7f-9323-9525dc3f68c9', '8b556b6b-d366-3d23-bf7b-232185a5a1f2', 1710000002.0)",
            (str(t2_path),)
        )

        # Album 2: Unmatched (no MBIDs), no genre
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, genre, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (2, 'Unmatched Demo', 'Unknown Artist', '', 2020, '', '', '')"
        )
        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (201, 2, '/missing/demo/track1.mp3', 'Demo 1', 'Unknown Artist', 'Unmatched Demo', 'Unknown Artist', '', 2020, 1, 1, 180.0, 'MP3', 320000, 44100, 16, '', '', 1710000003.0)"
        )

        # Album 3: Empty album (0 items)
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, genre, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (3, 'Empty Album', 'Ghost Artist', 'Electronic', 2021, '', '', '')"
        )

        # Item 301: Orphaned item (album_id 999 does not exist)
        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (301, 999, '/missing/orphan.mp3', 'Orphaned Song', 'Orphan Artist', 'No Album', 'Orphan Artist', 'Pop', 2019, 1, 1, 200.0, 'MP3', 192000, 44100, 16, '', '', 1710000004.0)"
        )

        # Item 401: Singleton item (album_id NULL, no mb_trackid)
        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (401, NULL, '/missing/singleton.mp3', 'Singleton Song', 'Solo Artist', '', 'Solo Artist', 'Jazz', 2022, 1, 1, 210.0, 'MP3', 256000, 44100, 16, '', '', 1710000005.0)"
        )

        con.commit()
        con.close()

    def _http_request(self, method: str, path: str, body: dict = None, headers: dict = None):
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        if headers:
            h.update(headers)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        resp_data = resp.read().decode("utf-8")
        conn.close()
        try:
            return resp.status, json.loads(resp_data)
        except Exception:
            return resp.status, resp_data

    # ── Non-Existent ID Handling ───────────────────────────────────────────────
    def test_non_existent_album_mb_completeness_returns_404(self):
        with self.assertRaises(BeetsNotFoundError) as ctx:
            self.client.get_album_mb_completeness(999999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_non_existent_rgid_group_detail_returns_404(self):
        with self.assertRaises(BeetsNotFoundError) as ctx:
            self.client.get_rgid_group_detail("00000000-0000-0000-0000-000000000000")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_non_existent_merge_imported_album_returns_404(self):
        with self.assertRaises(BeetsNotFoundError) as ctx:
            self.client.merge_imported_album(1, 999999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_non_existent_merge_rgid_group_returns_404(self):
        with self.assertRaises(BeetsNotFoundError) as ctx:
            self.client.merge_rgid_group(1, [999999])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_non_existent_recording_replacement_returns_empty(self):
        res = self.client.find_recording_replacement("00000000-0000-0000-0000-000000000000")
        self.assertEqual(res, [])

    def test_non_existent_hardlink_candidates_returns_empty(self):
        res = self.client.find_files_for_hardlink("completely_fictional_song_9999.flac")
        self.assertEqual(res, [])

    # ── Bounded Responses ──────────────────────────────────────────────────────
    def test_bounded_response_limits(self):
        # get_unmatched_review_items limit=1
        res = self.client.get_unmatched_review_items(limit=1)
        self.assertLessEqual(len(res.get("albums", [])), 1)

        # get_genre_stats missing_limit=1
        res2 = self.client.get_genre_stats(missing_limit=1)
        self.assertLessEqual(len(res2.get("missing", [])), 1)

        # get_format_upgrade_candidates limit=1
        res3 = self.client.get_format_upgrade_candidates(format_filter="MP3", limit=1)
        self.assertLessEqual(len(res3), 1)

    # ── Direct HTTP Adversarial Requests (Rejecting 500s) ───────────────────────
    def test_direct_http_invalid_query_params_return_400_not_500(self):
        """Directly challenge Control Agent query parsing: must return 400, never 500."""
        endpoints = [
            "/review/queue/unmatched?limit=-1",
            "/review/queue/unmatched?limit=abc",
            "/review/queue/unmatched?offset=-1",
            "/stats/genres?missing_limit=-1",
            "/stats/genres?missing_limit=abc",
            "/clean/rgid-groups?limit=0",
            "/clean/rgid-groups?limit=abc",
            "/clean/rgid-groups?min_albums=1",
            "/clean/rgid-groups?min_albums=51",
            "/clean/rgid-groups/not-a-uuid",
            "/library/mbid-sticking/candidates?mode=invalid_mode",
            "/library/mbid-sticking/candidates?limit=0",
            "/albums/-1/mb-completeness",
            "/albums/not_a_number/mb-completeness",
            "/library/format-upgrades?format=INVALID_FORMAT!",
            "/library/format-upgrades?limit=0",
            "/library/recording-replacements?mb_trackid=not-a-uuid",
            "/library/recording-replacements?mb_trackid=11111111-1111-1111-1111-111111111111&limit=0",
            "/library/recording-replacements?mb_trackid=11111111-1111-1111-1111-111111111111&exclude_item_id=-1",
        ]
        for ep in endpoints:
            with self.subTest(endpoint=ep):
                status, body = self._http_request("GET", ep)
                self.assertEqual(status, 400, f"Expected 400 Bad Request for {ep}, got {status}: {body}")
                self.assertIsInstance(body, dict)
                self.assertFalse(body.get("ok", True))

    def test_direct_http_post_invalid_json_bodies_return_400_not_500(self):
        """Directly challenge Control Agent POST endpoints with invalid body payloads."""
        test_cases = [
            ("/library/resolve-folder", {"folder_path": ""}, 400),
            ("/library/resolve-folder", {"folder_path": 12345}, 400),
            ("/library/resolve-folder", {"folder_path": "/music/\x00/bad"}, 400),
            ("/library/resolve-folder", {"folder_path": "/valid", "since": -1}, 400),
            ("/clean/rgid-groups/merge", {"target_album_id": -1, "source_album_ids": [2]}, 400),
            ("/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_ids": []}, 400),
            ("/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_ids": [-1]}, 400),
            ("/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_ids": [1]}, 400),
            ("/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_ids": [2], "mb_releasegroupid": "bad-uuid"}, 400),
            ("/clean/orphaned-items", {"item_ids": "not-a-list"}, 400),
            ("/clean/orphaned-items", {"item_ids": [-1]}, 400),
            ("/clean/empty-albums", {"album_ids": "not-a-list"}, 400),
            ("/clean/empty-albums", {"album_ids": [0]}, 400),
            ("/library/sync-deleted", {"limit": 0}, 400),
            ("/library/sync-deleted", {"limit": 50001}, 400),
            ("/maintenance/artist-folders/stamp-mbids", {"folder_path": 123}, 400),
            ("/maintenance/artist-folders/stamp-mbids", {"mbid": "not-a-uuid"}, 400),
            ("/maintenance/artist-folders/stamp-mbids", {"artist_folders": "not-a-list"}, 400),
            ("/library/find-hardlink-candidates", {}, 400),  # No search params
            ("/library/albums/merge", {"target_album_id": -1, "source_album_id": 2}, 400),
            ("/library/albums/merge", {"target_album_id": 1, "source_album_id": 1}, 400),
        ]

        for path, payload, expected_status in test_cases:
            with self.subTest(path=path, payload=payload):
                status, body = self._http_request("POST", path, payload)
                self.assertEqual(status, expected_status, f"Expected {expected_status} for POST {path}, got {status}: {body}")
                self.assertIsInstance(body, dict)

    # ── Database Integrity Verification ─────────────────────────────────────────
    def test_sqlite_integrity_after_adversarial_barrage(self):
        """Verify that the SQLite database has 0 corruption after all adversarial calls."""
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        self.assertEqual(rows, [("ok",)], f"Integrity check failed: {rows}")

        # Check album and item table readability
        cur.execute("SELECT COUNT(*) FROM albums")
        album_cnt = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM items")
        item_cnt = cur.fetchone()[0]
        con.close()

        self.assertGreater(album_cnt, 0)
        self.assertGreater(item_cnt, 0)


class TestArch007ClientTimeoutHarness(unittest.TestCase):
    """Challenge 7: Read and connection timeout behaviors."""

    @classmethod
    def setUpClass(cls):
        cls.hanging_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.hanging_sock.bind(("127.0.0.1", 0))
        cls.hanging_port = cls.hanging_sock.getsockname()[1]
        cls.hanging_sock.listen(5)
        cls.running = True

        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.hanging_port}",
        })
        cls.env_patcher.start()

        def _drain_and_hang():
            while cls.running:
                try:
                    cls.hanging_sock.settimeout(0.2)
                    client_conn, _ = cls.hanging_sock.accept()
                    # Sleep before closing to trigger timeout on client
                    threading.Thread(target=lambda c: time.sleep(1.0), args=(client_conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    break

        cls.server_thread = threading.Thread(target=_drain_and_hang, daemon=True)
        cls.server_thread.start()
        cls.client = BeetsClient(base_url=f"http://127.0.0.1:{cls.hanging_port}", token="tok", timeout=0.2)

    @classmethod
    def tearDownClass(cls):
        cls.running = False
        cls.hanging_sock.close()
        cls.server_thread.join(timeout=2)
        cls.env_patcher.stop()

    def test_timeout_raises_beets_unavailable(self):
        with self.assertRaises(BeetsUnavailableError) as ctx:
            self.client.get_library_stats(timeout=0.2)
        self.assertTrue(
            "timed out" in str(ctx.exception).lower() or "unavailable" in str(ctx.exception).lower()
        )
