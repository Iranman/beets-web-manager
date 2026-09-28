"""Routes for replacing an album slot's file with a tracked duplicate.

/api/items/<iid>/replacement/plan with candidate_item_id: the candidate's
path comes from Beets (never the client), the pair is AcoustID-verified,
and a Preview transaction is created. Apply goes through the generic
/api/transactions/<id>/approve + /apply routes (or the item apply route)
and reaches the Beets engine only after approval.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import app as flask_app
import backend.composite_workflows as composite_workflows
from backend.transaction_engine import TransactionStore

try:
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402

REC = "2513c401-c500-42fe-9113-ce9d9c3295d5"


class ItemFileReplacementRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.mp3 = root / "17 Exotic.mp3"
        self.mp3.write_bytes(b"MP3")
        self.flac = root / "exotic (00).flac"
        self.flac.write_bytes(b"FLAC")
        self.store = TransactionStore(str(root / "transactions"))

        items = {
            24258: {"id": 24258, "title": "Exotic", "album_id": 1935, "mb_trackid": REC, "mb_albumid": "rel",
                    "mb_releasegroupid": "rg", "disc": 1, "track": 17, "format": "MP3", "path": str(self.mp3)},
            22575: {"id": 22575, "title": "exotic (00)", "album_id": None, "mb_trackid": "", "disc": 0,
                    "track": 17, "format": "FLAC", "path": str(self.flac)},
        }
        self.items = items
        self.adapter = mock.MagicMock()
        self.adapter.get_item.side_effect = lambda iid: items.get(int(iid))

        def lib_item(iid):
            row = items.get(int(iid))
            if row is None:
                return None
            obj = mock.MagicMock()
            for k, v in row.items():
                setattr(obj, k, v)
            return obj

        for target, attr, value in (
            (flask_app.lib, "get_item", lib_item),
        ):
            p = mock.patch.object(target, attr, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(composite_workflows, "beets_adapter", self.adapter)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(composite_workflows, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)
        for module in ("routes_maintenance",):
            mod = __import__(module)
            p = mock.patch.object(mod, "transactions", self.store)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        p.start()
        self.addCleanup(p.stop)
        flask_app.app.config["TESTING"] = True
        self.client = flask_app.app.test_client()

    def _plan(self, fingerprint=(REC, [REC], [REC])):
        with patch_app_family(flask_app, "_acoustid_fingerprint_match", return_value=fingerprint), \
             patch_app_family(flask_app, "_acoustid_fingerprint_ids", return_value=[]):
            return self.client.post("/api/items/24258/replacement/plan", json={"candidate_item_id": 22575})

    def test_plan_with_candidate_item_creates_preview_without_mutation(self):
        res = self._plan()
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertEqual(data["status"], "Preview")
        self.assertEqual(data["replacement_item"]["item_id"], 22575)
        tx = self.store.get(data["operation_id"])
        self.assertEqual(tx["metadata"]["mutation_family"], composite_workflows.ITEM_FILE_REPLACEMENT_FAMILY)
        self.assertEqual(tx["metadata"]["before"]["mb_trackid"], REC)
        self.adapter.replace_item_file.assert_not_called()

    def test_plan_resolves_library_relative_paths_for_fingerprinting(self):
        """The stock Beets web API reports paths relative to the library."""
        import backend.acoustid_service as acoustid_service
        root = self.mp3.parent
        self.items[24258]["path"] = self.mp3.name
        self.items[22575]["path"] = self.flac.name
        with mock.patch.object(acoustid_service, "MUSIC_ROOT", root), \
             patch_app_family(flask_app, "_acoustid_fingerprint_match", return_value=(REC, [REC], [REC])) as fp:
            res = self.client.post("/api/items/24258/replacement/plan", json={"candidate_item_id": 22575})
        self.assertEqual(res.status_code, 200, res.get_json())
        fp.assert_called_once_with(str(root / self.flac.name), str(root / self.mp3.name))

    def test_plan_refuses_an_unverified_candidate(self):
        res = self._plan(fingerprint=("", [], ["other-recording"]))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "candidate_not_verified")
        self.assertEqual([f for f in self.store.root.glob("*.json") if f.name != "settings.json"], [])

    def test_plan_rejects_unknown_candidate_item(self):
        with patch_app_family(flask_app, "_acoustid_fingerprint_match", return_value=(REC, [REC], [REC])):
            res = self.client.post("/api/items/24258/replacement/plan", json={"candidate_item_id": 999})
        self.assertEqual(res.status_code, 404)

    def test_preview_approve_apply_through_generic_transaction_routes(self):
        op_id = self._plan().get_json()["operation_id"]

        early = self.client.post(f"/api/transactions/{op_id}/apply")
        self.assertEqual(early.status_code, 409)
        self.assertEqual(early.get_json()["code"], "not_approved")
        self.adapter.replace_item_file.assert_not_called()

        self.assertEqual(self.client.post(f"/api/transactions/{op_id}/approve").status_code, 200)
        new_path = str(self.mp3.with_suffix(".flac"))
        self.adapter.replace_item_file.return_value = {
            "success": True, "new_target_path": new_path, "quarantine_id": "0" * 32, "quarantine_path": "/config/webmanager-quarantine/x/a.mp3",
            "target_snapshot": {"id": 24258}, "source_snapshot": {"id": 22575},
        }
        self.items[24258] = {**self.items[24258], "format": "FLAC", "path": new_path}
        res = self.client.post(f"/api/transactions/{op_id}/apply")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["status"], "Completed")
        self.adapter.replace_item_file.assert_called_once_with(24258, 22575, idempotency_key=op_id)

        self.adapter.rollback_replace_item_file.return_value = {"success": True, "recreated_source_item_id": 30000}
        rb = self.client.post(f"/api/transactions/{op_id}/rollback")
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self.store.get(op_id)["status"], "Rolled Back")


if __name__ == "__main__":
    unittest.main()
