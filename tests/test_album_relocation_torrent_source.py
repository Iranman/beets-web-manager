"""Move to library of a preserved torrent source (#332, plugin 1.15.0).

Real BeetsAdapter -> real webmanager plugin -> real Beets library and files.
A track in a seeding folder (TORRENT_SOURCE_ROOTS, not app-created) is hard
linked into the library, or copied when a hard link is impossible; the
original keeps its path, bytes and inode throughout, including the rollback,
which points the row back and removes only the library file it made.
"""

import os
from pathlib import Path
from unittest import mock

from beets import util

import backend.composite_workflows as cw
import backend.library_service as ls
from tests.test_album_relocation_transaction import _Engine, sha, tree


def ident(path):
    st = os.stat(path)
    return sha(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime


def exdev(*_args, **_kwargs):
    raise util.FilesystemError("Cannot hard link across devices.", "link", ("a", "b"))


class TorrentSourceRelocationTests(_Engine):
    def setUp(self):
        super().setUp()
        for name, value in {"TORRENT_SOURCE_ROOTS": [Path(self.downloads)],
                            "DOWNLOADS_ALLOWED_ROOTS": [Path(self.downloads)],
                            "MUSIC_ROOT": Path(self.music), "TORRENT_SOURCE_MOVE_ALLOWED": False}.items():
            patcher = mock.patch.object(ls, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.folder = os.path.join(self.downloads, "torrentA")
        self.album = self.make_album(self.folder)
        self.old = self.beets_state(self.album.id)
        self.originals = {p: ident(p) for p in [*self.old[0].values(), self.old[1]]}

    def assert_originals_untouched(self):
        self.assertEqual({p: ident(p) for p in self.originals}, self.originals)  # bytes, inode, size, mtime

    def relocate(self):
        res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assert_originals_untouched()
        return res

    def rolled_back(self, op):
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["status"]), (True, "Rolled Back"), rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assert_originals_untouched()
        self.assertEqual(tree(self.music), {})  # only the files this move made were removed
        self.assertFalse(os.path.exists(os.path.join(self.music, "A")))
        return self.store.get(op)

    def test_hard_link_keeps_the_torrent_original_and_rolls_back(self):
        res = self.relocate()
        self.assertTrue(res["ok"], res)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(set(tx["metadata"]["before"]["operations"].values()), {"link"})
        self.assertEqual(tx["metadata"]["before"]["art_operation"], "link")
        engine = tx["metadata"]["engine_result"]
        self.assertEqual(set(engine["methods"].values()), {"linked"})
        self.assertEqual(engine["art_method"], "linked")
        now, art = self.beets_state(self.album.id)
        for iid, path in now.items():
            self.assertTrue(path.startswith(self.music + os.sep), path)
            original = self.old[0][iid]
            self.assertTrue(os.path.samefile(path, original))  # one inode, two names
            self.assertEqual(os.stat(original).st_nlink, 2)
        self.assertTrue(os.path.samefile(art, self.old[1]))
        self.assertIn("hard linked", " ".join(map(str, tx["logs"])))
        tx = self.rolled_back(res["operation_id"])
        self.assertTrue(all(os.stat(p).st_nlink == 1 for p in self.originals))
        result = tx["metadata"]["rollback_result"]
        self.assertEqual((result["repointed_items"], result["removed_library_files"]), (2, 3))

    def test_impossible_hard_link_falls_back_to_a_copy(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            res = self.relocate()
        self.assertTrue(res["ok"], res)
        engine = self.store.get(res["operation_id"])["metadata"]["engine_result"]
        self.assertEqual(set(engine["methods"].values()), {"copied"})
        self.assertEqual(engine["art_method"], "copied")
        now, _art = self.beets_state(self.album.id)
        for iid, path in now.items():
            self.assertFalse(os.path.samefile(path, self.old[0][iid]))
            self.assertEqual(sha(path), sha(self.old[0][iid]))
        self.rolled_back(res["operation_id"])

    def test_a_mixed_album_links_only_its_preserved_tracks(self):
        first = min(self.old[0])
        with mock.patch.object(cw, "_preserved_torrent_file", side_effect=lambda p: p == self.old[0][first]):
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        engine = self.store.get(res["operation_id"])["metadata"]["engine_result"]
        self.assertEqual(engine["methods"], {**{str(k): "moved" for k in self.old[0]}, str(first): "linked"})
        self.assertEqual(ident(self.old[0][first]), self.originals[self.old[0][first]])
        self.assertFalse(any(os.path.exists(p) for k, p in self.old[0].items() if k != first))
        rb = cw.rollback_album_relocation(res["operation_id"], adapter=self.ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assert_originals_untouched()
        self.assertEqual(tree(self.music), {})

    def test_changed_library_copy_is_refused_and_nothing_changes(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            op = self.relocate()["operation_id"]
        path = next(iter(self.beets_state(self.album.id)[0].values()))
        with open(path, "ab") as f:
            f.write(b"edited")
        before = self.snapshot(self.album.id)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["code"], rb["mutated"]), (False, "library_file_changed", False), rb)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assert_originals_untouched()

    def test_replaced_library_link_is_refused(self):
        op = self.relocate()["operation_id"]
        path = next(iter(self.beets_state(self.album.id)[0].values()))
        os.unlink(path)
        with open(path, "wb") as f:
            f.write(b"someone else's file")
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual(rb["code"], "library_file_changed", rb)
        self.assert_originals_untouched()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"someone else's file")

    def test_changed_original_is_refused_and_nothing_is_removed(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            op = self.relocate()["operation_id"]
        original = next(iter(self.old[0].values()))
        with open(original, "ab") as f:
            f.write(b"re-checked by the torrent client")
        before = self.snapshot(self.album.id)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["code"], rb["mutated"]), ("source_changed", False), rb)
        self.assertEqual(self.snapshot(self.album.id), before)

    def test_library_file_already_gone_points_the_row_back(self):
        op = self.relocate()["operation_id"]
        os.unlink(next(iter(self.beets_state(self.album.id)[0].values())))
        self.rolled_back(op)

    def test_interrupted_rollback_resumes_and_removes_only_its_library_files(self):
        op = self.relocate()["operation_id"]
        made = sorted(self.beets_state(self.album.id)[0].values())
        for item in self.lib.get_album(self.album.id).items():  # a crash after the rows were pointed back
            item.path = os.fsencode(self.old[0][item.id])
            item.store()
        self.assertTrue(all(os.path.exists(p) for p in made))
        self.rolled_back(op)

    def test_failure_part_way_unlinks_what_it_linked_and_changes_nothing(self):
        real = util.hardlink
        calls = []

        def second_fails(src, dest, *a, **k):
            calls.append(dest)
            if len(calls) == 2:
                raise RuntimeError("disk gone")
            return real(src, dest, *a, **k)

        before = self.snapshot(self.album.id)
        with mock.patch.object(util, "hardlink", side_effect=second_fails):
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assertEqual(res["code"], "undone", res)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assert_originals_untouched()

    def test_older_plugin_is_never_asked_to_move_a_torrent(self):
        with mock.patch.object(self.ad, "get_plugin_status", return_value={"capabilities": ["album_relocation"]}), \
                mock.patch.object(self.ad, "relocate_album") as relocate:
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        relocate.assert_not_called()
        self.assertEqual((res["ok"], res["code"], res["status"]), (False, "plugin_outdated", "Failed"))
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assert_originals_untouched()

    def test_app_created_download_and_library_albums_still_move(self):
        self.assertFalse(cw._preserved_torrent_file(os.path.join(self.downloads, "_beets_missing_import", "x",
                                                                 "t.flac")))
        self.assertFalse(cw._preserved_torrent_file(os.path.join(self.music, "x", "t.flac")))
        self.assertTrue(cw._preserved_torrent_file(os.path.join(self.downloads, "torrentA", "CD1", "t.flac")))
        with mock.patch.object(ls, "TORRENT_SOURCE_MOVE_ALLOWED", True):
            self.assertFalse(cw._preserved_torrent_file(os.path.join(self.downloads, "torrentA", "t.flac")))

    # --- F1: the rollback trusts only the plugin's own record -------------

    def forged_rollback(self, op, method):
        """The reported attack: ask the plugin to 'clean up' the torrent original."""
        def ev(path):
            st = os.stat(path)
            return [st.st_size, st.st_mtime, st.st_dev, st.st_ino]

        now, art = self.beets_state(self.album.id)
        items = [{"id": int(k), "path": self.old[0][k], "restore_path": p, "method": method,
                  "evidence": ev(p), "library_evidence": ev(self.old[0][k])} for k, p in now.items()]
        body = {"album_id": self.album.id, "items": items, "artpath": art, "restore_artpath": art,
                "art_method": method, "art_evidence": None, **({"apply_operation_id": op} if op else {})}
        return self.client.post("/webmanager/album-relocation/rollback", json=body,
                                headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": f"forged-{op}-{method}"})

    def test_forged_rollback_never_removes_the_torrent_original(self):
        op = self.relocate()["operation_id"]
        made = self.beets_state(self.album.id)
        for apply_id in (op, ""):
            for method in ("linked", "copied"):
                self.forged_rollback(apply_id, method)
                self.assert_originals_untouched()
                self.assertTrue(all(os.path.exists(p) for p in made[0].values()))
        self.assertEqual(self.beets_state(self.album.id), made)

    def test_unlink_never_leaves_the_library_directory(self):
        from beetsplug.webmanager import relocation_ops as ro
        self.relocate()
        torrent = next(iter(self.old[0].values()))
        st = os.stat(torrent)
        ev = [st.st_size, st.st_mtime, st.st_dev, st.st_ino]
        self.assertFalse(ro._unlink_ours(self.lib, torrent, os.path.join(self.music, "x.flac"), "copied", ev, ev))
        self.assert_originals_untouched()

    def test_expired_record_keeps_linked_rows_and_both_files(self):
        import beetsplug.webmanager.operations as ops_mod
        op = self.relocate()["operation_id"]
        made = self.beets_state(self.album.id)
        with ops_mod._operations_lock:
            ops_mod._operations.pop(op)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["code"], rb["mutated"]), (False, "relocation_record_missing", False), rb)
        self.assertEqual(self.beets_state(self.album.id), made)  # rows still on the library files
        self.assert_originals_untouched()
        self.assertTrue(all(os.path.exists(p) for p in made[0].values()))

    # --- round 3 (security N1-N4): payload evidence never decides ----------

    def post_forged(self, items, op, key, art=None, art_method=None):
        now, art_now = self.beets_state(self.album.id)
        body = {"album_id": self.album.id, "items": items, "artpath": art_now,
                "restore_artpath": art_now if art is None else art,
                **({"art_method": art_method} if art_method else {}),
                **({"apply_operation_id": op} if op is not None else {})}
        return self.client.post("/webmanager/album-relocation/rollback", json=body,
                                headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": key})

    def _forged_live_paths(self, op):
        """F1's real shape: path = the live library path, restore = the torrent
        original, evidence = stat(original), with no, foreign, failed or unknown
        operation ids. Nothing is unlinked or repointed."""
        import beetsplug.webmanager.operations as ops_mod

        def ev(path):
            st = os.stat(path)
            return [st.st_size, st.st_mtime, st.st_dev, st.st_ino]

        made = self.beets_state(self.album.id)
        other = self.make_album(os.path.join(self.downloads, "torrentB"))
        foreign = cw.relocate_album(other.id, mode="move", adapter=self.ad, store=self.store)["operation_id"]
        with ops_mod._operations_lock:
            ops_mod._operations["failed-op"] = {**ops_mod._operations[op], "status": "failed"}
        for n, apply_id in enumerate((None, "", foreign, "failed-op", "nonexistent")):
            for method in ("linked", "copied", None):
                items = [{"id": int(k), "path": p, "restore_path": self.old[0][k], "evidence": ev(self.old[0][k]),
                          **({"method": method} if method else {})} for k, p in made[0].items()]
                r = self.post_forged(items, apply_id, f"p1-{n}-{method}", art=self.old[1], art_method=method)
                self.assertEqual(r.status_code, 409, r.get_json())
                self.assert_originals_untouched()
                self.assertEqual(self.beets_state(self.album.id), made)
                self.assertTrue(all(os.path.exists(p) for p in [*made[0].values(), made[1]]))
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)  # the real record still works
        self.assertEqual((rb["ok"], rb["status"]), (True, "Rolled Back"), rb)
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assertFalse(any(os.path.exists(p) for p in [*made[0].values(), made[1]]))
        self.assert_originals_untouched()

    def test_forged_live_path_rollback_linked(self):
        self._forged_live_paths(self.relocate()["operation_id"])

    def test_forged_live_path_rollback_copied(self):
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            op = self.relocate()["operation_id"]
        self._forged_live_paths(op)

    def test_no_record_never_repoints_to_an_arbitrary_file(self):
        self.relocate()
        made = self.beets_state(self.album.id)
        victims = os.path.join(self.downloads, "someone-elses")
        os.makedirs(victims)
        items = []
        for k, p in made[0].items():
            v = os.path.join(victims, f"unrelated{k}.flac")
            with open(v, "wb") as f:
                f.write(b"not this album")
            st = os.stat(v)
            items.append({"id": int(k), "path": p, "restore_path": v, "method": "linked",
                          "evidence": [st.st_size, st.st_mtime, st.st_dev, st.st_ino]})
        r = self.post_forged(items, None, "p2")
        self.assertEqual((r.status_code, r.get_json()["error_code"]), (409, "RELOCATION_RECORD_MISSING"))
        self.assertEqual(self.beets_state(self.album.id), made)

    def test_no_record_never_adopts_a_file_by_payload_evidence(self):
        # A moved (non-preserved) track whose library file is gone: without the
        # record, payload size/mtime never adopt an unrelated file.
        import beetsplug.webmanager.operations as ops_mod
        with mock.patch.object(ls, "TORRENT_SOURCE_MOVE_ALLOWED", True):
            op = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)["operation_id"]
        made = self.beets_state(self.album.id)
        k, lib_path = next(iter(made[0].items()))
        os.unlink(lib_path)
        victim = os.path.join(self.downloads, "someone-elses.flac")
        with open(victim, "wb") as f:
            f.write(b"unrelated")
        st = os.stat(victim)
        items = [{"id": int(i), "path": p, "restore_path": victim if i == k else self.old[0][i],
                  "evidence": [st.st_size, st.st_mtime, st.st_dev, st.st_ino]} for i, p in made[0].items()]
        with ops_mod._operations_lock:
            ops_mod._operations.pop(op)
        r = self.post_forged(items, None, "p2-moved")
        self.assertEqual((r.status_code, r.get_json()["error_code"]), (409, "FILE_MISSING"))
        self.assertEqual(self.beets_state(self.album.id), made)

    def test_write_by_another_item_at_a_recorded_path_never_poisons_the_record(self):
        import beetsplug.webmanager.operations as ops_mod
        op = self.relocate()["operation_id"]
        made = self.beets_state(self.album.id)
        self.rolled_back(op)
        k, lib_path = next(iter(made[0].items()))
        os.makedirs(os.path.dirname(lib_path), exist_ok=True)
        self._write_flac(lib_path)
        with open(lib_path, "ab") as f:
            f.write(b"the user's other track")
        other = self._Item.from_path(lib_path)
        other.title = "unrelated"
        self.lib.add(other)
        recorded = ops_mod._operations[op]["result"]["evidence"]["library"][str(k)]
        self.assertTrue(other.try_write())
        self.assertEqual(ops_mod._operations[op]["result"]["evidence"]["library"][str(k)], recorded)
        other.remove(delete=False)  # untracked; the user keeps the file
        items = [{"id": int(i), "path": p, "restore_path": p} for i, p in self.old[0].items()]
        r = self.post_forged(items, op, "p3-replay")  # the real apply id, replayed under a new key
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["removed_library_files"], 0)
        self.assertTrue(os.path.exists(lib_path))
        self.assert_originals_untouched()

    def test_write_to_a_replaced_library_file_is_not_recorded(self):
        op, item, original = self.linked_item()
        lib_path = os.fsdecode(item.path)
        replacement = lib_path + ".new"
        with open(original, "rb") as src, open(replacement, "wb") as out:
            out.write(src.read())
        os.replace(replacement, lib_path)  # another file, another inode, at the recorded path
        item.title = "Edited"
        self.assertTrue(item.try_write())
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["code"], rb["mutated"]), ("library_file_changed", False), rb)
        self.assertTrue(os.path.exists(lib_path))
        self.assert_originals_untouched()

    def test_unlink_uses_the_identity_it_proved(self):
        from beetsplug.webmanager import relocation_ops as ro
        op = self.relocate()["operation_id"]
        swapped = []
        real = ro._unlink_at

        def swap_then_unlink(root, path, ident):
            if not swapped:
                with open(path + ".other", "wb") as f:
                    f.write(b"different file")
                os.replace(path + ".other", path)
                swapped.append(path)
            return real(root, path, ident)
        with mock.patch.object(ro, "_unlink_at", side_effect=swap_then_unlink):
            cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertTrue(swapped and os.path.exists(swapped[0]))
        with open(swapped[0], "rb") as f:
            self.assertEqual(f.read(), b"different file")
        self.assert_originals_untouched()

    def test_copy_on_write_never_follows_a_planted_temp_symlink(self):
        import shutil
        import stat as stat_mod
        item = self.linked_item()[1]
        p = os.fsdecode(item.path)
        secret = os.path.join(self.td, "config-secret.yaml")
        with open(secret, "w") as f:
            f.write("apikey: x")
        os.chmod(secret, 0o600)
        try:
            os.symlink(secret, os.path.join(self.td, "probe-link"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here (Linux CI runs this)")
        secret_mode = stat_mod.S_IMODE(os.stat(secret).st_mode)
        real = shutil.copyfileobj

        def copy_then_plant(src, out, *a, **k):
            real(src, out, *a, **k)
            d = os.path.dirname(p)
            tmp = os.path.join(d, [n for n in os.listdir(d) if n.startswith(".webmanager-cow-")][0])
            os.unlink(tmp)
            os.symlink(secret, tmp)
        with mock.patch.object(shutil, "copyfileobj", side_effect=copy_then_plant):
            item.title = "x"
            self.assertFalse(item.try_write())
        self.assertEqual(stat_mod.S_IMODE(os.stat(secret).st_mode), secret_mode)
        self.assertFalse(os.path.islink(p))
        self.assertTrue(os.path.samefile(p, self.old[0][item.id]))  # still the untouched link
        self.assert_originals_untouched()

    # --- F4: a failed copy leaves no orphan -------------------------------

    def test_failed_copy_leaves_no_partial_file(self):
        def partial_copy(src, dest, *a, **k):
            with open(dest, "wb") as f:
                f.write(b"half")
            raise util.FilesystemError("No space left on device", "copy", (src, dest))

        before = self.snapshot(self.album.id)
        with mock.patch.object(util, "hardlink", side_effect=exdev), \
                mock.patch.object(util, "copy", side_effect=partial_copy):
            res = cw.relocate_album(self.album.id, mode="move", adapter=self.ad, store=self.store)
        self.assertEqual(res["code"], "undone", res)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assertEqual(tree(self.music), {})
        self.assert_originals_untouched()

    # --- B: copy-on-write before any tag write -----------------------------

    def linked_item(self):
        op = self.relocate()["operation_id"]
        item = next(iter(self.lib.get_album(self.album.id).items()))
        original = self.old[0][item.id]
        self.assertTrue(os.path.samefile(os.fsdecode(item.path), original))
        return op, item, original

    def assert_link_broken(self, item, original):
        self.assert_originals_untouched()  # torrent bytes, inode, size, mtime
        self.assertEqual(os.stat(original).st_nlink, 1)
        self.assertFalse(os.path.samefile(os.fsdecode(item.path), original))

    def test_tag_write_breaks_the_link_and_leaves_the_torrent_untouched(self):
        from mediafile import MediaFile
        op, item, original = self.linked_item()
        item.title = "Edited"
        self.assertTrue(item.try_write())
        self.assert_link_broken(item, original)
        self.assertEqual(MediaFile(os.fsdecode(item.path)).title, "Edited")
        self.assertNotEqual(MediaFile(original).title, "Edited")
        self.rolled_back(op)  # the recorded copy-on-write evidence proves the library file is ours

    def test_embedded_art_write_breaks_the_link(self):
        from mediafile import Image, MediaFile
        _op, item, original = self.linked_item()
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        self.assertTrue(item.try_write(tags={"images": [Image(data=jpeg)]}))  # what embedart's embed_item calls
        self.assert_link_broken(item, original)
        self.assertEqual(len(MediaFile(os.fsdecode(item.path)).images), 1)
        self.assertFalse(MediaFile(original).images)

    def test_cover_replace_never_writes_into_the_torrent_cover(self):
        op = self.relocate()["operation_id"]
        album = self.lib.get_album(self.album.id)
        new = os.path.join(self.td, "new.jpg")
        with open(new, "wb") as f:
            f.write(b"\xff\xd8\xff-new-cover")
        album.set_art(os.fsencode(new))  # the call the plugin's album-art endpoint makes
        album.store()
        self.assert_originals_untouched()
        self.assertEqual(os.stat(self.old[1]).st_nlink, 1)
        self.assertTrue(op)

    def test_copy_on_write_unrecorded_refuses_the_rollback_safely(self):
        import beetsplug.webmanager.operations as ops_mod
        op, item, original = self.linked_item()
        with mock.patch.object(ops_mod, "_durable_file", None):  # a `beet write` in another process
            item.title = "Edited"
            self.assertTrue(item.try_write())
        self.assert_link_broken(item, original)
        before = self.snapshot(self.album.id)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["code"], rb["mutated"]), ("library_file_changed", False), rb)
        self.assertEqual(self.snapshot(self.album.id), before)
        self.assert_originals_untouched()

    def test_copy_on_write_failure_skips_the_write(self):
        op, item, original = self.linked_item()
        with mock.patch("tempfile.mkstemp", side_effect=OSError("read-only")):
            item.title = "Edited"
            self.assertFalse(item.try_write())  # logged by Beets; nothing written through the link
        self.assert_originals_untouched()
        self.assertTrue(os.path.samefile(os.fsdecode(item.path), original))
        self.assertTrue(op)

    def test_unevaluable_rule_keeps_the_original(self):
        with mock.patch.object(ls, "_preserve_torrent_source_file", side_effect=OSError("boom")):
            self.assertTrue(cw._preserved_torrent_file(os.path.join(self.music, "x", "t.flac")))


if __name__ == "__main__":
    import unittest
    unittest.main()
