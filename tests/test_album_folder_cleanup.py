import ast
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
JOBS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")


def _load_album_cleanup_helpers() -> Dict[str, Any]:
    names = {
        "_ALBUM_FOLDER_UUID_IN_BRACES_RE",
        "_ALBUM_FOLDER_BAD_MBID_SUFFIX_RE",
        "_ALBUM_FOLDER_YEAR_RE",
        "_ALBUM_CLEANUP_LOSSLESS_EXTS",
        "_album_cleanup_safe_component",
        "_album_cleanup_norm_text",
        "_album_cleanup_parse_folder_name",
        "_album_cleanup_canonical_name",
        "_album_cleanup_majority",
        "_album_cleanup_file_inventory",
        "_album_cleanup_embedded_musicbrainz_tags",
        "_album_cleanup_folder_record",
        "_album_cleanup_issue_id",
        "_album_cleanup_file_hash",
        "_album_cleanup_verified_same_file",
        "_album_cleanup_safe_artwork_relative",
        "_album_cleanup_merge_plan",
        "_album_cleanup_count_duplicate_files",
        "_album_cleanup_artwork_to_move",
        "_album_cleanup_source_audio_count",
        "_album_cleanup_valid_rgid",
        "_album_cleanup_canonical_candidates",
        "_album_cleanup_existing_canonical_path",
        "_album_cleanup_safe_reason",
        "_album_cleanup_classification_reason",
        "_album_cleanup_build_issue",
        "_album_cleanup_quality_tuple",
        "_album_cleanup_duplicate_file_choice",
        "_album_cleanup_file_info",
        "_album_cleanup_apply_plan",
        "_album_cleanup_apply_issue",
        "_album_cleanup_trash_path",
        "_album_cleanup_remove_empty_dirs",
        "_album_cleanup_remove_empty_tree",
    }
    namespace: Dict[str, Any] = {
        "Any": Any,
        "Dict": Dict,
        "Iterable": Iterable,
        "List": List,
        "Optional": Optional,
        "Tuple": Tuple,
        "Path": Path,
        "hashlib": __import__("hashlib"),
        "re": re,
        "shutil": __import__("shutil"),
        "uuid": __import__("uuid"),
        "unicodedata": __import__("unicodedata"),
        "_s": lambda value: "" if value is None else str(value),
        "_MB_UUID_RE": re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            re.IGNORECASE,
        ),
        "_LITERAL_PLACEHOLDER_RE": re.compile(
            r"\{(?:Album\s+MbId|Track\s+ArtistMbId)\}",
            re.IGNORECASE,
        ),
        "_UNRESOLVED_TEMPLATE_TOKEN_RE": re.compile(
            r"%\w+\{[^}]+\}|\$(?:disc_subfolder|albumartist|album|artist|title|track|disc|year|mb_[A-Za-z0-9_]+)|\{(?:Album\s+MbId|Track\s+ArtistMbId)\}",
            re.IGNORECASE,
        ),
        "AUDIO_EXT": {".mp3", ".flac", ".m4a"},
        "_ART_EXTS": {".jpg", ".jpeg", ".png", ".webp"},
        "_path_under": lambda path, root: _test_path_under(Path(path), Path(root)),
        "_folder_cleanup_db_items": lambda folder: [],
        "_album_cleanup_update_db_path": lambda old, new, log: 0,
    }
    tree = ast.parse(APP_SOURCE)
    for node in tree.body:
        target_names = set()
        if isinstance(node, ast.Assign):
            target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = {node.target.id}
        elif isinstance(node, ast.FunctionDef):
            target_names = {node.name}
        if target_names & names:
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, "app.py", "exec"), namespace)
    return namespace


def _test_path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def _test_unique_dest(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("")
    suffix = path.suffix
    idx = 1
    while True:
        candidate = Path(f"{base}-{idx}{suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


class AlbumFolderCleanupPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_album_cleanup_helpers()
        cls.helpers["_album_cleanup_trash_path"] = lambda path, trash_root: _test_unique_dest(Path(trash_root) / Path(path).name)

    def test_album_mbid_suffix_parses_as_bad_folder_name(self):
        parse = self.helpers["_album_cleanup_parse_folder_name"]
        parsed = parse("B.O.A.T.S. II #METIME (2013) Album Mbid")
        self.assertEqual(parsed["title"], "B.O.A.T.S. II #METIME")
        self.assertEqual(parsed["year"], "2013")
        self.assertTrue(parsed["has_bad_mbid_suffix"])
        self.assertEqual(parsed["uuid_stamp"], "")

    def test_embedded_musicbrainz_tags_infer_release_group_for_bad_folder(self):
        folder_record = self.helpers["_album_cleanup_folder_record"]
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "12121212-1212-1212-1212-121212121212"
        release_id = "34343434-3434-3434-3434-343434343434"
        old_beets = sys.modules.get("beets")
        old_mediafile = sys.modules.get("beets.mediafile")
        beets_module = types.ModuleType("beets")
        mediafile_module = types.ModuleType("beets.mediafile")

        class FakeMediaFile:
            def __init__(self, path: str):
                self.album = "Tagged Album"
                self.year = 2024
                self.mb_albumid = release_id
                self.mb_releasegroupid = rgid

        mediafile_module.MediaFile = FakeMediaFile
        sys.modules["beets"] = beets_module
        sys.modules["beets.mediafile"] = mediafile_module
        try:
            with tempfile.TemporaryDirectory() as tmp:
                artist_dir = Path(tmp) / "Artist"
                source = artist_dir / "Tagged Album (2024){Album Mbid}"
                source.mkdir(parents=True)
                (source / "01 Song.flac").write_bytes(b"audio")
                record = folder_record(source, {"folders": {}})
                self.assertEqual(record["effective_rgid"], rgid)
                self.assertEqual(record["tag_rgids"], [rgid])
                self.assertEqual(record["tag_release_ids"], [release_id])
                issue = build(artist_dir, [record], ["bad_folder_name"])
                self.assertTrue(issue["safe"])
                self.assertEqual(issue["release_group_id"], rgid)
                self.assertEqual(issue["canonical_folder"], str(artist_dir / f"Tagged Album (2024) {{{rgid}}}"))
                self.assertEqual(issue["risk_reason"], "Safe merge: source resolved to canonical Release Group folder.")
        finally:
            if old_beets is None:
                sys.modules.pop("beets", None)
            else:
                sys.modules["beets"] = old_beets
            if old_mediafile is None:
                sys.modules.pop("beets.mediafile", None)
            else:
                sys.modules["beets.mediafile"] = old_mediafile
    def test_duplicate_album_folder_consolidates_to_release_group_folder(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "511eea39-083a-4741-ae35-5a4d686ca2a6"
        artist_dir = Path("/music/2 Chainz")
        records = [
            {
                "path": str(artist_dir / "B.O.A.T.S. II #METIME (2013)"),
                "name": "B.O.A.T.S. II #METIME (2013)",
                "album": "B.O.A.T.S. II #METIME",
                "year": "2013",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"01 Intro.mp3": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "same"}},
            },
            {
                "path": str(artist_dir / f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}"),
                "name": f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}",
                "album": "B.O.A.T.S. II #METIME",
                "year": "2013",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 Intro.mp3": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "same"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders", "missing_release_group_id_stamp"])
        self.assertTrue(issue["safe"])
        self.assertEqual(issue["safety"], "Safe")
        self.assertEqual(issue["release_group_id"], rgid)
        self.assertEqual(
            issue["canonical_folder"],
            str(artist_dir / f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}"),
        )
        self.assertEqual(issue["duplicate_tracks"], 1)

    def test_same_title_different_release_groups_is_blocked(self):
        build = self.helpers["_album_cleanup_build_issue"]
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2020) {11111111-1111-1111-1111-111111111111}"),
                "name": "Album (2020) {11111111-1111-1111-1111-111111111111}",
                "album": "Album",
                "year": "2020",
                "effective_rgid": "11111111-1111-1111-1111-111111111111",
                "files": {},
            },
            {
                "path": str(artist_dir / "Album (2020) {22222222-2222-2222-2222-222222222222}"),
                "name": "Album (2020) {22222222-2222-2222-2222-222222222222}",
                "album": "Album",
                "year": "2020",
                "effective_rgid": "22222222-2222-2222-2222-222222222222",
                "files": {},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Blocked")
        self.assertIn("different MusicBrainz Release Group IDs", issue["blocking_reasons"])

    def test_source_only_audio_under_same_rgid_is_safe_move(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "e7cf6b26-44f9-3b52-b8f8-bf140d8e85be"
        artist_dir = Path("/music/112")
        records = [
            {
                "path": str(artist_dir / "Hot & Wet (2003)"),
                "name": "Hot & Wet (2003)",
                "album": "Hot & Wet",
                "year": "2003",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {".14.flac": {"is_audio": True, "is_artwork": False, "size": 7, "sha1": "move-me"}},
            },
            {
                "path": str(artist_dir / f"Hot & Wet (2003) {{{rgid}}}"),
                "name": f"Hot & Wet (2003) {{{rgid}}}",
                "album": "Hot & Wet",
                "year": "2003",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"112 - Hot & Wet - 01 - Intro.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "existing"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertTrue(issue["safe"])
        self.assertEqual(issue["safety"], "Safe")
        self.assertEqual(issue["files_to_move"], 1)
        self.assertIn("Safe merge", issue["risk_reason"])

    def test_stale_album_mbid_folder_merges_to_release_group_folder_as_safe(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "33333333-3333-3333-3333-333333333333"
        release_id = "44444444-4444-4444-4444-444444444444"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / f"Album (2024) {{{release_id}}}"),
                "name": f"Album (2024) {{{release_id}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": release_id,
                "release_id_stamp": True,
                "files": {"02 Song.flac": {"is_audio": True, "is_artwork": False, "size": 6, "sha1": "move"}},
            },
            {
                "path": str(artist_dir / f"Album (2024) {{{rgid}}}"),
                "name": f"Album (2024) {{{rgid}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 Intro.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "intro"}},
            },
        ]
        issue = build(artist_dir, records, ["release_id_used_instead_of_release_group_id", "same_release_group_id"])
        self.assertTrue(issue["safe"])
        self.assertEqual(issue["safety"], "Safe")
        self.assertEqual(issue["canonical_folder"], str(artist_dir / f"Album (2024) {{{rgid}}}"))

    def test_missing_source_rgid_with_obvious_canonical_target_is_safe(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "350f8f0c-4dc7-458b-b6af-779ef280c2c4"
        artist_dir = Path("/music/2 Chainz")
        records = [
            {
                "path": str(artist_dir / "B.O.A.T.S. II #METIME (2013)"),
                "name": "B.O.A.T.S. II #METIME (2013)",
                "album": "B.O.A.T.S. II #METIME",
                "year": "2013",
                "effective_rgid": "",
                "folder_uuid": "",
                "files": {"02 Song.flac": {"is_audio": True, "is_artwork": False, "size": 6, "sha1": "move"}},
            },
            {
                "path": str(artist_dir / f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}"),
                "name": f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}",
                "album": "B.O.A.T.S. II #METIME",
                "year": "2013",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 Intro.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "intro"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertTrue(issue["safe"])
        self.assertEqual(issue["safety"], "Safe")
        self.assertEqual(issue["release_group_id"], rgid)
        self.assertEqual(issue["release_group_inference"], "missing_rgid_from_target")
        self.assertEqual(issue["risk_reason"], "Safe merge: missing RGID inferred from target folder.")
        self.assertEqual(issue["classification_reason"], issue["risk_reason"])

    def test_placeholder_folder_with_resolved_file_tags_is_safe(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "55555555-5555-5555-5555-555555555555"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2024){Album Mbid}"),
                "name": "Album (2024){Album Mbid}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "has_literal_placeholder": True,
                "files": {"02 Song.flac": {"is_audio": True, "is_artwork": False, "size": 6, "sha1": "move"}},
            },
            {
                "path": str(artist_dir / f"Album (2024) {{{rgid}}}"),
                "name": f"Album (2024) {{{rgid}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 Intro.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "intro"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders", "bad_folder_name"])
        self.assertTrue(issue["safe"])
        self.assertEqual(issue["risk_reason"], "Safe merge: source resolved to canonical Release Group folder.")

    def test_normalized_album_match_with_canonical_target_is_safe(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "66666666-6666-6666-6666-666666666666"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "BOATS II METIME (2013)"),
                "name": "BOATS II METIME (2013)",
                "album": "BOATS II METIME",
                "year": "2013",
                "effective_rgid": "",
                "folder_uuid": "",
                "files": {"02 Song.flac": {"is_audio": True, "is_artwork": False, "size": 6, "sha1": "move"}},
            },
            {
                "path": str(artist_dir / f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}"),
                "name": f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}",
                "album": "B.O.A.T.S. II #METIME",
                "year": "2013",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 Intro.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "intro"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertTrue(issue["safe"])
        self.assertEqual(issue["canonical_folder"], str(artist_dir / f"B.O.A.T.S. II #METIME (2013) {{{rgid}}}"))
        self.assertEqual(issue["risk_reason"], "Safe merge: missing RGID inferred from target folder.")

    def test_multiple_possible_canonical_targets_need_review(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid_a = "77777777-7777-7777-7777-777777777777"
        rgid_b = "88888888-8888-8888-8888-888888888888"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2024)"),
                "name": "Album (2024)",
                "album": "Album",
                "year": "2024",
                "effective_rgid": "",
                "folder_uuid": "",
                "files": {},
            },
            {
                "path": str(artist_dir / f"Album (2024) {{{rgid_a}}}"),
                "name": f"Album (2024) {{{rgid_a}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid_a,
                "folder_uuid": rgid_a,
                "files": {},
            },
            {
                "path": str(artist_dir / f"Album (2024) {{{rgid_b}}}"),
                "name": f"Album (2024) {{{rgid_b}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid_b,
                "folder_uuid": rgid_b,
                "files": {},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Needs review")
        self.assertEqual(issue["risk_reason"], "Needs review: multiple possible target folders.")

    def test_target_audio_differs_is_blocked_in_classification(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "99999999-9999-9999-9999-999999999999"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2024)"),
                "name": "Album (2024)",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"01 Song.flac": {"is_audio": True, "is_artwork": False, "size": 6, "sha1": "source"}},
            },
            {
                "path": str(artist_dir / f"Album (2024) {{{rgid}}}"),
                "name": f"Album (2024) {{{rgid}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 Song.flac": {"is_audio": True, "is_artwork": False, "size": 7, "sha1": "target"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Blocked")
        self.assertEqual(issue["risk_reason"], "Blocked: target file exists but audio differs.")

    def test_unknown_leftover_files_block_classification(self):
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2024)"),
                "name": "Album (2024)",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"notes.txt": {"is_audio": False, "is_artwork": False, "size": 4, "sha1": "notes"}},
            },
            {
                "path": str(artist_dir / f"Album (2024) {{{rgid}}}"),
                "name": f"Album (2024) {{{rgid}}}",
                "album": "Album",
                "year": "2024",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Blocked")
        self.assertEqual(issue["risk_reason"], "Blocked: unknown leftover files.")

    def test_auto_fix_applies_inferred_safe_merge_and_removes_source_folder(self):
        build = self.helpers["_album_cleanup_build_issue"]
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        rgid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            artist_dir = root / "Artist"
            source = artist_dir / "Album (2024)"
            target = artist_dir / f"Album (2024) {{{rgid}}}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            source_file = source / "02 Song.flac"
            source_file.write_bytes(b"move-me")
            records = [
                {
                    "path": str(source),
                    "name": source.name,
                    "album": "Album",
                    "year": "2024",
                    "effective_rgid": "",
                    "folder_uuid": "",
                    "files": {"02 Song.flac": {"path": str(source_file), "is_audio": True, "is_artwork": False, "size": 7, "sha1": "move"}},
                },
                {
                    "path": str(target),
                    "name": target.name,
                    "album": "Album",
                    "year": "2024",
                    "effective_rgid": rgid,
                    "folder_uuid": rgid,
                    "files": {},
                },
            ]
            issue = build(artist_dir, records, ["duplicate_album_folders"])
            self.assertTrue(issue["safe"])
            result = apply_issue(issue, root, Path(tmp) / "trash", [], self._summary(), [])
            self.assertEqual(result["status"], "Completed")
            self.assertTrue((target / "02 Song.flac").exists())
            self.assertFalse(source.exists())
    def test_duplicate_file_choice_prefers_lossless_then_existing_tie(self):
        choose = self.helpers["_album_cleanup_duplicate_file_choice"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate.flac"
            existing = root / "existing.mp3"
            candidate.write_bytes(b"small")
            existing.write_bytes(b"x" * 5000)
            self.assertEqual(choose(candidate, existing), "candidate")

            equal_candidate = root / "candidate2.mp3"
            equal_existing = root / "existing2.mp3"
            equal_candidate.write_bytes(b"same")
            equal_existing.write_bytes(b"same")
            self.assertEqual(choose(equal_candidate, equal_existing), "existing")

    def test_empty_folder_tree_removes_nested_empty_dirs(self):
        remove_tree = self.helpers["_album_cleanup_remove_empty_tree"]
        with tempfile.TemporaryDirectory() as tmp:
            album = Path(tmp) / "Artist" / "Album (2024)"
            (album / "disc 1" / "art").mkdir(parents=True)
            log: List[str] = []
            removed = remove_tree(album, log)
            self.assertGreaterEqual(removed, 3)
            self.assertFalse(album.exists())
            self.assertTrue(any("Removed empty album folder" in line for line in log))

    def _summary(self) -> Dict[str, int]:
        return {
            "files_moved": 0,
            "artwork_moved": 0,
            "duplicate_files_quarantined": 0,
            "folders_deleted": 0,
            "db_paths_updated": 0,
            "errors": 0,
            "blocked": 0,
            "completed": 0,
        }

    def _issue(self, source: Path, target: Path, safety: str = "Safe") -> Dict[str, Any]:
        return {
            "id": "issue-1",
            "artist": source.parent.name,
            "album": "Album",
            "release_group_id": "11111111-1111-1111-1111-111111111111",
            "current_folders": [str(source), str(target)],
            "canonical_folder": str(target),
            "proposed_canonical_folder": str(target),
            "issue_types": ["duplicate_album_folders", "same_release_group_id"],
            "proposed_action": "Merge duplicate album folders into the canonical Release Group ID folder",
            "safety": safety,
            "safe": safety == "Safe",
        }

    def test_approved_merge_moves_audio_into_canonical_folder(self):
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "02 Song.flac").write_bytes(b"audio-2")
            log: List[str] = []
            operations: List[Dict[str, Any]] = []
            result = apply_issue(self._issue(source, target), root, Path(tmp) / "trash", log, self._summary(), operations)
            self.assertEqual(result["status"], "Completed")
            self.assertTrue((target / "02 Song.flac").exists())
            self.assertFalse(source.exists())

    def test_verified_duplicate_track_is_quarantined_during_auto_fix(self):
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "01 Intro.flac").write_bytes(b"same-audio")
            (target / "01 Intro.flac").write_bytes(b"same-audio")
            summary = self._summary()
            result = apply_issue(self._issue(source, target), root, Path(tmp) / "trash", [], summary, [])
            self.assertEqual(result["status"], "Completed")
            self.assertFalse((source / "01 Intro.flac").exists())
            self.assertTrue((Path(tmp) / "trash" / "01 Intro.flac").exists())
            self.assertEqual(summary["duplicate_files_quarantined"], 1)

    def test_target_track_with_different_audio_blocks_that_file(self):
        apply_plan = self.helpers["_album_cleanup_apply_plan"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "01 Intro.flac").write_bytes(b"source")
            (target / "01 Intro.flac").write_bytes(b"target")
            plan = apply_plan(self._issue(source, target), root)
            self.assertFalse(plan["safe"])
            self.assertIn("target file exists but differs", plan["blockers"][0])

    def test_album_art_moves_into_canonical_folder(self):
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "cover.jpg").write_bytes(b"art")
            summary = self._summary()
            result = apply_issue(self._issue(source, target), root, Path(tmp) / "trash", [], summary, [])
            self.assertEqual(result["status"], "Completed")
            self.assertTrue((target / "cover.jpg").exists())
            self.assertEqual(summary["artwork_moved"], 1)

    def test_different_artwork_conflict_uses_safe_renamed_target(self):
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "cover.jpg").write_bytes(b"new-art")
            (target / "cover.jpg").write_bytes(b"old-art")
            result = apply_issue(self._issue(source, target), root, Path(tmp) / "trash", [], self._summary(), [])
            self.assertEqual(result["status"], "Completed")
            self.assertTrue((target / "cover-1.jpg").exists())
            self.assertEqual((target / "cover.jpg").read_bytes(), b"old-art")

    def test_unknown_leftover_files_block_folder_removal(self):
        apply_plan = self.helpers["_album_cleanup_apply_plan"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "notes.txt").write_text("keep me", encoding="utf-8")
            plan = apply_plan(self._issue(source, target), root)
            self.assertFalse(plan["safe"])
            self.assertIn("unknown leftover files", plan["blockers"][0])

    def test_manually_approved_needs_review_item_becomes_completed(self):
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            source = root / "Artist" / "Album (2024)"
            target = root / "Artist" / "Album (2024) {11111111-1111-1111-1111-111111111111}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "03 Manual.flac").write_bytes(b"manual")
            result = apply_issue(self._issue(source, target, "Needs review"), root, Path(tmp) / "trash", [], self._summary(), [])
            self.assertEqual(result["status"], "Completed")
            self.assertTrue((target / "03 Manual.flac").exists())


    def test_same_rgid_title_differs_by_article_is_safe(self):
        """RGID-confirmed identity: 'The Dark Side' vs 'Dark Side' is metadata noise, not a blocker."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        artist_dir = Path("/music/Pink Floyd")
        records = [
            {
                "path": str(artist_dir / "Dark Side of the Moon (1973)"),
                "name": "Dark Side of the Moon (1973)",
                "album": "Dark Side of the Moon",
                "year": "1973",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"01 Speak to Me.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "a1"}},
            },
            {
                "path": str(artist_dir / f"The Dark Side of the Moon (1973) {{{rgid}}}"),
                "name": f"The Dark Side of the Moon (1973) {{{rgid}}}",
                "album": "The Dark Side of the Moon",
                "year": "1973",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"02 Breathe.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "a2"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders", "missing_release_group_id_stamp"])
        self.assertTrue(issue["safe"], f"Expected Safe but got {issue['safety']!r}: {issue['risk_reason']}")
        self.assertEqual(issue["safety"], "Safe")
        self.assertEqual(issue["release_group_id"], rgid)
        self.assertEqual(issue["canonical_folder"], str(artist_dir / f"The Dark Side of the Moon (1973) {{{rgid}}}"))

    def test_same_rgid_two_stale_sources_plus_canonical_all_safe(self):
        """Three folders sharing one RGID — two without stamps, one canonical — should all be Safe."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2020)"),
                "name": "Album (2020)",
                "album": "Album",
                "year": "2020",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"01 Track.flac": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "t1"}},
            },
            {
                "path": str(artist_dir / "Album [2020]"),
                "name": "Album [2020]",
                "album": "Album",
                "year": "2020",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"02 Track.flac": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "t2"}},
            },
            {
                "path": str(artist_dir / f"Album (2020) {{{rgid}}}"),
                "name": f"Album (2020) {{{rgid}}}",
                "album": "Album",
                "year": "2020",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders", "missing_release_group_id_stamp"])
        self.assertTrue(issue["safe"], f"Expected Safe but got {issue['safety']!r}: {issue['risk_reason']}")
        self.assertEqual(issue["files_to_move"], 2)
        self.assertEqual(issue["canonical_folder"], str(artist_dir / f"Album (2020) {{{rgid}}}"))

    def test_release_id_stamp_alone_no_db_rgid_needs_review(self):
        """Single folder with stale release-ID stamp and no DB RGID cannot map to canonical — Needs review."""
        build = self.helpers["_album_cleanup_build_issue"]
        release_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        artist_dir = Path("/music/Artist")
        record = {
            "path": str(artist_dir / f"Album (2024) {{{release_id}}}"),
            "name": f"Album (2024) {{{release_id}}}",
            "album": "Album",
            "year": "2024",
            "effective_rgid": "",
            "folder_uuid": release_id,
            "release_id_stamp": True,
            "db_rgids": [],
            "tag_rgids": [],
            "files": {"01 Song.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "s1"}},
        }
        issue = build(artist_dir, [record], ["release_id_used_instead_of_release_group_id"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Needs review")
        self.assertIn("Release Group ID cannot be inferred", issue["blocking_reasons"])

    def test_tag_rgid_conflict_in_source_causes_needs_review(self):
        """Source folder whose audio files carry mixed RGIDs is Needs review even when canonical is clear."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "Album (2022)"),
                "name": "Album (2022)",
                "album": "Album",
                "year": "2022",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "tag_rgid_conflict": True,
                "files": {"01 Mixed.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "m1"}},
            },
            {
                "path": str(artist_dir / f"Album (2022) {{{rgid}}}"),
                "name": f"Album (2022) {{{rgid}}}",
                "album": "Album",
                "year": "2022",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Needs review")
        self.assertIn("multiple embedded Release Group IDs", issue["blocking_reasons"])

    def test_source_outside_artist_dir_is_blocked(self):
        """Source folder whose parent differs from artist_dir is Blocked (path safety)."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "01010101-0101-0101-0101-010101010101"
        artist_dir = Path("/music/CorrectArtist")
        records = [
            {
                "path": "/music/DifferentArtist/Album (2020)",
                "name": "Album (2020)",
                "album": "Album",
                "year": "2020",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"01 Track.flac": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "x1"}},
            },
            {
                "path": str(artist_dir / f"Album (2020) {{{rgid}}}"),
                "name": f"Album (2020) {{{rgid}}}",
                "album": "Album",
                "year": "2020",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertFalse(issue["safe"])
        self.assertEqual(issue["safety"], "Blocked")
        self.assertIn("source folder is outside the artist folder", issue["blocking_reasons"])

    def test_needs_review_reason_text_for_unresolvable_rgid(self):
        """risk_reason contains the human-readable 'Needs review:' message for RGID inference failure."""
        build = self.helpers["_album_cleanup_build_issue"]
        artist_dir = Path("/music/Artist")
        record = {
            "path": str(artist_dir / "Unknown Album (2023)"),
            "name": "Unknown Album (2023)",
            "album": "Unknown Album",
            "year": "2023",
            "effective_rgid": "",
            "folder_uuid": "",
            "files": {"01 Song.flac": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "u1"}},
        }
        issue = build(artist_dir, [record], ["missing_release_group_id_stamp"])
        self.assertEqual(issue["safety"], "Needs review")
        self.assertTrue(
            issue["risk_reason"].startswith("Needs review:"),
            f"Expected 'Needs review:' prefix but got: {issue['risk_reason']!r}",
        )

    def test_safe_reason_when_rgid_confirmed_but_titles_differ(self):
        """risk_reason is a 'Safe merge:' message when RGID is confirmed despite title metadata mismatch."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "23232323-2323-2323-2323-232323232323"
        artist_dir = Path("/music/Artist")
        records = [
            {
                "path": str(artist_dir / "My Album (2019)"),
                "name": "My Album (2019)",
                "album": "My Album",
                "year": "2019",
                "effective_rgid": rgid,
                "folder_uuid": "",
                "files": {"02 B.flac": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "b"}},
            },
            {
                "path": str(artist_dir / f"The My Album (2019) {{{rgid}}}"),
                "name": f"The My Album (2019) {{{rgid}}}",
                "album": "The My Album",
                "year": "2019",
                "effective_rgid": rgid,
                "folder_uuid": rgid,
                "files": {"01 A.flac": {"is_audio": True, "is_artwork": False, "size": 4, "sha1": "a"}},
            },
        ]
        issue = build(artist_dir, records, ["duplicate_album_folders"])
        self.assertTrue(issue["safe"])
        self.assertTrue(
            issue["risk_reason"].startswith("Safe merge:"),
            f"Expected 'Safe merge:' prefix but got: {issue['risk_reason']!r}",
        )

    def test_single_folder_db_rgid_no_stamp_is_safe_rename(self):
        """Single folder with RGID from Beets DB but no folder-name stamp classifies as Safe (rename action)."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "45454545-4545-4545-4545-454545454545"
        artist_dir = Path("/music/Artist")
        record = {
            "path": str(artist_dir / "My Album (2019)"),
            "name": "My Album (2019)",
            "album": "My Album",
            "year": "2019",
            "effective_rgid": rgid,
            "folder_uuid": "",
            "db_rgids": [rgid],
            "files": {"01 Track.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "t1"}},
        }
        issue = build(artist_dir, [record], ["missing_release_group_id_stamp"])
        self.assertTrue(issue["safe"], f"Expected Safe but got {issue['safety']!r}: {issue['risk_reason']}")
        self.assertEqual(issue["release_group_id"], rgid)
        self.assertEqual(issue["canonical_folder"], str(artist_dir / f"My Album (2019) {{{rgid}}}"))

    def test_stale_release_id_with_db_rgid_single_folder_is_safe(self):
        """Single folder with release_id_stamp=True but DB supplies the RGID → Safe (the stamp can be fixed)."""
        build = self.helpers["_album_cleanup_build_issue"]
        rgid = "56565656-5656-5656-5656-565656565656"
        release_id = "78787878-7878-7878-7878-787878787878"
        artist_dir = Path("/music/Artist")
        record = {
            "path": str(artist_dir / f"Album (2021) {{{release_id}}}"),
            "name": f"Album (2021) {{{release_id}}}",
            "album": "Album",
            "year": "2021",
            "effective_rgid": rgid,
            "folder_uuid": release_id,
            "release_id_stamp": True,
            "db_rgids": [rgid],
            "files": {"01 Song.flac": {"is_audio": True, "is_artwork": False, "size": 5, "sha1": "s2"}},
        }
        issue = build(artist_dir, [record], ["release_id_used_instead_of_release_group_id"])
        self.assertTrue(issue["safe"], f"Expected Safe but got {issue['safety']!r}: {issue['risk_reason']}")
        self.assertEqual(issue["canonical_folder"], str(artist_dir / f"Album (2021) {{{rgid}}}"))
        self.assertIn("stale Album MBID", issue["risk_reason"])

    def test_auto_fix_same_rgid_title_mismatch_merges_correctly(self):
        """Full apply_issue: two folders with same RGID but different title metadata merge to canonical."""
        build = self.helpers["_album_cleanup_build_issue"]
        apply_issue = self.helpers["_album_cleanup_apply_issue"]
        rgid = "89898989-8989-8989-8989-898989898989"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            artist_dir = root / "Pink Floyd"
            source = artist_dir / "Dark Side of the Moon (1973)"
            target = artist_dir / f"The Dark Side of the Moon (1973) {{{rgid}}}"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            source_file = source / "01 Speak.flac"
            source_file.write_bytes(b"audio-data")
            records = [
                {
                    "path": str(source),
                    "name": source.name,
                    "album": "Dark Side of the Moon",
                    "year": "1973",
                    "effective_rgid": rgid,
                    "folder_uuid": "",
                    "files": {"01 Speak.flac": {"path": str(source_file), "is_audio": True, "is_artwork": False, "size": 10, "sha1": "x"}},
                },
                {
                    "path": str(target),
                    "name": target.name,
                    "album": "The Dark Side of the Moon",
                    "year": "1973",
                    "effective_rgid": rgid,
                    "folder_uuid": rgid,
                    "files": {},
                },
            ]
            issue = build(artist_dir, records, ["duplicate_album_folders", "missing_release_group_id_stamp"])
            self.assertTrue(issue["safe"], f"Expected Safe but got {issue['safety']!r}: {issue['risk_reason']}")
            result = apply_issue(issue, root, Path(tmp) / "trash", [], self._summary(), [])
            self.assertEqual(result["status"], "Completed")
            self.assertTrue((target / "01 Speak.flac").exists())
            self.assertFalse(source.exists())


class AlbumFolderCleanupRouteAndUiTests(unittest.TestCase):
    def test_job_backed_album_folder_routes_exist(self):
        self.assertIn('@app.post("/api/clean/album-folders/scan")', APP_SOURCE)
        self.assertIn('@app.post("/api/clean/album-folders/apply-safe")', APP_SOURCE)
        self.assertIn('@app.post("/api/clean/album-folders/apply-issue")', APP_SOURCE)
        self.assertIn('@app.get("/api/clean/album-folders/report")', APP_SOURCE)
        self.assertIn('"album-folder-cleanup-scan"', APP_SOURCE)
        self.assertIn('"album-folder-cleanup-apply-safe"', APP_SOURCE)
        self.assertIn('"album-folder-cleanup-apply-issue"', APP_SOURCE)
        self.assertIn("_ALBUM_FOLDER_CLEANUP_LOCK", APP_SOURCE)
        self.assertIn("album-folder-cleanup-trash", APP_SOURCE)
        self.assertIn("issues = [issue for issue in plan.get(\"issues\", []) if issue.get(\"safe\")]", APP_SOURCE)

    def test_jobs_page_uses_compact_album_cleanup_actions(self):
        self.assertIn("function JobStatusBar", JOBS_SOURCE)
        self.assertIn("Clean All", JOBS_SOURCE)
        self.assertIn("Refresh Folder Report", JOBS_SOURCE)
        self.assertIn("Auto-Fix Safe Issues", JOBS_SOURCE)
        self.assertIn("Review Issues", JOBS_SOURCE)
        self.assertIn("Retry Failed Jobs", JOBS_SOURCE)
        self.assertIn("Cleanup Report", JOBS_SOURCE)
        self.assertIn("applySafeAlbumFolderCleanup", JOBS_SOURCE)
        self.assertIn("applyAlbumFolderCleanupIssue", JOBS_SOURCE)
        self.assertIn("getAlbumFolderCleanupReport", JOBS_SOURCE)
        self.assertNotIn("Open log", JOBS_SOURCE)
        self.assertNotIn("View Log", JOBS_SOURCE)

    def test_cleanup_issue_rows_show_preview_fix_skip_actions(self):
        self.assertIn("Preview</Button>", JOBS_SOURCE)
        self.assertIn("Fix", JOBS_SOURCE)
        self.assertIn("Skip", JOBS_SOURCE)
        self.assertIn("Ignore", JOBS_SOURCE)

    def test_duplicate_album_folder_preview_shows_source_and_target(self):
        self.assertIn("Current/source folder", JOBS_SOURCE)
        self.assertIn("Canonical target folder", JOBS_SOURCE)
        self.assertIn("Release Group ID", JOBS_SOURCE)
        self.assertIn("Duplicate files to quarantine", JOBS_SOURCE)
        self.assertIn("Final expected folder layout", JOBS_SOURCE)

    def test_blocked_issue_shows_real_reason(self):
        self.assertIn("issueReason(issue)", JOBS_SOURCE)
        self.assertIn("blocking_reasons", JOBS_SOURCE)
        self.assertIn("Blocked: target file exists but audio differs.", APP_SOURCE)

    def test_needs_review_count_drops_after_safe_merge_classification(self):
        self.assertNotIn("source audio file(s) are not proven duplicates", APP_SOURCE)
        self.assertIn("Safe merge: same album identity, no file conflicts.", APP_SOURCE)

    def test_advanced_maintenance_tools_are_collapsed_by_default(self):
        self.assertIn("function AdvancedMaintenanceSection", JOBS_SOURCE)
        self.assertIn("const [open, setOpen] = useState(false)", JOBS_SOURCE)
        self.assertIn("<MaintenanceRunnerBar jobs={jobs}", JOBS_SOURCE)

    def test_zero_result_panels_are_hidden_or_collapsed_after_scan(self):
        self.assertIn("].filter((stat) => stat.value > 0)", JOBS_SOURCE)
        self.assertIn("<summary className=\"cursor-pointer px-3 py-2 text-xs font-semibold text-zinc-400\">Cleanup details</summary>", JOBS_SOURCE)

    def test_grouped_jobs_page_still_exposes_existing_maintenance_actions(self):
        for text in [
            "Library Cleanup",
            "Metadata & Artwork",
            "Database Health",
            "Advanced Maintenance",
            "Folder Name Issues",
            "Leaked DB Paths",
            "Artist Alias Repair",
            "Album Track Repair",
            "Advanced Library Move",
        ]:
            self.assertIn(text, JOBS_SOURCE)

    def test_job_history_remains_accessible_but_compact(self):
        self.assertIn("Job History", JOBS_SOURCE)
        self.assertIn("Show more history", JOBS_SOURCE)
        self.assertIn("View raw log", JOBS_SOURCE)
        self.assertIn("h-[14rem]", JOBS_SOURCE)

    def test_api_client_has_album_folder_cleanup_functions(self):
        self.assertIn("scanAlbumFolders", CLIENT_SOURCE)
        self.assertIn("/api/clean/album-folders/scan", CLIENT_SOURCE)
        self.assertIn("applySafeAlbumFolderCleanup", CLIENT_SOURCE)
        self.assertIn("/api/clean/album-folders/apply-safe", CLIENT_SOURCE)
        self.assertIn("applyAlbumFolderCleanupIssue", CLIENT_SOURCE)
        self.assertIn("/api/clean/album-folders/apply-issue", CLIENT_SOURCE)
        self.assertIn("getAlbumFolderCleanupReport", CLIENT_SOURCE)
        self.assertIn("/api/clean/album-folders/report", CLIENT_SOURCE)


if __name__ == "__main__":
    unittest.main()



