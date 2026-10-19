"""QA edge cases for PR #423 (CodeQL py/path-injection #1411-#1416, #1422):
symlinked/trailing-slash cleanup roots, library Needs MB ID deletes, prefix
safety, playlist symlinked manifests, and TransactionStore relative roots."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import test_sec002_app_path_import_review_cleanup as _route_mod
    from _app_family import patch_app_family
except ImportError:  # pragma: no cover
    from tests import test_sec002_app_path_import_review_cleanup as _route_mod
    from tests._app_family import patch_app_family

import app as app_module
import backend.playlist_service as ps
import backend.transaction_engine as te
import routes_import

_Route = _route_mod.ImportReviewCleanupPathBoundaryTests
URL = "/api/import/review-folder/delete"


class ReviewFolderDeleteRootEdgeTests(unittest.TestCase):
    setUp = _Route.setUp
    tearDown = _Route.tearDown
    _write_audio = _Route._write_audio
    _write_pending = _Route._write_pending

    def _set_downloads(self, roots):
        p = patch_app_family(app_module, "_DOWNLOADS_ROOTS", [str(r) for r in roots])
        p.start()
        self.addCleanup(p.stop)

    def test_symlinked_downloads_root_passes_the_new_root_gate(self):
        # Roots are realpath-resolved, so a path reached through a symlinked
        # configured root passes the #1422 gate. (The pending-store match
        # itself is lexical against resolved roots; that predates PR #423.)
        link = self.root / "dl-link"
        link.symlink_to(self.downloads, target_is_directory=True)
        self._set_downloads([link])
        folder = link / "album"
        self._write_audio(folder / "01.flac")
        with mock.patch.object(routes_import, "_pending_review_matches", return_value=True), \
                mock.patch.object(routes_import, "_delete_review_source_folder", return_value={}) as delete:
            resp = self.client.post(URL, json={"path": str(folder)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        delete.assert_called_once()

    def test_symlinked_downloads_root_real_path_still_deletes(self):
        link = self.root / "dl-link"
        link.symlink_to(self.downloads, target_is_directory=True)
        self._set_downloads([link])
        folder = self.downloads / "album"
        self._write_audio(folder / "01.flac")
        self._write_pending(folder)
        resp = self.client.post(URL, json={"path": str(folder)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse((folder / "01.flac").exists())

    def test_trailing_slash_root_still_deletes(self):
        self._set_downloads([str(self.downloads) + "/"])
        folder = self.downloads / "album"
        self._write_audio(folder / "01.flac")
        self._write_pending(folder)
        resp = self.client.post(URL, json={"path": str(folder)})
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_sibling_prefix_root_is_refused(self):
        sibling = Path(str(self.downloads) + "2") / "album"
        self._write_audio(sibling / "01.flac")
        self._write_pending(sibling)
        with mock.patch.object(routes_import, "_delete_review_source_folder") as delete:
            resp = self.client.post(URL, json={"path": str(sibling)})
        self.assertEqual(resp.status_code, 400)
        delete.assert_not_called()

    def test_needs_mbid_library_row_reaches_delete(self):
        folder = self.music / "Artist" / "Album"
        self._write_audio(folder / "01.flac")
        with mock.patch.object(routes_import, "_library_no_mb_album_matches_folder", return_value=True), \
                mock.patch.object(routes_import, "_delete_review_source_folder", return_value={}) as delete:
            resp = self.client.post(URL, json={"path": str(folder), "album_id": 7})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        delete.assert_called_once()
        self.assertTrue(delete.call_args.kwargs["confirmed_wrong_library_folder"])

    def test_needs_mbid_with_symlinked_music_root_reaches_delete(self):
        link = self.root / "music-link"
        link.symlink_to(self.music, target_is_directory=True)
        for name, val in (("MUSIC_ROOT", link), ("_MUSIC_LIBRARY_ROOT", str(link))):
            p = patch_app_family(app_module, name, val)
            p.start()
            self.addCleanup(p.stop)
        folder = link / "Artist" / "Album"
        self._write_audio(folder / "01.flac")
        with mock.patch.object(routes_import, "_library_no_mb_album_matches_folder", return_value=True), \
                mock.patch.object(routes_import, "_delete_review_source_folder", return_value={}) as delete:
            resp = self.client.post(URL, json={"path": str(folder), "album_id": 7})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        delete.assert_called_once()

    def test_symlink_inside_downloads_to_outside_is_refused(self):
        target = self.outside / "album"
        self._write_audio(target / "01.flac")
        link = self.downloads / "album"
        link.symlink_to(target, target_is_directory=True)
        self._write_pending(link)
        resp = self.client.post(URL, json={"path": str(link)})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue((target / "01.flac").exists())


class PlaylistAtomicReplaceSymlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.state = base / "state"
        self.outside = base / "outside"
        self.state.mkdir()
        self.outside.mkdir()
        for name in ("WEB_MANAGER_DATA_DIR", "PLAYLIST_STATE_ROOT", "PLAYLIST_MANIFESTS_DIR",
                     "PLAYLIST_JOB_STATE_DIR", "PLAYLIST_MEMBERSHIP_DIR"):
            p = mock.patch.object(ps, name, self.state)
            p.start()
            self.addCleanup(p.stop)

    def test_plain_write_and_relative_dotdot_escape(self):
        ps._playlist_atomic_json_replace(self.state / "a.json", {"x": 1})
        self.assertTrue((self.state / "a.json").exists())
        with self.assertRaises(ValueError):
            ps._playlist_atomic_json_replace(self.state / ".." / "outside" / "b.json", {"x": 1})
        self.assertFalse((self.outside / "b.json").exists())

    def test_link_in_root_to_outside_is_refused(self):
        (self.outside / "t.json").write_text("{}")
        (self.state / "m.json").symlink_to(self.outside / "t.json")
        with self.assertRaises(ValueError):
            ps._playlist_atomic_json_replace(self.state / "m.json", {"x": 1})
        self.assertEqual((self.outside / "t.json").read_text(), "{}")
        self.assertEqual([p.name for p in self.outside.iterdir()], ["t.json"])

    def test_link_in_root_to_inside_target_replaces_leaf_not_target(self):
        (self.state / "t.json").write_text("{}")
        (self.state / "m.json").symlink_to(self.state / "t.json")
        ps._playlist_atomic_json_replace(self.state / "m.json", {"x": 1})
        self.assertEqual((self.state / "t.json").read_text(), "{}")
        self.assertIn('"x": 1', (self.state / "m.json").read_text())
        self.assertFalse((self.state / "m.json").is_symlink())
        self.assertFalse(list(self.state.glob("*.tmp")))


    def test_planted_tmp_symlink_cannot_redirect_write_outside_roots(self):
        import uuid
        sentinel = self.outside / "sentinel"
        sentinel.write_text("KEEP")
        (self.state / "m.k.1000.00000000.tmp").symlink_to(sentinel)
        with mock.patch.object(ps.time, "time", return_value=1.0),                 mock.patch.object(ps.uuid, "uuid4", return_value=uuid.UUID(int=0)):
            with self.assertRaises(OSError):
                ps._playlist_atomic_json_replace(self.state / "m.json", {"x": 1}, save_key="k")
        self.assertEqual(sentinel.read_text(), "KEEP")
        self.assertFalse((self.state / "m.json").exists())


class TransactionStoreRootFormsTests(unittest.TestCase):
    def test_root_with_symlink_then_dotdot_still_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            (base / "real" / "sub").mkdir(parents=True)
            (base / "link").symlink_to(base / "real" / "sub", target_is_directory=True)
            store = te.TransactionStore(str(base / "link" / ".." / "state"))
            tx = store.create(operation_type="Rename", initiating_user="qa")
            self.assertIn(f"{tx['id']}.json", os.listdir(base / "real" / "state"))
            self.assertEqual(store.get(tx["id"])["id"], tx["id"])

    def test_relative_root_roundtrip_across_cwd_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                store = te.TransactionStore("rel/tx")
                tx = store.create(operation_type="Rename", initiating_user="qa")
                self.assertEqual(store._path(tx["id"]).parent, Path(tmp).resolve() / "rel" / "tx")
                self.assertEqual(store.get(tx["id"])["id"], tx["id"])
            finally:
                os.chdir(old)

    def test_symlinked_root_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real, target_is_directory=True)
            store = te.TransactionStore(str(link))
            tx = store.create(operation_type="Rename", initiating_user="qa")
            self.assertIn(f"{tx['id']}.json", os.listdir(real))
            self.assertEqual(te.TransactionStore(str(real)).get(tx["id"])["id"], tx["id"])


del _Route

if __name__ == "__main__":
    unittest.main()
