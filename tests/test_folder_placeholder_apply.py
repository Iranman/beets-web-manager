"""Apply/rollback for album folders whose template placeholder was never
substituted (e.g. ``Hot & Wet (2003){Album MbId}`` -> ``Hot & Wet (2003)``).

Disk-only folders are renamed through folder_cleanup_v1 on a temp library;
DB-tracked folders are relocated through a real Beets library so the DB item
paths follow the files. Covers: safe rename applied, target-exists skipped,
path-escape refused, resume after interruption, single rollback, and a
tracked-folder relocation that updates the DB path.
"""

import os
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.library_service as ls
from tests._folder_ops_local import patch_local_folder_ops
from tests.test_wave0_s1_containment import FakeAdapter, _Env


PH = "{Album MbId}"


class _DiskOnly(_Env):
    def setUp(self):
        super().setUp()
        # No Beets items anywhere: safe_rename's tracked guard reads the global
        # adapter; folder_db reads the one we pass. Both must see zero items.
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

    def apply(self, folders, *, dry_run=False, **kw):
        return ls._apply_folder_name_placeholders(
            [str(f) for f in folders], dry_run=dry_run,
            adapter=self.empty, store=self.store, **kw)

    def test_safe_rename_applied(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        res = self.apply([src])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertFalse(src.exists())
        self.assertTrue((self.music / "Jade/Hot & Wet (2003)").is_dir())
        self.assertEqual(len(res["operations"]), 1)
        self.assertEqual(res["operations"][0]["kind"], "folder")

    def test_dry_run_changes_nothing(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        res = self.apply([src], dry_run=True)
        self.assertTrue(src.exists())  # untouched
        self.assertEqual(res["operations"], [])
        self.assertEqual(res["final_summary"]["would_change_count"], 1)

    def test_target_exists_row_skipped(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        clean = self.make_folder("Jade/Hot & Wet (2003)")  # collision already present
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 0)
        self.assertEqual(res["skipped"][0]["reason"], "target_exists")
        self.assertTrue(src.exists() and clean.exists())  # neither touched
        self.assertEqual(res["operations"], [])

    def test_path_escape_refused(self):
        outside = self.root / "outside" / f"Album{PH}"
        outside.mkdir(parents=True)
        (outside / "x.flac").write_bytes(b"a")
        res = self.apply([outside])
        self.assertEqual(res["final_summary"]["changed_count"], 0)
        self.assertTrue(res["errors"], res)
        self.assertEqual(res["errors"][0]["code"], "folder_cleanup_path_out_of_root")
        self.assertTrue(outside.exists())  # not moved

    def test_resume_after_interruption(self):
        a = self.make_folder(f"A/One (2001){PH}")
        b = self.make_folder(f"B/Two (2002){PH}")

        # Interrupt after the first folder: a cancel flag that trips once.
        trip = threading.Event()
        real_rename = cw.safe_rename_library_folder

        def rename_then_cancel(*args, **kw):
            out = real_rename(*args, **kw)
            trip.set()
            return out

        with mock.patch.object(cw, "safe_rename_library_folder", rename_then_cancel):
            first = self.apply([a, b], cancel_event=trip)
        self.assertEqual(first["final_summary"]["changed_count"], 1, first)
        self.assertEqual(len(first["done_folders"]), 1)
        done_one = first["done_folders"][0]
        self.assertFalse(Path(done_one).exists())  # first renamed
        remaining = b if Path(done_one) == a else a
        self.assertTrue(remaining.exists())        # second not yet done

        # Resume: skip the finished folder, finish the rest.
        second = self.apply([a, b], resume_done=set(first["done_folders"]))
        self.assertEqual(second["final_summary"]["changed_count"], 1, second)
        self.assertFalse(remaining.exists())
        self.assertTrue((self.music / "A/One (2001)").is_dir())
        self.assertTrue((self.music / "B/Two (2002)").is_dir())

    def test_rollback_restores(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        res = self.apply([src])
        self.assertFalse(src.exists())
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["final_summary"]["rolled_back_count"], 1)
        self.assertTrue(src.exists())  # original name restored
        self.assertFalse((self.music / "Jade/Hot & Wet (2003)").exists())

    def test_rollback_spans_resumed_runs(self):
        a = self.make_folder(f"A/One (2001){PH}")
        b = self.make_folder(f"B/Two (2002){PH}")
        first = self.apply([a, b], batch_limit=1)
        self.assertEqual(first["final_summary"]["changed_count"], 1, first)
        self.assertEqual(len(first["operations"]), 1)
        self.assertEqual(first["done_folders"], [str(a)])
        self.assertFalse(a.exists()); self.assertTrue(b.exists())
        second = self.apply([a, b], resume_done=set(first["done_folders"]), prior_operations=first["operations"])
        self.assertEqual(second["final_summary"]["changed_count"], 1, second)
        self.assertEqual(len(second["operations"]), 2, second)
        self.assertFalse(b.exists())
        rb = ls._rollback_folder_placeholder_apply(second["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["final_summary"]["rolled_back_count"], 2)
        self.assertTrue(a.exists() and b.exists())

    def test_batch_limit_leading_collision_resumes(self):
        # F3: a persistent skip at/before the batch limit must not stall resume.
        coll = self.make_folder(f"C/Coll (2000){PH}")
        self.make_folder("C/Coll (2000)")          # collision present -> coll held
        a = self.make_folder(f"A/One (2001){PH}")
        b = self.make_folder(f"B/Two (2002){PH}")
        folders = [coll, a, b]
        first = self.apply(folders, batch_limit=1)
        self.assertEqual(first["final_summary"]["changed_count"], 1, first)  # a renamed, coll skipped
        self.assertFalse(a.exists()); self.assertTrue(b.exists())
        second = self.apply(folders, batch_limit=1,
                            resume_done=set(first["done_folders"]),
                            prior_operations=first["operations"])
        self.assertEqual(second["final_summary"]["changed_count"], 1, second)  # progress past coll
        self.assertFalse(b.exists())                # b renamed on resume
        self.assertTrue(coll.exists())              # collision never touched

    def test_failed_op_id_captured_for_rollback(self):
        # F1 + F4: a row that fails AFTER leaving a rollback record keeps its
        # op in `operations`, and the run is reported as a failure.
        a = self.make_folder(f"A/One (2001){PH}")   # succeeds
        b = self.make_folder(f"B/Two (2002){PH}")   # applied, then reports failure
        real = cw.safe_rename_library_folder

        def rename_then_report_failure(src, tgt, *a_, **kw):
            out = real(src, tgt, *a_, **kw)          # renames + records a rollback-able op
            if "Two (2002)" in src:
                return {"ok": False, "code": "folder_cleanup_move_failed",
                        "error": "simulated post-apply failure", "operation_id": out["operation_id"]}
            return out

        with mock.patch.object(cw, "safe_rename_library_folder", rename_then_report_failure):
            res = self.apply([a, b])
        self.assertFalse(res["ok"], res)                           # F4
        self.assertEqual(res["final_summary"]["error_count"], 1, res)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertEqual(len(res["operations"]), 2, res["operations"])  # failed op NOT dropped
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["final_summary"]["rolled_back_count"], 2)
        self.assertTrue(a.exists() and b.exists())                 # both restored

    def test_crash_before_checkpoint_still_rollbackable(self):
        # F2: a crash between the rename and recording its op must still be
        # rollback-able -- the write-ahead marker is resolved from the store.
        a = self.make_folder(f"A/One (2001){PH}")
        real = cw.safe_rename_library_folder

        def rename_then_crash(src, tgt, *a_, **kw):
            real(src, tgt, *a_, **kw)                # physical rename + store record
            raise RuntimeError("crash after rename, before checkpoint")

        with mock.patch.object(cw, "safe_rename_library_folder", rename_then_crash):
            res = self.apply([a])
        self.assertFalse(res["ok"], res)                           # F4
        self.assertFalse(a.exists())                               # the rename happened
        self.assertTrue((self.music / "A/One (2001)").is_dir())
        self.assertTrue(any(o.get("pending") for o in res["operations"]), res["operations"])
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["final_summary"]["rolled_back_count"], 1)
        self.assertTrue(a.exists())                                # restored
        self.assertFalse((self.music / "A/One (2001)").is_dir())

    def test_source_missing_on_resume_recovers_op(self):
        # F2 (b): on resume a vanished source is recovered from the store, not
        # silently marked done with its op dropped.
        a = self.make_folder(f"A/One (2001){PH}")
        first = self.apply([a])
        self.assertTrue(first["ok"], first)
        self.assertFalse(a.exists())
        # Resume with NO carried operations (checkpoint lost): the renamed
        # source is gone, but its op must be recovered so rollback still works.
        second = self.apply([a], resume_done=set(), prior_operations=[])
        self.assertEqual(second["skipped"][0]["reason"], "source_missing", second)
        self.assertEqual(len(second["operations"]), 1, second["operations"])
        rb = ls._rollback_folder_placeholder_apply(second["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertTrue(a.exists())

    def test_empty_clean_name_skipped(self):
        src = self.make_folder(PH)  # name is only the placeholder -> empty after strip
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 0)
        self.assertEqual(res["skipped"][0]["reason"], "no_clean_name")
        self.assertTrue(src.exists())

    def test_resume_after_partial_failure_no_dup_and_rollback_ok(self):
        # C4: a resume of a partially-failed apply must not duplicate the failed
        # row's op (which rolled back twice -> rollback_not_eligible) and
        # rollback-by-job must report ok when the restore completes.
        a = self.make_folder("A/One (2001){Album MbId}")
        b = self.make_folder("B/Two (2002){Album MbId}")
        real = cw.safe_rename_library_folder
        def rename_then_fail(src, tgt, *a_, **kw):
            out = real(src, tgt, *a_, **kw)
            if "Two (2002)" in src:
                return {"ok": False, "code": "folder_cleanup_move_failed", "error": "x", "operation_id": out["operation_id"]}
            return out
        with mock.patch.object(cw, "safe_rename_library_folder", rename_then_fail):
            first = self.apply([a, b])
        self.assertFalse(first["ok"]); self.assertEqual(first["done_folders"], [str(a)])
        second = self.apply([a, b], resume_done=set(first["done_folders"]), prior_operations=list(first["operations"]))
        ids = [o.get("operation_id") for o in second["operations"]]
        self.assertEqual(len(ids), len(set(ids)), ids)
        rb = ls._rollback_folder_placeholder_apply(second["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb["errors"])
        self.assertTrue(a.exists() and b.exists())

    def test_unapplied_failed_op_is_rollback_noop(self):
        # C7: a failure that carries an operation_id but mutated nothing (an
        # unapplied Preview tx) must be a rollback no-op, not a rollback failure.
        from backend.transaction_engine import create_folder_cleanup_plan
        a = self.make_folder(f"A/One (2001){PH}")
        def fail_unapplied(src, tgt, *a_, **kw):
            plan = create_folder_cleanup_plan(self.store, {"action": "safe_rename", "source": src, "target": tgt})
            return {"ok": False, "code": "not_approved", "error": "not approved",
                    "operation_id": plan["operation_id"]}
        with mock.patch.object(cw, "safe_rename_library_folder", fail_unapplied):
            res = self.apply([a])
        self.assertFalse(res["ok"], res)                         # F4: run with an error
        self.assertEqual(len(res["operations"]), 1, res["operations"])
        self.assertTrue(a.exists())                              # never renamed
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)                            # proven-unapplied op = no-op
        self.assertTrue(a.exists())

    def test_held_tracked_row_does_not_consume_batch_budget(self):
        # C5: a leading HELD (DB-tracked) row -- here a disc-subfolder album the
        # top gate now catches descendant-aware (C6) -- must NOT consume
        # batch_limit, else the next actionable row is never reached and a
        # batched resume stalls. Pre-fix the held row ate the budget.
        leading = self.make_folder(f"A/Disc (2001){PH}")      # album folder
        (leading / "CD1").mkdir()                             # tracks live in a disc subfolder
        (leading / "CD1" / "01 track.flac").write_bytes(b"audio")
        plain = self.make_folder(f"B/Plain (2002){PH}")        # disk-only, actionable
        # The adapter reports the one Beets item under leading/CD1 (a descendant
        # of leading), nothing under plain.
        tracked = FakeAdapter(items={1: {"id": 1, "album_id": 1,
                                         "path": str(leading / "CD1" / "01 track.flac")}})
        with mock.patch.object(cw, "beets_adapter", tracked):
            res = ls._apply_folder_name_placeholders(
                [str(leading), str(plain)], dry_run=False, batch_limit=1,
                adapter=tracked, store=self.store)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)  # plain renamed within budget
        self.assertTrue(leading.exists())                                # held, untouched
        self.assertFalse(plain.exists())                                 # renamed
        self.assertTrue((self.music / "B/Plain (2002)").is_dir())
        self.assertIn("tracked_needs_relocation", {s["reason"] for s in res["skipped"]}, res)


class TrackedFolderHeldTests(unittest.TestCase):
    """F5 decision: a DB-tracked placeholder folder is NOT relocated by this
    disk-only rename flow. Its clean destination is whatever Beets' path
    templates compute -- which this flow cannot preview and which could merge
    into an existing folder via Beets suffixing. So a tracked folder is held
    for review (relocate the album directly instead), and preview == apply."""

    def setUp(self):
        from tests.test_album_relocation_transaction import _Engine
        self.eng = _Engine()
        self.eng.setUp()
        self.addCleanup(self.eng.doCleanups)

    def test_tracked_folder_is_held_not_renamed(self):
        eng = self.eng
        folder = os.path.join(eng.music, "A", f"B{PH}")
        album = eng.make_album(folder)
        before_paths, _art = eng.beets_state(album.id)

        res = ls._apply_folder_name_placeholders(
            [folder], dry_run=False, adapter=eng.ad, store=eng.store)
        self.assertTrue(res["ok"], res)                      # a held row is not an error
        self.assertEqual(res["final_summary"]["changed_count"], 0, res)
        self.assertEqual(res["operations"], [], res)         # nothing mutated
        self.assertEqual(res["skipped"][0]["reason"], "tracked_needs_relocation", res)
        self.assertTrue(os.path.exists(folder))              # folder untouched
        self.assertEqual(eng.beets_state(album.id)[0], before_paths)  # DB untouched

    def test_tracked_preview_matches_apply(self):
        eng = self.eng
        folder = os.path.join(eng.music, "A", f"B{PH}")
        eng.make_album(folder)
        preview = ls._apply_folder_name_placeholders(
            [folder], dry_run=True, adapter=eng.ad, store=eng.store)
        apply = ls._apply_folder_name_placeholders(
            [folder], dry_run=False, adapter=eng.ad, store=eng.store)
        # preview holds the tracked folder exactly as apply does
        self.assertEqual(preview["skipped"][0]["reason"], "tracked_needs_relocation", preview)
        self.assertEqual(apply["skipped"][0]["reason"], "tracked_needs_relocation", apply)
        self.assertEqual(preview["final_summary"]["would_change_count"], 0, preview)
        self.assertEqual(apply["final_summary"]["changed_count"], 0, apply)

    def test_disc_subfolder_album_held_preview_equals_apply(self):
        # C6: a DB-tracked album whose tracks live under a DISC SUBFOLDER must be
        # HELD (tracked_needs_relocation) identically in preview and apply.
        # Pre-fix the top gate counted only immediate-dirname items, so preview
        # said "would rename" while apply refused (folder_cleanup_db_references).
        eng = self.eng
        album_folder = os.path.join(eng.music, "A", f"B{PH}")
        eng.make_album(os.path.join(album_folder, "CD1"))  # items under a disc subfolder
        with mock.patch.object(cw, "beets_adapter", eng.ad):
            preview = ls._apply_folder_name_placeholders(
                [album_folder], dry_run=True, adapter=eng.ad, store=eng.store)
            apply = ls._apply_folder_name_placeholders(
                [album_folder], dry_run=False, adapter=eng.ad, store=eng.store)
        self.assertEqual(preview["skipped"][0]["reason"], "tracked_needs_relocation", preview)
        self.assertEqual(apply["skipped"][0]["reason"], "tracked_needs_relocation", apply)
        self.assertEqual(preview["final_summary"]["would_change_count"], 0, preview)
        self.assertEqual(apply["final_summary"]["changed_count"], 0, apply)
        self.assertEqual(apply["operations"], [], apply)
        self.assertTrue(os.path.exists(album_folder))  # untouched


class _FakeJob:
    def __init__(self, status, job_id="started-job"):
        self.status = status
        self.job_id = job_id
        self.state = {"checkpoint": {"done_folders": [], "operations": []}}
        self.result = {}


class _FakeJobs:
    """Returns one job for any id; start_python returns a stub without running
    the work, so a guard that (correctly) passes starts nothing real."""
    def __init__(self, job):
        self.job = job

    def get(self, _job_id):
        return self.job

    def start_python(self, *_a, **_kw):
        return _FakeJob("running", job_id="started-job")


class RunningJobGuardTests(unittest.TestCase):
    """F6: resume_from_job / rollback job_id must refuse a non-terminal source
    job, whose checkpoint would miss the ops it has not recorded yet."""

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

    def test_resume_refuses_running_source_job(self):
        self._patch_jobs("running")
        r = self.client.post("/api/library/folder-placeholders/apply",
                             json={"dry_run": True, "resume_from_job": "job-x"})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["ok"])

    def test_resume_allows_terminal_source_job(self):
        self._patch_jobs("success")
        r = self.client.post("/api/library/folder-placeholders/apply",
                             json={"dry_run": True, "resume_from_job": "job-x"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))  # guard passes, job starts

    def test_rollback_refuses_running_source_job(self):
        self._patch_jobs("running")
        r = self.client.post("/api/library/folder-placeholders/rollback",
                             json={"job_id": "job-x"})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
