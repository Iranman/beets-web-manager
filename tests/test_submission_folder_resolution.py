import ast
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = ROOT / "routes_submissions.py"


class _FakeLib:
    """Stand-in for the Beets Library used by routes_submissions.py. Tests
    control exactly which albums/items (if any) are "already imported" so the
    folder-resolution state machine can be exercised without a real library."""

    def __init__(self, albums=None, items=None):
        self._albums = albums or []
        self._items = items or []

    def albums(self):
        return self._albums

    def items(self):
        return self._items


class _FakeAlbum:
    def __init__(self, album_id: int, path: str):
        self.id = album_id
        self._path = path

    def item_dir(self):
        return self._path


class _FakeItem:
    def __init__(self, item_id: int, path: str, album_id: int = 0):
        self.id = item_id
        self.path = path
        self.album_id = album_id
        self.title = "Stub Title"
        self.artist = "Stub Artist"
        self.disc = 1
        self.track = 1
        self.length = 180


def _stub_build_folder_evidence(folder_path: str) -> Dict[str, Any]:
    folder = Path(folder_path)
    audio = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp3", ".flac"}) if folder.is_dir() else []
    return {
        "folder_path": str(folder_path),
        "audio_files": [str(p) for p in audio],
        "folder_track_count": len(audio),
        "nested_audio_count": 0,
        "guessed_artist": "",
        "guessed_artist_mbid": "",
        "guessed_album": "",
        "guessed_year": "",
        "track_titles": [],
        "track_lines": [],
        "filenames": [p.name for p in audio],
    }


def _load_namespace(lib: _FakeLib):
    tree = ast.parse(ROUTES_SOURCE.read_text(encoding="utf-8"))
    names = {
        "_abs_resolved",
        "_find_beets_album_for_folder",
        "_find_beets_items_for_folder",
        "_media_tag_track_payload",
        "_folder_cover_art_url",
        "_empty_folder_summary",
        "_summary_for_folder_tracks",
        "_folder_audio_file_listing",
        "_resolve_folder_submission_target",
        "_duration_label",
        "_item_abs_path",
        "_reference_url_source",
        "_yt_normalize_title",
        "_yt_split_artist_title",
        "_yt_channel_is_topic",
        "_yt_channel_looks_like_label",
        "_YT_BRACKET_NOISE_RE",
        "_YT_BARE_REMASTER_RE",
        "_YT_TOPIC_SUFFIX_RE",
        "_YT_PROVIDED_TO_RE",
        "_YT_LABEL_CHANNEL_RE",
        "_YT_HOSTS",
        "_MB_HOSTS",
        "_DISCOGS_HOSTS",
        "_SOUNDCLOUD_HOSTS",
        "_DISCOGS_RELEASE_ID_RE",
        "_DISCOGS_MASTER_ID_RE",
        "_discogs_release_id_from_url",
    }
    ns: Dict[str, Any] = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Tuple": Tuple,
        "Path": Path,
        "re": re,
        "urlparse": urlparse,
        "lib": lib,
        "AUDIO_EXT": frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}),
        "_s": lambda value: (value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")),
        "_build_folder_evidence": _stub_build_folder_evidence,
        "_path_is_under": lambda path, root: True,
        "_SUBMISSION_ALLOWED_ROOTS": (Path("/data/media/music"), Path("/data/torrents/music")),
        "_summary_for_album": lambda album, tracks: {"target_type": "album", "album_id": album.id, "resolved_state": "imported_album", "title": "", "track_count": len(tracks)},
        "_album_track_rows": lambda album: [],
        "_track_payload": lambda item, idx: {
            "index": idx, "item_id": item.id, "album_id": item.album_id, "disc": item.disc, "track": item.track,
            "title": item.title, "artist": item.artist, "album": "", "albumartist": "", "duration": item.length,
            "duration_display": "", "file_name": Path(item.path).name, "file_path": item.path,
            "file_available": True, "format": "", "mb_trackid": "", "mb_albumid": "",
            "fingerprint_status": "Missing recording MBID", "validation_status": "Ready",
        },
    }
    for node in tree.body:
        node_name = ""
        if isinstance(node, ast.Assign):
            node_name = getattr(node.targets[0], "id", "")
        elif isinstance(node, ast.FunctionDef):
            node_name = node.name
        if node_name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, str(ROUTES_SOURCE), "exec"), ns)
    return ns


class FolderResolutionStateTests(unittest.TestCase):
    def test_inaccessible_path_is_detected(self):
        ns = _load_namespace(_FakeLib())
        target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"]("/definitely/not/a/real/path")
        self.assertEqual(target_type, "folder")
        self.assertEqual(summary["resolved_state"], "inaccessible")
        self.assertEqual(tracks, [])

    def test_empty_folder_is_detected_not_zero_tracks_masquerading_as_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = _load_namespace(_FakeLib())
            target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"](tmp)
            self.assertEqual(target_type, "folder")
            self.assertEqual(summary["resolved_state"], "empty")
            self.assertEqual(summary["track_count"], 0)
            self.assertEqual(tracks, [])

    def test_unimported_folder_with_audio_files_resolves_real_track_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "01 - Track One.mp3").write_bytes(b"not real audio data")
            (folder / "02 - Track Two.mp3").write_bytes(b"not real audio data")
            ns = _load_namespace(_FakeLib())
            target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"](str(folder))
            self.assertEqual(target_type, "folder")
            self.assertEqual(summary["resolved_state"], "unimported_album")
            # This is the core reported bug: a folder with real audio files must
            # never report zero tracks.
            self.assertEqual(len(tracks), 2)
            self.assertEqual(summary["track_count"], 2)

    def test_folder_matching_an_existing_beets_album_delegates_to_album_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "01 - Track One.mp3").write_bytes(b"not real audio data")
            album = _FakeAlbum(album_id=42, path=str(folder))
            ns = _load_namespace(_FakeLib(albums=[album]))
            target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"](str(folder))
            self.assertEqual(target_type, "album")
            self.assertEqual(target_ref, 42)
            self.assertEqual(summary["resolved_state"], "imported_album")

    def test_folder_matching_existing_singleton_items_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            f1 = folder / "01 - Track One.mp3"
            f1.write_bytes(b"not real audio data")
            item = _FakeItem(item_id=7, path=str(f1), album_id=0)
            ns = _load_namespace(_FakeLib(items=[item]))
            target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"](str(folder))
            self.assertEqual(target_type, "folder")
            self.assertEqual(summary["resolved_state"], "imported_singletons")
            self.assertEqual(len(tracks), 1)

    def test_single_loose_audio_file_path_resolves_as_loose_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            f1 = folder / "Loose Track.mp3"
            f1.write_bytes(b"not real audio data")
            ns = _load_namespace(_FakeLib())
            target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"](str(f1))
            self.assertEqual(summary["resolved_state"], "loose_tracks")
            self.assertEqual(len(tracks), 1)

    def test_non_audio_file_path_is_inaccessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            f1 = folder / "notes.txt"
            f1.write_text("hello")
            ns = _load_namespace(_FakeLib())
            target_type, target_ref, summary, tracks = ns["_resolve_folder_submission_target"](str(f1))
            self.assertEqual(summary["resolved_state"], "inaccessible")
            self.assertEqual(tracks, [])


class YoutubeTitleNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_namespace(_FakeLib())

    def test_strips_official_video_and_hd_noise(self):
        result = self.ns["_yt_normalize_title"]("Artist - Track Name (Official Video) [HD]")
        self.assertEqual(result, "Artist - Track Name")

    def test_preserves_remix_and_live_version_markers(self):
        self.assertIn("Remix", self.ns["_yt_normalize_title"]("Artist - Track Name (Extended Remix)"))
        self.assertIn("Live", self.ns["_yt_normalize_title"]("Artist - Track Name (Live)"))

    def test_bare_remastered_is_stripped_but_remaster_with_year_is_kept(self):
        self.assertNotIn("Remastered", self.ns["_yt_normalize_title"]("Artist - Track Name Remastered"))
        self.assertIn("2011", self.ns["_yt_normalize_title"]("Artist - Track Name (2011 Remaster)"))

    def test_splits_artist_and_title_on_dash(self):
        artist, title = self.ns["_yt_split_artist_title"]("40 Cal. - Capone (Official Audio)")
        self.assertEqual(artist, "40 Cal.")
        self.assertEqual(title, "Capone")

    def test_topic_channel_suffix_detected(self):
        self.assertTrue(self.ns["_yt_channel_is_topic"]("Chubs & Rob Viktum - Topic"))
        self.assertFalse(self.ns["_yt_channel_is_topic"]("Chubs & Rob Viktum"))

    def test_label_channel_is_flagged_low_confidence_source(self):
        self.assertTrue(self.ns["_yt_channel_looks_like_label"]("Some Records"))
        self.assertTrue(self.ns["_yt_channel_looks_like_label"]("Provided to YouTube by Sony Music"))
        self.assertFalse(self.ns["_yt_channel_looks_like_label"]("Chubs & Rob Viktum"))


class ReferenceUrlHostRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_namespace(_FakeLib())

    def test_youtube_hosts_route_to_youtube_adapter(self):
        for host in ("youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com"):
            self.assertEqual(self.ns["_reference_url_source"](host), "youtube")

    def test_musicbrainz_host_routes_to_musicbrainz_adapter(self):
        self.assertEqual(self.ns["_reference_url_source"]("musicbrainz.org"), "musicbrainz")

    def test_bandcamp_subdomain_routes_to_bandcamp_adapter(self):
        self.assertEqual(self.ns["_reference_url_source"]("someartist.bandcamp.com"), "bandcamp")

    def test_unknown_host_falls_back_to_generic_web_adapter(self):
        self.assertEqual(self.ns["_reference_url_source"]("example.com"), "web")


class DiscogsUrlParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_namespace(_FakeLib())

    def test_release_url_extracts_numeric_id(self):
        entity_type, entity_id = self.ns["_discogs_release_id_from_url"]("https://www.discogs.com/release/249504-Chubs-Rob-Viktum")
        self.assertEqual(entity_type, "release")
        self.assertEqual(entity_id, "249504")

    def test_master_url_extracts_numeric_id(self):
        entity_type, entity_id = self.ns["_discogs_release_id_from_url"]("https://www.discogs.com/master/12345-Some-Album")
        self.assertEqual(entity_type, "master")
        self.assertEqual(entity_id, "12345")

    def test_unparseable_discogs_url_returns_empty(self):
        entity_type, entity_id = self.ns["_discogs_release_id_from_url"]("https://www.discogs.com/artist/12345-Some-Artist")
        self.assertEqual((entity_type, entity_id), ("", ""))


class DiskArtBothRootsTests(unittest.TestCase):
    """The Submissions folder-resolution feature needs art from downloads-root
    review folders too, not just the music library. disk_art_serve() must
    check _BROWSE_ALLOWED_ROOTS (music root + downloads root), not just the
    music root it originally hardcoded."""

    def test_disk_art_serve_checks_both_allowed_roots(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("any(_path_is_under(p, root) for root in _BROWSE_ALLOWED_ROOTS)", source)


if __name__ == "__main__":
    unittest.main()
