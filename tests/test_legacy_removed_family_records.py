"""PR #249 (BA-7): transaction records written by older versions for engine
families that no longer exist in backend/transaction_engine.py must fail
safely: the startup recovery sweep leaves them alone without raising, and the
generic apply/rollback/detail/list/export routes answer a controlled 4xx (or
200 for reads) without mutating or claiming the record."""

import json
import time
from unittest import mock

import backend.transaction_recovery as recovery
from tests.test_staging_mutation_hardening import _RouteEnv

# Engine-side families removed by #249 (the ones with no composite executor of
# the same name are the riskiest: nothing can run them any more).
REMOVED_FAMILIES = (
    "album_artwork_fetch_v1", "album_duplicate_merge_v1", "confirmed_import_v1",
    "library_cleanup_v1", "existing_album_reconcile_v1", "artist_folder_reconcile_v1",
    "album_maintenance_v1", "album_mb_track_repair_v1", "import_folder_v1",
    "playlist_media_cleanup_v1", "album_relocation_v1", "genre_repair_v1", "album_artwork_v1",
)
LEGACY_OPS = [{"type": "move_file", "source": "/old/a.flac", "dest": "/old/b.flac"},
              {"type": "delete_db_record", "item_id": 3}]


class LegacyRemovedFamilyRecordTests(_RouteEnv):
    def _legacy(self, family, status, n):
        tx_id = f"txn_{int(time.time())}_legacy{n:04d}"
        record = {
            "id": tx_id, "operation_type": "Library Cleanup", "status": status,
            "created_at": time.time(), "updated_at": time.time(), "dry_run": False,
            "summary": f"legacy {family}", "metadata": {"mutation_family": family, "db_path": "/x/library.db"},
            "rollback": {"available": True, "operations": LEGACY_OPS}, "logs": [], "changes": [],
        }
        self.store._ensure()
        (self.store.root / f"{tx_id}.json").write_text(json.dumps(record), encoding="utf-8")
        return tx_id

    def test_recovery_sweep_skips_legacy_running_records(self):
        ids = [self._legacy(fam, "Running", i) for i, fam in enumerate(REMOVED_FAMILIES)]
        adapter = mock.Mock()
        results = recovery.sweep(adapter=adapter, store=self.store)
        self.assertEqual(results, [])
        self.assertEqual(adapter.method_calls, [])
        for tx_id in ids:
            self.assertEqual(self.store.get(tx_id)["status"], "Running")
            self.assertEqual(recovery.resolve_transaction(self.store.get(tx_id), adapter=adapter,
                                                          store=self.store)["action"], "skipped")

    def test_routes_refuse_legacy_records_without_mutating(self):
        n = 0
        for fam in REMOVED_FAMILIES:
            for status in ("Preview", "Approved", "Running", "Completed", "Failed"):
                n += 1
                tx_id = self._legacy(fam, status, n)
                before = self.store.get(tx_id)
                for verb in ("apply", "rollback"):
                    resp = self.client.post(f"/api/transactions/{tx_id}/{verb}")
                    self.assertIn(resp.status_code, (400, 404, 409), (fam, status, verb, resp.get_json()))
                    self.assertFalse(resp.get_json()["ok"], (fam, status, verb))
                after = self.store.get(tx_id)
                self.assertEqual((after["status"], after["rollback"]), (before["status"], before["rollback"]),
                                 (fam, status))
                self.assertEqual(self.client.get(f"/api/transactions/{tx_id}").status_code, 200, (fam, status))
        listing = self.client.get("/api/transactions?limit=500")
        self.assertEqual(listing.status_code, 200)
        for fmt in ("json", "csv", "md"):
            self.assertLess(self.client.get(f"/api/transactions/{tx_id}/export?format={fmt}").status_code, 500)


if __name__ == "__main__":
    import unittest
    unittest.main()
