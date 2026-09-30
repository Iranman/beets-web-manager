"""Engine op: album-row merge + rollback (ARCH-020), on a real Beets library.

Requests run on a fresh thread with an empty music-dir context, as on the
Beets web server. A merge changes only item ownership (album_id): files are
never rewritten or moved.
"""

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
import uuid
import wave
from unittest import mock

from beets import config as beets_config
from beets import context as beets_context
from beets.library import Item, Library
from beetsplug.web import app as beets_web_app

import beetsplug.webmanager.merge_ops as merge_mod
import beetsplug.webmanager.operations as ops_mod
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REL = "45347542-db98-422a-a307-ae95d5371f60"
REL_OTHER = "11111111-2222-3333-4444-555555555555"


def rec(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def write_wav(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes([0, value % 250]) * 4000)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class AlbumRowMergeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=self.music)
        key_file = os.path.join(self.td, "key")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        self.plugin = WebManagerPlugin()
        set_api_key_file(key_file)
        ops_mod.set_allowed_roots([self.music])
        merge_mod.set_merge_root(os.path.join(self.td, "merges"))
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        # Target row: tracks 1-3. Source row: tracks 4-5. One album, one edition.
        self.target = self._album("Target", [1, 2, 3])
        self.source = self._album("Source", [4, 5])

    def tearDown(self):
        ops_mod.set_allowed_roots(None)
        merge_mod.set_merge_root(None)
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        try:
            beets_config["web"]["readonly"] = True
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _album(self, name, tracks, rel=REL, rg=RG):
        items = []
        for t in tracks:
            path = os.path.join(self.music, name, f"{t:02d}.wav")
            write_wav(path, t)
            item = Item.from_path(path)
            item.update({"title": f"t{t}", "track": t, "disc": 1, "mb_trackid": rec(t), "album": "Album",
                         "albumartist": "Artist", "mb_albumid": rel, "mb_releasegroupid": rg})
            items.append(item)
        album = self.lib.add_album(items)
        album.update({"mb_albumid": rel, "mb_releasegroupid": rg, "comments": f"row {name}"})
        album.store()
        return album

    def _items_payload(self, album):
        return [{"item_id": it.id, "source_album_id": album.id, "sha256": sha(os.fsdecode(it.path)),
                 "mb_trackid": it.mb_trackid, "disc": it.disc, "track": it.track} for it in album.items()]

    def _post(self, path, body, key=None):
        out = {}

        def run():
            beets_context.set_music_dir(b"")
            out["res"] = self.client.post(path, json=body, headers={**self.auth,
                                                                    "Idempotency-Key": key or f"tx-{uuid.uuid4()}"})
        t = threading.Thread(target=run)
        t.start()
        t.join()
        return out["res"]

    def _merge(self, key=None, **overrides):
        body = {"target_album_id": self.target.id, "source_album_ids": [self.source.id],
                "expected_release_group_id": RG, "expected_release_id": REL,
                "items": self._items_payload(self.source)}
        body.update(overrides)
        return self._post("/webmanager/album-row-merge", body, key=key)

    def _state(self):
        items = sorted((i.id, i.album_id, os.fsdecode(i.path), i.mb_trackid, i.mb_albumid, i.mb_releasegroupid,
                        i.disc, i.track) for i in self.lib.items())
        albums = sorted((a.id, a.album, a.mb_albumid, a.mb_releasegroupid, a.get("comments")) for a in self.lib.albums())
        files = {os.fsdecode(i.path): (sha(os.fsdecode(i.path)), os.stat(os.fsdecode(i.path)).st_mtime_ns)
                 for i in self.lib.items()}
        return items, albums, files

    def test_complementary_merge_moves_ownership_only_and_retires_the_source(self):
        before_items, _before_albums, before_files = self._state()
        source_id, moved = self.source.id, [i.id for i in self.source.items()]
        res = self._merge()
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["retired_album_ids"], [source_id])
        self.assertIsNone(self.lib.get_album(source_id))
        for iid in moved:
            self.assertEqual(self.lib.get_item(iid).album_id, self.target.id)
        after_items, _albums, after_files = self._state()
        self.assertEqual(after_files, before_files)  # no bytes or mtimes changed, no path changed
        strip = lambda rows: [r[:1] + r[2:] for r in rows]  # everything but album_id unchanged
        self.assertEqual(strip(after_items), strip(before_items))

    # -- partial move ("partial": true) --------------------------------------

    def _partial(self, tracks, **overrides):
        items = [e for e in self._items_payload(self.source) if e["track"] in tracks]
        return self._merge(items=items, partial=True, **overrides)

    def test_subset_without_partial_is_still_refused(self):
        res = self._merge(items=self._items_payload(self.source)[:1])
        self.assertEqual(res.get_json()["error_code"], "SOURCE_NOT_FULLY_COVERED")

    def test_partial_move_keeps_the_source_row_with_the_remaining_item(self):
        before = self._state()
        source_id = self.source.id
        res = self._partial({4})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["retired_album_ids"], [])
        self.assertIsNotNone(self.lib.get_album(source_id))
        self.assertEqual(sorted(i.track for i in self.lib.get_album(source_id).items()), [5])
        self.assertEqual(sorted(i.track for i in self.lib.get_album(self.target.id).items()), [1, 2, 3, 4])
        self.assertEqual(self._state()[2], before[2])  # no file changed
        rb = self._post("/webmanager/album-row-merge/rollback", {"merge_id": res.get_json()["merge_id"]})
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self._state(), before)

    def test_partial_move_of_every_item_retires_the_row_and_rollback_restores_it(self):
        before = self._state()
        source_id = self.source.id
        res = self._partial({4, 5})
        self.assertEqual(res.get_json()["retired_album_ids"], [source_id])
        self.assertIsNone(self.lib.get_album(source_id))
        rb = self._post("/webmanager/album-row-merge/rollback", {"merge_id": res.get_json()["merge_id"]})
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self._state(), before)

    def test_partial_move_refuses_an_item_from_another_row_and_keeps_every_gate(self):
        stranger = self._album("Stranger", [9])
        items = self._items_payload(self.source)[:1] + self._items_payload(stranger)
        res = self._merge(items=items, partial=True)
        self.assertEqual(res.get_json()["error_code"], "ITEM_NOT_IN_SOURCE")
        other = self._album("OtherEdition", [7], rel=REL_OTHER)
        res = self._post("/webmanager/album-row-merge", {
            "target_album_id": self.target.id, "source_album_ids": [other.id], "partial": True,
            "expected_release_group_id": RG, "expected_release_id": REL, "items": self._items_payload(other)})
        self.assertEqual(res.get_json()["error_code"], "EDITION_DIFFERS")
        overlap = self._album("Overlap", [2, 8])
        res = self._post("/webmanager/album-row-merge", {
            "target_album_id": self.target.id, "source_album_ids": [overlap.id], "partial": True,
            "expected_release_group_id": RG, "expected_release_id": REL,
            "items": [e for e in self._items_payload(overlap) if e["track"] == 2]})
        self.assertEqual(res.get_json()["error_code"], "SLOT_OVERLAP")

    def test_failure_after_a_row_was_retired_restores_that_row(self):
        from beets.library import Album
        before = self._state()
        real_remove = Album.remove

        def remove_then_fail(album, *a, **k):
            real_remove(album, *a, **k)
            raise OSError("db locked")

        with mock.patch.object(Album, "remove", remove_then_fail):
            res = self._merge()
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self._state(), before)

    def test_overlapping_slot_is_refused(self):
        overlap = self._album("Overlap", [2])
        res = self._post("/webmanager/album-row-merge", {
            "target_album_id": self.target.id, "source_album_ids": [overlap.id],
            "expected_release_group_id": RG, "expected_release_id": REL, "items": self._items_payload(overlap)})
        self.assertEqual(res.get_json()["error_code"], "SLOT_OVERLAP")

    def test_release_group_mismatch_is_refused(self):
        self.assertEqual(self._merge(expected_release_group_id=rec(9)).get_json()["error_code"],
                         "RELEASE_GROUP_MISMATCH")

    def test_recording_id_conflict_since_preview_is_refused(self):
        payload = self._items_payload(self.source)
        payload[0]["mb_trackid"] = rec(99)
        self.assertEqual(self._merge(items=payload).get_json()["error_code"], "ITEM_IDENTITY_DRIFT")

    def test_edition_difference_is_refused_for_review(self):
        other = self._album("OtherEdition", [6], rel=REL_OTHER)
        res = self._post("/webmanager/album-row-merge", {
            "target_album_id": self.target.id, "source_album_ids": [other.id],
            "expected_release_group_id": RG, "expected_release_id": REL, "items": self._items_payload(other)})
        self.assertEqual(res.get_json()["error_code"], "EDITION_DIFFERS")

    def test_file_drift_after_preview_is_refused(self):
        payload = self._items_payload(self.source)
        write_wav(os.fsdecode(self.lib.get_item(payload[0]["item_id"]).path), 200)
        self.assertEqual(self._merge(items=payload).get_json()["error_code"], "ITEM_CONTENT_DRIFT")

    def test_partial_coverage_is_refused(self):
        payload = self._items_payload(self.source)[:1]
        self.assertEqual(self._merge(items=payload).get_json()["error_code"], "SOURCE_NOT_FULLY_COVERED")

    def test_failure_after_first_moved_item_restores_everything(self):
        before = self._state()
        real_store = Item.store
        calls = {"n": 0}

        def flaky(item, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_store(item, *a, **kw)

        with mock.patch.object(Item, "store", flaky):
            res = self._merge()
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self._state(), before)
        self.assertIsNotNone(self.lib.get_album(self.source.id))

    def test_rollback_restores_the_exact_original_rows(self):
        before = self._state()
        data = self._merge().get_json()
        self.assertIsNone(self.lib.get_album(self.source.id))
        res = self._post("/webmanager/album-row-merge/rollback", {"merge_id": data["merge_id"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self._state(), before)  # same album ids, metadata, ownership, files

    def test_repeated_apply_and_rollback_replay_instead_of_rerunning(self):
        key = f"tx-{uuid.uuid4()}"
        first = self._merge(key=key).get_json()
        ops_mod._operations.clear()  # a Beets restart loses the in-memory registry...
        again = self._merge(key=key)  # ...the manifest still replays the recorded result
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.get_json()["replayed"])
        self.assertEqual(again.get_json()["moved_item_ids"], first["moved_item_ids"])
        other = self._merge()  # a NEW merge of the same rows finds nothing left to move
        self.assertEqual(other.get_json()["error_code"], "ALBUM_NOT_FOUND")

        before_rb = self._post("/webmanager/album-row-merge/rollback", {"merge_id": first["merge_id"]})
        self.assertEqual(before_rb.status_code, 200)
        state = self._state()
        repeat = self._post("/webmanager/album-row-merge/rollback", {"merge_id": first["merge_id"]})
        self.assertTrue(repeat.get_json()["replayed"])
        self.assertEqual(self._state(), state)

    def test_status_endpoint_reports_the_manifest(self):
        data = self._merge().get_json()
        res = self.client.get(f"/webmanager/album-row-merge/{data['merge_id']}", headers=self.auth)
        self.assertEqual(res.get_json()["status"], "applied")
        self.assertEqual(self.client.get("/webmanager/album-row-merge/zzz", headers=self.auth).status_code, 404)

    def test_rollback_refuses_when_an_item_changed_after_the_merge(self):
        data = self._merge().get_json()
        moved = self.lib.get_item(data["moved_item_ids"][0])
        moved.track = 9
        moved.store()
        res = self._post("/webmanager/album-row-merge/rollback", {"merge_id": data["merge_id"]})
        self.assertEqual(res.get_json()["error_code"], "ITEM_IDENTITY_DRIFT")

    def test_idempotency_key_is_required(self):
        res = self.client.post("/webmanager/album-row-merge", json={}, headers=self.auth)
        self.assertEqual(res.get_json()["error_code"], "IDEMPOTENCY_KEY_REQUIRED")

    def test_capability(self):
        self.assertIn("album_row_merge", ops_mod.get_capabilities())


if __name__ == "__main__":
    unittest.main()
