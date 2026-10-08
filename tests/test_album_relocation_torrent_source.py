"""Move to library of a preserved torrent source (#332, plugin 1.15.0).

Real BeetsAdapter -> real webmanager plugin -> real Beets library and files.
A track in a seeding folder (TORRENT_SOURCE_ROOTS, not app-created) is hard
linked into the library, or copied when a hard link is impossible; the
original keeps its path, bytes and inode throughout, including the rollback,
which points the row back and removes only the library file it made.
"""

import os
from pathlib import Path
from unittest import mock

from beets import util

import backend.composite_workflows as cw
import backend.library_service as ls
from tests.test_album_relocation_transaction import _Engine, sha, tree


def ident(path):
    st = os.stat(path)
    return sha(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime


def exdev(*_args, **_kwargs):
    raise util.FilesystemError("Cannot hard link across devices.", "link", ("a", "b"))


class TorrentSourceRelocationTests(_Engine):
    def setUp(self):
        super().setUp()
        for name, value in {"TORRENT_SOURCE_ROOTS": [Path(self.downloads)],
                            "DOWNLOADS_ALLOWED_ROOTS": [Path(self.downloads)],
                            "MUSIC_ROOT": Path(self.music), "TORRENT_SOURCE_MOVE_ALLOWED": False}.items():
            patcher = mock.patch.object(ls, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = os.path.join(self.downloads, "torrentA")
        self.album = self.make_album(self.folder)
        self.old = self.beets_state(self.album.id)
        self.originals = {p: ident(p) for p in [*self.old[0].values(), self.old[1]]}

    def assert_originals_untouched(self):
        self.assertEqual({p: ident(p) for p in self.originals}, self.originals)  # bytes, inode, size, mtime

    def relocate(self):
        res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assert_originals_untouched()
        return res

    def rolled_back(self, op):
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["status"]), (True, "Rolled Back"), rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assert_originals_untouched()
        self.assertEqual(tree(self.music), {})  # only the files this move made were removed
        self.assertFalse(os.path.exists(os.path.join(self.music, "A")))
        return self.store.get(op)

    def test_hard_link_keeps_the_torrent_original_and_rolls_back(self):
        res = self.relocate()
        self.assertTrue(res["ok"], res)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(set(tx["metadata"]["before"]["operations"].values()), {"link"})
        self.assertEqual(tx["metadata"]["before"]["art_operation"], "link")
        engine = tx["metadata"]["engine_result"]
        self.assertEqual(set(engine["methods"].values()), {"linked"})
        self.assertEqual(engine["art_method"], "linked")
        now, art = self.beets_state(self.album.id)
        for iid, path in now.items():
            self.assertTrue(path.startswith(self.music + os.sep), path)
            original = self.old[0][iid]
            self.assertTrue(os.path.samefile(path, original))  # one inode, two names
            self.assertEqual(os.stat(original).st_nlink, 2)
        self.assertTrue(os.path.samefile(art, self.old[1]))
        self.assertIn("hard linked", " ".join(map(str, tx["logs"])))
        tx = self.rolled_back(res["operation_id"])
        self.assertTrue(all(os.stat(p).st_nlink == 1 for p in self.originals))
        result = tx["metadata"]["rollback_result"]
        self.assertEqual((result["repointed_items"], result["removed_library_files"]), (2, 3))

    def test_impossible_hard_link_falls_back_to_a_copy(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            res = self.relocate()
        self.assertTrue(res["ok"], res)
        engine = self.store.get(res["operation_id"])["metadata"]["engine_result"]
        self.assertEqual(set(engine["methods"].values()), {"copied"})
        self.assertEqual(engine["art_method"], "copied")
        now, _art = self.beets_state(self.album.id)
        for iid, path in now.items():
            self.assertFalse(os.path.samefile(path, self.old[0][iid]))
            self.assertEqual(sha(path), sha(self.old[0][iid]))
        self.rolled_back(res["operation_id"])

    def test_a_mixed_album_links_only_its_preserved_tracks(self):
        first = min(self.old[0])
        with mock.patch.object(cw, "_preserved_torrent_file", side_effect=lambda p: p == self.old[0][first]):
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        engine = self.store.get(res["operation_id"])["metadata"]["engine_result"]
        self.assertEqual(engine["methods"], {**{str(k): "moved" for k in self.old[0]}, str(first): "linked"})
        self.assertEqual(ident(self.old[0][first]), self.originals[self.old[0][first]])
        self.assertFalse(any(os.path.exists(p) for k, p in self.old[0].items() if k != first))
        rb = cw.rollback_album_relocation(res["operation_id"], adapter=self.ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assert_originals_untouched()
        self.assertEqual(tree(self.music), {})

    def test_changed_library_copy_is_refused_and_nothing_changes(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            op = self.relocate()["operation_id"]
        path = next(iter(self.beets_state(self.album.id)[0].values()))
        with open(path, "ab") as f:
            f.write(b"edited")
        before = self.snapshot(self.album.id)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["code"], rb["mutated"]), (False, "library_file_changed", False), rb)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assert_originals_untouched()

    def test_replaced_library_link_is_refused(self):
        op = self.relocate()["operation_id"]
        path = next(iter(self.beets_state(self.album.id)[0].values()))
        os.unlink(path)
        with open(path, "wb") as f:
            f.write(b"someone else's file")
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual(rb["code"], "library_file_changed", rb)
        self.assert_originals_untouched()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"someone else's file")

    def test_changed_original_is_refused_and_nothing_is_removed(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            op = self.relocate()["operation_id"]
        original = next(iter(self.old[0].values()))
        with open(original, "ab") as f:
            f.write(b"re-checked by the torrent client")
        before = self.snapshot(self.album.id)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["code"], rb["mutated"]), ("source_changed", False), rb)
        self.assertEqual(self.snapshot(self.album.id), before)

    def test_library_file_already_gone_points_the_row_back(self):
        op = self.relocate()["operation_id"]
        os.unlink(next(iter(self.beets_state(self.album.id)[0].values())))
        self.rolled_back(op)

    def test_interrupted_rollback_resumes_and_removes_only_its_library_files(self):
        op = self.relocate()["operation_id"]
        made = sorted(self.beets_state(self.album.id)[0].values())
        for item in self.lib.get_album(self.album.id).items():  # a crash after the rows were pointed back
            item.path = os.fsencode(self.old[0][item.id])
            item.store()
        self.assertTrue(all(os.path.exists(p) for p in made))
        self.rolled_back(op)

    def test_failure_part_way_unlinks_what_it_linked_and_changes_nothing(self):
        real = util.hardlink
        calls = []

        def second_fails(src, dest, *a, **k):
            calls.append(dest)
            if len(calls) == 2:
                raise RuntimeError("disk gone")
            return real(src, dest, *a, **k)

        before = self.snapshot(self.album.id)
        with mock.patch.object(util, "hardlink", side_effect=second_fails):
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assertEqual(res["code"], "undone", res)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assert_originals_untouched()

    def test_older_plugin_is_never_asked_to_move_a_torrent(self):
        with mock.patch.object(self.ad, "get_plugin_status", return_value={"capabilities": ["album_relocation"]}), \
                mock.patch.object(self.ad, "relocate_album") as relocate:
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        relocate.assert_not_called()
        self.assertEqual((res["ok"], res["code"], res["status"]), (False, "plugin_outdated", "Failed"))
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assert_originals_untouched()

    def test_app_created_download_and_library_albums_still_move(self):
        self.assertFalse(cw._preserved_torrent_file(os.path.join(self.downloads, "_beets_missing_import", "x",
                                                                 "t.flac")))
        self.assertFalse(cw._preserved_torrent_file(os.path.join(self.music, "x", "t.flac")))
        self.assertTrue(cw._preserved_torrent_file(os.path.join(self.downloads, "torrentA", "CD1", "t.flac")))
        with mock.patch.object(ls, "TORRENT_SOURCE_MOVE_ALLOWED", True):
            self.assertFalse(cw._preserved_torrent_file(os.path.join(self.downloads, "torrentA", "t.flac")))

    def test_unevaluable_rule_keeps_the_original(self):
        with mock.patch.object(ls, "_preserve_torrent_source_file", side_effect=OSError("boom")):
            self.assertTrue(cw._preserved_torrent_file(os.path.join(self.music, "x", "t.flac")))


if __name__ == "__main__":
    import unittest
    unittest.main()
