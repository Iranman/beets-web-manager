"""ARCH-007 Milestone 1 Iteration 2 Challenger Deep Empirical Probe Suite.

Empirically challenges:
1. Path traversal in POST /maintenance/artist-folders/stamp-mbids (blocked with HTTP 403).
2. Strict route matching in GET /albums/<aid>/mb-completeness (rejects injected segments with HTTP 400).
3. BeetsClient HTTP 403 mapping to BeetsAuthError(status_code=403).
4. Client null-byte and parameter validation in stamp_artist_folder_mbids.
5. Control Agent error taxonomy and fail-closed contracts (never 500 on malformed input, 404 when DB missing).
6. Parameter bounds, wildcards, and edge cases across all new endpoints.
7. Concurrency and OS lock release verification.
8. Database integrity post-adversarial challenge.
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


class TestChallengerM1_2RemediationAndDeepProbe(unittest.TestCase):
    """Exhaustive empirical challenger suite for Milestone 1 Iteration 2."""

    @classmethod
    def setUpClass(cls):
        cls.token = "m1_2_challenger_secret_token_abcdef1234567890"
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

        cls._init_db()

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
    def _init_db(cls):
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
            "  title TEXT, "
            "  artist TEXT, "
            "  albumartist TEXT, "
            "  album TEXT, "
            "  genre TEXT, "
            "  year INTEGER, "
            "  track INTEGER, "
            "  disc INTEGER, "
            "  length REAL, "
            "  bitrate INTEGER, "
            "  samplerate INTEGER, "
            "  bitdepth INTEGER, "
            "  format TEXT, "
            "  path BLOB, "
            "  added REAL, "
            "  mb_trackid TEXT, "
            "  mb_albumid TEXT, "
            "  mb_artistid TEXT"
            ")"
        )
        # Seed test album 1 (complete)
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (1, 'OK Computer', 'Radiohead', 1997, 'b951c31a-df28-40b9-8735-86641f9bead7', "
            "'19696cf6-0062-38b4-82f7-eef4f1db12e2', 'a74b1b7f-71a5-4011-9441-d0b5e4122711')"
        )
        # Seed test tracks for album 1
        cur.execute(
            "INSERT INTO items (id, album_id, title, artist, albumartist, album, format, path, added, mb_trackid, mb_albumid) "
            "VALUES (101, 1, 'Airbag', 'Radiohead', 'Radiohead', 'OK Computer', 'FLAC', "
            "?, 1700000000.0, '23e8c973-c1cf-41c3-a3b0-6f917537cb90', 'b951c31a-df28-40b9-8735-86641f9bead7')",
            (str(cls.music_dir / "Radiohead" / "OK Computer" / "01 Airbag.flac"),)
        )
        cur.execute(
            "INSERT INTO items (id, album_id, title, artist, albumartist, album, format, path, added, mb_trackid, mb_albumid) "
            "VALUES (102, 1, 'Paranoid Android', 'Radiohead', 'Radiohead', 'OK Computer', 'FLAC', "
            "?, 1700000001.0, '3a53c153-294b-4b13-a417-64016d953930', 'b951c31a-df28-40b9-8735-86641f9bead7')",
            (str(cls.music_dir / "Radiohead" / "OK Computer" / "02 Paranoid Android.flac"),)
        )
        # Seed test album 2 (incomplete / missing track mbids)
        cur.execute(
            "INSERT INTO albums (id, album, albumartist, year, mb_albumid, mb_releasegroupid, mb_albumartistid) "
            "VALUES (2, 'The Bends', 'Radiohead', 1995, '916d6105-021c-43f1-b92e-9d2a67e8a937', "
            "'19696cf6-0062-38b4-82f7-eef4f1db12e2', 'a74b1b7f-71a5-4011-9441-d0b5e4122711')"
        )
        cur.execute(
            "INSERT INTO items (id, album_id, title, artist, albumartist, album, format, path, added, mb_trackid, mb_albumid) "
            "VALUES (201, 2, 'Planet Telex', 'Radiohead', 'Radiohead', 'The Bends', 'MP3', "
            "?, 1700000002.0, '', '916d6105-021c-43f1-b92e-9d2a67e8a937')",
            (str(cls.music_dir / "Radiohead" / "The Bends" / "01 Planet Telex.mp3"),)
        )
        con.commit()
        con.close()

    def _http_request(self, method: str, path: str, body=None, headers=None):
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

    # =========================================================================
    # ITEM 1: Path Traversal in POST /maintenance/artist-folders/stamp-mbids
    # =========================================================================

    def test_item1_stamp_mbids_outside_paths_rejected_with_403(self):
        """Verify that all paths outside MUSIC_LIBRARY_PATH return HTTP 403."""
        outside_test_dir = Path(self.tmpdir.name) / "outside_dir"
        outside_test_dir.mkdir(exist_ok=True)

        test_cases = [
            str(outside_test_dir),
            str(self.music_dir / ".." / "outside_dir"),
            str(self.music_dir / ".." / ".." / "tmp"),
            "/tmp/arbitrary_path",
            "/etc",
            "C:\\Windows\\System32",
            str(self.music_dir) + "_sibling_dir",  # prefix overlap defense
            str(self.music_dir) + "/../music2",
        ]

        valid_mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
        for candidate_path in test_cases:
            with self.subTest(candidate=candidate_path):
                status, body = self._http_request(
                    "POST",
                    "/maintenance/artist-folders/stamp-mbids",
                    {"folder_path": candidate_path, "mbid": valid_mbid, "dry_run": False},
                )
                self.assertEqual(
                    status, 403,
                    f"Path outside music library '{candidate_path}' was not blocked with 403! Got: {status}, {body}"
                )
                self.assertIsInstance(body, dict)
                self.assertEqual(body.get("error_code"), "FORBIDDEN_PATH")
                # Confirm no stamp file created in outside directory
                self.assertFalse((outside_test_dir / ".artist_mbids.json").exists())

    def test_item1_stamp_mbids_null_byte_rejected_with_403(self):
        """Verify null byte in folder_path returns HTTP 403 on server."""
        valid_mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
        null_byte_path = str(self.music_dir / "Artist\x00extra")
        status, body = self._http_request(
            "POST",
            "/maintenance/artist-folders/stamp-mbids",
            {"folder_path": null_byte_path, "mbid": valid_mbid, "dry_run": False},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error_code"), "FORBIDDEN_PATH")

    def test_item1_stamp_mbids_inside_path_succeeds_200(self):
        """Verify valid path inside MUSIC_LIBRARY_PATH succeeds with HTTP 200."""
        valid_artist_dir = self.music_dir / "Radiohead"
        valid_artist_dir.mkdir(exist_ok=True)
        valid_mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"

        status, body = self._http_request(
            "POST",
            "/maintenance/artist-folders/stamp-mbids",
            {"folder_path": str(valid_artist_dir), "mbid": valid_mbid, "dry_run": False},
        )
        self.assertEqual(status, 200, f"Expected 200, got {status}: {body}")
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("stamped_count"), 1)
        stamp_file = valid_artist_dir / ".artist_mbids.json"
        self.assertTrue(stamp_file.exists())
        with open(stamp_file, "r", encoding="utf-8") as f:
            stamp_data = json.load(f)
            self.assertEqual(stamp_data["mbid"], valid_mbid)

    # =========================================================================
    # ITEM 2: Strict Route Matching in GET /albums/<aid>/mb-completeness
    # =========================================================================

    def test_item2_mb_completeness_rejects_injected_segments_with_400(self):
        """Verify strict 4-segment schema rejects injected path segments with HTTP 400."""
        injected_paths = [
            "/albums/1/injected/mb-completeness",
            "/albums/1/segment2/segment3/mb-completeness",
            "/albums/1/admin/mb-completeness",
            "/albums/1/delete/mb-completeness",
            "/albums//1/mb-completeness",
            "/albums/1//mb-completeness",
            "/albums/1/./mb-completeness",
            "/albums/1/*comment*/mb-completeness",
        ]
        for p in injected_paths:
            with self.subTest(path=p):
                status, body = self._http_request("GET", p)
                self.assertEqual(
                    status, 400,
                    f"Route '{p}' did not return HTTP 400! Got: {status}, {body}"
                )
                self.assertIsInstance(body, dict)
                self.assertEqual(body.get("error_code"), "INVALID_ALBUM_ID")

    def test_item2_mb_completeness_invalid_aid_types_rejected_with_400(self):
        """Verify non-positive or non-integer album IDs return HTTP 400."""
        invalid_aids = [
            "/albums/0/mb-completeness",
            "/albums/-1/mb-completeness",
            "/albums/-999/mb-completeness",
            "/albums/abc/mb-completeness",
            "/albums/1.5/mb-completeness",
            "/albums/true/mb-completeness",
        ]
        for p in invalid_aids:
            with self.subTest(path=p):
                status, body = self._http_request("GET", p)
                self.assertEqual(
                    status, 400,
                    f"Route '{p}' did not return HTTP 400! Got: {status}, {body}"
                )
                self.assertEqual(body.get("error_code"), "INVALID_ALBUM_ID")

    def test_item2_mb_completeness_valid_aids(self):
        """Verify valid album IDs return 200 when found and 404 when not found."""
        # Album 1 exists
        status, body = self._http_request("GET", "/albums/1/mb-completeness")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("album_id"), 1)
        self.assertTrue(body.get("is_complete"))
        self.assertEqual(body.get("total_tracks"), 2)

        # Album 2 exists but incomplete
        status, body = self._http_request("GET", "/albums/2/mb-completeness")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("album_id"), 2)
        self.assertFalse(body.get("is_complete"))

        # Album 99999 does not exist -> 404
        status, body = self._http_request("GET", "/albums/99999/mb-completeness")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error_code"), "ALBUM_NOT_FOUND")

    # =========================================================================
    # ITEM 3: BeetsClient HTTP 403 Mapping to BeetsAuthError(status_code=403)
    # =========================================================================

    def test_item3_beets_client_maps_403_to_beets_auth_error(self):
        """Verify BeetsClient maps HTTP 403 responses to BeetsAuthError with status_code=403."""
        # 1. Direct call to raw_query endpoint which returns 403
        with self.assertRaises(BeetsAuthError) as ctx:
            self.client._request("POST", "/library/raw_query", {"query": "SELECT 1"})
        exc = ctx.exception
        self.assertEqual(exc.status_code, 403)
        self.assertIn("Raw SQL queries are not permitted", str(exc))
        self.assertIn("forbidden", str(exc).lower())
        self.assertTrue(issubclass(BeetsAuthError, BeetsError))

        # 2. Call to stamp-mbids with forbidden path which returns 403
        outside_path = str(Path(self.tmpdir.name) / "outside")
        valid_mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
        with self.assertRaises(BeetsAuthError) as ctx2:
            self.client._request(
                "POST",
                "/maintenance/artist-folders/stamp-mbids",
                {"folder_path": outside_path, "mbid": valid_mbid},
            )
        exc2 = ctx2.exception
        self.assertEqual(exc2.status_code, 403)
        self.assertEqual(exc2.error_code, "FORBIDDEN_PATH")

    # =========================================================================
    # ITEM 4: Client Null-Byte & Parameter Validation in stamp_artist_folder_mbids
    # =========================================================================

    def test_item4_stamp_artist_folder_mbids_null_byte_validation(self):
        """Verify stamp_artist_folder_mbids validates null bytes, empty strings, types locally."""
        # Null byte in folder_path
        with self.assertRaises(BeetsBadRequestError) as ctx:
            self.client.stamp_artist_folder_mbids(folder_path="/music/Radiohead\x00hack")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.error_code, "INVALID_PARAMETER")

        # Empty string
        with self.assertRaises(BeetsBadRequestError) as ctx:
            self.client.stamp_artist_folder_mbids(folder_path="")
        self.assertEqual(ctx.exception.status_code, 400)

        # Whitespace only
        with self.assertRaises(BeetsBadRequestError) as ctx:
            self.client.stamp_artist_folder_mbids(folder_path="    ")
        self.assertEqual(ctx.exception.status_code, 400)

        # Non-string folder_path
        for bad_fp in [123, [], {}, True]:
            with self.subTest(bad_fp=bad_fp):
                with self.assertRaises(BeetsBadRequestError) as ctx:
                    self.client.stamp_artist_folder_mbids(folder_path=bad_fp)
                self.assertEqual(ctx.exception.status_code, 400)

        # Malformed MBID UUID
        for bad_mbid in ["not-a-uuid", "12345", "'; DROP TABLE items; --", ""]:
            with self.subTest(bad_mbid=bad_mbid):
                with self.assertRaises(BeetsBadRequestError) as ctx:
                    self.client.stamp_artist_folder_mbids(mbid=bad_mbid)
                self.assertEqual(ctx.exception.status_code, 400)

        # Non-list artist_folders
        for bad_af in ["not-a-list", 123, {}]:
            with self.subTest(bad_af=bad_af):
                with self.assertRaises(BeetsBadRequestError) as ctx:
                    self.client.stamp_artist_folder_mbids(artist_folders=bad_af)
                self.assertEqual(ctx.exception.status_code, 400)

    # =========================================================================
    # ITEM 5: Deep Adversarial Probing Across Remaining Control Agent Endpoints
    # =========================================================================

    def test_probe_resolve_folder_adversarial_inputs(self):
        """Probe POST /library/resolve-folder with invalid types, null bytes, negative since."""
        # Null byte
        status, body = self._http_request("POST", "/library/resolve-folder", {"folder_path": "/music/\x00evil"})
        self.assertEqual(status, 400)

        # Empty folder_path
        status, body = self._http_request("POST", "/library/resolve-folder", {"folder_path": ""})
        self.assertEqual(status, 400)

        # Non-string folder_path
        status, body = self._http_request("POST", "/library/resolve-folder", {"folder_path": 123})
        self.assertEqual(status, 400)

        # Negative since
        status, body = self._http_request("POST", "/library/resolve-folder", {"folder_path": "/music", "since": -10})
        self.assertEqual(status, 400)

        # Non-numeric since
        status, body = self._http_request("POST", "/library/resolve-folder", {"folder_path": "/music", "since": "abc"})
        self.assertEqual(status, 400)

    def test_probe_rgid_groups_merge_adversarial_inputs(self):
        """Probe POST /clean/rgid-groups/merge with invalid IDs, identical target/source, mismatched RGIDs."""
        # Identical target and source
        status, body = self._http_request("POST", "/clean/rgid-groups/merge", {
            "target_album_id": 1,
            "source_album_id": 1,
        })
        self.assertEqual(status, 400)

        # Negative target_album_id
        status, body = self._http_request("POST", "/clean/rgid-groups/merge", {
            "target_album_id": -1,
            "source_album_id": 2,
        })
        self.assertEqual(status, 400)

        # Non-existent album IDs
        status, body = self._http_request("POST", "/clean/rgid-groups/merge", {
            "target_album_id": 99999,
            "source_album_id": 99998,
        })
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error_code"), "ALBUM_NOT_FOUND")

        # Invalid RGID UUID format
        status, body = self._http_request("POST", "/clean/rgid-groups/merge", {
            "target_album_id": 1,
            "source_album_id": 2,
            "mb_releasegroupid": "not-a-uuid",
        })
        self.assertEqual(status, 400)

    def test_probe_clean_orphans_and_empty_albums_adversarial_inputs(self):
        """Probe POST /clean/orphaned-items and POST /clean/empty-albums."""
        # Non-list item_ids
        status, body = self._http_request("POST", "/clean/orphaned-items", {"item_ids": "1,2,3"})
        self.assertEqual(status, 400)

        # Negative item_ids
        status, body = self._http_request("POST", "/clean/orphaned-items", {"item_ids": [1, -2]})
        self.assertEqual(status, 400)

        # Non-list album_ids
        status, body = self._http_request("POST", "/clean/empty-albums", {"album_ids": "1,2"})
        self.assertEqual(status, 400)

        # Negative album_ids
        status, body = self._http_request("POST", "/clean/empty-albums", {"album_ids": [-5]})
        self.assertEqual(status, 400)

    def test_probe_sync_deleted_adversarial_inputs(self):
        """Probe POST /library/sync-deleted boundary limits."""
        for bad_limit in [0, -1, 50001, "invalid"]:
            with self.subTest(bad_limit=bad_limit):
                status, body = self._http_request("POST", "/library/sync-deleted", {"limit": bad_limit})
                self.assertEqual(status, 400)

    def test_probe_find_hardlink_candidates_adversarial_inputs(self):
        """Probe POST /library/find-hardlink-candidates parameter validation and wildcards."""
        # Missing all parameters
        status, body = self._http_request("POST", "/library/find-hardlink-candidates", {})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "MISSING_SEARCH_PARAM")

        # Wildcards and special characters (must be safely escaped and not crash)
        wildcards = ["%", "_", "\\", "'", "\"", ";", "--", "[]"]
        for wc in wildcards:
            with self.subTest(wc=wc):
                status, body = self._http_request("POST", "/library/find-hardlink-candidates", {"filename": f"test{wc}track.mp3"})
                self.assertEqual(status, 200)
                self.assertTrue(body.get("ok"))
                self.assertIsInstance(body.get("candidates"), list)

    def test_probe_albums_merge_adversarial_inputs(self):
        """Probe POST /library/albums/merge with identical IDs, negative IDs, non-existent IDs."""
        # Identical target and source
        status, body = self._http_request("POST", "/library/albums/merge", {
            "target_album_id": 1,
            "source_album_id": 1,
        })
        self.assertEqual(status, 400)

        # Negative IDs
        status, body = self._http_request("POST", "/library/albums/merge", {
            "target_album_id": -1,
            "source_album_id": 2,
        })
        self.assertEqual(status, 400)

        # Non-existent albums
        status, body = self._http_request("POST", "/library/albums/merge", {
            "target_album_id": 88888,
            "source_album_id": 88889,
        })
        self.assertEqual(status, 404)
        self.assertEqual(body.get("error_code"), "ALBUM_NOT_FOUND")

    def test_probe_missing_db_file_returns_404_never_500(self):
        """Verify that when database file does not exist, endpoints return 404 NOT_FOUND."""
        non_existent_db = Path(self.tmpdir.name) / "non_existent_db.blb"
        with mock.patch.object(agent, "LIB_PATH", str(non_existent_db)):
            endpoints_to_test = [
                ("GET", "/stats/library"),
                ("GET", "/stats/genres"),
                ("GET", "/clean/rgid-groups"),
                ("GET", "/clean/rgid-groups/19696cf6-0062-38b4-82f7-eef4f1db12e2"),
                ("GET", "/library/mbid-sticking/candidates"),
                ("GET", "/albums/1/mb-completeness"),
                ("GET", "/library/artist-aliases"),
                ("GET", "/library/format-upgrades"),
                ("GET", "/library/recording-replacements?mb_trackid=23e8c973-c1cf-41c3-a3b0-6f917537cb90"),
                ("POST", "/library/resolve-folder", {"folder_path": "/music"}),
                ("POST", "/clean/rgid-groups/merge", {"target_album_id": 1, "source_album_id": 2}),
                ("POST", "/clean/orphaned-items", {}),
                ("POST", "/clean/empty-albums", {}),
                ("POST", "/library/sync-deleted", {}),
                ("POST", "/library/scan-integrity", {}),
                ("POST", "/library/find-hardlink-candidates", {"filename": "song.mp3"}),
                ("POST", "/library/albums/merge", {"target_album_id": 1, "source_album_id": 2}),
            ]
            for method, path, *extra in endpoints_to_test:
                body = extra[0] if extra else None
                with self.subTest(method=method, path=path):
                    status, res = self._http_request(method, path, body=body)
                    self.assertEqual(status, 404, f"Expected 404 for missing DB on {method} {path}, got {status}: {res}")
                    self.assertEqual(res.get("error_code"), "NOT_FOUND")

    def test_probe_os_lock_releases_cleanly(self):
        """Verify that OS lock is never left locked after successful or failed requests."""
        # Run a request that fails validation
        self._http_request("POST", "/library/resolve-folder", {"folder_path": ""})
        # Run a request that succeeds
        self._http_request("GET", "/stats/library")
        # Run a request that fails 404
        self._http_request("GET", "/albums/99999/mb-completeness")

        # Now acquire exclusive lock directly - should succeed immediately without deadlock
        lock = agent.acquire_os_lock(read_only=False)
        self.assertIsNotNone(lock)
        agent.release_os_lock(lock)

    def test_probe_database_integrity_post_challenge(self):
        """Verify SQLite database integrity is 100% clean post deep probing."""
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        self.assertEqual(rows, [("ok",)], f"Integrity check failed: {rows}")
        con.close()


if __name__ == "__main__":
    unittest.main()
