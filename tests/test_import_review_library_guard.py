"""Security F2 (PR #235 review): Import Review cleanup must refuse a target
that CONTAINS the music library, and an irreversible delete of a file inside
the library without the library-delete gate -- at plan time and again at
apply time. Real temp folders; no sink is mocked."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
from backend.transaction_engine import (
    TransactionStore, execute_import_review_cleanup_apply, execute_import_review_cleanup_plan)


class ImportReviewLibraryGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name).resolve() / "data"
        self.music = self.data / "media" / "music"
        self.track = self.music / "Artist" / "Album" / "01.flac"
        self.track.parent.mkdir(parents=True)
        self.track.write_bytes(b"audio")
        self.store = TransactionStore(root=str(Path(tmp.name) / "tx"))
        env = mock.patch.dict(os.environ, {
            "DOWNLOADS_ROOT": str(self.data), "MUSIC_ROOT": str(self.music),
            "WEB_MANAGER_DATA_DIR": str(Path(tmp.name) / "wm"),
            "IMPORT_REVIEW_QUARANTINE_DIR": str(Path(tmp.name) / "q")})
        env.start()
        self.addCleanup(env.stop)

    def test_probe_target_containing_library_is_refused(self):
        res = cw.plan_import_review_cleanup({"path": str(self.data / "media"), "action": "delete"},
                                            store=self.store)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("code"), "import_review_target_contains_library")
        self.assertEqual(self.store.list()[1], 0)
        self.assertTrue(self.track.exists())

    def test_library_ancestor_refused_even_with_gate(self):
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.data / "media"), "action": "delete", "allow_library_delete": True},
            [str(self.music)], music_root=str(self.music))
        self.assertEqual(res.get("code"), "import_review_target_contains_library")

    def test_delete_inside_library_needs_gate_at_plan(self):
        # Library is listed as an allowed root, but the delete gate is absent.
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete"},
            [str(self.music)], music_root=str(self.music))
        self.assertFalse(res.get("ok"), res)
        self.assertTrue(self.track.exists())

    def test_allowed_root_overlapping_library_is_refused(self):
        """#235 helpers at the engine: a root that contains the library is
        refused even for a target outside the library."""
        other = self.data / "dl" / "x"
        other.mkdir(parents=True)
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(other), "action": "delete"}, [str(self.data)], music_root=str(self.music))
        self.assertEqual(res.get("code"), "import_review_unsafe_root", res)

    def test_apply_rechecks_against_current_music_root(self):
        """Plan made while MUSIC_ROOT pointed elsewhere; at apply the album
        folder is inside the configured library: refused, nothing deleted."""
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete"},
            [str(self.music)], music_root=str(self.data / "elsewhere"))
        self.assertTrue(res.get("ok"), res)
        op = res["operation_id"]
        self.store.update(op, status="Approved", metadata={"music_root": str(self.data / "elsewhere")})
        out = execute_import_review_cleanup_apply(self.store, op, quarantine_root=str(self.data / "q"))
        self.assertEqual(out.get("code"), "import_review_library_delete_refused", out)
        self.assertIs(out.get("mutated"), False)
        self.assertTrue(self.track.exists())

    def test_gated_library_delete_still_plans(self):
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete", "allow_library_delete": True},
            [str(self.music)], music_root=str(self.music))
        self.assertTrue(res.get("ok"), res)


class ReviewFolderDeleteAlbumGateTests(unittest.TestCase):
    """F-243-3: an album_id only opens the library-delete gate when that
    album actually matches the folder."""

    def _call(self, matches):
        import app as app_module  # noqa: F401  (registers routes)
        import routes_import
        with mock.patch.object(routes_import, "_library_no_mb_album_matches_folder", return_value=matches), \
             mock.patch.object(routes_import, "_AI_PENDING_FILE",
                               Path(tempfile.gettempdir()) / "no-such-pending.json"), \
             mock.patch.object(routes_import, "_delete_review_source_folder", return_value={}) as delete:
            with routes_import.app.test_request_context(
                    "/api/import/review-folder/delete", method="POST",
                    json={"path": "/music/Artist/Album", "album_id": 5}):
                routes_import.delete_import_review_folder()
        return delete.call_args.kwargs

    def test_unmatched_album_id_is_not_a_gate(self):
        kw = self._call(False)
        self.assertEqual((kw["album_id"], kw["confirmed_wrong_library_folder"]), (0, False))

    def test_matched_album_id_opens_the_gate(self):
        kw = self._call(True)
        self.assertEqual((kw["album_id"], kw["confirmed_wrong_library_folder"]), (5, True))


def _can_symlink(base):
    try:
        os.symlink(str(base), str(base) + ".probe-link", target_is_directory=True)
        os.unlink(str(base) + ".probe-link")
        return True
    except (OSError, NotImplementedError):
        return False


class ImportReviewLibraryGuardSymlinkTests(ImportReviewLibraryGuardTests):
    """Same probe with a symlinked MUSIC_ROOT, then a symlinked DOWNLOADS_ROOT."""

    def setUp(self):
        super().setUp()
        if not _can_symlink(self.data):
            self.skipTest("symlinks unavailable")

    def _probe(self):
        res = cw.plan_import_review_cleanup({"path": str(self.data / "media"), "action": "delete"}, store=self.store)
        self.assertEqual(res.get("code"), "import_review_target_contains_library", res)
        self.assertTrue(self.track.exists())

    def test_symlinked_music_root(self):
        link = self.data.parent / "musiclink"
        os.symlink(str(self.music), str(link), target_is_directory=True)
        with mock.patch.dict(os.environ, {"MUSIC_ROOT": str(link)}):
            self._probe()
            # Apply-time: plan recorded elsewhere; the current MUSIC_ROOT is the link.
            res = execute_import_review_cleanup_plan(
                self.store, {"path": str(self.track.parent), "action": "delete"},
                [str(self.music)], music_root=str(self.data / "elsewhere"))
            self.assertTrue(res.get("ok"), res)
            op = res["operation_id"]
            self.store.update(op, status="Approved")
            out = execute_import_review_cleanup_apply(self.store, op, quarantine_root=str(self.data / "q"))
            self.assertEqual(out.get("code"), "import_review_library_delete_refused", out)
            self.assertTrue(self.track.exists())

    def test_symlinked_downloads_root(self):
        link = self.data.parent / "dllink"
        os.symlink(str(self.data), str(link), target_is_directory=True)
        with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": str(link)}):
            res = cw.plan_import_review_cleanup({"path": str(link / "media"), "action": "delete"}, store=self.store)
            self.assertFalse(res.get("ok"), res)
            self.assertTrue(self.track.exists())
            self._probe()


if __name__ == "__main__":
    unittest.main()
