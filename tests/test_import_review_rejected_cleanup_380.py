"""#380 F3 + QA #387 NB1: Import Review rejected-file cleanup through the real
route with real temp files (unlink/rename sinks not mocked).

F3: delete_* is the client's claim, so the route downgrades it to a
restorable quarantine. NB1: the UI sends absolute source_path values; only
those lexically under the authorized review folder are acted on."""

import os
import sys
import unittest

try:
    from test_sec002_app_path_import_review_cleanup import ImportReviewCleanupPathBoundaryTests as _Base
except ImportError:  # pragma: no cover
    from tests.test_sec002_app_path_import_review_cleanup import ImportReviewCleanupPathBoundaryTests as _Base

from backend.transaction_engine import execute_import_review_cleanup_plan, rollback_import_review_cleanup


class RejectedCleanupTests(unittest.TestCase):
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    _write_audio = _Base._write_audio
    _write_pending = _Base._write_pending
    _post_cleanup = _Base._post_cleanup

    def _snapshot(self, root):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def test_ui_shaped_absolute_paths_quarantine_exactly_the_requested_files(self):
        folder = self.downloads / "Artist - Album"
        bad = self._write_audio(folder / "03 Wrong Track.flac")
        nested = self._write_audio(folder / "CD2" / "01 x.flac")
        good = self._write_audio(folder / "01 Good.flac")
        prefix_sibling = self._write_audio(self.downloads / "Artist - Album 2" / "03 Wrong Track.flac")
        victim = self._write_audio(self.downloads / "victim.flac")
        outside = self._write_audio(self.outside / "o.flac")
        self._write_pending(folder)
        files = [str(bad), str(nested), str(prefix_sibling), str(victim), str(outside),
                 str(folder / ".." / "victim.flac"), str(folder) + "/", str(folder / "missing.flac")]

        data = self._post_cleanup(folder, files).get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["quarantined_count"], 2, data)
        self.assertEqual(data["deleted_count"], 0, data)
        self.assertEqual({p.name for p in (bad, nested)},
                         {os.path.basename(item["source"]) for item in data["quarantined"]})
        self.assertFalse(bad.exists())
        self.assertFalse(nested.exists())
        for kept in (good, prefix_sibling, victim, outside):
            self.assertTrue(kept.exists(), kept)
        self.assertEqual(len(self._snapshot(self.quarantine)), 2)
        self.assertEqual(data["remaining_audio_count"], 1)

    def test_delete_rejected_is_downgraded_to_restorable_quarantine(self):
        folder = self.downloads / "Review"
        bad = self._write_audio(folder / "bad.flac")
        good = self._write_audio(folder / "good.flac")
        self._write_pending(folder)

        data = self._post_cleanup(folder, [str(bad)], action="delete_rejected", allow_delete=True).get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["requested_action"], "delete_rejected")
        self.assertEqual(data["action"], "quarantine_rejected")
        self.assertTrue(data["downgraded_to_quarantine"])
        self.assertEqual(data["deleted_count"], 0)
        self.assertEqual(data["quarantined_count"], 1)
        self.assertFalse(bad.exists())
        self.assertTrue(good.exists())

        rb = rollback_import_review_cleanup(self.tx_store, data["operation_id"])
        self.assertTrue(rb.get("ok"), rb)
        self.assertEqual(bad.read_bytes(), b"not-real-audio-but-extension-is-enough")

    def test_delete_duplicate_is_downgraded_too(self):
        folder = self.downloads / "Dupes"
        dup = self._write_audio(folder / "dup.flac")
        self._write_pending(folder)

        data = self._post_cleanup(folder, ["dup.flac"], action="delete_duplicate", allow_delete=True).get_json()

        self.assertEqual(data["action"], "quarantine_duplicate", data)
        self.assertEqual((data["deleted_count"], data["quarantined_count"]), (0, 1))
        self.assertFalse(dup.exists())
        self.assertEqual(len(self._snapshot(self.quarantine)), 1)
        self.assertTrue(data["pending_review_removed"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "symlink + POSIX path semantics")
    def test_raw_folder_with_dotdot_through_symlink_uses_resolved_prefix_only(self):
        # S-LOW-1: raw "dl/hop/../Album" with hop -> dl/deep/sub resolves to
        # dl/deep/Album, but normpath says dl/Album. A client path under
        # dl/Album must not be mapped onto dl/deep/Album.
        (self.downloads / "deep" / "sub").mkdir(parents=True)
        os.symlink(self.downloads / "deep" / "sub", self.downloads / "hop")
        real = self._write_audio(self.downloads / "deep" / "Album" / "x.flac")
        decoy = self._write_audio(self.downloads / "Album" / "x.flac")
        raw = f"{self.downloads}/hop/../Album"

        res = execute_import_review_cleanup_plan(
            self.tx_store, {"path": raw, "files": [str(decoy)], "action": "quarantine_rejected"},
            [str(self.downloads)], music_root=str(self.music))

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["skipped"], [{"file": str(decoy), "reason": "outside_review_folder"}])
        steps = self.tx_store.get(res["operation_id"])["metadata"]["steps"]
        self.assertEqual(steps, [])
        # The resolved folder's own absolute path is still accepted.
        res2 = execute_import_review_cleanup_plan(
            self.tx_store, {"path": raw, "files": [str(real)], "action": "quarantine_rejected"},
            [str(self.downloads)], music_root=str(self.music))
        self.assertEqual([s["source"] for s in self.tx_store.get(res2["operation_id"])["metadata"]["steps"]],
                         [str(real)])
        self.assertTrue(decoy.exists() and real.exists())


del _Base  # borrow its fixtures without re-running its tests here


if __name__ == "__main__":
    unittest.main()
