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


class FolderCleanupTests(_Env):
    """BA-2: folder cleanup does the work it reports; #218: no apply after cancel."""

    def setUp(self):
        super().setUp()
        self.adapter = FakeAdapter()
        p = mock.patch.object(cw, "beets_adapter", self.adapter)
        p.start()
        self.addCleanup(p.stop)

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
        with mock.patch.dict(os.environ, {"BEETS_LIBRARY_DB": ""}):
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
        with mock.patch.dict(os.environ, {"BEETS_LIBRARY_DB": ""}):
            plan = cw.plan_folder_cleanup({"action": "remove_empty", "source": str(src)}, store=self.store)
        self.assertEqual(plan.get("code"), "folder_cleanup_not_empty", plan)

    def test_engine_apply_of_cancelled_plan_changes_nothing(self):
        src = self.music / "Empty"
        src.mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(src)}, db_path="")
        self.store.transition(plan["operation_id"], "Preview", "Cancelled")
        res = te.execute_folder_cleanup_apply(self.store, plan["operation_id"], db_path="")
        self.assertFalse(res["ok"])
        self.assertTrue(src.exists())
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Cancelled")


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


class EngineFamilyInterleavingTests(unittest.TestCase):
    """#218: one interleaving per engine apply family. The existing family
    suites run with a cancel landing between each apply's status check and
    its claim; every family must lose the claim and stop (the suites' own
    results are irrelevant here: their applies are being cancelled)."""

    MODULES = ["test_album_lifecycle_wave24_engine_unit", "test_beets_transaction_engine",
               "test_sec002_wave22_album_maintenance", "test_album_artwork_fetch_v1",
               "test_sec002_wave19_mb_track_repair", "test_sec002_wave21_artist_folder_reconcile",
               "test_confirmed_import_v1", "test_sec002_wave20_existing_album_reconcile",
               "test_library_cleanup_closure", "test_sec002_wave27_genre_repair"]

    def test_cancel_before_claim_stops_every_family(self):
        families = sorted({f.name for f in ast.walk(ast.parse(inspect.getsource(te)))
                           if isinstance(f, ast.FunctionDef) and any(
                               isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_claim_apply_running"
                               for n in ast.walk(f))})
        self.assertEqual(len(families), 16, families)
        real, seen = te._claim_apply_running, {}

        def racing_claim(store, operation_id, observed_status, metadata):
            store.update(operation_id, status="Cancelled")  # the cancel wins the race
            claimed = real(store, operation_id, observed_status, metadata)
            caller = inspect.currentframe().f_back.f_code.co_name
            seen.setdefault(caller, []).append((claimed, store.get(operation_id).get("status")))
            return claimed

        suite = unittest.defaultTestLoader.loadTestsFromNames(f"tests.{m}" for m in self.MODULES)
        with mock.patch.object(te, "_claim_apply_running", racing_claim):
            suite.run(unittest.TestResult())
        self.assertEqual(sorted(seen), families)
        for caller, outcomes in seen.items():
            self.assertTrue(all(c is None and s == "Cancelled" for c, s in outcomes), (caller, outcomes))


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


class MbTrackRepairEngineEvidenceTests(unittest.TestCase):
    """MI-18: no Recording ID from text/position alignment when AcoustID is unavailable."""

    def test_blank_slots_go_to_review_without_acoustid(self):
        from tests.test_sec002_wave19_mb_track_repair import Wave19FixtureBase, _engine_repair_plan, _fake_tracklist_a

        class _Case(Wave19FixtureBase):
            def runTest(inner):
                inner._create_album_and_items(album_id=1)
                res = _engine_repair_plan(inner.store, {"album_id": 1}, music_allowed_roots=[str(inner.music_root)],
                                          db_path=str(inner.db_path), fetch_tracklist_fn=lambda _: _fake_tracklist_a())
                inner.assertEqual(res.get("updated", 0), 0, res)
                inner.assertEqual(res.get("conflicts"), 2, res)

        result = unittest.TestResult()
        _Case().run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)


class MbTrackRepairAcoustidRuleTests(unittest.TestCase):
    """MI-6 in the engine's own copy: weak (< 80) or tied hits never confirm
    and never contradict the tracklist."""

    OTHER = "eeeeeeee-0000-4000-8000-00000000000e"
    TRACKS = [{"mb_trackid": REC_1, "title": "Song"}]

    def check(self, hits):
        path = mock.Mock()
        path.exists.return_value = True
        return te._mb_track_repair_acoustid_check(path, self.TRACKS, lambda _p: hits)["status"]

    def test_weak_or_tied_hits_are_unclear(self):
        cases = {
            "weak in tracklist": [{"mb_trackid": REC_1, "title": "Song", "score": 79}],
            "weak outside": [{"mb_trackid": self.OTHER, "title": "Unrelated", "score": 75}],
            "tied in tracklist": [{"mb_trackid": REC_1, "title": "Song", "score": 90},
                                  {"mb_trackid": self.OTHER, "title": "Unrelated", "score": 88}],
            "tied outside": [{"mb_trackid": self.OTHER, "title": "Unrelated", "score": 90},
                             {"mb_trackid": REC_1, "title": "Song", "score": 89}],
        }
        for name, hits in cases.items():
            self.assertEqual(self.check(hits), "unclear", name)

    def test_confirmed_hits_still_decide(self):
        self.assertEqual(self.check([{"mb_trackid": REC_1, "title": "Song", "score": 0.95}]), "match")
        self.assertEqual(self.check([{"mb_trackid": self.OTHER, "title": "Unrelated", "score": 95}]), "mismatch")


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
