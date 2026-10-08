"""Album rename / move to library through Beets (album_move_v1).

Real BeetsAdapter -> real webmanager plugin (plugin 1.14.0) -> real Beets
library and files (Flask test client in place of HTTP). The plan records every
path; the operator's click is the approval; the apply is Beets' Album.move();
the rollback moves every file and the cover back through Beets, and Beets
refuses (changing nothing) when the album changed since.
"""

import hashlib
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import backend.composite_workflows as cw
import backend.transaction_recovery as recovery
from backend.beets_adapter import (
    BeetsAdapter,
    BeetsAdapterBadRequestError,
    BeetsAdapterError,
    BeetsAdapterNotFoundError,
)
from backend.transaction_engine import TransactionStore


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree(root):
    """{relative path: sha256} of every file under root."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            out[os.path.relpath(p, root)] = sha(p)
    return out


class _Engine(unittest.TestCase):
    def setUp(self):
        from beets.library import Item, Library
        from beetsplug.web import app as beets_web_app
        import beetsplug.webmanager.operations as ops_mod
        from beetsplug.webmanager import WebManagerPlugin
        from beetsplug.webmanager.auth import set_api_key_file
        from tests.test_webmanager_album_art import TOKEN, write_flac

        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.downloads = os.path.join(self.td, "downloads")
        os.makedirs(self.music)
        os.makedirs(self.downloads)
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=self.music)
        key = os.path.join(self.td, "key")
        with open(key, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        WebManagerPlugin()
        set_api_key_file(key)
        ops_mod.set_allowed_roots([self.music, self.downloads])
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.token = TOKEN
        self.store = TransactionStore(os.path.join(self.td, "tx"))
        from backend import resource_locks
        resource_locks.set_locks(resource_locks.ResourceLocks(os.path.join(self.td, "locks")))
        self._write_flac, self._Item = write_flac, Item

        errors = {400: BeetsAdapterBadRequestError, 404: BeetsAdapterNotFoundError}

        def bridge(method, path, params=None, json_data=None, headers=None, timeout=None):
            r = self.client.open(path, method=method, json=json_data, query_string=params,
                                 headers={**(headers or {}), "Authorization": f"Bearer {TOKEN}"})
            body = r.get_json(silent=True) or {}
            if r.status_code >= 400:
                raise errors.get(r.status_code, BeetsAdapterError)(
                    "x", status_code=r.status_code, error_code=body.get("error_code") or "BEETS_NOT_FOUND")
            return body

        self.ad = BeetsAdapter(base_url="http://beets.test:8337", api_key=TOKEN)
        self.ad._request = bridge

        def cleanup():
            ops_mod.set_allowed_roots(None)
            set_api_key_file(None)
            resource_locks.set_locks(None)
            self.lib._connection().close()
            shutil.rmtree(self.td, ignore_errors=True)
        self.addCleanup(cleanup)

    def make_album(self, folder, n=2, art=True):
        os.makedirs(folder, exist_ok=True)
        items = []
        for i in range(1, n + 1):
            path = os.path.join(folder, f"track{i}.flac")
            self._write_flac(path)
            with open(path, "ab") as f:
                f.write(f"payload-{i}".encode())  # distinct bytes per file
            item = self._Item.from_path(path)
            item.update({"title": f"T{i}", "track": i, "album": "B", "albumartist": "A", "artist": "A"})
            items.append(item)
        album = self.lib.add_album(items)
        if art:
            cover = os.path.join(folder, "cover.jpg")
            with open(cover, "wb") as f:
                f.write(b"\xff\xd8\xff-cover")
            album.artpath = os.fsencode(cover)
            album.store()
        return album

    def beets_state(self, album_id):
        album = self.lib.get_album(album_id)
        if album is None:
            return None
        return ({it.id: os.fsdecode(it.path) for it in album.items()}, os.fsdecode(album.artpath or b""))

    def snapshot(self, album_id):
        return self.beets_state(album_id), tree(self.music), tree(self.downloads)


class RenameAndMoveRoundTripTests(_Engine):
    def _round_trip(self, folder, mode):
        album = self.make_album(folder)
        before = self.snapshot(album.id)
        for p in before[0][0].values():
            os.chmod(p, 0o640)

        def owner_mode(state):  # util.move is os.replace on one filesystem: owner and mode kept
            return sorted((os.stat(p).st_uid, os.stat(p).st_mode) for p in [*state[0].values(), state[1]])

        kept = owner_mode(before[0])
        res = cw.relocate_album(album.id, mode=mode, adapter=self.ad, store=self.store)
        self.assertEqual(owner_mode(self.beets_state(album.id)), kept)
        self.assertTrue(res["ok"], res)
        op = res["operation_id"]
        tx = self.store.get(op)
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(tx["metadata"]["approved_by"], f"operator album {mode}")
        self.assertEqual(tx["operation_type"], "Move" if mode == "move" else "Rename")
        self.assertEqual(len(self.store.list(limit=100)[0]), 1)  # exactly one transaction row
        planned = tx["metadata"]["before"]
        self.assertEqual(planned["items"], {str(k): v for k, v in before[0][0].items()})
        self.assertEqual(planned["artpath"], before[0][1])
        self.assertEqual(planned["folders"], [folder])

        moved_paths, moved_art = self.beets_state(album.id)
        dest = os.path.join(self.music, "A", "B")
        self.assertTrue(all(os.path.dirname(p) == dest for p in moved_paths.values()), moved_paths)
        self.assertEqual(moved_art, os.path.join(dest, "cover.jpg"))
        self.assertEqual(res["dest_dir"], dest)
        # Same bytes, new place.
        old_audio = [v for t in before[1:] for k, v in t.items() if k.endswith(".flac")]
        self.assertEqual(sorted(sha(p) for p in moved_paths.values()), sorted(old_audio))

        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")
        self.assertEqual(self.snapshot(album.id), before)  # paths, artpath and every file's bytes
        self.assertEqual(owner_mode(before[0]), kept)
        self.assertFalse(os.path.exists(os.path.join(self.music, "A")))  # vacated folders pruned
        self.assertEqual(len(self.store.list(limit=100)[0]), 1)

    def test_rename_inside_the_library_rolls_back_exactly(self):
        self._round_trip(os.path.join(self.music, "incoming", "x"), "rename")

    def test_move_to_library_from_downloads_rolls_back_exactly(self):
        self._round_trip(os.path.join(self.downloads, "Some Album"), "move")

    def test_generic_rollback_route_dispatch_table_has_the_family(self):
        import routes_maintenance
        self.assertEqual(routes_maintenance._ENGINE_FAMILIES[cw.ALBUM_RELOCATION_FAMILY],
                         (cw.apply_album_relocation, cw.rollback_album_relocation))
        album = self.make_album(os.path.join(self.music, "x"))
        res = cw.relocate_album(album.id, adapter=self.ad, store=self.store)
        verdict = routes_maintenance.rollback_eligibility(self.store.get(res["operation_id"]))
        self.assertTrue(verdict["allowed"], verdict)


class RollbackRefusalTests(_Engine):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.music, "incoming", "x")
        self.album = self.make_album(self.folder)
        self.old = self.beets_state(self.album.id)
        res = cw.relocate_album(self.album.id, adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.op = res["operation_id"]

    def refused(self, code):
        before = self.snapshot(self.album.id)
        tx_before = self.store.get(self.op)["status"]
        rb = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertFalse(rb["ok"], rb)
        self.assertEqual(rb["code"], code)
        self.assertIs(rb["mutated"], False)
        self.assertEqual(self.snapshot(self.album.id), before)  # nothing changed
        self.assertEqual(self.store.get(self.op)["status"], tx_before)
        return rb

    def test_album_moved_again_is_refused(self):
        album = self.lib.get_album(self.album.id)
        album.albumartist = "C"
        album.store(inherit=True)
        album.move()
        self.refused("item_moved")

    def test_occupied_old_path_is_refused_and_never_overwritten(self):
        blocker = self.old[0][min(self.old[0])]
        os.makedirs(os.path.dirname(blocker), exist_ok=True)
        with open(blocker, "wb") as f:
            f.write(b"someone else's file")
        self.refused("target_exists")
        with open(blocker, "rb") as f:
            self.assertEqual(f.read(), b"someone else's file")

    def test_occupied_old_cover_path_is_refused(self):
        os.makedirs(self.folder, exist_ok=True)
        with open(self.old[1], "wb") as f:
            f.write(b"other cover")
        self.refused("target_exists")

    def test_album_gone_is_refused(self):
        self.lib.get_album(self.album.id).remove(delete=False)
        self.refused("album_not_found")

    def test_track_removed_from_album_is_refused(self):
        next(iter(self.lib.get_album(self.album.id).items())).remove(delete=False)
        self.refused("album_changed")

    def test_cover_changed_is_refused(self):
        album = self.lib.get_album(self.album.id)
        album.artpath = b""
        album.store()
        self.refused("art_changed")

    def test_refusal_is_not_remembered_so_a_retry_after_the_fix_runs(self):
        blocker = self.old[0][min(self.old[0])]
        os.makedirs(os.path.dirname(blocker), exist_ok=True)
        with open(blocker, "wb") as f:
            f.write(b"x")
        self.refused("target_exists")
        os.remove(blocker)
        rb = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)

    def test_route_answers_409_with_the_code(self):
        import routes_maintenance
        album = self.lib.get_album(self.album.id)
        album.albumartist = "C"
        album.store(inherit=True)
        album.move()
        with mock.patch.object(cw, "beets_adapter", self.ad), mock.patch.object(cw, "_default_store", self.store):
            from flask import Flask
            with Flask(__name__).app_context():
                resp, status = routes_maintenance._item_file_replacement_response(
                    cw.rollback_album_relocation, self.op, rollback_family=cw.ALBUM_RELOCATION_FAMILY)
        self.assertEqual(status, 409)
        self.assertEqual(resp.get_json()["code"], "item_moved")

    def test_failure_part_way_puts_everything_back(self):
        import beetsplug.webmanager.relocation_ops as rel
        before = self.snapshot(self.album.id)
        real_move = rel.util.move

        def fail_on_cover(src, dst, *a, **k):
            if os.fsdecode(dst).endswith("cover.jpg"):
                raise OSError("disk full")
            return real_move(src, dst, *a, **k)

        with mock.patch.object(rel.util, "move", side_effect=fail_on_cover):
            rb = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertEqual(rb["code"], "undone")
        self.assertIs(rb["mutated"], False)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assertEqual(self.store.get(self.op)["status"], "Completed")
        self.assertTrue(cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)["ok"])
        self.assertEqual(self.beets_state(self.album.id), self.old)

    def test_moved_track_file_missing_is_refused(self):
        os.remove(self.beets_state(self.album.id)[0][min(self.old[0])])
        self.refused("file_missing")

    def test_moved_cover_file_missing_is_refused(self):
        os.remove(self.beets_state(self.album.id)[1])
        self.refused("file_missing")

    def test_unique_path_rename_is_never_accepted_and_nothing_is_overwritten(self):
        import beetsplug.webmanager.relocation_ops as rel
        target = self.old[0][min(self.old[0])]
        real_mkdirall = rel.util.mkdirall

        def race(path):  # someone takes the old path between the check and the move
            real_mkdirall(path)
            if os.fsdecode(path) == target:
                with open(target, "wb") as f:
                    f.write(b"raced in")

        state = self.beets_state(self.album.id)
        with mock.patch.object(rel.util, "mkdirall", side_effect=race):
            rb = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["code"], rb["mutated"]), ("undone", False))
        self.assertEqual(self.beets_state(self.album.id), state)  # Beets' name.1.flac was moved back
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"raced in")
        self.assertFalse(os.path.exists(target[:-5] + ".1.flac"))

    def test_compensation_that_cannot_be_proven_is_rollback_failed_not_undone(self):
        import beetsplug.webmanager.relocation_ops as rel
        real_move = rel.util.move
        first_back = self.old[0][min(self.old[0])]

        def fail_on_cover(src, dst, *a, **k):
            if os.fsdecode(dst).endswith("cover.jpg"):
                os.remove(first_back)  # a moved-back track vanishes before compensation
                raise OSError("disk full")
            return real_move(src, dst, *a, **k)

        with mock.patch.object(rel.util, "move", side_effect=fail_on_cover):
            rb = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["code"], rb["mutated"]), ("rollback_failed", None))
        self.assertEqual(self.store.get(self.op)["status"], "Recovery Required")

    def test_interrupted_rollback_resumes_from_the_recorded_paths(self):
        import beetsplug.webmanager.relocation_ops as rel
        first = min(self.old[0])
        item = self.lib.get_item(first)
        rel._move_item(self.lib, item, self.old[0][first])  # Beets restarted after this step
        rb = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["restored"]), (True, 1), rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)

    def test_rollback_request_is_recorded(self):
        cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        req = self.store.get(self.op)["metadata"]["rollback_request"]
        self.assertEqual(req["idempotency_key"], f"{self.op}:rollback")
        self.assertEqual({str(e["id"]): e["restore_path"] for e in req["items"]},
                         {str(k): v for k, v in self.old[0].items()})

    def _raw_rollback(self, items, artpath=None, restore_artpath=None):
        now_items, now_art = self.beets_state(self.album.id)
        return self.client.post(
            "/webmanager/album-relocation/rollback", headers={"Authorization": f"Bearer {self.token}"},
            json={"album_id": self.album.id, "artpath": now_art if artpath is None else artpath,
                  "restore_artpath": now_art if restore_artpath is None else restore_artpath,
                  "items": [{"id": k, "path": now_items[k], "restore_path": items[k]} for k in now_items]})

    def test_two_tracks_restored_to_one_path_are_refused(self):
        one = self.old[0][min(self.old[0])]
        r = self._raw_rollback({k: one for k in self.old[0]})
        self.assertEqual(r.get_json()["error_code"], "PATH_INVALID")

    def test_cover_restored_onto_a_track_path_is_refused(self):
        before = self.snapshot(self.album.id)
        r = self._raw_rollback(self.old[0], restore_artpath=self.old[0][min(self.old[0])])
        self.assertEqual((r.status_code, r.get_json()["error_code"]), (400, "PATH_INVALID"))
        self.assertEqual(self.snapshot(self.album.id), before)

    def test_restore_onto_a_tracked_path_whose_file_is_missing_is_refused(self):
        other = self.make_album(os.path.join(self.music, "other"), n=1, art=False)
        tracked = os.fsdecode(next(iter(other.items())).path)
        os.remove(tracked)  # Beets still references it
        r = self._raw_rollback({k: tracked if k == min(self.old[0]) else v for k, v in self.old[0].items()})
        self.assertEqual((r.status_code, r.get_json()["error_code"]), (409, "TARGET_EXISTS"))

    def test_second_rollback_is_a_no_op(self):
        self.assertTrue(cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)["ok"])
        before = self.snapshot(self.album.id)
        again = cw.rollback_album_relocation(self.op, adapter=self.ad, store=self.store)
        self.assertEqual((again["ok"], again["status"]), (True, "Rolled Back"))
        self.assertEqual(self.snapshot(self.album.id), before)


class ApplyRefusalAndRecoveryTests(_Engine):
    def setUp(self):
        super().setUp()
        self.album = self.make_album(os.path.join(self.music, "incoming", "x"))

    def _approved(self):
        plan = cw.plan_album_relocation(album_id=self.album.id, adapter=self.ad, store=self.store)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        return plan["operation_id"]

    def test_stale_plan_is_refused_and_moves_nothing(self):
        op = self._approved()
        album = self.lib.get_album(self.album.id)
        album.albumartist = "C"
        album.store(inherit=True)
        album.move()
        before = self.snapshot(self.album.id)
        res = cw.apply_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "stale_plan", False))
        self.assertEqual(self.store.get(op)["status"], "Failed")
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assertFalse(cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)["ok"])

    def test_preview_is_not_applied(self):
        plan = cw.plan_album_relocation(album_id=self.album.id, adapter=self.ad, store=self.store)
        res = cw.apply_album_relocation(plan["operation_id"], adapter=self.ad, store=self.store)
        self.assertEqual(res["code"], "not_approved")
        self.assertEqual(self.beets_state(self.album.id)[0], {it.id: os.fsdecode(it.path)
                                                              for it in self.lib.get_album(self.album.id).items()})

    def test_restart_before_the_engine_call_resolves_failed_from_the_recorded_paths(self):
        op = self._approved()
        self.store.transition(op, "Approved", "Running", metadata={"engine_request": {"operation_id": op}})
        before = self.snapshot(self.album.id)
        out = recovery.sweep(adapter=self.ad, store=self.store, before=time.time() + 1)
        self.assertEqual(out[0]["action"], "Failed", out)
        self.assertEqual(self.store.get(op)["status"], "Failed")
        self.assertEqual(self.snapshot(self.album.id), before)  # never replayed

    def test_restart_after_the_engine_moved_finishes_from_engine_evidence(self):
        op = self._approved()
        planned = self.store.get(op)["metadata"]["before"]["items"]
        self.store.transition(op, "Approved", "Running", metadata={"engine_request": {"operation_id": op}})
        self.ad.relocate_album(self.album.id, planned, idempotency_key=op)  # Web Manager died here
        moved = self.snapshot(self.album.id)
        out = recovery.sweep(adapter=self.ad, store=self.store, before=time.time() + 1)
        self.assertEqual(out[0], {"operation_id": op, "action": "finished", "status": "Completed"})
        self.assertEqual(self.snapshot(self.album.id), moved)  # not moved twice
        self.assertTrue(cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)["ok"])
        self.assertEqual(self.beets_state(self.album.id)[0], {int(k): v for k, v in planned.items()})

    def test_restart_with_engine_record_lost_and_album_moved_needs_recovery(self):
        import beetsplug.webmanager.operations as ops_mod
        op = self._approved()
        planned = self.store.get(op)["metadata"]["before"]["items"]
        self.store.transition(op, "Approved", "Running", metadata={"engine_request": {"operation_id": op}})
        self.ad.relocate_album(self.album.id, planned, idempotency_key=op)
        with ops_mod._operations_lock:
            ops_mod._operations.pop(op, None)  # Beets restarted and lost the record
        with mock.patch.object(ops_mod, "_durable_file", None):
            out = recovery.sweep(adapter=self.ad, store=self.store, before=time.time() + 1)
        self.assertEqual(out[0]["action"], "Recovery Required", out)

    def test_missing_track_file_is_a_recorded_partial_move_and_rollback_undoes_only_the_moved(self):
        old = self.beets_state(self.album.id)
        missing = max(old[0])
        os.remove(old[0][missing])
        res = cw.relocate_album(self.album.id, adapter=self.ad, store=self.store)
        self.assertEqual((res["ok"], res["code"], res["status"]), (False, "partial_move", "Failed"), res)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["metadata"]["engine_result"]["skipped"], [str(missing)])
        self.assertEqual(tx["metadata"]["engine_result"]["moved"], [str(min(old[0]))])
        now = self.beets_state(self.album.id)[0]
        self.assertEqual(now[missing], old[0][missing])  # Beets skipped it
        self.assertNotEqual(now[min(old[0])], old[0][min(old[0])])
        rb = cw.rollback_album_relocation(res["operation_id"], adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["restored"]), (True, 1), rb)
        self.assertEqual(self.beets_state(self.album.id), old)

    def test_missing_cover_file_artpath_cleared_by_beets_is_restored_by_rollback(self):
        old = self.beets_state(self.album.id)
        os.remove(old[1])
        res = cw.relocate_album(self.album.id, adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.beets_state(self.album.id)[1], "")  # Album.move_art dropped it
        self.assertIn("cleared the cover path", str(self.store.get(res["operation_id"])))
        self.assertTrue(cw.rollback_album_relocation(res["operation_id"], adapter=self.ad, store=self.store)["ok"])
        self.assertEqual(self.beets_state(self.album.id), old)

    def test_old_plugin_without_the_endpoint_changes_nothing(self):
        op = self._approved()
        with mock.patch.object(self.ad, "relocate_album",
                               side_effect=BeetsAdapterNotFoundError("x", error_code="BEETS_NOT_FOUND")):
            res = cw.apply_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertIn("1.14.0", res["error"])
        self.assertEqual(self.store.get(op)["status"], "Failed")


class PluginPathSafetyTests(_Engine):
    def setUp(self):
        super().setUp()
        self.album = self.make_album(os.path.join(self.music, "incoming", "x"), n=1, art=False)
        res = cw.relocate_album(self.album.id, adapter=self.ad, store=self.store)
        self.now = self.beets_state(self.album.id)[0]

    def _rollback(self, restore):
        iid, path = next(iter(self.now.items()))
        return self.client.post("/webmanager/album-relocation/rollback", headers={"Authorization": f"Bearer {self.token}"},
                                json={"album_id": self.album.id, "artpath": "", "restore_artpath": "",
                                      "items": [{"id": iid, "path": path, "restore_path": restore}]})

    def test_restore_outside_the_allowed_roots_is_refused(self):
        r = self._rollback(os.path.join(self.td, "elsewhere", "t.flac"))
        self.assertEqual((r.status_code, r.get_json()["error_code"]), (400, "PATH_OUTSIDE_ROOTS"))

    def test_restore_with_another_extension_is_refused(self):
        r = self._rollback(os.path.join(self.music, "x", "t.mp3"))
        self.assertEqual(r.get_json()["error_code"], "EXTENSION_CHANGED")

    def test_restore_onto_an_allowed_root_itself_or_a_sibling_prefix_is_refused(self):
        self.assertEqual(self._rollback(self.music).get_json()["error_code"], "PATH_OUTSIDE_ROOTS")
        r = self._rollback(os.path.join(self.music + "2", "t.flac"))
        self.assertEqual(r.get_json()["error_code"], "PATH_OUTSIDE_ROOTS")

    def test_relative_or_dotted_restore_path_is_refused(self):
        r = self._rollback(os.path.join(self.music, "x", "..", "..", "t.flac"))
        self.assertEqual(r.get_json()["error_code"], "PATH_INVALID")

    def test_restore_through_a_symlink_is_refused(self):
        outside = os.path.join(self.td, "outside")
        os.makedirs(outside)
        link = os.path.join(self.music, "link")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        r = self._rollback(os.path.join(link, "t.flac"))
        self.assertEqual(r.get_json()["error_code"], "SYMLINK_REJECTED")
        self.assertEqual(os.listdir(outside), [])

    def test_capability_is_advertised(self):
        r = self.client.get("/webmanager/status", headers={"Authorization": f"Bearer {self.token}"})
        self.assertIn("album_relocation", r.get_json()["capabilities"])


class OneTransactionPerRouteTests(unittest.TestCase):
    def test_rename_and_move_jobs_opt_out_of_the_job_hook_row(self):
        from backend.transaction_service import _transaction_create_for_job
        with mock.patch("backend.transaction_service.transactions") as store:
            for label, kind in (("Rename: A - B", "album-rename"), ("Move to library: A - B", "album-move-to-library")):
                self.assertIsNone(_transaction_create_for_job(label, {"type": kind, "album_id": 1,
                                                                      "transaction": False}))
        store.create.assert_not_called()
        import inspect
        import routes_library
        for fn in (routes_library.album_rename, routes_library.album_move_to_library):
            self.assertIn('"transaction": False', inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()
