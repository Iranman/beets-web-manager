"""A quality-upgrade Review row is informational: every import path refuses
its folder (409 quality_upgrade_informational) with no plan/apply call, no
file touched and the row kept. Dismissing the row still works, and a normal
Review row still starts an import."""
import hashlib
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.ai_service as ai_service
import backend.import_review_service as irs
import backend.import_service as isvc
import backend.pending_review_store as prs

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"
CODE = "quality_upgrade_informational"
_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


def _digest(folder: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


class QualityUpgradeRowRefusedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.torrent = root / "torrents" / "Artist - Album [FLAC]"
        self.torrent.mkdir(parents=True)
        for name in ("01 - One.flac", "02 - Two.flac"):
            (self.torrent / name).write_bytes(name.encode() * 64)
        self.normal = root / "torrents" / "Other - Album"
        self.normal.mkdir()
        (self.normal / "01.flac").write_bytes(b"normal")
        self.store = root / "ai_pending_review.json"
        self.store.write_text(json.dumps([
            {"path": str(self.torrent), "status": "review_required",
             "suggestion": {"mb_releasegroupid": RG, "mb_albumid": ""},
             "evidence": {"origin_step": "quality_upgrade", "duplicate_rule": "quality_upgrade",
                          "quality_upgrade": {"release_group_id": RG, "library_album_ids": [5]}}},
            {"path": str(self.normal), "status": "review_required",
             "suggestion": {"mb_releasegroupid": RG, "mb_albumid": REL}, "evidence": {}},
        ]))
        self.files_before = _digest(self.torrent)
        self.plan = mock.Mock(return_value={"ok": True, "operation_id": "op"})
        self.apply = mock.Mock(return_value={"ok": True})
        self.start = mock.Mock(return_value=types.SimpleNamespace(job_id="job-1"))
        cw = isvc.composite_workflows
        for target, name, kw in (
            (prs, "_AI_PENDING_FILE", {"new": self.store}),
            (cw, "plan_confirmed_import", {"new": self.plan}),
            (cw, "apply_confirmed_import", {"new": self.apply}),
            (isvc.jobs, "start_python", {"new": self.start}),
            (isvc, "_resolve_import_review_source_path", {"side_effect": lambda p, **k: (Path(p), None)}),
            (isvc, "_folder_import_selected_sources", {"return_value": ([], None)}),
            (isvc, "_preserve_torrent_source_path", {"return_value": True}),
            (isvc, "_review_row_is_torrent_source", {"return_value": True}),
            (isvc, "_library_album_ids_for_folder", {"return_value": []}),
            (isvc, "_folder_import_reentry_identity", {"side_effect": lambda f, a, w: (a, w)}),
            (isvc, "_cached_import_target_preview", {"return_value": {"safe": True, "tracks_to_import_count": 2}}),
            (prs, "_record_ai_review_decision", {}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def _assert_untouched(self):
        self.plan.assert_not_called()
        self.apply.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(_digest(self.torrent), self.files_before)
        rows = json.loads(self.store.read_text())
        self.assertEqual([r["path"] for r in rows], [str(self.torrent), str(self.normal)])
        self.assertEqual(rows[0]["status"], "review_required")

    def test_import_with_id_refused(self):
        resp = self.client.post("/api/folders/import-with-id", headers=_CSRF, json={
            "path": str(self.torrent), "mb_albumid": REL, "mb_releasegroupid": RG,
            "review_item_id": f"pending:{self.torrent}"})
        self.assertEqual(resp.status_code, 409, resp.get_json())
        body = resp.get_json()
        self.assertEqual(body["error_code"], CODE)
        self.assertFalse(body["ok"])
        self.assertIn("not available yet", body["error"])
        self._assert_untouched()

    def test_auto_enqueue_refused(self):
        with mock.patch.object(irs, "start_folder_import_with_id") as started:
            resp = self.client.post("/api/import-review/auto-enqueue", headers=_CSRF, json={
                "path": str(self.torrent), "mb_albumid": REL, "mb_releasegroupid": RG,
                "confidence_score": 99, "selected_source_files": [str(p) for p in self.torrent.iterdir()]})
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertEqual(resp.get_json()["error_code"], CODE)
        started.assert_not_called()
        self._assert_untouched()

    def test_eligibility_blocks_ready_batch_and_revalidate(self):
        eligibility = isvc.evaluate_import_eligibility({"path": str(self.torrent), "mb_albumid": REL,
                                                        "mb_releasegroupid": RG, "confidence_score": 99})
        self.assertFalse(eligibility["eligible"])
        self.assertIn(prs.QUALITY_UPGRADE_INFORMATIONAL["error"], eligibility["blocking_reasons"])
        with mock.patch.object(irs, "_pending_item_is_ready_for_backend_auto_import", return_value=True), \
                mock.patch.object(irs, "_import_review_suggestion_is_stale", return_value=False), \
                mock.patch.object(irs, "_trusted_stored_review_path", side_effect=lambda p: p), \
                mock.patch.object(irs, "_import_review_start_auto_import") as started:
            out = irs._run_import_review_auto_enqueue_ready_batch(25)
        queued_paths = [c.args[0].get("path") for c in started.call_args_list]
        self.assertNotIn(str(self.torrent), queued_paths, out)
        self._assert_untouched()

    def test_reimport_disk_refused(self):
        with mock.patch.object(isvc.composite_workflows, "inspect_import_source",
                               side_effect=lambda p, **k: {"ok": True, "path": p}):
            resp = self.client.post("/api/albums/reimport-disk", headers=_CSRF, json={
                "aldir": str(self.torrent), "mb_albumid": REL, "existing_album_id": 5})
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertEqual(resp.get_json()["error_code"], CODE)
        self._assert_untouched()

    def test_ai_import_folder_refused(self):
        with self.assertRaisesRegex(RuntimeError, "not available yet"):
            ai_service._ai_import_folder(str(self.torrent), REL, {}, [])
        self._assert_untouched()

    def test_dismiss_still_removes_the_row(self):
        resp = self.client.delete("/api/ai-pending-review", headers=_CSRF, json={"path": str(self.torrent)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual([r["path"] for r in json.loads(self.store.read_text())], [str(self.normal)])
        self.assertEqual(_digest(self.torrent), self.files_before)
        self.plan.assert_not_called()

    def test_normal_row_still_starts_import(self):
        body, code = isvc.start_folder_import_with_id(
            {"path": str(self.normal), "mb_albumid": REL, "mb_releasegroupid": RG})
        self.assertEqual(code, 200, body)
        self.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
