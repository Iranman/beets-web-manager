"""Untracked recovery service (backend/untracked_recovery_service.py):
backend-owned eligibility, deterministic identity for Class B attach,
provider failures never treated as "no match", strict Class A quarantine,
Class C through the one replacement authority, Plan -> Approve -> Apply ->
Verify -> Rollback."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as composite_workflows
import backend.untracked_recovery_service as svc
from backend.provider_boundary import ProviderOutcome, ProviderResult
from backend.resource_locks import ResourceLocks, set_locks
from backend.transaction_engine import TransactionStore

REL = "45347542-db98-422a-a307-ae95d5371f60"
RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REC13 = "00000000-0000-0000-0000-000000000013"


class FakeAdapter:
    def __init__(self, root):
        self.root = root
        self.albums = {1935: {"id": 1935, "mb_albumid": REL, "mb_releasegroupid": RG}}
        self.items = {25264: {"id": 25264, "album_id": 1935, "disc": 1, "track": 11,
                              "mb_trackid": "00000000-0000-0000-0000-000000000011",
                              "path": str(root / "a" / "11 Top Notch.flac")}}
        self.attach_calls = []
        self.quarantine_calls = []

    def find_all_albums_by_mb_albumid(self, rel):
        return [a for a in self.albums.values() if a["mb_albumid"] == rel]

    def find_all_items_by_album_id(self, aid):
        return [i for i in self.items.values() if i["album_id"] == aid]

    def find_all_items_by_mbid(self, rid):
        return [i for i in self.items.values() if i["mb_trackid"] == rid]

    def get_items(self, query=None):
        return list(self.items.values())

    def get_item(self, iid):
        return self.items.get(int(iid))

    def get_stats(self):
        return {"items": len(self.items), "albums": len(self.albums)}

    def untracked_attach(self, path, sha256, album_id, expected, idempotency_key):
        self.attach_calls.append(idempotency_key)
        self.items[30000] = {"id": 30000, "album_id": album_id, "disc": expected["disc"], "track": expected["track"],
                             "mb_trackid": expected["mb_trackid"], "path": path}
        return {"success": True, "record_id": svc.record_id_for("attach", idempotency_key), "item_id": 30000}

    def untracked_quarantine(self, files, idempotency_key):
        self.quarantine_calls.append(files)
        for f in files:
            Path(f["path"]).rename(Path(f["path"] + ".q"))
        return {"success": True, "record_id": svc.record_id_for("quarantine", idempotency_key)}

    def untracked_rollback(self, record_id, idempotency_key=None):
        self.items.pop(30000, None)
        return {"success": True}


def confirmed(*ids):
    return ProviderResult("acoustid", ProviderOutcome.CONFIRMED, data=[{"mb_trackid": i} for i in ids])


class UntrackedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "music"
        (self.root / "a").mkdir(parents=True)
        (self.root / "a" / "11 Top Notch.flac").write_bytes(b"tracked")
        self.missing = self.root / "a" / "BossMan Dlow - 2 Slippery - 13 - Parmesan.flac"
        self.missing.write_bytes(b"parmesan")
        self.inv = Path(self._tmp.name) / "inv"
        self.inv.mkdir()
        self.adapter = FakeAdapter(self.root)
        self.store = TransactionStore(str(Path(self._tmp.name) / "tx"))
        set_locks(ResourceLocks(Path(self._tmp.name) / "locks"))
        self.addCleanup(set_locks, None)
        p = mock.patch.object(composite_workflows, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)
        self.tags = {"mb_trackid": REC13, "mb_albumid": REL, "mb_releasegroupid": RG, "disc": 1, "track": 13,
                     "title": "Parmesan"}
        self.mb = {"ok": True, "outcome": "confirmed", "tracks": [{"disc": 1, "track": 13, "mb_trackid": REC13}]}
        self.heard = confirmed(REC13)
        self.deps = {"music_root": self.root, "inventory_dir": self.inv, "abs_path": lambda p: p,
                     "acoustid": lambda p: self.heard, "mb_tracklist": lambda rel: self.mb,
                     "read_tags": lambda p: dict(self.tags)}

    def _plan(self, action="attach", paths=None):
        return svc.plan_recovery(action, paths or ["a/BossMan Dlow - 2 Slippery - 13 - Parmesan.flac"],
                                 adapter=self.adapter, store=self.store, deps=self.deps)

    def _inventory(self, *records):
        with open(self.inv / "untracked_inventory.jsonl", "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def test_candidates_carry_backend_owned_eligibility(self):
        self._inventory({"path": "x/a.flac", "category": "import_artifact"},
                        {"path": "a/b.flac", "category": "canonical_album_file_missing_from_beets"},
                        {"path": "a/c.flac", "category": "exact_duplicate_of_tracked", "sha256": "1" * 64})
        rows = {r["path"]: r for r in svc.candidates(deps=self.deps)["rows"]}
        self.assertEqual(rows["x/a.flac"]["action_eligibility"], "not_eligible")
        self.assertIsNone(rows["x/a.flac"]["action"])  # a naming pattern alone is never proof
        self.assertEqual(rows["a/b.flac"]["action"], "attach")
        self.assertEqual(rows["a/c.flac"]["action"], "quarantine")
        for row in rows.values():
            for key in ("action", "action_eligibility", "requires_review", "safety_result", "conflicts", "reason"):
                self.assertIn(key, row)

    def test_class_b_attach_is_planned_only_with_deterministic_identity(self):
        res = self._plan()
        self.assertTrue(res["ok"], res)
        meta = self.store.get(res["operation_id"])["metadata"]
        self.assertEqual((meta["album_id"], meta["expected"]["track"]), (1935, 13))
        self.assertEqual(meta["sha256"], hashlib.sha256(b"parmesan").hexdigest())
        self.assertEqual(self.adapter.attach_calls, [])  # planning never mutates

    def test_class_b_refusals(self):
        self.tags["mb_trackid"] = ""
        self.assertEqual(self._plan()["code"], "no_recording_id")
        self.tags["mb_trackid"] = REC13
        self.adapter.albums[1936] = {"id": 1936, "mb_albumid": REL}
        self.assertEqual(self._plan()["code"], "album_row_not_unique")
        del self.adapter.albums[1936]
        self.mb = {"ok": True, "tracks": [{"disc": 1, "track": 13, "mb_trackid": "other"}]}
        self.assertEqual(self._plan()["code"], "slot_recording_mismatch")
        self.mb = {"ok": True, "tracks": [{"disc": 1, "track": 13, "mb_trackid": REC13}]}
        self.heard = confirmed("someone-else")
        self.assertEqual(self._plan()["code"], "fingerprint_disagreement")
        self.adapter.items[1] = {"id": 1, "album_id": 1935, "disc": 1, "track": 13, "mb_trackid": REC13, "path": "z"}
        self.heard = confirmed(REC13)
        self.assertEqual(self._plan()["code"], "slot_occupied")

    def test_provider_outages_fail_as_outages_never_as_no_match(self):
        self.mb = {"ok": False, "outcome": "unavailable", "tracks": []}
        self.assertEqual(self._plan()["code"], "musicbrainz_unavailable")
        self.mb = {"ok": True, "tracks": [{"disc": 1, "track": 13, "mb_trackid": REC13}]}
        for outcome in (ProviderOutcome.UNAVAILABLE, ProviderOutcome.RATE_LIMITED,
                        ProviderOutcome.AUTHENTICATION_ERROR, ProviderOutcome.TRANSIENT_ERROR):
            self.heard = ProviderResult("acoustid", outcome, data=[])
            self.assertEqual(self._plan()["code"], f"acoustid_{outcome.value}")

    def test_attach_apply_verify_and_roll_back(self):
        op = self._plan()["operation_id"]
        self.assertEqual(svc.apply_recovery(op, adapter=self.adapter, store=self.store)["code"], "not_approved")
        self.store.update(op, status="Approved")
        res = svc.apply_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["items_before"], res["items_after"]), (1, 2))
        self.assertEqual(self.adapter.attach_calls, [op])
        self.assertEqual(svc.apply_recovery(op, adapter=self.adapter, store=self.store)["code"], "already_applied")
        rb = svc.rollback_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_class_c_tracks_as_singleton_then_plans_the_canonical_replacement(self):
        self.tags.update(mb_trackid="00000000-0000-0000-0000-000000000011", track=11)
        self.heard = confirmed("00000000-0000-0000-0000-000000000011")
        res = self._plan("track_for_replacement")
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["album_id"])
        op = res["operation_id"]
        self.store.update(op, status="Approved")
        with mock.patch("backend.item_replacement.plan_verified_replacement",
                        return_value={"ok": True, "operation_id": "tx-repl"}) as plan_repl:
            out = svc.apply_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(out["ok"], out)
        plan_repl.assert_called_once()
        self.assertEqual(plan_repl.call_args.args[:2], (25264, 30000))
        self.assertEqual(out["replacement_plan"]["operation_id"], "tx-repl")

    def test_class_a_quarantine_only_for_proven_byte_identical_copies(self):
        twin = self.root / "a" / "11 Top Notch.flac"
        copy = self.root / "a" / "copy.flac"
        copy.write_bytes(b"tracked")
        artifact = self.root / "a" / "X.1.flac"
        artifact.write_bytes(b"whatever")
        self._inventory({"path": "a/copy.flac", "category": "exact_duplicate_of_tracked", "duplicate_of": str(twin)},
                        {"path": "a/X.1.flac", "category": "import_artifact"})
        res = self._plan("quarantine", ["a/copy.flac", "a/X.1.flac"])
        self.assertTrue(res["ok"], res)
        self.assertEqual([Path(f["path"]).name for f in res["files"]], ["copy.flac"])
        self.assertEqual(res["refused"][0]["path"], "a/X.1.flac")
        copy.write_bytes(b"changed")  # no longer identical -> refused at plan time
        self.assertEqual(self._plan("quarantine", ["a/copy.flac"])["code"], "nothing_eligible")

    def test_paths_outside_the_music_root_are_refused(self):
        self.assertEqual(self._plan(paths=["../../etc/passwd"])["code"], "file_missing")


REL_NEW = "9d3c1b2a-7e4f-4c6d-8a1b-2f3e4d5c6b7a"
RG_NEW = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


def rec(n):
    return f"00000000-0000-0000-0000-{n:012d}"


class AlbumAdapter(FakeAdapter):
    """FakeAdapter plus the attach-album engine op."""

    def __init__(self, root):
        super().__init__(root)
        self.album_calls = []

    def get_album(self, aid, expand=True):
        return self.albums.get(int(aid))

    def untracked_attach_album(self, release_id, release_group_id, files, idempotency_key):
        self.album_calls.append({"release_id": release_id, "files": files, "key": idempotency_key})
        self.albums[5000] = {"id": 5000, "mb_albumid": release_id, "mb_releasegroupid": release_group_id}
        ids = []
        for n, f in enumerate(files):
            iid = 40000 + n
            self.items[iid] = {"id": iid, "album_id": 5000, "disc": f["expected"]["disc"],
                               "track": f["expected"]["track"], "mb_trackid": f["expected"]["mb_trackid"],
                               "path": f["path"]}
            ids.append(iid)
        return {"success": True, "record_id": svc.record_id_for("attach_album", idempotency_key), "album_id": 5000,
                "item_ids": ids, "paths": [f["path"] for f in files]}

    def untracked_rollback(self, record_id, idempotency_key=None):
        self.albums.pop(5000, None)
        for iid in [i for i, row in self.items.items() if row["album_id"] == 5000]:
            del self.items[iid]
        return {"success": True}


class AlbumAttachTests(unittest.TestCase):
    """A release with no album row yet: the proven files of one folder become
    a new album row, in place (the 16.5k files attach cannot place)."""

    FOLDER = "New Artist/New Album (2020)"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "music"
        (self.root / "a").mkdir(parents=True)
        (self.root / "a" / "11 Top Notch.flac").write_bytes(b"tracked")
        self.folder = self.root / self.FOLDER
        self.folder.mkdir(parents=True)
        self.inv = Path(self._tmp.name) / "inv"
        self.inv.mkdir()
        self.adapter = AlbumAdapter(self.root)
        self.store = TransactionStore(str(Path(self._tmp.name) / "tx"))
        set_locks(ResourceLocks(Path(self._tmp.name) / "locks"))
        self.addCleanup(set_locks, None)
        self.tags, self.heard, self.records = {}, {}, []
        for track in (1, 2, 3):
            self.records.append(self._file(f"{track:02d}.flac", track))
        self._write_inventory()
        self.mb = {"ok": True, "outcome": "confirmed", "release_group": RG_NEW, "release_title": "New Album",
                   "release_artist": "New Artist",
                   "tracks": [{"disc": 1, "track": t, "mb_trackid": rec(t)} for t in (1, 2, 3, 4)]}
        self.deps = {"music_root": self.root, "inventory_dir": self.inv, "abs_path": lambda p: p,
                     "acoustid": lambda p: self.heard[p], "mb_tracklist": lambda rel: self.mb,
                     "read_tags": lambda p: dict(self.tags[p])}

    def _file(self, name, track, recording=None, **tag_overrides):
        path = self.folder / name
        path.write_bytes(f"audio-{name}".encode())
        key = self._key(name)
        self.tags[key] = {"mb_trackid": recording or rec(track), "mb_albumid": REL_NEW, "mb_releasegroupid": RG_NEW,
                          "disc": 1, "track": track, "title": f"T{track}", **tag_overrides}
        self.heard[key] = confirmed(recording or rec(track))
        return {"path": f"{self.FOLDER}/{name}", "size": path.stat().st_size,
                "category": "canonical_album_file_missing_from_beets"}

    def _key(self, name):
        import os
        return os.path.realpath(str(self.folder / name))

    def _write_inventory(self):
        with open(self.inv / "untracked_inventory.jsonl", "w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record) + "\n")

    def _plan(self, folder=None):
        return svc.plan_recovery("attach_album", [folder or self.FOLDER], adapter=self.adapter, store=self.store,
                                 deps=self.deps)

    def test_album_candidates_group_the_inventory_by_folder(self):
        self.records.append({"path": "Other/Album/01.flac", "size": 5,
                             "category": "canonical_album_file_missing_from_beets"})
        self.records.append({"path": "Junk/x{Album MbId}.flac", "size": 5, "category": "import_artifact"})
        self._write_inventory()
        res = svc.album_candidates(deps=self.deps)
        self.assertEqual((res["total"], res["files"]), (2, 4))
        self.assertEqual((res["rows"][0]["folder"], res["rows"][0]["files"]), (self.FOLDER, 3))
        self.assertEqual(res["rows"][0]["action"], "attach_album")

    def test_plan_proves_every_file_and_mutates_nothing(self):
        res = self._plan()
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["files"], res["release_track_count"], res["excluded"]), (3, 4, []))
        meta = self.store.get(res["operation_id"])["metadata"]
        self.assertEqual((meta["release_id"], meta["release_group_id"]), (REL_NEW, RG_NEW))
        self.assertEqual([f["expected"]["track"] for f in meta["files"]], [1, 2, 3])
        self.assertEqual(meta["files"][0]["sha256"], hashlib.sha256(b"audio-01.flac").hexdigest())
        self.assertEqual(self.adapter.album_calls, [])

    def test_unproven_files_are_excluded_with_a_reason_and_stay_untracked(self):
        self.records.append(self._file("05.flac", 5))                       # not on the MusicBrainz tracklist
        self.records.append(self._file("02 (copy).flac", 2))                # a second file for slot 2
        self.records.append(self._file("09.flac", 9, mb_releasegroupid=""))  # no Release Group tag
        self.heard[self._key("03.flac")] = confirmed(rec(77))               # the audio is another recording
        self._write_inventory()
        res = self._plan()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["files"], 1)
        reasons = {Path(e["path"]).name: e["reason"] for e in res["excluded"]}
        self.assertEqual(reasons, {"05.flac": "slot_recording_mismatch", "02.flac": "slot_contested",
                                   "02 (copy).flac": "slot_contested", "09.flac": "no_release_group_tag",
                                   "03.flac": "fingerprint_disagreement"})

    def test_refusals(self):
        self.assertEqual(self._plan("../outside")["code"], "folder_missing")
        self.assertEqual(self._plan("a")["code"], "no_candidates")
        self.adapter.albums[77] = {"id": 77, "mb_albumid": REL_NEW, "mb_releasegroupid": RG_NEW}
        self.assertEqual(self._plan()["code"], "album_row_exists")
        del self.adapter.albums[77]
        self.mb["release_group"] = RG
        self.assertEqual(self._plan()["code"], "release_group_mismatch")
        self.mb["release_group"] = RG_NEW
        self.tags[self._key("03.flac")]["mb_albumid"] = REL
        self.assertEqual(self._plan()["code"], "multiple_releases")
        self.tags[self._key("03.flac")]["mb_albumid"] = REL_NEW
        for key in self.heard:
            self.heard[key] = confirmed(rec(99))
        self.assertEqual(self._plan()["code"], "nothing_proven")
        for key in self.tags:
            self.tags[key]["mb_trackid"] = ""
        self.assertEqual(self._plan()["code"], "nothing_tagged")
        self.assertEqual(self.adapter.album_calls, [])

    def test_a_provider_that_cannot_be_asked_fails_the_whole_plan(self):
        self.mb = {"ok": False, "outcome": "rate_limited", "tracks": []}
        self.assertEqual(self._plan()["code"], "musicbrainz_rate_limited")
        self.mb = {"ok": True, "release_group": RG_NEW,
                   "tracks": [{"disc": 1, "track": t, "mb_trackid": rec(t)} for t in (1, 2, 3)]}
        self.heard[self._key("02.flac")] = ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[])
        self.assertEqual(self._plan()["code"], "acoustid_unavailable")  # never "excluded as no match"

    def test_apply_needs_approval_verifies_and_rolls_back(self):
        op = self._plan()["operation_id"]
        self.assertEqual(svc.apply_recovery(op, adapter=self.adapter, store=self.store)["code"], "not_approved")
        self.store.update(op, status="Approved")
        res = svc.apply_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["album_id"], res["items_after"] - res["items_before"]), (5000, 3))
        [call] = self.adapter.album_calls
        self.assertEqual((call["release_id"], call["key"], len(call["files"])), (REL_NEW, op, 3))
        self.assertEqual(sorted(call["files"][0]), ["expected", "path", "sha256"])
        self.assertEqual(svc.apply_recovery(op, adapter=self.adapter, store=self.store)["code"], "already_applied")
        rb = svc.rollback_recovery(op, adapter=self.adapter, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertNotIn(5000, self.adapter.albums)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_verification_flags_a_row_that_does_not_hold_exactly_the_planned_files(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        original = self.adapter.untracked_attach_album

        def short(release_id, release_group_id, files, idempotency_key):
            out = original(release_id, release_group_id, files, idempotency_key)
            del self.adapter.items[40002]
            return out
        self.adapter.untracked_attach_album = short
        res = svc.apply_recovery(op, adapter=self.adapter, store=self.store)
        self.assertEqual(res["status"], "Recovery Required")
        self.assertTrue(any("not in the new album row" in p for p in res["verification_problems"]))

    def test_restart_recovery_knows_the_family(self):
        import backend.transaction_recovery as recovery
        self.assertIn(svc.ATTACH_ALBUM_FAMILY, recovery._families())


if __name__ == "__main__":
    unittest.main()
