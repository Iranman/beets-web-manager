"""D1/D10: every import route goes through Beets' own importer.

/api/import -> reimport_source() -> beet import -q (autotag, quiet_fallback)
import-with-id / reimport-disk -> plan/apply_confirmed_import() -> beet import
-q --search-id <Release ID> (autotag, quiet_fallback skip).

A preserved torrent source is copied, never moved. A plugin refusal comes back
as a clean ok=false and the transaction ends Failed, never a stray Preview.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

import app as APP
import backend.composite_workflows as cw
import routes_import
from backend.beets_adapter import BeetsAdapter, BeetsAdapterBadRequestError, BeetsAdapterTimeoutError
from backend.transaction_engine import TransactionStore

try:
    from _app_family import patch_app_family
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"


def _adapter(new_album=None, existing=()):
    """find_all_albums_by_mb_albumid answers the pre-import set, then adds new_album."""
    ad = mock.MagicMock()
    rows = [list(existing), list(existing) + ([new_album] if new_album else [])]
    ad.find_all_albums_by_mb_albumid.side_effect = lambda _rid: rows.pop(0) if len(rows) > 1 else rows[0]
    ad.find_all_items_by_album_id.return_value = [{"id": 31}, {"id": 32}]
    return ad


class ConfirmedImportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TransactionStore(tmp.name)

    def _run(self, ad, **meta):
        payload = {"source_folder": "/downloads/A", "mb_albumid": REL, **meta}
        plan = cw.plan_confirmed_import(payload, store=self.store)
        res = cw.apply_confirmed_import(plan["operation_id"], adapter=ad, store=self.store,
                                        acceptance_failpoint=None, timeout=60.0)
        return res, self.store.get(plan["operation_id"])

    def test_uses_beets_autotagger_pinned_to_the_release_and_copies_by_default(self):
        ad = _adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG})
        res, tx = self._run(ad, mb_releasegroupid=RG)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["album_id"], res["item_ids"]), (9, [31, 32]))
        kw = ad.run_import.call_args.kwargs
        self.assertEqual(kw["paths"], "/downloads/A")
        self.assertTrue(kw["autotag"])
        self.assertEqual(kw["search_ids"], [REL])
        self.assertEqual(kw["quiet_fallback"], "skip")
        self.assertEqual((kw["copy"], kw["move"]), (True, False))
        self.assertEqual(tx["status"], "Completed")

    def test_move_and_in_place_modes(self):
        ad = _adapter({"id": 9, "mb_albumid": REL})
        self._run(ad, use_move=True)
        self.assertEqual((ad.run_import.call_args.kwargs["copy"], ad.run_import.call_args.kwargs["move"]), (False, True))
        ad = _adapter({"id": 9, "mb_albumid": REL})
        self._run(ad, use_move=True, in_place=True)
        self.assertEqual((ad.run_import.call_args.kwargs["copy"], ad.run_import.call_args.kwargs["move"]), (False, False))

    def test_plugin_refusal_is_clean_and_leaves_no_preview(self):
        ad = _adapter()
        ad.run_import.side_effect = BeetsAdapterBadRequestError(
            "Beets request failed", error_code="AUTOTAG_NOT_ALLOWED", response_data={"raw": "secret-ish body"})
        res, tx = self._run(ad)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "autotag_not_allowed")
        self.assertFalse(res["mutated"])
        self.assertNotIn("secret-ish", res["error"])
        self.assertEqual(tx["status"], "Failed")

    def test_timeout_and_unexpected_errors_end_failed(self):
        for exc in (BeetsAdapterTimeoutError("slow"), KeyError("x")):
            ad = _adapter()
            ad.run_import.side_effect = exc
            res, tx = self._run(ad)
            self.assertFalse(res["ok"])
            self.assertEqual(tx["status"], "Failed")

    def test_invalid_plan_never_calls_beets(self):
        ad = _adapter()
        plan = cw.plan_confirmed_import({"source_folder": "/downloads/A"}, store=self.store)
        res = cw.apply_confirmed_import(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(res["code"], "invalid_plan")
        ad.run_import.assert_not_called()
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Failed")

    def test_beets_skip_is_not_imported_and_existing_rows_are_not_claimed(self):
        ad = _adapter(None, existing=[{"id": 4, "mb_albumid": REL}])
        res, tx = self._run(ad)
        self.assertEqual(res["code"], "not_imported")
        self.assertEqual(tx["status"], "Failed")

    def test_release_group_mismatch_fails(self):
        ad = _adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": "33333333-3333-3333-3333-333333333333"})
        res, tx = self._run(ad, mb_releasegroupid=RG)
        self.assertEqual(res["code"], "release_group_mismatch")
        self.assertTrue(res["mutated"])
        self.assertEqual(tx["status"], "Failed")

    def test_second_apply_is_refused(self):
        ad = _adapter({"id": 9, "mb_albumid": REL})
        plan = cw.plan_confirmed_import({"source_folder": "/downloads/A", "mb_albumid": REL}, store=self.store)
        cw.apply_confirmed_import(plan["operation_id"], adapter=ad, store=self.store)
        again = cw.apply_confirmed_import(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(again["code"], "not_applicable")
        self.assertEqual(ad.run_import.call_count, 1)


class ReimportSourceTests(unittest.TestCase):
    def test_copy_wins_over_move(self):
        ad = mock.MagicMock()
        res = cw.reimport_source("/downloads/A", {"copy": True, "move": True}, adapter=ad)
        kw = ad.run_import.call_args.kwargs
        self.assertTrue(res["ok"])
        self.assertEqual((kw["copy"], kw["move"], kw["autotag"]), (True, False, True))

    def test_move_and_search_id_and_fallback(self):
        ad = mock.MagicMock()
        cw.reimport_source("/downloads/A", {"move": True, "search_id": REL, "quiet_fallback": "asis"}, adapter=ad)
        kw = ad.run_import.call_args.kwargs
        self.assertEqual((kw["copy"], kw["move"]), (False, True))
        self.assertEqual((kw["search_ids"], kw["quiet_fallback"]), ([REL], "asis"))

    def test_bad_fallback_and_refusal(self):
        ad = mock.MagicMock()
        self.assertEqual(cw.reimport_source("/d/A", {"quiet_fallback": "x"}, adapter=ad)["code"], "invalid_fallback")
        ad.run_import.side_effect = BeetsAdapterBadRequestError("x", error_code="PATH_NOT_ALLOWED")
        self.assertEqual(cw.reimport_source("/d/A", {}, adapter=ad)["code"], "path_not_allowed")


    def test_skipped_albums_are_reported_never_dropped(self):
        def ad_with(albums, result):
            ad = mock.MagicMock()
            ad.get_stats.side_effect = [{"albums": n} for n in albums]
            ad.run_import.return_value = result
            return ad
        # The plugin names the folders Beets skipped.
        res = cw.reimport_source("/downloads/A", {}, adapter=ad_with([0, 1], {"skipped_paths": ["/downloads/A/B"]}))
        self.assertEqual((res["albums_imported"], res["not_matched"], res["not_matched_known"]),
                         (1, ["/downloads/A/B"], True))
        # Nothing was added: the whole source is left in place for review.
        res = cw.reimport_source("/downloads/A", {}, adapter=ad_with([3, 3], {"success": True}))
        self.assertEqual((res["not_matched"], res["not_matched_known"]), (["/downloads/A"], True))
        # Some added, plugin silent about skips: flagged as unknown, not claimed complete.
        res = cw.reimport_source("/downloads/A", {}, adapter=ad_with([3, 4], {"success": True}))
        self.assertEqual((res["not_matched"], res["not_matched_known"]), ([], False))

    def test_default_fallback_is_skip(self):
        ad = mock.MagicMock()
        cw.reimport_source("/downloads/A", {}, adapter=ad)
        self.assertEqual(ad.run_import.call_args.kwargs["quiet_fallback"], "skip")


class AdapterPayloadTests(unittest.TestCase):
    def test_native_options_only_sent_when_given(self):
        ad = BeetsAdapter(base_url="http://beets:8337")
        with mock.patch.object(ad, "_request", return_value={}) as req:
            ad.run_import("/downloads/A")
            self.assertNotIn("search_ids", req.call_args.kwargs["json_data"])
            ad.run_import("/downloads/A", autotag=True, search_ids=[REL], quiet_fallback="skip", timeout=5)
            body = req.call_args.kwargs["json_data"]
        self.assertEqual((body["search_ids"], body["quiet_fallback"]), ([REL], "skip"))
        self.assertEqual(req.call_args.kwargs["timeout"], 5)


class ApiImportRouteTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = APP.app.test_client()
        self.logs = []

    def _post(self, preserved, **payload):
        def run_now(fn, label=""):
            self.job_result = fn(self.logs)
            return types.SimpleNamespace(job_id="j1")
        captured = {}

        def fake_reimport(path, beets_options=None, timeout=0):
            captured.update(beets_options)
            return {"ok": True, "albums_imported": 1, "not_matched": ["/downloads/A/Unmatched"],
                    "not_matched_known": True}
        src = os.path.join(self.tmp.name, "A")
        with mock.patch.object(routes_import, "_resolve_import_source_path", return_value=(src, None)), \
             mock.patch.object(routes_import, "_preserve_torrent_source_path", return_value=preserved), \
             mock.patch.object(routes_import, "_validate_import_source_audio"), \
             mock.patch.object(routes_import, "_invalidate_lib_cache", create=True), \
             mock.patch.object(routes_import.jobs, "start_python", side_effect=run_now), \
             mock.patch.object(APP.composite_workflows, "reimport_source", side_effect=fake_reimport):
            res = self.client.post("/api/import", json={"path": src, **payload})
        return res, captured

    def test_preserved_torrent_source_is_copied(self):
        res, opts = self._post(True)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual((opts["copy"], opts["move"]), (True, False))

    def test_fallback_defaults_to_skip_and_asis_only_on_request(self):
        res, opts = self._post(False)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(opts["quiet_fallback"], "skip")
        self.assertEqual(self.job_result["not_matched"],
                         [{"path": "/downloads/A/Unmatched", "status": "not matched; left in place for review"}])
        self.assertTrue(any("not matched; left in place for review" in line for line in self.logs))
        _, opts = self._post(False, fallback="asis")
        self.assertEqual(opts["quiet_fallback"], "asis")

    def test_preserved_torrent_source_move_is_refused(self):
        res, opts = self._post(True, move=True)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(opts, {})

    def test_move_honoured_for_unprotected_source(self):
        res, opts = self._post(False, move=True, search_id=f"https://musicbrainz.org/release/{REL}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((opts["copy"], opts["move"], opts["search_id"]), (False, True, REL))

    def test_bad_options_rejected_before_a_job(self):
        self.assertEqual(self._post(False, fallback="nope")[0].status_code, 400)
        self.assertEqual(self._post(False, search_id="not-an-id")[0].status_code, 400)


class ReimportDiskRouteTests(unittest.TestCase):
    """reimport-disk binds to the confirmed-import family (KNOWN_UNBOUND_CALLS shrank)."""

    def test_routes_use_the_confirmed_import_family_and_the_copy_rule(self):
        import inspect
        import backend.import_service as isvc
        disk = inspect.getsource(isvc.start_reimport_disk)
        self.assertIn("composite_workflows.plan_confirmed_import(", disk)
        self.assertIn("not _preserve_torrent_source_path(aldir)", disk)
        self.assertNotIn("composite_workflows.reimport_source(", disk)
        with_id = inspect.getsource(isvc.start_folder_import_with_id)
        self.assertIn('"use_move": import_mode == "--move"', with_id)
        self.assertIn('if not apply_res.get("ok"):', with_id)


class ConfirmedImportJobTests(unittest.TestCase):
    """reimport-disk and import-with-id after Beets imported the confirmed
    Release: no Web Manager retag (ARCH-024), and a verification failure
    never removes the rows Beets just created."""

    RG_OTHER = "33333333-3333-3333-3333-333333333333"
    RETAG_AND_REMOVAL = ("plan_album_mb_track_repair", "apply_album_mb_track_repair", "relocate_album",
                         "update_album_metadata", "remove_album_rows_after_failed_import",
                         "plan_album_cleanup", "apply_album_cleanup", "plan_playlist_media_cleanup")

    def setUp(self):
        import contextlib
        import backend.import_service as isvc
        self.isvc = isvc
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TransactionStore(tmp.name)
        self.result = {}

        def run_now(fn, label="", metadata=None):
            log = []
            try:
                self.result = {"status": "completed", "result": fn(log), "log": log}
            except Exception as ex:
                self.result = {"status": "failed", "error": str(ex), "log": log}
            return types.SimpleNamespace(job_id="job-1")

        tracklist = {"ok": True, "release_group": RG, "tracks": []}
        for target, name, kw in (
            (isvc.jobs, "start_python", {"side_effect": run_now}),
            (isvc.job_contract, "held", {"side_effect": lambda *a, **k: contextlib.nullcontext()}),
            (isvc, "_fetch_mb_release_tracklist", {"return_value": tracklist}),
            (isvc, "_match_tracks_from_mb", {"side_effect": AssertionError("retag ran")}),
            (isvc, "_repair_album_mbid_sticking_once", {"side_effect": AssertionError("retag ran")}),
            (isvc, "_validate_import_source_audio", {}),
            (isvc, "_remove_pending_review_for_path", {}),
            (isvc, "_invalidate_lib_cache", {}),
            (isvc, "_trigger_plex_refresh", {}),
            (isvc, "_record_recent_import", {}),
            (isvc, "_maybe_queue_review", {"create": True}),
            (isvc, "_queue_folder_for_manual_review", {"return_value": True}),
            (isvc, "_library_album_ids_for_folder", {"return_value": []}),
            (isvc, "_auto_merge_case_duplicate_artist_folder", {}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cw_calls = []
        for name in self.RETAG_AND_REMOVAL:
            patcher = mock.patch.object(isvc.composite_workflows, name,
                                        side_effect=lambda *a, _n=name, **k: self.cw_calls.append(_n) or {"ok": False})
            patcher.start()
            self.addCleanup(patcher.stop)
        plan, apply = cw.plan_confirmed_import, cw.apply_confirmed_import
        for name, fn in (("plan_confirmed_import", lambda *a, **k: plan(*a, store=self.store, **k)),
                         ("apply_confirmed_import", lambda *a, **k: apply(*a, store=self.store, adapter=self.ad, **k))):
            patcher = mock.patch.object(isvc.composite_workflows, name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        art = mock.patch.object(isvc.composite_workflows, "fetch_and_embed_album_art", return_value={"ok": True})
        art.start()
        self.addCleanup(art.stop)
        getalb = mock.patch.object(isvc.composite_workflows, "get_album", return_value=None)
        getalb.start()
        self.addCleanup(getalb.stop)

    def _reimport_disk(self, album_rg):
        isvc = self.isvc
        self.ad = _adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": album_rg})
        aldir = str(isvc.MUSIC_ROOT) + "/Artist/Album"
        with mock.patch.object(isvc.composite_workflows, "inspect_import_source",
                               return_value={"ok": True, "path": aldir, "audio_file_count": 2}),              mock.patch.object(isvc, "_resolve_album_release_for_import", return_value=REL),              mock.patch.object(isvc, "_folder_release_preflight", return_value={"ok": True, "matches": 2, "expected": 2}):
            body, code = isvc.start_reimport_disk({"aldir": aldir, "mb_albumid": REL, "skip_import_lock": True})
        self.assertEqual(code, 200, body)
        return self.result

    def _import_with_id(self, album_rg):
        isvc = self.isvc
        self.ad = _adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": album_rg})
        src = "/downloads/Artist - Album"
        with mock.patch.object(isvc, "_resolve_import_review_source_path", return_value=(src, None)),              mock.patch.object(isvc, "_preserve_torrent_source_path", return_value=True),              mock.patch.object(isvc, "_prefer_album_mb_release", side_effect=lambda rid, log: rid),              mock.patch.object(isvc, "_beet_import_timeout", return_value=60):
            body, code = isvc.start_folder_import_with_id({"path": src, "mb_albumid": REL})
        self.assertEqual(code, 200, body)
        return self.result

    def _assert_kept_and_not_retagged(self):
        self.assertEqual(self.cw_calls, [])  # no retag, relocate or row removal
        kw = self.ad.run_import.call_args.kwargs
        self.assertEqual((kw["search_ids"], kw["autotag"], kw["quiet_fallback"]), ([REL], True, "skip"))

    def test_reimport_disk_keeps_the_album_and_verifies_the_release_group(self):
        res = self._reimport_disk(RG)
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["result"]["album_ids"], [9])
        kw = self.ad.run_import.call_args.kwargs
        self.assertEqual((kw["copy"], kw["move"]), (False, False))  # library folder: in place
        self._assert_kept_and_not_retagged()

    def test_reimport_disk_verification_mismatch_keeps_rows_and_fails(self):
        res = self._reimport_disk(self.RG_OTHER)
        self.assertEqual(res["status"], "failed")
        self.assertIn("different Release Group", res["error"])
        self._assert_kept_and_not_retagged()
        (tx,) = [t for t in self.store.list()[0] if t.get("operation_type") == "Import"]
        self.assertEqual(tx["status"], "Failed")

    def test_import_with_id_keeps_the_album_and_verifies_the_release_group(self):
        res = self._import_with_id(RG)
        self.assertEqual(res["status"], "completed", res)
        kw = self.ad.run_import.call_args.kwargs
        self.assertEqual((kw["copy"], kw["move"]), (True, False))  # torrent source copied
        self._assert_kept_and_not_retagged()

    def test_import_with_id_verification_mismatch_keeps_rows_and_fails(self):
        res = self._import_with_id(self.RG_OTHER)
        self.assertEqual(res["status"], "failed")
        self.assertIn("different Release Group", res["error"])
        self._assert_kept_and_not_retagged()
        (tx,) = [t for t in self.store.list()[0] if t.get("operation_type") == "Import"]
        self.assertEqual(tx["status"], "Failed")


if __name__ == "__main__":
    unittest.main()
