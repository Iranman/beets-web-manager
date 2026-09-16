"""ARCH-007 Milestone 1 Challenger 2 Empirical Test Harness.

Covers:
1. Boundary limits (limit=0, limit=-1, limit=9999999, offset=-1, min_albums bounds, etc.)
2. Empty/whitespace strings, unicode characters, control characters (\x00, \n, \r)
3. Malformed UUIDs, non-existent IDs
4. Bounded responses and timeout behaviors
5. Engine-offline / closed socket simulation across all 19 BeetsClient methods
6. Live HTTP Control Agent adversarial input verification (no 500s, proper error taxonomy)
7. SQLite database integrity (zero corruption, schema consistency, PRAGMA integrity_check)
"""

import http.client
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


class TestBeetsClientBoundaryLimitsChallenger(unittest.TestCase):
    """Challenge 1: Boundary limits against BeetsClient semantic methods."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok" * 10)

    # 1. resolve_folder_to_albums
    def test_resolve_folder_boundary_limits(self):
        invalid_since = [-1, -0.0001, -999, "123", [], {}, object()]
        for s in invalid_since:
            with self.subTest(since=s):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.resolve_folder_to_albums("/music/album", since=s)

        # Valid since values (0, 0.0, positive floats) should not raise BeetsBadRequestError
        # (they would try to make an HTTP request and raise BeetsUnavailableError because no server is running)
        for valid_s in [0, 0.0, 1710000000.5]:
            with self.subTest(valid_since=valid_s):
                with self.assertRaises(BeetsUnavailableError):
                    self.client.resolve_folder_to_albums("/music/album", since=valid_s)

    # 2. get_unmatched_review_items
    def test_unmatched_review_items_boundary_limits(self):
        invalid_limits = [0, -1, -999, 1001, 9999999, "200", None, 1.5]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_unmatched_review_items(limit=lim)

        invalid_offsets = [-1, -999, "0", None, 1.5]
        for off in invalid_offsets:
            with self.subTest(offset=off):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_unmatched_review_items(offset=off)

    # 3. get_genre_stats
    def test_genre_stats_boundary_limits(self):
        invalid_missing_limits = [-1, -999, 2001, 9999999, "200", None, 1.5]
        for mlim in invalid_missing_limits:
            with self.subTest(missing_limit=mlim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_genre_stats(missing_limit=mlim)

    # 4. get_rgid_groups
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

    # 5. merge_rgid_group
    def test_merge_rgid_group_boundary_limits(self):
        # Target album id <= 0 or invalid type
        for t in [0, -1, -999]:
            with self.subTest(target=t):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.merge_rgid_group(t, [2, 3])

        # Non-integer target types: empirically raises (BeetsBadRequestError, TypeError, ValueError)
        for t in [None, "bad", []]:
            with self.subTest(target_invalid_type=t):
                with self.assertRaises((BeetsBadRequestError, TypeError, ValueError)):
                    self.client.merge_rgid_group(t, [2, 3])

        # Source album ids empty or containing <= 0
        for srcs in [[], [0], [-1], [2, 0], [2, -5]]:
            with self.subTest(sources=srcs):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.merge_rgid_group(1, srcs)

        # Source album ids containing non-int types
        for srcs in [["bad"], [None], "not_a_list"]:
            with self.subTest(sources_invalid_type=srcs):
                with self.assertRaises((BeetsBadRequestError, TypeError, ValueError)):
                    self.client.merge_rgid_group(1, srcs)

        # Target contained in source ids
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [1, 2])

    # 6. clean_orphaned_items
    def test_clean_orphaned_items_boundary_limits(self):
        invalid_ids = [[0], [-1], [1, 0], [1, -5], ["bad"], [None], "not_a_list", 123]
        for ids in invalid_ids:
            with self.subTest(ids=ids):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.clean_orphaned_items(ids)

    # 7. clean_empty_albums
    def test_clean_empty_albums_boundary_limits(self):
        invalid_ids = [[0], [-1], [2, -3], ["abc"], [None], "not_a_list", 123]
        for ids in invalid_ids:
            with self.subTest(ids=ids):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.clean_empty_albums(ids)

    # 8. get_mbid_sticking_candidates
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

        invalid_phases = [0, -1, 4, 99, "1", "2", None, 1.5]
        for ph in invalid_phases:
            if ph is None:
                continue
            with self.subTest(phase=ph):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_mbid_sticking_candidates(phase=ph)

    # 9. get_album_mb_completeness
    def test_album_mb_completeness_boundary_limits(self):
        invalid_aids = [0, -1, -9999, "123", None, 1.5]
        for aid in invalid_aids:
            with self.subTest(album_id=aid):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_album_mb_completeness(aid)

    # 10. sync_deleted_files
    def test_sync_deleted_files_boundary_limits(self):
        invalid_limits = [0, -1, 50001, 9999999, "1000", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.sync_deleted_files(limit=lim)

    # 11. find_files_for_hardlink
    def test_find_files_for_hardlink_boundary_limits(self):
        invalid_limits = [0, -1, 201, 9999999, "50", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_files_for_hardlink(filename="test.mp3", limit=lim)

    # 12. get_format_upgrade_candidates
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

    # 13. find_recording_replacement
    def test_find_recording_replacement_boundary_limits(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        invalid_limits = [0, -1, 51, 9999999, "20", None]
        for lim in invalid_limits:
            with self.subTest(limit=lim):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_recording_replacement(uuid_str, limit=lim)

        invalid_exclude = [0, -1, -999, "10"]
        for exc_id in invalid_exclude:
            with self.subTest(exclude_item_id=exc_id):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_recording_replacement(uuid_str, exclude_item_id=exc_id)

    # 14. merge_imported_album
    def test_merge_imported_album_boundary_limits(self):
        for aid in [0, -1, -999, "1", None]:
            with self.subTest(target=aid):
                with self.assertRaises((BeetsBadRequestError, ValueError)):
                    self.client.merge_imported_album(aid, 2)
            with self.subTest(source=aid):
                with self.assertRaises((BeetsBadRequestError, ValueError)):
                    self.client.merge_imported_album(1, aid)

        # Identical target and source
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(10, 10)


class TestBeetsClientStringsAndCharactersChallenger(unittest.TestCase):
    """Challenge 2: Empty/whitespace strings, unicode, and control characters."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok" * 10)

    def test_resolve_folder_empty_and_control_chars(self):
        invalid_paths = ["", "   ", "\t\n\r", None, 123, "/music/\x00/album"]
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
            "", "   ", "A", "TOOLONGFORMATNAME", "MP3\x00", "MP3\n", "MP3\r\n",
            "MP3; DROP TABLE items;", "' OR 1=1 --", None, 123
        ]
        for fmt in invalid_filters:
            with self.subTest(format_filter=fmt):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.get_format_upgrade_candidates(format_filter=fmt)

    def test_mbid_sticking_candidates_invalid_mode(self):
        invalid_modes = ["", "   ", "UNKNOWN", "all\x00", "mode\n", 123, None, "' OR '1'='1"]
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


class TestBeetsClientMalformedUUIDsChallenger(unittest.TestCase):
    """Challenge 3: Malformed UUIDs and ID validation across all relevant methods."""

    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="tok" * 10)

    malformed_uuids = [
        "", "   ", "not-a-uuid", "12345",
        "3a0b3d68-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "3a0b3d68-3a0b-3a0b-3a0b-3a0b3d68",        # 32 chars, missing segment
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
            if u is None:
                continue
            with self.subTest(rgid=u):
                with self.assertRaises((BeetsBadRequestError, ValueError)):
                    self.client.merge_rgid_group(1, [2], rgid=u)

    def test_find_recording_replacement_malformed_uuid(self):
        for u in self.malformed_uuids:
            with self.subTest(mb_trackid=u):
                with self.assertRaises(BeetsBadRequestError):
                    self.client.find_recording_replacement(u)

    def test_stamp_artist_folder_mbids_malformed_uuid(self):
        for u in self.malformed_uuids:
            if u is None:
                continue
            with self.subTest(mbid=u):
                with self.assertRaises((BeetsBadRequestError, ValueError)):
                    self.client.stamp_artist_folder_mbids(mbid=u)


class TestBeetsClientEngineOfflineChallenger(unittest.TestCase):
    """Challenge 4: Closed socket simulation and offline fail-closed behavior across all 19 methods."""

    @classmethod
    def setUpClass(cls):
        cls.dead_port = _get_free_port()
        cls.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{cls.dead_port}",
        })
        cls.env_patcher.start()
        cls.client = BeetsClient(base_url=f"http://127.0.0.1:{cls.dead_port}", token="tok" * 10, timeout=1.0)

    @classmethod
    def tearDownClass(cls):
        cls.env_patcher.stop()

    def test_all_19_methods_fail_closed_on_dead_engine(self):
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


class TestLiveControlAgentAdversarialHarnessChallenger(unittest.TestCase):
    """Challenge 5 & 6: Live HTTP Control Agent verification against adversarial inputs and SQLite integrity."""

    @classmethod
    def setUpClass(cls):
        cls.token = "arch007_challenger_m1_2_token_safe_and_strong_12345"
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

        # Seed test album
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, genre, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (1, 'OK Computer', 'Radiohead', 'Rock', 1997, "
            "  '8b556b6b-d366-3d23-bf7b-232185a5a1f2', '3a0b3d68-07e0-3165-b1a8-8b01bb363a0b', 'a74b1b7f-71a5-4011-9441-d0b5e4122711')"
        )
        # Create physical media file
        a1_dir = cls.music_dir / "Radiohead" / "OK Computer"
        a1_dir.mkdir(parents=True, exist_ok=True)
        t1_path = a1_dir / "01 Airbag.flac"
        t1_path.write_bytes(b"dummy flac content")

        cur.execute(
            "INSERT INTO items (id, album_id, path, title, artist, album, albumartist, genre, year, track, disc, length, format, bitrate, samplerate, bitdepth, mb_trackid, mb_albumid, added) "
            "VALUES (101, 1, ?, 'Airbag', 'Radiohead', 'OK Computer', 'Radiohead', 'Rock', 1997, 1, 1, 284.0, 'FLAC', 850000, 44100, 16, "
            "  '4b1a134a-921c-4b6e-8212-8414cb2f57b8', '8b556b6b-d366-3d23-bf7b-232185a5a1f2', 1710000001.0)",
            (str(t1_path),)
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

    def test_direct_http_auth_failure_returns_401(self):
        """Requests without token or wrong token return 401."""
        status, body = self._http_request("GET", "/stats/library", headers={"Authorization": "Bearer wrong_token"})
        self.assertEqual(status, 401)

    def test_direct_http_raw_query_returns_403(self):
        """POST /library/raw_query returns 403 Forbidden unconditionally."""
        status, body = self._http_request("POST", "/library/raw_query", {"query": "SELECT * FROM albums"})
        self.assertEqual(status, 403)
        self.assertIn("not permitted", str(body))

    def test_direct_http_invalid_inputs_return_400_not_500(self):
        """Directly verify query parameters and bodies reject invalid input with 400 (never 500)."""
        adversarial_urls = [
            "/review/queue/unmatched?limit=-1",
            "/review/queue/unmatched?limit=invalid_int",
            "/review/queue/unmatched?offset=-1",
            "/stats/genres?missing_limit=-1",
            "/stats/genres?missing_limit=99999",
            "/clean/rgid-groups?limit=0",
            "/clean/rgid-groups?min_albums=1",
            "/clean/rgid-groups/not-a-valid-uuid",
            "/library/mbid-sticking/candidates?mode=invalid_mode_xyz",
            "/library/mbid-sticking/candidates?limit=0",
            "/albums/-1/mb-completeness",
            "/albums/not_a_number/mb-completeness",
            "/library/format-upgrades?format=INVALID!!",
            "/library/format-upgrades?limit=0",
            "/library/recording-replacements?mb_trackid=invalid-uuid",
        ]
        for url in adversarial_urls:
            with self.subTest(url=url):
                status, body = self._http_request("GET", url)
                self.assertEqual(status, 400, f"Expected 400 for {url}, got {status}: {body}")

    def test_sqlite_integrity_check(self):
        """Verify database remains clean and uncorrupted after adversarial requests."""
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        self.assertEqual(rows, [("ok",)], f"Integrity check failed: {rows}")
        con.close()


class TestBeetsClientTimeoutChallenger(unittest.TestCase):
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

        def _slow_drain():
            while cls.running:
                try:
                    cls.hanging_sock.settimeout(0.2)
                    conn, _ = cls.hanging_sock.accept()
                    threading.Thread(target=lambda c: time.sleep(1.0), args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    break

        cls.server_thread = threading.Thread(target=_slow_drain, daemon=True)
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


if __name__ == "__main__":
    unittest.main()
