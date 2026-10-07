"""SEC-002 / ARCH-003 Wave 27 result-handling regressions."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

from tests.test_import_review_attach_enforcement import APP, _APP_IMPORT_ERROR
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402


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
        with mock.patch.object(APP.composite_workflows, "update_item_metadata", side_effect=RuntimeError("ipc down")):
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
            patch_app_family(APP, "_invalidate_lib_cache"),
            patch_app_family(APP, "_trigger_plex_refresh"),
            patch_app_family(APP, "_mb_release_group_for_release", return_value=VALID_RGID),
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
        with mock.patch.object(APP.composite_workflows, "update_album_metadata", return_value={"ok": False, "error": "metadata plan failed"}) as meta, \
             mock.patch.object(APP.composite_workflows, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        meta.assert_called_once()
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()
        APP._trigger_plex_refresh.assert_not_called()

    def test_relocation_failure_prevents_completed_log_and_plex_refresh(self):
        with mock.patch.object(APP.composite_workflows, "update_album_metadata", return_value={"ok": True}) as meta, \
             mock.patch.object(APP.composite_workflows, "relocate_album", return_value={"ok": False, "error": "relocation apply failed"}) as relocate:
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
            patch_app_family(APP, "_resolve_mb_release_id", return_value=VALID_RELEASE_ID),
            patch_app_family(APP, "_album_mb_match_plan", return_value={
                "matched_count": 1,
                "actual_count": 1,
                "expected_count": 1,
                "release_title": "Album",
                "unmatched_items": [],
            }),
            patch_app_family(APP, "_match_tracks_from_mb", return_value=1),
            patch_app_family(APP, "_strip_year_from_album_name"),
            patch_app_family(APP, "_invalidate_lib_cache"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self):
        return self.client.post("/api/albums/123/match", json={"mb_id": VALID_RELEASE_ID})

    def test_identity_update_failure_stops_before_repair_and_relocation(self):
        with mock.patch.object(APP.composite_workflows, "update_album_metadata", return_value={"ok": False, "error": "metadata apply failed"}) as update, \
             mock.patch.object(APP.composite_workflows, "plan_album_mb_track_repair") as plan, \
             mock.patch.object(APP.composite_workflows, "relocate_album") as relocate:
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
        with mock.patch.object(APP.composite_workflows, "update_album_metadata", side_effect=update_side_effect) as update, \
             mock.patch.object(APP.composite_workflows, "plan_album_mb_track_repair", return_value={"ok": False, "error": "plan failed"}) as plan, \
             mock.patch.object(APP.composite_workflows, "apply_album_mb_track_repair") as apply, \
             mock.patch.object(APP.composite_workflows, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        self.assertEqual(update.call_count, 1)
        plan.assert_called_once()
        apply.assert_not_called()
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()

    def test_mb_track_repair_apply_failure_stops_before_write_and_relocation(self):
        with mock.patch.object(APP.composite_workflows, "update_album_metadata", return_value={"ok": True}) as update, \
             mock.patch.object(APP.composite_workflows, "plan_album_mb_track_repair", return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(APP.composite_workflows, "apply_album_mb_track_repair", return_value={"ok": False, "error": "apply failed"}), \
             mock.patch.object(APP.composite_workflows, "relocate_album") as relocate:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.inline.error)
        self.assertEqual(update.call_count, 1)
        relocate.assert_not_called()
        APP._invalidate_lib_cache.assert_not_called()

    def test_relocation_failure_is_not_reported_as_success(self):
        with mock.patch.object(APP.composite_workflows, "update_album_metadata", return_value={"ok": True}) as update, \
             mock.patch.object(APP.composite_workflows, "plan_album_mb_track_repair", return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(APP.composite_workflows, "apply_album_mb_track_repair", return_value={"ok": True}), \
             mock.patch.object(APP.composite_workflows, "relocate_album", return_value={"ok": False, "error": "move failed"}) as relocate:
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
            patch_app_family(APP, "_mb_release_group_for_release", return_value="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            mock.patch.object(APP.lib, "get_album", return_value=None),
            patch_app_family(APP, "_db", side_effect=lambda *a, **k: sqlite_app_db(self.db_path, row_factory=k.get("row_factory"), text_factory=k.get("text_factory"))),
            patch_app_family(APP, "_album_duplicate_resolver_plan", return_value={
                "ok": True,
                "mb_albumid": VALID_RELEASE_ID,
                "groups": [{"action_items": [{"id": 999, "album_id": 7, "filename": "wrong.mp3", "title": "Wrong title"}]}],
                "missing_tracks": [{"mb_trackid": VALID_TRACK_ID, "disc": 1, "track": 1, "title": "Correct title"}],
            }),
            patch_app_family(APP, "_invalidate_lib_cache"),
            patch_app_family(APP, "_trigger_plex_refresh"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_retag_uses_selected_album_id_not_item_id_or_album_zero(self):
        # ARCH-003 Wave 33 continuation: the retag path now decomposes
        # into one album_duplicate_merge_v1 Plan+Apply call per distinct
        # source album (here, exactly one: album 7) instead of raw local
        # SQL -- see docs/TECHNICAL_DEBT.md.
        with mock.patch.object(
            APP.composite_workflows, "plan_album_duplicate_merge",
            return_value={"ok": True, "operation_id": "op-retag-1"},
        ) as plan_merge, mock.patch.object(
            APP.composite_workflows, "apply_album_duplicate_merge", return_value={"ok": True},
        ) as apply_merge, mock.patch.object(
            APP.composite_workflows, "update_album_metadata", return_value={"ok": True},
        ) as update, mock.patch.object(
            APP.composite_workflows, "relocate_album", return_value={"ok": True},
        ) as relocate:
            response = self.client.post("/api/albums/123/duplicate-resolver/apply", json={
                "mb_albumid": VALID_RELEASE_ID,
                "write_tags": True,
                "delete_files": False,
                "actions": [{"item_id": 999, "action": "retag", "target_mb_trackid": VALID_TRACK_ID}],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.inline.error)

        plan_merge.assert_called_once()
        merge_payload = plan_merge.call_args.args[0]
        self.assertEqual(merge_payload["target_album_id"], 123)
        self.assertEqual(merge_payload["source_album_id"], 7)
        self.assertEqual(merge_payload["item_ids"], [999])
        self.assertTrue(merge_payload["adopt_target_fields"])
        self.assertEqual(
            merge_payload["item_field_overrides"]["999"]["mb_trackid"],
            VALID_TRACK_ID,
        )
        apply_merge.assert_called_once_with("op-retag-1")

        # Target album's own mb_albumid is stamped separately (the
        # merge family never touches the target's own row), plus the
        # existing tag-write/relocate stage -- three total
        # update_album_metadata-family calls: the mb_albumid stamp and
        # the force_write_tags call.
        update.assert_any_call(123, {"mb_albumid": VALID_RELEASE_ID})
        update.assert_any_call(123, {}, force_write_tags=True)
        relocate.assert_called_once_with(123, mode="rename")
        self.assertNotEqual(relocate.call_args.args[0], 999)
        self.assertNotEqual(relocate.call_args.args[0], 0)


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class DuplicateResolverMultiSourceRetagTests(unittest.TestCase):
    """ARCH-003 Wave 33 continuation: apply_album_duplicate_resolver()'s
    retag path decomposed into N single-source-album
    album_duplicate_merge_v1 calls, one per distinct source album among
    the selected retag items -- see docs/TECHNICAL_DEBT.md for the
    accepted whole-batch-atomicity trade-off this makes (deliberate, not
    an oversight): a later source's merge failing after an earlier one
    already committed is now a real, reportable partial-completion
    outcome, never silently folded into an overall "ok": true.
    """

    VALID_TRACK_ID_2 = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wave33_multi_source_retag_")
        self.db_path = Path(self.tmp.name) / "library.blb"
        con = sqlite3.connect(self.db_path)
        try:
            con.executescript(
                """
                CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, year INTEGER, mb_albumid TEXT);
                CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, album TEXT, albumartist TEXT, artist TEXT, title TEXT, disc INTEGER, track INTEGER, year INTEGER, mb_albumid TEXT, mb_trackid TEXT, path BLOB);
                INSERT INTO albums (id, album, albumartist, year, mb_albumid) VALUES (123, 'Target Album', 'Target Artist', 2020, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
                INSERT INTO albums (id, album, albumartist, year, mb_albumid) VALUES (7, 'Duplicate Album A', 'Wrong Artist A', 2019, 'old-release-a');
                INSERT INTO albums (id, album, albumartist, year, mb_albumid) VALUES (8, 'Duplicate Album B', 'Wrong Artist B', 2018, 'old-release-b');
                INSERT INTO items (id, album_id, album, albumartist, artist, title, disc, track, year, mb_albumid, mb_trackid, path) VALUES (999, 7, 'Duplicate Album A', 'Wrong Artist A', 'Wrong Artist A', 'Wrong title A', 1, 99, 2019, 'old-release-a', 'old-track-a', X'2f6d757369632f612e6d7033');
                INSERT INTO items (id, album_id, album, albumartist, artist, title, disc, track, year, mb_albumid, mb_trackid, path) VALUES (888, 8, 'Duplicate Album B', 'Wrong Artist B', 'Wrong Artist B', 'Wrong title B', 1, 98, 2018, 'old-release-b', 'old-track-b', X'2f6d757369632f622e6d7033');
                """
            )
            con.commit()
        finally:
            con.close()
        self.inline = InlineJobs()
        self.client = APP.app.test_client()
        self.patches = [
            mock.patch.object(APP.jobs, "start_python", side_effect=self.inline.start_python),
            patch_app_family(APP, "_mb_release_group_for_release", return_value="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            mock.patch.object(APP.lib, "get_album", return_value=None),
            patch_app_family(APP, "_db", side_effect=lambda *a, **k: sqlite_app_db(self.db_path, row_factory=k.get("row_factory"), text_factory=k.get("text_factory"))),
            patch_app_family(APP, "_album_duplicate_resolver_plan", return_value={
                "ok": True,
                "mb_albumid": VALID_RELEASE_ID,
                "groups": [{"action_items": [
                    {"id": 999, "album_id": 7, "filename": "a.mp3", "title": "Wrong title A"},
                    {"id": 888, "album_id": 8, "filename": "b.mp3", "title": "Wrong title B"},
                ]}],
                "missing_tracks": [
                    {"mb_trackid": VALID_TRACK_ID, "disc": 1, "track": 1, "title": "Correct title A"},
                    {"mb_trackid": self.VALID_TRACK_ID_2, "disc": 1, "track": 2, "title": "Correct title B"},
                ],
            }),
            patch_app_family(APP, "_invalidate_lib_cache"),
            patch_app_family(APP, "_trigger_plex_refresh"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _actions(self):
        return [
            {"item_id": 999, "action": "retag", "target_mb_trackid": VALID_TRACK_ID},
            {"item_id": 888, "action": "retag", "target_mb_trackid": self.VALID_TRACK_ID_2},
        ]

    def test_two_distinct_source_albums_produce_two_separate_merge_calls(self):
        with mock.patch.object(
            APP.composite_workflows, "plan_album_duplicate_merge",
            return_value={"ok": True, "operation_id": "op-x"},
        ) as plan_merge, mock.patch.object(
            APP.composite_workflows, "apply_album_duplicate_merge", return_value={"ok": True},
        ) as apply_merge, mock.patch.object(
            APP.composite_workflows, "update_album_metadata", return_value={"ok": True},
        ), mock.patch.object(
            APP.composite_workflows, "relocate_album", return_value={"ok": True},
        ):
            response = self.client.post("/api/albums/123/duplicate-resolver/apply", json={
                "mb_albumid": VALID_RELEASE_ID, "write_tags": True, "delete_files": False,
                "actions": self._actions(),
            })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.inline.error)
        self.assertEqual(self.inline.result.get("retagged"), 2)
        self.assertEqual(sorted(self.inline.result.get("retagged_ids")), [888, 999])
        self.assertEqual(self.inline.result.get("retag_failures"), [])

        self.assertEqual(plan_merge.call_count, 2)
        source_ids = sorted(c.args[0]["source_album_id"] for c in plan_merge.call_args_list)
        self.assertEqual(source_ids, [7, 8])
        for call in plan_merge.call_args_list:
            body = call.args[0]
            self.assertEqual(body["target_album_id"], 123)
            self.assertTrue(body["adopt_target_fields"])
            if body["source_album_id"] == 7:
                self.assertEqual(body["item_ids"], [999])
                self.assertEqual(body["item_field_overrides"]["999"]["mb_trackid"], VALID_TRACK_ID)
            else:
                self.assertEqual(body["item_ids"], [888])
                self.assertEqual(body["item_field_overrides"]["888"]["mb_trackid"], self.VALID_TRACK_ID_2)
        self.assertEqual(apply_merge.call_count, 2)

    def test_one_source_failing_does_not_block_the_other_and_is_reported_truthfully(self):
        """The accepted atomicity trade-off, proven concretely: source
        album 8's merge fails, source album 7's still succeeds, and the
        job's own final result truthfully reports both outcomes rather
        than claiming total success or aborting the whole operation."""
        def fake_plan(body):
            if body["source_album_id"] == 8:
                return {"ok": False, "error": "simulated engine rejection", "code": "album_duplicate_merge_identity_mismatch"}
            return {"ok": True, "operation_id": "op-ok"}

        with mock.patch.object(
            APP.composite_workflows, "plan_album_duplicate_merge", side_effect=fake_plan,
        ), mock.patch.object(
            APP.composite_workflows, "apply_album_duplicate_merge", return_value={"ok": True},
        ) as apply_merge, mock.patch.object(
            APP.composite_workflows, "update_album_metadata", return_value={"ok": True},
        ), mock.patch.object(
            APP.composite_workflows, "relocate_album", return_value={"ok": True},
        ):
            response = self.client.post("/api/albums/123/duplicate-resolver/apply", json={
                "mb_albumid": VALID_RELEASE_ID, "write_tags": True, "delete_files": False,
                "actions": self._actions(),
            })
        self.assertEqual(response.status_code, 200)
        # The job itself must not crash/raise just because one of several
        # source albums failed -- it completes, truthfully reporting the
        # mixed outcome.
        self.assertIsNone(self.inline.error)
        result = self.inline.result
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("retagged"), 1)
        self.assertEqual(result.get("retagged_ids"), [999])
        self.assertEqual(len(result.get("retag_failures")), 1)
        self.assertEqual(result["retag_failures"][0]["source_album_id"], 8)
        self.assertEqual(result["retag_failures"][0]["item_ids"], [888])
        self.assertIn("simulated engine rejection", result["retag_failures"][0]["error"])
        # Only the successful source's operation_id is ever applied.
        apply_merge.assert_called_once_with("op-ok")

    def test_dry_run_reports_both_without_calling_the_engine(self):
        with mock.patch.object(APP.composite_workflows, "plan_album_duplicate_merge") as plan_merge, \
             mock.patch.object(APP.composite_workflows, "apply_album_duplicate_merge") as apply_merge:
            response = self.client.post("/api/albums/123/duplicate-resolver/apply", json={
                "mb_albumid": VALID_RELEASE_ID, "dry_run": True,
                "actions": self._actions(),
            })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.inline.error)
        self.assertEqual(self.inline.result.get("retagged"), 2)
        self.assertTrue(self.inline.result.get("dry_run"))
        plan_merge.assert_not_called()
        apply_merge.assert_not_called()


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class LastgenreControlledRepairTests(unittest.TestCase):
    def test_non_album_query_fails_closed(self):
        result = APP._lastgenre_cmd(False, "id:999", [], {}, timeout=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("album_id", result.stderr)

    def test_album_query_uses_controlled_genre_repair(self):
        with mock.patch.object(APP.composite_workflows, "repair_album_genre", return_value={"ok": True, "output": "genre ok"}) as repair, \
             mock.patch.object(APP.composite_workflows, "run_command") as run_command:
            result = APP._lastgenre_cmd(True, "album_id:123", [], {}, timeout=2)
        self.assertEqual(result.returncode, 0)
        self.assertIn("genre ok", result.stdout)
        repair.assert_called_once_with(123, force=True, timeout=2.0)
        run_command.assert_not_called()

    def test_album_query_failure_is_process_failure(self):
        with mock.patch.object(APP.composite_workflows, "repair_album_genre", return_value={"ok": False, "error": "plugin failed"}):
            result = APP._lastgenre_cmd(False, "album_id:123", [], {}, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("plugin failed", result.stderr)


class BeetsClientRequiredWrapperTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.composite_workflows

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

    def test_update_item_metadata_forwards_write_tags_false_to_plan(self):
        with mock.patch.object(self.client, "plan_item_metadata", return_value={"ok": True, "operation_id": None}) as plan,              mock.patch.object(self.client, "apply_item_metadata") as apply:
            res = self.client.update_item_metadata(99, {"title": "x"}, force_write_tags=False, write_tags=False)
        self.assertTrue(res["ok"])
        apply.assert_not_called()
        payload = plan.call_args.args[0]
        self.assertEqual(payload["item_id"], 99)
        self.assertEqual(payload["updates"], {"title": "x"})
        self.assertIs(payload.get("write_tags"), False)
        self.assertIs(payload.get("force_write_tags"), False)

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
        self.patches = [patch_app_family(APP, "MUSIC_ROOT", self.music_root)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_plan_uses_source_and_target_paths_under_music_root(self):
        with mock.patch.object(APP.composite_workflows, "plan_artist_folder_reconcile",
                                return_value={"ok": True, "operation_id": "txn_1"}) as plan, \
             mock.patch.object(APP.composite_workflows, "apply_artist_folder_reconcile",
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
        apply.assert_called_once_with("txn_1", acceptance_failpoint=None, timeout=APP.BEETS_ARTIST_RECONCILE_TIMEOUT_SECONDS)

    def test_nonexistent_source_folder_is_skipped_without_calling_engine(self):
        with mock.patch.object(APP.composite_workflows, "plan_artist_folder_reconcile") as plan:
            log = []
            res = APP._run_artist_folder_reconcile_for_alias_merge(["Never Existed"], "New Name", VALID_ARTIST_ID, log)
        self.assertTrue(res["ok"])
        plan.assert_not_called()

    def test_engine_unavailable_fails_closed_not_silently(self):
        with mock.patch.object(APP.composite_workflows, "plan_artist_folder_reconcile",
                                side_effect=APP.BeetsUnavailableError("down")), \
             mock.patch.object(APP.composite_workflows, "apply_artist_folder_reconcile") as apply:
            log = []
            with self.assertRaises(RuntimeError):
                APP._run_artist_folder_reconcile_for_alias_merge(["Old Name"], "New Name", VALID_ARTIST_ID, log)
        apply.assert_not_called()

    def test_plan_rejection_fails_closed_without_apply(self):
        with mock.patch.object(APP.composite_workflows, "plan_artist_folder_reconcile",
                                return_value={"ok": False, "error": "identity conflict"}), \
             mock.patch.object(APP.composite_workflows, "apply_artist_folder_reconcile") as apply:
            log = []
            with self.assertRaises(RuntimeError):
                APP._run_artist_folder_reconcile_for_alias_merge(["Old Name"], "New Name", VALID_ARTIST_ID, log)
        apply.assert_not_called()

    def test_apply_failure_is_not_reported_as_success(self):
        with mock.patch.object(APP.composite_workflows, "plan_artist_folder_reconcile",
                                return_value={"ok": True, "operation_id": "txn_1"}), \
             mock.patch.object(APP.composite_workflows, "apply_artist_folder_reconcile",
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
        with mock.patch.object(APP.composite_workflows, "rollback_album_metadata", return_value={"ok": True}):
            with self.assertRaises(RuntimeError) as ctx:
                APP._compensate_committed_metadata_or_raise(123, "meta_op_1", "relocate stage", downstream, log)
        self.assertIn("relocate stage failed and metadata was rolled back", str(ctx.exception))
        self.assertTrue(any("Metadata rolled back cleanly" in msg for msg in log))

    def test_compensate_rollback_failure_raises_recovery_required(self):
        log = []
        downstream = RuntimeError("relocate failed")
        with mock.patch.object(APP.composite_workflows, "rollback_album_metadata", return_value={"ok": False, "error": "DB locked"}):
            with self.assertRaises(RuntimeError) as ctx:
                APP._compensate_committed_metadata_or_raise(123, "meta_op_1", "relocate stage", downstream, log)
        self.assertIn("Recovery Required", str(ctx.exception))
        self.assertTrue(any("RECOVERY REQUIRED" in msg for msg in log))






if __name__ == "__main__":
    unittest.main()