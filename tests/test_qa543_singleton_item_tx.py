"""QA #543: the singleton attach path's item metadata transaction, end to end
against a real Beets library and file: before-state, actor, rollback, and the
attach Undo that must not move a singleton the attach never moved."""
import os
import tempfile
import unittest
from unittest import mock

from beets.library import Item
from mediafile import MediaFile

from backend import composite_workflows as cw
from tests.test_arch004_023_real_restore import RealRestoreBase, _wav

REC = "11111111-2222-3333-4444-555555555555"


class SingletonItemTransactionTests(RealRestoreBase):
    @classmethod
    def setUpClass(cls):
        # library_service loads the app runtime: import it the way _RouteEnv
        # does, with a disposable data dir, so it never touches /config.
        cls._import_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls._import_tmp.cleanup)
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": cls._import_tmp.name}):
            import app  # noqa: F401

    def _add(self, path):
        _wav(path, "Song")
        item = Item.from_path(path)
        self.lib.add(item)
        self.lib._connection().commit()
        self.assertFalse(item.album_id)
        return item

    def test_update_writes_db_and_tag_records_actor_and_rolls_back(self):
        path = os.path.join(self.music, "Non-Album", "Artist", "Song.wav")
        item = self._add(path)
        res = cw.update_item_metadata(item.id, {"mb_trackid": REC}, write_tags=True,
                                      initiating_user="qa-actor", adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, REC)
        self.assertEqual(MediaFile(path).mb_trackid, REC)
        self.assertEqual(os.fsdecode(self.lib.get_item(item.id).path), path)  # no move

        txs = [t for t in (self.store.get(r["id"]) for r in self.store.list(limit=100)[0])
               if (t.get("metadata") or {}).get("mutation_family") == cw.ITEM_METADATA_FAMILY]
        self.assertEqual(len(txs), 1)
        tx = self.store.get(txs[0]["id"])
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(tx["initiating_user"], "qa-actor")
        self.assertNotIn("initiating_user", tx["metadata"])
        self.assertTrue(tx["metadata"]["engine_result"]["before_state"]["items"])

        rolled = cw.rollback_item_metadata(tx["id"], adapter=self.adapter, store=self.store)
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, "")
        self.assertFalse(MediaFile(path).mb_trackid)

    def test_attach_undo_keeps_an_out_of_format_singleton_in_place(self):
        """The attach audit's Undo (recording_id_restore) used to rewrite tags
        with move=True: a singleton the attach never moved went to the
        singleton path format, and its old path was not recorded."""
        path = os.path.join(self.music, "hand placed", "keep-me-here.wav")
        item = self._add(path)
        attached = cw.update_item_metadata(item.id, {"mb_trackid": REC}, write_tags=True,
                                           adapter=self.adapter, store=self.store)
        self.assertTrue(attached["ok"], attached)
        self.assertEqual(MediaFile(path).mb_trackid, REC)

        from backend import library_service
        log = []
        with mock.patch.object(library_service.lib, "get_item", side_effect=self.lib.get_item), \
                mock.patch.object(cw, "beets_adapter", self.adapter), \
                mock.patch.object(library_service, "_invalidate_lib_cache", return_value=None), \
                mock.patch.object(library_service.composite_workflows, "relocate_album",
                                  side_effect=AssertionError("singleton relocated")):
            ok = library_service._run_item_recording_id_restore(
                item.id, {"mb_trackid": "", "mb_albumid": "", "mb_releasegroupid": ""}, log)
        self.assertTrue(ok, log)
        self.assertEqual(os.fsdecode(self.lib.get_item(item.id).path), path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, "")
        self.assertFalse(MediaFile(path).mb_trackid)


if __name__ == "__main__":
    unittest.main()
