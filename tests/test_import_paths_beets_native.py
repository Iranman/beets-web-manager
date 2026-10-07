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
            fn(self.logs)
            return types.SimpleNamespace(job_id="j1")
        captured = {}

        def fake_reimport(path, beets_options=None, timeout=0):
            captured.update(beets_options)
            return {"ok": True}
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

if __name__ == "__main__":
    unittest.main()
