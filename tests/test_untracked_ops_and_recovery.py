"""Untracked recovery service (backend/untracked_recovery_service.py):
backend-owned eligibility, deterministic identity for Class B attach,
provider failures never treated as "no match", strict Class A quarantine,
Class C through the one replacement authority, Plan -> Approve -> Apply ->
Verify -> Rollback."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as composite_workflows
import backend.untracked_recovery_service as svc
from backend.provider_boundary import ProviderOutcome, ProviderResult
from backend.resource_locks import ResourceLocks, set_locks
from backend.transaction_engine import TransactionStore

REL = "45347542-db98-422a-a307-ae95d5371f60"
RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REC13 = "00000000-0000-0000-0000-000000000013"


class FakeAdapter:
    def __init__(self, root):
        self.root = root
        self.albums = {1935: {"id": 1935, "mb_albumid": REL, "mb_releasegroupid": RG}}
        self.items = {25264: {"id": 25264, "album_id": 1935, "disc": 1, "track": 11,
                              "mb_trackid": "00000000-0000-0000-0000-000000000011",
                              "path": str(root / "a" / "11 Top Notch.flac")}}
        self.attach_calls = []
        self.quarantine_calls = []

    def find_all_albums_by_mb_albumid(self, rel):
        return [a for a in self.albums.values() if a["mb_albumid"] == rel]

    def find_all_items_by_album_id(self, aid):
        return [i for i in self.items.values() if i["album_id"] == aid]

    def find_all_items_by_mbid(self, rid):
        return [i for i in self.items.values() if i["mb_trackid"] == rid]

    def get_items(self, query=None):
        return list(self.items.values())

    def get_item(self, iid):
        return self.items.get(int(iid))

    def get_stats(self):
        return {"items": len(self.items), "albums": len(self.albums)}

    def untracked_attach(self, path, sha256, album_id, expected, idempotency_key):
        self.attach_calls.append(idempotency_key)
        self.items[30000] = {"id": 30000, "album_id": album_id, "disc": expected["disc"], "track": expected["track"],
                             "mb_trackid": expected["mb_trackid"], "path": path}
        return {"success": True, "record_id": svc.record_id_for("attach", idempotency_key), "item_id": 30000}

    def untracked_quarantine(self, files, idempotency_key):
        self.quarantine_calls.append(files)
        for f in files:
            Path(f["path"]).rename(Path(f["path"] + ".q"))
        return {"success": True, "record_id": svc.record_id_for("quarantine", idempotency_key)}

    def untracked_rollback(self, record_id, idempotency_key=None):
        self.items.pop(30000, None)
        return {"success": True}


def confirmed(*ids):
    return ProviderResult("acoustid", ProviderOutcome.CONFIRMED, data=[{"mb_trackid": i} for i in ids])


class UntrackedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "music"
        (self.root / "a").mkdir(parents=True)
        (self.root / "a" / "11 Top Notch.flac").write_bytes(b"tracked")
        self.missing = self.root / "a" / "BossMan Dlow - 2 Slippery - 13 - Parmesan.flac"
        self.missing.write_bytes(b"parmesan")
        self.inv = Path(self._tmp.name) / "inv"
        self.inv.mkdir()
        self.adapter = FakeAdapter(self.root)
        self.store = TransactionStore(str(Path(self._tmp.name) / "tx"))
        set_locks(ResourceLocks(Path(self._tmp.name) / "locks"))
        self.addCleanup(set_locks, None)
        p = mock.patch.object(composite_workflows, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)
        self.tags = {"mb_trackid": REC13, "mb_albumid": REL, "mb_releasegroupid": RG, "disc": 1, "track": 13,
                     "title": "Parmesan"}
        self.mb = {"ok": True, "outcome": "confirmed", "tracks": [{"disc": 1, "track": 13, "mb_trackid": REC13}]}
        self.heard = confirmed(REC13)
        self.deps = {"music_root": self.root, "inventory_dir": self.inv, "abs_path": lambda p: p,
                     "acoustid": lambda p: self.heard, "mb_tracklist": lambda rel: self.mb,
                     "read_tags": lambda p: dict(self.tags)}

    def _plan(self, action="attach", paths=None):
        return svc.plan_recovery(action, paths or ["a/BossMan Dlow - 2 Slippery - 13 - Parmesan.flac"],
                                 adapter=self.adapter, store=self.store, deps=self.deps)

    def _inventory(self, *records):
        with open(self.inv / "untracked_inventory.jsonl", "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def test_candidates_carry_backend_owned_eligibility(self):
        self._inventory({"path": "x/a.flac", "category": "import_artifact"},
                        {"path": "a/b.flac", "category": "canonical_album_file_missing_from_beets"},
                        {"path": "a/c.flac", "category": "exact_duplicate_of_tracked", "sha256": "1" * 64})
        rows = {r["path"]: r for r in svc.candidates(deps=self.deps)["rows"]}
        self.assertEqual(rows["x/a.flac"]["action_eligibility"], "not_eligible")
        self.assertIsNone(rows["x/a.flac"]["action"])  # a naming pattern alone is never proof
        self.assertEqual(rows["a/b.flac"]["action"], "attach")
        self.assertEqual(rows["a/c.flac"]["action"], "quarantine")
        for row in rows.values():
            for key in ("action", "action_eligibility", "requires_review", "safety_result", "conflicts", "reason"):
                self.assertIn(key, row)

    def test_class_b_attach_is_planned_only_with_deterministic_identity(self):
        res = self._plan()
        self.assertTrue(res["ok"], res)
        meta = self.store.get(res["operation_id"])["metadata"]
        self.assertEqual((meta["album_id"], meta["expected"]["track"]), (1935, 13))
        self.assertEqual(meta["sha256"], hashlib.sha256(b"parmesan").hexdigest())
        self.assertEqual(self.adapter.attach_calls, [])  # planning never mutates

    def test_class_b_refusals(self):
        self.tags["mb_trackid"] = ""
        self.assertEqual(self._plan()["code"], "no_recording_id")
        self.tags["mb_trackid"] = REC13
        self.adapter.albums[1936] = {"id": 1936, "mb_albumid": REL}
        self.assertEqual(self._plan()["code"], "album_row_not_unique")
        del self.adapter.albums[1936]
        self.mb = {"ok": True, "tracks": [{"disc": 1, "track": 13, "mb_trackid": "other"}]}
        self.assertEqual(self._plan()["code"], "slot_recording_mismatch")
        self.mb = {"ok": True, "tracks": [{"disc": 1, "track": 13, "mb_trackid": REC13}]}
        self.heard = confirmed("someone-else")
        self.assertEqual(self._plan()["code"], "fingerprint_disagreement")
        self.adapter.items[1] = {"id": 1, "album_id": 1935, "disc": 1, "track": 13, "mb_trackid": REC13, "path": "z"}
        self.heard = confirmed(REC13)
        self.assertEqual(self._plan()["code"], "slot_occupied")

    def test_provider_outages_fail_as_outages_never_as_no_match(self):
        self.mb = {"ok": False, "outcome": "unavailable", "tracks": []}
        self.assertEqual(self._plan()["code"], "musicbrainz_unavailable")
        self.mb = {"ok": True, "tracks": [{"disc": 1, "track": 13, "mb_trackid": REC13}]}
        for outcome in (ProviderOutcome.UNAVAILABLE, ProviderOutcome.RATE_LIMITED,
                        ProviderOutcome.AUTHENTICATION_ERROR, ProviderOutcome.TRANSIENT_ERROR):
            self.heard = ProviderResult("acoustid", outcome, data=[])
            self.assertEqual(self._plan()["code"], f"acoustid_{outcome.value}")

    def test_attach_apply_verify_and_roll_back(self):
        op = self._plan()["operation_id"]
        self.assertEqual(svc.apply_recovery(op, adapter=self.adapter, store=self.store)["code"], "not_approved")
        self.store.update(op, status="Approved")
        res = svc.apply_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["items_before"], res["items_after"]), (1, 2))
        self.assertEqual(self.adapter.attach_calls, [op])
        self.assertEqual(svc.apply_recovery(op, adapter=self.adapter, store=self.store)["code"], "already_applied")
        rb = svc.rollback_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_class_c_tracks_as_singleton_then_plans_the_canonical_replacement(self):
        self.tags.update(mb_trackid="00000000-0000-0000-0000-000000000011", track=11)
        self.heard = confirmed("00000000-0000-0000-0000-000000000011")
        res = self._plan("track_for_replacement")
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["album_id"])
        op = res["operation_id"]
        self.store.update(op, status="Approved")
        with mock.patch("backend.item_replacement.plan_verified_replacement",
                        return_value={"ok": True, "operation_id": "tx-repl"}) as plan_repl:
            out = svc.apply_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(out["ok"], out)
        plan_repl.assert_called_once()
        self.assertEqual(plan_repl.call_args.args[:2], (25264, 30000))
        self.assertEqual(out["replacement_plan"]["operation_id"], "tx-repl")

    def test_class_a_quarantine_only_for_proven_byte_identical_copies(self):
        twin = self.root / "a" / "11 Top Notch.flac"
        copy = self.root / "a" / "copy.flac"
        copy.write_bytes(b"tracked")
        artifact = self.root / "a" / "X.1.flac"
        artifact.write_bytes(b"whatever")
        self._inventory({"path": "a/copy.flac", "category": "exact_duplicate_of_tracked", "duplicate_of": str(twin)},
                        {"path": "a/X.1.flac", "category": "import_artifact"})
        res = self._plan("quarantine", ["a/copy.flac", "a/X.1.flac"])
        self.assertTrue(res["ok"], res)
        self.assertEqual([Path(f["path"]).name for f in res["files"]], ["copy.flac"])
        self.assertEqual(res["refused"][0]["path"], "a/X.1.flac")
        copy.write_bytes(b"changed")  # no longer identical -> refused at plan time
        self.assertEqual(self._plan("quarantine", ["a/copy.flac"])["code"], "nothing_eligible")

    def test_paths_outside_the_music_root_are_refused(self):
        self.assertEqual(self._plan(paths=["../../etc/passwd"])["code"], "file_missing")


if __name__ == "__main__":
    unittest.main()
