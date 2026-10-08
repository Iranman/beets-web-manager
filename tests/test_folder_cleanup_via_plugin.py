"""Folder cleanup runs in Beets through the webmanager plugin (D2/D3).

Web Manager mounts the library read-only, so folder_cleanup_v1 apply and
rollback steps are performed by POST /webmanager/folder-op inside stock
Beets. Covered here: the plugin endpoint's containment on a real Beets
library, the adapter call, the engine's status CAS and failure reporting,
the rollback route dispatch, and Clean All's empty-folder step failing loudly.
"""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from beets.library import Item, Library
from beetsplug.web import app as beets_web_app

import backend.composite_workflows as cw
import beetsplug.webmanager.operations as ops_mod
from backend import transaction_engine as te
from backend.beets_adapter import (BeetsAdapter, BeetsAdapterError, BeetsAdapterNotFoundError,
                                   BeetsAdapterTimeoutError)
from beetsplug.webmanager import WebManagerPlugin, folder_ops
from beetsplug.webmanager.auth import set_api_key_file
from tests._folder_ops_local import LocalFolderOps, patch_local_folder_ops
from tests.test_s1_containment_followup import CAN_SYMLINK
from tests.test_staging_mutation_hardening import _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter, _Env

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


class PluginFolderOpTests(unittest.TestCase):
    """The endpoint on a real Beets library and the stock web app."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        self.music = os.path.join(self.td, "music")
        os.makedirs(os.path.join(self.music, "Artist", "Albm"))
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=self.music)
        self.addCleanup(lambda: self.lib._connection().close())
        key_file = os.path.join(self.td, "key")
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(TOKEN + "\n")
        self.plugin = WebManagerPlugin()
        set_api_key_file(key_file)
        self.addCleanup(set_api_key_file, None)
        ops_mod.set_allowed_roots([self.music])
        self.addCleanup(ops_mod.set_allowed_roots, None)
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.n = 0

    def post(self, body, auth=True):
        self.n += 1
        headers = {"Idempotency-Key": f"k{self.n}-{id(self)}"}
        if auth:
            headers["Authorization"] = f"Bearer {TOKEN}"
        return self.client.post("/webmanager/folder-op", json=body, headers=headers)

    def p(self, *parts):
        return os.path.join(self.music, *parts)

    def test_requires_auth_and_is_advertised(self):
        self.assertEqual(self.post({"op": "create_dir", "path": self.p("X")}, auth=False).status_code, 401)
        self.assertIn("folder_op", ops_mod.get_capabilities())

    def test_rename_remove_and_recreate(self):
        res = self.post({"op": "rename_dir", "source": self.p("Artist", "Albm"), "target": self.p("Artist", "Album")})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(os.path.isdir(self.p("Artist", "Album")))
        res = self.post({"op": "remove_empty_dir", "path": self.p("Artist", "Album")})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertFalse(os.path.exists(self.p("Artist", "Album")))
        self.assertTrue(os.path.isdir(self.p("Artist")))  # only the named folder is removed
        res = self.post({"op": "create_dir", "path": self.p("Artist", "Album")})
        self.assertEqual((res.status_code, res.get_json()["existed"]), (200, False))

    def test_move_file_never_overwrites(self):
        with open(self.p("Artist", "Albm", "a.txt"), "w") as fh:
            fh.write("a")
        with open(self.p("Artist", "b.txt"), "w") as fh:
            fh.write("b")
        res = self.post({"op": "move_file", "source": self.p("Artist", "Albm", "a.txt"), "target": self.p("Artist", "b.txt")})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (409, "TARGET_EXISTS"))
        with open(self.p("Artist", "b.txt")) as fh:
            self.assertEqual(fh.read(), "b")

    def test_containment_refusals(self):
        outside = os.path.join(self.td, "outside")
        os.makedirs(outside)
        cases = [
            ({"op": "remove_empty_dir", "path": outside}, "PATH_OUTSIDE_LIBRARY"),
            ({"op": "remove_empty_dir", "path": self.p("Artist", "..", "..", "outside")}, "PATH_OUTSIDE_LIBRARY"),
            ({"op": "remove_empty_dir", "path": self.music}, "PATH_OUTSIDE_LIBRARY"),
            ({"op": "remove_empty_dir", "path": "Artist/Albm"}, "PATH_INVALID"),
            ({"op": "rename_dir", "source": self.p("Artist", "Albm"), "target": os.path.join(outside, "x")},
             "PATH_OUTSIDE_LIBRARY"),
            ({"op": "delete", "path": self.p("Artist")}, "INVALID_OP"),
        ]
        for body, code in cases:
            res = self.post(body)
            self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, code), body)
        self.assertTrue(os.path.isdir(outside) and os.path.isdir(self.p("Artist", "Albm")))

    def test_non_empty_folder_is_refused(self):
        with open(self.p("Artist", "Albm", "cover.jpg"), "w") as fh:
            fh.write("x")
        res = self.post({"op": "remove_empty_dir", "path": self.p("Artist", "Albm")})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (409, "NOT_EMPTY"))
        self.assertTrue(os.path.exists(self.p("Artist", "Albm", "cover.jpg")))

    def test_file_created_after_the_emptiness_check_survives(self):
        """F1: util.prune_dirs would rmtree() a file that appears after its
        listdir; os.rmdir refuses instead."""
        late = self.p("Artist", "Albm", "late.flac")
        real_listdir = os.listdir

        def listdir_then_race(path):
            names = real_listdir(path)
            if os.path.abspath(path) == self.p("Artist", "Albm"):
                with open(late, "wb") as fh:
                    fh.write(b"x")
            return names

        with mock.patch.object(folder_ops.os, "listdir", side_effect=listdir_then_race):
            res = self.post({"op": "remove_empty_dir", "path": self.p("Artist", "Albm")})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (409, "NOT_EMPTY"))
        self.assertTrue(os.path.exists(late))

    def test_step_holds_the_plugin_mutation_lock(self):
        """F2: a folder step is serialised with every other mutation endpoint."""
        seen = []

        def step(lib, root, data):
            got = []
            t = threading.Thread(target=lambda: got.append(ops_mod.mutation_lock.acquire(blocking=False)))
            t.start()
            t.join()
            if got[0]:
                ops_mod.mutation_lock.release()
            seen.append(got[0])
            return {"op": data["op"]}

        with mock.patch.object(folder_ops, "_step", side_effect=step):
            self.assertEqual(self.post({"op": "create_dir", "path": self.p("X")}).status_code, 200)
        self.assertEqual(seen, [False])

    @unittest.skipIf(os.name == "nt", "Beets' directory PathQuery does not match on Windows")
    def test_folder_with_library_items_is_refused(self):
        path = self.p("Artist", "Albm", "01.mp3")
        with open(path, "wb") as fh:
            fh.write(b"x")
        self.lib.add(Item(path=path.encode(), title="t"))
        res = self.post({"op": "rename_dir", "source": self.p("Artist", "Albm"), "target": self.p("Artist", "Album")})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (409, "PATH_IS_TRACKED"))
        self.assertTrue(os.path.exists(path))
        # F4: nor may a step re-create a path the Beets DB still references.
        os.remove(path)
        with open(self.p("Artist", "stray.mp3"), "wb") as fh:
            fh.write(b"y")
        res = self.post({"op": "move_file", "source": self.p("Artist", "stray.mp3"), "target": path})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (409, "PATH_IS_TRACKED"))
        self.assertFalse(os.path.exists(path))

    @unittest.skipUnless(CAN_SYMLINK, "symlinks unavailable")
    def test_symlink_component_is_refused(self):
        outside = os.path.join(self.td, "outside")
        os.makedirs(os.path.join(outside, "victim"))
        os.symlink(outside, self.p("link"))
        res = self.post({"op": "remove_empty_dir", "path": self.p("link", "victim")})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, "SYMLINK_REJECTED"))
        self.assertTrue(os.path.isdir(os.path.join(outside, "victim")))


class AdapterTests(unittest.TestCase):
    def test_folder_op_posts_one_step_with_idempotency_key(self):
        ad = BeetsAdapter(base_url="http://beets:8337", api_key=TOKEN)
        with mock.patch.object(ad, "_request", return_value={"success": True}) as req:
            ad.folder_op("rename_dir", "txn_1:apply:0", source="/music/a", target="/music/b")
        req.assert_called_once_with("POST", "/webmanager/folder-op",
                                    json_data={"op": "rename_dir", "source": "/music/a", "target": "/music/b"},
                                    headers={"Idempotency-Key": "txn_1:apply:0"})


class _ReadOnlyBeets(LocalFolderOps):
    """Performs the first ``ok_steps`` steps, then fails like Beets would."""

    def __init__(self, root, ok_steps):
        super().__init__(root)
        self.ok_steps = ok_steps

    def folder_op(self, op, key, **paths):
        if len(self.calls) >= self.ok_steps:
            self.calls.append((op, key, paths))
            raise BeetsAdapterError("boom", status_code=500, error_code="FOLDER_OP_FAILED")
        return super().folder_op(op, key, **paths)


class EngineTests(_Env):
    def plan_merge(self):
        src, dst = self.music / "Old", self.music / "New"
        src.mkdir()
        dst.mkdir()
        for name in ("a.txt", "b.txt"):
            (src / name).write_text(name)
        plan = te.create_folder_cleanup_plan(self.store, {"action": "merge", "source": str(src), "target": str(dst)})
        self.assertTrue(plan["ok"], plan)
        return plan["operation_id"], src, dst

    def test_only_an_approved_transaction_applies(self):
        local = patch_local_folder_ops(self, self.music)
        op, src, _dst = self.plan_merge()
        res = te.execute_folder_cleanup_apply(self.store, op)
        self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "not_approved", False))
        self.assertEqual((self.store.get(op)["status"], local.calls), ("Preview", []))
        self.assertTrue((src / "a.txt").exists())

    def test_failure_part_way_is_failed_with_record_and_rolls_back(self):
        beets = _ReadOnlyBeets(self.music, ok_steps=1)
        op, src, dst = self.plan_merge()
        self.store.transition(op, "Preview", "Approved")
        res = te.execute_folder_cleanup_apply(self.store, op, adapter=beets)
        self.assertFalse(res["ok"])
        self.assertEqual((res["code"], res["mutated"]), ("folder_cleanup_move_failed", True))
        self.assertIn("FOLDER_OP_FAILED", res["error"])
        tx = self.store.get(op)
        self.assertEqual(tx["status"], "Failed")
        self.assertEqual(len(tx["metadata"]["engine_result"]["moved_records"]), 1)
        self.assertTrue(any("Apply failed" in line for line in tx["logs"]))
        self.assertEqual(sum(line.startswith("Beets: moved ") for line in tx["logs"]), 1)

        rb = te.rollback_folder_cleanup(self.store, op, adapter=LocalFolderOps(self.music))
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")
        self.assertIn("Rollback Rolled Back: 1 restored, 0 failed.", self.store.get(op)["logs"])
        self.assertEqual(sorted(p.name for p in src.iterdir()), ["a.txt", "b.txt"])
        self.assertEqual(list(dst.iterdir()), [])
        again = te.rollback_folder_cleanup(self.store, op, adapter=LocalFolderOps(self.music))
        self.assertEqual(again["code"], "folder_cleanup_already_rolled_back")

    def setUp(self):
        super().setUp()
        p = mock.patch.object(te, "_FOLDER_STEP_RETRY_DELAY", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_unconfirmed_step_is_never_reported_success(self):
        (self.music / "Empty").mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(self.music / "Empty")})
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        silent = mock.Mock()
        silent.folder_op.return_value = {"operation_id": "x", "status": "running"}
        res = te.execute_folder_cleanup_apply(self.store, plan["operation_id"], adapter=silent)
        self.assertEqual((res["ok"], res["status"]), (False, "Failed"))
        self.assertIn("FOLDER_OP_UNCONFIRMED", res["error"])

    def test_lost_response_is_confirmed_by_replaying_the_key(self):
        """Beets did the step but the reply was lost: the replay with the same
        idempotency key reports it, so it is recorded and can be rolled back."""
        (self.music / "Empty").mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "remove_empty", "source": str(self.music / "Empty")})
        op = plan["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        local, done = LocalFolderOps(self.music), {}

        def flaky(op_name, key, **paths):
            if key in done:
                return {"operation_id": key, "status": "succeeded", "result": done[key]}
            done[key] = local.folder_op(op_name, key, **paths)
            raise BeetsAdapterTimeoutError("read timed out")

        beets = mock.Mock()
        beets.folder_op.side_effect = flaky
        res = te.execute_folder_cleanup_apply(self.store, op, adapter=beets)
        self.assertTrue(res["ok"], res)
        self.assertEqual(beets.folder_op.call_count, 2)
        self.assertEqual({c.args[1] for c in beets.folder_op.call_args_list}, {f"{op}:apply:0"})
        self.assertEqual(self.store.get(op)["metadata"]["engine_result"]["removed_dirs"], [str(self.music / "Empty")])
        self.assertTrue(te.rollback_folder_cleanup(self.store, op, adapter=local)["ok"])
        self.assertTrue((self.music / "Empty").is_dir())

    def test_never_confirmed_step_is_recorded_so_rollback_covers_it(self):
        op, src, dst = self.plan_merge()
        self.store.transition(op, "Preview", "Approved")
        local = LocalFolderOps(self.music)

        def done_but_silent(op_name, key, **paths):
            if not any(c[1] == key for c in local.calls):
                local.folder_op(op_name, key, **paths)  # Beets does it; the reply never arrives
            raise BeetsAdapterTimeoutError("read timed out")

        beets = mock.Mock()
        beets.folder_op.side_effect = done_but_silent
        res = te.execute_folder_cleanup_apply(self.store, op, adapter=beets)
        self.assertEqual((res["ok"], res["status"], res["mutated"]), (False, "Failed", True))
        self.assertIn("FOLDER_OP_UNCONFIRMED", res["error"])
        self.assertEqual(beets.folder_op.call_count, te._FOLDER_STEP_ATTEMPTS)
        records = self.store.get(op)["metadata"]["engine_result"]["moved_records"]
        self.assertEqual([r.get("unconfirmed") for r in records], [True])
        rb = te.rollback_folder_cleanup(self.store, op, adapter=LocalFolderOps(self.music))
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(sorted(p.name for p in src.iterdir()), ["a.txt", "b.txt"])
        self.assertEqual(list(dst.iterdir()), [])

    def test_rollback_refuses_unapplied_transactions(self):
        local = patch_local_folder_ops(self, self.music)
        op, _src, _dst = self.plan_merge()
        for status in ("Preview", "Approved"):
            self.store.update(op, status=status)
            res = te.rollback_folder_cleanup(self.store, op)
            self.assertEqual((res["ok"], res["code"]), (False, "rollback_not_eligible"), status)
            self.assertEqual(self.store.get(op)["status"], status)
        self.assertEqual(local.calls, [])

    def test_web_manager_writes_nothing_itself(self):
        """Every mutation is a plugin call: with a Beets that does nothing,
        nothing on disk changes."""
        src, dst = self.music / "Albm", self.music / "Album"
        src.mkdir()
        plan = te.create_folder_cleanup_plan(self.store, {"action": "safe_rename", "source": str(src), "target": str(dst)})
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        noop = mock.Mock()
        noop.folder_op.return_value = {"success": True}
        res = te.execute_folder_cleanup_apply(self.store, plan["operation_id"], adapter=noop)
        self.assertTrue(res["ok"], res)
        noop.folder_op.assert_called_once_with("rename_dir", f"{plan['operation_id']}:apply:0",
                                               source=str(src), target=str(dst))
        self.assertTrue(src.is_dir() and not dst.exists())


class RollbackRouteTests(_RouteEnv):
    """D3: a store-found folder_cleanup_v1 transaction rolls back through the route."""

    def setUp(self):
        super().setUp()
        patch_local_folder_ops(self, self.music)
        p = mock.patch.object(cw, "beets_adapter", FakeAdapter())
        p.start()
        self.addCleanup(p.stop)

    def test_route_rolls_back_an_applied_folder_cleanup(self):
        src = self.music / "Artist" / "Albm"
        src.mkdir(parents=True)
        dst = self.music / "Artist" / "Album"
        plan = cw.plan_folder_cleanup({"action": "safe_rename", "source": str(src), "target": str(dst)}, store=self.store)
        self.assertTrue(cw.apply_folder_cleanup(plan["operation_id"], store=self.store)["ok"])
        self.assertTrue(dst.is_dir() and not src.exists())
        tx = self.client.get(f"/api/transactions/{plan['operation_id']}").get_json()["transaction"]
        self.assertTrue(tx["rollback"]["allowed"], tx["rollback"])
        resp = self.client.post(f"/api/transactions/{plan['operation_id']}/rollback")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(src.is_dir() and not dst.exists())
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Rolled Back")
        again = self.client.post(f"/api/transactions/{plan['operation_id']}/rollback")
        self.assertEqual((again.status_code, again.get_json()["code"]), (409, "already_rolled_back"))

    def test_route_refuses_an_unapplied_folder_cleanup_without_claiming_it(self):
        (self.music / "Empty").mkdir()
        plan = cw.plan_folder_cleanup({"action": "remove_empty", "source": str(self.music / "Empty")}, store=self.store)
        self.store.update(plan["operation_id"], status="Completed")  # no engine_result: nothing applied
        resp = self.client.post(f"/api/transactions/{plan['operation_id']}/rollback")
        self.assertEqual((resp.status_code, resp.get_json()["code"]), (409, "not_applied"))
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Completed")


class FolderCleanupRouteErrorTests(_RouteEnv):
    """QA N3: a failed plugin step is named in the API response, using only
    allowlisted text."""

    def _apply_remove_empty(self, exc):
        from backend import cleanup_service
        (self.music / "Empty").mkdir()
        with mock.patch.object(cleanup_service, "MUSIC_ROOT", self.music),              mock.patch.object(cw, "beets_adapter", FakeAdapter()),              mock.patch("backend.beets_adapter.beets_adapter.folder_op", side_effect=exc):
            return self.client.post("/api/clean/folder-placeholder/apply", json={
                "action": "remove_empty", "source_path": str(self.music / "Empty"),
                "confirmed": True, "preview_token": "t"})

    def test_old_plugin_is_named_in_the_response(self):
        resp = self._apply_remove_empty(BeetsAdapterNotFoundError("Not Found"))
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(body["step_error_code"], "BEETS_NOT_FOUND")
        self.assertIn("the webmanager plugin needs 1.7.0; restart Beets after the plugin update", body["error"])
        self.assertTrue((self.music / "Empty").is_dir())

    def test_unknown_upstream_code_and_text_are_not_echoed(self):
        resp = self._apply_remove_empty(
            BeetsAdapterError("<script>upstream text</script>", status_code=500, error_code="WEIRD<b>"))
        body = resp.get_json()
        self.assertEqual(body["step_error_code"], "FOLDER_OP_FAILED")
        self.assertNotIn("upstream", resp.get_data(as_text=True))
        self.assertNotIn("WEIRD", resp.get_data(as_text=True))


class ArtistFolderAlbumRowsTests(unittest.TestCase):
    def test_rows_carry_decoded_path_album_id_and_artist_mbid(self):
        from backend import matching_service
        items = [{"id": 1, "path": b"/music/Artist/Album/01.flac", "album_id": 7, "mb_albumartistid": "mbid-a"},
                 {"id": 2, "path": "/music/Single.mp3", "album_id": None, "mb_albumartistid": ""}]
        with mock.patch.object(matching_service.beets_adapter, "get_items", return_value=items):
            rows = matching_service._artist_folder_album_rows()
        self.assertEqual(rows, [
            {"path": "/music/Artist/Album/01.flac", "album_id": 7, "mb_albumartistid": "mbid-a"},
            {"path": "/music/Single.mp3", "album_id": None, "mb_albumartistid": ""},
        ])


class CleanAllEmptyFolderStepTests(_Env):
    """D2: an apply failure fails the step instead of logging a skip."""

    def test_apply_failure_raises(self):
        from backend import cleanup_service
        (self.music / "Empty").mkdir()
        log = []
        with mock.patch.object(cw, "beets_adapter", FakeAdapter()), \
             mock.patch.object(cw, "_default_store", self.store), \
             mock.patch("backend.beets_adapter.beets_adapter.folder_op",
                        side_effect=BeetsAdapterError("ro", status_code=500, error_code="FOLDER_OP_FAILED")):
            with self.assertRaises(RuntimeError):
                cleanup_service._album_cleanup_remove_empty_tree(self.music / "Empty", log)
        self.assertTrue((self.music / "Empty").is_dir())
        self.assertTrue(any("ERROR empty-folder cleanup failed" in line for line in log), log)
        self.assertFalse(any("SKIP" in line for line in log), log)

    def test_success_removes_through_beets(self):
        from backend import cleanup_service
        (self.music / "Empty").mkdir()
        patch_local_folder_ops(self, self.music)
        log = []
        with mock.patch.object(cw, "beets_adapter", FakeAdapter()), \
             mock.patch.object(cw, "_default_store", self.store):
            self.assertEqual(cleanup_service._album_cleanup_remove_empty_tree(self.music / "Empty", log), 1)
        self.assertFalse((self.music / "Empty").exists())


if __name__ == "__main__":
    unittest.main()
