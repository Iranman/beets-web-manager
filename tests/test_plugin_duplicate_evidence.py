"""webmanager plugin 1.20.0 (capability import_duplicate_evidence): when Beets'
name-only duplicate check flags an import task, the plugin records identity
evidence from the in-process task and returns it on the ``skipped`` row.

Real Beets objects over a temporary Library; no mocks of the evidence path.
"""
import os
import shutil
import tempfile
import unittest

from beets.autotag.distance import Distance
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.autotag.match import AlbumMatch
from beets.importer import Action, ImportTask
from beets.library import Item, Library

import beetsplug.webmanager.operations as ops_mod

RG = "22222222-2222-4222-8222-222222222222"
REL = "a6a6e718-3d8e-4c3e-9eaa-7b54af639bb9"
REC = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]


class DuplicateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=self.td)
        old = [Item(title=f"T{i}", artist="A", album="B", mb_trackid=REC[i], path=f"/x/{i}.flac".encode())
               for i in range(2)]
        self.album = self.lib.add_album(old)
        self.album.mb_releasegroupid, self.album.mb_albumid = RG, REL
        self.album.store()

    def tearDown(self):
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _task(self, with_match=True):
        items = [Item(title=f"T{i}", artist="A", album="B", path=f"/d/{i}.flac".encode()) for i in range(3)]
        task = ImportTask(b"/d", [b"/d"], items)
        if not with_match:
            task.set_choice(Action.ASIS)
            return task
        tracks = [TrackInfo(title=f"T{i}", track_id=REC[i], index=i + 1) for i in range(2)]
        info = AlbumInfo(tracks=tracks + [TrackInfo(title="T9", track_id="r9", index=3)],
                         album="B", album_id=REL, artist="A", releasegroup_id=RG, data_source="MusicBrainz")
        task.set_choice(AlbumMatch(Distance(), info, dict(zip(items[:2], tracks)), extra_items=[items[2]],
                                   extra_tracks=[info.tracks[2]]))
        return task

    def test_match_and_found_duplicates_recorded(self):
        ev = ops_mod.duplicate_evidence(self._task(), [self.album])
        self.assertEqual(ev["choice"], "APPLY")
        self.assertEqual((ev["data_source"], ev["distance"]), ("MusicBrainz", 0.0))
        self.assertEqual((ev["release_id"], ev["release_group_id"]), (REL, RG))
        self.assertEqual((ev["release_track_count"], ev["task_item_count"], ev["mapped_count"]), (3, 3, 2))
        self.assertEqual((ev["extra_items"], ev["extra_tracks"]), (1, 1))
        self.assertEqual(ev["recording_ids"], REC)
        self.assertEqual(ev["duplicates"], [{
            "album_id": self.album.id, "item_id": None, "mb_releasegroupid": RG, "mb_albumid": REL,
            "item_count": 2, "recording_ids": REC}])

    def test_no_match_yields_null_fields(self):
        ev = ops_mod.duplicate_evidence(self._task(with_match=False), [self.album])
        self.assertEqual((ev["choice"], ev["task_item_count"]), ("ASIS", 3))
        for k in ("data_source", "distance", "release_id", "release_group_id", "release_track_count",
                  "mapped_count", "extra_items", "extra_tracks", "recording_ids"):
            self.assertIsNone(ev[k], k)
        self.assertEqual(len(ev["duplicates"]), 1)

    def test_broken_task_never_raises(self):
        ev = ops_mod.duplicate_evidence(object(), [object()])
        self.assertIsNone(ev["choice"])
        self.assertEqual(ev["duplicates"][0]["item_id"], None)

    def test_hook_records_evidence_and_keeps_duplicate_action(self):
        from beets import config as beets_config
        tasks = []
        session = ops_mod._import_session_class(False, tasks)(self.lib, loghandler=None, paths=[], query=None)
        orig = beets_config["import"]["duplicate_action"].get()
        try:
            beets_config["import"]["duplicate_action"] = "skip"
            task = self._task()
            action = session.get_duplicate_action(task, [self.album])
        finally:
            beets_config["import"]["duplicate_action"] = orig
        self.assertEqual(action.name, "SKIP")
        self.assertTrue(task.webmanager_duplicate)
        self.assertEqual(task.webmanager_duplicate_evidence["release_group_id"], RG)
        self.assertEqual(tasks, [task])

    def test_hook_survives_evidence_failure(self):
        from unittest import mock
        tasks = []
        session = ops_mod._import_session_class(False, tasks)(self.lib, loghandler=None, paths=[], query=None)
        task = self._task()
        with mock.patch.object(ops_mod, "duplicate_evidence", side_effect=RuntimeError("boom")):
            session.get_duplicate_action(task, [self.album])
        self.assertIsNone(task.webmanager_duplicate_evidence)

    def test_capability_advertised(self):
        self.assertIn("import_duplicate_evidence", ops_mod.get_capabilities())


if __name__ == "__main__":
    unittest.main()
