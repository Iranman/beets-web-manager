"""A qBittorrent unit's staged copy is kept only while a Review row points at
it, and a re-run batch reuses the unit's one stage instead of copying again.

A unit whose folder holds anything besides the torrent's own files (art, a cue
sheet, a log) is imported from a copy under ``_beets_missing_import``. The
copy is removed through ``import_service._delete_staged_import_folder`` when
the unit ends without a Review row on it (proven duplicate, quality-upgrade
row, source changed); the torrent's own files are never touched.
"""
import inspect
import shutil
import unittest
from pathlib import Path
from unittest import mock

import backend.import_service as imp
import tests.test_import_qbittorrent_source as base
from tests.test_import_qbittorrent_source import RG, REL, _Batch, _fingerprint, setUpModule  # noqa: F401

REC = ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"]


def _duplicate(folder):
    return {"path": str(folder), "reason": "duplicate", "duplicate_evidence": {
        "choice": "APPLY", "data_source": "MusicBrainz", "distance": 0.01,
        "release_id": REL, "release_group_id": RG, "release_track_count": 1,
        "task_item_count": 1, "mapped_count": 1, "extra_items": 0, "extra_tracks": 0,
        "recording_ids": REC,
        "duplicates": [{"album_id": 3, "item_id": None, "mb_releasegroupid": RG, "mb_albumid": REL,
                        "item_count": 1, "recording_ids": REC}]}}


class StagedCopyCleanupTests(_Batch):
    def setUp(self):
        super().setUp()
        self.rows = {}  # Review rows: path -> evidence
        self.skip = None  # Beets' skip record for the unit, None = not matched

        def queue(folder, suggestion, evidence=None, batch_job_id=""):
            self.rows[str(Path(folder).resolve())] = evidence or {}
            return "rid"

        def in_review(folder, *, unknown=False):
            p = Path(folder).resolve()
            return any(Path(r) == p or p in Path(r).parents or Path(r) in p.parents for r in self.rows)

        self.queued.side_effect = queue
        for p in (mock.patch.object(base.ai, "_pending_review_has_path",
                                    side_effect=lambda f, **k: str(Path(f).resolve()) in self.rows),
                  mock.patch.object(imp, "_pending_review_has_path_or_overlap", side_effect=in_review),
                  # Staging-only delete stand-in for the engine's classified delete.
                  mock.patch.object(imp.composite_workflows, "delete_file",
                                    side_effect=lambda p: shutil.rmtree(p) or {"deleted": True})):
            p.start()
            self.addCleanup(p.stop)

    def reimport_result(self, folder):
        return {"ok": True, "albums_imported": 0, "imported_album_ids": [],
                "skipped": [self.skip(folder) if self.skip else {"path": str(folder), "reason": "no_strong_match"}],
                "not_matched": [folder], "not_matched_known": True}

    def run_batch(self, **kw):
        if "stage_cleanup" in inspect.signature(base.ai._run_ai_batch_import).parameters:
            kw.setdefault("stage_cleanup", imp._delete_staged_import_folder)
        return super().run_batch(**kw)

    def stage_root(self):
        return self.dl / "_beets_missing_import"

    def staged_audio(self):
        root = self.stage_root()
        return sorted(root.rglob("*.flac")) if root.is_dir() else []

    def stage_dirs(self):
        root = self.stage_root()
        return sorted(p for p in root.iterdir()) if root.is_dir() else []

    def mixed_torrent(self):
        self.torrent("Album", ["01.flac"])
        (self.dl / "Album" / "cover.jpg").write_bytes(b"torrent art")
        f = self.dl / "Album" / "01.flac"
        return f, _fingerprint(f)

    def test_proven_duplicate_leaves_no_staged_copy_in_any_batch(self):
        f, before = self.mixed_torrent()
        self.skip = _duplicate
        out, folders = self.run_batch()
        state = folders[str(self.dl / "Album")]
        self.assertEqual(state["failure_reason"], "already in library", self.log)
        self.assertNotEqual(state.get("staged_folder"), None, "the unit was imported from a staged copy")
        self.assertEqual(self.staged_audio(), [])
        self.assertEqual(self.stage_dirs(), [])
        self.batch_id += "-2"
        self.run_batch()
        self.assertEqual(self.staged_audio(), [])
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(_fingerprint(f), before)
        self.assertEqual((self.dl / "Album" / "cover.jpg").read_bytes(), b"torrent art")

    def test_source_changed_during_import_leaves_no_staged_copy(self):
        f, before = self.mixed_torrent()

        def changed(folder):
            f.write_bytes(b"X" * f.stat().st_size)  # the torrent re-checks its file mid-import
            return {"ok": True, "albums_imported": 0, "imported_album_ids": [],
                    "skipped": [{"path": str(folder), "reason": "no_strong_match"}],
                    "not_matched": [folder], "not_matched_known": True}

        self.reimport.side_effect = lambda folder, opts, timeout: changed(folder)
        out, folders = self.run_batch()
        state = folders[str(self.dl / "Album")]
        self.assertEqual(state["status"], "source_missing", self.log)
        self.assertEqual(state["current_step"], "source modified during import")
        self.assertEqual(self.rows, {})
        self.assertEqual(self.staged_audio(), [])
        self.assertEqual(self.stage_dirs(), [])

    def test_quality_upgrade_row_points_at_the_torrent_and_keeps_no_copy(self):
        f, before = self.mixed_torrent()
        self.skip = _duplicate
        upgrade = {"release_group_id": RG, "library_album_ids": [3], "library_format": "MP3 320",
                   "source_format": "FLAC"}
        with mock.patch.object(base.ai, "_ai_batch_quality_upgrade", return_value=upgrade):
            out, folders = self.run_batch()
        state = folders[str(self.dl / "Album")]
        self.assertEqual(state["current_step"], "upgrade available", self.log)
        self.assertEqual(list(self.rows), [str((self.dl / "Album").resolve())])
        evidence = self.rows[str((self.dl / "Album").resolve())]
        self.assertEqual(evidence["source_mode"], "qbittorrent")
        self.assertEqual(evidence["torrent_files"], ["01.flac"])
        self.assertEqual(self.staged_audio(), [])
        self.assertEqual(self.stage_dirs(), [])
        self.assertEqual(_fingerprint(f), before)

    def test_second_batch_keeps_only_the_stage_a_review_row_needs(self):
        f, before = self.mixed_torrent()
        base.ai.identify_import_folder.return_value = {
            "candidates": [], "warnings": [], "auto_import": {"eligible": False, "reasons": ["no_proof"]}}
        out, folders = self.run_batch()
        state = folders[str(self.dl / "Album")]
        self.assertEqual(state["status"], "review_created", self.log)
        stages = self.stage_dirs()
        self.assertEqual(len(stages), 1)
        self.assertEqual(list(self.rows), [str(stages[0].resolve())])  # the row needs this copy
        self.batch_id += "-2"
        out, folders = self.run_batch()
        state = folders[str(self.dl / "Album")]
        self.assertEqual(state["current_step"], "review item already exists", self.log)
        self.assertEqual(self.stage_dirs(), stages)
        self.assertEqual(len(self.staged_audio()), 1)
        self.assertEqual(_fingerprint(f), before)
        self.assertEqual(self.rows[str(stages[0].resolve())]["source_mode"], "qbittorrent")


if __name__ == "__main__":
    unittest.main()
