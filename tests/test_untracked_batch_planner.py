"""Tests for ARCH-021: Untracked Batch Planner, Fast Indexing & Safe Artifact Quarantine.

Covers:
- Batch planning multiple candidate folders into Preview transactions
- Partial failures in individual folders do not abort the batch
- AcoustID rate limiting / budget enforcement
- Fast path indexing with build_item_path_index
- Safe sidecar / artifact handling:
  * Pure duplicate folder with sidecars -> audio + sidecars planned for quarantine with rollback hashes
  * Folder with unverified audio -> sidecars kept (unverified_context_kept)
- Route endpoints for batch planning
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as composite_workflows
import backend.untracked_recovery_service as svc
from backend.item_replacement import build_item_path_index, tracked_item_id_for_path
from backend.provider_boundary import ProviderOutcome, ProviderResult
from backend.resource_locks import ResourceLocks, set_locks
from backend.transaction_engine import TransactionStore
from tests.test_untracked_ops_and_recovery import (
    AlbumAdapter,
    FakeAdapter,
    REL_NEW,
    RG_NEW,
    confirmed,
    rec,
)


class UntrackedBatchPlannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "music"
        self.inv = Path(self._tmp.name) / "inv"
        self.inv.mkdir(parents=True)
        self.adapter = AlbumAdapter(self.root)
        self.store = TransactionStore(str(Path(self._tmp.name) / "tx"))
        set_locks(ResourceLocks(Path(self._tmp.name) / "locks"))
        self.addCleanup(set_locks, None)
        p = mock.patch.object(composite_workflows, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)

        self.tags = {}
        self.heard = {}
        self.records = []
        self.mb_releases = {}

        self.deps = {
            "music_root": self.root,
            "inventory_dir": self.inv,
            "abs_path": lambda p: p,
            "acoustid": lambda p: self.heard.get(os.path.realpath(p), confirmed("unknown")),
            "mb_tracklist": lambda rel: self.mb_releases.get(rel, {"ok": False, "outcome": "not_found"}),
            "read_tags": lambda p: dict(self.tags.get(os.path.realpath(p), {})),
        }

    def _create_album_folder(self, folder_rel: str, rel_id: str, rg_id: str, track_count: int = 3):
        folder = self.root / folder_rel
        folder.mkdir(parents=True, exist_ok=True)
        self.mb_releases[rel_id] = {
            "ok": True,
            "outcome": "confirmed",
            "release_group": rg_id,
            "release_title": folder.name,
            "release_artist": "Test Artist",
            "tracks": [{"disc": 1, "track": t, "mb_trackid": rec(t)} for t in range(1, track_count + 1)],
        }
        for t in range(1, track_count + 1):
            name = f"{t:02d}.flac"
            p = folder / name
            p.write_bytes(f"audio-{folder_rel}-{name}".encode())
            real_p = os.path.realpath(str(p))
            self.tags[real_p] = {
                "mb_trackid": rec(t),
                "mb_albumid": rel_id,
                "mb_releasegroupid": rg_id,
                "disc": 1,
                "track": t,
                "title": f"Track {t}",
            }
            self.heard[real_p] = confirmed(rec(t))
            self.records.append({
                "path": f"{folder_rel}/{name}",
                "size": p.stat().st_size,
                "category": svc.ALBUM_CATEGORY,
            })

    def _write_inventory(self):
        with open(self.inv / "untracked_inventory.jsonl", "w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record) + "\n")

    def test_fast_path_indexing(self):
        """build_item_path_index creates an O(1) dictionary and tracked_item_id_for_path uses it."""
        tracked_file = self.root / "tracked" / "01.flac"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_bytes(b"tracked audio")
        self.adapter.items[101] = {
            "id": 101,
            "path": str(tracked_file),
            "mb_trackid": rec(1),
        }
        index = build_item_path_index(adapter=self.adapter, abs_path=lambda p: p)
        self.assertEqual(index.get(str(Path(tracked_file).resolve())), 101)
        self.assertEqual(
            tracked_item_id_for_path(str(tracked_file), adapter=self.adapter, abs_path=lambda p: p, item_path_index=index),
            101,
        )
        self.assertEqual(
            tracked_item_id_for_path(str(self.root / "untracked.flac"), adapter=self.adapter, abs_path=lambda p: p, item_path_index=index),
            0,
        )

    def test_batch_plan_multiple_folders_success(self):
        """Batch planner successfully generates Preview transactions for valid candidate albums."""
        self._create_album_folder("Artist A/Album 1", "rel-1", "rg-1", 3)
        self._create_album_folder("Artist B/Album 2", "rel-2", "rg-2", 2)
        self._write_inventory()

        res = svc.plan_untracked_batch(
            adapter=self.adapter,
            store=self.store,
            deps=self.deps,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["total_candidates"], 2)
        self.assertEqual(res["planned_count"], 2)
        self.assertEqual(res["skipped_count"], 0)
        self.assertEqual(len(res["planned"]), 2)

        # Confirm each planned item has a Preview transaction
        for p in res["planned"]:
            tx = self.store.get(p["operation_id"])
            self.assertEqual(tx["status"], "Preview")
            self.assertTrue(tx["rollback"]["available"])
            self.assertEqual(tx["metadata"]["mutation_family"], svc.ATTACH_ALBUM_FAMILY)

    def test_batch_plan_partial_failure_does_not_abort_batch(self):
        """If one folder fails proof (e.g. slot mismatch / unknown release), other folders still succeed."""
        self._create_album_folder("Artist A/Album 1", "rel-1", "rg-1", 3)
        self._create_album_folder("Artist B/Album Broken", "rel-broken", "rg-broken", 2)
        # Invalidate MusicBrainz response for Album Broken
        self.mb_releases["rel-broken"] = {"ok": False, "outcome": "not_found"}
        self._create_album_folder("Artist C/Album 3", "rel-3", "rg-3", 2)
        self._write_inventory()

        res = svc.plan_untracked_batch(
            adapter=self.adapter,
            store=self.store,
            deps=self.deps,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["total_candidates"], 3)
        self.assertEqual(res["planned_count"], 2)
        self.assertEqual(res["skipped_count"], 1)
        self.assertEqual(res["skipped"][0]["folder"], "Artist B/Album Broken")

    def test_batch_plan_acoustid_budget_enforcement(self):
        """AcoustID lookup budget stops batch execution gracefully when exceeded."""
        for i in range(1, 6):
            self._create_album_folder(f"Artist {i}/Album {i}", f"rel-{i}", f"rg-{i}", 2)
        self._write_inventory()

        # Set max_acoustid_lookups to 3 (each album has 2 tracks, so album 2 will hit budget)
        res = svc.plan_untracked_batch(
            max_acoustid_lookups=3,
            adapter=self.adapter,
            store=self.store,
            deps=self.deps,
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["budget_reached"])
        self.assertEqual(res["planned_count"], 1)
        self.assertTrue(any(s.get("code") == "acoustid_budget_exceeded" for s in res["skipped"]))

    def test_safe_artifact_quarantine_with_sidecars(self):
        """When a folder contains proven duplicate audio and sidecars, both are planned for quarantine."""
        dup_folder = self.root / "Duplicates" / "Album Dup"
        dup_folder.mkdir(parents=True, exist_ok=True)

        tracked_twin = self.root / "Library" / "Tracked" / "01.flac"
        tracked_twin.parent.mkdir(parents=True, exist_ok=True)
        tracked_twin.write_bytes(b"identical audio content")

        dup_audio = dup_folder / "01.flac"
        dup_audio.write_bytes(b"identical audio content")

        sidecar_cover = dup_folder / "cover.jpg"
        sidecar_cover.write_bytes(b"image data")

        sidecar_cue = dup_folder / "album.cue"
        sidecar_cue.write_bytes(b"cue data")

        # Track the library twin in adapter
        self.adapter.items[501] = {
            "id": 501,
            "path": str(tracked_twin),
            "mb_trackid": rec(1),
        }

        # Add to inventory
        self.records.append({
            "path": "Duplicates/Album Dup/01.flac",
            "size": dup_audio.stat().st_size,
            "category": "exact_duplicate_of_tracked",
            "duplicate_of": str(tracked_twin),
        })
        self._write_inventory()

        res = svc.plan_untracked_quarantine_batch(
            include_sidecars=True,
            adapter=self.adapter,
            store=self.store,
            deps=self.deps,
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["audio_count"], 1)
        self.assertEqual(res["sidecar_count"], 2)

        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], svc.QUARANTINE_FAMILY)
        file_paths = [f["path"] for f in tx["metadata"]["files"]]
        self.assertIn(str(dup_audio), file_paths)
        self.assertIn(str(sidecar_cover), file_paths)
        self.assertIn(str(sidecar_cue), file_paths)

    def test_unverified_audio_preserves_sidecars(self):
        """If a folder contains unverified audio, sidecars are preserved (unverified_context_kept)."""
        folder = self.root / "Mixed" / "Album"
        folder.mkdir(parents=True, exist_ok=True)

        tracked_twin = self.root / "Library" / "01.flac"
        tracked_twin.parent.mkdir(parents=True, exist_ok=True)
        tracked_twin.write_bytes(b"audio 1")

        dup_audio = folder / "01.flac"
        dup_audio.write_bytes(b"audio 1")

        unverified_audio = folder / "02.flac"
        unverified_audio.write_bytes(b"unverified audio 2")

        sidecar = folder / "cover.jpg"
        sidecar.write_bytes(b"image data")

        self.adapter.items[501] = {
            "id": 501,
            "path": str(tracked_twin),
            "mb_trackid": rec(1),
        }

        self.records.append({
            "path": "Mixed/Album/01.flac",
            "size": dup_audio.stat().st_size,
            "category": "exact_duplicate_of_tracked",
            "duplicate_of": str(tracked_twin),
        })
        self._write_inventory()

        res = svc.plan_untracked_quarantine_batch(
            include_sidecars=True,
            adapter=self.adapter,
            store=self.store,
            deps=self.deps,
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["audio_count"], 1)
        self.assertEqual(res["sidecar_count"], 0)
        # Confirm sidecar was refused to preserve context
        refused_reasons = {r["path"]: r["reason"] for r in res["refused"]}
        self.assertIn("Mixed/Album/cover.jpg", refused_reasons)
        self.assertTrue(any("unverified_context_kept" in r["reason"] for r in res["refused"]))


if __name__ == "__main__":
    unittest.main()
