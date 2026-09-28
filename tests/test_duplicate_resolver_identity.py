"""Duplicate-resolver identity regressions (ARCH-002 Part H / ARCH-009).

Proves, against the real functions:
* fuzzy/album+title text matches are provisional and an AcoustID
  disagreement rejects them (source contract),
* manual cleanup without an explicit path list deletes nothing, and the
  default is dry-run,
* unattended maintenance deletion never treats "same Recording ID on a
  different release" as a duplicate file, and never selects both sides of
  a mutual pair.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app as app_module
from backend.duplicate_identity import release_relation
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402


REL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REL2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _item(item_id, album_id, mb_albumid=REL, disc=1, track=3):
    return SimpleNamespace(id=item_id, album_id=album_id, mb_albumid=mb_albumid, disc=disc, track=track)


class ReleaseRelationTests(unittest.TestCase):
    def test_same_album_same_position(self):
        self.assertEqual(release_relation(_item(2, 7), _item(1, 7)), "same_album_position")

    def test_same_release_split_across_album_rows(self):
        # Live shape: one release imported twice as two album rows.
        self.assertEqual(release_relation(_item(2, 8), _item(1, 7)), "same_release_position")

    def test_same_release_singletons(self):
        self.assertEqual(release_relation(_item(2, None), _item(1, None)), "same_release_position")

    def test_same_recording_on_compilation(self):
        self.assertEqual(release_relation(_item(2, 8, REL2, track=9), _item(1, 7)), "different_release")

    def test_same_recording_twice_on_one_album(self):
        self.assertEqual(release_relation(_item(2, 7, track=12), _item(1, 7)), "different_position")

    def test_untracked_source(self):
        self.assertEqual(release_relation(None, _item(1, 7)), "not_library_item")

    def test_missing_release_identity_is_unknown(self):
        self.assertEqual(release_relation(_item(2, None, ""), _item(1, 7)), "unknown")


class MaintenanceAutoSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = self.root / "Artist" / "Album" / "01 Song.flac"
        self.b = self.root / "Artist" / "Album" / "01 Song.1.flac"
        self.c = self.root / "Various" / "Hits" / "07 Song.flac"
        for path in (self.a, self.b, self.c):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
        patcher = patch_app_family(app_module, "MUSIC_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _dup(self, source, lib, source_item_id, lib_id, relation, match_type="MB Track ID", **extra):
        data = {
            "source_path": str(source),
            "lib_path": str(lib),
            "source_item_id": source_item_id,
            "lib_id": lib_id,
            "release_relation": relation,
            "match_type": match_type,
            "confidence": "high",
            "fingerprint_verified": True,
        }
        data.update(extra)
        return data

    def test_same_recording_on_compilation_is_never_auto_deleted(self):
        scan = {"duplicates": [self._dup(self.c, self.a, 30, 10, "different_release")]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [])

    def test_mutual_in_album_pair_selects_exactly_one_copy(self):
        scan = {"duplicates": [
            self._dup(self.a, self.b, 10, 11, "same_release_position"),
            self._dup(self.b, self.a, 11, 10, "same_release_position"),
        ]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [str(self.b.resolve())])

    def test_shared_embedded_recording_id_without_audio_proof_is_not_auto_deleted(self):
        # Live finding: embedded Recording IDs shared by "duplicates" were
        # contradicted by the fingerprint in 6 of 7 groups.
        scan = {"duplicates": [self._dup(self.b, self.a, 11, 10, "same_release_position",
                                         match_type="MB Track ID", fingerprint_verified=False)]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [])

    def test_byte_identical_copy_without_fingerprint_is_selected(self):
        self.a.write_bytes(b"same-bytes")
        self.b.write_bytes(b"same-bytes")
        scan = {"duplicates": [self._dup(self.b, self.a, 11, 10, "same_release_position",
                                         match_type="identical file size", fingerprint_verified=False)]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [str(self.b.resolve())])
    def test_fuzzy_text_match_without_fingerprint_is_not_auto_deleted(self):
        scan = {"duplicates": [self._dup(self.b, self.a, 11, 10, "same_album_position", match_type="fuzzy match 93%", fingerprint_verified=False)]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [])

    def test_same_recording_at_another_position_is_not_auto_deleted(self):
        scan = {"duplicates": [self._dup(self.b, self.a, 11, 10, "different_position")]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [])

    def test_unknown_release_relation_is_not_auto_deleted(self):
        scan = {"duplicates": [self._dup(self.b, self.a, 11, 10, "unknown")]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [])

    def test_medium_confidence_is_not_auto_deleted(self):
        scan = {"duplicates": [self._dup(self.b, self.a, 11, 10, "same_album_position", confidence="medium")]}
        self.assertEqual(app_module._maintenance_duplicate_cleanup_paths(scan), [])


class ManualCleanupContractTests(unittest.TestCase):
    def _post(self, payload):
        return app_module.app.test_request_context("/api/dedup/cleanup", method="POST", json=payload)

    def test_no_explicit_paths_deletes_nothing(self):
        with mock.patch.object(app_module.composite_workflows, "plan_library_cleanup",
                               return_value={"ok": False, "results": [], "planned_count": 0}) as plan, \
                mock.patch.object(app_module.composite_workflows, "apply_library_cleanup") as apply_:
            with self._post({}):
                response = app_module.dedup_cleanup()
        data = response.get_json()
        self.assertEqual(data.get("deleted"), 0)
        apply_.assert_not_called()
        self.assertEqual(plan.call_args[0][0]["paths"], [])

    def test_default_is_dry_run_and_never_applies(self):
        planned = {"ok": True, "results": [{"path": "/music/x.flac", "ok": True}], "planned_count": 1,
                   "operation_id": "op-1"}
        with mock.patch.object(app_module.composite_workflows, "plan_library_cleanup", return_value=planned), \
                mock.patch.object(app_module.composite_workflows, "apply_library_cleanup") as apply_:
            with self._post({"paths": ["/music/x.flac"]}):
                response = app_module.dedup_cleanup()
        data = response.get_json()
        self.assertTrue(data["dry_run"])
        apply_.assert_not_called()


class ScanSourceContractTests(unittest.TestCase):
    """Fuzzy/album+title matches are provisional; a disagreeing AcoustID
    fingerprint rejects them, and the album+title step no longer raises."""

    @classmethod
    def setUpClass(cls):
        src = Path(app_module.__file__).read_text(encoding="utf-8")
        start = src.index('@app.post("/api/dedup/scan")')
        cls.scan = src[start:src.index('@app.post("/api/dedup/cleanup")')]

    def test_fuzzy_match_plus_acoustid_disagreement_is_rejected(self):
        self.assertIn('match_type.startswith("fuzzy match")', self.scan)
        self.assertIn('match_type.startswith("album+title")', self.scan)
        self.assertIn("elif src_fp_ids and lib_fp_ids:", self.scan)
        self.assertIn("REJECTED", self.scan)

    def test_mb_track_id_matches_are_fingerprint_cross_checked(self):
        self.assertIn('match_type == "MB Track ID" or match_type.startswith("fuzzy match")', self.scan)
    def test_album_title_step_uses_a_defined_logger(self):
        self.assertIn("logger_instance=_app_logger", self.scan)

    def test_album_title_helper_runs_without_name_error(self):
        library = mock.Mock()
        library.items.return_value = []
        self.assertEqual(
            app_module._resolve_album_title_duplicate_candidate(library, "WILLOW (2019)", "Wait a Minute"),
            (None, ""),
        )


class DuplicateGroupSafetyTests(unittest.TestCase):
    """Never regress the v0.1.28 fix: unattended cleanup keeps at least one
    copy of every duplicate group, whatever its size or scan order."""

    def test_no_group_can_lose_every_copy(self):
        import itertools
        import random
        from backend.duplicate_identity import select_unattended_cleanup_paths
        rng = random.Random(7)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for size in range(2, 7):
                paths = []
                for n in range(size):
                    p = root / f"g{size}" / f"copy{n}.flac"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"x")
                    paths.append(p)
                ids = rng.sample(range(100, 999), size)
                dups = [
                    {"source_path": str(paths[a]), "lib_path": str(paths[b]), "source_item_id": ids[a],
                     "lib_id": ids[b], "release_relation": "same_release_position",
                     "match_type": "MB Track ID", "confidence": "high", "fingerprint_verified": True}
                    for a, b in itertools.permutations(range(size), 2)
                ]
                rng.shuffle(dups)
                selected = select_unattended_cleanup_paths({"duplicates": dups}, root, lambda p, r: True)
                self.assertEqual(len(selected), size - 1, size)
                kept = {str(paths[i].resolve()) for i in range(size)} - set(selected)
                self.assertEqual(kept, {str(paths[ids.index(min(ids))].resolve())})


if __name__ == "__main__":
    unittest.main()
