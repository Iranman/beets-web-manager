"""ARCH-030 reconcile / auto-insert authorization hardening regressions.

Found by independent security + QA review of the recovery redesign. Each test
proves one gap closed, against the pre-fix behaviour:

  F6/C4 (BLOCKING): ``reconcile_orphan_staging_folders`` deleted an aged,
      carrier-unowned folder under ``_beets_missing_import`` without the #427
      pending-review check -- but ordinary Review imports stage there too, so a
      Review-queued staged import with no carrier binding could be swept after
      1h (losing a not-yet-imported download, dangling the Review row). The
      sweep now applies the SAME check as
      ``import_service._delete_staged_import_folder`` and fails closed on an
      unreadable queue.

  C3: an authorized recovery auto-insert must be bound to the CARRIER's own
      canonical identity -- a crafted payload with a valid authorized carrier +
      its folder but a different release group / existing album cannot redirect
      the insert.

  C7: the auto-insert refusal transition back to ``review_required`` is a CAS on
      a fresh read, so a concurrent claim is never clobbered.

  F7sec/C11: the live-carrier ownership read no longer fails OPEN past a fixed
      cap -- a folder owned by a carrier beyond the single-pass scan limit is
      skipped (ownership UNKNOWN -> fail closed), never swept.

Isolated fixtures only (a per-test carrier state dir + temp staged/staging
folders); no production music or library is touched.
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.recovery_carrier as rc
import backend.recovery_service as recovery_service
import backend.import_service as import_service
import backend.pending_review_store as pending_review_store

RGID = "37cb4a72-3333-4444-5555-666666666666"
RELEASE = "11111111-2222-3333-4444-555555555555"
OTHER_RGID = "99999999-aaaa-bbbb-cccc-dddddddddddd"
OTHER_RELEASE = "88888888-7777-6666-5555-444444444444"


class _CarrierStateMixin(unittest.TestCase):
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


class ReconcilePendingReviewTests(_CarrierStateMixin):
    """F6/C4: a Review-queued staged folder is never swept (fail closed)."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp) / "_beets_missing_import"
        self.root.mkdir(parents=True, exist_ok=True)
        self.root_real = os.path.realpath(str(self.root))
        self._root_patch = mock.patch.object(
            recovery_service, "_staging_root", return_value=self.root_real)
        self._root_patch.start()
        self.old = recovery_service.ORPHAN_MIN_AGE_S + 3600

    def tearDown(self):
        self._root_patch.stop()
        super().tearDown()

    def _folder(self, name, *, age_s):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "01.flac").write_bytes(b"\x00" * 8)
        t = time.time() - age_s
        os.utime(str(d), (t, t))
        return str(d)

    def test_review_queued_folder_is_never_swept(self):
        # Aged + carrier-unowned (so it reaches the cleanup gate) but queued for
        # human Review: pre-fix it was deleted after 1h; now it is kept, and the
        # #427 check is invoked with the fail-closed unknown=True flag.
        orphan = self._folder("deadbeef-review-pending", age_s=self.old)
        seen = {}

        def fake_pending(path, *, unknown=False):
            seen["path"] = path
            seen["unknown"] = unknown
            return True

        spy = mock.MagicMock()
        # ARCH-030 reconcile-overlap: the sweep now uses the ancestor/descendant
        # -aware check (a Review row that is a child/parent of a scanned
        # top-level orphan is also protected), still with the fail-closed
        # unknown=True flag.
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch.object(pending_review_store, "_pending_review_has_path_or_overlap",
                                  side_effect=fake_pending), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()                                   # never swept
        self.assertTrue(os.path.exists(orphan))
        self.assertGreaterEqual(summary["kept"], 1, summary)
        self.assertEqual(summary["removed"], 0, summary)
        self.assertIs(seen.get("unknown"), True)                  # fail-closed flag
        self.assertEqual(os.path.realpath(seen["path"]), os.path.realpath(orphan))

    def test_unreadable_review_queue_prevents_deletion(self):
        # F6 fail-closed wiring: the REAL _pending_review_has_path returns its
        # unknown=True default when the queue cannot be read, so an aged unowned
        # folder is kept, never deleted, while the queue is unreadable.
        orphan = self._folder("deadbeef-queue-unreadable", age_s=self.old)
        spy = mock.MagicMock()
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch.object(pending_review_store, "_load_pending_reviews",
                                  side_effect=pending_review_store.PendingReviewLoadError("boom")), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()
        self.assertTrue(os.path.exists(orphan))
        self.assertGreaterEqual(summary["kept"], 1, summary)
        self.assertEqual(summary["removed"], 0, summary)

    def test_unowned_not_in_review_still_cleaned(self):
        # Control: the pending-review guard does NOT block a genuine orphan that
        # no Review row points at (the real queue read returns []).
        orphan = self._folder("deadbeef-true-orphan", age_s=self.old)
        calls = []

        def _rm(path):
            calls.append(os.path.realpath(path))
            shutil.rmtree(path)
            return {"ok": True, "deleted": True, "path": path}

        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", side_effect=_rm):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        self.assertEqual(summary["removed"], 1, summary)
        self.assertEqual(calls, [os.path.realpath(orphan)])
        self.assertFalse(os.path.exists(orphan))


class LiveCarrierCapFailClosedTests(_CarrierStateMixin):
    """F7sec/C11: no fail-open past the single-pass live-carrier scan cap."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp) / "_beets_missing_import"
        self.root.mkdir(parents=True, exist_ok=True)
        self.root_real = os.path.realpath(str(self.root))
        self._root_patch = mock.patch.object(
            recovery_service, "_staging_root", return_value=self.root_real)
        self._root_patch.start()
        self.old = recovery_service.ORPHAN_MIN_AGE_S + 3600

    def tearDown(self):
        self._root_patch.stop()
        super().tearDown()

    def test_folder_owned_by_carrier_beyond_cap_is_not_swept(self):
        # A live carrier owns the folder (its rec-<hex> prefix would ADOPT it,
        # per test_prefix_match_adopts). With the single-pass scan cap hit, the
        # live-carrier read is TRUNCATED -> ownership UNKNOWN for the run -> the
        # sweep fails closed and the owned folder is skipped, never deleted.
        # Pre-fix the omitted carrier's folder was treated as unowned and swept.
        owner = rc.create_carrier(
            existing_album_id=77, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 3, "recording_id": "r3"}],
            expected_tracks=[{"recording_id": "r3", "track": 3, "disc": 1, "title": "x"}],
            staged_folder_path="", state="downloaded")
        key = owner["recovery_identity_key"]
        orphan = self.root / ("rec-" + key[:16] + "-Some Album")
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "01.flac").write_bytes(b"\x00" * 8)
        t = time.time() - self.old
        os.utime(str(orphan), (t, t))
        self.assertIsNone(rc.load_for_folder(str(orphan)))  # unbound folder

        spy = mock.MagicMock()
        with mock.patch.object(recovery_service, "_LIVE_CARRIER_SCAN_LIMIT", 1), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()
        self.assertTrue(orphan.exists())
        self.assertEqual(summary["removed"], 0, summary)
        self.assertEqual(summary["adopted"], 0, summary)
        self.assertGreaterEqual(summary["skipped"], 1, summary)

    def test_live_carrier_prefixes_raises_on_truncation(self):
        # Unit: a live set at/over the cap raises (caller reads that as UNKNOWN).
        rc.create_carrier(existing_album_id=1, mb_releasegroupid=RGID,
                          release_id=RELEASE, wanted_tracks=[{"track": 1}],
                          staged_folder_path="", state="downloaded")
        with self.assertRaises(RuntimeError):
            recovery_service._live_carrier_prefixes(limit=1)

    def test_kept_and_adopted_do_not_consume_the_delete_cap(self):
        # C11: the cap bounds DELETES, not entries examined. A run of kept/
        # adopted folders ahead of an orphan must not starve the orphan (the old
        # top-of-loop `scanned >= cap` break did exactly that).
        rec = rc.create_carrier(
            existing_album_id=5, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "a"}],
            staged_folder_path="", state="downloaded")
        key = rec["recovery_identity_key"]
        # Entries are scanned in sorted-name order. "rec-..." sorts before
        # "zzzz-...", so the adopt candidate is examined first; it must not
        # consume the single delete the cap allows.
        adopt = self.root / ("rec-" + key[:16] + "-Album")
        adopt.mkdir(parents=True, exist_ok=True)
        (adopt / "01.flac").write_bytes(b"\x00" * 8)
        orphan = self.root / "zzzz-orphan-old"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "01.flac").write_bytes(b"\x00" * 8)
        t = time.time() - self.old
        for d in (adopt, orphan):
            os.utime(str(d), (t, t))
        calls = []

        def _rm(path):
            calls.append(os.path.realpath(path))
            shutil.rmtree(path)
            return {"ok": True, "deleted": True, "path": path}

        # cap=1: one delete is allowed. The adopted folder must NOT consume it,
        # so the single orphan behind it is still deleted this run.
        with mock.patch("backend.composite_workflows.delete_file", side_effect=_rm):
            summary = recovery_service.reconcile_orphan_staging_folders(limit=1, log=[])
        self.assertEqual(summary["adopted"], 1, summary)
        self.assertEqual(summary["removed"], 1, summary)
        self.assertEqual(calls, [os.path.realpath(str(orphan))])


class AutoInsertTargetBindingTests(_CarrierStateMixin):
    """C3 + C7: auto-insert is bound to the carrier identity; refusal is CAS."""

    def _staged_dir(self, name):
        d = Path(self.tmp) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "01 track.flac").write_bytes(b"\x00" * 16)
        return str(d)

    def _carrier(self, folder, *, action_allowed, state="review_required",
                 reason="identity not verified", mb_releasegroupid=RGID,
                 release_id=RELEASE, existing_album_id=42):
        cid = rc.create_carrier(
            existing_album_id=existing_album_id, mb_releasegroupid=mb_releasegroupid,
            release_id=release_id, wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=folder, state=state)["carrier_id"]
        rc.advance_state(cid, state, payload_extra={"matching_decision": {
            "action_allowed": action_allowed,
            "identity_proof": "deterministic_metadata_slot" if action_allowed else "release_group_id",
            "reason": reason}})
        return cid

    def _call(self, payload, spy):
        folder = payload["path"]
        with mock.patch.object(import_service, "_resolve_import_review_source_path",
                               return_value=(folder, None)), \
                mock.patch.object(import_service, "_preserve_torrent_source_path", return_value=False), \
                mock.patch.object(import_service.jobs, "start_python", spy):
            return import_service.start_folder_import_with_id(payload)

    def test_divergent_release_group_is_refused(self):
        # Authorized carrier (RGID) + its folder, but the payload targets a
        # DIFFERENT release group -> the authorized insert is refused, no job.
        folder = self._staged_dir("diverge-rg")
        cid = self._carrier(folder, action_allowed=True)
        spy = mock.MagicMock()
        payload = {"path": folder, "mb_albumid": OTHER_RELEASE,
                   "mb_releasegroupid": OTHER_RGID, "recovery_carrier_id": cid,
                   "existing_album_id": 42,
                   "wanted_tracks": [{"track": 1, "recording_id": "r1"}]}
        body, code = self._call(payload, spy)
        self.assertEqual(code, 409, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body.get("code"), "recovery_target_mismatch")
        spy.assert_not_called()

    def test_divergent_existing_album_is_refused(self):
        # Same RG, but a different existing album than the carrier's -> refused.
        folder = self._staged_dir("diverge-album")
        cid = self._carrier(folder, action_allowed=True)
        spy = mock.MagicMock()
        payload = {"path": folder, "mb_albumid": RELEASE, "mb_releasegroupid": RGID,
                   "recovery_carrier_id": cid, "existing_album_id": 999,
                   "wanted_tracks": [{"track": 1, "recording_id": "r1"}]}
        body, code = self._call(payload, spy)
        self.assertEqual(code, 409, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body.get("code"), "recovery_target_mismatch")
        spy.assert_not_called()

    def test_matching_target_is_allowed_and_bound_to_carrier(self):
        # Control: a payload whose target matches the carrier is authorized and
        # routed as the carrier's subset merge into its existing album.
        folder = self._staged_dir("match")
        cid = self._carrier(folder, action_allowed=True)
        job = mock.MagicMock()
        job.job_id = "imp-1"
        spy = mock.MagicMock(return_value=job)
        payload = {"path": folder, "mb_albumid": RELEASE, "mb_releasegroupid": RGID,
                   "recovery_carrier_id": cid, "existing_album_id": 42,
                   "wanted_tracks": [{"track": 1, "recording_id": "r1"}]}
        body, code = self._call(payload, spy)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        spy.assert_called_once()
        meta = spy.call_args.kwargs["metadata"]
        self.assertEqual(meta["existing_album_id"], 42)
        self.assertEqual(meta["missing_track_count"], 1)

    def test_refusal_transition_is_cas_and_skips_on_concurrent_claim(self):
        # C7: the gate re-reads the carrier before its refusal transition.
        # Simulate a concurrent claim landing in that window -- the real store is
        # now 'importing' at a newer revision while the gate's re-read still sees
        # the stale 'downloaded' revision. The CAS must LOSE to the racer and
        # leave the claim intact, not clobber it back to review_required.
        folder = self._staged_dir("race")
        cid = self._carrier(folder, action_allowed=False, state="downloaded",
                            reason="no fingerprint evidence")
        stale = rc.get_carrier(cid)                 # stale snapshot (downloaded)
        rc.advance_state(cid, "importing")          # racer claims it (newer rev)
        with mock.patch.object(rc, "get_carrier", return_value=stale):
            gate = import_service._recovery_auto_insert_authorized(folder, cid)
        self.assertFalse(gate["ok"])
        # The racing claim is preserved; the blind fallback did not win.
        self.assertEqual(rc.get_carrier(cid)["state"], "importing")

    def test_refusal_without_race_records_review_required(self):
        # Control: with no concurrent claim, a non-allowed carrier in an
        # advanceable non-review state IS moved to review_required (CAS succeeds).
        folder = self._staged_dir("norace")
        cid = self._carrier(folder, action_allowed=False, state="downloaded",
                            reason="identity not verified")
        gate = import_service._recovery_auto_insert_authorized(folder, cid)
        self.assertFalse(gate["ok"])
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")


if __name__ == "__main__":
    unittest.main()
