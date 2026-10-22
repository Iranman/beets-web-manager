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
