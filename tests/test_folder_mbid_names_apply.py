"""Apply/rollback for naming album folders ``Album (Year) {release-group-mbid}``
from the Lidarr release-group resolver (the correct replacement for the earlier
strip-based placeholder apply, which wrongly removed the token).

The resolver is MOCKED here (never a live Lidarr call). Disk-only folders are
renamed through folder_cleanup_v1 on a temp library; a DB-tracked album is
relocated through a real Beets library so its DB item paths follow. Covers:
resolved rename (disk-only + tracked relocation), already_correct idempotent
skip, every unresolved reason skipped, target_exists held, a provider error
aborting the run touching nothing, dry-run mutating nothing, confirmed required
(400), resume after a partial failure (no dup op + rollback-by-job), batch_limit
counting only mutations, and rollback restoring.
"""

import os
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.library_service as ls
import backend.provider_boundary as provider_boundary
from tests._folder_ops_local import patch_local_folder_ops
from tests.test_wave0_s1_containment import FakeAdapter, _Env


RGID = "11111111-1111-1111-1111-111111111111"
RGID2 = "22222222-2222-2222-2222-222222222222"
ARTIST_MBID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PH = "{Album MbId}"


class FakeResolver:
    """Stand-in for LidarrRgidResolver: verdicts keyed by album title. Records
    calls so a test can prove the one bulk snapshot is reused per folder."""

    def __init__(self, by_title):
        self.by_title = by_title
        self.calls = []

    def resolve(self, *, artist_mbid, album_title, album_year="", album_basename=""):
        self.calls.append({"artist_mbid": artist_mbid, "album_title": album_title,
                           "album_year": album_year, "album_basename": album_basename})
        return self.by_title.get(album_title, {"status": "unresolved", "reason": "album_not_in_lidarr"})


def _resolved(rgid=RGID, tier="title_year"):
    return {"status": "resolved", "release_group_mbid": rgid, "matched_tier": tier, "lidarr_album_id": 1}


class _DiskOnly(_Env):
    def setUp(self):
        super().setUp()
        self.empty = FakeAdapter()
        p = mock.patch.object(cw, "beets_adapter", self.empty)
        p.start()
        self.addCleanup(p.stop)
        patch_local_folder_ops(self, self.music)

    def make_folder(self, rel, *, audio=True):
        folder = self.music / rel
        folder.mkdir(parents=True, exist_ok=True)
        if audio:
            (folder / "01 track.flac").write_bytes(b"audio-bytes")
        return folder

    def apply(self, folders, *, dry_run=False, resolver=None, **kw):
        if resolver is None:
            resolver = FakeResolver({"Album": _resolved()})
        return ls._apply_folder_mbid_names(
            [str(f) for f in folders], dry_run=dry_run,
            adapter=self.empty, store=self.store, resolver=resolver, **kw)

    def test_resolved_rename_fills_mbid(self):
        # Stripped folder (no token) -> filled with the resolver's MBID verbatim.
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003)")
        res = self.apply([src])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertFalse(src.exists())
        self.assertTrue((src.parent / f"Album (2003) {{{RGID}}}").is_dir())
        self.assertEqual(res["operations"][0]["kind"], "folder")

    def test_placeholder_token_replaced_not_stripped(self):
        # The old bug stripped the token; this fills it.
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003){PH}")
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertTrue((src.parent / f"Album (2003) {{{RGID}}}").is_dir())
        self.assertFalse((src.parent / "Album (2003)").exists())  # NOT stripped

    def test_already_correct_is_idempotent_skip(self):
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003) {{{RGID}}}")
        res = self.apply([src])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["final_summary"]["already_correct_count"], 1, res)
        self.assertEqual(res["operations"], [])
        self.assertTrue(src.exists())  # untouched

    def test_yearless_folder_is_idempotent_across_reruns(self):
        # Regression: a folder with no parseable year must stabilize. The old bug
        # emitted a bare "()" so each run grew the name
        # (Album {rgid} -> Album () {rgid} -> ...); over ~9k resumeable folders
        # that never settles. First run fills the MBID (no "()"); second run must
        # be a pure already_correct skip with the name unchanged.
        resolver = FakeResolver({"Album": _resolved()})
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album")
        res1 = self.apply([src], resolver=resolver)
        self.assertTrue(res1["ok"], res1)
        self.assertEqual(res1["final_summary"]["changed_count"], 1, res1)
        renamed = src.parent / f"Album {{{RGID}}}"
        self.assertTrue(renamed.is_dir(), res1)
        self.assertFalse(src.exists())

        res2 = self.apply([renamed], resolver=resolver)
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(res2["final_summary"]["changed_count"], 0, res2)
        self.assertEqual(res2["final_summary"]["already_correct_count"], 1, res2)
        self.assertEqual(res2["operations"], [])
        self.assertTrue(renamed.is_dir())  # name did NOT grow
        self.assertFalse((src.parent / f"Album () {{{RGID}}}").exists())

    def test_wrong_mbid_token_is_corrected(self):
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003) {{{RGID2}}}")
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertTrue((src.parent / f"Album (2003) {{{RGID}}}").is_dir())
        self.assertFalse(src.exists())

    def test_unresolved_reasons_skipped_with_reason(self):
        reasons = ["artist_not_in_lidarr", "album_not_in_lidarr",
                   "ambiguous_multiple_albums", "lidarr_missing_rgid"]
        for reason in reasons:
            with self.subTest(reason=reason):
                src = self.make_folder(f"Artist ({ARTIST_MBID})/Unres {reason} (2003)")
                resolver = FakeResolver({f"Unres {reason}": {"status": "unresolved", "reason": reason}})
                res = self.apply([src], resolver=resolver)
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["final_summary"]["changed_count"], 0)
                self.assertEqual(res["skipped"][0]["reason"], reason, res)
                self.assertEqual(res["final_summary"]["skipped_by_reason"].get(reason), 1)
                self.assertTrue(src.exists())

    def test_target_exists_held(self):
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003)")
        collision = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003) {{{RGID}}}")
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["skipped"][0]["reason"], "target_exists", res)
        self.assertTrue(src.exists() and collision.exists())  # neither touched
        self.assertEqual(res["operations"], [])

    def test_dry_run_changes_nothing(self):
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003)")
        res = self.apply([src], dry_run=True)
        self.assertTrue(src.exists())
        self.assertEqual(res["operations"], [])
        self.assertEqual(res["final_summary"]["would_change_count"], 1, res)
        self.assertEqual(res["final_summary"]["changed_count"], 0)

    def test_provider_error_aborts_touching_nothing(self):
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003)")
        err = provider_boundary.ProviderError(
            provider_boundary.ProviderOutcome.UNAVAILABLE, "lidarr down", provider="lidarr")
        with mock.patch("backend.lidarr_rgid_resolver.LidarrRgidResolver.from_lidarr", side_effect=err):
            with self.assertRaises(provider_boundary.ProviderError):
                # resolver=None forces the bulk snapshot, which raises.
                ls._apply_folder_mbid_names([str(src)], dry_run=False,
                                            adapter=self.empty, store=self.store, resolver=None)
        self.assertTrue(src.exists())  # nothing touched
        self.assertEqual(self.store.list(limit=100)[0], [])  # no transaction created

    def test_batch_limit_counts_only_mutations(self):
        # A leading already_correct + a leading unresolved must NOT consume the
        # budget; the one actionable rename does.
        correct = self.make_folder(f"Artist ({ARTIST_MBID})/Correct (2001) {{{RGID}}}")
        unres = self.make_folder(f"Artist ({ARTIST_MBID})/Nope (2002)")
        act = self.make_folder(f"Artist ({ARTIST_MBID})/Act (2003)")
        resolver = FakeResolver({"Correct": _resolved(), "Act": _resolved()})  # Nope -> unresolved
        res = self.apply([correct, unres, act], batch_limit=1, resolver=resolver)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertEqual(res["final_summary"]["already_correct_count"], 1)
        self.assertFalse(act.exists())  # the actionable row ran within budget
        self.assertTrue((act.parent / f"Act (2003) {{{RGID}}}").is_dir())

    def test_rollback_restores(self):
        src = self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003)")
        res = self.apply([src])
        self.assertFalse(src.exists())
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["final_summary"]["rolled_back_count"], 1)
        self.assertTrue(src.exists())
        self.assertFalse((src.parent / f"Album (2003) {{{RGID}}}").exists())

    def test_resume_after_partial_failure_no_dup_and_rollback_ok(self):
        a = self.make_folder(f"Artist ({ARTIST_MBID})/One (2001)")
        b = self.make_folder(f"Artist ({ARTIST_MBID})/Two (2002)")
        resolver = FakeResolver({"One": _resolved(), "Two": _resolved()})
        real = cw.safe_rename_library_folder

        def rename_then_fail(src, tgt, *a_, **kw):
            out = real(src, tgt, *a_, **kw)
            if "Two (2002)" in src:
                return {"ok": False, "code": "folder_cleanup_move_failed", "error": "x",
                        "operation_id": out["operation_id"]}
            return out

        with mock.patch.object(cw, "safe_rename_library_folder", rename_then_fail):
            first = self.apply([a, b], resolver=resolver)
        self.assertFalse(first["ok"])
        self.assertEqual(first["done_folders"], [str(a)])
        second = self.apply([a, b], resume_done=set(first["done_folders"]),
                            prior_operations=list(first["operations"]), resolver=resolver)
        ids = [o.get("operation_id") for o in second["operations"]]
        self.assertEqual(len(ids), len(set(ids)), ids)  # no duplicated op
        rb = ls._rollback_folder_placeholder_apply(second["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb["errors"])
        self.assertTrue(a.exists() and b.exists())

    def test_walk_covers_every_album_folder(self):
        self.make_folder(f"Artist ({ARTIST_MBID})/Album (2003)")
        self.make_folder(f"Artist ({ARTIST_MBID})/Other (2004)")
        with mock.patch.object(ls, "MUSIC_ROOT", self.music):
            found = {p.name for p in ls._iter_album_folders()}
        self.assertEqual(found, {"Album (2003)", "Other (2004)"})


class TrackedRelocationTests(unittest.TestCase):
    """A DB-tracked album is renamed by relocating it through Beets so its DB
    item paths follow -- and ONLY when the DB mb_releasegroupid already equals
    the resolver's, so the template-driven rename never writes a non-resolver
    MBID. A DB rgid that differs is held (tracked_rgid_mismatch)."""

    def setUp(self):
        from tests.test_album_relocation_transaction import _Engine
        self.eng = _Engine()
        self.eng.setUp()
        self.addCleanup(self.eng.doCleanups)

    def _album(self, folder, rgid):
        album = self.eng.make_album(folder)
        album.mb_releasegroupid = rgid
        album.store()
        return album

    def test_tracked_relocation_updates_db_paths(self):
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID)
        before_paths, _ = eng.beets_state(album.id)
        resolver = FakeResolver({"B": _resolved()})
        res = ls._apply_folder_mbid_names([folder], dry_run=False, adapter=eng.ad,
                                          store=eng.store, resolver=resolver)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertEqual(res["operations"][0]["kind"], "album", res)
        after_paths, _ = eng.beets_state(album.id)
        self.assertNotEqual(after_paths, before_paths)  # DB item paths moved
        self.assertFalse(os.path.exists(folder))        # source vacated
        # rollback-by-job restores
        rb = ls._rollback_folder_placeholder_apply(res["operations"],
                                                   adapter=eng.ad, store=eng.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)

    def test_tracked_rgid_mismatch_is_held(self):
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID2)  # DB carries a DIFFERENT rgid
        before_paths, _ = eng.beets_state(album.id)
        resolver = FakeResolver({"B": _resolved(RGID)})  # resolver says RGID
        res = ls._apply_folder_mbid_names([folder], dry_run=False, adapter=eng.ad,
                                          store=eng.store, resolver=resolver)
        self.assertTrue(res["ok"], res)  # a held row is not an error
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["skipped"][0]["reason"], "tracked_rgid_mismatch", res)
        self.assertEqual(res["operations"], [])
        self.assertTrue(os.path.exists(folder))
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)  # DB untouched

    def test_path_is_tracked_desync_relocates_not_folder_rename(self):
        # Live failure (25-folder batch): Beets reports a TRACKED album's item
        # paths RELATIVE to the library dir (the engine already anticipates this:
        # _item_abs_path rebuilds them on MUSIC_ROOT). On base the apply's
        # dirname-only tracked map keys those relative paths off the CWD, so it
        # reports db_item_count=0, picks the disk-only folder_rename, and the
        # engine's DB-reference refusal -- which DOES resolve the relative paths
        # -- rejects it (folder_cleanup_rename_failed / PATH_IS_TRACKED). After
        # the fix both detectors resolve+normalize through the same path helper,
        # so it is classified tracked and RELOCATED (DB paths follow), never
        # bare-renamed. Fail-on-base: base returns ok:false (PATH_IS_TRACKED).
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID)
        before_paths, _ = eng.beets_state(album.id)
        real_list = eng.ad.list_item_paths

        def relative_paths(*a, **kw):  # Beets with a relative library: paths are rel to MUSIC_ROOT
            out = []
            for rec in real_list(*a, **kw):
                p = rec.get("path") or ""
                out.append({**rec, "path": os.path.relpath(p, eng.music) if os.path.isabs(p) else p})
            return out

        resolver = FakeResolver({"B": _resolved()})
        with mock.patch.dict(os.environ, {"MUSIC_ROOT": eng.music}), \
                mock.patch.object(eng.ad, "list_item_paths", side_effect=relative_paths), \
                mock.patch.object(cw, "beets_adapter", eng.ad):  # the engine refusal reads the global
            res = ls._apply_folder_mbid_names([folder], dry_run=False, adapter=eng.ad,
                                              store=eng.store, resolver=resolver)
        self.assertTrue(res["ok"], res)  # base: PATH_IS_TRACKED error -> ok False
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertEqual(res["operations"][0]["kind"], "album", res)  # relocated, not folder_rename
        after_paths, _ = eng.beets_state(album.id)
        self.assertNotEqual(after_paths, before_paths)  # DB item paths moved with the files
        for p in after_paths.values():
            self.assertTrue(os.path.exists(p), p)  # no file lost

    def test_plugin_path_is_tracked_held_not_errored(self):
        # Exact live error: Web Manager's path view of the library is blind to a
        # tracked album (adapter list_item_paths returns nothing for it -- a
        # path-representation gap with Beets), so BOTH the apply's tracked map
        # and the engine's plan-time DB-reference refusal miss it and route a
        # bare folder rename. The Beets PLUGIN's apply-time guard then refuses
        # (PATH_IS_TRACKED) before touching disk. On base that surfaces as
        # folder_cleanup_rename_failed and a failed job; the fix recognizes the
        # tracked refusal and HOLDS (needs relocation), ok, folder untouched.
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID)
        before_paths, _ = eng.beets_state(album.id)
        resolver = FakeResolver({"B": _resolved()})
        import backend.beets_adapter as ba_mod
        # Blind the Web-Manager-side detectors (list_item_paths empty); the real
        # plugin -- reached via the bridge on folder_op, unaffected by that patch
        # -- still sees the items and raises PATH_IS_TRACKED. MUSIC_ROOT=eng.music
        # so the engine's allowed-root check passes and it reaches the plugin.
        with mock.patch.dict(os.environ, {"MUSIC_ROOT": eng.music}), \
                mock.patch.object(eng.ad, "list_item_paths", return_value=[]), \
                mock.patch.object(cw, "beets_adapter", eng.ad), \
                mock.patch.object(ba_mod, "beets_adapter", eng.ad):
            res = ls._apply_folder_mbid_names([folder], dry_run=False, adapter=eng.ad,
                                              store=eng.store, resolver=resolver)
        self.assertTrue(res["ok"], res)  # base: folder_cleanup_rename_failed -> ok False
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["skipped"][0]["reason"], "became_tracked_needs_relocation", res)
        self.assertTrue(os.path.exists(folder))  # plugin refused before renaming
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)  # DB untouched
        self.assertFalse((os.path.dirname(folder) and os.path.exists(
            os.path.join(os.path.dirname(folder), f"B {{{RGID}}}"))))  # no target created

    def test_stale_db_paths_held_needs_db_resync(self):
        # Strip-desync: the DB names files under the folder that an earlier
        # rename already moved away (gone on disk). Relocating off those stale
        # paths would skip every track (partial_move error) and desync further.
        # The fix HOLDS (needs_db_resync) instead: ok, nothing changed, no file
        # lost. Fail-on-base: base relocates off stale paths and returns ok:false.
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID)
        before_paths, _ = eng.beets_state(album.id)
        # Rename the FILES on disk under the SAME folder, so the folder is still
        # detected tracked (DB paths lie under it) but every DB path is now gone.
        for p in list(before_paths.values()):
            os.rename(p, os.path.join(os.path.dirname(p), "resynced_" + os.path.basename(p)))
        resolver = FakeResolver({"B": _resolved()})
        res = ls._apply_folder_mbid_names([folder], dry_run=False, adapter=eng.ad,
                                          store=eng.store, resolver=resolver)
        self.assertTrue(res["ok"], res)  # a held row is not an error (base: ok False)
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["skipped"][0]["reason"], "needs_db_resync", res)
        self.assertEqual(res["operations"], [])
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)  # DB untouched
        for p in os.listdir(folder):  # the renamed files are all still present
            self.assertTrue(p.startswith("resynced_") or p == "cover.jpg", p)

    def test_dangling_symlink_db_path_held_needs_db_resync(self):
        # A DB path that is a DANGLING symlink (link present, target gone) has
        # no real file to move. os.path.lexists would call it present and let
        # the relocation run -> Beets skips every track -> a loud rollback-able
        # partial_move. os.path.exists treats it as stale -> HOLD needs_db_resync.
        # Fails on lexists, passes on exists.
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID)
        before_paths, _ = eng.beets_state(album.id)
        for p in list(before_paths.values()):  # replace each real file with a dangling link
            os.remove(p)
            try:
                os.symlink(os.path.join(os.path.dirname(p), "gone-target"), p)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
        resolver = FakeResolver({"B": _resolved()})
        res = ls._apply_folder_mbid_names([folder], dry_run=False, adapter=eng.ad,
                                          store=eng.store, resolver=resolver)
        self.assertTrue(res["ok"], res)  # not a partial_move error
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["skipped"][0]["reason"], "needs_db_resync", res)
        self.assertEqual(res["operations"], [])
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)  # DB untouched

    def test_tracked_dry_run_mutates_nothing(self):
        eng = self.eng
        folder = os.path.join(eng.music, f"Artist ({ARTIST_MBID})", f"B{PH}")
        album = self._album(folder, RGID)
        before_paths, _ = eng.beets_state(album.id)
        resolver = FakeResolver({"B": _resolved()})
        res = ls._apply_folder_mbid_names([folder], dry_run=True, adapter=eng.ad,
                                          store=eng.store, resolver=resolver)
        self.assertEqual(res["final_summary"]["would_change_count"], 1, res)
        self.assertEqual(res["operations"], [])
        self.assertTrue(os.path.exists(folder))
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)


class _FakeJob:
    def __init__(self, status, job_id="started-job"):
        self.status = status
        self.job_id = job_id
        self.state = {"checkpoint": {"done_folders": [], "operations": []}}
        self.result = {}


class _FakeJobs:
    def __init__(self, job):
        self.job = job

    def get(self, _job_id):
        return self.job

    def start_python(self, *_a, **_kw):
        return _FakeJob("running", job_id="started-job")


class RouteGuardTests(unittest.TestCase):
    """confirmed:true gate (400) and the F6 running-source-job guard (409) for
    the two new endpoints. start_python is stubbed so no real Lidarr call runs."""

    @classmethod
    def setUpClass(cls):
        import app
        import routes_library
        cls.routes_library = routes_library
        cls.flask_app = app.app

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = self.flask_app.test_client()

    def _patch_jobs(self, status):
        p = mock.patch.object(self.routes_library, "jobs", _FakeJobs(_FakeJob(status)))
        p.start()
        self.addCleanup(p.stop)

    def test_apply_requires_confirmed(self):
        r = self.client.post("/api/library/folder-mbid-names/apply",
                             json={"dry_run": False})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["ok"])

    def test_apply_refuses_running_source_job(self):
        self._patch_jobs("running")
        r = self.client.post("/api/library/folder-mbid-names/apply",
                             json={"dry_run": True, "resume_from_job": "job-x"})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))

    def test_rollback_refuses_running_source_job(self):
        self._patch_jobs("running")
        r = self.client.post("/api/library/folder-mbid-names/rollback",
                             json={"job_id": "job-x"})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
