"""Library-mutation safety for 0.4 (#335, #327 and the failure-injection
matrix). Real BeetsAdapter -> real webmanager plugin -> real Beets library
and files (Flask test client in place of HTTP), on synthetic fixtures."""

import os
import unittest
from pathlib import Path
from unittest import mock

import app  # noqa: F401  (registers the routes rollback_eligibility lives in)
import backend.composite_workflows as cw
import backend.library_service as ls
from backend import transaction_engine as te
from backend.beets_adapter import BeetsAdapterError, BeetsAdapterTimeoutError
from routes_maintenance import rollback_eligibility
from tests._folder_ops_local import LocalFolderOps
from tests.test_album_relocation_torrent_source import ident
from tests.test_album_relocation_transaction import _Engine, tree
from tests.test_arch003_wave33_library_mbsync_move_all import LibraryTablesFixture as _MoveAllFixture
from tests.test_staging_mutation_hardening import _RouteEnv as _RouteEnvBase
from tests.test_wave0_s1_containment import _Env as _FolderEnv


class _TorrentEnv(_Engine):
    """An album whose rows point into a preserved torrent folder, as after a
    linked relocation's rollback (#332)."""

    def setUp(self):
        super().setUp()
        for name, value in {"TORRENT_SOURCE_ROOTS": [Path(self.downloads)],
                            "DOWNLOADS_ALLOWED_ROOTS": [Path(self.downloads)],
                            "MUSIC_ROOT": Path(self.music), "TORRENT_SOURCE_MOVE_ALLOWED": False}.items():
            patcher = mock.patch.object(ls, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.album = self.make_album(os.path.join(self.downloads, "torrentA"))
        self.paths = self.beets_state(self.album.id)[0]
        self.originals = {p: ident(p) for p in self.paths.values()}

    def assert_torrent_untouched(self):
        self.assertEqual({p: ident(p) for p in self.originals}, self.originals)  # bytes, inode, size, mtime
        self.assertEqual(self.beets_state(self.album.id)[0], self.paths)
        self.assertEqual(tree(self.music), {})


class TorrentSourceModifyTests(_TorrentEnv):
    """#335: no Web Manager modify path moves or tag-writes a torrent source."""

    def test_artist_folder_reconcile_never_moves_or_writes_a_torrent_album(self):
        plan = cw.plan_artist_folder_reconcile({"album_ids": [self.album.id], "canonical_name": "Canon"},
                                               adapter=self.ad, store=self.store)
        res = cw.apply_artist_folder_reconcile(plan["operation_id"], adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["preserved_album_ids"], [self.album.id])
        self.assertEqual(self.lib.get_album(self.album.id).albumartist, "Canon")  # stored in Beets
        self.assert_torrent_untouched()
        self.assertIn("preserved torrent source", " ".join(map(str, self.store.get(plan["operation_id"])["logs"])))

    def test_rollback_restore_never_moves_or_writes_a_torrent_album(self):
        before = {"album": {"id": self.album.id, "fields": {"albumartist": "Old", "album": "Older"}},
                  "items": [{"id": iid, "fields": {"title": "Was"}} for iid in self.paths]}
        self.assertEqual(cw._restore_rows(self.ad, before, write=True), (1 + len(self.paths), 0))
        self.assertEqual(self.lib.get_album(self.album.id).albumartist, "Old")
        self.assertEqual({it.title for it in self.lib.get_album(self.album.id).items()}, {"Was"})
        self.assert_torrent_untouched()

    def test_metadata_edit_with_forced_write_never_touches_a_torrent_album(self):
        res = cw.update_album_metadata(self.album.id, {"albumartist": "Edited"}, force_write_tags=True,
                                       adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.lib.get_album(self.album.id).albumartist, "Edited")
        self.assert_torrent_untouched()

    def test_unevaluable_rule_keeps_the_files_in_place(self):
        with mock.patch.object(ls, "_preserve_torrent_source_file", side_effect=OSError("boom")):
            res = cw._modify(self.ad, {"albumartist": "X"}, album_ids=[self.album.id], write=True, move=True)
        self.assertEqual(res["preserved_album_ids"], [self.album.id])
        self.assert_torrent_untouched()

    def test_library_album_still_moves_and_writes(self):
        album = self.make_album(os.path.join(self.music, "A", "B"))
        res = cw._modify(self.ad, {"albumartist": "Moved"}, album_ids=[album.id], write=True, move=True)
        self.assertFalse(res.get("preserved_album_ids"))
        self.assertTrue(all(os.sep + "Moved" + os.sep in p for p in self.beets_state(album.id)[0].values()))


class PluginModifyContractTests(_Engine):
    def post(self, body):
        return self.client.post("/webmanager/modify", json=body,
                                headers={"Authorization": f"Bearer {self.token}"})

    def test_modify_never_moves_unless_asked(self):
        """#335: /webmanager/modify defaults to move=False."""
        album = self.make_album(os.path.join(self.music, "A", "B"))
        before = self.beets_state(album.id)
        r = self.post({"album_ids": [album.id], "fields": {"albumartist": "Other"}, "write": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.beets_state(album.id), before)
        self.assertEqual(self.lib.get_album(album.id).albumartist, "Other")

    def test_artpath_is_validated(self):
        """#327: artpath only names an existing image inside the allowed roots,
        with no symlink; clearing it is allowed."""
        album = self.make_album(os.path.join(self.music, "A", "B"))
        cover = os.fsdecode(album.artpath)
        outside = os.path.join(self.td, "outside.jpg")
        with open(outside, "wb") as f:
            f.write(b"\xff\xd8\xff")
        not_image = os.path.join(self.music, "A", "B", "notes.txt")
        with open(not_image, "w") as f:
            f.write("x")
        bad = [outside, not_image, os.path.join(self.music, "A", "B", "missing.jpg"),
               os.path.join(self.music, "A", "..", "A", "B", "cover.jpg"), "relative/cover.jpg", 7]
        link = os.path.join(self.music, "A", "link.jpg")
        try:
            os.symlink(outside, link)
            bad.append(link)
        except (OSError, NotImplementedError):
            pass
        for value in bad:
            r = self.post({"album_ids": [album.id], "fields": {"artpath": value}, "write": False})
            self.assertEqual((r.status_code, r.get_json()["error_code"]), (400, "ARTPATH_INVALID"), value)
            self.assertEqual(os.fsdecode(self.lib.get_album(album.id).artpath), cover)
        self.assertEqual(self.post({"album_ids": [album.id], "fields": {"artpath": ""}, "write": False}).status_code, 200)
        self.assertFalse(self.lib.get_album(album.id).artpath)
        r = self.post({"album_ids": [album.id], "fields": {"artpath": cover}, "write": False})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(os.fsdecode(self.lib.get_album(album.id).artpath), cover)


class PartialCancelAuditTests(_RouteEnvBase):
    """#317: a cooperative cancel after the job started changing the library
    is never audited as Cancelled ("nothing happened")."""

    def _run(self, body):
        import threading
        import time
        import backend.transaction_service as ts
        from job_engine import JobStore
        store_jobs = JobStore(self.data / "jobs")
        for p in (mock.patch.object(ts, "jobs", store_jobs), mock.patch.object(ts, "transactions", self.store)):
            p.start()
            self.addCleanup(p.stop)
        ts._install_transaction_job_hooks()
        self.addCleanup(store_jobs.close)
        started, release = threading.Event(), threading.Event()

        def gated(log, cancel, update_state=None):
            started.set()
            release.wait(5)
            return body(log, cancel, update_state)

        job = store_jobs.start_python(gated, label="Clean All", metadata={"transaction": {"operation_type": "Library Cleanup"}})
        self.assertTrue(started.wait(5))
        job.kill()
        release.set()
        deadline = time.time() + 5
        tx_id = job.metadata["transaction_id"]
        while time.time() < deadline and self.store.get(tx_id)["status"] == "Running":
            time.sleep(0.01)
        return job, self.store.get(tx_id)

    def test_cancel_after_a_mutation_started_is_failed_partially_applied(self):
        def body(log, cancel, update_state):
            update_state({"mutation_started": True})
            if cancel.is_set():
                raise RuntimeError("cancelled")
        _job, tx = self._run(body)
        self.assertEqual(tx["status"], "Failed")
        self.assertTrue(tx["metadata"]["engine_result"]["partial_cancel"])
        logs = " ".join(map(str, tx.get("logs") or []))
        self.assertIn("partially applied", logs)
        self.assertNotIn("had already done its work", logs)

    def test_cooperative_return_after_a_mutation_is_failed_too(self):
        def body(log, cancel, update_state):
            update_state(mutation_started=True)
            if cancel.is_set():
                return {"ok": False, "partial": True}
        _job, tx = self._run(body)
        self.assertEqual(tx["status"], "Failed")

    def test_cancel_before_any_mutation_stays_cancelled(self):
        def body(log, cancel, update_state):
            if cancel.is_set():
                raise RuntimeError("cancelled")
            update_state({"mutation_started": True})
        _job, tx = self._run(body)
        self.assertEqual(tx["status"], "Cancelled")
        self.assertFalse((tx["metadata"].get("engine_result") or {}).get("partial_cancel"))


class _Beets(LocalFolderOps):
    """Plugin step code with a durable key registry (as plugin 1.10+). Steps
    whose key ends with one of ``silent`` run but their reply is lost; steps
    whose target ends with one of ``fail`` are refused."""

    def __init__(self, root, silent=(), fail=()):
        super().__init__(root)
        self.registry, self.silent, self.fail = {}, set(silent), set(fail)

    def folder_op(self, op, key, **paths):
        if any(key.endswith(s) for s in self.silent):  # done once, never answered
            if key not in self.registry:
                self.registry[key] = super().folder_op(op, key, **paths)
            raise BeetsAdapterTimeoutError("read timed out")
        if key in self.registry:
            self.calls.append((op, key, paths))
            return {"operation_id": key, "status": "succeeded", "result": self.registry[key]}
        if any(str(paths.get("target", "")).endswith(s) for s in self.fail):
            self.calls.append((op, key, paths))
            raise BeetsAdapterError("refused", status_code=500, error_code="FOLDER_OP_FAILED")
        self.registry[key] = res = super().folder_op(op, key, **paths)
        return res


class FolderCleanupRollbackRetryTests(_FolderEnv):
    """#298 N4/L1/L2 on the folder cleanup engine, steps run by plugin code."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(te, "_FOLDER_STEP_RETRY_DELAY", 0)
        p.start()
        self.addCleanup(p.stop)
        self.src, self.dst = self.music / "Old", self.music / "New"
        self.src.mkdir()
        self.dst.mkdir()
        for name in ("a.txt", "b.txt"):
            (self.src / name).write_text(name)
        plan = te.create_folder_cleanup_plan(self.store, {"action": "merge", "source": str(self.src),
                                                          "target": str(self.dst)})
        self.op = plan["operation_id"]
        self.store.transition(self.op, "Preview", "Approved")

    def use(self, beets):
        p = mock.patch("backend.beets_adapter.beets_adapter.folder_op", beets.folder_op)
        p.start()
        self.addCleanup(p.stop)

    def test_partially_rolled_back_retries_the_rest_without_replaying_apply_keys(self):
        beets = _Beets(self.music, silent={":apply:1"})
        self.use(beets)
        res = te.execute_folder_cleanup_apply(self.store, self.op)
        self.assertEqual((res["status"], res["mutated"]), ("Failed", True))  # the second move is unconfirmed
        self.assertEqual(len(self.store.get(self.op)["metadata"]["unconfirmed_steps"]), 1)

        beets.silent, beets.fail = set(), {"a.txt"}
        first = cw.rollback_folder_cleanup(self.op, store=self.store)
        self.assertEqual(first["status"], "Partially Rolled Back", first)
        tx = self.store.get(self.op)
        self.assertTrue(rollback_eligibility(tx)["allowed"], rollback_eligibility(tx))

        beets.fail, beets.calls = set(), []
        beets.registry.clear()  # pruned: a replayed apply key would run afresh
        again = cw.rollback_folder_cleanup(self.op, store=self.store)
        self.assertEqual((again["ok"], again["status"]), (True, "Rolled Back"), again)
        self.assertFalse([c for c in beets.calls if ":apply:" in c[1]], beets.calls)
        self.assertEqual(sorted(p.name for p in self.src.iterdir()), ["a.txt", "b.txt"])
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_5xx_without_a_plugin_code_is_unconfirmed(self):
        """L2: a proxy 502 / crashed handler is not proof the step did nothing."""
        beets = mock.Mock()
        beets.folder_op.side_effect = BeetsAdapterError("bad gateway", status_code=502,
                                                        error_code="BEETS_UPSTREAM_ERROR")
        res = te.execute_folder_cleanup_apply(self.store, self.op, adapter=beets)
        self.assertEqual(beets.folder_op.call_count, te._FOLDER_STEP_ATTEMPTS)
        self.assertEqual(res["step_error_code"], "FOLDER_OP_UNCONFIRMED")
        self.assertEqual(len(self.store.get(self.op)["metadata"]["unconfirmed_steps"]), 1)

    def test_failure_log_names_the_code_not_the_reply(self):
        """L1: Beets' reply text and unknown codes never reach the audit log."""
        beets = mock.Mock()
        beets.folder_op.side_effect = BeetsAdapterError("<b>secret-ish text</b>", status_code=500,
                                                        error_code="WEIRD<b>")
        te.execute_folder_cleanup_apply(self.store, self.op, adapter=beets)
        logs = " ".join(self.store.get(self.op)["logs"])
        self.assertIn("FOLDER_OP_FAILED", logs)
        self.assertNotIn("WEIRD", logs)
        self.assertNotIn("secret-ish", logs)


class MoveAllFolderCleanupFailureTests(_MoveAllFixture):
    """#298: a folder cleanup that stopped part-way fails Move All, naming the
    transaction to roll back; a refused (unchanged) one stays a warning."""

    def _move_all(self, apply_res):
        with mock.patch.object(app.composite_workflows, "list_distinct_item_paths", return_value=["A/B/t.mp3"]), \
             mock.patch.object(app.composite_workflows, "move_library",
                               return_value={"ok": True, "returncode": 0, "stdout": ""}), \
             mock.patch.object(app.composite_workflows, "plan_folder_cleanup",
                               return_value={"ok": True, "operation_id": "txn_x"}), \
             mock.patch.object(app.composite_workflows, "apply_folder_cleanup", return_value=apply_res):
            return self._run(app.library_move_all, "/api/library/move-all")

    def test_part_way_failure_fails_the_job(self):
        with self.assertRaises(RuntimeError):
            self._move_all({"ok": False, "mutated": True, "error": "Move failed"})

    def test_refusal_without_change_is_a_warning(self):
        log = self._move_all({"ok": False, "mutated": False, "error": "not empty"})
        self.assertTrue(any("[warn]" in line for line in log))


class AlbumMetadataWriteFailedRollbackTests(_Engine):
    """#318: Beets stores an album edit but cannot write the tags
    (WRITE_FAILED). The Failed transaction is offered for rollback, and
    rollback restores the stored values."""

    def test_write_failed_album_edit_rolls_back(self):
        from beets.library import Item
        from beets.library.exceptions import WriteError
        album = self.make_album(os.path.join(self.music, "A", "B"))
        old = self.lib.get_album(album.id).album
        denied = WriteError(self.music, PermissionError(13, "Permission denied"))
        with mock.patch.object(Item, "write", side_effect=denied):
            res = cw.update_album_metadata(album.id, {"album": "Edited"}, adapter=self.ad, store=self.store)
        self.assertFalse(res["ok"], res)
        tx = self.store.list(limit=5)[0][0]
        tx = self.store.get(tx["id"])
        self.assertEqual((tx["status"], tx["metadata"]["mutation_family"]), ("Failed", cw.ALBUM_METADATA_FAMILY))
        self.assertEqual(self.lib.get_album(album.id).album, "Edited")  # Beets kept the DB edit
        self.assertTrue(rollback_eligibility(tx)["allowed"], rollback_eligibility(tx))
        import routes_maintenance
        rb = routes_maintenance._ROLLBACK_ONLY_FAMILIES[cw.ALBUM_METADATA_FAMILY](tx["id"], adapter=self.ad,
                                                                                    store=self.store)
        self.assertEqual(rb["status"], "Rolled Back", rb)
        self.assertEqual(self.lib.get_album(album.id).album, old)


class ItemMetadataWriteFailedRollbackTests(_RouteEnvBase):
    """#318: a Failed item edit keeps its metadata_restore rollback."""

    def _apply(self, update_side_effect):
        import backend.transaction_service as ts
        op = self.store.create(operation_type="Metadata Update", status="Approved",
                               metadata={"item_id": 3, "pending_fields": {"title": "New"}})["id"]
        self.store.update(op, rollback={"available": True, "operations": [
            {"type": "metadata_restore", "item_id": 3, "fields": {"title": "Old"}}]})
        started = {}

        def start_python(fn, label="", metadata=None):
            started["fn"] = fn
            return mock.Mock(job_id="job1")
        for p in (mock.patch.object(ts, "transactions", self.store),
                  mock.patch.object(ts.jobs, "start_python", side_effect=start_python),
                  mock.patch.object(ts, "_invalidate_lib_cache"),
                  mock.patch.object(ts.composite_workflows, "update_item_metadata",
                                    side_effect=update_side_effect)):
            p.start()
            self.addCleanup(p.stop)
        ts._start_metadata_apply_transaction(op)
        return op, started["fn"]

    def test_failed_apply_is_rollbackable(self):
        op, fn = self._apply(BeetsAdapterError("write failed", status_code=500, error_code="WRITE_FAILED"))
        with self.assertRaises(BeetsAdapterError):
            fn([])
        tx = self.store.get(op)
        self.assertEqual(tx["status"], "Failed")
        self.assertTrue(rollback_eligibility(tx)["allowed"], rollback_eligibility(tx))

    def test_route_dispatches_album_metadata_rollback(self):
        import routes_maintenance
        op = self.store.create(operation_type="Metadata Update", status="Failed",
                               metadata={"mutation_family": cw.ALBUM_METADATA_FAMILY,
                                         "engine_result": {"mutation_started": True}})["id"]
        fake = mock.Mock(return_value={"ok": True, "operation_id": op, "status": "Rolled Back"})
        with mock.patch.dict(routes_maintenance._ROLLBACK_ONLY_FAMILIES, {cw.ALBUM_METADATA_FAMILY: fake}):
            resp = self.client.post(f"/api/transactions/{op}/rollback")
            self.assertEqual(resp.status_code, 200, resp.get_json())
            fake.assert_called_once_with(op)
        # The generic apply route would skip apply_album_metadata's re-validation.
        self.assertNotIn(cw.ALBUM_METADATA_FAMILY, routes_maintenance._ENGINE_FAMILIES)

    def test_apply_replay_names_the_status(self):
        """D15: replaying apply on a Completed edit does not ask to approve it."""
        import backend.transaction_service as ts
        op = self.store.create(operation_type="Metadata Update", status="Completed",
                               metadata={"item_id": 3, "pending_fields": {"title": "New"}})["id"]
        with mock.patch.object(ts, "transactions", self.store):
            with self.assertRaises(ValueError) as ctx:
                ts._start_metadata_apply_transaction(op)
        self.assertIn("status is Completed", str(ctx.exception))
        self.assertNotIn("Approve", str(ctx.exception))

    def test_job_that_never_started_is_not_rollbackable(self):
        import backend.transaction_service as ts
        op = self.store.create(operation_type="Metadata Update", status="Approved",
                               metadata={"item_id": 3, "pending_fields": {"title": "New"}})["id"]
        self.store.update(op, rollback={"available": True, "operations": [
            {"type": "metadata_restore", "item_id": 3, "fields": {"title": "Old"}}]})
        with mock.patch.object(ts, "transactions", self.store), \
             mock.patch.object(ts.jobs, "start_python", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ts._start_metadata_apply_transaction(op)
        self.assertFalse(rollback_eligibility(self.store.get(op))["allowed"])


class TransactionSettingsRouteTests(_RouteEnvBase):
    """D8: the route answers 400 with the reason; nothing is clamped or saved."""

    def test_out_of_range_is_a_400(self):
        before = self.client.get("/api/transactions/settings").get_json()["settings"]
        for body in ({"automatic_approval_threshold": 0.0}, {"automatic_approval_threshold": 1.5},
                     {"backup_retention_days": -3}, [1]):
            res = self.client.post("/api/transactions/settings", json=body)
            self.assertEqual(res.status_code, 400, body)
            self.assertFalse(res.get_json()["ok"])
        self.assertIn("automatic_approval_threshold must be from 0.9 to 1.0",
                      self.client.post("/api/transactions/settings",
                                       json={"automatic_approval_threshold": 0}).get_json()["error"])
        self.assertEqual(self.client.get("/api/transactions/settings").get_json()["settings"], before)
        ok = self.client.post("/api/transactions/settings", json={"automatic_approval_threshold": 0.99})
        self.assertEqual((ok.status_code, ok.get_json()["settings"]["automatic_approval_threshold"]), (200, 0.99))


class FixMetadataRouteTests(_RouteEnvBase):
    """D9: fix-metadata records no wrapper "Repair" row claiming rollback is
    unavailable; the rollbackable Metadata Update row is the record, under
    the requesting user, and the job result names it."""

    def test_no_wrapper_row_and_the_real_row_is_named(self):
        import routes_library
        started = {}

        def start_python(fn, label="", metadata=None):
            started.update(fn=fn, metadata=metadata)
            return mock.Mock(job_id="job1")
        update = mock.Mock(return_value={"ok": True, "operation_id": "txn_1_abcdefgh"})
        with mock.patch.object(routes_library.jobs, "start_python", side_effect=start_python), \
                mock.patch.object(routes_library.composite_workflows, "update_album_metadata", update), \
                mock.patch.object(routes_library, "_transaction_user_label", return_value="alice"), \
                mock.patch.object(routes_library, "_invalidate_lib_cache"):
            res = self.client.post("/api/albums/5/fix-metadata", json={"album": "Fixed"})
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertIs(started["metadata"]["transaction"], False)
            out = started["fn"]([])
        update.assert_called_once_with(5, {"album": "Fixed"}, initiating_user="alice")
        self.assertEqual(out["transaction_ids"], ["txn_1_abcdefgh"])

    def test_album_metadata_row_records_the_user(self):
        album = {"id": 5, "album": "Old", "albumartist": "A"}
        ad = mock.Mock(get_album=mock.Mock(return_value=album))
        res = cw.plan_album_metadata({"album_id": 5, "updates": {"album": "New"}, "initiating_user": "alice"},
                                     adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["initiating_user"], "alice")
        self.assertNotIn("initiating_user", tx["metadata"])


class MergeDuplicateAlbumRouteTests(_RouteEnvBase):
    """D18: the route runs the album-row merge transaction (plan/approve/
    apply/rollback) and records no wrapper row claiming rollback is
    unavailable; the job result names the merge transaction."""

    def test_route_uses_the_merge_family_and_no_wrapper_row(self):
        import routes_cleanup
        album = {"id": 1, "albumartist": "A", "album": "B", "year": 2000, "mb_albumid": "", "mb_releasegroupid": ""}
        started = {}

        def start_python(fn, label="", metadata=None):
            started.update(fn=fn, metadata=metadata)
            return mock.Mock(job_id="job1")
        merge = mock.Mock(return_value={"ok": True, "moved": 2, "operation_id": "txn_1_mergeabc",
                                        "retired_album_ids": [2]})
        cwm = routes_cleanup.composite_workflows
        with mock.patch.object(cwm, "get_album", side_effect=lambda i: {**album, "id": i}), \
                mock.patch.object(cwm, "find_all_items_by_album_id", return_value=[]), \
                mock.patch.object(routes_cleanup, "_library_duplicate_merge_safety",
                                  return_value={"merge_safe": True, "merge_target_album_id": 1}), \
                mock.patch.object(cwm, "merge_duplicate_albums", merge), \
                mock.patch.object(routes_cleanup.jobs, "start_python", side_effect=start_python), \
                mock.patch.object(routes_cleanup, "_invalidate_lib_cache"):
            res = self.client.post("/api/clean/merge-duplicate-album",
                                   json={"target_album_id": 1, "source_album_id": 2})
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertIs(started["metadata"]["transaction"], False)
            out = started["fn"]([])
        merge.assert_called_once_with(1, 2)
        self.assertEqual(out["transaction_id"], "txn_1_mergeabc")


class AlbumArtDeleteTests(_Engine):
    """D10: deleting the cover removes the file (kept in the engine's
    quarantine), clears artpath, reports what it removed, and rolls back."""

    def test_delete_removes_the_file_and_rolls_back(self):
        album = self.make_album(os.path.join(self.music, "A", "B"))
        cover = os.fsdecode(self.lib.get_album(album.id).artpath)
        with open(cover, "rb") as fh:
            original = fh.read()
        res = cw.delete_album_art(album.id, adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["removed"], res["removed_count"], res["status"]), ([cover], 1, "Completed"))
        self.assertFalse(os.path.exists(cover))
        self.assertFalse(self.lib.get_album(album.id).artpath)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["metadata"]["mutation_family"], cw.ALBUM_ART_REPLACE_FAMILY)
        rb = cw.rollback_album_art_replace(res["operation_id"], adapter=self.ad, store=self.store)
        self.assertEqual(rb["status"], "Rolled Back", rb)
        with open(cover, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(os.fsdecode(self.lib.get_album(album.id).artpath), cover)

    def test_no_cover_changes_nothing_and_records_nothing(self):
        album = self.make_album(os.path.join(self.music, "A", "C"), art=False)
        res = cw.delete_album_art(album.id, adapter=self.ad, store=self.store)
        self.assertEqual((res["ok"], res["removed_count"]), (True, 0))
        self.assertEqual(self.store.list(limit=5)[0], [])

    def test_upload_floor_matches_the_usable_floor(self):
        import io
        from PIL import Image
        import backend.artwork_service as art
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "PNG")  # a valid image the art scan would call unusable
        self.assertLess(len(buf.getvalue()), art.MIN_ALBUM_ART_BYTES)
        with self.assertRaises(art.AlbumArtRequestError) as ctx:
            art._validate_album_art_bytes(buf.getvalue())
        self.assertIn("too small", ctx.exception.message)


class TransactionAttributionTests(_Engine):
    """D7: an apply or rollback records one row under the signed-in user;
    its item write no longer adds a second "Metadata Update" row as
    "operator"."""

    def test_write_inside_a_transaction_records_no_second_row(self):
        album = self.make_album(os.path.join(self.music, "A", "B"))
        iid = album.items()[0].id
        res = cw.update_item_metadata(iid, {"title": "Inner"}, within_transaction=True,
                                      adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.lib.get_item(iid).title, "Inner")
        self.assertEqual(self.store.list(limit=10)[0], [])
        cw.update_item_metadata(iid, {"title": "Standalone"}, adapter=self.ad, store=self.store)
        self.assertEqual(len(self.store.list(limit=10)[0]), 1)  # a standalone edit is still audited

    def test_label_is_the_session_user(self):
        from flask import session
        from backend import auth_service
        with app.app.test_request_context("/"):
            session["authenticated"], session["user"] = True, "alice"
            self.assertEqual(auth_service._transaction_user_label(), "alice")
        with app.app.test_request_context("/", headers={"Authorization": "Basic Ym9iOndyb25n"}):  # bob:wrong
            self.assertNotEqual(auth_service._transaction_user_label(), "bob")  # unverified name never recorded


class CleanAllBlankIdentityVerificationTests(_Engine):
    """D3: an album Beets would file under "/music/ (0000)" (blank artist and
    album) is reported by Clean All's final verification, which only scanned
    artist/album folder names before."""

    def test_blank_identity_album_is_counted_as_a_placeholder(self):
        import backend.maintenance_service as ms
        self.make_album(os.path.join(self.music, "ok"))
        blank = self.make_album(os.path.join(self.music, " (0000)"), art=False)
        blank.album, blank.albumartist = "", ""
        blank.store(inherit=False)
        log = []
        with mock.patch("backend.beets_adapter.beets_adapter", self.ad), \
                mock.patch.object(ms, "_library_health_payload", return_value={}), \
                mock.patch.object(ms, "_stamp_artist_folder_scan", return_value={}), \
                mock.patch.object(ms, "_scan_folder_name_placeholders", return_value=[]), \
                mock.patch.object(ms, "_maintenance_duplicate_recording_mbid_groups", return_value=0), \
                mock.patch.object(ms, "_maintenance_save_last_report"):
            res = ms._maintenance_final_verification(log)
        self.assertEqual(res["final_summary"]["placeholder_identity_folders_remaining"], 1)
        self.assertEqual([p["album_id"] for p in res["placeholder_folders"]], [blank.id])
        self.assertIn("[verification] Placeholder identity folders remaining: 1", log)


class RootFolderRepairLeadingSpaceTests(unittest.TestCase):
    """D6: "/music/ (0000)" holding tracked items was queued for review as
    untracked audio: absolute Beets paths were skipped and the leading space
    was stripped before matching the folder name."""

    def test_tracked_shallow_folder_with_a_leading_space_is_recognised(self):
        import tempfile
        from types import SimpleNamespace
        td = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, td, True)
        root = Path(td) / "music"
        (root / " (0000)").mkdir(parents=True)
        (root / " (0000)" / "01.flac").write_bytes(b"x")
        (root / "Untracked").mkdir()
        (root / "Untracked" / "01.flac").write_bytes(b"y")
        items = [SimpleNamespace(id=7, path=(root / " (0000)" / "01.flac").as_posix().encode())]
        with mock.patch.object(ls, "lib", SimpleNamespace(items=lambda q: items)):
            scan = ls._root_folder_repair_scan(root)
        self.assertEqual([(f["name"], f["item_ids"]) for f in scan["shallow_folders"]], [(" (0000)", [7])])
        self.assertEqual([f["name"] for f in scan["orphaned_folders"]], ["Untracked"])

    def test_artwork_path_summary_keeps_the_leading_space(self):
        from types import SimpleNamespace
        import backend.artwork_service as art
        album = SimpleNamespace(items=lambda: [SimpleNamespace(path=b" (0000)/01.flac")])
        self.assertEqual(art._album_path_repair_summary(album)["first_track_path"],
                         str(art.MUSIC_ROOT / " (0000)" / "01.flac"))


if __name__ == "__main__":
    unittest.main()
