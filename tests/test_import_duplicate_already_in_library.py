"""B1: a folder Beets skips as ``duplicate`` is "already in library" (no
Review row) only with identity proof: a strong MusicBrainz match whose every
recording is already in a library album of the same Release Group. Any other
``duplicate`` (name collision, deluxe edition, weak match, ...) still goes to
Review. Beets is stubbed at ``reimport_source``; IDs are synthetic UUIDs.
"""
import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RG = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_RG = "aaaaaaaa-0000-4000-8000-000000000002"
REL = "bbbbbbbb-0000-4000-8000-000000000001"
RECS = [f"cccccccc-0000-4000-8000-00000000000{i}" for i in (1, 2)]


def evidence(**over):
    ev = {"choice": "APPLY", "data_source": "MusicBrainz", "distance": 0.01, "release_id": REL,
          "release_group_id": RG, "release_track_count": 2, "task_item_count": 2, "mapped_count": 2,
          "extra_items": 0, "extra_tracks": 0, "recording_ids": list(RECS),
          "duplicates": [{"album_id": 5, "item_id": None, "mb_releasegroupid": RG, "mb_albumid": REL,
                          "item_count": 2, "recording_ids": list(RECS)}]}
    ev.update(over)
    return ev


def dup_lib(**over):
    ev = evidence()
    ev["duplicates"][0].update(over)
    return ev


# name -> evidence; every one of these must go to Review.
NOT_PROVEN = {
    "deluxe_edition_recording_missing": evidence(
        task_item_count=3, mapped_count=3, recording_ids=RECS + ["cccccccc-0000-4000-8000-000000000003"]),
    "name_collision_other_release_group": dup_lib(mb_releasegroupid=OTHER_RG),
    "distance_0_05": evidence(distance=0.05),
    "extra_items": evidence(extra_items=1),
    "extra_tracks": evidence(extra_tracks=1),
    "choice_asis": evidence(choice="ASIS"),
    "not_musicbrainz": evidence(data_source="Discogs"),
    "release_group_empty": evidence(release_group_id=""),
    "release_group_malformed": evidence(release_group_id="not-a-uuid"),
    "library_blank_mb_trackid": dup_lib(recording_ids=[RECS[0], None]),
    "old_plugin_no_evidence": None,
}


class RuleTests(unittest.TestCase):
    def test_rule(self):
        from backend.matching.duplicate_identity import duplicate_is_already_in_library as rule
        self.assertEqual(rule(evidence()), (True, "recordings_present_in_release_group"))
        for name, ev in NOT_PROVEN.items():
            with self.subTest(name):
                self.assertFalse(rule(ev)[0])
        for bad in ({"distance": None}, {"distance": "0.01"}, {"mapped_count": None},
                    {"recording_ids": None}, {"duplicates": None}, {"recording_ids": [RECS[0], RECS[0]]}):
            with self.subTest(bad):
                self.assertFalse(rule(evidence(**bad))[0])


try:
    from tests import test_import_qbittorrent_source as q
except ImportError:  # pragma: no cover
    import test_import_qbittorrent_source as q


def setUpModule():
    q.setUpModule()


from backend.composite_workflows import reimport_source as REAL_REIMPORT  # noqa: E402  (before any patch)


class BatchTests(q._Batch):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(q.ai, "identify_import_folder", return_value={
            "candidates": [], "warnings": [], "auto_import": {"eligible": False, "reasons": ["no_proof"]}})
        p.start()
        self.addCleanup(p.stop)

    def skipped(self, folder, ev, reason="duplicate"):
        row = {"path": str(folder), "reason": reason}
        if reason == "duplicate":
            row["duplicate_evidence"] = copy.deepcopy(ev)
        return row

    def run_local(self, name, rows):
        self.audio(f"{name}/01.flac")
        self.audio(f"{name}/02.flac")
        folder = self.dl / name
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(folder)]
        self.result = {"ok": True, "albums_imported": 0, "imported_album_ids": [],
                       "skipped": rows(folder), "not_matched": [str(folder)], "not_matched_known": True}
        out, folders = self.run_batch(source="local", qbit_inventory=None)
        return folders[str(folder)]

    def test_proven_duplicate_is_already_in_library_without_a_review_row(self):  # 1
        state = self.run_local("Dup", lambda f: [self.skipped(f, evidence())])
        self.assertEqual(state["status"], "skipped", self.log)
        self.assertEqual(state["failure_reason"], "already in library")
        self.assertEqual(state["already_in_library_evidence"],
                         {"reason": "recordings_present_in_release_group", "release_group_id": RG, "album_ids": [5]})
        self.queued.assert_not_called()

    def test_unproven_duplicates_go_to_review_with_the_rule_reason(self):  # 2-10
        from backend.matching.duplicate_identity import duplicate_is_already_in_library as rule
        for name, ev in NOT_PROVEN.items():
            with self.subTest(name):
                self.queued.reset_mock()
                state = self.run_local(f"Dup {name}", lambda f: [self.skipped(f, ev)])
                self.assertEqual(state["status"], "review_created", self.log)
                self.queued.assert_called_once()
                self.assertEqual(self.queued.call_args.kwargs["evidence"]["duplicate_rule"], rule(ev)[1])

    def test_proven_duplicate_with_another_task_not_matched_goes_to_review(self):  # 11
        state = self.run_local("Two", lambda f: [self.skipped(f / "CD1", evidence()),
                                                 self.skipped(f / "CD2", None, reason="no_strong_match")])
        self.assertEqual(state["status"], "review_created", self.log)
        self.assertEqual(self.queued.call_args.kwargs["evidence"]["duplicate_rule"], "not_only_duplicates")

    def test_partial_import_whose_skipped_paths_are_proven_duplicates_is_imported(self):  # 12
        self.audio("Part/CD1/01.flac")
        folder = self.dl / "Part"
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(folder)]
        self.result = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                       "skipped": [self.skipped(folder / "CD2", evidence())],
                       "not_matched": [str(folder / "CD2")], "not_matched_known": True}
        out, folders = self.run_batch(source="local", qbit_inventory=None)
        state = folders[str(folder)]
        self.assertEqual(state["status"], "imported", self.log)
        self.assertIn("already in the library", state["note"])
        self.queued.assert_not_called()

    def test_partial_import_with_an_unproven_duplicate_still_goes_to_review(self):
        self.audio("Part2/CD1/01.flac")
        folder = self.dl / "Part2"
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(folder)]
        self.result = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                       "skipped": [self.skipped(folder / "CD2", evidence(distance=0.2))],
                       "not_matched": [str(folder / "CD2")], "not_matched_known": True}
        out, folders = self.run_batch(source="local", qbit_inventory=None)
        self.assertEqual(folders[str(folder)]["status"], "review_required", self.log)
        self.assertEqual(self.queued.call_args.kwargs["evidence"]["duplicate_rule"], "match_not_strong")

    def test_qbittorrent_duplicate_rerun_is_skipped_with_no_review_row(self):  # 14
        self.torrent("Seeded Album", ["01.flac", "02.flac"])
        folder = self.dl / "Seeded Album"
        self.result = {"ok": True, "albums_imported": 0, "imported_album_ids": [],
                       "skipped": [self.skipped(folder, evidence())],
                       "not_matched": [str(folder)], "not_matched_known": True}
        out, folders = self.run_batch()
        state = folders[str(folder)]
        self.assertEqual(state["status"], "skipped", self.log)
        self.assertEqual(state["failure_reason"], "already in library")
        self.queued.assert_not_called()

    def test_real_reimport_source_carries_plugin_duplicate_evidence_to_b1(self):
        """NEW-3: the real result shaping in ``reimport_source`` (only the
        adapter is faked, with the plugin's own result shape) must keep
        ``duplicate_evidence`` so the B1 rule sees it."""
        from backend import composite_workflows as cw
        fake = mock.Mock()
        fake.get_stats.return_value = {"albums": 1}
        fake.run_import.side_effect = lambda paths, **kw: {
            "success": True, "imported_album_ids": [], "skipped_paths": [paths],
            "skipped": [{"path": paths, "reason": "duplicate", "duplicate_evidence": evidence()}]}
        self.reimport.side_effect = lambda f, opts, timeout: REAL_REIMPORT(f, opts, timeout=timeout)
        with mock.patch.object(cw, "beets_adapter", fake):
            self.audio("Real/01.flac")
            self.audio("Real/02.flac")
            folder = self.dl / "Real"
            self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(folder)]
            out, folders = self.run_batch(source="local", qbit_inventory=None)
        fake.run_import.assert_called_once()
        state = folders[str(folder)]
        self.assertEqual(state["status"], "skipped", self.log)
        self.assertEqual(state["failure_reason"], "already in library")
        self.queued.assert_not_called()


class AlreadyInLibraryGuardTests(q._Roots):  # 13: the folder lookup, un-mocked, against a fake adapter
    def test_folder_inside_the_library_is_already_in_library(self):
        album = self.music / "Artist" / "Album"
        album.mkdir(parents=True)
        fake = mock.Mock()
        fake.get_items.return_value = [{"path": str(album / "01.flac"), "album_id": 5}]
        with mock.patch.object(q.ai.composite_workflows, "beets_adapter", fake), \
                mock.patch.object(q.ai.composite_workflows, "_music_root", return_value=self.music.resolve()):
            self.assertTrue(q.ai._ai_batch_already_in_library(str(album)))
            self.assertFalse(q.ai._ai_batch_already_in_library(str(self.music / "Artist" / "Other")))
            self.assertFalse(q.ai._ai_batch_already_in_library(str(self.dl / "Album")))


if __name__ == "__main__":
    unittest.main()
