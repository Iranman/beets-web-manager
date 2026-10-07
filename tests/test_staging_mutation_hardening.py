"""Staging delete/move and transaction-state hardening (#182 items 1-5,
#187 F-4 and F-6). Everything runs against temp directories, a temp
TransactionStore and a fake adapter."""

import os
import tempfile
import unittest
from unittest import mock

import backend.composite_workflows as cw
from tests.test_s1_containment_followup import CAN_SYMLINK
from tests.test_wave0_s1_containment import FakeAdapter, _Env


class IdentityTests(_Env):
    """#182 item 1: the entry deleted/moved is the one validated."""

    def _swap_dir(self, path):
        os.rename(path, str(path) + ".orig")
        path.mkdir()
        (path / "new.flac").write_bytes(b"n")

    def test_remove_refuses_entry_replaced_after_validation(self):
        folder = self.dl / "junk"
        folder.mkdir()
        validated = cw._validated_staging_target(folder, "delete")
        self._swap_dir(folder)
        with self.assertRaises(ValueError):
            cw._remove_resolved(validated)
        self.assertTrue((folder / "new.flac").exists())

    def test_move_refuses_source_replaced_after_validation(self):
        src = self.dl / "src"
        src.mkdir()
        p_src = cw._validated_staging_target(src, "move")
        p_dst = cw._validated_staging_target(self.dl / "dst", "move to")
        self._swap_dir(src)
        with self.assertRaises(ValueError):
            cw._move_resolved(p_src, p_dst)
        self.assertTrue((src / "new.flac").exists())
        self.assertFalse((self.dl / "dst").exists())

    def test_unvalidated_path_is_refused(self):
        folder = self.dl / "junk"
        folder.mkdir()
        with self.assertRaises(ValueError):
            cw._remove_resolved(folder.resolve())
        self.assertTrue(folder.exists())

    def test_validated_delete_and_move_still_work(self):
        (self.dl / "a").mkdir()
        (self.dl / "b.flac").write_bytes(b"b")
        cw.move_staging_file(str(self.dl / "a"), str(self.dl / "x" / "a"))
        self.assertTrue((self.dl / "x" / "a").is_dir())
        cw.delete_staging_file(str(self.dl / "b.flac"))
        self.assertFalse((self.dl / "b.flac").exists())


@unittest.skipUnless(CAN_SYMLINK, "symlinks unavailable")
class MoveMkdirOrderTests(_Env):
    """#182 item 2: no target parent is created through a swapped symlink."""

    def test_symlink_recheck_runs_before_mkdir(self):
        src = self.dl / "f.flac"
        src.write_bytes(b"a")
        (self.dl / "a").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        p_src = cw._validated_staging_target(src, "move")
        p_dst = cw._validated_staging_target(self.dl / "a" / "sub" / "f.flac", "move to")
        os.rmdir(self.dl / "a")
        os.symlink(outside, self.dl / "a", target_is_directory=True)
        with self.assertRaises(ValueError):
            cw._move_resolved(p_src, p_dst)
        self.assertFalse((outside / "sub").exists())
        self.assertTrue(src.exists())


class MusicRootAncestorTests(_Env):
    """#182 item 3: an ancestor of MUSIC_ROOT is never a staging target."""

    def test_music_root_ancestor_refused(self):
        media = self.root / "media"
        music = media / "music"
        music.mkdir(parents=True)
        (music / "01.flac").write_bytes(b"a")
        with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": str(self.root), "MUSIC_ROOT": str(music)}):
            self.assertFalse(cw._is_safe_staging_path(media))
            with self.assertRaises(ValueError):
                cw.delete_staging_file(str(media))
            with self.assertRaises(ValueError):
                cw.move_staging_file(str(media), str(self.root / "elsewhere"))
        self.assertTrue((music / "01.flac").exists())


class ImportReviewFamilyTests(_Env):
    """#182 item 4: the family is checked before the Preview -> Approved CAS."""

    def test_other_family_is_not_approved(self):
        tx = self.store.create(operation_type="Delete", status="Preview",
                               metadata={"mutation_family": cw.ALBUM_CLEANUP_FAMILY, "album_id": 1})
        res = cw.apply_import_review_cleanup(tx["id"], store=self.store, approved_by="operator")
        self.assertEqual(res["code"], "wrong_family")
        self.assertEqual(self.store.get(tx["id"])["status"], "Preview")


class _InternalTypeErrorAdapter(FakeAdapter):
    def remove(self, item_ids=None, album_ids=None, delete_files=False, idempotency_key=None):
        self.calls.append(("remove", sorted(item_ids or []), [], delete_files, idempotency_key))
        raise TypeError("bug inside remove()")


class _LegacyAdapter(FakeAdapter):
    def remove(self, item_ids=None, album_ids=None, delete_files=False):
        return super().remove(item_ids=item_ids, album_ids=album_ids, delete_files=delete_files)


class PlaylistCleanupTypeErrorTests(_Env):
    """#182 item 5: an internal TypeError is not retried without the key."""

    def _approved(self, ad):
        plan = cw.plan_playlist_media_cleanup({"item_ids": [1]}, adapter=ad, store=self.store)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        return plan["operation_id"]

    def test_internal_type_error_is_not_retried(self):
        ad = _InternalTypeErrorAdapter(items={1: {"id": 1, "album_id": None, "path": self.media("A/01.flac")}})
        op = self._approved(ad)
        with self.assertRaises(TypeError):
            cw.apply_playlist_media_cleanup(op, adapter=ad, store=self.store)
        self.assertEqual(len(ad.calls), 1)
        self.assertEqual(ad.calls[0][4], op)
        self.assertEqual(self.store.get(op)["status"], "Failed")

    def test_adapter_without_key_still_works(self):
        ad = _LegacyAdapter(items={1: {"id": 1, "album_id": None, "path": self.media("A/01.flac")}})
        res = cw.apply_playlist_media_cleanup(self._approved(ad), adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(ad.calls), 1)


class _RouteEnv(_Env):
    @classmethod
    def setUpClass(cls):
        cls._import_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": cls._import_tmp.name}):
            import app  # noqa: F401

    @classmethod
    def tearDownClass(cls):
        cls._import_tmp.cleanup()

    def setUp(self):
        super().setUp()
        import app as app_module
        import routes_maintenance
        for p in (mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}),
                  mock.patch.object(routes_maintenance, "transactions", self.store),
                  mock.patch.object(cw, "_default_store", self.store)):
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.app.test_client()


class AlbumCleanupRouteTests(_RouteEnv):
    """#187 F-4: an approved album_cleanup_v1 plan applies through the route."""

    def test_apply_approved_album_cleanup(self):
        path = self.media("A/B/01.flac")
        ad = FakeAdapter(items={7: {"id": 7, "album_id": 5, "path": path}}, albums={5: {"id": 5, "album": "B"}})
        plan = cw.plan_album_cleanup(5, adapter=ad, store=self.store)
        op = plan["operation_id"]
        self.assertEqual(self.client.post(f"/api/transactions/{op}/approve").status_code, 200)
        with mock.patch.object(cw, "beets_adapter", ad):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self.store.get(op)["status"], "Completed")
        self.assertEqual(ad.destructive_calls(), [("remove", [], [5], False)])
        self.assertTrue(os.path.exists(path))
        rb = self.client.post(f"/api/transactions/{op}/rollback")
        self.assertEqual(rb.get_json()["code"], "not_supported")


class _AlbumStaysAdapter(FakeAdapter):
    def remove(self, item_ids=None, album_ids=None, delete_files=False, idempotency_key=None):
        self.calls.append(("remove", sorted(item_ids or []), sorted(album_ids or []), delete_files))
        return {"success": True}  # the album row survives: verification_failed


class AlbumCleanupFailureMessageTests(_RouteEnv):
    """PR #200 QA F-1: a row-only failure never claims files were deleted."""

    def test_classifier_row_only(self):
        from backend.cleanup_service import _classify_album_cleanup_apply_failure as classify
        kind, msg = classify({"ok": False, "mutated": True, "delete_files": False,
                              "error": "Album row still present after removal."})
        self.assertEqual(kind, "partial_mutation")
        self.assertNotIn("deleted", msg)
        self.assertIn("no audio files", msg)

    def test_classifier_delete_files(self):
        from backend.cleanup_service import _classify_album_cleanup_apply_failure as classify
        for res in ({"mutated": True, "delete_files": True, "error": "x"}, {"mutated": True, "error": "x"}):
            kind, msg = classify(res)
            self.assertEqual(kind, "partial_mutation")
            self.assertIn("already deleted", msg)

    def test_row_only_route_failure_message(self):
        path = self.media("A/B/01.flac")
        ad = _AlbumStaysAdapter(items={7: {"id": 7, "album_id": 5, "path": path}}, albums={5: {"id": 5}})
        plan = cw.plan_album_cleanup(5, adapter=ad, store=self.store)
        with mock.patch.object(cw, "beets_adapter", ad):
            resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": plan["operation_id"]})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400, body)
        self.assertTrue(body["mutated"])
        self.assertEqual(body["error_kind"], "partial_mutation")
        self.assertNotIn("deleted", body["error"])
        self.assertTrue(os.path.exists(path))


class AlbumCleanupRowOnlyResultTests(_Env):
    """PR #200 QA F-3: the UI count relies on removed_item_ids / deleted."""

    def test_row_only_apply_result(self):
        path = self.media("A/B/01.flac")
        ad = FakeAdapter(items={7: {"id": 7, "album_id": 5, "path": path},
                                8: {"id": 8, "album_id": 5, "path": path}}, albums={5: {"id": 5}})
        plan = cw.plan_album_cleanup(5, adapter=ad, store=self.store)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        res = cw.apply_album_cleanup(plan["operation_id"], adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["delete_files"])
        self.assertEqual(res["removed_item_ids"], [7, 8])
        self.assertEqual(res["deleted"], [])


class CancelCasTests(_RouteEnv):
    """#187 F-6: cancel is a compare-and-set."""

    def test_preview_is_cancelled(self):
        tx = self.store.create(operation_type="Delete", status="Preview")
        resp = self.client.post(f"/api/transactions/{tx['id']}/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.store.get(tx["id"])["status"], "Cancelled")

    def test_terminal_state_is_not_overwritten(self):
        tx = self.store.create(operation_type="Delete", status="Completed")
        resp = self.client.post(f"/api/transactions/{tx['id']}/cancel")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.store.get(tx["id"])["status"], "Completed")

    def test_cancel_racing_apply_keeps_completed(self):
        tx = self.store.create(operation_type="Delete", status="Approved")
        real_read = self.store._read
        fired = []

        def racing_read(tid):
            if not fired:  # the apply finishes between cancel's read and write
                fired.append(1)
                cur = real_read(tid)
                cur["status"] = "Completed"
                self.store._write(cur)
            return real_read(tid)

        with mock.patch.object(self.store, "_read", side_effect=racing_read):
            resp = self.client.post(f"/api/transactions/{tx['id']}/cancel")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.store.get(tx["id"])["status"], "Completed")

    def test_unknown_id(self):
        self.assertEqual(self.client.post("/api/transactions/nope/cancel").status_code, 404)


if __name__ == "__main__":
    unittest.main()
