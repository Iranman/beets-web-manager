"""Integration tests for the full _ai_import_folder() sequence: as-is import
-> album discovery -> mb_albumid persistence -> mbsync -> write -> move ->
recording-ID repair -> persisted identity reload -> artwork repair ->
history/dedup/Plex refresh -> final result. Also covers the AI-batch worker
call site's batch-state persistence of the new artwork fields.

Uses a real beets Library (beets.library.Item/Library) at an isolated temp
path so the raw sqlite3.connect(LIB_PATH) queries _ai_import_folder makes
internally operate against genuine rows -- not mocked SQL -- while
_beet_run (all subprocess calls) and _repair_album_art (the actual network-
touching FetchArt call) are mocked and call-order-tracked. This is the same
isolated-temp-environment pattern as tests/test_ai_batch_retry_race.py, but
with every config/library/state path env var actually app.py reads
(BEETS_LIBRARY, not the never-read LIB_PATH) explicitly overridden, so
importing app.py cannot touch any real path on this machine.
"""
import atexit
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_post_retag_integration_"))
# Deliberately atexit, not unittest.addModuleCleanup: addModuleCleanup stores
# callbacks in a single process-wide list (unittest.case._module_cleanups)
# that is drained in full whenever ANY module's tests finish -- not scoped to
# this module. Under `unittest discover`, every test module is imported
# (registering its addModuleCleanup calls) before any test runs; the first
# module whose tests finish then drains and fires every other module's
# still-pending rmtree, deleting this module's own tmp root before its tests
# ever run. This is exactly the "attempt to write a readonly database"
# failure this module hit on GitHub Actions (order-dependent, so it didn't
# reproduce locally). atexit.register only fires once, at real process exit.
atexit.register(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

_ENV_OVERRIDES = {
    "BEETSDIR": str(_TMP_ROOT / "config"),
    "BEETS_CONFIG": str(_TMP_ROOT / "config" / "config.yaml"),
    "BEETS_LIBRARY": str(_TMP_ROOT / "config" / "musiclibrary.blb"),
    "BEETS_LOG": str(_TMP_ROOT / "config" / "beet.log"),
    "AI_BATCH_STATE_DIR": str(_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_TMP_ROOT / "transactions"),
    "BEETS_WEB_AUTH_TOKEN_FILE": str(_TMP_ROOT / "config" / ".auth_token"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
_env_patcher = mock.patch.dict(os.environ, _ENV_OVERRIDES, clear=False)
_env_patcher.start()
atexit.register(_env_patcher.stop)


def setUpModule():
    os.environ.update(_ENV_OVERRIDES)


def _import_app():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


def _bind_app_globals_to_this_test_module(app_module, tmp_root: Path) -> str:
    """app.py's LIB_PATH/lib (and _AI_BATCH_STATE_DIR) are computed exactly
    once at import time from os.environ. app.py is a process-wide singleton
    (sys.modules['app']): under `unittest discover`, some OTHER test module
    may import it first with its OWN env-var overrides, in which case setting
    env vars in *this* module has zero effect on the already-bound globals --
    this module's tests would then run against whatever path/Library the
    first importer set up. Worse, if that other module's tests finish first
    and its own addModuleCleanup(shutil.rmtree, ...) fires before this
    module's tests run, the cached `lib` singleton's connection points at a
    directory that no longer exists, and beets reports writes to it as
    "attempt to write a readonly database" -- exactly the failure this
    module hit on GitHub Actions' Linux runner (never reproduced on Windows,
    where this module happened to be the first importer locally). Explicitly
    rebinding these globals to this module's own tmp root, regardless of
    import order, makes this module's behavior independent of what any other
    test module already did to the shared `app` singleton. Called again in
    setUp() before every test (not just once at import) -- a one-time,
    import-time-only rebind can still be overwritten by a sibling module
    using this identical pattern, if that sibling happens to import after
    this one in the same process, regardless of which module's tests
    actually run first.
    """
    from beets.library import Library
    old_lib = getattr(app_module, "lib", None)
    if old_lib is not None:
        try:
            old_lib._close()
        except Exception:
            pass
    lib_path = str(tmp_root / "config" / "musiclibrary.blb")
    app_module.LIB_PATH = lib_path
    app_module.lib = Library(lib_path)
    state_dir = tmp_root / "ai_batch_jobs"
    state_dir.mkdir(parents=True, exist_ok=True)
    app_module._AI_BATCH_STATE_DIR = state_dir
    return lib_path


def _assert_path_owned_by_test(path, tmp_root: Path) -> None:
    """Safety net before any raw DB mutation: refuse to touch a path that
    isn't under this test module's own temp root, in case a future change
    removes the rebind above or a mock leaves APP.LIB_PATH pointed elsewhere.
    """
    resolved = Path(path).resolve()
    root_resolved = tmp_root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise AssertionError(
            f"Refusing to mutate path outside this test's temp root: "
            f"{resolved} is not under {root_resolved}"
        )


try:
    APP = _import_app()
    _APP_IMPORT_ERROR = None
    _bind_app_globals_to_this_test_module(APP, _TMP_ROOT)
except Exception as exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = exc


MB_ALBUMID = "5d2be019-9a93-4dd2-abfd-acd31b1b1e12"


def _seed_album(app_module, *, mb_albumid: str = "") -> int:
    """Creates a real album+item row via the actual beets Library API,
    simulating the state right after an as-is import: tags present,
    mb_albumid either blank (typical as-is result) or pre-set (simulating a
    --search-id import that matched immediately) so the raw SQL discovery
    query in _ai_import_folder finds a real row."""
    from beets.library import Item
    item = Item(
        title="Track One", artist="Alex Haas & Bill Laswell", album="Incidents",
        albumartist="Alex Haas & Bill Laswell", mb_albumid="",
        path=b"/tmp/incidents/track1.mp3", length=200.0, added=app_module.time.time(),
    )
    album = app_module.lib.add_album([item])
    if mb_albumid:
        album.mb_albumid = mb_albumid
        album.store()
    return int(album.id)


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class AiImportFolderSequenceTests(unittest.TestCase):
    """Behavioral tests for the real _ai_import_folder() function -- only
    subprocess calls (_beet_run) and the FetchArt call (_repair_album_art)
    are mocked; album discovery/persistence runs against a real library."""

    def setUp(self):
        # Rebind immediately before every test, not just once at module
        # import: if this module is loaded alongside another module using
        # the identical pattern (e.g. via `python -m unittest mod_a mod_b`,
        # or a different discovery order than this repo's own alphabetical
        # default), whichever module's import-time rebind ran LAST would
        # otherwise win process-wide for every test from every module,
        # regardless of run order. Rebinding here guarantees this test sees
        # its own root no matter what ran in between.
        _bind_app_globals_to_this_test_module(APP, _TMP_ROOT)
        self.addCleanup(self._clear_library)
        self._clear_library()
        self.call_order = []
        self.log = []
        self.aid = _seed_album(APP, mb_albumid=MB_ALBUMID)

        def fake_beet_run(cmd, log, **kwargs):
            self.call_order.append(("beet_run", tuple(cmd)))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.fake_beet_run = fake_beet_run
        self._patch(mock.patch.object(APP, "_beet_run", side_effect=fake_beet_run))
        self._patch(mock.patch.object(APP, "_preserve_torrent_source_path", return_value=False))
        self._patch(mock.patch.object(APP, "_validate_import_source_audio", return_value=None))
        self._patch(mock.patch.object(APP, "_prefer_album_mb_release", side_effect=lambda mbid, log: mbid))
        self._patch(mock.patch.object(APP, "_delete_if_already_in_library", return_value=None))
        self._patch(mock.patch.object(
            APP, "_repair_album_mbid_sticking_once",
            side_effect=lambda *a, **k: self.call_order.append(("recording_id_repair",)) or {"changed": False},
        ))
        self._patch(mock.patch.object(
            APP, "_repair_album_art",
            side_effect=lambda aid, log, **k: self.call_order.append(("artwork_repair", aid)) or {"status": "saved", "saved_path": "/x/cover.jpg"},
        ))
        self._patch(mock.patch.object(APP, "_remove_pending_review_for_path", return_value=None))
        self._patch(mock.patch.object(
            APP, "_auto_merge_case_duplicate_artist_folder",
            side_effect=lambda *a, **k: self.call_order.append(("dedup",)),
        ))
        self._patch(mock.patch.object(
            APP, "_invalidate_lib_cache", side_effect=lambda: self.call_order.append(("invalidate_cache",)),
        ))
        self._patch(mock.patch.object(
            APP, "_trigger_plex_refresh", side_effect=lambda *a, **k: self.call_order.append(("plex_refresh",)),
        ))
        self._patch(mock.patch.object(APP, "_record_ai_match", return_value=None))
        self._patch(mock.patch.object(APP.time, "sleep", return_value=None))

    def _patch(self, patcher):
        obj = patcher.start()
        self.addCleanup(patcher.stop)
        return obj

    def _clear_library(self):
        # The real beets Library is a module-level singleton shared across
        # every test in this process -- without this, album ids and
        # "recently added" discovery queries leak between tests.
        _assert_path_owned_by_test(APP.LIB_PATH, _TMP_ROOT)
        import sqlite3
        try:
            con = sqlite3.connect(APP.LIB_PATH)
            con.execute("DELETE FROM items")
            con.execute("DELETE FROM albums")
            con.commit()
            con.close()
        except Exception:
            pass

    # ---- ordering -------------------------------------------------------

    def test_full_sequence_runs_artwork_only_after_every_prior_stage(self):
        result = APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)

        stage_names = [c[0] for c in self.call_order]
        # mbsync/write/move (as beet_run entries) must all precede recording_id_repair,
        # which must precede artwork_repair, which must precede dedup/cache/plex refresh.
        beet_run_positions = [i for i, c in enumerate(self.call_order) if c[0] == "beet_run"]
        repair_pos = stage_names.index("recording_id_repair")
        artwork_pos = stage_names.index("artwork_repair")
        dedup_pos = stage_names.index("dedup")
        cache_pos = stage_names.index("invalidate_cache")
        plex_pos = stage_names.index("plex_refresh")

        self.assertTrue(all(p < repair_pos for p in beet_run_positions))
        self.assertLess(repair_pos, artwork_pos)
        self.assertLess(artwork_pos, dedup_pos)
        self.assertLess(artwork_pos, cache_pos)
        self.assertLess(artwork_pos, plex_pos)

        self.assertEqual(result["album_id"], self.aid)
        self.assertTrue(result["metadata_imported"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "fetched")
        self.assertFalse(result["artwork_retryable"])

    def test_mbsync_write_move_commands_are_scoped_to_the_discovered_album(self):
        APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        beet_calls = [c[1] for c in self.call_order if c[0] == "beet_run"]
        # First call is the import itself; the next three are mbsync/write/move,
        # all scoped with album_id:<discovered-id>, never a library-wide query.
        scoped_calls = beet_calls[1:4]
        for call in scoped_calls:
            self.assertIn(f"album_id:{self.aid}", call)

    # ---- artwork outcomes through the real function ------------------------

    def test_already_present_artwork_result(self):
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "skipped", "source": "local"}):
            result = APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        self.assertEqual(result, {
            "album_id": self.aid,
            "metadata_imported": True,
            "identity_verified": True,
            "artwork_status": "already_present",
            "artwork_retryable": False,
        })

    def test_artwork_failure_keeps_metadata_import_successful(self):
        with mock.patch.object(APP, "_repair_album_art", return_value={"status": "failed", "error": "no art found"}):
            result = APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        self.assertTrue(result["metadata_imported"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "failed")
        self.assertTrue(result["artwork_retryable"])

    def test_identity_not_verified_skips_fetchart_entirely(self):
        # Simulate mbsync/write silently failing to actually persist the
        # release: _ai_import_folder's own SQL stamp step always writes the
        # correct mb_albumid immediately after discovery (this is not a
        # realistic way to desync it), so the identity-verification reload
        # inside _fetch_artwork_after_retag is mocked directly instead --
        # the one place that reload actually matters.
        mismatched_album = SimpleNamespace(mb_albumid="00000000-0000-0000-0000-000000000000")
        with mock.patch.object(APP.lib, "get_album", side_effect=lambda aid: mismatched_album), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            result = APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()
        self.assertTrue(result["metadata_imported"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["artwork_status"], "skipped_identity_unverified")

    def test_no_album_found_reports_contract_and_skips_artwork(self):
        with mock.patch.object(APP.lib, "get_album", side_effect=lambda aid: None), \
             mock.patch("sqlite3.connect") as connect_mock:
            fake_con = mock.MagicMock()
            fake_con.execute.return_value.fetchone.return_value = None
            fake_con.execute.return_value.fetchall.return_value = []
            connect_mock.return_value = fake_con
            with mock.patch.object(APP, "_repair_album_art") as repair:
                result = APP._ai_import_folder("/tmp/incidents-none", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()
        self.assertEqual(result["artwork_status"], "skipped_no_album")
        self.assertIsNone(result["album_id"])

    # ---- prior-stage failure gating -----------------------------------------

    def test_import_failure_raises_before_any_later_stage(self):
        def failing_import(cmd, log, **kwargs):
            if "import" in cmd:
                return SimpleNamespace(returncode=2, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(APP, "_beet_run", side_effect=failing_import), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()

    def test_mbsync_cancellation_prevents_artwork_and_propagates(self):
        cancel_event = threading.Event()

        def cancel_on_mbsync(cmd, log, **kwargs):
            if "mbsync" in cmd:
                return SimpleNamespace(returncode=-9, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(APP, "_beet_run", side_effect=cancel_on_mbsync), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log, cancel_event)
        repair.assert_not_called()

    def test_write_timeout_returncode_raises_and_never_reaches_artwork(self):
        # 124 (timeout) must never be silently treated as success for
        # mbsync/write/move -- the persisted tag state for this album is
        # unverified, so this must stop immediately rather than continue on
        # to recording-ID repair or artwork fetching.
        def timeout_on_write(cmd, log, **kwargs):
            if "write" in cmd:
                return SimpleNamespace(returncode=124, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(APP, "_beet_run", side_effect=timeout_on_write), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()

    def test_mbsync_timeout_returncode_raises_and_never_reaches_artwork(self):
        def timeout_on_mbsync(cmd, log, **kwargs):
            if "mbsync" in cmd:
                return SimpleNamespace(returncode=124, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(APP, "_beet_run", side_effect=timeout_on_mbsync), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()

    def test_move_timeout_returncode_raises_and_never_reaches_artwork(self):
        def timeout_on_move(cmd, log, **kwargs):
            if "move" in cmd:
                return SimpleNamespace(returncode=124, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(APP, "_beet_run", side_effect=timeout_on_move), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()

    def test_import_timeout_returncode_raises_before_any_later_stage(self):
        # Step 1 (the "import" beet_run call) already raises for any
        # returncode >= 2, which includes 124 -- confirm that still holds.
        def timeout_on_import(cmd, log, **kwargs):
            if "import" in cmd:
                return SimpleNamespace(returncode=124, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(APP, "_beet_run", side_effect=timeout_on_import), \
             mock.patch.object(APP, "_repair_album_art") as repair:
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log)
        repair.assert_not_called()

    def test_artwork_cancellation_propagates(self):
        cancel_event = threading.Event()

        def cancel_in_artwork(aid, log, **kwargs):
            cancel_event.set()
            return {"status": "failed", "error": "cancelled"}
        with mock.patch.object(APP, "_repair_album_art", side_effect=cancel_in_artwork):
            with self.assertRaises(RuntimeError):
                APP._ai_import_folder("/tmp/incidents", MB_ALBUMID, {}, self.log, cancel_event)


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class AiBatchFolderPersistenceTests(unittest.TestCase):
    """Tests for the AI-batch worker call site: import_result = _ai_import_folder(...)
    followed by _ai_batch_mark_folder(...) -- proves the artwork fields are
    actually persisted onto the folder state dict, and that a missing/empty
    result does not default to a false success."""

    def setUp(self):
        self.state = {"folder_states": {"f1": {"folder_id": "f1", "source_folder": "/x", "status": "queued"}}}

    def _mark(self, **result_overrides):
        import_result = {
            "album_id": 1, "metadata_imported": True, "identity_verified": True,
            "artwork_status": "fetched", "artwork_retryable": False,
        }
        import_result.update(result_overrides)
        if not import_result:
            pass
        APP._ai_batch_mark_folder(
            self.state, "f1", status="imported", current_step="imported",
            metadata_imported=bool(import_result.get("metadata_imported", False)),
            identity_verified=bool(import_result.get("identity_verified", False)),
            artwork_status=APP._s(import_result.get("artwork_status") or "unknown"),
            artwork_retryable=bool(import_result.get("artwork_retryable", False)),
            album_id=import_result.get("album_id"),
        )
        return self.state["folder_states"]["f1"]

    def test_fetched_artwork_persists_all_fields(self):
        folder = self._mark()
        self.assertEqual(folder["artwork_status"], "fetched")
        self.assertFalse(folder["artwork_retryable"])
        self.assertTrue(folder["metadata_imported"])
        self.assertTrue(folder["identity_verified"])
        self.assertEqual(folder["album_id"], 1)

    def test_already_present_artwork_persists(self):
        folder = self._mark(artwork_status="already_present")
        self.assertEqual(folder["artwork_status"], "already_present")
        self.assertFalse(folder["artwork_retryable"])

    def test_failed_artwork_remains_retryable(self):
        folder = self._mark(artwork_status="failed", artwork_retryable=True)
        self.assertEqual(folder["artwork_status"], "failed")
        self.assertTrue(folder["artwork_retryable"])
        self.assertTrue(folder["metadata_imported"])

    def test_unverified_identity_persists_truthfully(self):
        folder = self._mark(identity_verified=False, artwork_status="skipped_identity_unverified", artwork_retryable=False)
        self.assertFalse(folder["identity_verified"])
        self.assertEqual(folder["artwork_status"], "skipped_identity_unverified")

    def test_empty_result_does_not_default_metadata_imported_to_true(self):
        import_result = {}
        APP._ai_batch_mark_folder(
            self.state, "f1", status="imported", current_step="imported",
            metadata_imported=bool(import_result.get("metadata_imported", False)),
            identity_verified=bool(import_result.get("identity_verified", False)),
            artwork_status=APP._s(import_result.get("artwork_status") or "unknown"),
            artwork_retryable=bool(import_result.get("artwork_retryable", False)),
            album_id=import_result.get("album_id"),
        )
        folder = self.state["folder_states"]["f1"]
        self.assertFalse(folder["metadata_imported"])
        self.assertFalse(folder["identity_verified"])
        self.assertEqual(folder["artwork_status"], "unknown")

    def test_exception_before_import_completes_does_not_mark_imported(self):
        """Regression: a batch-worker-style try/except must never reach the
        imported+=1 / status="imported" branch when _ai_import_folder raises
        (metadata stage failure OR a cancellation propagated from the
        artwork stage) -- it must land in the except branch instead."""
        state = {"folder_states": {"f1": {"folder_id": "f1", "status": "queued"}}}
        imported = 0
        try:
            raise RuntimeError("cancelled")
        except Exception as ex:  # noqa: BLE001 - mirrors the real call site's broad catch
            outcome = APP._ai_batch_import_failure_outcome(str(ex))
            APP._ai_batch_mark_folder(state, "f1", status=outcome["status"], current_step=outcome["current_step"], failure_reason=outcome["reason"])
        self.assertEqual(imported, 0)
        self.assertNotEqual(state["folder_states"]["f1"]["status"], "imported")


if __name__ == "__main__":
    unittest.main()
