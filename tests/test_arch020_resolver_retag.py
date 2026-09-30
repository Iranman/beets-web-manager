"""ARCH-020 tests for duplicate resolver retagging and recording attach.

Covers:
1. correct recording attachment
2. incorrect proposed Recording ID
3. AcoustID conflict
4. AcoustID unavailable
5. ambiguous fingerprint
6. MB tracklist mismatch
7. disc/track conflict
8. already-correct item
9. recording repair followed by ownership move
10. rollback/recovery
11. repeated apply/idempotency
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.album_row_merge as arm
import backend.composite_workflows as cw
import backend.dedup_service as ds
from backend.provider_boundary import ProviderOutcome, ProviderResult
from backend.resource_locks import ResourceLocks, set_locks
from backend.transaction_engine import TransactionStore

RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REL = "45347542-db98-422a-a307-ae95d5371f60"
REC1 = "11111111-1111-1111-1111-111111111111"
REC2 = "22222222-2222-2222-2222-222222222222"
REC3 = "33333333-3333-3333-3333-333333333333"


class FakeAdapter:
    """In-memory Beets adapter simulating library items and album rows."""

    def __init__(self, root: Path):
        self.root = root
        self.albums = {
            10: {"id": 10, "album": "Test Album", "albumartist": "Test Artist", "mb_albumid": REL, "mb_releasegroupid": RG},
            11: {"id": 11, "album": "Test Album", "albumartist": "Test Artist", "mb_albumid": REL, "mb_releasegroupid": RG},
        }
        self.items = {}
        # Target album 10 has track 1 (REC1)
        self.add_item(1, 10, disc=1, track=1, title="Track 1", recording=REC1)
        # Source album 11 has duplicate item 2 with wrong tag / duplicate slot
        self.add_item(2, 11, disc=1, track=1, title="Track 2 (Mislabelled)", recording=REC1)

    def add_item(self, iid, aid, disc=1, track=1, title="Title", recording=REC1):
        path = self.root / f"{iid}.flac"
        if not path.exists():
            path.write_bytes(f"audio-{iid}".encode("utf-8"))
        self.items[iid] = {
            "id": iid,
            "album_id": aid,
            "disc": disc,
            "track": track,
            "title": title,
            "artist": "Test Artist",
            "album": "Test Album",
            "albumartist": "Test Artist",
            "format": "FLAC",
            "length": 180.0,
            "mb_trackid": recording,
            "mb_albumid": REL,
            "mb_releasegroupid": RG,
            "path": str(path),
        }

    def get_album(self, aid, expand=True):
        return self.albums.get(int(aid))

    def get_item(self, iid):
        return self.items.get(int(iid))

    def find_all_items_by_album_id(self, aid):
        return [i for i in self.items.values() if i["album_id"] == int(aid)]

    def get_stats(self):
        return {"albums": len(self.albums), "items": len(self.items)}

    def modify_item(self, iid, fields):
        item = self.items.get(int(iid))
        if not item:
            return {"success": False, "error": "Item not found"}
        item.update(fields)
        return {"success": True, "item": dict(item)}

    def album_row_merge(self, target, sources, items, rg, rel, idempotency_key, partial=False):
        self._undo = ({s: dict(self.albums[s]) for s in sources if s in self.albums}, {i["item_id"]: i["source_album_id"] for i in items})
        for i in items:
            self.items[i["item_id"]]["album_id"] = target
        retired = [s for s in sources if not self.find_all_items_by_album_id(s)]
        for s in retired:
            del self.albums[s]
        return {
            "success": True,
            "merge_id": arm.merge_id_for(idempotency_key),
            "retired_album_ids": retired,
            "moved_item_ids": [i["item_id"] for i in items],
        }

    def rollback_album_row_merge(self, merge_id, idempotency_key=None):
        rows, owners = self._undo
        self.albums.update(rows)
        for iid, aid in owners.items():
            self.items[iid]["album_id"] = aid
        return {"success": True}


class Arch020ResolverRetagTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.adapter = FakeAdapter(self.root)
        self.store = TransactionStore(str(self.root / "tx"))
        set_locks(ResourceLocks(self.root / "locks"))
        self.addCleanup(set_locks, None)

        # Standard MusicBrainz tracklist mock: track 1 = REC1, track 2 = REC2
        self.mb_tracklist_data = {
            "ok": True,
            "outcome": "confirmed",
            "release_id": REL,
            "release_group": RG,
            "release_artist": "Test Artist",
            "release_title": "Test Album",
            "tracks": [
                {"disc": 1, "track": 1, "title": "Track 1", "mb_trackid": REC1},
                {"disc": 1, "track": 2, "title": "Track 2", "mb_trackid": REC2},
            ],
        }
        self.acoustid_outcome = ProviderResult(
            provider="acoustid",
            outcome=ProviderOutcome.CONFIRMED,
            data=[{"mb_trackid": REC2, "score": 95.0, "title": "Track 2", "artist": "Test Artist"}],
        )

        self.deps = {
            "mb_tracklist": lambda rid: self.mb_tracklist_data,
            "acoustid_lookup": lambda p: self.acoustid_outcome,
            "abs_path": lambda p: p,
            "music_root": self.root,
            "eval_recording": ds._resolver_retag_deps()["eval_recording"],
            "acoustid_evidence": ds._resolver_retag_deps()["acoustid_evidence"],
        }

    # 1. Correct recording attachment
    def test_correct_recording_attachment_and_evaluation(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertTrue(eval_res["ok"], eval_res)
        self.assertEqual(eval_res["proposed_recording_id"], REC2)
        self.assertEqual(eval_res["target_disc"], 1)
        self.assertEqual(eval_res["target_track"], 2)
        self.assertFalse(eval_res["already_correct"])

    # 2. Incorrect proposed Recording ID (missing from release or invalid)
    def test_incorrect_proposed_recording_id(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": "", "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertFalse(eval_res["ok"])
        self.assertEqual(eval_res["code"], "recording_id_required")

    # 3. AcoustID conflict
    def test_acoustid_conflict_blocks_automatic_repair(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        # AcoustID says audio is REC3, which conflicts with proposed REC2
        self.acoustid_outcome = ProviderResult(
            provider="acoustid",
            outcome=ProviderOutcome.CONFIRMED,
            data=[{"mb_trackid": REC3, "score": 95.0, "title": "Track 3", "artist": "Test Artist"}],
        )

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertFalse(eval_res["ok"])
        self.assertEqual(eval_res["code"], "fingerprint_conflict")

    # 4. AcoustID unavailable (not treated as "no match")
    def test_acoustid_unavailable_reported_as_unavailable(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        self.acoustid_outcome = ProviderResult(
            provider="acoustid",
            outcome=ProviderOutcome.UNAVAILABLE,
            data=None,
        )

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertFalse(eval_res["ok"])
        self.assertEqual(eval_res["code"], "acoustid_unavailable")

    # 5. Ambiguous fingerprint / insufficient proof
    def test_ambiguous_fingerprint_requires_review(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        # No AcoustID result and embedded ID is REC1 (differs from REC2)
        self.acoustid_outcome = ProviderResult(
            provider="acoustid",
            outcome=ProviderOutcome.NO_RESULT,
            data=[],
        )

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertFalse(eval_res["ok"])
        self.assertIn(eval_res["code"], ("insufficient_proof", "recording_id_conflict"))

    # 6. MusicBrainz tracklist mismatch
    def test_mb_tracklist_mismatch(self):
        item = self.adapter.get_item(2)
        # Target claims disc 1 track 2 is REC3, but MB tracklist has REC2 at that position
        target = {"mb_trackid": REC3, "disc": 1, "track": 2, "title": "Track 3"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertFalse(eval_res["ok"])
        self.assertEqual(eval_res["code"], "mb_tracklist_mismatch")

    # 7. Disc/track conflict (slot in target album already occupied by another item)
    def test_disc_track_conflict_blocks_repair(self):
        item = self.adapter.get_item(2)
        # Target slot (1, 1) is already occupied by item 1 in target album 10
        target = {"mb_trackid": REC1, "disc": 1, "track": 1, "title": "Track 1"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertFalse(eval_res["ok"])
        self.assertEqual(eval_res["code"], "disc_track_conflict")

    # 8. Already-correct item
    def test_already_correct_item(self):
        # Item 1 is already track 1 (REC1) in album 10
        item = self.adapter.get_item(1)
        target = {"mb_trackid": REC1, "disc": 1, "track": 1, "title": "Track 1"}
        target_album = self.adapter.get_album(10)

        self.acoustid_outcome = ProviderResult(
            provider="acoustid",
            outcome=ProviderOutcome.CONFIRMED,
            data=[{"mb_trackid": REC1, "score": 98.0, "title": "Track 1", "artist": "Test Artist"}],
        )

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertTrue(eval_res["ok"])
        self.assertTrue(eval_res["already_correct"])

        apply_res = ds.apply_resolver_retag(eval_res, adapter=self.adapter, store=self.store)
        self.assertTrue(apply_res["ok"])
        self.assertFalse(apply_res.get("changed", True))
        self.assertEqual(apply_res.get("reason"), "already_correct")

    # 9. Recording repair followed by ownership move
    def test_recording_repair_followed_by_ownership_move(self):
        item = self.adapter.get_item(2)
        self.assertEqual(item["album_id"], 11)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertTrue(eval_res["ok"], eval_res)

        apply_res = ds.apply_resolver_retag(eval_res, adapter=self.adapter, store=self.store)
        self.assertTrue(apply_res["ok"], apply_res)
        self.assertTrue(apply_res["changed"])

        # Check item after repair & merge
        repaired = self.adapter.get_item(2)
        self.assertEqual(repaired["album_id"], 10)  # Ownership moved to target album
        self.assertEqual(repaired["mb_trackid"], REC2)  # Recording ID updated
        self.assertEqual(repaired["disc"], 1)
        self.assertEqual(repaired["track"], 2)
        self.assertEqual(repaired["title"], "Track 2")

        # Source album 11 was emptied and retired
        self.assertNotIn(11, self.adapter.albums)

    # 10. Rollback / recovery
    def test_rollback_restores_album_ownership(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        apply_res = ds.apply_resolver_retag(eval_res, adapter=self.adapter, store=self.store)
        self.assertTrue(apply_res["ok"])

        # Rollback the ownership merge
        merge_op_id = apply_res["merge_res"]["operation_id"]
        rb_res = cw.rollback_album_duplicate_merge(merge_op_id, adapter=self.adapter, store=self.store)
        self.assertTrue(rb_res["ok"], rb_res)

        # Source album row 11 restored
        self.assertIn(11, self.adapter.albums)
        self.assertEqual(self.adapter.get_item(2)["album_id"], 11)

    # 11. Repeated apply / idempotency
    def test_repeated_apply_is_idempotent(self):
        item = self.adapter.get_item(2)
        target = {"mb_trackid": REC2, "disc": 1, "track": 2, "title": "Track 2"}
        target_album = self.adapter.get_album(10)

        eval_res = ds.evaluate_resolver_retag(item, target, target_album, adapter=self.adapter, deps=self.deps)
        apply_res = ds.apply_resolver_retag(eval_res, adapter=self.adapter, store=self.store)
        self.assertTrue(apply_res["ok"])
        self.assertTrue(apply_res["changed"])

        # Second apply with fresh read of item
        item_fresh = self.adapter.get_item(2)
        eval_res2 = ds.evaluate_resolver_retag(item_fresh, target, target_album, adapter=self.adapter, deps=self.deps)
        self.assertTrue(eval_res2["ok"])
        self.assertTrue(eval_res2["already_correct"])

        apply_res2 = ds.apply_resolver_retag(eval_res2, adapter=self.adapter, store=self.store)
        self.assertTrue(apply_res2["ok"])
        self.assertFalse(apply_res2.get("changed", True))
        self.assertEqual(apply_res2.get("reason"), "already_correct")
