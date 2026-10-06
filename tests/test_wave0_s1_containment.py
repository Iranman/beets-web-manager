"""Wave 0 S1 containment regressions (LT-1/1b/1c, LT-2, LT-4, LT-13, LT-16,
LT-17, MI-3, MI-4, MI-8, QA-1, O-1, album-tracks routes).

Each test fails on deb4ec3 (where the unsafe behavior was proven by the audit)
and passes once the path fails closed. A fake adapter records every mutation;
temp directories stand in for the music library and staging roots.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
from backend.beets_adapter import BeetsAdapterTimeoutError
from backend.transaction_engine import TransactionStore

REC = "5d39b3b9-290a-493e-93fc-30b5ec6c614a"
OTHER = "11111111-2222-4333-8444-555555555555"


class FakeAdapter:
    def __init__(self, items=None, albums=None):
        self.items = dict(items or {})
        self.albums = dict(albums or {})
        self.calls = []

    def get_item(self, iid):
        return self.items.get(int(iid))

    def get_album(self, aid, expand=True):
        return self.albums.get(int(aid))

    def get_items(self, query=None):
        return list(self.items.values())

    def get_albums(self, query=None):
        return list(self.albums.values())

    def find_all_items_by_album_id(self, aid):
        return [i for i in self.items.values() if i.get("album_id") == aid]

    def list_item_paths(self, details=False):
        return [{"id": i["id"], "album_id": i.get("album_id"), "path": i["path"]} for i in self.items.values()]

    def get_stats(self):
        return {"items": len(self.items), "albums": len(self.albums)}

    def remove(self, item_ids=None, album_ids=None, delete_files=False, idempotency_key=None):
        self.calls.append(("remove", sorted(item_ids or []), sorted(album_ids or []), delete_files))
        for iid in item_ids or []:
            self.items.pop(iid, None)
        for aid in album_ids or []:
            self.albums.pop(aid, None)
            for iid in [i["id"] for i in self.find_all_items_by_album_id(aid)]:
                self.items.pop(iid)
        return {"success": True}

    def move(self, item_ids=None, album_ids=None, idempotency_key=None):
        self.calls.append(("move", item_ids, album_ids))
        return {"success": True}

    def run_import(self, **kw):
        self.calls.append(("run_import", kw))
        return {"success": True}

    def quarantine_remove_items(self, entries, idempotency_key=None):
        self.calls.append(("quarantine", [e["item_id"] for e in entries]))
        removed = []
        for e in entries:
            row = self.items.pop(int(e["item_id"]))
            removed.append({"item_id": row["id"], "original_path": row["path"], "quarantine_path": "/q/x"})
        return {"success": True, "quarantine_id": "b" * 32, "removed": removed}

    def destructive_calls(self):
        return [c for c in self.calls if c[0] in ("remove", "move", "quarantine")]


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.music = self.root / "music"
        self.dl = self.root / "downloads"
        self.data = self.root / "data"
        for d in (self.music, self.dl, self.data):
            d.mkdir()
        (self.music / "keep.txt").write_text("library is mounted")
        env = mock.patch.dict(os.environ, {"MUSIC_ROOT": str(self.music), "DOWNLOAD_PATH": str(self.dl),
                                           "BEETS_IMPORT_ROOTS": str(self.dl),
                                           "WEB_MANAGER_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)
        self.store = TransactionStore(str(self.data / "tx"))
        from backend import resource_locks
        resource_locks.set_locks(resource_locks.ResourceLocks(self.data / "locks"))
        self.addCleanup(resource_locks.set_locks, None)

    def media(self, rel, data=b"audio"):
        p = self.music / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)


class OrphanCleanupTests(_Env):
    def test_present_file_is_never_removed_and_files_never_deleted(self):
        present = self.media("A/B/01.flac")
        missing = str(self.music / "A/B/02.flac")
        ad = FakeAdapter(items={1: {"id": 1, "album_id": 3, "path": present},
                                2: {"id": 2, "album_id": 3, "path": missing},
                                **{i: {"id": i, "album_id": 3, "path": present} for i in range(10, 20)}},
                         albums={3: {"id": 3}})
        res = cw.clean_orphaned_items(item_ids=[1, 2], dry_run=False, adapter=ad, store=self.store)
        self.assertTrue(res["ok"])
        self.assertEqual(ad.destructive_calls(), [("remove", [2], [], False)])
        self.assertEqual(res["removed_count"], 1)
        self.assertEqual([s["id"] for s in res["skipped"]], [1])
        self.assertTrue(os.path.exists(present))
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Completed")

    def test_empty_selection_is_refused_not_widened(self):
        ad = FakeAdapter(items={1: {"id": 1, "album_id": None, "path": str(self.music / "gone.mp3")}})
        for ids in ([], None):
            res = cw.clean_orphaned_items(item_ids=ids, dry_run=False, adapter=ad)
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "empty_selection")
        self.assertEqual(ad.destructive_calls(), [])

    def test_unusable_music_root_refuses(self):
        (self.music / "keep.txt").unlink()
        ad = FakeAdapter(items={1: {"id": 1, "album_id": 3, "path": str(self.music / "x.flac")}})
        res = cw.clean_orphaned_items(item_ids=[1], dry_run=False, adapter=ad)
        self.assertEqual(res["code"], "music_root_unusable")
        self.assertEqual(ad.destructive_calls(), [])

    def test_ratio_cap_refuses_mass_removal(self):
        ad = FakeAdapter(items={i: {"id": i, "album_id": 1, "path": str(self.music / f"{i}.flac")} for i in range(4)})
        res = cw.clean_orphaned_items(item_ids=[0, 1, 2, 3], dry_run=False, adapter=ad)
        self.assertEqual(res["code"], "too_many_missing")
        self.assertEqual(ad.destructive_calls(), [])

    def test_cleanup_service_wrapper_reports_and_never_crashes(self):
        import backend.cleanup_service as cs
        present = self.media("x.flac")
        ad = FakeAdapter(items={9: {"id": 9, "album_id": None, "path": str(self.music / "gone.flac")},
                                **{i: {"id": i, "album_id": 1, "path": present} for i in range(10, 20)}})
        with mock.patch.object(cw, "beets_adapter", ad), mock.patch.object(cw, "_default_store", self.store):
            out = cs._clean_remove_orphaned_items([9], dry_run=False, log=[], trigger_plex=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed_count"], 1)
        self.assertEqual(ad.destructive_calls(), [("remove", [9], [], False)])


class SyncDeletedTests(_Env):
    def _ad(self):
        present = self.media("p.flac")
        return FakeAdapter(items={1: {"id": 1, "album_id": 1, "path": str(self.music / "gone1.flac")},
                                  2: {"id": 2, "album_id": 1, "path": str(self.music / "gone2.flac")},
                                  **{i: {"id": i, "album_id": 1, "path": present} for i in range(3, 12)}})

    def test_missing_mount_removes_nothing(self):
        ad = FakeAdapter(items={i: {"id": i, "album_id": 1, "path": f"/nonexistent-mount/{i}.flac"}
                                for i in range(1, 51)})
        (self.music / "keep.txt").unlink()
        res = cw.sync_deleted_files(dry_run=False, adapter=ad, item_ids=list(range(1, 51)))
        self.assertFalse(res["ok"])
        self.assertEqual(ad.destructive_calls(), [])

    def test_ratio_cap(self):
        (self.music / "p.flac").write_bytes(b"x")
        ad = FakeAdapter(items={i: {"id": i, "album_id": 1, "path": str(self.music / f"g{i}.flac")}
                                for i in range(10)})
        res = cw.sync_deleted_files(dry_run=False, adapter=ad, item_ids=list(range(10)))
        self.assertEqual(res["code"], "too_many_missing")
        self.assertEqual(ad.destructive_calls(), [])

    def test_apply_requires_planned_ids_and_removes_only_those(self):
        ad = self._ad()
        preview = cw.sync_deleted_files(dry_run=True, adapter=ad)
        self.assertEqual(sorted(preview["missing_item_ids"]), [1, 2])
        self.assertIn("removed_from_db", preview)
        refused = cw.sync_deleted_files(dry_run=False, adapter=ad)
        self.assertEqual(refused["code"], "planned_ids_required")
        self.assertEqual(ad.destructive_calls(), [])
        res = cw.sync_deleted_files(dry_run=False, adapter=ad, item_ids=[1, 5], store=self.store)
        self.assertEqual(ad.destructive_calls(), [("remove", [1], [], False)])
        self.assertEqual(res["removed_from_db"], 1)

    def test_legacy_auto_scan_never_removes(self):
        import app as app_module
        ad = self._ad()
        captured = {}

        def run_now(fn, label=None, metadata=None):
            log = []
            fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="j")

        with mock.patch.object(cw, "beets_adapter", ad), \
                mock.patch.object(app_module.jobs, "start_python", side_effect=run_now), \
                mock.patch.object(app_module.threading, "Thread"), \
                mock.patch.object(cw, "lib", mock.MagicMock(get_library_stats=lambda: {"tracks": 11, "albums": 1})):
            app_module._do_scan_job()
        self.assertEqual(ad.destructive_calls(), [])
        self.assertTrue(any("not removed" in line for line in captured["log"]))


class AlbumCleanupTests(_Env):
    def _ad(self):
        return FakeAdapter(items={1: {"id": 1, "album_id": 8, "path": self.media("y/1.flac")},
                                  2: {"id": 2, "album_id": 8, "path": self.media("y/2.flac")}},
                           albums={8: {"id": 8, "album": "Y"}})

    def test_preview_cannot_apply_and_second_apply_is_refused(self):
        ad = self._ad()
        plan = cw.plan_album_cleanup(8, adapter=ad, store=self.store)
        self.assertFalse(plan["delete_files"])
        res = cw.apply_album_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(res["code"], "not_approved")
        self.assertEqual(ad.destructive_calls(), [])
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        ok = cw.apply_album_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertTrue(ok["ok"])
        again = cw.apply_album_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(again["code"], "already_applied")
        self.assertEqual(ad.destructive_calls(), [("remove", [], [8], False)])

    def test_stale_plan_is_refused(self):
        ad = self._ad()
        plan = cw.plan_album_cleanup(8, adapter=ad, store=self.store)
        ad.items[3] = {"id": 3, "album_id": 8, "path": self.media("y/3.flac")}
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        res = cw.apply_album_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(res["code"], "stale_plan")
        self.assertEqual(ad.destructive_calls(), [])

    def test_transport_error_leaves_running(self):
        ad = self._ad()
        ad.remove = mock.MagicMock(side_effect=BeetsAdapterTimeoutError("timeout"))
        plan = cw.plan_album_cleanup(8, adapter=ad, store=self.store)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        with self.assertRaises(BeetsAdapterTimeoutError):
            cw.apply_album_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Running")

    def test_failed_import_rollback_is_row_only(self):
        ad = self._ad()
        res = cw.remove_album_rows_after_failed_import(8, reason="test", adapter=ad, store=self.store)
        self.assertTrue(res["ok"])
        self.assertEqual(ad.destructive_calls(), [("remove", [], [8], False)])

    def test_library_failed_import_helper_never_deletes_files(self):
        import backend.library_service as ls
        ad = self._ad()
        with mock.patch.object(cw, "beets_adapter", ad), mock.patch.object(cw, "_default_store", self.store):
            ls._delete_album_ids_from_db([8], [], delete_files=True)
        self.assertEqual(ad.destructive_calls(), [("remove", [], [8], False)])

    def test_confirmed_import_honours_copy_mode(self):
        ad = FakeAdapter()
        plan = cw.plan_confirmed_import({"paths": [str(self.dl / "x")], "use_move": False}, store=self.store)
        cw.apply_confirmed_import(plan["operation_id"], adapter=ad, store=self.store)
        kw = [c for c in ad.calls if c[0] == "run_import"][0][1]
        self.assertTrue(kw["copy"])
        self.assertFalse(kw["move"])

    def test_album_maintenance_no_longer_fakes_track_removal(self):
        ad = self._ad()
        with mock.patch.object(cw, "lib", mock.MagicMock()):
            res = cw.delete_album(8, delete_files=True, adapter=ad, store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "album_not_empty")
        self.assertEqual(ad.destructive_calls(), [])


class TrackQuarantineTests(_Env):
    def _ad(self):
        return FakeAdapter(items={i: {"id": i, "album_id": 4, "path": self.media(f"z/{i}.flac", bytes([i]) * 10)}
                                  for i in (1, 2, 3)}, albums={4: {"id": 4}})

    def test_all_tracks_refused_and_apply_needs_approval(self):
        ad = self._ad()
        self.assertEqual(cw.plan_track_quarantine(4, [1, 2, 3], adapter=ad, store=self.store)["code"],
                         "album_would_empty")
        plan = cw.plan_track_quarantine(4, [2], adapter=ad, store=self.store)
        self.assertEqual(cw.apply_track_quarantine(plan["operation_id"], adapter=ad, store=self.store)["code"],
                         "not_approved")
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        res = cw.apply_track_quarantine(plan["operation_id"], adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(ad.destructive_calls(), [("quarantine", [2])])
        self.assertTrue(os.path.exists(self.music / "z/2.flac"), "the fake engine never deletes; nor does WM")

    def test_helper_requires_explicit_approval_and_never_deletes(self):
        from backend.matching_service import _remove_album_track_items
        ad = self._ad()
        with mock.patch.object(cw, "beets_adapter", ad), mock.patch.object(cw, "_default_store", self.store), \
                mock.patch("backend.matching_service._trigger_plex_refresh"):
            with self.assertRaises(RuntimeError):
                _remove_album_track_items(4, [1], dry_run=False, delete_files=True, log=[])
            self.assertEqual(ad.destructive_calls(), [])
            out = _remove_album_track_items(4, [1], dry_run=False, delete_files=True, log=[], approved_by="test")
        self.assertEqual(out["deleted_files"], 0)
        self.assertEqual(out["quarantined_files"], 1)
        self.assertEqual(ad.destructive_calls(), [("quarantine", [1])])


class StagingHelperTests(_Env):
    def test_delete_file_refuses_library_symlink_and_root(self):
        album = self.music / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"x")
        with self.assertRaises(ValueError):
            cw.delete_file(str(album))
        self.assertTrue(album.exists())
        with self.assertRaises(ValueError):
            cw.delete_file(str(self.dl))
        staged = self.dl / "job1"
        staged.mkdir()
        (staged / "a.flac").write_bytes(b"x")
        try:
            (self.dl / "link").symlink_to(album, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(ValueError):
                cw.delete_file(str(self.dl / "link"))
            self.assertTrue(album.exists())
        self.assertTrue(cw.delete_file(str(staged))["deleted"])
        self.assertFalse(staged.exists())

    def test_move_file_refuses_library(self):
        src = self.music / "a.flac"
        src.write_bytes(b"x")
        with self.assertRaises(ValueError):
            cw.move_file(str(src), str(self.music / "b.flac"))
        self.assertTrue(src.exists())


class NoAudioFolderTests(_Env):
    def test_music_root_refused_and_staging_rechecked(self):
        import backend.cleanup_service as cs
        empty_lib = self.music / "Empty"
        empty_lib.mkdir()
        (empty_lib / "cover.jpg").write_bytes(b"x")
        with mock.patch.object(cs, "MUSIC_ROOT", self.music), \
                mock.patch.object(cs, "FOLDER_CLEAN_ROOTS", [self.music, self.dl]):
            res = cs._delete_no_audio_folders(str(self.music), [str(empty_lib)], dry_run=False, log=[])
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "music_root_not_allowed")
            self.assertTrue(empty_lib.exists())
            junk = self.dl / "junk"
            junk.mkdir()
            (junk / "info.nfo").write_text("x")
            with mock.patch.object(cs, "_scan_no_audio_folder_candidates",
                                   return_value={"folders": [{"path": str(junk), "files": 1, "bytes": 1}]}):
                (junk / "late.flac").write_bytes(b"x")  # audio arrived after the scan
                late = cs._delete_no_audio_folders(str(self.dl), [str(junk)], dry_run=False, log=[])
                self.assertFalse(late["ok"])
                self.assertTrue(junk.exists())
                (junk / "late.flac").unlink()
                ok = cs._delete_no_audio_folders(str(self.dl), [str(junk)], dry_run=False, log=[])
            self.assertTrue(ok["ok"], ok)
            self.assertFalse(junk.exists())


class RouteTests(_Env):
    def setUp(self):
        super().setUp()
        import app as app_module
        self.app_module = app_module
        p = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def test_approve_only_from_preview(self):
        import routes_maintenance
        with mock.patch.object(routes_maintenance, "transactions", self.store):
            for status in ("Completed", "Failed", "Rolled Back", "Recovery Required", "Cancelled", "Approved"):
                tx = self.store.create(operation_type="Metadata Update", status=status)
                res = self.client.post(f"/api/transactions/{tx['id']}/approve")
                self.assertEqual(res.status_code, 409, status)
                self.assertEqual(self.store.get(tx["id"])["status"], status)
            tx = self.store.create(operation_type="Metadata Update", status="Preview")
            self.assertEqual(self.client.post(f"/api/transactions/{tx['id']}/approve").status_code, 200)
            self.assertEqual(self.store.get(tx["id"])["status"], "Approved")

    def test_album_track_removal_needs_confirmation(self):
        import routes_cleanup
        with mock.patch.object(routes_cleanup.jobs, "start_python") as start:
            r1 = self.client.post("/api/clean/album-tracks/remove",
                                  json={"album_id": 1, "item_ids": [2], "dry_run": False, "delete_files": True})
            r2 = self.client.post("/api/clean/album-tracks/remove-batch",
                                  json={"groups": [{"album_id": 1, "item_ids": [2]}], "dry_run": False})
            r3 = self.client.post("/api/clean/remove-orphaned-items", json={"item_ids": [], "dry_run": False})
        self.assertEqual((r1.status_code, r1.get_json()["code"]), (400, "confirmation_required"))
        self.assertEqual((r2.status_code, r2.get_json()["code"]), (400, "confirmation_required"))
        self.assertEqual((r3.status_code, r3.get_json()["code"]), (400, "empty_selection"))
        start.assert_not_called()

    def test_sync_deleted_apply_needs_preview_ids(self):
        res = self.client.post("/api/library/sync-deleted", json={"dry_run": False, "confirmed": True})
        self.assertEqual(res.get_json()["code"], "planned_ids_required")

    def test_album_cleanup_apply_needs_confirmation_for_file_deletion(self):
        ad = FakeAdapter(items={1: {"id": 1, "album_id": 8, "path": self.media("q/1.flac")}},
                         albums={8: {"id": 8, "album": "Q"}})
        with mock.patch.object(cw, "beets_adapter", ad), mock.patch.object(cw, "_default_store", self.store):
            bad = self.client.post("/api/albums/8/cleanup/plan", json={"delete_files": True})
            self.assertEqual(bad.get_json()["code"], "confirmation_required")
            plan = self.client.post("/api/albums/8/cleanup/plan", json={
                "delete_files": True, "confirm_delete_files": cw.DELETE_ALBUM_FILES_CONFIRMATION}).get_json()
            refused = self.client.post("/api/albums/cleanup/apply", json={"operation_id": plan["operation_id"]})
            self.assertEqual(refused.get_json()["code"], "confirmation_required")
            self.assertEqual(ad.destructive_calls(), [])
            row_only = self.client.post("/api/albums/8/cleanup/plan", json={}).get_json()
            applied = self.client.post("/api/albums/cleanup/apply", json={"operation_id": row_only["operation_id"]})
        self.assertEqual(applied.status_code, 200, applied.get_json())
        self.assertEqual(ad.destructive_calls(), [("remove", [], [8], False)])

    def test_match_album_never_deletes_unmatched_tracks(self):
        import routes_library
        album = mock.MagicMock(albumartist="A", album="B")
        plan = {"matched_count": 1, "actual_count": 2, "expected_count": 1,
                "unmatched_items": [{"id": 7, "filename": "bonus.flac"}]}
        log = []

        def run_now(fn, label=None, metadata=None):
            with self.assertRaises(RuntimeError):
                fn(log, cancel_event=None)
            return mock.Mock(job_id="j")

        with mock.patch.object(routes_library, "lib", mock.MagicMock(get_album=lambda aid: album)), \
                mock.patch.object(routes_library, "_resolve_mb_release_id", return_value=REC), \
                mock.patch.object(routes_library, "_album_mb_match_plan", return_value=plan), \
                mock.patch.object(routes_library, "_remove_album_track_items") as remove, \
                mock.patch.object(routes_library.composite_workflows, "update_album_metadata") as meta, \
                mock.patch.object(routes_library.jobs, "start_python", side_effect=run_now):
            self.client.post("/api/albums/5/match", json={"mb_id": REC})
        remove.assert_not_called()
        meta.assert_not_called()
        self.assertTrue(any("needs review" in line for line in log))


class AlbumDeduplicateTests(_Env):
    def _run(self, items, proof, confirm=False):
        import routes_library
        self.result = None

        def run_now(fn, label=None, metadata=None):
            self.result = fn([], cancel_event=None)
            return mock.Mock(job_id="j")

        plan = mock.MagicMock(return_value={"ok": True, "operation_id": "op1", "skipped": []})
        apply_ = mock.MagicMock(return_value={"ok": True, "removed": [{}], "status": "Completed"})
        with mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}), \
                mock.patch.object(routes_library, "lib", mock.MagicMock(get_album=lambda aid: mock.MagicMock())), \
                mock.patch.object(routes_library.composite_workflows, "get_album", return_value={"mb_albumid": ""}), \
                mock.patch.object(routes_library.composite_workflows, "find_all_items_by_album_id",
                                  return_value=items), \
                mock.patch.object(routes_library.composite_workflows, "relocate_album", return_value={"ok": True}), \
                mock.patch.object(routes_library, "_strip_year_from_album_name"), \
                mock.patch.object(routes_library, "same_recording_proof", side_effect=proof), \
                mock.patch.object(routes_library._duplicate_cleanup, "plan_reviewed_cleanup", plan), \
                mock.patch.object(routes_library._duplicate_cleanup, "apply_reviewed_cleanup", apply_), \
                mock.patch.object(routes_library.jobs, "start_python", side_effect=run_now):
            self.app_client().post("/api/albums/3/deduplicate", json={"confirm": confirm})
        return plan, apply_

    def app_client(self):
        import app as app_module
        return app_module.app.test_client()

    def test_disc_is_part_of_the_slot(self):
        items = [{"id": 1, "track": 3, "disc": 1, "path": "/m/a.flac"}, {"id": 2, "track": 3, "disc": 2, "path": "/m/b.flac"}]
        proof = mock.MagicMock(return_value={"proven": True, "recording_id": REC, "reason": ""})
        plan, apply_ = self._run(items, proof)
        proof.assert_not_called()
        plan.assert_not_called()

    def test_unproven_copy_is_spared_and_unmatched_kept(self):
        items = [{"id": 1, "track": 3, "disc": 1, "path": "/m/a.flac"}, {"id": 2, "track": 3, "disc": 1, "path": "/m/a.1.flac"},
                 {"id": 9, "track": 0, "disc": 1, "path": "/m/x.flac"}]
        proof = mock.MagicMock(return_value={"proven": False, "recording_id": "", "reason": "fingerprint_unavailable",
                                             "drop_status": "unavailable", "keep_status": "confirmed"})
        plan, apply_ = self._run(items, proof)
        plan.assert_not_called()
        self.assertEqual(sorted(s["item_id"] for s in self.result["spared"]), [2, 9])

    def test_proven_copy_is_planned_and_applied_only_with_confirm(self):
        items = [{"id": 1, "track": 3, "disc": 1, "path": "/m/a.flac"}, {"id": 2, "track": 3, "disc": 1, "path": "/m/a.1.flac"}]
        proof = mock.MagicMock(return_value={"proven": True, "recording_id": REC, "reason": ""})
        with mock.patch.object(cw, "_default_store", self.store):
            plan, apply_ = self._run(items, proof)
        plan.assert_called_once()
        self.assertEqual(plan.call_args[0][0], [{"delete_item_id": 2, "keep_item_id": 1}])
        apply_.assert_not_called()


class SameRecordingProofTests(_Env):
    def test_requires_both_confirmed_for_one_recording(self):
        import backend.acoustid_service as acs
        a, b = self.media("a.flac"), self.media("b.flac")
        hits = {a: [{"mb_trackid": REC, "score": 0.95}], b: [{"mb_trackid": REC, "score": 0.93}]}
        with mock.patch.object(acs, "_acoustid_hits_or_none", side_effect=lambda p: hits.get(p)):
            self.assertTrue(acs.same_recording_proof(a, b)["proven"])
            hits[a] = [{"mb_trackid": OTHER, "score": 0.95}]
            self.assertFalse(acs.same_recording_proof(a, b)["proven"])
            hits[a] = None
            self.assertEqual(acs.same_recording_proof(a, b)["reason"], "fingerprint_unavailable")
            hits[a] = [{"mb_trackid": REC, "score": 0.5}]  # weak hit is not proof
            self.assertFalse(acs.same_recording_proof(a, b)["proven"])


class IntegrityScanTests(_Env):
    def _scan(self, best_score, fp, ai=None):
        import backend.matching_service as ms
        items = [{"id": i, "title": f"t{i}", "track": i, "disc": 1, "path": f"/m/{i}.flac", "mb_trackid": "",
                  "length": 1.0} for i in (1, 2, 3, 4)]
        tracks = [{"title": "x", "disc": 1, "track": 1, "mb_trackid": REC, "title_norm": "x"}]
        patches = [
            mock.patch.object(ms, "_fetch_mb_release_tracklist", return_value={"ok": True, "tracks": tracks}),
            mock.patch.object(ms.composite_workflows, "find_all_items_by_album_id", return_value=items),
            mock.patch.object(ms, "_best_album_track_match",
                              return_value={"score": best_score, "track": tracks[0], "idx": 0}),
            mock.patch.object(ms, "_album_track_fingerprint_check", return_value=fp),
        ]
        if ai is not None:
            patches.append(mock.patch.object(ms, "_ai_review_album_track_candidates", return_value=ai))
        for p in patches:
            p.start()
        try:
            return ms._scan_album_track_integrity({"id": 9, "mb_albumid": REC}, use_ai=ai is not None,
                                                  use_fingerprint=True, fingerprint_limit=50, log=[])
        finally:
            for p in patches:
                p.stop()

    def test_title_ai_lowmatch_and_weak_conflict_only_review(self):
        from backend.matching import AcoustIDStatus
        weak = {"status": AcoustIDStatus.CONFLICT, "candidate": {"score": 70, "title": "y"}}
        res = self._scan(0.3, weak, ai={"status": "ok", "decisions": [
            {"id": i, "action": "remove", "confidence": "high", "reason": "ai"} for i in (1, 2, 3, 4)]})
        self.assertEqual(res["remove_candidates"], [])
        self.assertEqual(len(res["review_candidates"]), 4)

    def test_strong_conflict_may_propose_removal(self):
        from backend.matching import AcoustIDStatus
        strong = {"status": AcoustIDStatus.CONFLICT, "candidate": {"score": 92, "title": "y"}}
        res = self._scan(0.95, strong)
        self.assertEqual(len(res["remove_candidates"]), 4)


class AiDuplicateTests(_Env):
    def test_ai_only_match_never_becomes_a_cleanup_pair(self):
        import backend.dedup_service as ds
        src = self.media("s.flac")
        state = {"created_at": 1.0, "duplicates": [
            {"source_path": src, "lib_id": 5, "source_item_id": 6, "match_type": "AI duplicate",
             "confidence": "medium", "fingerprint_verified": False}]}
        with mock.patch.dict(ds._dedup_scans, {"ai": state}, clear=True), \
                mock.patch("backend.maintenance_service._maintenance_load_last_report", return_value={}):
            pairs, unmatched = ds._dedup_pairs_for_paths([src])
        self.assertEqual(pairs, [])
        self.assertEqual(unmatched, [src])


if __name__ == "__main__":
    unittest.main()
