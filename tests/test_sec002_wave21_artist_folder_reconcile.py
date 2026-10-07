"""SEC-002 / ARCH-003 Wave 21: Artist Folder Merge & MBID Stamping Controlled Mutation Boundary.

Tests for artist_folder_reconcile_v1 mutation family in transaction_engine.py,
beets_control_agent.py, beets_client.py, and app.py.
"""

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402
try:  # ARCH-001: app.py module family
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    albumartists TEXT,
    mb_albumartistid TEXT,
    mb_albumartistids TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER,
    artpath BLOB
);
-- Real Beets schema (verified against actual `beets` package output, hotfix
-- v0.1.17 follow-up) has NO `albums.path` column -- only `items.path` and
-- `albums.artpath`. A prior version of this fixture synthetically added one,
-- which masked a real production bug: create_artist_folder_reconcile_plan()
-- querying a nonexistent `albums.path` column, raising
-- sqlite3.OperationalError against every real Beets library. Do not add it
-- back.

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    artists TEXT,
    albumartist TEXT,
    albumartists TEXT,
    album TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_artistid TEXT,
    mb_artistids TEXT,
    mb_albumartistid TEXT,
    mb_albumartistids TEXT,
    mb_releasegroupid TEXT,
    length REAL
);
"""

MBID_A = "aaaaaaaa-0000-0000-0000-000000000001"
MBID_B = "bbbbbbbb-0000-0000-0000-000000000002"


class WebManagerMutationProhibitionTests(unittest.TestCase):
    """AST structural inspection asserting Web Manager contains zero direct
    mutations AND zero direct references to engine mutation internals
    (SEC-002 Wave 21 final review, finding #3: the original AST test only
    checked for raw os/shutil calls, so it passed even while the function
    imported and directly executed transaction_engine's Plan/Apply
    functions in-process)."""

    def test_artist_folder_functions_contain_no_direct_mutations(self):
        if True:  # ARCH-001: the app.py module family
            source = app_family_source()
            tree = ast.parse(source, filename="app.py")

        target_funcs = {
            "_merge_artist_dir_contents",
            "_apply_artist_folder_groups",
            "clean_artist_folders_stamp_mbid",
        }

        prohibited_attributes = {"unlink", "rename", "replace", "rmdir", "remove"}
        prohibited_shutil = {"move", "rmtree", "copy", "copy2"}
        prohibited_names = {
            "TransactionStore",
            "create_artist_folder_reconcile_plan",
            "execute_artist_folder_reconcile_apply",
            "rollback_artist_folder_reconcile",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in target_funcs:
                fn_source = ast.get_source_segment(source, node) or ""
                for name in prohibited_names:
                    self.assertNotIn(name, fn_source, f"Prohibited reference '{name}' found in {node.name} -- engine mutation must only be reached via BeetsClient")
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in prohibited_attributes:
                        self.fail(f"Direct mutation .{sub.attr} found in {node.name}")
                    if isinstance(sub, ast.Call):
                        if isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
                            if sub.func.value.id == "shutil" and sub.func.attr in prohibited_shutil:
                                self.fail(f"Direct shutil.{sub.func.attr} found in {node.name}")
                        if isinstance(sub.func, ast.Name) and sub.func.id == "sqlite3":
                            self.fail(f"Direct sqlite3 usage found in {node.name}")
                    if isinstance(sub, ast.ImportFrom) and sub.module == "backend.transaction_engine":
                        imported = {alias.name for alias in sub.names}
                        leaked = imported & prohibited_names
                        self.assertFalse(leaked, f"{node.name} imports engine mutation functions directly: {leaked}")


class ResilientApplyAgainstLostResponseTests(unittest.TestCase):
    """Hotfix v0.1.17 (BUG-4/BUG-5): a real TrueNAS v0.1.16 production
    incident. Web Manager's own client-side timeout fired on
    apply_artist_folder_reconcile() while the Beets Engine kept executing
    the controlled mutation normally; Web Manager logged "engine
    unavailable" and gave up, then the engine's own attempt to write back
    its now-orphaned response produced a BrokenPipeError. Production
    inspection proved the mutation continued moving from file to file
    after Web Manager had already reported failure.

    These tests exercise app._apply_artist_folder_reconcile_resilient()
    directly (the shared helper all three real apply_artist_folder_reconcile
    call sites in app.py now go through) against a mocked beets_client,
    with poll/max-wait configuration patched to small values so the tests
    run in well under a second rather than actually waiting minutes.
    """

    def setUp(self):
        self.patchers = []
        self._patch(patch_app_family(app_module, "BEETS_LONG_OPERATION_POLL_SECONDS", 0.01))
        self._patch(patch_app_family(app_module, "BEETS_LONG_OPERATION_MAX_SECONDS", 0.2))

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _patch(self, patcher):
        self.patchers.append(patcher)
        return patcher.start()

    def test_apply_success_on_the_first_call_never_polls(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            return_value={"ok": True, "operation_id": "op-1", "status": "Completed"},
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-1", log)

        self.assertTrue(result.get("ok"))
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_lost_apply_response_polls_and_reports_the_real_completed_outcome(self):
        """The exact production incident: apply's own HTTP response is
        lost (client-side timeout), but the engine actually completed the
        mutation. Apply must be called exactly once; the real outcome must
        come from polling the transaction, not from a second apply call."""
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        statuses = iter(["Running", "Running", "Completed"])
        get_tx_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            side_effect=lambda op_id: {"ok": True, "transaction": {"status": next(statuses), "operation_id": op_id}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-2", log)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "Completed")
        self.assertTrue(result.get("recovered_via_poll"))
        apply_mock.assert_called_once()
        self.assertGreaterEqual(get_tx_mock.call_count, 3, "must have polled through both Running states to Completed")
        self.assertFalse(any("ENGINE_OFFLINE" in line for line in log), "must not report ENGINE_OFFLINE while the engine is genuinely still working")
        joined_log = "\n".join(log)
        self.assertIn("op-2", joined_log)

    def test_lost_apply_response_then_confirmed_failed_is_reported_as_failed(self):
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Failed", "operation_id": "op-3"}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-3", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "Failed")
        self.assertTrue(result.get("recovered_via_poll"))

    def test_apply_never_called_a_second_time_even_across_many_poll_iterations(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Running", "operation_id": "op-4"}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-4", log)

        # Max-wait exceeded while still "Running" -- must report "still
        # running", never fabricate success, and never call Apply again.
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("still_running"))
        apply_mock.assert_called_once()

    def test_transient_transaction_lookup_failures_are_retried_not_fatal(self):
        """A poll that itself fails to reach the engine (still recovering)
        must be retried within the bound, not treated as a final failure."""
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        responses = iter([
            app_module.BeetsUnavailableError("engine still recovering"),
            app_module.BeetsUnavailableError("engine still recovering"),
            {"ok": True, "transaction": {"status": "Completed", "operation_id": "op-5"}},
        ])

        def _get_transaction(op_id):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction", side_effect=_get_transaction))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-5", log)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "Completed")

    def test_cancellation_stops_polling_without_reporting_false_success_or_failure(self):
        cancel_event = mock.MagicMock()
        cancel_event.is_set.return_value = True
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-6", log, cancel_event=cancel_event)

        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("still_running"))
        get_tx_mock.assert_not_called()

    def test_clean_artist_folders_stamp_mbid_job_does_not_reapply_on_lost_response(self):
        """End-to-end through the real production call site: clean_artist_folders_stamp_mbid()'s
        background job must call apply_artist_folder_reconcile at most
        once even when its own client-side call fails, and must recover
        the real outcome via transaction polling."""
        with patch_app_family(app_module, "MUSIC_ROOT", Path(tempfile.mkdtemp())), \
             patch_app_family(app_module, "_security_auth_disabled", return_value=True):
            with patch_app_family(app_module, "_stamp_artist_folder_scan",
                return_value={"candidates": [{"source": "x"}], "skipped": []},
            ), mock.patch.object(
                app_module.composite_workflows, "plan_artist_folder_reconcile",
                return_value={"ok": True, "operation_id": "op-7"},
            ), mock.patch.object(
                app_module.composite_workflows, "apply_artist_folder_reconcile",
                side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
            ) as apply_mock, mock.patch.object(
                app_module.composite_workflows, "get_transaction",
                return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-7", "renamed": 2, "merged": 1}},
            ):
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid",
                    method="POST",
                    json={"root": str(app_module.MUSIC_ROOT), "dry_run": True},
                ):
                    payload = app_module.request.get_json(silent=True) or {}
                root_path, _err = app_module._artist_folder_repair_root(payload.get("root") or str(app_module.MUSIC_ROOT))

                # Reach into the real, unexported `_do` closure the same way
                # the maintenance job does: call the route in non-dry-run
                # mode via test_request_context, but capture the job body
                # directly rather than going through the async job store.
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid",
                    method="POST",
                    json={"root": str(root_path), "dry_run": False},
                ):
                    captured = {}
                    real_start_python = app_module.jobs.start_python

                    def _capture_and_run(fn, label="", metadata=None):
                        log = []
                        fn(log)
                        captured["log"] = log
                        return mock.MagicMock(job_id="job-7")

                    with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run):
                        app_module.clean_artist_folders_stamp_mbid()

                apply_mock.assert_called_once()
                joined_log = "\n".join(captured.get("log") or [])
                self.assertNotIn("ENGINE_OFFLINE", joined_log)
                self.assertIn("op-7", joined_log)

    def test_bad_request_fails_immediately_without_polling(self):
        """A definite HTTP 400 means the engine already answered "no" (bad
        operation_id/payload) -- not that the response was lost. Must never
        enter the transaction poll loop, and must never call Apply again."""
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsBadRequestError(
                "Beets API bad request: operation not in Pending/Approved state",
                error_code="INVALID_STATE", status_code=400,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-400", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "INVALID_STATE")
        self.assertEqual(result.get("status_code"), 400)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()
        self.assertFalse(any("polling" in line.lower() and "response was lost" in line.lower() for line in log))

    def test_auth_error_fails_immediately_without_polling(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsAuthError(
                "Authentication with Beets Control Agent failed: HTTP 401",
                error_code="ENGINE_AUTH_FAILED", status_code=401,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-401", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status_code"), 401)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_not_found_fails_immediately_without_polling(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsNotFoundError(
                "Beets API resource not found: operation_id unknown",
                error_code="NOT_FOUND", status_code=404,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-404", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status_code"), 404)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_forbidden_403_fails_immediately_without_polling(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsAuthError(
                "Access to Beets Control Agent forbidden: HTTP 403",
                error_code="FORBIDDEN", status_code=403,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-403", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status_code"), 403)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_ambiguous_5xx_still_polls_unlike_definite_4xx(self):
        """A generic 5xx (not one of the specific 4xx rejection types) means
        the engine may have started mutating before failing to answer --
        this is transport/execution uncertainty, not a definite rejection,
        and must still recover via the transaction poll like a
        BeetsUnavailableError does."""
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsError(
                "Beets Control Agent server error: HTTP 500", error_code="ENGINE_SERVER_ERROR", status_code=500,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-500"}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-500", log)

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("recovered_via_poll"))
        apply_mock.assert_called_once()
        get_tx_mock.assert_called()


class StampArtistFolderScanFailClosedTests(unittest.TestCase):
    """Independent review follow-up: an engine inventory failure must never
    be indistinguishable from a genuine successful scan that found zero
    eligible folders. _stamp_artist_folder_scan() must report ok=False with
    the real error/error_code, not the same empty
    {"candidates": [], "skipped": []} shape a real empty scan returns."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name) / "music"
        self.root.mkdir()

    def test_beets_unavailable_reports_ok_false_not_empty_success(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result.get("error"), "Beets Control Agent is unavailable.")

    def test_beets_unavailable_does_not_expose_raw_exception_text(self):
        """CodeQL: information exposure through an exception. A
        BeetsUnavailableError's own message can carry internal URLs, host
        names, or ports (it is built from the real connection failure) --
        none of that may reach the "error" field callers surface to HTTP
        responses, job logs, and job results."""
        sensitive = "http://internal-secret-host.example:9999/beets-agent?token=abc123secret"
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsUnavailableError(f"Beets Control Agent is unavailable at {sensitive}: refused"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("secret", result.get("error", "").lower())
        self.assertEqual(result.get("error"), "Beets Control Agent is unavailable.")

    def test_beets_auth_error_reports_ok_false_with_error_code(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsAuthError(
                "Authentication with Beets Control Agent failed: HTTP 401",
                error_code="ENGINE_AUTH_FAILED", status_code=401,
            ),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "ENGINE_AUTH_FAILED")
        self.assertEqual(result.get("status_code"), 401)
        # error_code/status_code are agent-controlled structured fields and
        # stay intact; the message itself is the canned safe text, not the
        # raw exception string.
        self.assertEqual(result.get("error"), "Authentication with Beets Control Agent failed.")

    def test_beets_auth_error_does_not_expose_raw_exception_text(self):
        sensitive = "/config/.beet_secret_token_file"
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsAuthError(f"Authentication failed reading {sensitive}: permission denied"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("permission denied", result.get("error", ""))

    def test_unexpected_exception_does_not_expose_raw_text(self):
        sensitive = "/home/runner/work/secret-internal-path/credentials.json"
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=RuntimeError(f"unexpected failure reading {sensitive}"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("credentials", result.get("error", "").lower())
        self.assertEqual(result.get("error"), "Artist-folder inventory failed.")

    def test_genuine_empty_inventory_reports_ok_true(self):
        with mock.patch.object(app_module.composite_workflows, "get_artist_folder_inventory", return_value=[]), \
             mock.patch.object(app_module.composite_workflows, "get_artist_folder_album_mbids", return_value=[]):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"], [])

    def test_mbid_counts_engine_failure_reports_ok_false(self):
        artist_dir = self.root / "Some Artist"
        artist_dir.mkdir()
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            return_value=[{"name": "Some Artist", "path": str(artist_dir), "audio_files": 1, "subfolders": 0}],
        ), patch_app_family(app_module, "_stamp_artist_folder_album_mbid_counts",
            return_value=({}, {}, "engine unavailable"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertIn("engine unavailable", result.get("error", ""))

    def test_only_genuine_success_produces_no_folders_need_stamping_message(self):
        """End-to-end through the real production call sites: only a
        genuinely successful, empty scan may produce the "No artist folders
        need MB ID stamping" outcome -- an engine failure must be reported
        as a failure instead."""
        with patch_app_family(app_module, "MUSIC_ROOT", self.root), \
             patch_app_family(app_module, "_security_auth_disabled", return_value=True):
            # Failure case: the real job must raise (fail closed), not
            # report "no artist folders need MB ID stamping".
            with mock.patch.object(
                app_module.composite_workflows, "get_artist_folder_inventory",
                side_effect=app_module.BeetsUnavailableError("engine unreachable"),
            ):
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid", method="POST",
                    json={"root": str(self.root), "dry_run": False},
                ):
                    captured = {}

                    def _capture_and_run(fn, label="", metadata=None):
                        log = []
                        try:
                            fn(log)
                            captured["raised"] = None
                        except Exception as ex:
                            captured["raised"] = ex
                        captured["log"] = log
                        return mock.MagicMock(job_id="job-fail")

                    with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run):
                        app_module.clean_artist_folders_stamp_mbid()
                self.assertIsNotNone(captured["raised"], "an engine inventory failure must raise, not silently succeed")
                joined = "\n".join(captured["log"])
                self.assertNotIn("No artist folders need MB ID stamping", joined)

            # Genuine success case: an empty inventory legitimately produces
            # the "no folders need stamping" outcome.
            with mock.patch.object(app_module.composite_workflows, "get_artist_folder_inventory", return_value=[]), \
                 mock.patch.object(app_module.composite_workflows, "get_artist_folder_album_mbids", return_value=[]):
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid", method="POST",
                    json={"root": str(self.root), "dry_run": False},
                ):
                    captured2 = {}

                    def _capture_and_run2(fn, label="", metadata=None):
                        log = []
                        fn(log)
                        captured2["log"] = log
                        return mock.MagicMock(job_id="job-ok")

                    with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run2):
                        app_module.clean_artist_folders_stamp_mbid()
                self.assertIn("No artist folders need MB ID stamping", "\n".join(captured2["log"]))

    def test_dry_run_http_response_does_not_expose_raw_exception_text(self):
        """CodeQL finding: the dry-run route's error response used to
        return scan.get("error") straight from str(ex). Verify the real
        HTTP JSON response for the exact route CodeQL flagged never
        contains the raw exception text, while still returning a safe
        status code and error_code."""
        sensitive = "postgresql://internal-user:hunter2@10.0.0.55:5432/beetsdb"
        with patch_app_family(app_module, "MUSIC_ROOT", self.root), \
             patch_app_family(app_module, "_security_auth_disabled", return_value=True), \
             mock.patch.object(
                 app_module.composite_workflows, "get_artist_folder_inventory",
                 side_effect=app_module.BeetsAuthError(
                     f"Authentication with Beets Control Agent failed via {sensitive}: HTTP 401",
                     error_code="ENGINE_AUTH_FAILED", status_code=401,
                 ),
             ):
            with app_module.app.test_client() as client:
                resp = client.post(
                    "/api/clean/artist-folders/stamp-mbid",
                    json={"root": str(self.root), "dry_run": True},
                )
        raw_body = resp.get_data(as_text=True)
        self.assertNotIn(sensitive, raw_body)
        self.assertNotIn("hunter2", raw_body)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(data.get("error_code"), "ENGINE_AUTH_FAILED")
        self.assertEqual(data.get("error"), "Authentication with Beets Control Agent failed.")

    def test_apply_job_does_not_expose_raw_exception_text_in_log_or_result(self):
        """The async (non-dry-run) stamp-mbid job's log and returned result
        must not expose the raw exception either -- only the job must
        raise (fail closed), never the leaked internal detail."""
        sensitive = "s3://internal-bucket/private-config.yaml?sig=abcdef123456"
        with patch_app_family(app_module, "MUSIC_ROOT", self.root), \
             patch_app_family(app_module, "_security_auth_disabled", return_value=True), \
             mock.patch.object(
                 app_module.composite_workflows, "get_artist_folder_inventory",
                 side_effect=app_module.BeetsUnavailableError(f"connection to {sensitive} failed"),
             ):
            with app_module.app.test_request_context(
                "/api/clean/artist-folders/stamp-mbid", method="POST",
                json={"root": str(self.root), "dry_run": False},
            ):
                captured = {}

                def _capture_and_run(fn, label="", metadata=None):
                    log = []
                    try:
                        fn(log)
                        captured["raised"] = None
                    except Exception as ex:
                        captured["raised"] = ex
                    captured["log"] = log
                    return mock.MagicMock(job_id="job-fail")

                with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run):
                    app_module.clean_artist_folders_stamp_mbid()
            self.assertIsNotNone(captured["raised"])
            joined_log = "\n".join(captured["log"])
            self.assertNotIn(sensitive, joined_log)
            self.assertNotIn("sig=abcdef123456", joined_log)
            # The raised exception itself (used only for the job's internal
            # failed-status bookkeeping, never echoed to the user as JSON)
            # is allowed to carry the real detail -- what matters is that no
            # HTTP response or job-visible log line does.


class ResilientApplySanitizedErrorTests(unittest.TestCase):
    """CodeQL follow-up, audited path #2: _apply_artist_folder_reconcile_resilient()'s
    own exception handling (definite rejection, lost-response, and
    transaction-poll-retry branches) must not leak raw exception text into
    job-visible logs or result "error" fields either."""

    def setUp(self):
        self.patchers = []
        self._patch(patch_app_family(app_module, "BEETS_LONG_OPERATION_POLL_SECONDS", 0.01))
        self._patch(patch_app_family(app_module, "BEETS_LONG_OPERATION_MAX_SECONDS", 0.05))

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _patch(self, patcher):
        self.patchers.append(patcher)
        return patcher.start()

    def test_rejected_apply_does_not_expose_raw_exception_text(self):
        sensitive = "http://engine-internal.local:8338/artists/reconcile/apply?token=zzz"
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsBadRequestError(
                f"Beets API bad request via {sensitive}: operation not in Pending/Approved state",
                error_code="INVALID_STATE", status_code=400,
            ),
        ))
        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-sanitize-1", log)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "INVALID_STATE")
        self.assertEqual(result.get("status_code"), 400)
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("token=zzz", result.get("error", ""))
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)
        self.assertNotIn("token=zzz", joined_log)

    def test_lost_response_poll_does_not_expose_raw_exception_text(self):
        sensitive = "/var/lib/beets/private/musiclibrary.blb"
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError(f"Timed out reaching {sensitive}"),
        ))
        self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-sanitize-2"}},
        ))
        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-sanitize-2", log)
        self.assertTrue(result.get("ok"), result)
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)

    def test_transaction_poll_failure_does_not_expose_raw_exception_text(self):
        sensitive = "postgresql://user:swordfish@10.1.2.3/beets"
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        responses = iter([
            app_module.BeetsUnavailableError(f"engine still recovering, tried {sensitive}"),
            {"ok": True, "transaction": {"status": "Completed", "operation_id": "op-sanitize-3"}},
        ])

        def _get_transaction(op_id):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction", side_effect=_get_transaction))
        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-sanitize-3", log)
        self.assertTrue(result.get("ok"), result)
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)
        self.assertNotIn("swordfish", joined_log)


class SavedOperationLookupSanitizedErrorTests(unittest.TestCase):
    """CodeQL follow-up, audited path #3: _maintenance_artist_folder_merge_step()'s
    saved-operation transaction-status-lookup-failure branch (Clean All
    resume) must not leak raw exception text into its job-visible log line
    or "error" field, while still preserving still_running=True and the
    operation_id (the actual fail-closed behavior under test)."""

    def test_lookup_failure_does_not_expose_raw_exception_text(self):
        sensitive = "http://internal-agent.local:8338/transactions/op-abc?key=topsecret"
        with mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            side_effect=app_module.BeetsUnavailableError(f"Timed out reaching {sensitive}"),
        ):
            log = []
            result = app_module._maintenance_artist_folder_merge_step(
                log, None, "/data/media/music", resume_operation_id="op-abc",
            )
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("still_running"))
        self.assertEqual(result.get("operation_id"), "op-abc")
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("topsecret", result.get("error", ""))
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)
        self.assertNotIn("topsecret", joined_log)
        self.assertEqual(result.get("error"), "Beets Control Agent is unavailable.")


if __name__ == "__main__":
    unittest.main()
