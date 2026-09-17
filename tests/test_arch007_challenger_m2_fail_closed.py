"""Milestone 2 Challenger: Adversarial Fail-Closed and Error Handling Verification.

Verifies:
1. When Beets engine is down (BeetsUnavailableError), review queue and other routes fail closed (HTTP 503).
2. Zero fallbacks to local SQLite: sqlite3.connect is NEVER called.
3. app._db() is NEVER called in any route when engine is down.
4. routes_submissions._find_beets_album_for_folder and _find_beets_items_for_folder
   do NOT swallow BeetsUnavailableError into fake success.
5. All newly migrated M2 callers fail closed safely without touching SQLite.
"""

import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from backend.beets_client import (
    BeetsClient,
    BeetsError,
    BeetsUnavailableError,
    BeetsAuthError,
)
import routes_submissions
from app import (
    app,
    lib,
    _db,
    _album_cleanup_db_index,
    _stamp_artist_folder_album_mbid_counts,
    _music_format_find_verified_replacement,
    _album_cleanup_item_id_for_path,
)


class TestArch007M2FailClosedAdversarial(unittest.TestCase):
    def setUp(self):
        self.env_patcher = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        self.env_patcher.start()

        self.app = app
        self.client = app.test_client()

        # Sentinel spy to catch any attempt to open local SQLite
        self.sqlite_connect_spy = mock.MagicMock(
            side_effect=AssertionError("ARCH-007 VIOLATION: sqlite3.connect called during Beets engine down!")
        )
        self.sqlite_patcher = mock.patch("sqlite3.connect", self.sqlite_connect_spy)
        self.sqlite_patcher.start()

        # Spy on app._db to ensure it is never called
        self.db_spy = mock.MagicMock(
            side_effect=AssertionError("ARCH-007 VIOLATION: app._db() called during Beets engine down!")
        )
        self.db_patcher = mock.patch("app._db", self.db_spy)
        self.db_patcher.start()

    def tearDown(self):
        self.sqlite_patcher.stop()
        self.db_patcher.stop()
        self.env_patcher.stop()

    def test_review_queue_fails_closed_503_on_beets_unavailable(self):
        """Review queue must return HTTP 503 with ENGINE_UNAVAILABLE when engine is down."""
        with mock.patch("app.beets_client.get_unmatched_review_items") as mock_unmatched:
            mock_unmatched.side_effect = BeetsUnavailableError("Engine connection refused: [WinError 10061]")

            response = self.client.get("/api/import/review-queue")

            self.assertEqual(
                response.status_code,
                503,
                f"Expected HTTP 503 on engine down, got {response.status_code}: {response.get_data(as_text=True)}",
            )
            data = json.loads(response.get_data(as_text=True))
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("error_code"), "ENGINE_UNAVAILABLE")
            self.assertIn("Beets engine unavailable", data.get("error", ""))

            # Confirm zero SQLite calls
            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_config_get_fails_closed_503_on_beets_unavailable(self):
        """GET /api/config must return HTTP 503 when engine is down."""
        with mock.patch("app.beets_client.get_config") as mock_get_config:
            mock_get_config.side_effect = BeetsUnavailableError("Connection timeout")

            response = self.client.get("/api/config")

            self.assertEqual(response.status_code, 503)
            data = json.loads(response.get_data(as_text=True))
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), "beets_unavailable")

            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_item_replacement_plan_fails_closed_503_on_beets_unavailable(self):
        """POST /api/items/<iid>/replacement/plan must return HTTP 503 on engine down."""
        fake_item = mock.Mock(path="/data/media/music/Artist/Album/track.mp3", mb_trackid="rec-123")
        with mock.patch("app.lib.get_item", return_value=fake_item), \
             mock.patch("app._resolve_import_review_source_path", return_value=(Path("/data/staging/replacement.flac"), None)), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("app._acoustid_fingerprint_match", return_value=("rec-123", {"rec-123"}, {"rec-123"})), \
             mock.patch("app.beets_client.plan_track_replacement") as mock_plan:
            mock_plan.side_effect = BeetsUnavailableError("Connection refused")

            response = self.client.post(
                "/api/items/42/replacement/plan",
                json={"candidate_path": "/data/staging/replacement.flac"},
            )

            self.assertEqual(response.status_code, 503)
            data = json.loads(response.get_data(as_text=True))
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), "beets_unavailable")

            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_item_replacement_apply_fails_closed_503_on_beets_unavailable(self):
        """POST /api/items/<iid>/replacement/apply must return HTTP 503 on engine down."""
        with mock.patch("app.beets_client.apply_track_replacement") as mock_apply:
            mock_apply.side_effect = BeetsUnavailableError("Connection refused")

            response = self.client.post(
                "/api/items/42/replacement/apply",
                json={"operation_id": "op-test-123"},
            )

            self.assertEqual(response.status_code, 503)
            data = json.loads(response.get_data(as_text=True))
            self.assertFalse(data.get("ok"))
            self.assertEqual(data.get("code"), "beets_unavailable")

            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_routes_submissions_find_beets_album_raises_on_beets_unavailable(self):
        """_find_beets_album_for_folder must re-raise BeetsUnavailableError, not swallow."""
        folder = Path("/data/media/music/Artist/Album")
        with mock.patch.object(routes_submissions.beets_client, "resolve_folder_to_albums") as mock_res:
            mock_res.side_effect = BeetsUnavailableError("Engine dead")

            with self.assertRaises(BeetsUnavailableError):
                routes_submissions._find_beets_album_for_folder(folder)

            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_routes_submissions_find_beets_items_raises_on_beets_unavailable(self):
        """_find_beets_items_for_folder must re-raise BeetsUnavailableError, not swallow."""
        folder = Path("/data/media/music/Artist/Album")
        with mock.patch.object(routes_submissions.beets_client, "resolve_folder_to_albums") as mock_res:
            mock_res.side_effect = BeetsUnavailableError("Engine dead")

            with self.assertRaises(BeetsUnavailableError):
                routes_submissions._find_beets_items_for_folder(folder)

            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_album_cleanup_db_index_no_sqlite_fallback(self):
        """_album_cleanup_db_index must handle client failure without local SQLite access."""
        with mock.patch("app.beets_client.get_album_cleanup_index") as mock_idx:
            mock_idx.side_effect = BeetsUnavailableError("Engine offline")

            res = _album_cleanup_db_index(Path("/data/music"))

            self.assertEqual(res, {"folders": {}, "files": {}})
            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_album_cleanup_item_id_for_path_no_sqlite_fallback(self):
        """_album_cleanup_item_id_for_path must handle client failure returning 0 without SQLite."""
        with mock.patch("app.beets_client.find_item_by_path") as mock_find:
            mock_find.side_effect = BeetsUnavailableError("Engine offline")

            item_id = _album_cleanup_item_id_for_path(Path("/data/music/track.mp3"))

            self.assertEqual(item_id, 0)
            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_stamp_artist_folder_album_mbid_counts_no_sqlite_fallback(self):
        """_stamp_artist_folder_album_mbid_counts returns error string on failure without SQLite."""
        with mock.patch("app.beets_client.get_artist_folder_album_mbids") as mock_mbids:
            mock_mbids.side_effect = BeetsUnavailableError("Engine offline")

            id_sets, totals, err = _stamp_artist_folder_album_mbid_counts(
                Path("/data/music"), [Path("/data/music/Artist")]
            )

            self.assertEqual(id_sets, {})
            self.assertEqual(totals, {})
            self.assertIn("Engine offline", err)
            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()

    def test_music_format_find_verified_replacement_no_sqlite_fallback(self):
        """_music_format_find_verified_replacement must not query SQLite on engine failure."""
        with mock.patch("app.beets_client.find_all_items_by_mbid") as mock_mbid, \
             mock.patch("app.beets_client.find_all_items_by_album_id") as mock_aid, \
             mock.patch("app.beets_client.get_items_page") as mock_page:
            mock_mbid.side_effect = BeetsUnavailableError("Engine offline")
            mock_aid.side_effect = BeetsUnavailableError("Engine offline")
            mock_page.side_effect = BeetsUnavailableError("Engine offline")

            row = {
                "album_id": 10,
                "item_id": 101,
                "path": "/data/music/track.mp3",
                "mb_trackid": "11111111-1111-1111-1111-111111111111",
            }
            res = _music_format_find_verified_replacement(row, {})

            self.assertEqual(res, {})
            self.sqlite_connect_spy.assert_not_called()
            self.db_spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
