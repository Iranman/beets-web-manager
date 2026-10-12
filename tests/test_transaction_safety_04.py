"""Library-mutation safety for 0.4 (#335, #327 and the failure-injection
matrix). Real BeetsAdapter -> real webmanager plugin -> real Beets library
and files (Flask test client in place of HTTP), on synthetic fixtures."""

import os
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


if __name__ == "__main__":
    import unittest
    unittest.main()
