"""SEC-002 app.py path-injection wave 2 regression tests.

These tests exercise the import-review source deletion and per-file cleanup
routes against real temporary folders, files, and symlinks. The sensitive sinks
(unlink, os.rename, rmdir, rmtree, and rglob) are not mocked.
"""

import json
import os
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import app as app_module


AUDIO_NAMES_WITH_LITERAL_PERCENT = [
    "convention%20album.flac",
    "literal%2520name.flac",
    "100%25 legit.flac",
    "percent%file.flac",
]


class ImportReviewCleanupPathBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.music = self.root / "music"
        self.downloads = self.root / "downloads"
        self.other_downloads = self.root / "other-downloads"
        self.tmp_downloads = self.root / "tmp-downloads"
        self.outside = self.root / "outside"
        self.quarantine = self.root / "quarantine"
        self.pending_file = self.root / "ai_pending_review.json"
        for directory in (
            self.music,
            self.downloads,
            self.other_downloads,
            self.tmp_downloads,
            self.outside,
            self.quarantine,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text("[]", encoding="utf-8")
        from backend.transaction_engine import TransactionStore, execute_import_review_cleanup_plan, execute_import_review_cleanup_apply
        self.tx_store = TransactionStore(root=str(self.root / "transactions"))

        def mock_plan(payload, **kwargs):
            roots = [
                str(self.downloads),
                str(self.music),
                str(self.other_downloads),
                str(self.tmp_downloads),
                str(self.quarantine),
            ]
            # execute_import_review_cleanup_plan no longer reaches into
            # sys.modules["app"] for MUSIC_ROOT (SEC-002 Wave 15 final
            # review fix: transaction_engine.py must not depend on app.py)
            # -- the caller must pass it explicitly, exactly as
            # beets_control_agent.py does in production.
            return execute_import_review_cleanup_plan(
                self.tx_store, payload, roots, music_root=str(self.music),
            )

        def mock_apply(op_id, **kwargs):
            return execute_import_review_cleanup_apply(self.tx_store, op_id, quarantine_root=str(self.quarantine))

        self.patches = [
            mock.patch.dict(os.environ, {
                "BEETS_WEB_AUTH_DISABLED": "1",
                "IMPORT_REVIEW_QUARANTINE_DIR": str(self.quarantine),
            }),
            mock.patch.object(app_module, "MUSIC_ROOT", self.music),
            mock.patch.object(app_module, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(app_module, "_MUSIC_LIBRARY_ROOT", str(self.music)),
            mock.patch.object(app_module, "_DOWNLOADS_ROOTS", [
                str(self.downloads),
                str(self.other_downloads),
                str(self.tmp_downloads),
            ]),
            mock.patch.object(app_module, "_AI_PENDING_FILE", self.pending_file),
            mock.patch.object(app_module, "_record_ai_review_decision", return_value=None),
            mock.patch.object(app_module.beets_client, "plan_import_review_cleanup", side_effect=mock_plan),
            mock.patch.object(app_module.beets_client, "apply_import_review_cleanup", side_effect=mock_apply),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_audio(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-real-audio-but-extension-is-enough")
        return path

    def _write_pending(self, folder: Path) -> str:
        review_id = f"pending:{folder}"
        self.pending_file.write_text(
            json.dumps([
                {
                    "path": str(folder),
                    "folder_name": folder.name,
                    "review_item_id": review_id,
                    "suggestion": {},
                    "added_at": 1,
                }
            ]),
            encoding="utf-8",
        )
        return review_id

    def _post_cleanup(self, folder: Path, files, *, action="quarantine_rejected", allow_delete=False):
        return self.client.post(
            "/api/import/review-files/cleanup",
            json={
                "path": str(folder),
                "review_item_id": f"pending:{folder}",
                "action": action,
                "files": files,
                "allow_delete": allow_delete,
            },
        )

    def _post_delete_folder(self, folder: Path):
        return self.client.post("/api/import/review-folder/delete", json={"path": str(folder)})

    def _make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_quarantine_preserves_literal_percent_filenames(self):
        folder = self.downloads / "literal-percent-review"
        for name in AUDIO_NAMES_WITH_LITERAL_PERCENT:
            self._write_audio(folder / name)
        self._write_pending(folder)

        response = self._post_cleanup(folder, AUDIO_NAMES_WITH_LITERAL_PERCENT)
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["quarantined_count"], len(AUDIO_NAMES_WITH_LITERAL_PERCENT))
        self.assertEqual(data["deleted_count"], 0)
        quarantined_names = {Path(item["quarantined"]).name for item in data["quarantined"]}
        self.assertEqual(quarantined_names, set(AUDIO_NAMES_WITH_LITERAL_PERCENT))
        for item in data["quarantined"]:
            quarantined = Path(item["quarantined"])
            self.assertTrue(quarantined.exists())
            self.assertTrue(app_module._path_is_under(quarantined, self.quarantine.resolve()))
        for name in AUDIO_NAMES_WITH_LITERAL_PERCENT:
            self.assertFalse((folder / name).exists())

    def test_delete_rejected_removes_only_file_under_review_folder(self):
        folder = self.downloads / "delete-one-review"
        track = self._write_audio(folder / "track.flac")
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        self._write_pending(folder)

        response = self._post_cleanup(
            folder,
            ["track.flac"],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_count"], 1)
        self.assertFalse(track.exists())
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_bytes(), b"not-real-audio-but-extension-is-enough")

    def test_cleanup_rejects_stored_outside_root_without_touching_sentinel(self):
        folder = self.outside / "hostile-stored-review"
        sentinel = self._write_audio(folder / "sentinel.flac")
        self._write_pending(folder)

        response = self._post_cleanup(
            folder,
            ["sentinel.flac"],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertIn("outside", data["error"])
        self.assertTrue(sentinel.exists())
        self.assertNotIn(str(sentinel), data["error"])

    def test_cleanup_rejects_music_library_folder_without_deleting_file(self):
        folder = self.music / "Artist" / "Album"
        track = self._write_audio(folder / "track.flac")
        self._write_pending(folder)

        response = self._post_cleanup(
            folder,
            ["track.flac"],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertIn("music library", data["error"])
        self.assertTrue(track.exists())
        self.assertNotIn(str(track), data["error"])

    def test_cleanup_refuses_approved_download_root_itself(self):
        root_track = self._write_audio(self.downloads / "root-track.flac")
        self._write_pending(self.downloads)

        response = self._post_cleanup(
            self.downloads,
            ["root-track.flac"],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertIn("approved root", data["error"])
        self.assertTrue(root_track.exists())
        self.assertTrue(self.downloads.exists())

    def test_cleanup_skips_traversal_encoded_and_absolute_outside_files(self):
        folder = self.downloads / "adversarial-review"
        self._write_audio(folder / "track.flac")
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        encoded_absolute = urllib.parse.quote(str(sentinel), safe="")
        self._write_pending(folder)

        response = self._post_cleanup(
            folder,
            [
                "../outside/sentinel.flac",
                "%2e%2e%2foutside%2fsentinel.flac",
                "%252e%252e%252foutside%252fsentinel.flac",
                "..\\outside\\sentinel.flac",
                "%5c%5coutside%5csentinel.flac",
                "%00sentinel.flac",
                encoded_absolute,
                str(sentinel),
                "./track.flac",
            ],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(data["skipped_count"], 9)
        self.assertTrue(sentinel.exists())
        self.assertTrue((folder / "track.flac").exists())
        self.assertNotIn(str(sentinel), data.get("error", ""))

    def test_cleanup_rejects_symlink_file_escape(self):
        folder = self.downloads / "symlink-file-review"
        folder.mkdir(parents=True)
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        self._make_symlink(folder / "escape.flac", sentinel)
        self._write_pending(folder)

        response = self._post_cleanup(
            folder,
            ["escape.flac"],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertTrue(sentinel.exists())

    def test_cleanup_rejects_symlink_directory_escape(self):
        target = self.outside / "escape-dir"
        target.mkdir()
        sentinel = self._write_audio(target / "sentinel.flac")
        link = self.downloads / "symlink-dir-review"
        self._make_symlink(link, target, directory=True)
        self._write_pending(link)

        response = self._post_cleanup(
            link,
            ["sentinel.flac"],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertTrue(sentinel.exists())
        self.assertNotIn(str(sentinel), data["error"])

    def test_quarantine_rejects_symlinked_destination_parent(self):
        folder = self.downloads / "quarantine-parent-review"
        source = self._write_audio(folder / "track.flac")
        escaped_target = self.outside / "quarantine-escape"
        escaped_target.mkdir()
        date_parent = self.quarantine / time.strftime("%Y%m%d")
        self._make_symlink(date_parent, escaped_target, directory=True)
        self._write_pending(folder)

        response = self._post_cleanup(folder, ["track.flac"])
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["quarantined_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertTrue(source.exists())
        self.assertEqual(list(escaped_target.iterdir()), [])

    def test_quarantine_revalidates_destination_parent_after_creation(self):
        folder = self.downloads / "quarantine-race-review"
        source = self._write_audio(folder / "track.flac")
        escaped_target = self.outside / "quarantine-race-escape"
        date_parent = self.quarantine / time.strftime("%Y%m%d")
        self._write_pending(folder)
        original_mkdir = Path.mkdir
        swapped = False

        def swapping_mkdir(path_self, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                try:
                    path_self.relative_to(date_parent)
                except ValueError:
                    return original_mkdir(path_self, *args, **kwargs)
                original_mkdir(escaped_target, parents=True, exist_ok=True)
                try:
                    date_parent.symlink_to(escaped_target, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
                swapped = True
            return original_mkdir(path_self, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", swapping_mkdir):
            response = self._post_cleanup(folder, ["track.flac"])
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["quarantined_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertTrue(source.exists())
        self.assertEqual(list(escaped_target.rglob("*.flac")), [])

    def test_delete_review_folder_requires_pending_review_for_download_folder(self):
        folder = self.downloads / "not-pending"
        track = self._write_audio(folder / "track.flac")

        response = self._post_delete_folder(folder)
        data = response.get_json()

        self.assertEqual(response.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertIn("Pending Review", data["error"])
        self.assertTrue(folder.exists())
        self.assertTrue(track.exists())

    def test_delete_review_folder_allows_pending_download_folder(self):
        folder = self.downloads / "pending-delete"
        track = self._write_audio(folder / "track.flac")
        self._write_pending(folder)

        response = self._post_delete_folder(folder)
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["files_removed"], 1)
        self.assertFalse(folder.exists())
        self.assertFalse(track.exists())

    def test_folder_stats_rejects_outside_root_without_scanning(self):
        sentinel = self._write_audio(self.outside / "sentinel.flac")

        response = self.client.get("/api/import/folder-stats", query_string={"path": str(self.outside)})
        data = response.get_json()

        self.assertEqual(response.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertIn("outside", data["error"])
        self.assertTrue(sentinel.exists())
        self.assertNotIn(str(sentinel), data["error"])

    def test_folder_stats_allows_download_and_music_roots_but_not_root_self(self):
        review_folder = self._write_audio(self.downloads / "stats-review" / "track.flac").parent
        music_folder = self._write_audio(self.music / "Artist" / "Album" / "track.flac").parent

        downloads_response = self.client.get(
            "/api/import/folder-stats",
            query_string={"path": str(review_folder)},
        )
        music_response = self.client.get(
            "/api/import/folder-stats",
            query_string={"path": str(music_folder)},
        )
        root_response = self.client.get(
            "/api/import/folder-stats",
            query_string={"path": str(self.downloads)},
        )

        self.assertEqual(downloads_response.status_code, 200, downloads_response.get_json())
        self.assertEqual(music_response.status_code, 200, music_response.get_json())
        self.assertEqual(downloads_response.get_json()["audio_count"], 1)
        self.assertEqual(music_response.get_json()["audio_count"], 1)
        self.assertEqual(root_response.status_code, 400, root_response.get_json())
        self.assertIn("approved root", root_response.get_json()["error"])

    def test_folder_stats_rejects_encoded_traversal_query_values(self):
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        payloads = [
            "%2e%2e%2f" + urllib.parse.quote(str(self.outside), safe=""),
            "%252e%252e%252f" + urllib.parse.quote(urllib.parse.quote(str(self.outside), safe=""), safe=""),
            str(self.downloads) + "/%2e%2e/outside",
        ]
        for raw in payloads:
            response = self.client.get("/api/import/folder-stats", query_string={"path": raw})
            data = response.get_json()
            self.assertEqual(response.status_code, 400, (raw, data))
            self.assertFalse(data["ok"])
        self.assertTrue(sentinel.exists())

    def test_cleanup_skips_deeply_encoded_traversal_beyond_decode_budget(self):
        # _import_review_path_text_error decodes up to 3 additional layers.
        # Values still percent-encoded beyond that budget are never decoded
        # by any downstream consumer either (Path/pathlib never URL-decodes),
        # so they can only match a literal on-disk filename -- they cannot
        # resurface as a traversal segment later. This proves 4-, 5-, and
        # 6-layer encoded traversal payloads fail closed (are skipped, not
        # silently accepted) rather than resolving through some deeper decode.
        folder = self.downloads / "deep-encoding-review"
        self._write_audio(folder / "track.flac")
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        self._write_pending(folder)

        segment = "../" + "outside/sentinel.flac"
        four = urllib.parse.quote(urllib.parse.quote(urllib.parse.quote(urllib.parse.quote(segment, safe=""), safe=""), safe=""), safe="")
        five = urllib.parse.quote(four, safe="")
        six = urllib.parse.quote(five, safe="")

        response = self._post_cleanup(
            folder,
            [four, five, six],
            action="delete_rejected",
            allow_delete=True,
        )
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(data["skipped_count"], 3)
        self.assertTrue(sentinel.exists())
        self.assertTrue((folder / "track.flac").exists())

    def test_delete_rejects_source_replaced_by_symlink_immediately_before_unlink(self):
        folder = self.downloads / "source-race-review"
        track = self._write_audio(folder / "track.flac")
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        self._write_pending(folder)

        original_unlink = Path.unlink
        swapped = False

        def swapping_unlink(path_self, *args, **kwargs):
            nonlocal swapped
            if not swapped and path_self == track:
                swapped = True
                original_unlink(path_self)
                try:
                    path_self.symlink_to(sentinel)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
            return original_unlink(path_self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", swapping_unlink):
            response = self._post_cleanup(folder, ["track.flac"], action="delete_rejected", allow_delete=True)
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        # Whether the app's final symlink recheck catches the swap, or the
        # unlink() falls through and removes only the symlink entry itself
        # (POSIX unlink never follows a symlink to its target), the outside
        # sentinel file must survive intact either way.
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_bytes(), b"not-real-audio-but-extension-is-enough")

    def test_quarantine_rejects_source_replaced_by_symlink_immediately_before_move(self):
        folder = self.downloads / "quarantine-source-race-review"
        track = self._write_audio(folder / "track.flac")
        sentinel = self._write_audio(self.outside / "sentinel.flac")
        self._write_pending(folder)

        original_rename = os.rename
        swapped = False

        def swapping_rename(src, dst, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(src) == track:
                swapped = True
                Path(src).unlink()
                try:
                    Path(src).symlink_to(sentinel)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
            return original_rename(src, dst, *args, **kwargs)

        with mock.patch.object(app_module.os, "rename", swapping_rename):
            response = self._post_cleanup(folder, ["track.flac"])
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        # rename() moves the symlink itself (not sentinel's content) into the
        # quarantine dir; the app's post-move check must detect that and
        # undo it, reporting the file skipped rather than falsely quarantined.
        self.assertEqual(data["quarantined_count"], 0)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(data["skipped"][0]["reason"], "symlink")
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_bytes(), b"not-real-audio-but-extension-is-enough")
        for item in data.get("quarantined", []):
            quarantined_path = Path(item["quarantined"])
            if quarantined_path.exists():
                self.assertFalse(quarantined_path.is_symlink())
        self.assertFalse(any(p.is_symlink() for p in self.quarantine.rglob("*")))

    def test_quarantine_destination_leaf_replaced_by_symlink_before_move(self):
        folder = self.downloads / "dest-leaf-race-review"
        source = self._write_audio(folder / "track.flac")
        escaped_target = self.outside / "dest-leaf-race-escape"
        escaped_target.mkdir()
        self._write_pending(folder)

        original_rename = os.rename
        swapped = False

        def swapping_rename(src, dst, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                try:
                    Path(dst).symlink_to(escaped_target, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink creation unavailable: {exc}")
            return original_rename(src, dst, *args, **kwargs)

        with mock.patch.object(app_module.os, "rename", swapping_rename):
            response = self._post_cleanup(folder, ["track.flac"])
        data = response.get_json()

        self.assertEqual(response.status_code, 200, data)
        self.assertTrue(data["ok"])
        # os.rename() never auto-descends into an existing directory target
        # the way shutil.move() does. Depending on platform, replacing a
        # directory-symlink leaf via rename() either atomically displaces it
        # (POSIX) or is rejected outright (observed as PermissionError on
        # this Windows host) -- either disposition is acceptable. What must
        # never happen, on any platform, is the file being written through
        # the symlink into escaped_target.
        self.assertEqual(list(escaped_target.iterdir()), [])
        if data["quarantined_count"] == 1:
            self.assertFalse(source.exists())
            quarantined_path = Path(data["quarantined"][0]["quarantined"])
            self.assertTrue(quarantined_path.exists())
            self.assertFalse(quarantined_path.is_symlink())
        else:
            self.assertEqual(data["skipped_count"], 1)
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()