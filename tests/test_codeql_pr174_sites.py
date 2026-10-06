"""Regressions for the CodeQL alerts raised on PR #174.

- py/path-injection: plan_import_review_cleanup classifies "inside the music
  library" by normpath containment, so traversal and sibling-prefix paths are
  not treated as in-library (and in-library paths still get forced to
  quarantine).
- py/stack-trace-exposure: error responses/logs returned to the client carry
  no exception text.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.cleanup_service as cs
import backend.composite_workflows as cw
from backend.beets_adapter import BeetsUnavailableError
from backend.transaction_engine import TransactionStore

LEAK = "LEAK_MARKER_/secret/internal/path"


class ImportReviewClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.music = Path(os.path.realpath(self.tmp.name)) / "music"
        self.music.mkdir()
        env = mock.patch.dict(os.environ, {"MUSIC_ROOT": str(self.music)})
        env.start()
        self.addCleanup(env.stop)
        roots = mock.patch.object(cw, "_import_review_allowed_roots", return_value=[])
        roots.start()
        self.addCleanup(roots.stop)
        self.store = TransactionStore(str(Path(self.tmp.name) / "tx"))

    def _plan(self, path, exec_side_effect=None):
        captured = {}

        def fake_exec(st, data, roots, music_root=None):
            if exec_side_effect:
                raise exec_side_effect
            captured.update(data)
            return {"ok": True, "operation_id": "op1"}

        with mock.patch("backend.transaction_engine.execute_import_review_cleanup_plan", side_effect=fake_exec):
            res = cw.plan_import_review_cleanup({"path": path, "action": "delete_rejected"}, store=self.store)
        return res, captured

    def test_inside_library_forced_to_quarantine(self):
        res, data = self._plan(str(self.music / "Artist" / "Album"))
        self.assertEqual(data["action"], "quarantine_rejected")
        self.assertTrue(res["library_paths_quarantined"])

    def test_traversal_out_of_library_not_in_library(self):
        res, data = self._plan(str(self.music) + os.sep + ".." + os.sep + "outside")
        self.assertEqual(data["action"], "delete_rejected")
        self.assertFalse(res["library_paths_quarantined"])

    def test_sibling_prefix_not_in_library(self):
        res, data = self._plan(str(self.music) + "-evil" + os.sep + "x")
        self.assertEqual(data["action"], "delete_rejected")
        self.assertFalse(res["library_paths_quarantined"])

    def test_value_error_returns_generic_message(self):
        res, _ = self._plan(str(self.music / "a"), exec_side_effect=ValueError(LEAK))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "invalid_request")
        self.assertNotIn("LEAK_MARKER", repr(res))

    def test_apply_exception_returns_generic_message(self):
        tx = self.store.create(operation_type="Library Cleanup", status="Approved",
                               metadata={"mutation_family": cw.IMPORT_REVIEW_CLEANUP_FAMILY})
        with mock.patch.object(cw, "_apply_engine_import_review_cleanup", side_effect=RuntimeError(LEAK)), \
                self.assertLogs("beets.workflows", level="ERROR"):
            res = cw.apply_import_review_cleanup(tx["id"], store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "Failed")
        self.assertNotIn("LEAK_MARKER", repr(res))
        self.assertNotIn("LEAK_MARKER", repr(self.store.get(tx["id"])))


class MusicRootUsableTests(unittest.TestCase):
    def test_oserror_reason_has_no_exception_text(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MUSIC_ROOT": d}), \
                mock.patch.object(Path, "iterdir", side_effect=OSError(LEAK)):
            ok, reason = cw._music_root_usable()
        self.assertFalse(ok)
        self.assertIn("OSError", reason)
        self.assertNotIn("LEAK_MARKER", reason)


class NoAudioFolderLogTests(unittest.TestCase):
    def test_scan_unreadable_folder_log_has_no_exception_text(self):
        log = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(Path, "iterdir", side_effect=OSError(LEAK)):
            cs._scan_no_audio_folder_candidates(Path(d), log)
        self.assertTrue(any("cannot read" in line for line in log))
        self.assertNotIn("LEAK_MARKER", "\n".join(log))

    def test_delete_failure_log_has_no_exception_text(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d)) / "staging"
            target = root / "junk"
            target.mkdir(parents=True)
            log = []
            scan = {"folders": [{"path": str(target), "files": 1, "bytes": 1}]}
            with mock.patch.object(cs, "_folder_clean_root", return_value=root), \
                    mock.patch.object(cs, "MUSIC_ROOT", Path(d) / "music"), \
                    mock.patch.object(cs, "_scan_no_audio_folder_candidates", return_value=scan), \
                    mock.patch.object(cs, "_no_audio_tree_still_safe", return_value=None), \
                    mock.patch.object(cw, "_validated_staging_target", return_value=target), \
                    mock.patch.object(cw, "_remove_resolved", side_effect=OSError(LEAK)):
                res = cs._delete_no_audio_folders(str(root), [str(target)], dry_run=False, log=log)
        self.assertNotIn("LEAK_MARKER", repr(res))
        self.assertTrue(any("ERROR deleting" in line for line in log))
        self.assertNotIn("LEAK_MARKER", "\n".join(log))


class RouteStackTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        try:
            from _app_family import patch_app_family
        except ImportError:
            from tests._app_family import patch_app_family
        cls.app_module = app_module
        cls.patch_app_family = staticmethod(patch_app_family)

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = self.app_module.app.test_client()

    def test_album_tracks_remove_preview_error_is_generic(self):
        with self.patch_app_family(self.app_module, "_remove_album_track_items", mock.Mock(side_effect=RuntimeError(LEAK))):
            resp = self.client.post("/api/clean/album-tracks/remove",
                                    json={"album_id": 7, "item_ids": [1], "dry_run": True})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["code"], "preview_failed")
        self.assertNotIn("LEAK_MARKER", resp.get_data(as_text=True))

    def test_album_remove_engine_offline_is_generic(self):
        fake_lib = mock.Mock()
        fake_lib.get_album.return_value = object()
        fake_lib.items.return_value = []
        with self.patch_app_family(self.app_module, "lib", fake_lib), \
                mock.patch.object(cw, "plan_album_cleanup", side_effect=BeetsUnavailableError(LEAK)):
            resp = self.client.post("/api/albums/5/remove", json={})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error_code"], "ENGINE_OFFLINE")
        self.assertNotIn("LEAK_MARKER", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
