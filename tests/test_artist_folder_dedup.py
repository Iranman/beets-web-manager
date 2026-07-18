import ast
import re
import tempfile
import unicodedata
import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "app.py"


def _load_namespace(apply_stub=None):
    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    names = {
        "_UNICODE_NORM",
        "_normalize_name",
        "_artist_folder_key",
        "_scan_artist_folder_groups",
        "_auto_merge_case_duplicate_artist_folder",
    }
    ns: Dict[str, Any] = {
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional,
        "Path": Path, "re": re, "unicodedata": unicodedata, "defaultdict": defaultdict,
        "_s": lambda value: (value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")),
        "_count_audio_files": lambda folder: {"audio": 0, "folders": 0},
        "_artist_folder_db_counts": lambda: {},
        "_mb_canonical_for_artist_entries": lambda entries, key: {},
    }
    if apply_stub is not None:
        ns["_apply_artist_folder_groups"] = apply_stub
    for node in tree.body:
        node_name = ""
        if isinstance(node, ast.Assign):
            node_name = getattr(node.targets[0], "id", "")
        elif isinstance(node, ast.FunctionDef):
            node_name = node.name
        if node_name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, str(APP_SOURCE), "exec"), ns)
    return ns


class ArtistFolderKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_namespace()

    def test_case_variants_produce_the_same_key(self):
        key = self.ns["_artist_folder_key"]
        self.assertEqual(key("Aaliyah"), key("aaliyah"))
        self.assertEqual(key("Al Campbell"), key("al campbell"))

    def test_mbid_stamped_variants_still_match_case_only_difference(self):
        # Real case seen live: "aaliyah (bc85da58-...)" vs "Aaliyah (bc85da58-...)"
        key = self.ns["_artist_folder_key"]
        a = "aaliyah (bc85da58-52d9-457d-ae8d-5d8d4ec870a9)"
        b = "Aaliyah (bc85da58-52d9-457d-ae8d-5d8d4ec870a9)"
        self.assertEqual(key(a), key(b))

    def test_different_artists_produce_different_keys(self):
        key = self.ns["_artist_folder_key"]
        self.assertNotEqual(key("Aaliyah"), key("Al Campbell"))


class ArtistFolderScanTests(unittest.TestCase):
    def test_punctuation_variant_folders_are_grouped_together(self):
        # NTFS is case-insensitive, so "Aaliyah"/"aaliyah" can't coexist as two
        # real directories in a Windows test fixture (that exact collision is
        # proven at the key level in ArtistFolderKeyTests instead, and is the
        # real-world failure mode on the live Linux container). This exercises
        # the same grouping logic via a punctuation/spacing variant, which
        # Windows can represent as two distinct directories.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Al Campbell").mkdir()
            (root / "Al  Campbell").mkdir()
            ns = _load_namespace()
            groups = ns["_scan_artist_folder_groups"](str(root), use_musicbrainz=False)
            self.assertEqual(len(groups), 1)
            names = sorted(v["name"] for v in groups[0]["variants"])
            self.assertEqual(names, ["Al  Campbell", "Al Campbell"])

    def test_single_folder_is_not_reported_as_a_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Only Artist").mkdir()
            ns = _load_namespace()
            groups = ns["_scan_artist_folder_groups"](str(root), use_musicbrainz=False)
            self.assertEqual(groups, [])

    def test_unrelated_artist_folders_are_not_grouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Aaliyah").mkdir()
            (root / "Al Campbell").mkdir()
            ns = _load_namespace()
            groups = ns["_scan_artist_folder_groups"](str(root), use_musicbrainz=False)
            self.assertEqual(groups, [])


class AutoMergeCaseDuplicateTests(unittest.TestCase):
    def test_merges_when_a_case_duplicate_is_found_without_musicbrainz(self):
        calls = []

        def fake_apply(root, keys, dry_run, log, use_musicbrainz=True):
            calls.append({"root": root, "keys": keys, "dry_run": dry_run, "use_musicbrainz": use_musicbrainz})
            return {"groups": 1, "folders": 1, "files": 3, "db_paths": 3, "db_artists": 2, "db_tags": 1}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Al Campbell").mkdir()
            (root / "Al  Campbell").mkdir()
            ns = _load_namespace(apply_stub=fake_apply)
            log: List[str] = []
            merged = ns["_auto_merge_case_duplicate_artist_folder"](str(root), "Al Campbell", log)
            self.assertTrue(merged)
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0]["use_musicbrainz"])
            self.assertIn("al campbell", "".join(log).lower())

    def test_does_not_merge_when_no_duplicate_exists(self):
        calls = []

        def fake_apply(root, keys, dry_run, log, use_musicbrainz=True):
            calls.append(1)
            return {"groups": 0, "folders": 0, "files": 0, "db_paths": 0, "db_artists": 0, "db_tags": 0}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Only Artist").mkdir()
            ns = _load_namespace(apply_stub=fake_apply)
            merged = ns["_auto_merge_case_duplicate_artist_folder"](str(root), "Only Artist", [])
            self.assertFalse(merged)
            # No group found for this key, so the (expensive) apply/merge path
            # must never be invoked at all.
            self.assertEqual(calls, [])

    def test_empty_albumartist_is_a_no_op(self):
        ns = _load_namespace(apply_stub=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
        self.assertFalse(ns["_auto_merge_case_duplicate_artist_folder"]("/tmp", "", None))
        self.assertFalse(ns["_auto_merge_case_duplicate_artist_folder"]("/tmp", "   ", None))


if __name__ == "__main__":
    unittest.main()
