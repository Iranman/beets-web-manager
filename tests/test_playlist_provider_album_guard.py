import ast
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _app_ast_cache import get_app_ast  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def load_symbols(names, namespace):
    wanted = set(names)
    tree = get_app_ast()
    body = []
    for node in tree.body:
        node_name = getattr(node, "name", None)
        if node_name in wanted:
            body.append(node)
            continue
        if isinstance(node, ast.Assign):
            target_names = {getattr(target, "id", None) for target in node.targets}
            if target_names & wanted:
                body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "app.py", "exec"), namespace)
    return {name: namespace[name] for name in wanted if name in namespace}


class PlaylistProviderAlbumGuardTests(unittest.TestCase):
    def _namespace(self):
        return {
            "Any": Any,
            "Dict": Dict,
            "List": List,
            "Optional": Optional,
            "Path": Path,
            "re": __import__("re"),
            "_s": lambda value: value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or ""),
        }

    def test_provider_and_generic_album_values_are_rejected(self):
        ns = self._namespace()
        load_symbols([
            "_PLAYLIST_PROVIDER_ALBUM_LABELS",
            "_PLAYLIST_BAD_ALBUM_VALUES",
            "_PLAYLIST_BAD_ALBUM_COMPACT_VALUES",
            "_playlist_album_value_key",
            "_playlist_album_value_is_bad_fallback",
            "_playlist_provider_album_label",
        ], ns)
        is_bad = ns["_playlist_album_value_is_bad_fallback"]

        for value in ["SoundCloud", "YouTube", "Spotify", "slskd", "spotiflac", "Soulseek", "Playlist", "Downloads"]:
            self.assertTrue(is_bad(value), value)
        self.assertFalse(is_bad("Trap Is In Session"))
        self.assertEqual(ns["_playlist_provider_album_label"]("SoundCloud"), "SoundCloud")

    def test_preimport_tag_enrichment_replaces_provider_album_and_clears_year(self):
        ns = self._namespace()
        load_symbols([
            "_PLAYLIST_PROVIDER_ALBUM_LABELS",
            "_PLAYLIST_BAD_ALBUM_VALUES",
            "_PLAYLIST_BAD_ALBUM_COMPACT_VALUES",
            "_playlist_album_value_key",
            "_playlist_album_value_is_bad_fallback",
            "_enrich_playlist_file_tags",
        ], ns)

        written_tags = {}
        mock_client = mock.MagicMock()
        mock_client.write_tags = lambda p, tags: written_tags.update(tags)
        ns["beets_client"] = mock_client
        ns["_read_file_media_tags"] = lambda p: {"artist": "", "albumartist": "", "title": "", "album": "SoundCloud", "year": 2026}

        log = []
        ns["_enrich_playlist_file_tags"](
            Path("hoe phase.mp3"),
            {"artist": "1takejay", "title": "Hoe Phase"},
            log,
        )
        self.assertEqual(written_tags.get("album"), "Hoe Phase")
        self.assertEqual(written_tags.get("artist"), "1takejay")
        self.assertEqual(written_tags.get("albumartist"), "1takejay")
        self.assertEqual(written_tags.get("title"), "Hoe Phase")
        self.assertEqual(written_tags.get("year"), 0)
        self.assertTrue(any("provider name" in line for line in log))

    def test_empty_album_fallback_clears_upload_year(self):
        ns = self._namespace()
        load_symbols([
            "_PLAYLIST_PROVIDER_ALBUM_LABELS",
            "_PLAYLIST_BAD_ALBUM_VALUES",
            "_PLAYLIST_BAD_ALBUM_COMPACT_VALUES",
            "_playlist_album_value_key",
            "_playlist_album_value_is_bad_fallback",
            "_enrich_playlist_file_tags",
        ], ns)

        written_tags = {}
        mock_client = mock.MagicMock()
        mock_client.write_tags = lambda p, tags: written_tags.update(tags)
        ns["beets_client"] = mock_client
        ns["_read_file_media_tags"] = lambda p: {"artist": "", "albumartist": "", "title": "", "album": "", "year": 2025}

        ns["_enrich_playlist_file_tags"](
            Path("till i bust.mp3"),
            {"artist": "1takejay", "title": "Till I Bust"},
            [],
        )
        self.assertEqual(written_tags.get("album"), "Till I Bust")
        self.assertEqual(written_tags.get("year"), 0)

    def test_verified_album_tag_is_preserved(self):
        ns = self._namespace()
        load_symbols([
            "_PLAYLIST_PROVIDER_ALBUM_LABELS",
            "_PLAYLIST_BAD_ALBUM_VALUES",
            "_PLAYLIST_BAD_ALBUM_COMPACT_VALUES",
            "_playlist_album_value_key",
            "_playlist_album_value_is_bad_fallback",
            "_enrich_playlist_file_tags",
        ], ns)

        written_tags = {}
        mock_client = mock.MagicMock()
        mock_client.write_tags = lambda p, tags: written_tags.update(tags)
        ns["beets_client"] = mock_client
        ns["_read_file_media_tags"] = lambda p: {"artist": "AiritOut JuJu", "albumartist": "AiritOut JuJu", "title": "Track One", "album": "Trap Is In Session", "year": 2024}

        ns["_enrich_playlist_file_tags"](
            Path("track.mp3"),
            {"artist": "AiritOut JuJu", "title": "Track One"},
            [],
        )
        self.assertNotIn("album", written_tags)
        self.assertNotIn("year", written_tags)

    def test_quality_and_repair_paths_mark_existing_provider_album_rows(self):
        ns = self._namespace()
        load_symbols([
            "_PLAYLIST_PROVIDER_ALBUM_LABELS",
            "_PLAYLIST_BAD_ALBUM_VALUES",
            "_PLAYLIST_BAD_ALBUM_COMPACT_VALUES",
            "_playlist_album_value_key",
            "_playlist_album_value_is_bad_fallback",
            "_playlist_provider_album_label",
            "_playlist_source_guess",
            "_playlist_quality_for_item",
            "_playlist_repair_metadata",
        ], ns)
        ns["PLAYLIST_MIN_DOWNLOAD_SECONDS"] = 30
        ns["_playlist_resolve_item_path"] = lambda _path: ROOT / "app.py"
        ns["_playlist_split_artist_title"] = lambda _title: None

        item = SimpleNamespace(
            album="SoundCloud",
            albumartist="1takejay",
            length=180,
            bitrate=320000,
            format="MP3",
        )
        quality = ns["_playlist_quality_for_item"](item, "/data/music/1takejay/SoundCloud (2026)/track.mp3")
        self.assertEqual(quality["quality"], "review")
        self.assertIn("provider_album", quality["quality_flags"])
        self.assertEqual(quality["source"], "SoundCloud")
        self.assertEqual(
            ns["_playlist_repair_metadata"]({"artist": "1takejay", "title": "hoe phase", "album": "SoundCloud"})["album"],
            "",
        )
        self.assertIn("provider_album_sql", APP_SOURCE)

    def test_musicbrainz_album_hint_rejects_provider_album_before_search(self):
        ns = self._namespace()
        load_symbols([
            "_PLAYLIST_PROVIDER_ALBUM_LABELS",
            "_PLAYLIST_BAD_ALBUM_VALUES",
            "_PLAYLIST_BAD_ALBUM_COMPACT_VALUES",
            "_playlist_album_value_key",
            "_playlist_album_value_is_bad_fallback",
            "_playlist_log",
            "_playlist_album_tag_release_placement",
        ], ns)
        log = []
        result = ns["_playlist_album_tag_release_placement"](
            {"album": "SoundCloud"},
            {"artist": "1takejay", "title": "Hoe Phase"},
            "Hoe Phase",
            "1takejay",
            "/tmp/hoe phase.mp3",
            180,
            0.86,
            0.68,
            log,
        )
        self.assertEqual(result, {})
        self.assertTrue(any("provider name" in line for line in log))

    def test_all_playlist_import_entrypoints_share_preimport_sanitizer(self):
        self.assertIn("def _enrich_playlist_file_tags", APP_SOURCE)
        self.assertIn("beets_client.import_playlist_staged", APP_SOURCE)
        self.assertIn("elif action == \"import_downloaded\":\n                result = _playlist_run_import_downloaded", APP_SOURCE)
        self.assertIn("_playlist_run_import_downloaded(name, state[\"log\"]", APP_SOURCE)
        self.assertIn("full = action in {\"run_full\", \"resume\"}", APP_SOURCE)


if __name__ == "__main__":
    unittest.main()