"""SEC-002 / ARCH-003 Wave 27 result-handling regressions."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

from backend import transaction_engine as txn
from tests.test_import_review_attach_enforcement import APP, _APP_IMPORT_ERROR


VALID_RELEASE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
VALID_RGID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
VALID_ARTIST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
VALID_TRACK_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


class InlineJobs:
    def __init__(self):
        self.logs = []
        self.result = None
        self.error = None
        self.status = None

    def start_python(self, fn, label="", metadata=None):
        self.logs = []
        self.result = None
        self.error = None
        try:
            self.result = fn(self.logs, cancel_event=SimpleNamespace(is_set=lambda: False))
            self.status = "success"
        except Exception as exc:
            self.error = exc
            self.status = "failed"
        return SimpleNamespace(job_id="inline-wave27", status=self.status, logs=self.logs, result=self.result)


@contextlib.contextmanager
def sqlite_app_db(path: Path, *, row_factory=None, text_factory=None):
    con = sqlite3.connect(path)
    if row_factory is not None:
        con.row_factory = row_factory
    if text_factory is not None:
        con.text_factory = text_factory
    try:
        yield con
    finally:
        con.close()


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class StrictAttachStageResultTests(unittest.TestCase):
    def test_dict_success(self):
        APP._require_attach_stage_success({"ok": True}, "stage")

    def test_dict_failure(self):
        with self.assertRaisesRegex(RuntimeError, "stage failed"):
            APP._require_attach_stage_success({"ok": False, "error": "denied"}, "stage")

    def test_empty_dict_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            APP._require_attach_stage_success({}, "stage")

    def test_process_success(self):
        APP._require_attach_stage_success(SimpleNamespace(returncode=0), "stage")

    def test_process_failure(self):
        with self.assertRaisesRegex(RuntimeError, "stage failed"):
            APP._require_attach_stage_success(SimpleNamespace(returncode=2), "stage")

    def test_process_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            APP._require_attach_stage_success(SimpleNamespace(returncode=124), "stage")

    def test_cancellation(self):
        with self.assertRaises(APP.AttachRecordingCancelled):
            APP._require_attach_stage_success(SimpleNamespace(returncode=-9), "stage")

    def test_unknown_shape_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            APP._require_attach_stage_success(object(), "stage")

    def test_exception_from_ipc_call_propagates_to_rollback_failure(self):
        logs = []
        with mock.patch.object(APP.beets_client, "update_item_metadata", side_effect=RuntimeError("ipc down")):
            ok = APP._run_item_metadata_restore(10, {"title": "old"}, logs)
        self.assertFalse(ok)
        self.assertTrue(any("Metadata restore failed" in line for line in logs))


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class AlbumAddMbidsRequiredStageTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        self.inline = InlineJobs()
        self.patches = [
            mock.patch.object(APP.jobs, "start_python", side_effect=self.inline.start_python),
            mock.patch.object(APP, "_invalidate_lib_cache"),
            mock.patch.object(APP, "_trigger_plex_refresh"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self):
        return self.client.post(f"/api/albums/123/add-mbids", json={
            "mb_albumartistid": VALID_ARTIST_ID,
            "mb_releasegroupid": VALID_RGID,
            "mb_albumid": VALID_RELEASE_ID,
        })

    def test_metadata_failure_stops_before_relocation_and_success_side_effects(self):
        with mock.patch.object(APP.beets_client, "update_album_metadata", return_value={"ok": False, "error": "metadata plan failed"}) as meta, \
             mock.patch.object(APP.beets_client, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        meta.assert_called_once()
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()
        APP._trigger_plex_refresh.assert_not_called()

    def test_relocation_failure_prevents_completed_log_and_plex_refresh(self):
        with mock.patch.object(APP.beets_client, "update_album_metadata", return_value={"ok": True}) as meta, \
             mock.patch.object(APP.beets_client, "relocate_album", return_value={"ok": False, "error": "relocation apply failed"}) as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        meta.assert_called_once()
        relocate.assert_called_once_with(123)
        self.assertFalse(any("MBIDs applied" in line for line in self.inline.logs))
        APP._invalidate_lib_cache.assert_not_called()
        APP._trigger_plex_refresh.assert_not_called()


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class MatchAlbumRequiredStageTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        self.inline = InlineJobs()
        self.patches = [
            mock.patch.object(APP.jobs, "start_python", side_effect=self.inline.start_python),
            mock.patch.object(APP.lib, "get_album", return_value=SimpleNamespace(albumartist="Artist", album="Album")),
            mock.patch.object(APP, "_resolve_mb_release_id", return_value=VALID_RELEASE_ID),
            mock.patch.object(APP, "_album_mb_match_plan", return_value={
                "matched_count": 1,
                "actual_count": 1,
                "expected_count": 1,
                "release_title": "Album",
                "unmatched_items": [],
            }),
            mock.patch.object(APP, "_match_tracks_from_mb", return_value=1),
            mock.patch.object(APP, "_strip_year_from_album_db"),
            mock.patch.object(APP, "_invalidate_lib_cache"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self):
        return self.client.post("/api/albums/123/match", json={"mb_id": VALID_RELEASE_ID})

    def test_identity_update_failure_stops_before_repair_and_relocation(self):
        with mock.patch.object(APP.beets_client, "update_album_metadata", return_value={"ok": False, "error": "metadata apply failed"}) as update, \
             mock.patch.object(APP.beets_client, "plan_album_mb_track_repair") as plan, \
             mock.patch.object(APP.beets_client, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        update.assert_called_once_with(123, {"mb_albumid": VALID_RELEASE_ID})
        plan.assert_not_called()
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()

    def test_mb_track_repair_plan_failure_stops_before_apply_and_relocation(self):
        def update_side_effect(*args, **kwargs):
            return {"ok": True}
        with mock.patch.object(APP.beets_client, "update_album_metadata", side_effect=update_side_effect) as update, \
             mock.patch.object(APP.beets_client, "plan_album_mb_track_repair", return_value={"ok": False, "error": "plan failed"}) as plan, \
             mock.patch.object(APP.beets_client, "apply_album_mb_track_repair") as apply, \
             mock.patch.object(APP.beets_client, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        self.assertEqual(update.call_count, 1)
        plan.assert_called_once()
        apply.assert_not_called()
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()

    def test_mb_track_repair_apply_failure_stops_before_write_and_relocation(self):
        with mock.patch.object(APP.beets_client, "update_album_metadata", return_value={"ok": True}) as update, \
             mock.patch.object(APP.beets_client, "plan_album_mb_track_repair", return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(APP.beets_client, "apply_album_mb_track_repair", return_value={"ok": False, "error": "apply failed"}), \
             mock.patch.object(APP.beets_client, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        self.assertEqual(update.call_count, 1)
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()

    def test_relocation_failure_is_not_reported_as_success(self):
        with mock.patch.object(APP.beets_client, "update_album_metadata", return_value={"ok": True}) as update, \
             mock.patch.object(APP.beets_client, "plan_album_mb_track_repair", return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(APP.beets_client, "apply_album_mb_track_repair", return_value={"ok": True}), \
             mock.patch.object(APP.beets_client, "relocate_album", return_value={"ok": False, "error": "move failed"}) as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        self.assertEqual(update.call_count, 2)
        relocate.assert_called_once_with(123)
        APP._invalidate_lib_cache.assert_not_called()


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class DuplicateResolverIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wave27_duplicate_db_")
        self.db_path = Path(self.tmp.name) / "library.blb"
        con = sqlite3.connect(self.db_path)
        try:
            con.executescript(
                """
                CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, year INTEGER, mb_albumid TEXT);
                CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, album TEXT, albumartist TEXT, artist TEXT, title TEXT, disc INTEGER, track INTEGER, year INTEGER, mb_albumid TEXT, mb_trackid TEXT, path BLOB);
                INSERT INTO albums (id, album, albumartist, year, mb_albumid) VALUES (123, 'Target Album', 'Target Artist', 2020, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
                INSERT INTO albums (id, album, albumartist, year, mb_albumid) VALUES (7, 'Duplicate Album', 'Wrong Artist', 2019, 'old-release');
                INSERT INTO items (id, album_id, album, albumartist, artist, title, disc, track, year, mb_albumid, mb_trackid, path) VALUES (999, 7, 'Duplicate Album', 'Wrong Artist', 'Wrong Artist', 'Wrong title', 1, 99, 2019, 'old-release', 'old-track', X'2f6d757369632f77726f6e672e6d7033');
                """
            )
            con.commit()
        finally:
            con.close()
        self.inline = InlineJobs()
        self.client = APP.app.test_client()
        self.patches = [
            mock.patch.object(APP.jobs, "start_python", side_effect=self.inline.start_python),
            mock.patch.object(APP, "_db", side_effect=lambda *a, **k: sqlite_app_db(self.db_path, row_factory=k.get("row_factory"), text_factory=k.get("text_factory"))),
            mock.patch.object(APP, "_album_duplicate_resolver_plan", return_value={
                "ok": True,
                "mb_albumid": VALID_RELEASE_ID,
                "groups": [{"action_items": [{"id": 999, "album_id": 7, "filename": "wrong.mp3", "title": "Wrong title"}]}],
                "missing_tracks": [{"mb_trackid": VALID_TRACK_ID, "disc": 1, "track": 1, "title": "Correct title"}],
            }),
            mock.patch.object(APP, "_invalidate_lib_cache"),
            mock.patch.object(APP, "_trigger_plex_refresh"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_retag_uses_selected_album_id_not_item_id_or_album_zero(self):
        with mock.patch.object(APP.beets_client, "update_album_metadata", return_value={"ok": True}) as update, \
             mock.patch.object(APP.beets_client, "relocate_album", return_value={"ok": True}) as relocate:
            response = self.client.post("/api/albums/123/duplicate-resolver/apply", json={
                "mb_albumid": VALID_RELEASE_ID,
                "write_tags": True,
                "delete_files": False,
                "actions": [{"item_id": 999, "action": "retag", "target_mb_trackid": VALID_TRACK_ID}],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.inline.error)
        update.assert_called_once_with(123, {}, force_write_tags=True)
        relocate.assert_called_once_with(123, mode="rename")
        self.assertNotEqual(relocate.call_args.args[0], 999)
        self.assertNotEqual(relocate.call_args.args[0], 0)


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class LastgenreControlledRepairTests(unittest.TestCase):
    def test_non_album_query_fails_closed(self):
        result = APP._lastgenre_cmd(False, "id:999", [], {}, timeout=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("album_id", result.stderr)

    def test_album_query_uses_controlled_genre_repair(self):
        with mock.patch.object(APP.beets_client, "repair_album_genre", return_value={"ok": True, "output": "genre ok"}) as repair, \
             mock.patch.object(APP.beets_client, "run_command") as run_command:
            result = APP._lastgenre_cmd(True, "album_id:123", [], {}, timeout=2)
        self.assertEqual(result.returncode, 0)
        self.assertIn("genre ok", result.stdout)
        repair.assert_called_once_with(123, force=True, timeout=2.0)
        run_command.assert_not_called()

    def test_album_query_failure_is_process_failure(self):
        with mock.patch.object(APP.beets_client, "repair_album_genre", return_value={"ok": False, "error": "plugin failed"}):
            result = APP._lastgenre_cmd(False, "album_id:123", [], {}, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("plugin failed", result.stderr)


class BeetsClientRequiredWrapperTests(unittest.TestCase):
    def setUp(self):
        from backend.beets_client import BeetsClient
        self.client = BeetsClient(base_url="http://engine", token="x" * 32)

    def test_update_album_metadata_plan_failure_never_applies(self):
        with mock.patch.object(self.client, "plan_album_metadata", return_value={"ok": False, "error": "bad plan", "code": "bad"}), \
             mock.patch.object(self.client, "apply_album_metadata") as apply:
            res = self.client.update_album_metadata(1, {"mb_albumid": VALID_RELEASE_ID})
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "bad")
        apply.assert_not_called()

    def test_update_album_metadata_apply_failure_is_returned(self):
        with mock.patch.object(self.client, "plan_album_metadata", return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(self.client, "apply_album_metadata", return_value={"ok": False, "error": "apply failed", "code": "apply"}):
            res = self.client.update_album_metadata(1, {})
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "apply")

    def test_relocate_album_plan_failure_never_applies(self):
        with mock.patch.object(self.client, "plan_album_relocation", return_value={"ok": False, "error": "bad plan"}), \
             mock.patch.object(self.client, "apply_album_relocation") as apply:
            res = self.client.relocate_album(1)
        self.assertFalse(res["ok"])
        apply.assert_not_called()

    def test_relocate_album_apply_failure_is_returned(self):
        with mock.patch.object(self.client, "plan_album_relocation", return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(self.client, "apply_album_relocation", return_value={"ok": False, "error": "move failed"}):
            res = self.client.relocate_album(1)
        self.assertFalse(res["ok"])
        self.assertIn("move failed", res["error"])

    def test_update_item_metadata_plan_and_apply_failures(self):
        with mock.patch.object(self.client, "plan_item_metadata", return_value={"ok": False, "error": "bad item plan", "code": "plan"}), \
             mock.patch.object(self.client, "apply_item_metadata") as apply:
            res = self.client.update_item_metadata(99, {"title": "x"})
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "plan")
        apply.assert_not_called()

        with mock.patch.object(self.client, "plan_item_metadata", return_value={"ok": True, "operation_id": "txn_item"}), \
             mock.patch.object(self.client, "apply_item_metadata", return_value={"ok": False, "error": "apply item", "code": "apply"}):
            res = self.client.update_item_metadata(99, {"title": "x"})
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "apply")

    def test_genre_repair_plan_and_apply_failures(self):
        with mock.patch.object(self.client, "plan_album_genre_repair", return_value={"ok": False, "error": "bad genre plan", "code": "plan"}), \
             mock.patch.object(self.client, "apply_album_genre_repair") as apply:
            res = self.client.repair_album_genre(5)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "plan")
        apply.assert_not_called()

        with mock.patch.object(self.client, "plan_album_genre_repair", return_value={"ok": True, "operation_id": "txn_genre"}), \
             mock.patch.object(self.client, "apply_album_genre_repair", return_value={"ok": False, "error": "apply genre", "code": "apply"}):
            res = self.client.repair_album_genre(5)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "apply")


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class ArtistAliasFolderReconcileTests(unittest.TestCase):
    """Blocker 7: artist alias merge must delegate the on-disk folder move
    to the engine-owned artist_folder_reconcile_v1 transaction family
    instead of looping per-album update_album_metadata+relocate_album
    calls that were never actually that transaction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.music_root = Path(self.tmp.name)
        (self.music_root / "Old Name").mkdir()
        self.patches = [mock.patch.object(APP, "MUSIC_ROOT", self.music_root)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_plan_uses_source_and_target_paths_under_music_root(self):
        with mock.patch.object(APP.beets_client, "plan_artist_folder_reconcile",
                                return_value={"ok": True, "operation_id": "txn_1"}) as plan, \
             mock.patch.object(APP.beets_client, "apply_artist_folder_reconcile",
                                return_value={"ok": True, "moved_files": 3, "quarantined_files": 0, "removed_dirs": 1}) as apply:
            log = []
            res = APP._run_artist_folder_reconcile_for_alias_merge(["Old Name"], "New Name", VALID_ARTIST_ID, log)
        self.assertTrue(res["ok"])
        plan.assert_called_once()
        payload = plan.call_args[0][0]
        self.assertEqual(payload["mode"], "scan_merge")
        self.assertEqual(len(payload["candidates"]), 1)
        cand = payload["candidates"][0]
        self.assertEqual(cand["source_path"], str(self.music_root / "Old Name"))
        self.assertEqual(cand["target_path"], str(self.music_root / "New Name"))
        self.assertEqual(cand["source_mbid"], VALID_ARTIST_ID)
        apply.assert_called_once_with("txn_1")

    def test_nonexistent_source_folder_is_skipped_without_calling_engine(self):
        with mock.patch.object(APP.beets_client, "plan_artist_folder_reconcile") as plan:
            log = []
            res = APP._run_artist_folder_reconcile_for_alias_merge(["Never Existed"], "New Name", VALID_ARTIST_ID, log)
        self.assertTrue(res["ok"])
        plan.assert_not_called()

    def test_engine_unavailable_fails_closed_not_silently(self):
        with mock.patch.object(APP.beets_client, "plan_artist_folder_reconcile",
                                side_effect=APP.BeetsUnavailableError("down")), \
             mock.patch.object(APP.beets_client, "apply_artist_folder_reconcile") as apply:
            log = []
            with self.assertRaises(RuntimeError):
                APP._run_artist_folder_reconcile_for_alias_merge(["Old Name"], "New Name", VALID_ARTIST_ID, log)
        apply.assert_not_called()

    def test_plan_rejection_fails_closed_without_apply(self):
        with mock.patch.object(APP.beets_client, "plan_artist_folder_reconcile",
                                return_value={"ok": False, "error": "identity conflict"}), \
             mock.patch.object(APP.beets_client, "apply_artist_folder_reconcile") as apply:
            log = []
            with self.assertRaises(RuntimeError):
                APP._run_artist_folder_reconcile_for_alias_merge(["Old Name"], "New Name", VALID_ARTIST_ID, log)
        apply.assert_not_called()

    def test_apply_failure_is_not_reported_as_success(self):
        with mock.patch.object(APP.beets_client, "plan_artist_folder_reconcile",
                                return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(APP.beets_client, "apply_artist_folder_reconcile",
                                return_value={"ok": False, "error": "move failed"}):
            log = []
            with self.assertRaises(RuntimeError):
                APP._run_artist_folder_reconcile_for_alias_merge(["Old Name"], "New Name", VALID_ARTIST_ID, log)


class CompensatingMetadataRollbackTests(unittest.TestCase):
    def test_compensate_without_meta_op_id_raises_downstream_exception(self):
        log = []
        downstream = RuntimeError("relocate failed")
        with self.assertRaises(RuntimeError) as ctx:
            APP._compensate_committed_metadata_or_raise(123, None, "relocate stage", downstream, log)
        self.assertIs(ctx.exception, downstream)

    def test_compensate_rollback_success_logs_and_raises_rolled_back_exception(self):
        log = []
        downstream = RuntimeError("relocate failed")
        with mock.patch.object(APP.beets_client, "rollback_album_metadata", return_value={"ok": True}):
            with self.assertRaises(RuntimeError) as ctx:
                APP._compensate_committed_metadata_or_raise(123, "meta_op_1", "relocate stage", downstream, log)
        self.assertIn("relocate stage failed and metadata was rolled back", str(ctx.exception))
        self.assertTrue(any("Metadata rolled back cleanly" in msg for msg in log))

    def test_compensate_rollback_failure_raises_recovery_required(self):
        log = []
        downstream = RuntimeError("relocate failed")
        with mock.patch.object(APP.beets_client, "rollback_album_metadata", return_value={"ok": False, "error": "DB locked"}):
            with self.assertRaises(RuntimeError) as ctx:
                APP._compensate_committed_metadata_or_raise(123, "meta_op_1", "relocate stage", downstream, log)
        self.assertIn("Recovery Required", str(ctx.exception))
        self.assertTrue(any("RECOVERY REQUIRED" in msg for msg in log))


class ArtistTrustTierRegressionTests(unittest.TestCase):
    def test_a_caller_mbid_exists_elsewhere_in_db_without_recording_proof_requires_review(self):
        """Case A (Section 9): Both folders blank, fingerprint true, caller MBID exists elsewhere in local DB
        but no recording->artist proof -> REVIEW REQUIRED."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        def _no_recording_proof_verifier(mbid, rec_ids):
            return False
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=True, mb_verifier=_no_recording_proof_verifier
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "artist_reconcile_requires_review")

    def test_b_musicbrainz_artist_exists_without_recording_proof_requires_review(self):
        """Case B (Section 9): Both folders blank, fingerprint true, caller MBID valid UUID,
        MusicBrainz Artist itself exists but no recording->artist proof -> REVIEW REQUIRED."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        def _no_recording_proof_verifier(mbid, rec_ids):
            return False
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=True, mb_verifier=_no_recording_proof_verifier
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "artist_reconcile_requires_review")

    def test_c_recording_artist_credit_contains_caller_mbid_eligible(self):
        """Case C (Section 9): Fingerprint recording resolves to MusicBrainz Recording whose artist-credit
        contains caller MBID -> ELIGIBLE."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        def _matching_verifier(mbid, rec_ids):
            return mbid == caller_mbid and bool(rec_ids)
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=True,
            recording_mbids=["11111111-1111-1111-1111-111111111111"], mb_verifier=_matching_verifier
        )
        self.assertTrue(eligible)
        self.assertEqual(mbid, caller_mbid)
        self.assertEqual(reason, "")

    def test_d_recording_artist_credit_differs_requires_review(self):
        """Case D (Section 9): Fingerprint recording resolves to different Artist MBID -> REVIEW REQUIRED."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        def _differing_verifier(mbid, rec_ids):
            return False
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=True,
            recording_mbids=["11111111-1111-1111-1111-111111111111"], mb_verifier=_differing_verifier
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "artist_reconcile_requires_review")

    def test_e_musicbrainz_lookup_unavailable_fails_closed(self):
        """Case E (Section 9): MusicBrainz lookup unavailable -> fail closed -> REVIEW REQUIRED."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        with mock.patch("helpers_mb._fetch_mb_recording_details", side_effect=Exception("Network down")):
            eligible, mbid, reason = txn._artist_folder_identity_decision(
                src_id, dst_id, is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=True,
                recording_mbids=["11111111-1111-1111-1111-111111111111"]
            )
        self.assertFalse(eligible)
        self.assertEqual(reason, "artist_reconcile_requires_review")

    def test_f_ai_only_requires_review(self):
        """Case F (Section 9): AI only (fingerprint_confirmed=False) -> REVIEW REQUIRED."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=False
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "artist_reconcile_requires_review")

    def test_g_matching_established_local_mbids_allowed_without_network(self):
        """Case G (Section 9): Matching established local MBIDs -> eligible without network dependency."""
        uuid_str = "89ad4ac3-39f7-470e-963a-56509c546377"
        src_id = {"ids": {uuid_str}, "album_count": 1}
        dst_id = {"ids": {uuid_str}, "album_count": 1}
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=True, caller_mbid="", fingerprint_confirmed=False
        )
        self.assertTrue(eligible)
        self.assertEqual(mbid, uuid_str)

    def test_pure_path_rename_without_caller_mbid(self):
        """Pure path rename test (Section 12): is_merge=False, src_id blank, no caller_mbid
        -> eligible with resolved_mbid="" (path rename only, no artist MBID mutation)."""
        src_id = {"ids": set(), "album_count": 0}
        dst_id = {"ids": set(), "album_count": 0}
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            src_id, dst_id, is_merge=False, caller_mbid="", fingerprint_confirmed=False
        )
        self.assertTrue(eligible)
        self.assertEqual(mbid, "")
        self.assertEqual(reason, "")

    def test_spoofed_recording_not_established_on_source_item_requires_review(self):
        """Spoofed recording test: Caller supplies recording R and fingerprint_confirmed=True,
        where MB says R -> A, but R is NOT established on any source item under src_path in DB.
        _extract_recording_mbids returns [] because R is uncorroborated hint.
        Result: REVIEW REQUIRED."""
        cand = {"recording_mbid": "11111111-1111-1111-1111-111111111111", "fingerprint_confirmed": True}
        # In DB, items under src_path have no established mb_trackid
        with mock.patch("backend.transaction_engine._rows_by_path_prefix", return_value=[]):
            rec_ids = txn._extract_recording_mbids(cand, Path("/music/Artist"), db_path="")
        self.assertEqual(rec_ids, [])
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            {"ids": set(), "album_count": 0}, {"ids": set(), "album_count": 0},
            is_merge=True, caller_mbid="89ad4ac3-39f7-470e-963a-56509c546377", fingerprint_confirmed=True,
            recording_mbids=rec_ids
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "artist_reconcile_requires_review")

    def test_engine_established_recording_authorizes_candidate(self):
        """Engine-established recording test: Source item under src_path in DB has established mb_trackid=R.
        Caller supplies hint R (or no hint). MB confirms R -> A.
        Result: ELIGIBLE."""
        rec_id = "11111111-1111-1111-1111-111111111111"
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        cand = {"recording_mbid": rec_id, "fingerprint_confirmed": True}
        def _verifier(m, r_ids):
            return m == caller_mbid and rec_id in r_ids
        eligible, mbid, reason = txn._artist_folder_identity_decision(
            {"ids": set(), "album_count": 0}, {"ids": set(), "album_count": 0},
            is_merge=True, caller_mbid=caller_mbid, fingerprint_confirmed=True,
            recording_mbids=[rec_id], mb_verifier=_verifier
        )
        self.assertTrue(eligible)
        self.assertEqual(mbid, caller_mbid)
        self.assertEqual(reason, "")

    def test_recording_conflict_uncorroborated_caller_hint_ignored(self):
        """Recording conflict test: Engine DB establishes R1 on source item. Caller supplies hint R2.
        R2 is not established against source audio, so R2 is ignored.
        If established R1 does not credit caller MBID A -> REVIEW REQUIRED."""
        r1 = "11111111-1111-1111-1111-111111111111"
        r2 = "22222222-2222-2222-2222-222222222222"
        cand = {"recording_mbid": r2}
        fake_rows = [{"mb_trackid": r1, "path": "/music/Artist/song.mp3"}]
        with mock.patch("backend.transaction_engine.validate_path_under_allowed_roots", side_effect=lambda p, *args, **kwargs: Path(str(p))):
            with mock.patch("backend.transaction_engine.Path.exists", return_value=True):
                with mock.patch("backend.transaction_engine._rows_by_path_prefix", return_value=fake_rows):
                    with mock.patch("os.environ.get", return_value="/tmp/lib.db"):
                        rec_ids = txn._extract_recording_mbids(cand, Path("/music/Artist"), db_path="/tmp/lib.db")
        # R2 was uncorroborated, so rec_ids is [] (since intersection of {r1} and {r2} is empty)
        self.assertEqual(rec_ids, [])

    def test_multiple_established_recordings_contradictory_fails_closed(self):
        """Multiple recordings conflict test: Folder has R1 and R2 established.
        R1 credits A, but R2 does not credit A.
        _verify_mb_artist_recording_credit fails closed -> REVIEW REQUIRED."""
        r1 = "11111111-1111-1111-1111-111111111111"
        r2 = "22222222-2222-2222-2222-222222222222"
        caller_mbid = "89ad4ac3-39f7-470e-963a-56509c546377"
        def _mock_mb(rec_id):
            if rec_id == r1:
                return {"mb_artistid": caller_mbid}
            return {"mb_artistid": "99999999-9999-9999-9999-999999999999"}
        with mock.patch("helpers_mb._fetch_mb_recording_details", side_effect=_mock_mb):
            verified = txn._verify_mb_artist_recording_credit(caller_mbid, [r1, r2])
        self.assertFalse(verified)


if __name__ == "__main__":
    unittest.main()