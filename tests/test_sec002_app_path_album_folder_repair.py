"""SEC-002 Wave 4 regression tests for app.py album-folder repair and
Import Review album relocation path boundaries.

The selected CodeQL alerts are rooted in request/stored paths flowing into
Import Review target scans and album cleanup move/quarantine operations. These
tests use real temporary roots, files, symlinks, Flask requests, job execution,
and SQLite path rows.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
from backend import beets_control_agent as control_agent


RGID = "11111111-1111-1111-1111-111111111111"
RELEASE_ID = "22222222-2222-2222-2222-222222222222"
TRACK_ID = "33333333-3333-3333-3333-333333333333"
OTHER_RGID = "44444444-4444-4444-4444-444444444444"


def _summary():
    return {
        "files_moved": 0,
        "artwork_moved": 0,
        "duplicate_files_quarantined": 0,
        "folders_deleted": 0,
        "db_paths_updated": 0,
        "errors": 0,
        "completed": 0,
        "blocked": 0,
    }


class Wave4PathTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.downloads = self.root / "downloads"
        self.music = self.root / "music"
        self.outside = self.root / "outside"
        self.state = self.root / "state"
        for path in (self.downloads, self.music, self.outside, self.state):
            path.mkdir(parents=True, exist_ok=True)
        self.patches = [
            mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}),
            mock.patch.object(app_module, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(app_module, "_DOWNLOADS_ROOTS", [self.downloads]),
            mock.patch.object(app_module, "MUSIC_ROOT", self.music),
            mock.patch.object(app_module, "_MUSIC_LIBRARY_ROOT", str(self.music)),
            mock.patch.object(app_module, "METADATA_CACHE_ROOT", self.state),
            mock.patch.object(app_module, "ALBUM_FOLDER_CLEANUP_LAST_FILE", self.state / "album-folder-cleanup-last.json"),
            mock.patch.object(app_module, "_IMPORT_TARGET_PREVIEW_CACHE_TTL", 0),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        app_module._IMPORT_TARGET_PREVIEW_CACHE.clear()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def preview_payload(self, folder: Path, source_files=None):
        source_files = list(source_files or [])
        mapping = [
            {"status": "matched", "track": idx, "title": path.stem, "source_path": str(path)}
            for idx, path in enumerate(source_files, start=1)
        ]
        return {
            "path": str(folder),
            "artist": "Artist",
            "album": "Album",
            "year": "2024",
            "mb_releasegroupid": RGID,
            "representative_release_id": RELEASE_ID,
            "identity_validated": True,
            "track_mapping": mapping,
        }


class ImportReviewAlbumRelocationBoundaryTests(Wave4PathTestCase):
    def test_import_target_preview_preserves_literal_percent_filenames(self):
        folder = self.downloads / "Artist" / "Album"
        folder.mkdir(parents=True)
        filenames = [
            "convention%20album.flac",
            "literal%2520name.flac",
            "100%25 legit.flac",
            "percent%file.flac",
        ]
        files = []
        for name in filenames:
            path = folder / name
            path.write_bytes(b"audio")
            files.append(path)

        resp = self.client.post("/api/folders/import-target-preview", json=self.preview_payload(folder, files))
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["safe"], data)
        self.assertEqual(data["source_file_count"], len(files))
        returned = {Path(row["source_path"]).name for row in data["tracks"]}
        self.assertEqual(returned, set(filenames))
        self.assertNotIn("convention album.flac", json.dumps(data))

    def test_import_target_preview_rejects_outside_root(self):
        outside_folder = self.outside / "Album"
        outside_folder.mkdir()
        sentinel = outside_folder / "sentinel.flac"
        sentinel.write_bytes(b"outside")

        resp = self.client.post("/api/folders/import-target-preview", json=self.preview_payload(outside_folder))
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertFalse(data["safe"], data)
        self.assertEqual(data["source_file_count"], 0)
        self.assertTrue(any("outside" in reason.lower() for reason in data["blocked_reasons"]))
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertNotIn(str(outside_folder), json.dumps(data))

    def test_import_target_preview_rejects_encoded_traversal(self):
        sentinel = self.outside / "sentinel.flac"
        sentinel.write_bytes(b"outside")
        payload = self.preview_payload(self.downloads)
        payload["path"] = str(self.downloads) + "/%2e%2e/outside"

        resp = self.client.post("/api/folders/import-target-preview", json=payload)
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertFalse(data["safe"], data)
        self.assertTrue(any("traversal" in reason.lower() for reason in data["blocked_reasons"]))
        self.assertEqual(sentinel.read_bytes(), b"outside")

    def test_import_with_id_rejects_selected_source_outside_source_folder_before_job(self):
        folder = self.downloads / "Artist" / "Album"
        folder.mkdir(parents=True)
        (folder / "01 Real.flac").write_bytes(b"real")
        outside_file = self.outside / "escape.flac"
        outside_file.write_bytes(b"outside")
        payload = {
            "path": str(folder),
            "mb_albumid": RELEASE_ID,
            "mb_releasegroupid": RGID,
            "existing_album_id": 123,
            "selected_source_files": [str(outside_file)],
            "track_mapping": [{"status": "matched", "title": "escape", "source_path": str(outside_file)}],
        }

        with mock.patch.object(app_module.jobs, "start_python") as start_python:
            resp = self.client.post("/api/folders/import-with-id", json=payload)

        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertEqual(data["error"], "No verified selected source files were found for import.")
        start_python.assert_not_called()
        self.assertEqual(outside_file.read_bytes(), b"outside")
        self.assertNotIn(str(outside_file), json.dumps(data))

    def test_import_with_id_rejects_symlink_source_folder_before_job(self):
        target = self.outside / "escape-folder"
        target.mkdir()
        (target / "01 Escape.flac").write_bytes(b"outside")
        link = self.downloads / "linked-album"
        self.make_symlink(link, target, directory=True)
        payload = {"path": str(link), "mb_albumid": RELEASE_ID, "mb_releasegroupid": RGID, "existing_album_id": 123}

        with mock.patch.object(app_module.jobs, "start_python") as start_python:
            resp = self.client.post("/api/folders/import-with-id", json=payload)

        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertIn("symlink", data["error"].lower())
        start_python.assert_not_called()
        self.assertTrue((target / "01 Escape.flac").exists())

    def test_candidate_tracks_ignores_nested_symlink_file_escape(self):
        folder = self.downloads / "Artist" / "Album"
        folder.mkdir(parents=True)
        real = folder / "01 Real.flac"
        real.write_bytes(b"real")
        outside_file = self.outside / "02 Escape.flac"
        outside_file.write_bytes(b"outside")
        self.make_symlink(folder / "02 Escape.flac", outside_file)

        def fake_tracklist(_mbid, _log):
            return {
                "ok": True,
                "release_group": RGID,
                "tracks": [{"title": "Real", "track": 1, "disc": 1, "mb_trackid": TRACK_ID}],
            }

        with mock.patch.object(app_module, "_fetch_mb_release_tracklist", side_effect=fake_tracklist), \
             mock.patch.object(app_module, "_album_track_fingerprint_check", return_value={"status": "unknown"}):
            resp = self.client.get(f"/api/candidates/{RELEASE_ID}/tracks", query_string={"folder": str(folder)})

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        sources = {row.get("source_path", "") for row in data.get("comparison", [])}
        self.assertIn(str(real.resolve(strict=False)), sources)
        self.assertNotIn(str(outside_file.resolve(strict=False)), sources)
        self.assertEqual(outside_file.read_bytes(), b"outside")

    def test_normalised_submission_files_filters_hostile_paths(self):
        folder = self.downloads / "Artist" / "Album"
        folder.mkdir(parents=True)
        literal = folder / "literal%2520name.flac"
        literal.write_bytes(b"audio")
        hostile = [
            str(self.outside / "escape.flac"),
            str(self.downloads) + "/%2e%2e/outside/escape.flac",
            str(self.downloads) + "/%252e%252e/outside/escape.flac",
            str(self.downloads) + "\\..\\outside\\escape.flac",
        ]

        result = app_module._normalised_submission_files([str(literal), *hostile])

        self.assertEqual(result, [str(literal.resolve(strict=False))])


class AlbumFolderCleanupApplyBoundaryTests(Wave4PathTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = self.root / "library.sqlite"
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, title TEXT, artist TEXT, album TEXT, path BLOB)")
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, artpath BLOB)")
            con.commit()
        patch = mock.patch.object(app_module, "get_db_connection", side_effect=lambda _path=None: sqlite3.connect(self.db_path))
        patch.start()
        self.addCleanup(patch.stop)

    def issue(self, source: Path, target: Path, *, rgid: str = RGID, issue_types=None, safety: str = "Safe"):
        return {
            "id": "issue-1",
            "artist": "Artist",
            "album": "Album",
            "release_group_id": rgid,
            "current_folders": [str(source), str(target)],
            "canonical_folder": str(target),
            "proposed_canonical_folder": str(target),
            "issue_types": issue_types or ["duplicate_album_folders", "same_release_group_id"],
            "safety": safety,
            "safe": safety == "Safe",
        }

    def insert_item_path(self, item_id: int, path: Path) -> None:
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, path) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, 1, path.stem, "Artist", "Album", str(path).encode("utf-8")),
            )
            con.commit()

    def db_paths(self):
        with closing(sqlite3.connect(self.db_path)) as con:
            rows = con.execute("SELECT path FROM items ORDER BY id").fetchall()
        return [row[0].decode("utf-8") if isinstance(row[0], bytes) else str(row[0]) for row in rows]

    def test_apply_moves_literal_percent_files_and_updates_sqlite_paths(self):
        source = self.music / "Artist" / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        filenames = ["convention%20album.flac", "literal%2520name.flac", "100%25 legit.flac", "percent%file.flac"]
        source_files = []
        for idx, name in enumerate(filenames, start=1):
            path = source / name
            path.write_bytes(f"audio-{idx}".encode("utf-8"))
            source_files.append(path)
            self.insert_item_path(idx, path)
        outside_sentinel = self.outside / "sentinel.flac"
        outside_sentinel.write_bytes(b"outside")
        log = []
        operations = []
        rewrite_calls = []

        def fake_rewrite(old_path: str, new_path: str):
            rewrite_calls.append((Path(old_path), Path(new_path)))
            old_abs = str(Path(old_path))
            old_rel = Path(old_path).relative_to(self.music).as_posix()
            new_rel = Path(new_path).relative_to(self.music).as_posix()
            changed = 0
            with closing(sqlite3.connect(self.db_path)) as con:
                cur = con.execute(
                    "UPDATE items SET path=? WHERE path=? OR path=?",
                    (new_rel.encode("utf-8"), old_rel.encode("utf-8"), old_abs.encode("utf-8")),
                )
                changed += max(0, cur.rowcount)
                con.commit()
            return {"ok": True, "changed": changed}

        with mock.patch.object(app_module.beets_client, "rewrite_library_path", side_effect=fake_rewrite):
            result = app_module._album_cleanup_apply_issue(
                self.issue(source, target), self.music, self.state / "trash", log, _summary(), operations
            )

        self.assertEqual(result["status"], "Completed", (result, log))
        self.assertFalse(source.exists())
        self.assertEqual({path.name for path in target.iterdir()}, set(filenames))
        self.assertEqual({Path(path).name for path in self.db_paths()}, set(filenames))
        self.assertEqual(outside_sentinel.read_bytes(), b"outside")
        self.assertEqual(len(operations), len(filenames))
        self.assertEqual(len(rewrite_calls), len(filenames))
        for operation in operations:
            self.assertTrue(Path(operation["source"]).is_absolute())
            self.assertTrue(Path(operation["target"]).is_absolute())
            Path(operation["target"]).resolve(strict=False).relative_to(self.music.resolve(strict=False))
        for old_path, new_path in rewrite_calls:
            old_path.resolve(strict=False).relative_to(self.music.resolve(strict=False))
            new_path.resolve(strict=False).relative_to(self.music.resolve(strict=False))
            self.assertFalse(new_path.is_symlink())

    def test_apply_blocks_outside_stored_source(self):
        source = self.outside / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        sentinel = source / "01 Escape.flac"
        sentinel.write_bytes(b"outside")
        operations = []

        result = app_module._album_cleanup_apply_issue(
            self.issue(source, target), self.music, self.state / "trash", [], _summary(), operations
        )

        self.assertEqual(result["status"], "Blocked", result)
        self.assertTrue(any("outside" in reason.lower() for reason in result["blocking_reasons"]))
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertEqual(operations, [])

    def test_apply_blocks_missing_release_group_identity(self):
        source = self.music / "Artist" / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        source_file = source / "01 Song.flac"
        source_file.write_bytes(b"audio")

        result = app_module._album_cleanup_apply_issue(
            self.issue(source, target, rgid=""), self.music, self.state / "trash", [], _summary(), []
        )

        self.assertEqual(result["status"], "Blocked", result)
        self.assertTrue(any("release group" in reason.lower() for reason in result["blocking_reasons"]))
        self.assertTrue(source_file.exists())

    def test_apply_blocks_conflicting_release_group_issue_type(self):
        source = self.music / "Artist" / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        source_file = source / "01 Song.flac"
        source_file.write_bytes(b"audio")

        result = app_module._album_cleanup_apply_issue(
            self.issue(source, target, issue_types=["duplicate_album_folders", "release_group_conflict"]),
            self.music,
            self.state / "trash",
            [],
            _summary(),
            [],
        )

        self.assertEqual(result["status"], "Blocked", result)
        self.assertTrue(any("conflict" in reason.lower() for reason in result["blocking_reasons"]))
        self.assertTrue(source_file.exists())

    def test_apply_blocks_target_parent_symlink_escape(self):
        source = self.music / "Artist" / "Album (2024)"
        source.mkdir(parents=True)
        source_file = source / "01 Song.flac"
        source_file.write_bytes(b"audio")
        outside_parent = self.outside / "redirect"
        outside_parent.mkdir()
        link_parent = self.music / "LinkedArtist"
        self.make_symlink(link_parent, outside_parent, directory=True)
        target = link_parent / f"Album (2024) {{{RGID}}}"

        result = app_module._album_cleanup_apply_issue(
            self.issue(source, target), self.music, self.state / "trash", [], _summary(), []
        )

        self.assertEqual(result["status"], "Blocked", result)
        self.assertTrue(any("symlink" in reason.lower() for reason in result["blocking_reasons"]))
        self.assertTrue(source_file.exists())
        self.assertFalse((outside_parent / f"Album (2024) {{{RGID}}}").exists())

    def test_apply_blocks_destination_leaf_race_collision(self):
        source = self.music / "Artist" / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        source_file = source / "01 Song.flac"
        target_file = target / "01 Song.flac"
        source_file.write_bytes(b"source-audio")
        target_file.write_bytes(b"different-audio")

        result = app_module._album_cleanup_apply_issue(
            self.issue(source, target), self.music, self.state / "trash", [], _summary(), []
        )

        self.assertEqual(result["status"], "Blocked", result)
        self.assertTrue(any("target file exists" in reason.lower() or "audio differs" in reason.lower() for reason in result["blocking_reasons"]))
        self.assertEqual(source_file.read_bytes(), b"source-audio")
        self.assertEqual(target_file.read_bytes(), b"different-audio")

    def test_apply_issue_route_replans_and_blocks_outside_stored_issue(self):
        source = self.outside / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        sentinel = source / "01 Escape.flac"
        sentinel.write_bytes(b"outside")
        plan = {
            "root": str(self.music),
            "summary": {},
            "errors": [],
            "issues": [self.issue(source, target)],
        }
        captured = {}

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            captured["result"] = fn(log, cancel_event=None, update_state=lambda _state: None)
            captured["log"] = log
            captured["metadata"] = metadata
            return SimpleNamespace(job_id="job-wave4")

        with mock.patch.object(app_module.jobs, "all", return_value=[]), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python), \
             mock.patch.object(app_module, "_album_folder_cleanup_plan", return_value=plan):
            resp = self.client.post("/api/clean/album-folders/apply-issue", json={"issue_id": "issue-1", "confirmed": True})

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["job_id"], "job-wave4")
        result = captured["result"]
        self.assertEqual(result["issues"][0]["status"], "Blocked", result)
        self.assertTrue(any("outside" in reason.lower() for reason in result["errors"]))
        self.assertEqual(sentinel.read_bytes(), b"outside")
        self.assertEqual(result["operations"], [])
        self.assertNotIn(str(source), json.dumps(data))


class ReviewPathsEqualFailClosedTests(Wave4PathTestCase):
    """_review_paths_equal() authorizes destructive Pending Review cleanup
    (_pending_review_matches). It must fail closed -- never fall back to
    weak raw-string comparison -- when either side cannot be trust-resolved."""

    def test_stored_path_outside_root_is_not_equal_even_with_matching_text(self):
        outside = self.outside / "hostile"
        outside.mkdir()
        # Same raw text on both sides after naive backslash/trailing-slash
        # normalization, but outside the approved roots -- must not match.
        self.assertFalse(app_module._review_paths_equal(str(outside), str(outside) + "/"))

    def test_symlink_path_is_not_equal_to_its_target(self):
        target = self.outside / "target"
        target.mkdir()
        link = self.downloads / "linked"
        self.make_symlink(link, target, directory=True)
        self.assertFalse(app_module._review_paths_equal(str(link), str(target)))

    def test_two_valid_equivalent_paths_are_equal(self):
        folder = self.downloads / "Artist" / "Album"
        folder.mkdir(parents=True)
        self.assertTrue(app_module._review_paths_equal(str(folder), str(folder) + "/"))

    def test_pending_review_match_refuses_hostile_stored_path(self):
        folder = self.downloads / "Artist" / "Album"
        folder.mkdir(parents=True)
        pending_file = self.state / "ai_pending_review.json"
        pending_file.write_text(json.dumps([{"path": str(self.outside / "hostile")}]), encoding="utf-8")
        with mock.patch.object(app_module, "_AI_PENDING_FILE", pending_file):
            self.assertFalse(app_module._pending_review_matches(str(folder)))


class AlbumCleanupMoveDbFailureRecoveryTests(Wave4PathTestCase):
    """When the Beets-engine path rewrite fails after a file has already
    been moved, the move must be reverted rather than leaving the
    filesystem and database split with only a truthful error message."""

    def issue(self, source: Path, target: Path):
        return {
            "id": "issue-1",
            "artist": "Artist",
            "album": "Album",
            "release_group_id": RGID,
            "current_folders": [str(source), str(target)],
            "canonical_folder": str(target),
            "proposed_canonical_folder": str(target),
            "issue_types": ["duplicate_album_folders", "same_release_group_id"],
            "safety": "Safe",
            "safe": True,
        }

    def test_file_move_is_reverted_when_db_rewrite_fails(self):
        source = self.music / "Artist" / "Album (2024)"
        target = self.music / "Artist" / f"Album (2024) {{{RGID}}}"
        source.mkdir(parents=True)
        target.mkdir(parents=True)
        source_file = source / "01 Song.flac"
        source_file.write_bytes(b"audio")

        with mock.patch.object(
            app_module.beets_client, "rewrite_library_path",
            side_effect=RuntimeError("engine unavailable"),
        ):
            result = app_module._album_cleanup_apply_issue(
                self.issue(source, target), self.music, self.state / "trash", [], _summary(), [],
            )

        self.assertEqual(result["status"], "Blocked", result)
        # The file must be back at its original location, not stranded at
        # the destination with the database still pointing at the source.
        self.assertTrue(source_file.exists())
        self.assertEqual(source_file.read_bytes(), b"audio")
        self.assertFalse((target / "01 Song.flac").exists())


class ControlAgentLibraryRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.music = self.root / "music"
        self.music.mkdir()
        self.db_path = self.root / "musiclibrary.blb"
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
            con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, artpath BLOB)")
            con.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def post_rewrite(self, payload):
        body = json.dumps(payload).encode("utf-8")
        handler = control_agent.ControlAgentHandler.__new__(control_agent.ControlAgentHandler)
        handler.path = "/library/rewrite-path"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler._authenticate = lambda: True
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        with mock.patch.object(control_agent, "LIB_PATH", str(self.db_path)), \
             mock.patch.object(control_agent, "MUSIC_LIBRARY_PATH", str(self.music)), \
             mock.patch.object(control_agent, "acquire_os_lock", return_value=None), \
             mock.patch.object(control_agent, "release_os_lock"):
            handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_engine_rewrite_library_path_updates_real_sqlite_rows(self):
        if os.name == "nt":
            self.skipTest("control-agent path rewrite success uses POSIX container paths; covered by Docker runtime")
        folder = self.music / "Artist"
        folder.mkdir()
        old_path = folder / "old.flac"
        new_path = folder / "new.flac"
        new_path.write_bytes(b"audio")
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute("INSERT INTO items VALUES (?, ?)", (1, "Artist/old.flac"))
            con.execute("INSERT INTO albums VALUES (?, ?)", (1, str(old_path)))
            con.commit()

        code, data = self.post_rewrite({"old_path": str(old_path), "new_path": str(new_path)})

        self.assertEqual(code, 200, data)
        self.assertEqual(data["changed"], 2)
        self.assertEqual(data["stored_path"], "Artist/new.flac")
        with closing(sqlite3.connect(self.db_path)) as con:
            self.assertEqual(con.execute("SELECT path FROM items WHERE id=1").fetchone()[0], "Artist/new.flac")
            self.assertEqual(con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0], "Artist/new.flac")

    def test_engine_rewrite_library_path_rejects_outside_root(self):
        outside = self.root / "outside.flac"
        outside.write_bytes(b"outside")
        new_path = self.music / "new.flac"
        new_path.write_bytes(b"audio")

        code, data = self.post_rewrite({"old_path": str(outside), "new_path": str(new_path)})

        self.assertEqual(code, 403, data)
        self.assertNotIn(str(outside), json.dumps(data))

    def test_engine_rewrite_library_path_rejects_symlink_components(self):
        real = self.music / "Real"
        real.mkdir()
        link = self.music / "Linked"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        old_path = real / "old.flac"
        new_path = link / "new.flac"
        new_path.write_bytes(b"audio")

        code, data = self.post_rewrite({"old_path": str(old_path), "new_path": str(new_path)})

        self.assertEqual(code, 403, data)
        self.assertNotIn(str(new_path), json.dumps(data))

    def test_engine_rewrite_library_path_rejects_when_zero_rows_match(self):
        # old_path is a real, valid, in-library file but was never actually
        # stored in the DB -- the rewrite must not silently report success
        # (and must not commit) when nothing was actually changed.
        if os.name == "nt":
            self.skipTest("control-agent path rewrite uses POSIX container paths; covered by Docker runtime")
        folder = self.music / "Artist"
        folder.mkdir()
        old_path = folder / "old.flac"
        old_path.write_bytes(b"audio")
        new_path = folder / "new.flac"
        new_path.write_bytes(b"audio")

        code, data = self.post_rewrite({"old_path": str(old_path), "new_path": str(new_path)})

        self.assertEqual(code, 404, data)
        self.assertFalse(data.get("ok"))
        with closing(sqlite3.connect(self.db_path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
