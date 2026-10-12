"""0.5.1 deletion-gate hotfix regressions.

I-4: a downloads source folder is deleted as "already in library" only when
every audio file has byte or AcoustID same-recording proof against an item of
the library album AND that album's Release Group is the target one. Title or
file-name agreement is never proof.
F15: DELETE /api/ai-pending-review with no path must not clear the queue.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.library_service as ls

ROOT = Path(__file__).resolve().parents[1]
RG = "11111111-1111-1111-1111-111111111111"
OTHER_RG = "22222222-2222-2222-2222-222222222222"
NOT_PROVEN = {"proven": False, "recording_id": "", "reason": "not_both_confirmed_for_one_recording",
              "drop_status": "", "keep_status": ""}


class SourceDeletionProofGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.lib = base / "music" / "Artist" / "Album"
        self.src = base / "downloads" / "Artist - Album"
        self.lib.mkdir(parents=True)
        self.src.mkdir(parents=True)
        self.items = []
        for i, name in enumerate(("01 Song One.flac", "02 Song Two.flac"), 1):
            (self.lib / name).write_bytes(f"library-audio-{i}".encode())
            # Same titles and file names as the library: title evidence would match.
            (self.src / name).write_bytes(f"different-encode-{i}".encode())
            self.items.append({"id": i, "path": str(self.lib / name), "mb_trackid": f"rec-{i}"})
        self.album = {"id": 7, "mb_releasegroupid": RG}
        self.deleted = []

        def fake_delete(path, log):
            self.deleted.append(path)
            shutil.rmtree(path)
            return True

        for name, kw in (
                ("get_album", {"side_effect": lambda aid: self.album}),
                ("find_all_items_by_album_id", {"side_effect": lambda aid: self.items})):
            p = mock.patch.object(ls.composite_workflows, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ls, "_delete_source_folder_already_in_library", side_effect=fake_delete)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, proof=None, rg=RG):
        proof = proof or (lambda drop, keep, rid="": NOT_PROVEN)
        log = []
        with mock.patch.object(ls, "same_recording_proof", side_effect=proof):
            ev = ls._delete_source_if_proven_in_library(str(self.src), 7, rg, log)
        return ev, log

    @staticmethod
    def _fingerprint_all(drop, keep, rid=""):
        # Proven only for the library item with the matching track number.
        same = Path(drop).name == Path(keep).name
        return {"proven": same, "recording_id": rid if same else "", "reason": "" if same else "x",
                "drop_status": "", "keep_status": ""}

    def test_title_only_evidence_keeps_the_folder(self):
        ev, log = self._run()
        self.assertFalse(ev["proven"])
        self.assertFalse(ev["deleted"])
        self.assertEqual(ev["reason"], "unproven_file")
        self.assertTrue((self.src / "01 Song One.flac").exists())
        self.assertEqual(self.deleted, [])
        self.assertTrue(any("Source kept" in line for line in log))

    def test_full_fingerprint_proof_with_release_group_match_deletes(self):
        ev, _ = self._run(self._fingerprint_all)
        self.assertTrue(ev["proven"])
        self.assertTrue(ev["deleted"])
        self.assertEqual([f["proof"] for f in ev["files"]], ["fingerprint", "fingerprint"])
        self.assertFalse(self.src.exists())

    def test_byte_identical_files_are_proof(self):
        for item in self.items:
            shutil.copyfile(item["path"], self.src / Path(item["path"]).name)
        ev, _ = self._run()
        self.assertTrue(ev["deleted"])
        self.assertEqual([f["proof"] for f in ev["files"]], ["byte", "byte"])

    def test_release_group_mismatch_keeps_the_folder_even_with_proof(self):
        self.album = {"id": 7, "mb_releasegroupid": OTHER_RG}
        ev, _ = self._run(self._fingerprint_all)
        self.assertEqual((ev["deleted"], ev["reason"]), (False, "release_group_mismatch"))
        self.assertTrue(self.src.exists())

    def test_unknown_target_release_group_keeps_the_folder(self):
        ev, _ = self._run(self._fingerprint_all, rg="")
        self.assertEqual((ev["deleted"], ev["reason"]), (False, "release_group_mismatch"))
        self.assertTrue(self.src.exists())

    def test_one_unproven_file_keeps_the_folder(self):
        (self.src / "cd2").mkdir()
        (self.src / "cd2" / "03 Bonus.flac").write_bytes(b"bonus")
        ev, _ = self._run(self._fingerprint_all)
        self.assertFalse(ev["deleted"])
        self.assertTrue((self.src / "cd2" / "03 Bonus.flac").exists())

    def test_fingerprint_unavailable_keeps_the_folder(self):
        def unavailable(drop, keep, rid=""):
            return {**NOT_PROVEN, "reason": "fingerprint_unavailable"}
        ev, _ = self._run(unavailable)
        self.assertFalse(ev["deleted"])
        self.assertTrue(self.src.exists())


class NoForcedTriggerPhraseTests(unittest.TestCase):
    def test_no_caller_forces_the_already_in_library_phrase(self):
        for rel in ("backend/import_service.py", "backend/library_service.py", "routes_import.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("_delete_if_already_in_library", text, rel)
            self.assertNotIn('"already in library", log)', text, rel)

    def test_import_handoff_uses_the_proof_gate(self):
        text = (ROOT / "backend/import_service.py").read_text(encoding="utf-8")
        self.assertEqual(text.count(
            "_delete_source_if_proven_in_library(folder_path, target_album_id, selected_releasegroupid, log)"), 2)


class PendingReviewClearTests(unittest.TestCase):
    def setUp(self):
        import app as app_module  # noqa: F401  (registers routes)
        import routes_import
        self.r = routes_import
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pending = Path(tmp.name) / "ai_pending_review.json"
        self.items = [{"path": "/downloads/A"}, {"path": "/downloads/B"}]
        self.pending.write_text(json.dumps(self.items))
        for name, value in (("_AI_PENDING_FILE", self.pending),
                            ("_load_pending_reviews", lambda **k: json.loads(self.pending.read_text())),
                            ("_record_ai_review_decision", lambda *a, **k: None)):
            p = mock.patch.object(routes_import, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _delete(self, **kw):
        with self.r.app.test_request_context("/api/ai-pending-review", method="DELETE", **kw):
            resp = self.r.clear_ai_pending_review()
        return resp[1] if isinstance(resp, tuple) else resp.status_code

    def test_empty_body_is_refused_and_queue_kept(self):
        for kw in ({}, {"json": {}}, {"json": {"path": ""}}, {"json": {"all": "true"}}, {"json": []}):
            with self.subTest(kw=kw):
                self.assertEqual(self._delete(**kw), 400)
                self.assertEqual(json.loads(self.pending.read_text()), self.items)

    def test_path_removes_only_that_item_atomically(self):
        with mock.patch.object(self.r, "_atomic_write_text", wraps=self.r._atomic_write_text) as write:
            self.assertEqual(self._delete(json={"path": "/downloads/A"}), 200)
        write.assert_called_once()
        self.assertEqual(json.loads(self.pending.read_text()), [{"path": "/downloads/B"}])

    def test_explicit_all_clears(self):
        self.assertEqual(self._delete(json={"all": True}), 200)
        self.assertEqual(json.loads(self.pending.read_text()), [])


if __name__ == "__main__":
    unittest.main()
