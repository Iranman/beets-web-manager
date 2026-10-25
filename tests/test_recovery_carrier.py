"""Phase-2 missing-track recovery carrier: durable identity SPINE + contract.

Covers the carrier store (schema/CAS), the published accessor contract
(create/load-by-folder/load-by-job/advance_state), the canonical-RG discipline
(ADR-0002: Release Group canonical, Release stored separately, never
substituted), the "identity is read from the store, never derived from the
folder name" guarantee, and the Review hand-off threading (structured identity
replaces the lossy allow_existing boolean).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.recovery_carrier as rc
from backend.recovery_carrier import (
    RecoveryCarrierStore,
    RecoveryCarrierConflictError,
    RecoveryCarrierUnsupportedSchemaError,
)


RGID = "17a64abf-6d31-473f-a740-38ffa548f225"
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class RecoveryCarrierStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecoveryCarrierStore(Path(self.tmp) / "rc.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cols(self, **over):
        cols = {
            "state": "downloading",
            "existing_album_id": 42,
            "mb_releasegroupid": RGID,
            "release_id": RELEASE,
            "destination_album_path": "/music/21 Savage/American Dream",
            "staged_folder_path": "",
            "origin_job_id": "job-1",
        }
        cols.update(over)
        return cols

    def test_upsert_is_idempotent_by_carrier_id(self):
        a = self.store.upsert("c1", self._cols(), {"k": 1})
        self.assertEqual(a["revision"], 1)
        b = self.store.upsert("c1", self._cols(state="downloaded"), {"k": 2})
        self.assertEqual(b["revision"], 2)  # second write bumps, not a new row
        self.assertEqual(b["state"], "downloaded")
        self.assertEqual(b["k"], 2)

    def test_cas_update_rejects_stale_revision(self):
        self.store.upsert("c1", self._cols(), {})
        rec = self.store.get("c1")
        self.store.cas_update("c1", rec["revision"], {"state": "imported"}, {})
        with self.assertRaises(RecoveryCarrierConflictError):
            self.store.cas_update("c1", rec["revision"], {"state": "failed"}, {})

    def test_get_by_folder_and_job(self):
        folder = os.path.join(self.tmp, "downloads", "x - soundcloud missing 1")
        os.makedirs(folder, exist_ok=True)
        self.store.upsert("c1", self._cols(staged_folder_path=folder), {})
        self.assertEqual(self.store.get_by_folder(folder)["carrier_id"], "c1")
        self.assertEqual(self.store.get_by_job("job-1")["carrier_id"], "c1")
        self.assertIsNone(self.store.get_by_folder(os.path.join(self.tmp, "nope")))

    def test_unsupported_schema_refused(self):
        self.store.upsert("c1", self._cols(), {})
        with self.store._get_conn() as conn:
            conn.execute("UPDATE recovery_carrier SET schema_version = 999 WHERE carrier_id = 'c1'")
        with self.assertRaises(RecoveryCarrierUnsupportedSchemaError):
            self.store.get("c1")


class RecoveryCarrierAccessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = self.tmp
        rc._store_registry.clear()

    def tearDown(self):
        rc._store_registry.clear()
        if self._prev is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_requires_canonical_release_group(self):
        with self.assertRaises(ValueError):
            rc.create_carrier(existing_album_id=1, mb_releasegroupid="")

    def test_release_group_canonical_release_separate(self):
        rec = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 6, "title": "pop ur shit"}],
            expected_tracks=[{"recording_id": "rec-1", "track": 6, "disc": 1, "duration": 190}],
            source={"method": "soundcloud"}, origin_job_id="job-9",
        )
        self.assertEqual(rec["mb_releasegroupid"], RGID)      # canonical
        self.assertEqual(rec["release_id"], RELEASE)          # supporting, separate
        self.assertNotEqual(rec["mb_releasegroupid"], rec["release_id"])
        self.assertEqual(rec["expected_tracks"][0]["recording_id"], "rec-1")
        self.assertEqual(rec["expected_tracks"][0]["track"], 6)
        self.assertEqual(rec["checkpoint"], "CREATED")
        self.assertEqual(rec["recovery_workflow_id"], rc.recovery_workflow_id(rec["carrier_id"]))

    def test_load_for_folder_reads_stored_identity_not_folder_name(self):
        cid = rc.create_carrier(existing_album_id=42, mb_releasegroupid=RGID)["carrier_id"]
        folder = os.path.join(self.tmp, "21 Savage", "american dream - soundcloud missing 1792778835542")
        os.makedirs(folder, exist_ok=True)
        # Before binding, the folder resolves to nothing (identity is never
        # derived from the "american dream ..." name).
        self.assertIsNone(rc.load_for_folder(folder))
        rc.bind_staged_folder(cid, folder)
        got = rc.load_for_folder(folder)
        self.assertIsNotNone(got)
        self.assertEqual(got["mb_releasegroupid"], RGID)      # from the store
        self.assertEqual(got["existing_album_id"], 42)

    def test_advance_state_and_checkpoint(self):
        cid = rc.create_carrier(existing_album_id=7, mb_releasegroupid=RGID)["carrier_id"]
        rec = rc.advance_state(cid, "downloaded", checkpoint="DOWNLOADED")
        self.assertEqual(rec["state"], "downloaded")
        self.assertEqual(rec["checkpoint"], "DOWNLOADED")
        rec = rc.advance_state(cid, "verifying", evidence={"acoustid": "ok"})
        self.assertEqual(rec["verification_evidence"], {"acoustid": "ok"})
        with self.assertRaises(ValueError):
            rc.advance_state(cid, "not-a-state")
        with self.assertRaises(ValueError):
            rc.advance_state(cid, "downloaded", checkpoint="NOPE")

    def test_advance_state_missing_carrier_returns_none(self):
        self.assertIsNone(rc.advance_state("ghost", "downloaded"))
        self.assertIsNone(rc.bind_staged_folder("ghost", "/tmp/x"))

    def test_review_identity_flatten(self):
        rec = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 6}],
        )
        ident = rc.review_identity(rec)
        self.assertTrue(ident["recovery"])
        self.assertEqual(ident["existing_album_id"], 42)
        self.assertEqual(ident["mb_releasegroupid"], RGID)
        self.assertEqual(ident["release_id"], RELEASE)
        self.assertEqual(ident["wanted_tracks"], [{"track": 6}])
        self.assertEqual(rc.review_identity(None), {})


class ReviewHandoffThreadingTests(unittest.TestCase):
    """The Phase-1 root cause was the Review hand-off reducing identity to a
    boolean. Prove the carrier now threads STRUCTURED identity into the queued
    suggestion, with allow_existing implied (the staged folder is outside the
    library, so folder-derived linkage is empty)."""

    def test_queue_folder_carries_structured_recovery_identity(self):
        import backend.pending_review_store as prs

        carrier = {
            "carrier_id": "c1",
            "existing_album_id": 42,
            "mb_releasegroupid": RGID,
            "release_id": RELEASE,
            "destination_album_path": "/music/21 Savage/American Dream",
            "wanted_tracks": [{"track": 6}],
            "expected_tracks": [{"recording_id": "rec-1", "track": 6}],
        }
        captured = {}

        def _fake_add(folder_path, sug, allow_existing=False, evidence=None, origin=None):
            captured["folder"] = folder_path
            captured["sug"] = sug
            captured["allow_existing"] = allow_existing
            captured["evidence"] = evidence

        with mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[]), \
             mock.patch.object(prs, "_add_to_pending", _fake_add):
            ok = prs._queue_folder_for_manual_review(
                "/downloads/21 Savage/american dream - soundcloud missing 1",
                {"album": "American Dream"},
                "preflight failed",
                None,
                carrier=carrier,
            )
        self.assertTrue(ok)
        sug = captured["sug"]
        self.assertTrue(sug["recovery"])
        self.assertEqual(sug["recovery_carrier_id"], "c1")
        self.assertEqual(sug["recovery_existing_album_id"], 42)
        self.assertEqual(sug["recovery_mb_releasegroupid"], RGID)
        self.assertEqual(sug["recovery_release_id"], RELEASE)
        self.assertEqual(sug["recovery_expected_tracks"], [{"recording_id": "rec-1", "track": 6}])
        # A carrier naming an existing album implies allow_existing, and the
        # existing_album_id survives even though the folder is outside the lib.
        self.assertTrue(captured["allow_existing"])
        self.assertIn(42, sug["existing_album_ids"])
        self.assertEqual(captured["evidence"]["recovery_carrier"]["existing_album_id"], 42)


if __name__ == "__main__":
    unittest.main()
