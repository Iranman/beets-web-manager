"""Read-only library integrity reports: duplicate album rows and the
untracked-file inventory. Neither may mutate anything or call AcoustID."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.acoustid_service as acoustid_service
from backend.album_duplicate_analysis import analyze
from backend.untracked_inventory import build_inventory

RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REL_A = "45347542-db98-422a-a307-ae95d5371f60"
REL_B = "11111111-1111-1111-1111-111111111111"
REC = "2513c401-c500-42fe-9113-ce9d9c3295d5"
OTHER = "99999999-9999-9999-9999-999999999999"


def _item(iid, album, track, rid="", path=None, disc=1):
    return {"id": iid, "album_id": album, "disc": disc, "track": track, "mb_trackid": rid,
            "format": "FLAC", "path": path or f"A/B/{iid}.flac"}


class AlbumDuplicateAnalysisTests(unittest.TestCase):
    def _albums(self, rel_b=REL_A):
        return [{"id": 1935, "album": "2 Slippery", "albumartist": "BossMan Dlow", "mb_albumid": REL_A,
                 "mb_releasegroupid": RG},
                {"id": 2100, "album": "2 Slippery", "albumartist": "BossMan Dlow", "mb_albumid": rel_b,
                 "mb_releasegroupid": RG},
                {"id": 7, "album": "Other", "mb_releasegroupid": "rg-solo", "mb_albumid": "r7"},
                {"id": 8, "album": "No RG"}]

    def test_complementary_single_edition_group_is_deterministic(self):
        items = [_item(1, 1935, 11), _item(2, 1935, 17), _item(3, 2100, 1), _item(4, 2100, 2), _item(5, 2100, 3)]
        report = analyze(self._albums(), items)
        self.assertEqual((report["duplicate_groups"], report["album_rows_without_release_group"]), (1, 1))
        [group] = report["groups"]
        self.assertEqual(group["release_group_id"], RG)
        self.assertEqual(group["retain_album_id"], 2100)  # most tracks
        self.assertTrue(group["deterministic"], group["blockers"])
        self.assertEqual(sorted(m["item_id"] for m in group["proposed_moves"]), [1, 2])
        self.assertEqual({m["to_album_id"] for m in group["proposed_moves"]}, {2100})

    def test_differing_editions_block_the_merge(self):
        items = [_item(1, 1935, 1), _item(2, 2100, 2)]
        [group] = analyze(self._albums(rel_b=REL_B), items)["groups"]
        self.assertFalse(group["deterministic"])
        self.assertTrue(any("differing editions" in b for b in group["blockers"]))
        self.assertEqual(set(group["editions"]), {REL_A.lower(), REL_B.lower()})

    def test_overlapping_slots_are_classified_and_block(self):
        items = [_item(1, 1935, 11, REC), _item(2, 2100, 11, REC),        # same recording by ID
                 _item(3, 1935, 12, REC), _item(4, 2100, 12, OTHER),      # conflict
                 _item(5, 1935, 13, ""), _item(6, 2100, 13, "")]          # contested: AcoustID cache
        cached = {"A/B/5.flac": [REC], "A/B/6.flac": [REC]}
        [group] = analyze(self._albums(), items, cached_ids=cached.get)["groups"]
        rel = {o["track"]: o["relation"] for o in group["overlapping_slots"]}
        self.assertEqual(rel, {11: "same_recording_by_recording_id", 12: "recording_ids_differ",
                               13: "same_recording_by_acoustid"})
        self.assertFalse(group["deterministic"])
        self.assertTrue(any("conflicting recordings" in b for b in group["blockers"]))

    def test_uncached_audio_is_reported_not_fingerprinted(self):
        items = [_item(5, 1935, 13, ""), _item(6, 2100, 13, "")]
        [group] = analyze(self._albums(), items, cached_ids=lambda p: None)["groups"]
        [overlap] = group["overlapping_slots"]
        self.assertEqual(overlap["relation"], "recording_identity_missing")
        self.assertEqual(overlap["acoustid_cached"], ["not_cached", "not_cached"])

    def test_unpositioned_items_block(self):
        items = [_item(1, 1935, 0), _item(2, 2100, 1)]
        [group] = analyze(self._albums(), items)["groups"]
        self.assertIn(1, group["unpositioned_item_ids"])
        self.assertFalse(group["deterministic"])


class CacheOnlyFingerprintTests(unittest.TestCase):
    def test_cache_only_reader_never_calls_acoustid(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "x.flac"
            audio.write_bytes(b"audio")
            with mock.patch.object(acoustid_service, "_ACOUSTID_FILE_CACHE_DIR", Path(td) / "cache"), \
                    mock.patch.object(acoustid_service, "_acoustid_lookup") as lookup:
                self.assertIsNone(acoustid_service._acoustid_cached_fingerprint_ids(str(audio)))
                _path, key = acoustid_service._audio_cache_file_identity(str(audio))
                entry = Path(td) / "cache" / key[:2] / f"{key}.json"
                entry.parent.mkdir(parents=True)
                entry.write_text(json.dumps([{"mb_trackid": REC.upper()}]), encoding="utf-8")
                self.assertEqual(acoustid_service._acoustid_cached_fingerprint_ids(str(audio)), [REC])
            lookup.assert_not_called()


class UntrackedInventoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "music"
        self.out = Path(self._tmp.name) / "data"
        album = self.root / "BossMan Dlow (44fac0de-d5ae-4bea-a68c-948fedc3d9c5)" / \
            "2 Slippery (2023) {ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d}"
        album.mkdir(parents=True)
        self.files = {
            "tracked": album / "BossMan Dlow - 2 Slippery - 11 - Top Notch.flac",
            "exact": album / "copy of top notch.flac",
            "recording": album / "top notch.mp3",
            "artifact": album / "BossMan Dlow - 2 Slippery - 12 - X.1.flac",
            "canonical": album / "BossMan Dlow - 2 Slippery - 13 - Y.flac",
            "loose": self.root / "BossMan Dlow (44fac0de-d5ae-4bea-a68c-948fedc3d9c5)" / "single.flac",
            "unknown": self.root / "Misc" / "Deep" / "thing.flac",
            "not_audio": album / "cover.jpg",
        }
        self.files["unknown"].parent.mkdir(parents=True)
        self.files["tracked"].write_bytes(b"T" * 100)
        self.files["exact"].write_bytes(b"T" * 100)
        self.files["recording"].write_bytes(b"R" * 50)
        self.files["artifact"].write_bytes(b"A" * 60)
        self.files["canonical"].write_bytes(b"C" * 70)
        self.files["loose"].write_bytes(b"L" * 80)
        self.files["unknown"].write_bytes(b"U" * 90)
        self.files["not_audio"].write_bytes(b"jpg")
        self.tracked = [{"id": 1, "path": str(self.files["tracked"]), "mb_trackid": REC}]

    def _run(self, cached):
        before = {p: p.read_bytes() for p in self.files.values()}
        summary = build_inventory(self.root, self.tracked, abs_path=lambda p: p,
                                  cached_ids=lambda p: cached.get(Path(p).name), out_dir=self.out)
        self.assertEqual({p: p.read_bytes() for p in self.files.values()}, before)  # nothing touched
        return summary

    def test_every_category_and_no_mutation(self):
        summary = self._run({"top notch.mp3": [REC]})
        self.assertEqual(summary["audio_files_on_disk"], 7)
        self.assertEqual(summary["untracked_audio_files"], 6)
        self.assertEqual(summary["counts"], {
            "exact_duplicate_of_tracked": 1, "same_recording_other_encoding": 1, "import_artifact": 1,
            "canonical_album_file_missing_from_beets": 1, "loose_singleton": 1, "unknown": 1})
        self.assertEqual(summary["mutations_performed"], 0)
        self.assertEqual(summary["stats"]["acoustid_api_calls"], 0)
        rows = [json.loads(line) for line in (self.out / "untracked_inventory.jsonl").read_text().splitlines()]
        by_name = {os.path.basename(r["path"]): r for r in rows}
        self.assertEqual(by_name["copy of top notch.flac"]["duplicate_of"],
                         str(self.files["tracked"].resolve()))
        self.assertEqual(by_name["top notch.mp3"]["recording_ids"], [REC])
        self.assertTrue((self.out / "untracked_inventory_summary.json").is_file())

    def test_uncached_files_are_counted_not_fingerprinted(self):
        summary = self._run({})
        self.assertEqual(summary["counts"]["same_recording_other_encoding"], 0)
        self.assertEqual(summary["stats"]["acoustid_cache_misses"], 5)  # the exact duplicate needs no lookup


if __name__ == "__main__":
    unittest.main()
