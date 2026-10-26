"""Regression: the recovery carrier's compare-and-swap must reject the LOSER
of a concurrent-snapshot race, and a best-effort preflight evidence write must
never downgrade a carrier that has already been claimed for import.

Finding #1 (BLOCKING): two WAL connections can both SELECT the same revision
before either commits, both pass ``cas_update``'s Python pre-check, then one
UPDATE matches 1 row (winner) and the other matches 0 rows WITHOUT raising.
Before the fix ``cas_update`` ignored ``rowcount`` and handed the loser the
WINNER's record as a false success -- defeating ``_recovery_claim_importing``
and letting two confirmers import the same track. Exactly one call must win;
the other must raise ``RecoveryCarrierConflictError``.

Finding #2: a delayed/concurrent folder preflight must not move a carrier out
of ``importing``/``imported``/terminal back to ``verifying``/``review_required``
(which would reopen the import claim gate for a second claimer).

Store-level only; no provider, live key, or live library is touched.
"""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.matching_service as ms
import backend.recovery_carrier as rc
from backend.recovery_carrier import (
    RecoveryCarrierStore,
    RecoveryCarrierConflictError,
)

RGID = "17a64abf-6d31-473f-a740-38ffa548f225"


class CarrierCasRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = RecoveryCarrierStore(Path(self.tmp) / "rc.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_snapshot_race_yields_one_winner_one_conflict(self):
        """Two cas_update calls that BOTH read revision N before either commits
        must resolve to exactly one success and one RecoveryCarrierConflictError
        -- never two successes. Fails against the pre-fix code (both succeed)."""
        base = self.store.upsert(
            "c1",
            {"state": "downloaded", "existing_album_id": 7,
             "mb_releasegroupid": RGID},
            {},
        )
        rev = base["revision"]

        # Force both writers to complete SELECT + the Python pre-check (both
        # observing revision N) BEFORE either runs its UPDATE, by blocking on a
        # barrier at _folder_key -- which cas_update calls after the pre-check
        # and immediately before the UPDATE. This deterministically recreates
        # the WAL double-snapshot the raw-SQLite probe showed.
        real_folder_key = rc._folder_key
        barrier = threading.Barrier(2)

        def _barriered_folder_key(path):
            barrier.wait(timeout=10)
            return real_folder_key(path)

        results = {}

        def _claim(name):
            try:
                got = self.store.cas_update(
                    "c1", rev, {"state": "importing"}, {}, writer_id=name)
                results[name] = ("OK", got["revision"], got["state"])
            except RecoveryCarrierConflictError as exc:
                results[name] = ("CONFLICT", str(exc))
            except Exception as exc:  # pragma: no cover - surfaced by assert
                results[name] = ("ERR:" + type(exc).__name__, str(exc))

        with mock.patch.object(rc, "_folder_key", _barriered_folder_key):
            ta = threading.Thread(target=_claim, args=("A",))
            tb = threading.Thread(target=_claim, args=("B",))
            ta.start(); tb.start()
            ta.join(timeout=15); tb.join(timeout=15)

        oks = [n for n, r in results.items() if r[0] == "OK"]
        conflicts = [n for n, r in results.items() if r[0] == "CONFLICT"]
        self.assertEqual(len(oks), 1,
                         f"expected exactly one winner, got {results!r}")
        self.assertEqual(len(conflicts), 1,
                         f"expected exactly one conflict, got {results!r}")
        # The persisted record was bumped exactly once (one real UPDATE).
        self.assertEqual(self.store.get("c1")["revision"], rev + 1)
        self.assertEqual(self.store.get("c1")["state"], "importing")

    def test_blind_write_still_succeeds_on_an_uncontended_row(self):
        """expected_revision=None must still succeed on a real row -- the
        rowcount backstop only rejects a genuine 0-row (lost race) UPDATE."""
        self.store.upsert("c2", {"state": "downloaded",
                                  "mb_releasegroupid": RGID}, {})
        got = self.store.cas_update("c2", None, {"state": "verifying"}, {})
        self.assertEqual(got["state"], "verifying")
        self.assertEqual(got["revision"], 2)


class CarrierPreflightNoDowngradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = str(Path(self.tmp) / "rc")
        rc._store_registry.clear()

    def tearDown(self):
        rc._store_registry.clear()
        if self._prev is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _preflight_result(self):
        # A "verified" preflight result (would otherwise drive -> 'verifying').
        return {"ok": True, "missing_track_proof": {"ok": True},
                "matching_decision": {}, "audio_count": 1,
                "matches": 1, "expected": 1, "release_group_id": RGID}

    def test_preflight_evidence_does_not_downgrade_a_claimed_carrier(self):
        """A late preflight on a carrier already in 'importing' must not move it
        back to 'verifying' -- that would reopen the import claim gate. Fails
        against the pre-fix blind advance_state(expected_revision=None)."""
        created = rc.create_carrier(existing_album_id=7, mb_releasegroupid=RGID,
                                    staged_folder_path="/downloads/x")
        cid = created["carrier_id"]
        rc.advance_state(cid, "importing")  # a recovery job claimed it

        ms._record_carrier_preflight_evidence({"carrier_id": cid},
                                              self._preflight_result())

        self.assertEqual(rc.get_carrier(cid)["state"], "importing")

    def test_preflight_evidence_does_not_reopen_imported_or_terminal(self):
        for terminal in ("imported", "rolled_back", "failed"):
            created = rc.create_carrier(existing_album_id=7,
                                        mb_releasegroupid=RGID,
                                        staged_folder_path=f"/downloads/{terminal}")
            cid = created["carrier_id"]
            rc.advance_state(cid, terminal)
            ms._record_carrier_preflight_evidence({"carrier_id": cid},
                                                  self._preflight_result())
            self.assertEqual(rc.get_carrier(cid)["state"], terminal)

    def test_preflight_evidence_still_records_on_an_unclaimed_carrier(self):
        """The guard must not block the normal path: a 'downloaded' carrier is
        still advanced to 'verifying' with evidence."""
        created = rc.create_carrier(existing_album_id=7, mb_releasegroupid=RGID,
                                    staged_folder_path="/downloads/ok",
                                    state="downloaded")
        cid = created["carrier_id"]
        ms._record_carrier_preflight_evidence({"carrier_id": cid},
                                              self._preflight_result())
        self.assertEqual(rc.get_carrier(cid)["state"], "verifying")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
