"""#546: the item metadata apply fails closed without a before-state, and both
attach write paths compare-and-set against the request-time Recording ID.
Real Beets library, real files, the real webmanager plugin."""
import os
import unittest
from unittest import mock

from beets.library import Item
from mediafile import MediaFile

from backend import composite_workflows as cw
from tests.test_arch004_023_real_restore import RealRestoreBase, _wav

OLD = "aaaaaaaa-1111-2222-3333-444444444444"
NEW = "bbbbbbbb-1111-2222-3333-444444444444"
OTHER = "cccccccc-1111-2222-3333-444444444444"


class ItemMetadataFailClosedTests(RealRestoreBase):
    def _singleton(self, mb_trackid=OLD):
        path = os.path.join(self.music, "Non-Album", "Artist", "Song.wav")
        _wav(path, "Song")
        item = Item.from_path(path)
        item.mb_trackid = mb_trackid
        self.lib.add(item)
        item.write()
        self.lib._connection().commit()
        return item, path

    def _item_txs(self):
        return [t for t in (self.store.get(r["id"]) for r in self.store.list(limit=100)[0])
                if (t.get("metadata") or {}).get("mutation_family") == cw.ITEM_METADATA_FAMILY]

    def _assert_failed_without_write(self, res, code):
        self.assertFalse(res["ok"], res)
        self.assertEqual(res.get("code"), code, res)
        self.assertTrue(res.get("operation_id"), res)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["status"], "Failed")
        self.assertFalse((tx.get("metadata") or {}).get("engine_result"))
        self.assertIn("nothing was changed", "\n".join(tx.get("logs") or []))
        return tx

    def test_missing_item_fails_closed(self):
        # Before #546 this returned ok: True and the transaction Completed.
        res = cw.update_item_metadata(987654, {"title": "x"}, adapter=self.adapter, store=self.store)
        self._assert_failed_without_write(res, "before_state_unavailable")
        with mock.patch.object(self.adapter, "get_item", return_value=None):
            res = cw.update_item_metadata(987654, {"title": "x"}, adapter=self.adapter, store=self.store)
        self._assert_failed_without_write(res, "before_state_unavailable")
        self.assertIn("no longer exists", res["error"])

    def test_lookup_error_fails_closed_and_writes_nothing(self):
        item, path = self._singleton()
        with mock.patch.object(self.adapter, "get_item", side_effect=OSError("db locked")):
            res = cw.update_item_metadata(item.id, {"mb_trackid": NEW}, write_tags=True,
                                          adapter=self.adapter, store=self.store)
        self._assert_failed_without_write(res, "before_state_unavailable")
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, OLD)
        self.assertEqual(MediaFile(path).mb_trackid, OLD)

    def test_expected_current_mismatch_refuses_the_write(self):
        item, path = self._singleton()
        res = cw.update_item_metadata(item.id, {"mb_trackid": NEW}, write_tags=True,
                                      expected_current={"mb_trackid": OTHER},
                                      adapter=self.adapter, store=self.store)
        self._assert_failed_without_write(res, "item_changed_since_request")
        self.assertIn("changed since you requested it", res["error"])
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, OLD)
        self.assertEqual(MediaFile(path).mb_trackid, OLD)

    def test_expected_current_match_writes_and_rolls_back(self):
        item, path = self._singleton()
        res = cw.update_item_metadata(item.id, {"mb_trackid": NEW}, write_tags=True,
                                      expected_current={"mb_trackid": OLD.upper()},
                                      adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual([t["id"] for t in self._item_txs()], [res["operation_id"]])
        self.assertEqual(MediaFile(path).mb_trackid, NEW)
        rolled = cw.rollback_item_metadata(res["operation_id"], adapter=self.adapter, store=self.store)
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, OLD)
        self.assertEqual(MediaFile(path).mb_trackid, OLD)


class AlbumTrackRepairCompareAndSetTests(RealRestoreBase):
    def _album(self):
        album, paths = self._add_wav_album(count=1)
        item = next(iter(album.items()))
        item.mb_trackid = OLD
        item.store()
        self.lib._connection().commit()
        return album, item

    def test_plan_refuses_when_the_recording_id_changed_since_the_request(self):
        album, item = self._album()
        before = len(self.store.list(limit=100)[0])
        res = cw.plan_album_mb_track_repair(
            {"album_id": album.id, "track_mbids": {str(item.id): NEW},
             "expected_track_ids": {str(item.id): OTHER}},
            adapter=self.adapter, store=self.store)
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["code"], "item_changed_since_request")
        self.assertIn("changed since you requested it", res["error"])
        self.assertEqual(len(self.store.list(limit=100)[0]), before)
        self.assertEqual(self.lib.get_item(item.id).mb_trackid, OLD)

    def test_plan_accepts_the_unchanged_recording_id(self):
        album, item = self._album()
        res = cw.plan_album_mb_track_repair(
            {"album_id": album.id, "track_mbids": {str(item.id): NEW},
             "expected_track_ids": {str(item.id): OLD}},
            adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)


if __name__ == "__main__":
    unittest.main()
