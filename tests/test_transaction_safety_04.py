"""Library-mutation safety for 0.4 (#335, #327 and the failure-injection
matrix). Real BeetsAdapter -> real webmanager plugin -> real Beets library
and files (Flask test client in place of HTTP), on synthetic fixtures."""

import os
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.library_service as ls
from tests.test_album_relocation_torrent_source import ident
from tests.test_album_relocation_transaction import _Engine, tree


class _TorrentEnv(_Engine):
    """An album whose rows point into a preserved torrent folder, as after a
    linked relocation's rollback (#332)."""

    def setUp(self):
        super().setUp()
        for name, value in {"TORRENT_SOURCE_ROOTS": [Path(self.downloads)],
                            "DOWNLOADS_ALLOWED_ROOTS": [Path(self.downloads)],
                            "MUSIC_ROOT": Path(self.music), "TORRENT_SOURCE_MOVE_ALLOWED": False}.items():
            patcher = mock.patch.object(ls, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.album = self.make_album(os.path.join(self.downloads, "torrentA"))
        self.paths = self.beets_state(self.album.id)[0]
        self.originals = {p: ident(p) for p in self.paths.values()}

    def assert_torrent_untouched(self):
        self.assertEqual({p: ident(p) for p in self.originals}, self.originals)  # bytes, inode, size, mtime
        self.assertEqual(self.beets_state(self.album.id)[0], self.paths)
        self.assertEqual(tree(self.music), {})


class TorrentSourceModifyTests(_TorrentEnv):
    """#335: no Web Manager modify path moves or tag-writes a torrent source."""

    def test_artist_folder_reconcile_never_moves_or_writes_a_torrent_album(self):
        plan = cw.plan_artist_folder_reconcile({"album_ids": [self.album.id], "canonical_name": "Canon"},
                                               adapter=self.ad, store=self.store)
        res = cw.apply_artist_folder_reconcile(plan["operation_id"], adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["preserved_album_ids"], [self.album.id])
        self.assertEqual(self.lib.get_album(self.album.id).albumartist, "Canon")  # stored in Beets
        self.assert_torrent_untouched()
        self.assertIn("preserved torrent source", " ".join(map(str, self.store.get(plan["operation_id"])["logs"])))

    def test_rollback_restore_never_moves_or_writes_a_torrent_album(self):
        before = {"album": {"id": self.album.id, "fields": {"albumartist": "Old", "album": "Older"}},
                  "items": [{"id": iid, "fields": {"title": "Was"}} for iid in self.paths]}
        self.assertEqual(cw._restore_rows(self.ad, before, write=True), (1 + len(self.paths), 0))
        self.assertEqual(self.lib.get_album(self.album.id).albumartist, "Old")
        self.assertEqual({it.title for it in self.lib.get_album(self.album.id).items()}, {"Was"})
        self.assert_torrent_untouched()

    def test_metadata_edit_with_forced_write_never_touches_a_torrent_album(self):
        res = cw.update_album_metadata(self.album.id, {"albumartist": "Edited"}, force_write_tags=True,
                                       adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.lib.get_album(self.album.id).albumartist, "Edited")
        self.assert_torrent_untouched()

    def test_unevaluable_rule_keeps_the_files_in_place(self):
        with mock.patch.object(ls, "_preserve_torrent_source_file", side_effect=OSError("boom")):
            res = cw._modify(self.ad, {"albumartist": "X"}, album_ids=[self.album.id], write=True, move=True)
        self.assertEqual(res["preserved_album_ids"], [self.album.id])
        self.assert_torrent_untouched()

    def test_library_album_still_moves_and_writes(self):
        album = self.make_album(os.path.join(self.music, "A", "B"))
        res = cw._modify(self.ad, {"albumartist": "Moved"}, album_ids=[album.id], write=True, move=True)
        self.assertFalse(res.get("preserved_album_ids"))
        self.assertTrue(all(os.sep + "Moved" + os.sep in p for p in self.beets_state(album.id)[0].values()))


class PluginModifyContractTests(_Engine):
    def post(self, body):
        return self.client.post("/webmanager/modify", json=body,
                                headers={"Authorization": f"Bearer {self.token}"})

    def test_modify_never_moves_unless_asked(self):
        """#335: /webmanager/modify defaults to move=False."""
        album = self.make_album(os.path.join(self.music, "A", "B"))
        before = self.beets_state(album.id)
        r = self.post({"album_ids": [album.id], "fields": {"albumartist": "Other"}, "write": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.beets_state(album.id), before)
        self.assertEqual(self.lib.get_album(album.id).albumartist, "Other")

    def test_artpath_is_validated(self):
        """#327: artpath only names an existing image inside the allowed roots,
        with no symlink; clearing it is allowed."""
        album = self.make_album(os.path.join(self.music, "A", "B"))
        cover = os.fsdecode(album.artpath)
        outside = os.path.join(self.td, "outside.jpg")
        with open(outside, "wb") as f:
            f.write(b"\xff\xd8\xff")
        not_image = os.path.join(self.music, "A", "B", "notes.txt")
        with open(not_image, "w") as f:
            f.write("x")
        bad = [outside, not_image, os.path.join(self.music, "A", "B", "missing.jpg"),
               os.path.join(self.music, "A", "..", "A", "B", "cover.jpg"), "relative/cover.jpg", 7]
        link = os.path.join(self.music, "A", "link.jpg")
        try:
            os.symlink(outside, link)
            bad.append(link)
        except (OSError, NotImplementedError):
            pass
        for value in bad:
            r = self.post({"album_ids": [album.id], "fields": {"artpath": value}, "write": False})
            self.assertEqual((r.status_code, r.get_json()["error_code"]), (400, "ARTPATH_INVALID"), value)
            self.assertEqual(os.fsdecode(self.lib.get_album(album.id).artpath), cover)
        self.assertEqual(self.post({"album_ids": [album.id], "fields": {"artpath": ""}, "write": False}).status_code, 200)
        self.assertFalse(self.lib.get_album(album.id).artpath)
        r = self.post({"album_ids": [album.id], "fields": {"artpath": cover}, "write": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(os.fsdecode(self.lib.get_album(album.id).artpath), cover)


if __name__ == "__main__":
    import unittest
    unittest.main()
