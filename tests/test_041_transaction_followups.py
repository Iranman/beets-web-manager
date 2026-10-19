"""0.4.1 transaction and job-safety follow-ups: #317 (a parent waiting on a
mutating child job reconciles with the child's real outcome), #328 F9
(rollback log folders), #266 item 3 (unknown-family Running records),
#351 N4 (identity-safe no-replace rename), #325 F3/F5 (rejection copy, job
classification) and #356 item 1 (cancel.allowed on the transaction detail)."""

import os
import threading
import time
import types
import unittest
from unittest import mock

import backend.composite_workflows as cw
from tests.test_album_relocation_transaction import _Engine
from tests.test_staging_mutation_hardening import _RouteEnv as _RouteEnvBase
from tests.test_wave0_s1_containment import _Env


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not pred():
        time.sleep(0.01)
    return pred()


class ChildJobReconcileTests(_RouteEnvBase):
    """#317: cancel or timeout of a parent waiting on a mutating child job
    waits for the child's real outcome; the parent's audit says what happened."""

    def setUp(self):
        super().setUp()
        import backend.job_service as js
        import backend.transaction_service as ts
        from job_engine import JobStore
        self.js = js
        self.jobs = JobStore(self.data / "jobs")
        fast = types.SimpleNamespace(time=time.time, sleep=lambda _s: time.sleep(0.01))
        for p in (mock.patch.object(ts, "jobs", self.jobs), mock.patch.object(ts, "transactions", self.store),
                  mock.patch.object(js, "jobs", self.jobs), mock.patch.object(js, "time", fast),
                  mock.patch.object(js, "CHILD_SETTLE_SECONDS", 5, create=True)):
            p.start()
            self.addCleanup(p.stop)
        ts._install_transaction_job_hooks()
        self.addCleanup(self.jobs.close)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _child(self, body, job_type="reimport-disk"):
        return self.jobs.start_python(body, label="child", metadata={"transaction": False, "type": job_type})

    def _parent(self, child, *, cancel=True, timeout=60):
        seen = {}
        started = threading.Event()

        def parent(log, cancel_event, update_state=None):
            started.set()
            try:
                return self.js._wait_for_child_job(child.job_id, log, cancel_event, prefix="import", timeout=timeout)
            finally:
                seen["child_status"] = child.status

        job = self.jobs.start_python(parent, label="Parent",
                                     metadata={"transaction": {"operation_type": "Library Cleanup"}})
        self.assertTrue(started.wait(5))
        if cancel:
            job.kill()
        # #384: wait for the parent job, not the transaction status: the
        # wrapper writes the status before its last log line.
        self.assertTrue(_wait(lambda: job.status != "running"))
        tx_id = job.metadata["transaction_id"]
        self.assertNotEqual(self.store.get(tx_id)["status"], "Running")
        return job, self.store.get(tx_id), seen

    def test_child_that_finishes_its_work_after_the_cancel_is_not_audited_as_cancelled(self):
        def child_body(log, cancel):
            while not cancel.requested:  # a Beets operation that does not stop
                time.sleep(0.01)
            time.sleep(0.05)
            log.append("Updated 3 tracks")
            return {"ok": True}
        child = self._child(child_body)
        job, tx, seen = self._parent(child)
        self.assertEqual(seen["child_status"], "success")  # waited for the real outcome
        self.assertEqual(tx["status"], "Failed")
        self.assertTrue(tx["metadata"]["engine_result"]["partial_cancel"])
        self.assertIn("may have changed the library", " ".join(job.log))
        self.assertIn("partially applied", " ".join(map(str, tx["logs"])))

    def test_child_that_stops_before_changing_anything_stays_cancelled(self):
        def child_body(log, cancel):
            while not cancel.is_set():
                time.sleep(0.01)
            raise RuntimeError("cancelled")
        child = self._child(child_body)
        _job, tx, seen = self._parent(child)
        self.assertEqual(seen["child_status"], "cancelled")
        self.assertEqual(tx["status"], "Cancelled")

    def test_child_still_running_after_the_wait_is_recovery_required(self):
        self.js.CHILD_SETTLE_SECONDS = 0.2
        child = self._child(lambda log, cancel: self.release.wait(30))
        job, tx, seen = self._parent(child)
        self.assertEqual(seen["child_status"], "running")
        self.assertEqual(tx["status"], "Recovery Required")
        self.assertTrue(tx["metadata"]["engine_result"]["outcome_unknown"])
        self.assertIn("still running", " ".join(job.log))

    def test_timed_out_child_that_then_finishes_returns_its_result(self):
        def child_body(log, cancel):
            while not cancel.requested:
                time.sleep(0.01)
            return {"ok": True, "imported": 2}
        child = self._child(child_body)
        job, tx, _seen = self._parent(child, cancel=False, timeout=0.1)
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(job.result, {"ok": True, "imported": 2})

    def test_read_only_child_is_still_abandoned_at_once(self):
        child = self._child(lambda log, cancel: self.release.wait(30), job_type="dedup-scan")
        _job, tx, seen = self._parent(child)
        self.assertEqual(seen["child_status"], "running")
        self.assertEqual(tx["status"], "Cancelled")


class TransactionCancelEligibilityTests(_RouteEnvBase):
    """#356 item 1: the detail payload's cancel field and the cancel route
    share one rule."""

    def _tx(self, status, **meta):
        return self.store.create(operation_type="MusicBrainz Match", status=status, metadata=meta)["id"]

    def _cancel_field(self, tx):
        resp = self.client.get(f"/api/transactions/{tx}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["transaction"]["cancel"]

    def test_detail_reports_cancel_eligibility(self):
        sync = dict(mutation_family=cw.MBSYNC_LIBRARY_FAMILY, engine_operation_id="op-1")
        cases = {
            self._tx("Preview"): (True, "transaction"),
            self._tx("Approved"): (True, "transaction"),
            self._tx("Running", **sync): (True, "engine"),
            self._tx("Recovery Required", **sync): (True, "engine"),
            self._tx("Completed", **sync): (False, None),
            self._tx("Running"): (False, None),
            self._tx("Running", mutation_family=cw.MBSYNC_LIBRARY_FAMILY): (False, None),
        }
        for tx, (allowed, mode) in cases.items():
            field = self._cancel_field(tx)
            self.assertEqual((field["allowed"], field["mode"]), (allowed, mode), self.store.get(tx))
            self.assertTrue(field["reason"])

    def test_route_refusal_uses_the_same_reason(self):
        tx = self._tx("Completed")
        reason = self._cancel_field(tx)["reason"]
        resp = self.client.post(f"/api/transactions/{tx}/cancel")
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (409, reason))

    def test_engine_mode_sends_the_cancel_to_beets(self):
        tx = self._tx("Running", mutation_family=cw.MBSYNC_LIBRARY_FAMILY, engine_operation_id="op-7")
        ad = mock.Mock()
        with mock.patch.object(cw, "beets_adapter", ad):
            resp = self.client.post(f"/api/transactions/{tx}/cancel")
        self.assertEqual(resp.status_code, 202)
        ad.cancel_mbsync_library.assert_called_once_with("op-7")


class UnknownFamilyRecoveryTests(_RouteEnvBase):
    """#266 item 3: a Running record of a family recovery does not know gets a
    truthful terminal state instead of staying Running forever."""

    def test_unknown_family_running_record_becomes_recovery_required(self):
        from backend import transaction_recovery
        legacy = self.store.create(operation_type="Repair", status="Running",
                                   metadata={"mutation_family": "album_mb_track_repair_v1"})["id"]
        plain = self.store.create(operation_type="Repair", status="Running", metadata={})["id"]
        res = transaction_recovery.sweep(adapter=mock.Mock(), store=self.store, before=time.time() + 1)
        self.assertEqual({r["operation_id"]: r["action"] for r in res}, {legacy: "Recovery Required"})
        self.assertIn("album_mb_track_repair_v1", " ".join(self.store.get(legacy)["logs"]))
        self.assertEqual(self.store.get(plain)["status"], "Running")  # a job wrapper follows its job

    def test_a_record_this_process_is_running_is_left_alone(self):
        from backend import transaction_recovery
        live = self.store.create(operation_type="Repair", status="Running",
                                 metadata={"mutation_family": "album_mb_track_repair_v1"})["id"]
        transaction_recovery.sweep(adapter=mock.Mock(), store=self.store, before=time.time() - 60)
        self.assertEqual(self.store.get(live)["status"], "Running")


class JobClassificationTests(unittest.TestCase):
    """#325 F5: whole words, not substrings."""

    def test_labels(self):
        from backend.transaction_service import _transaction_operation_from_job as op
        self.assertEqual(op("Import: Torrent Artist - Torrent Album", {}), "Import")
        self.assertEqual(op("Artist folder rename", {}), "Rename")
        self.assertEqual(op("Fetch missing art", {}), "Artwork Update")
        self.assertEqual(op("", {"type": "album-art-rebuild"}), "Artwork Update")
        self.assertEqual(op("", {"type": "fetchart"}), "Artwork Update")
        self.assertEqual(op("Metadata repair", {}), "Repair")
        self.assertEqual(op("", {"type": "batch-ai-suggest"}), "AI Suggestion")


class RejectionCopyTests(unittest.TestCase):
    """#325 F3: the message says what happened to each file and why."""

    def test_all_kept_does_not_claim_handling(self):
        from backend.ai_service import _music_format_policy_rejection_error
        from backend.ai_batch_state_service import _is_music_format_policy_handled_error
        reason = "file is a preserved torrent source that may still be seeding"
        msg = _music_format_policy_rejection_error(2, [{"handling": "kept", "reason": reason}] * 2,
                                                   {"rejected_download_handling": "quarantine"})
        self.assertNotIn("handled according to settings", msg)
        self.assertNotIn("were quarantined", msg)
        self.assertIn(f"2 rejected file(s) left in place ({reason})", msg)
        self.assertTrue(_is_music_format_policy_handled_error(msg))

    def test_mixed_names_the_reason(self):
        from backend.ai_service import _music_format_policy_rejection_error
        msg = _music_format_policy_rejection_error(
            2, [{"handling": "kept", "reason": "file is in the music library"},
                {"handling": "quarantine", "removed": True}], {"rejected_download_handling": "quarantine"})
        self.assertIn("1/2 rejected file(s) were quarantined; 1 left in place (file is in the music library)", msg)

    def test_old_saved_messages_are_still_recognised(self):
        from backend.ai_batch_state_service import _is_music_format_policy_handled_error
        old = ("Downloaded audio does not match Music Format Preferences. Rejected files were handled according "
               "to settings. 1 rejected file was quarantined. Import stopped; choose another source.")
        self.assertTrue(_is_music_format_policy_handled_error(old))


@unittest.skipUnless(os.name == "posix", "fd-relative staging moves are POSIX-only")
class RenameNoReplaceIdentityTests(_Env):
    """#351 N4: the source is re-verified right before it is renamed or
    unlinked by name; a swapped source is never moved or deleted."""

    def _swap(self, path):
        os.rename(path, str(path) + ".orig")
        if str(path).endswith(".flac"):
            path.write_bytes(b"planted")
        else:
            path.mkdir()
            (path / "new.flac").write_bytes(b"n")

    def test_directory_swapped_before_rename_is_refused(self):
        src = self.dl / "src"
        src.mkdir()
        (src / "a.flac").write_bytes(b"a")
        p_src = cw._validated_staging_target(src, "move")
        p_dst = cw._validated_staging_target(self.dl / "dst", "move to")
        real = cw._rename_noreplace

        def swap_then_rename(*a, **kw):  # the swap lands after _move_resolved's own check
            self._swap(src)
            return real(*a, **kw)

        with mock.patch.object(cw, "_rename_noreplace", side_effect=swap_then_rename):
            with self.assertRaises(ValueError):
                cw._move_resolved(p_src, p_dst)
        self.assertTrue((src / "new.flac").exists())
        self.assertTrue((self.dl / "src.orig" / "a.flac").exists())
        self.assertFalse((self.dl / "dst").exists())

    def test_file_swapped_before_unlink_is_kept(self):
        src = self.dl / "a.flac"
        src.write_bytes(b"original")
        p_src = cw._validated_staging_target(src, "move")
        p_dst = cw._validated_staging_target(self.dl / "b.flac", "move to")
        real_link = os.link

        def link_then_swap(*a, **kw):
            real_link(*a, **kw)
            self._swap(src)

        with mock.patch.object(cw.os, "link", side_effect=link_then_swap):
            with self.assertRaises(ValueError):
                cw._move_resolved(p_src, p_dst)
        self.assertEqual(src.read_bytes(), b"planted")  # the swapped-in file was not unlinked
        self.assertEqual((self.dl / "a.flac.orig").read_bytes(), b"original")
        self.assertFalse((self.dl / "b.flac").exists())

    def test_unswapped_moves_still_work(self):
        (self.dl / "d").mkdir()
        (self.dl / "f.flac").write_bytes(b"f")
        cw.move_staging_file(str(self.dl / "d"), str(self.dl / "x" / "d"))
        cw.move_staging_file(str(self.dl / "f.flac"), str(self.dl / "x" / "f.flac"))
        self.assertTrue((self.dl / "x" / "d").is_dir())
        self.assertEqual((self.dl / "x" / "f.flac").read_bytes(), b"f")
        self.assertFalse((self.dl / "f.flac").exists())


class RollbackFolderLogTests(_Engine):
    """#328 F9: the rollback log names the folders as absolute paths, also for
    a plan recorded with library-relative folders (before 0.4.0's adapter fix)."""

    def test_rollback_log_shows_absolute_folders(self):
        album = self.make_album(os.path.join(self.music, "incoming", "x"))
        res = cw.relocate_album(album.id, adapter=self.ad, store=self.store)
        op = res["operation_id"]
        before = self.store.get(op)["metadata"]["before"]
        self.store.update(op, metadata={"before": {**before, "folders": ["incoming/x"]}})
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        line = next(str(l) for l in self.store.get(op)["logs"] if "back to" in str(l))
        self.assertIn(os.path.join(self.music, "incoming", "x"), line)
        self.assertNotIn(" incoming/x", line)


if __name__ == "__main__":
    unittest.main()
