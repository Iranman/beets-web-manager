"""ARCH-030 Step 4 (library-transaction) regressions.

Two gaps Step 4 closes, each proven against the pre-Step-4 behaviour:

  (1) The matching contract's ``action_allowed=True`` is the ONLY authorization
      to AUTO-insert a staged recovery's files. ``start_folder_import_with_id``
      now refuses an unattended recovery auto-insert (marked by
      ``recovery_carrier_id``) unless the carrier's server-side
      ``matching_decision`` authorizes it -- without starting a job or touching
      the library -- and keeps the carrier in manual review with the contract's
      reason. A VERIFIED_SUBSET authorization routes the carrier's wanted subset
      into the existing-album merge, never a whole-release import. (Pre-Step-4
      the entrypoint ignored the gate and started the job regardless.)

  (2) Orphaned non-terminal staging folders under ``_beets_missing_import`` are
      ADOPTED (re-bound) when they belong to a live carrier, or CLEANED UP
      through the controlled staging-delete path when they belong to none --
      always inside the SEC-007/#325 containment model, never touching the live
      music library.

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

RGID = "37cb4a72-3333-4444-5555-666666666666"
RELEASE = "11111111-2222-3333-4444-555555555555"


def _symlinks_work() -> bool:
    d = tempfile.mkdtemp()
    try:
        tgt = os.path.join(d, "t")
        os.mkdir(tgt)
        os.symlink(tgt, os.path.join(d, "l"), target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


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


class AutoInsertGateTests(_CarrierStateMixin):
    """Gap (1): the entrypoint consumes action_allowed as the auto-insert gate."""

    def _staged_dir(self, name):
        d = Path(self.tmp) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "01 track.flac").write_bytes(b"\x00" * 16)
        return str(d)

    def _carrier(self, folder, *, action_allowed, state="review_required", reason="identity not verified"):
        cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1, "recording_id": "r1"}],
            expected_tracks=[{"recording_id": "r1", "track": 1, "disc": 1, "title": "Intro"}],
            staged_folder_path=folder, state=state)["carrier_id"]
        # Attach the contract decision server-side, exactly as the advancer does.
        rc.advance_state(cid, state, payload_extra={"matching_decision": {
            "action_allowed": action_allowed,
            "identity_proof": "deterministic_metadata_slot" if action_allowed else "release_group_id",
            "reason": reason}})
        return cid

    def _call(self, folder, cid, start_python_spy):
        payload = {"path": folder, "mb_albumid": RELEASE, "mb_releasegroupid": RGID,
                   "recovery_carrier_id": cid, "existing_album_id": 42,
                   "wanted_tracks": [{"track": 1, "recording_id": "r1"}]}
        with mock.patch.object(import_service, "_resolve_import_review_source_path",
                               return_value=(folder, None)), \
                mock.patch.object(import_service, "_preserve_torrent_source_path", return_value=False), \
                mock.patch.object(import_service.jobs, "start_python", start_python_spy):
            return import_service.start_folder_import_with_id(payload)

    def test_refused_when_action_not_allowed_no_job_no_insert(self):
        folder = self._staged_dir("hold")
        cid = self._carrier(folder, action_allowed=False, reason="no fingerprint evidence for these tracks")
        spy = mock.MagicMock()
        body, code = self._call(folder, cid, spy)
        self.assertEqual(code, 409, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body.get("code"), "recovery_action_not_allowed")
        self.assertIn("fingerprint", body["error"])
        # Pre-Step-4 the entrypoint ignored the gate and started the job here.
        spy.assert_not_called()
        self.assertEqual(rc.get_carrier(cid)["state"], "review_required")

    def test_missing_decision_fails_closed(self):
        # No matching_decision attached at all -> refuse (never a permissive default).
        folder = self._staged_dir("nodecision")
        cid = rc.create_carrier(
            existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 1}], staged_folder_path=folder,
            state="review_required")["carrier_id"]
        spy = mock.MagicMock()
        body, code = self._call(folder, cid, spy)
        self.assertEqual(code, 409, body)
        self.assertFalse(body["ok"])
        spy.assert_not_called()

    def test_allowed_when_action_allowed_routes_verified_subset(self):
        folder = self._staged_dir("pass")
        cid = self._carrier(folder, action_allowed=True)
        job = mock.MagicMock()
        job.job_id = "imp-1"
        spy = mock.MagicMock(return_value=job)
        body, code = self._call(folder, cid, spy)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["job_id"], "imp-1")
        spy.assert_called_once()
        meta = spy.call_args.kwargs["metadata"]
        # Routed as a subset merge into the existing album -- not a whole release.
        self.assertEqual(meta["existing_album_id"], 42)
        self.assertEqual(meta["missing_track_count"], 1)

    def test_carrier_not_bound_to_folder_is_refused(self):
        # recovery_carrier_id present but the carrier is bound to a DIFFERENT
        # folder: a client cannot aim an authorized carrier at another path.
        bound = self._staged_dir("bound")
        other = self._staged_dir("other")
        cid = self._carrier(bound, action_allowed=True)
        spy = mock.MagicMock()
        body, code = self._call(other, cid, spy)
        self.assertEqual(code, 409, body)
        self.assertFalse(body["ok"])
        spy.assert_not_called()


class OrphanStagingReconcileTests(_CarrierStateMixin):
    """Gap (2): adopt/clean orphaned staging folders inside containment."""

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

    def _folder(self, name, *, age_s=None, with_file=True):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        if with_file:
            (d / "01.flac").write_bytes(b"\x00" * 8)
        if age_s is not None:
            t = time.time() - age_s
            os.utime(str(d), (t, t))
        return str(d)

    def _removing_spy(self, calls):
        def _spy(path):
            calls.append(os.path.realpath(path))
            shutil.rmtree(path)
            return {"ok": True, "deleted": True, "path": path}
        return _spy

    def test_unbound_old_orphan_is_cleaned_via_controlled_path(self):
        orphan = self._folder("deadbeef-uuid-old-album", age_s=self.old)
        calls = []
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", side_effect=self._removing_spy(calls)):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        self.assertEqual(summary["removed"], 1, summary)
        self.assertEqual(calls, [os.path.realpath(orphan)])
        self.assertFalse(os.path.exists(orphan))

    def test_per_folder_lookup_failure_fails_closed_no_delete(self):
        # F8: a transient carrier-store error on the per-folder ownership lookup
        # must NOT be read as "unowned". An aged folder that could belong to a
        # LIVE carrier is skipped, never deleted, when the store read raises.
        orphan = self._folder("rec-livecarrier-old-album", age_s=self.old)
        spy = mock.MagicMock()
        log = []
        with mock.patch.object(rc, "load_for_folder", side_effect=RuntimeError("store down")), \
                mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=log)
        spy.assert_not_called()                 # fail-closed: no deletion attempted
        self.assertEqual(summary["removed"], 0, summary)
        self.assertGreaterEqual(summary["skipped"], 1, summary)
        self.assertTrue(os.path.exists(orphan))
        self.assertTrue(any("carrier-store unavailable: RuntimeError" in m for m in log), log)

    def test_prefix_store_read_failure_fails_closed_no_delete(self):
        # F8: if the live-carrier prefix read itself fails, ownership is UNKNOWN
        # for every folder this run (a folder could match an unread live-carrier
        # prefix), so an aged unbound folder is skipped, never deleted.
        orphan = self._folder("rec-wouldmatch-old-album", age_s=self.old)
        spy = mock.MagicMock()
        log = []
        with mock.patch.object(recovery_service, "_live_carrier_prefixes",
                               side_effect=RuntimeError("store down")), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=log)
        spy.assert_not_called()
        self.assertEqual(summary["removed"], 0, summary)
        self.assertGreaterEqual(summary["skipped"], 1, summary)
        self.assertTrue(os.path.exists(orphan))
        self.assertTrue(any("carrier-store unavailable: RuntimeError" in m for m in log), log)

    def test_bound_live_carrier_folder_is_kept(self):
        folder = self._folder("rec-bound-folder", age_s=self.old)
        rc.create_carrier(existing_album_id=42, mb_releasegroupid=RGID, release_id=RELEASE,
                          wanted_tracks=[{"track": 1}], staged_folder_path=folder,
                          state="review_required")
        spy = mock.MagicMock()
        with mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()
        self.assertGreaterEqual(summary["kept"], 1)
        self.assertTrue(os.path.exists(folder))

    def test_prefix_match_adopts_not_mints(self):
        rec = rc.create_carrier(
            existing_album_id=77, mb_releasegroupid=RGID, release_id=RELEASE,
            wanted_tracks=[{"track": 3, "recording_id": "r3"}],
            expected_tracks=[{"recording_id": "r3", "track": 3, "disc": 1, "title": "x"}],
            staged_folder_path="", state="downloaded")
        cid = rec["carrier_id"]
        key = rec["recovery_identity_key"]
        orphan = self._folder("rec-" + key[:16] + "-Some Album", age_s=self.old)
        self.assertIsNone(rc.load_for_folder(orphan))  # truly unbound at first
        spy = mock.MagicMock()
        with mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()  # adopted, never cleaned
        self.assertEqual(summary["adopted"], 1, summary)
        self.assertTrue(os.path.exists(orphan))
        adopted = rc.load_for_folder(orphan)
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted["carrier_id"], cid)

    def test_fresh_unbound_orphan_is_not_touched(self):
        fresh = self._folder("deadbeef-fresh", age_s=0)
        spy = mock.MagicMock()
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()
        self.assertTrue(os.path.exists(fresh))
        self.assertGreaterEqual(summary["skipped"], 1)

    def test_delete_is_only_ever_called_inside_the_staging_root(self):
        self._folder("deadbeef-a", age_s=self.old)
        self._folder("deadbeef-b", age_s=self.old)
        calls = []
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", side_effect=self._removing_spy(calls)):
            recovery_service.reconcile_orphan_staging_folders(log=[])
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(os.path.commonpath([c, self.root_real]), self.root_real)

    @unittest.skipUnless(_symlinks_work(), "symlinks unavailable on this host")
    def test_symlink_child_escaping_to_music_is_skipped_fail_closed(self):
        music = Path(self.tmp) / "music_library"
        music.mkdir(parents=True, exist_ok=True)
        (music / "real_album.flac").write_bytes(b"\x00" * 8)
        os.symlink(str(music), str(self.root / "deadbeef-evil"), target_is_directory=True)
        spy = mock.MagicMock()
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()  # a symlinked child is never handed to the delete path
        self.assertTrue((music / "real_album.flac").exists())  # live-library file untouched


class SymlinkedStagingRootTests(_CarrierStateMixin):
    """SEC-007/#325 Finding B: a symlinked ``_beets_missing_import`` ROOT must
    fail closed. Runs the real ``_staging_root`` (no mock) by driving
    ``validated_downloads_root`` at a temp downloads tree."""

    @unittest.skipUnless(_symlinks_work(), "symlinks unavailable on this host")
    def test_symlinked_root_to_downloads_cleans_nothing(self):
        import backend.app_runtime as app_runtime
        downloads = Path(self.tmp) / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        # A completed-but-not-yet-imported download sitting at the downloads root.
        survivor = downloads / "completed-album"
        survivor.mkdir()
        (survivor / "01.flac").write_bytes(b"\x00" * 8)
        t = time.time() - (recovery_service.ORPHAN_MIN_AGE_S + 3600)
        os.utime(str(survivor), (t, t))
        # _beets_missing_import is a symlink redirecting the staging root at the
        # whole downloads root (the escape realpath would have followed).
        link = downloads / "_beets_missing_import"
        os.symlink(str(downloads), str(link), target_is_directory=True)

        spy = mock.MagicMock()
        with mock.patch.object(app_runtime, "validated_downloads_root", return_value=downloads), \
                mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", spy):
            # Real _staging_root must refuse the symlinked root outright...
            self.assertEqual(recovery_service._staging_root(), "")
            summary = recovery_service.reconcile_orphan_staging_folders(log=[])
        spy.assert_not_called()                     # ...so no delete is ever attempted
        self.assertEqual(summary["removed"], 0, summary)
        self.assertEqual(summary["scanned"], 0, summary)
        self.assertTrue(survivor.exists())          # the unrelated download survives

    def test_real_dir_root_still_resolves(self):
        # Sanity: a genuine (non-symlink) staging dir still resolves normally.
        import backend.app_runtime as app_runtime
        downloads = Path(self.tmp) / "downloads2"
        (downloads / "_beets_missing_import").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(app_runtime, "validated_downloads_root", return_value=downloads):
            root = recovery_service._staging_root()
        self.assertEqual(root, os.path.realpath(str(downloads / "_beets_missing_import")))


if __name__ == "__main__":
    unittest.main()
