"""#569: AI batch import Stage 1 refuses a folder at, inside or above an
"Upgrade available" Review row, and refuses every folder when the Review
queue cannot be read, using the same strict check as the Review import routes.
Unrelated folders still import."""
import json
import unittest
from pathlib import Path
from unittest import mock

import backend.pending_review_store as prs

import tests.test_import_batch_phase_b as pb

RG = "22222222-2222-2222-2222-222222222222"


def setUpModule():
    pb.setUpModule()


class BatchQualityUpgradeRefusedTests(pb._Batch):
    def setUp(self):
        super().setUp()
        self.store = self.tmp / "ai_pending_review.json"
        self._upgrade_row(self.tmp / "Upgrade - Album [FLAC]")
        patcher = mock.patch.object(prs, "_AI_PENDING_FILE", new=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upgrade_row(self, path):
        self.upgrade = path
        self.store.write_text(json.dumps([
            {"path": str(path), "status": "review_required",
             "suggestion": {"mb_releasegroupid": RG, "mb_albumid": ""},
             "evidence": {"origin_step": "quality_upgrade", "duplicate_rule": "quality_upgrade"}}]))

    def _use(self, *folders):
        self.folders = []
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "01.flac").write_bytes(b"x")
            self.folders.append(str(folder))

    def _imported_paths(self):
        return [c.args[0] for c in self.stubs["reimport_source"].call_args_list]

    def _assert_refused(self, folders, folder):
        self.assertNotIn(folder, self._imported_paths())
        self.assertEqual((folders[folder]["status"], folders[folder]["failure_reason"]),
                         ("import_failed", "quality_upgrade_informational"), self.log)

    def test_disc_subfolder_of_upgrade_row_refused_unrelated_still_imports(self):
        disc, other = self.upgrade / "CD 01", self.tmp / "Other - Album"
        self._use(disc, other)
        _, folders = self.run_batch()
        self._assert_refused(folders, str(disc))
        self.assertEqual(folders[str(other)]["status"], "imported", self.log)
        self.assertEqual(self._imported_paths(), [str(other)])

    def test_parent_of_upgrade_row_refused(self):
        self._upgrade_row(self.tmp / "Parent" / "Upgrade - Album [FLAC]")
        self._use(self.upgrade)
        parent = self.upgrade.parent
        self.folders = [str(parent)]
        _, folders = self.run_batch()
        self._assert_refused(folders, str(parent))

    def test_unreadable_review_queue_refuses(self):
        other = self.tmp / "Other - Album"
        self._use(other)
        self.store.write_text("{not json")
        _, folders = self.run_batch()
        self._assert_refused(folders, str(other))
        self.assertEqual(self._imported_paths(), [])


if __name__ == "__main__":
    unittest.main()
