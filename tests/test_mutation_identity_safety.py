"""Regression tests for the mutation identity/safety wave: MI-1, MI-2, MI-9,
MI-14, MI-18, BA-2, BA-20, #218 and #224. Each test fails on the code it
fixes. Temp stores, temp directories and fake adapters only."""

import ast
import inspect
import json
import os
import unittest
from unittest import mock

import backend.composite_workflows as cw
import backend.transaction_engine as te
from tests.test_wave0_s1_containment import FakeAdapter, _Env

REL_A = "aaaaaaaa-0000-4000-8000-00000000000a"
REL_A2 = "aaaaaaaa-0000-4000-8000-0000000000a2"
REL_B = "bbbbbbbb-0000-4000-8000-00000000000b"
RG_A = "aaaaaaaa-1111-4000-8000-00000000000a"
RG_B = "bbbbbbbb-1111-4000-8000-00000000000b"
REC_1 = "cccccccc-0000-4000-8000-000000000001"
RG_OF = {REL_A: RG_A, REL_A2: RG_A, REL_B: RG_B}


class MutAdapter(FakeAdapter):
    """FakeAdapter whose modify/mbsync really change the fake rows."""

    def modify(self, fields, item_ids=None, album_ids=None, write=True, move=False, **_kw):
        self.calls.append(("modify", dict(fields), list(item_ids or []), list(album_ids or [])))
        for iid in item_ids or []:
            self.items[int(iid)].update(fields)
        for aid in album_ids or []:
            self.albums[int(aid)].update(fields)
        return {"ok": True}

    def mbsync(self, album_ids=None, write=True, move=False, **_kw):
        self.calls.append(("mbsync", list(album_ids or [])))
        for aid in album_ids or []:  # mbsync rewrites metadata from MusicBrainz
            self.albums[aid]["album"] = "Synced Title"
            for it in self.find_all_items_by_album_id(aid):
                it["title"] = "Synced Track"
        return {"ok": True}

    def writes(self):
        return [c for c in self.calls if c[0] in ("modify", "mbsync")]


class _IdentityEnv(_Env):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(cw, "_release_group_for_release", side_effect=lambda rel: RG_OF.get(rel, ""))
        p.start()
        self.addCleanup(p.stop)

    def adapter(self, rel=REL_A, rg=RG_A):
        return MutAdapter(
            albums={1: {"id": 1, "album": "Old Title", "albumartist": "A", "mb_albumid": rel, "mb_releasegroupid": rg}},
            items={11: {"id": 11, "album_id": 1, "title": "Old Track", "mb_trackid": "", "path": self.media("A/1.flac")}},
        )


class MbTrackRepairTests(_IdentityEnv):
    """MI-1: apply does exactly what the plan validated."""

    def plan(self, ad, **payload):
        return cw.plan_album_mb_track_repair({"album_id": 1, **payload}, adapter=ad, store=self.store)

    def test_unsupported_options_are_refused_not_widened(self):
        for opt in ({"target_tracks": [11]}, {"zero_unmatched": True}):
            res = self.plan(self.adapter(), **opt)
            self.assertEqual(res.get("code"), "repair_option_unsupported", res)

    def test_release_from_other_release_group_is_refused(self):
        res = self.plan(self.adapter(), mb_albumid=REL_B)
        self.assertEqual(res.get("code"), "repair_identity_mismatch", res)

    def test_release_group_not_established_without_flag(self):
        res = self.plan(self.adapter(rg=""))
        self.assertEqual(res.get("code"), "repair_rg_not_established", res)

    def test_new_release_is_written_with_its_release_group(self):
        ad = self.adapter()
        res = cw.apply_album_mb_track_repair(self.plan(ad, mb_albumid=REL_A2)["operation_id"], adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertIn(("modify", {"mb_albumid": REL_A2, "mb_releasegroupid": RG_A}, [], [1]), ad.calls)
        self.assertEqual(ad.albums[1]["mb_releasegroupid"], RG_A)

    def test_track_mbids_are_honored(self):
        ad = self.adapter()
        op = self.plan(ad, track_mbids={"11": REC_1})["operation_id"]
        self.assertTrue(cw.apply_album_mb_track_repair(op, adapter=ad, store=self.store)["ok"])
        self.assertEqual(ad.items[11]["mb_trackid"], REC_1)

    def test_stale_plan_is_refused_without_writes(self):
        ad = self.adapter()
        op = self.plan(ad)["operation_id"]
        ad.albums[1]["mb_albumid"] = REL_A2
        res = cw.apply_album_mb_track_repair(op, adapter=ad, store=self.store)
        self.assertEqual(res.get("code"), "repair_plan_stale", res)
        self.assertEqual(ad.writes(), [])
        self.assertEqual(self.store.get(op)["status"], "Failed")

    def test_cancelled_transaction_is_never_applied(self):
        ad = self.adapter()
        op = self.plan(ad)["operation_id"]
        self.store.transition(op, "Preview", "Cancelled")
        res = cw.apply_album_mb_track_repair(op, adapter=ad, store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(ad.writes(), [])
        self.assertEqual(self.store.get(op)["status"], "Cancelled")

    def test_rollback_restores_captured_values(self):
        ad = self.adapter()
        op = self.plan(ad, track_mbids={"11": REC_1})["operation_id"]
        self.assertTrue(cw.apply_album_mb_track_repair(op, adapter=ad, store=self.store)["ok"])
        res = cw.rollback_album_mb_track_repair(op, adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(ad.albums[1]["album"], "Old Title")
        self.assertEqual((ad.items[11]["title"], ad.items[11]["mb_trackid"]), ("Old Track", ""))
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")


class AlbumMetadataTests(_IdentityEnv):
    """MI-2: no Release ID without its Release Group; never blank the RG."""

    def plan(self, ad, updates, **kw):
        return cw.plan_album_metadata({"album_id": 1, "updates": updates, **kw}, adapter=ad, store=self.store)

    def test_release_id_gets_its_resolved_release_group(self):
        ad = self.adapter()
        plan = self.plan(ad, {"mb_albumid": REL_A2})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(cw.apply_album_metadata(plan["operation_id"], adapter=ad, store=self.store)["ok"])
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_A2, RG_A))

    def test_blank_release_group_is_refused(self):
        self.assertEqual(self.plan(self.adapter(), {"mb_releasegroupid": ""}).get("code"), "release_group_blank")

    def test_other_release_group_needs_it_stated(self):
        self.assertEqual(self.plan(self.adapter(), {"mb_albumid": REL_B}).get("code"), "repair_identity_mismatch")
        self.assertTrue(self.plan(self.adapter(), {"mb_albumid": REL_B, "mb_releasegroupid": RG_B})["ok"])

    def test_unverifiable_release_is_refused(self):
        unknown = "dddddddd-0000-4000-8000-00000000000d"
        self.assertEqual(self.plan(self.adapter(), {"mb_albumid": unknown}).get("code"), "release_group_unverified")

    def test_per_item_identity_write_is_refused(self):
        res = self.plan(self.adapter(), {}, item_updates={"11": {"mb_albumid": REL_A2}})
        self.assertEqual(res.get("code"), "item_identity_write_refused")

    def test_stale_plan_is_refused_without_writes(self):
        ad = self.adapter()
        op = self.plan(ad, {"album": "New Title"})["operation_id"]
        ad.albums[1]["album"] = "Edited Elsewhere"
        res = cw.apply_album_metadata(op, adapter=ad, store=self.store)
        self.assertEqual(res.get("code"), "metadata_plan_stale", res)
        self.assertEqual(ad.writes(), [])

    def test_rollback_restores_captured_values(self):
        ad = self.adapter()
        op = self.plan(ad, {"album": "New Title"}, item_updates={"11": {"title": "New Track"}})["operation_id"]
        self.assertTrue(cw.apply_album_metadata(op, adapter=ad, store=self.store)["ok"])
        self.assertTrue(cw.rollback_album_metadata(op, adapter=ad, store=self.store)["ok"])
        self.assertEqual((ad.albums[1]["album"], ad.items[11]["title"]), ("Old Title", "Old Track"))


class OperatorReleaseAndCodeqlTests(_IdentityEnv):
    """QA #243 findings 1, 2 and 4, and F5."""

    def plan(self, ad, updates, **kw):
        return cw.plan_album_metadata({"album_id": 1, "updates": updates, **kw}, adapter=ad, store=self.store)

    def test_operator_selected_release_may_change_release_group(self):
        ad = self.adapter()
        plan = self.plan(ad, {"mb_albumid": REL_B}, release_selected_by_operator=True)
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(cw.apply_album_metadata(plan["operation_id"], adapter=ad, store=self.store)["ok"])
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_B, RG_B))

    def test_release_group_only_write_must_agree_with_release(self):
        ad = self.adapter()
        self.assertFalse(self.plan(ad, {"mb_releasegroupid": RG_B}).get("ok"))
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_A, RG_A))
        self.assertTrue(self.plan(ad, {"mb_releasegroupid": RG_A})["ok"])

    def test_manual_match_route_marks_release_operator_selected(self):
        import app as app_module  # noqa: F401  (registers routes)
        import routes_library
        ad = self.adapter()
        captured = {}
        plan = {"matched_count": 1, "actual_count": 1, "expected_count": 1, "unmatched_items": []}
        with mock.patch.object(cw, "beets_adapter", ad), \
             mock.patch.object(cw, "_get_store", return_value=self.store), \
             mock.patch.object(routes_library.lib, "get_album",
                               return_value=mock.Mock(albumartist="A", album="Old Title")), \
             mock.patch.object(routes_library, "_resolve_mb_release_id", return_value=REL_B), \
             mock.patch.object(routes_library, "_album_mb_match_plan", return_value=plan), \
             mock.patch.object(routes_library.jobs, "start_python",
                               side_effect=lambda fn, **kw: (captured.__setitem__("fn", fn), mock.Mock(job_id="j"))[1]):
            with routes_library.app.test_request_context(
                    "/api/albums/1/match", method="POST",
                    data=json.dumps({"mb_id": REL_B}), content_type="application/json"):
                routes_library.match_album(1)
            job_log = []
            try:
                captured["fn"](job_log)
                job_error = None
            except Exception as exc:  # a later stage may fail on the mocks
                job_error = exc
        self.assertNotIn("match album metadata update", str(job_error or ""), job_log)
        self.assertTrue(any(f"Release Group changed {RG_A} -> {RG_B}" in line for line in job_log), job_log)
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_B, RG_B))

    def test_library_refs_compare_normalized_strings_without_resolve(self):
        ad = FakeAdapter()
        ad.items = {1: {"id": 1, "path": os.path.join(str(self.music), "A", "..", "A", "01.flac")},
                    2: {"id": 2, "path": os.path.join(str(self.music), "AB", "01.flac")}}
        with mock.patch("pathlib.Path.resolve", side_effect=AssertionError("resolve() called")):
            refs = cw._library_refs_under(os.path.join(str(self.music), "A"), adapter=ad)
        self.assertEqual([r["id"] for r in refs], [1])

    def test_library_refs_with_symlinked_music_root(self):
        """F-243-2: MUSIC_ROOT is a link; the folder is given through it.
        Realpath-absolute, relative and link-absolute item paths all match."""
        link = os.path.join(os.path.dirname(str(self.music)), "musiclink")
        try:
            os.symlink(str(self.music), link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        real = os.path.realpath(str(self.music))
        for stored in (os.path.join(real, "A", "01.flac"), os.path.join("A", "01.flac"),
                       os.path.join(link, "A", "01.flac")):
            ad = FakeAdapter()
            ad.items = {1: {"id": 1, "path": stored}}
            with mock.patch.dict(os.environ, {"MUSIC_ROOT": link}):
                refs = cw._library_refs_under(os.path.join(link, "A"), adapter=ad)
            self.assertEqual([r["id"] for r in refs], [1], stored)

    def test_staging_roots_follow_downloads_root(self):
        dl = os.path.join(str(self.music), "..", "dl-root")
        with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": dl, "BEETS_IMPORT_ROOTS": "/elsewhere"}):
            roots = cw._get_staging_roots()
        self.assertEqual(roots[0], __import__("pathlib").Path(dl).resolve())
        self.assertNotIn("elsewhere", " ".join(map(str, roots)))


class ImportRetagOperatorFlagTests(_IdentityEnv):
    """QA F-2: the import retag stamp may change Release Group only for the
    album this import produced or the album the operator named."""

    def test_rule(self):
        from backend.import_service import _retag_release_operator_selected as sel
        self.assertTrue(sel(7, auto_import=False, confirmed_album_id=7, operator_album_id=0))
        self.assertTrue(sel(9, auto_import=False, confirmed_album_id=0, operator_album_id=9))
        # Guessed ids (strategies B-I): neither confirmed nor named.
        self.assertFalse(sel(11, auto_import=False, confirmed_album_id=0, operator_album_id=0))
        self.assertFalse(sel(11, auto_import=False, confirmed_album_id=7, operator_album_id=9))
        # Auto-import never.
        self.assertFalse(sel(7, auto_import=True, confirmed_album_id=7, operator_album_id=7))
        self.assertFalse(sel(0, auto_import=False, confirmed_album_id=0, operator_album_id=0))

    def test_retag_uses_the_rule(self):
        import backend.import_service as imp
        src = inspect.getsource(imp.start_folder_import_with_id)
        self.assertNotIn("release_selected_by_operator=True", src)
        self.assertIn("_stamp_import_release(", src)
        self.assertIn("release_selected_by_operator=selected", inspect.getsource(imp._stamp_import_release))
        self.assertIn("operator_album_id = 0 if auto_import else existing_album_id", src)

    def test_preference_swapped_release_is_not_the_operator_choice(self):
        """Music-identity F-1: the operator picked a single (REL_A2); the
        import preferred an album Release in another Release Group (REL_B)."""
        from backend.import_service import _release_is_operator_choice as choice
        from backend.import_service import _retag_release_operator_selected as sel
        self.assertFalse(choice(REL_B, RG_B, REL_A2, ""))
        self.assertFalse(choice(REL_B, RG_B, REL_A2, RG_A))
        self.assertTrue(choice(REL_A2, RG_A, REL_A2, ""))
        self.assertTrue(choice(REL_A, RG_A, REL_A2, RG_A))  # same Release Group the operator named
        self.assertFalse(sel(9, auto_import=False, confirmed_album_id=0, operator_album_id=9,
                             release_is_operator_choice=False))
        # With the flag off, the existing album keeps its Release Group.
        ad = self.adapter()
        res = cw.update_album_metadata(1, {"mb_albumid": REL_B}, release_selected_by_operator=False,
                                       adapter=ad, store=self.store)
        self.assertEqual(res.get("code"), "repair_identity_mismatch", res)
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_A, RG_A))

    def test_import_records_choice_before_preference_and_logs_refusal(self):
        import backend.import_service as imp
        src = inspect.getsource(imp.start_folder_import_with_id)
        self.assertLess(src.index("operator_release_id = "), src.index("_prefer_album_mb_release(mb_albumid"))
        self.assertIn("_stamp_import_release(", src)
        self.assertIn("operator_release_id=operator_release_id", src)

    def _stamp(self, ad, aid, **kw):
        from backend.import_service import _stamp_import_release
        args = dict(auto_import=False, confirmed_album_id=0, operator_album_id=0,
                    operator_release_id=REL_B, operator_releasegroup_id="")
        args.update(kw)
        log = []
        with mock.patch.object(cw, "beets_adapter", ad), mock.patch.object(cw, "_get_store", return_value=self.store):
            res = _stamp_import_release(aid, REL_B, RG_B, log, **args)
        return res, log

    def test_retag_stamp_behaviour(self):
        """QA N-1: the real stamp path. Guessed album (auto-import or not)
        and a preference-swapped Release keep RG_A; the operator's album and
        Release move to RG_B and the change is logged."""
        cases = {
            "auto-import, guessed": dict(auto_import=True),
            "auto-import, even the confirmed album": dict(auto_import=True, confirmed_album_id=1),
            "guessed id": dict(confirmed_album_id=7, operator_album_id=9),
            "preference-swapped Release": dict(operator_album_id=1, operator_release_id=REL_A2),
        }
        for name, kw in cases.items():
            ad = self.adapter()
            res, log = self._stamp(ad, 1, **kw)
            self.assertEqual(res.get("code"), "repair_identity_mismatch", name)
            self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_A, RG_A), name)
            # F-4: the refusal is in the job log with its fixed code.
            self.assertTrue(any("Release ID stamp refused for album 1: repair_identity_mismatch" in l for l in log), log)
        ad = self.adapter()
        res, log = self._stamp(ad, 1, operator_album_id=1)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_B, RG_B))
        self.assertTrue(any(f"Release Group changed {RG_A} -> {RG_B}" in l for l in log), log)

    def test_cross_release_group_change_is_recorded(self):
        """Music-identity F-2."""
        ad = self.adapter()
        res = cw.update_album_metadata(1, {"mb_albumid": REL_B}, release_selected_by_operator=True,
                                       adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        change = {"from": RG_A, "to": RG_B, "operator_selected": True}
        self.assertEqual(res["release_group_change"], change)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["metadata"]["release_group_change"], change)
        self.assertIn(f"Release Group {RG_A} -> {RG_B}", tx["summary"])
        # Same Release Group: nothing recorded.
        same = cw.update_album_metadata(1, {"album": "Renamed"}, adapter=ad, store=self.store)
        self.assertNotIn("release_group_change", same)

    def test_guessed_album_cannot_change_release_group(self):
        ad = self.adapter()
        res = cw.plan_album_metadata({"album_id": 1, "updates": {"mb_albumid": REL_B},
                                      "release_selected_by_operator": False}, adapter=ad, store=self.store)
        self.assertEqual(res.get("code"), "repair_identity_mismatch", res)
        self.assertEqual((ad.albums[1]["mb_albumid"], ad.albums[1]["mb_releasegroupid"]), (REL_A, RG_A))


class FolderCleanupTests(_Env):
    """BA-2: folder cleanup does the work it reports; #218: no apply after cancel."""

    def setUp(self):
        super().setUp()
        self.adapter = FakeAdapter()
        p = mock.patch.object(cw, "beets_adapter", self.adapter)
        p.start()
        self.addCleanup(p.stop)
        try:
            from _folder_ops_local import patch_local_folder_ops
        except ImportError:  # pragma: no cover
            from tests._folder_ops_local import patch_local_folder_ops
        patch_local_folder_ops(self, self.music)

    def test_apply_refuses_folder_that_gained_beets_items(self):
        src = self.music / "Artist" / "Albm"
        src.mkdir(parents=True)
        (src / "01.flac").write_bytes(b"a")
        plan = cw.plan_folder_cleanup({"action": "safe_rename", "source": str(src),
                                       "target": str(self.music / "Artist" / "Album")}, store=self.store)
        self.assertTrue(plan["ok"], plan)
        self.adapter.items[1] = {"id": 1, "album_id": 1, "path": str(src / "01.flac")}
        res = cw.apply_folder_cleanup(plan["operation_id"], store=self.store)
        self.assertEqual(res.get("code"), "folder_cleanup_db_references", res)
        self.assertTrue((src / "01.flac").exists())

    def test_safe_rename_really_renames(self):
        src = self.music / "Artist" / "Albm"
        src.mkdir(parents=True)
        (src / "01.flac").write_bytes(b"a")
        dst = self.music / "Artist" / "Album"
        plan = cw.plan_folder_cleanup({"action": "safe_rename", "source": str(src), "target": str(dst)}, store=self.store)
        self.assertTrue(plan["ok"], plan)
        res = cw.apply_folder_cleanup(plan["operation_id"], store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertTrue((dst / "01.flac").exists())
        self.assertFalse(src.exists())

    def test_non_empty_folder_is_not_planned_for_removal(self):
        src = self.music / "Artist" / "Album"
        src.mkdir(parents=True)
        (src / "01.flac").write_bytes(b"a")
        plan = cw.plan_folder_cleanup({"action": "remove_empty", "source": str(src)}, store=self.store)
        self.assertEqual(plan.get("code"), "folder_cleanup_not_empty", plan)

    def test_engine_folder_cleanup_uses_configured_music_root_alias(self):
        """F-243-4: MUSIC_ROOT unset, deprecated alias set -> same root."""
        src = self.music / "AliasEmpty"
        src.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("MUSIC_ROOT", "BEETS_MUSIC_DIR")}
        env["MUSIC_LIBRARY_PATH"] = str(self.music)
        with mock.patch.dict(os.environ, env, clear=True):
            plan = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(src)})
        self.assertTrue(plan.get("ok"), plan)

    def test_engine_apply_of_cancelled_plan_changes_nothing(self):
        src = self.music / "Empty"
        src.mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(src)})
        self.store.transition(plan["operation_id"], "Preview", "Cancelled")
        res = te.execute_folder_cleanup_apply(self.store, plan["operation_id"])
        self.assertFalse(res["ok"])
        self.assertTrue(src.exists())
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Cancelled")

    def test_cancel_between_check_and_claim_changes_nothing(self):
        src = self.music / "Empty"
        src.mkdir()
        op = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(src)})["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        real = te._claim_apply_running

        def racing_claim(store, operation_id, observed_status, metadata):
            store.update(operation_id, status="Cancelled")  # the cancel wins the race
            return real(store, operation_id, observed_status, metadata)

        with mock.patch.object(te, "_claim_apply_running", racing_claim):
            res = te.execute_folder_cleanup_apply(self.store, op)
        self.assertFalse(res["ok"], res)
        self.assertTrue(src.exists())
        self.assertEqual(self.store.get(op)["status"], "Cancelled")

    def test_engine_never_opens_a_sqlite_file(self):
        """BA-7: Web Manager never opens the Beets library file."""
        self.assertNotIn("sqlite3", inspect.getsource(te))


class EngineClaimTests(unittest.TestCase):
    """#218: no engine apply pre-marks Running without a compare-and-set."""

    def test_no_unconditional_running_update_in_engine(self):
        tree = ast.parse(inspect.getsource(te))
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
                    and any(k.arg == "status" and isinstance(k.value, ast.Constant) and k.value.value == "Running"
                            for k in node.keywords)):
                offenders.append(node.lineno)
        self.assertEqual(offenders, [])

    def test_claim_helper_refuses_finished_and_changed_status(self):
        store = mock.Mock()
        self.assertIsNone(te._claim_apply_running(store, "op", "Cancelled", {}))
        store.transition.assert_not_called()
        store.transition.return_value = None
        self.assertIsNone(te._claim_apply_running(store, "op", "Approved", {}))
        store.transition.assert_called_once_with("op", "Approved", "Running", metadata={})


class RollbackEligibilityTests(_Env):
    """#224: a rollback never marks an unapplied transaction Rolled Back."""

    def test_noop_rollbacks_refuse_unapplied_transactions(self):
        fns = [cw.rollback_album_artwork_fetch, cw.rollback_album_artwork, cw.rollback_item_metadata,
               cw.rollback_album_maintenance, cw.rollback_album_relocation, cw.rollback_album_genre_repair,
               cw.rollback_import_folder]
        for fn in fns:
            for status in ("Preview", "Cancelled"):
                tx = self.store.create(operation_type="Metadata Update", status="Preview", metadata={})
                if status != "Preview":
                    self.store.transition(tx["id"], "Preview", status)
                res = fn(tx["id"], store=self.store)
                self.assertFalse(res["ok"], (fn.__name__, status))
                self.assertEqual(self.store.get(tx["id"])["status"], status, fn.__name__)

    def test_artist_reconcile_rollback_restores_and_refuses_preview(self):
        ad = MutAdapter(albums={1: {"id": 1, "albumartist": "New"}})
        tx = self.store.create(operation_type="Metadata Update", status="Preview",
                               metadata={"before_state": [{"album_id": 1, "albumartist": "Old"}]})
        self.assertFalse(cw.rollback_artist_folder_reconcile(tx["id"], adapter=ad, store=self.store)["ok"])
        self.assertEqual(ad.writes(), [])
        self.store.transition(tx["id"], "Preview", "Completed")
        self.assertTrue(cw.rollback_artist_folder_reconcile(tx["id"], adapter=ad, store=self.store)["ok"])
        self.assertEqual(ad.albums[1]["albumartist"], "Old")
        self.assertEqual(self.store.get(tx["id"])["status"], "Rolled Back")


class DuplicateResolverDefaultTests(unittest.TestCase):
    """MI-14: a title score never preselects a destructive action."""

    def test_default_action_is_skip_regardless_of_title_score(self):
        from backend import dedup_service
        rec_dup, rec_missing = REC_1, "cccccccc-0000-4000-8000-000000000002"
        data = {"mb_albumid": REL_A,
                "tracks": [{"mb_trackid": rec_dup, "title": "Song", "item": {"id": 1}}],
                "missing": [{"mb_trackid": rec_missing, "title": "Other Song", "track": 2, "disc": 1}]}

        def row(iid, title):
            return {"id": iid, "album_id": 1, "title": title, "track": 1, "disc": 1, "path": f"/m/A/{iid}.flac",
                    "mb_trackid": rec_dup, "mb_albumid": REL_A, "length": 1, "album": "A", "albumartist": "A"}

        for dup_title in ("Other Song", "Zzz"):  # high and low title score
            rows = [row(1, "Song"), row(2, dup_title)]
            with mock.patch.object(dedup_service, "_album_mb_completeness", return_value=data), \
                 mock.patch.object(dedup_service.composite_workflows, "find_all_items_by_album_id", return_value=rows), \
                 mock.patch.object(dedup_service.composite_workflows, "get_folder_items", return_value=rows):
                plan = dedup_service._album_duplicate_resolver_plan(1)
            actions = [it["default_action"] for g in plan["groups"] for it in g["action_items"]]
            self.assertEqual(actions, ["skip"], dup_title)


class RelinkRouteTests(unittest.TestCase):
    """MI-9: the relink endpoint never writes a text-search hit as identity."""

    def test_relink_without_ids_is_refused_before_any_job(self):
        import app as app_module
        import routes_cleanup
        row = {"id": 1, "album": "Album", "albumartist": "Artist", "year": 2001,
               "mb_albumid": "", "mb_releasegroupid": ""}
        with app_module.app.test_request_context("/api/clean/rgid-group/relink", method="POST",
                                                 data=json.dumps({"album_id": 1}), content_type="application/json"), \
             mock.patch.object(routes_cleanup.composite_workflows, "get_album", return_value=row), \
             mock.patch.object(routes_cleanup.jobs, "start_python") as start:
            resp = routes_cleanup.clean_rgid_group_relink()
        resp, status = resp if isinstance(resp, tuple) else (resp, resp.status_code)
        self.assertEqual(status, 400)
        self.assertEqual(resp.get_json()["code"], "relink_identity_required")
        start.assert_not_called()


class MoveAllLogTests(unittest.TestCase):
    """BA-20: move-all logs only folders the engine actually removed."""

    def test_no_removed_line_without_removed_dirs(self):
        from tests.test_arch003_wave33_library_mbsync_move_all import LibraryTablesFixture, app_module

        class _Case(LibraryTablesFixture):
            def runTest(inner):
                cwm = app_module.composite_workflows
                with mock.patch.object(cwm, "list_distinct_item_paths", return_value=["A/B/1.mp3"]), \
                     mock.patch.object(cwm, "move_library", return_value={"ok": True, "success": True, "returncode": 0}), \
                     mock.patch.object(cwm, "plan_folder_cleanup", return_value={"ok": True, "operation_id": "op-1"}), \
                     mock.patch.object(cwm, "apply_folder_cleanup", return_value={"ok": True, "mutated": False}):
                    log = inner._run(app_module.library_move_all, "/api/library/move-all")
                inner.assertFalse([line for line in log if "Removed empty folder" in line], log)

        result = unittest.TestResult()
        _Case().run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)


class NoMaintainerPathsTests(unittest.TestCase):
    """BA-12: defaults come from config_layers, not a maintainer's host layout."""

    def test_no_hard_coded_host_paths(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("routes_cleanup.py", "backend/dedup_service.py", "backend/transaction_engine.py"):
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                src = fh.read()
            for path in ("/data/torrents", "/data/media"):
                self.assertNotIn(path, src, rel)


if __name__ == "__main__":
    unittest.main()
