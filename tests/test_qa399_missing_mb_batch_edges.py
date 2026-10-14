"""QA #399: independent edge / failure-mode checks for the Missing IDs batch.

Reuses the fake world from tests/test_missing_mb_batch.py. Tests named
``test_defect_*`` assert the required behavior and fail on 52bb70f.
"""
import threading
import time
import unittest
from unittest import mock

import backend.missing_mb_batch as mb
from backend.provider_boundary import ProviderOutcome, ProviderResult

try:
    import test_missing_mb_batch as base
except ImportError:  # pragma: no cover
    import tests.test_missing_mb_batch as base
FOLDER, RG, REL = base.FOLDER, base.RG, base.REL
_BatchCase, _Case, _World, _candidate, _ident = base._BatchCase, base._Case, base._World, base._candidate, base._ident

OTHER_RG = "55555555-5555-5555-5555-555555555555"


class ProofEdgeTests(_Case):
    def setUp(self):
        self.w = _World(self)

    def test_zero_track_album_is_not_proven(self):
        row = self.w.add_album(1)
        self.w.album_items[1] = []
        proof = mb.prove_row(row)
        self.assertFalse(proof["proven"])
        self.assertEqual(proof["reasons"], ["no_items"])

    def test_multi_disc_album_needs_total_track_count(self):
        row = self.w.add_album(1, files=4, tracks=4)
        self.w.album_items[1] = [{"id": 10 + n, "path": f"Artist/Album1/CD{d}/0{n}.flac"}
                                 for d, n in ((1, 1), (1, 2), (2, 3), (2, 4))]
        self.w.folder_files[f"{FOLDER}1"] = [str(base.MUSIC_ROOT / it["path"]) for it in self.w.album_items[1]]
        self.assertTrue(mb.prove_row(row)["proven"], mb.prove_row(row)["reasons"])
        self.w.idents[f"{FOLDER}1"] = _ident(1, files=4, tracks=2)  # one disc's count only
        self.assertFalse(mb.prove_row(row)["proven"])

    def test_items_outside_folder_are_not_proven(self):
        row = self.w.add_album(1)
        self.w.album_items[1][1]["path"] = "Elsewhere/x.flac"
        self.assertEqual(mb.prove_row(row)["reasons"], ["items_outside_album_folder"])

    def test_album_with_release_id_set_but_rg_empty_is_left_alone(self):
        row = self.w.add_album(1)
        self.w.albums[1].mb_albumid = REL
        proof = mb.prove_row(row)
        self.assertFalse(proof["proven"])
        self.assertEqual(proof["reasons"], ["already_has_release_id"])

    def test_album_with_matching_rg_and_no_release_is_proven(self):
        row = self.w.add_album(1)
        self.w.albums[1].mb_releasegroupid = RG
        self.assertTrue(mb.prove_row(row)["proven"])

    def test_defect_existing_different_release_group_is_not_overwritten(self):
        """F1: canonical mb_releasegroupid already set to another group must go
        to review, never be replaced by the batch."""
        row = self.w.add_album(1)
        self.w.albums[1].mb_releasegroupid = OTHER_RG
        proof = mb.prove_row(row)
        self.assertFalse(proof["proven"], proof)
        self.assertEqual(proof["write"], {})

    def test_item_fingerprint_unanswered_is_not_proven(self):
        row = self.w.add_item(5)
        path = self.w.items[5].path
        self.w.fingerprints[path] = ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=None)
        proof = mb.prove_row(row)
        self.assertFalse(proof["proven"])
        self.assertIn("fingerprint_unavailable", proof["reasons"])

    def test_item_discogs_or_ai_only_proof_is_not_proven(self):
        for iid, proof_kind in ((6, "discogs_release"), (7, "ai_suggestion"), (8, "")):
            proof = mb.prove_row(self.w.add_item(iid, candidates=[_candidate(proof=proof_kind)]))
            self.assertFalse(proof["proven"], (proof_kind, proof))

    def test_singleton_check_rejects_album_member(self):
        row = self.w.add_item(5)
        self.w.items[5].album_id = 3
        self.assertEqual(mb.prove_row(row)["reasons"], ["not_a_singleton"])


class BatchEdgeTests(_BatchCase):
    def test_defect_release_group_set_after_preview_is_not_overwritten(self):
        """F1 (apply side): the digest ignores the album's current identity."""
        pre = self.preview([self.w.add_album(1)])
        self.w.albums[1].mb_releasegroupid = OTHER_RG
        writer = mock.Mock(side_effect=self.w.write_album)
        out = self.apply(pre["transaction_id"], album=writer)
        writer.assert_not_called()
        self.assertNotEqual(out["rows"][0]["status"], "written")

    def test_defect_duplicate_release_group_is_not_batch_proven(self):
        """F2: two library albums proven to the same Release Group (a duplicate)
        both pass the proof; the batch would stamp both without dedup review."""
        pre = self.preview([self.w.add_album(1), self.w.add_album(2)])
        self.assertLess(pre["counts"]["proven"], 2, pre["rows"])

    def test_concurrent_apply_writes_once(self):
        pre = self.preview([self.w.add_album(1)])
        gate = threading.Event()
        calls = []

        def slow_writer(aid, proof, log):
            calls.append(aid)
            gate.wait(2)
            return self.w.write_album(aid, proof, log)
        outs = []
        threads = [threading.Thread(target=lambda: outs.append(self.apply(pre["transaction_id"], album=slow_writer)))
                   for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(calls, [1])
        self.assertEqual(sorted(bool(o.get("ok")) for o in outs), [False, False, False, True])

    def test_raw_token_is_not_stored_and_persisted_job_result_is_redacted(self):
        import json
        from backend.provider_boundary import redact
        pre = self.preview([self.w.add_album(1)])
        raw = json.dumps(self.store.get(pre["transaction_id"]))
        self.assertNotIn(pre["confirm_token"], raw)
        self.assertNotIn(pre["confirm_token"], json.dumps(redact(pre)))
        self.assertEqual(mb.check_apply_request(pre["transaction_id"], pre["confirm_token"], store=self.store),
                         (None, 200))

    def test_token_of_another_preview_is_refused(self):
        a = self.preview([self.w.add_album(1)])
        b = self.preview([self.w.add_album(2)])
        err, status = mb.check_apply_request(a["transaction_id"], b["confirm_token"], store=self.store)
        self.assertEqual((err["code"], status), ("confirm_token_mismatch", 409))

    def test_snapshot_failure_writes_nothing(self):
        pre = self.preview([self.w.add_album(1)])
        writer = mock.Mock()
        with mock.patch("backend.missing_mb_batch.beets_adapter.get_stats", side_effect=RuntimeError("down")):
            out = self.apply(pre["transaction_id"], album=writer)
        writer.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(self.store.get(pre["transaction_id"])["status"], "Failed")

    def test_snapshot_is_recorded_before_first_write(self):
        pre = self.preview([self.w.add_album(1)])
        seen = {}

        def writer(aid, proof, log):
            seen["snap"] = (self.store.get(pre["transaction_id"])["metadata"] or {}).get("pre_apply_snapshot")
            return self.w.write_album(aid, proof, log)
        self.apply(pre["transaction_id"], album=writer)
        self.assertIsNotNone(seen["snap"])
        self.assertEqual(seen["snap"]["rows"]["album:1"]["mb_albumid"], "")

    def test_restart_while_running_is_recovery_required_and_not_replayed(self):
        from backend import transaction_recovery
        pre = self.preview([self.w.add_album(1)])
        tid = pre["transaction_id"]
        self.assertIsNotNone(self.store.transition(tid, "Preview", "Running"))  # claimed, process dies
        res = transaction_recovery.sweep(store=self.store, before=time.time() + 5)
        self.assertEqual(self.store.get(tid)["status"], "Recovery Required", res)
        writer = mock.Mock()
        out = self.apply(tid, album=writer)
        writer.assert_not_called()
        self.assertEqual(out["code"], "not_preview")
        self.assertEqual(mb.check_apply_request(tid, pre["confirm_token"], store=self.store)[1], 409)

    def test_job_recovery_required_maps_to_recovery_required(self):
        pre = self.preview([self.w.add_album(1)])
        tid = pre["transaction_id"]
        self.store.transition(tid, "Preview", "Running")
        job = mock.Mock(status="recovery_required", job_id="j1", log=[], result=None,
                        metadata={"type": "missing-mb-ids-batch-apply", "mutating": True, "transaction_id": tid})
        self.store.update_from_job(tid, job)
        self.assertEqual(self.store.get(tid)["status"], "Recovery Required")


class RouteEdgeTests(base.RouteTests):
    def test_preview_route_never_mutates(self):
        r = "routes_library.composite_workflows."
        spies = [self.patch(r + n, side_effect=AssertionError(n)) for n in (
            "update_album_metadata", "relocate_album", "plan_album_mb_track_repair",
            "apply_album_mb_track_repair", "rollback_album_metadata")]
        pre = self.preview([self.w.add_album(1)["id"], self.w.add_item(5)["id"]])
        self.assertEqual(pre["counts"]["proven"], 2)
        for s in spies:
            s.assert_not_called()
        tx = self.transactions.get(pre["transaction_id"])
        self.assertEqual((tx["status"], tx["dry_run"]), ("Preview", True))
        self.assertEqual((self.w.albums[1].mb_albumid, self.w.items[5].mb_trackid), ("", ""))

    def test_ui_path_token_from_jobs_api_applies(self):
        """The UI reads the token from GET /api/jobs/<id>; confirm_token is now a
        redacted key, so check the live response still carries it."""
        self.patch_match_writer()
        res = self.client.post("/api/library/missing-mb-ids/batch/preview", json={"ids": [self.w.add_album(1)["id"]]})
        job_id = res.get_json()["job_id"]
        self.wait(job_id)
        body = self.client.get(f"/api/jobs/{job_id}").get_json()
        result = body.get("result") or (body.get("job") or {}).get("result") or {}
        self.assertRegex(result.get("confirm_token") or "", r"^[0-9a-f]{32}$", body)
        job = self.wait(self.apply(result).get_json()["job_id"])
        self.assertEqual(job.status, "success", job.log)
        self.assertEqual(self.w.albums[1].mb_albumid, REL)

    def test_generic_transaction_apply_is_refused(self):
        pre = self.preview([self.w.add_album(1)["id"]])
        res = self.client.post(f"/api/transactions/{pre['transaction_id']}/apply", json={})
        self.assertEqual(res.status_code, 409, res.get_json())
        self.assertEqual(self.transactions.get(pre["transaction_id"])["status"], "Preview")

    def test_wrong_token_route_refused_and_nothing_written(self):
        self.patch_match_writer()
        pre = self.preview([self.w.add_album(1)["id"]])
        res = self.apply(pre, token="0" * 32)
        self.assertEqual((res.status_code, res.get_json()["code"]), (409, "confirm_token_mismatch"))
        self.assertEqual(self.w.albums[1].mb_albumid, "")

    def test_second_apply_while_running_is_refused(self):
        self.patch_match_writer()
        gate = threading.Event()
        orig = self.relocate.side_effect

        def slow_relocate(*a, **kw):
            gate.wait(5)
            return {"ok": True}
        self.relocate.side_effect = slow_relocate
        pre = self.preview([self.w.add_album(1)["id"]])
        first = self.apply(pre)
        self.assertEqual(first.status_code, 200, first.get_json())
        second = self.apply(pre)
        gate.set()
        self.assertEqual(second.status_code, 409, second.get_json())
        job = self.wait(first.get_json()["job_id"])
        self.assertEqual(job.status, "success", job.log)
        self.relocate.side_effect = orig

    def test_single_match_route_tag_write_failure_rolls_back_and_stops(self):
        """Regression of the extracted Match writer (new tag-write rollback)."""
        self.patch_match_writer()
        self.w.add_album(1)
        cw = "routes_library.composite_workflows."

        def update_album_metadata(aid, fields, **kw):
            if "mb_albumid" in fields:
                self.w.albums[aid].mb_albumid, self.w.albums[aid].mb_releasegroupid = fields["mb_albumid"], RG
                return {"ok": True, "operation_id": f"txn_meta_{aid}"}
            return {"ok": False, "error": "tag write refused"}
        self.patch(cw + "update_album_metadata", side_effect=update_album_metadata)
        self.patch("routes_library.lib.get_album", side_effect=self.w.albums.get)
        res = self.client.post("/api/albums/1/match", json={"mb_id": REL})
        self.assertEqual(res.status_code, 200, res.get_json())
        job = self.wait(res.get_json()["job_id"])
        self.assertEqual(job.status, "failed", job.log)
        self.rollback.assert_called_once_with("txn_meta_1")
        self.relocate.assert_not_called()
        self.assertEqual(self.w.albums[1].mb_albumid, "")

    def test_single_match_route_success_unchanged(self):
        self.patch_match_writer()
        self.w.add_album(1)
        res = self.client.post("/api/albums/1/match", json={"mb_id": REL})
        job = self.wait(res.get_json()["job_id"])
        self.assertEqual(job.status, "success", job.log)
        self.assertEqual(self.w.albums[1].mb_albumid, REL)
        self.relocate.assert_called_once()
        self.rollback.assert_not_called()


# The inherited RouteTests cases already run in test_missing_mb_batch.
for _name in [n for n in vars(base.RouteTests) if n.startswith("test_")]:
    setattr(RouteEdgeTests, _name, None)


if __name__ == "__main__":
    unittest.main()
