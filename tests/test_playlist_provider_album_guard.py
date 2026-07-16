import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def load_symbols(names, namespace):
    wanted = set(names)
    tree = ast.parse(APP_SOURCE)
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

        class FakeMediaFile:
            instances = {}
            seed = {}

            def __init__(self, path):
                self.path = path
                data = dict(self.seed[path])
                self.artist = data.get("artist", "")
                self.albumartist = data.get("albumartist", "")
                self.title = data.get("title", "")
                self.album = data.get("album", "")
                self.year = data.get("year", 0)
                self.saved = False
                self.instances[path] = self

            def save(self):
                self.saved = True

        fake_module = types.ModuleType("mediafile")
        fake_module.MediaFile = FakeMediaFile
        old_mediafile = sys.modules.get("mediafile")
        sys.modules["mediafile"] = fake_module
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio = Path(tmp) / "hoe phase.mp3"
                audio.write_bytes(b"")
                FakeMediaFile.seed[str(audio)] = {
                    "artist": "",
                    "albumartist": "",
                    "title": "",
                    "album": "SoundCloud",
                    "year": 2026,
                }
                log = []
                ns["_enrich_playlist_file_tags"](
                    audio,
                    {"artist": "1takejay", "title": "Hoe Phase"},
                    log,
                )
                mf = FakeMediaFile.instances[str(audio)]
                self.assertEqual(mf.album, "Hoe Phase")
                self.assertEqual(mf.artist, "1takejay")
                self.assertEqual(mf.albumartist, "1takejay")
                self.assertEqual(mf.title, "Hoe Phase")
                self.assertEqual(mf.year, 0)
                self.assertTrue(mf.saved)
                self.assertTrue(any("provider name" in line for line in log))
        finally:
            if old_mediafile is None:
                sys.modules.pop("mediafile", None)
            else:
                sys.modules["mediafile"] = old_mediafile

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

        class FakeMediaFile:
            instance = None

            def __init__(self, path):
                self.artist = ""
                self.albumartist = ""
                self.title = ""
                self.album = ""
                self.year = 2025
                self.saved = False
                FakeMediaFile.instance = self

            def save(self):
                self.saved = True

        fake_module = types.ModuleType("mediafile")
        fake_module.MediaFile = FakeMediaFile
        old_mediafile = sys.modules.get("mediafile")
        sys.modules["mediafile"] = fake_module
        try:
            ns["_enrich_playlist_file_tags"](
                Path("till i bust.mp3"),
                {"artist": "1takejay", "title": "Till I Bust"},
                [],
            )
            mf = FakeMediaFile.instance
            self.assertEqual(mf.album, "Till I Bust")
            self.assertEqual(mf.year, 0)
            self.assertTrue(mf.saved)
        finally:
            if old_mediafile is None:
                sys.modules.pop("mediafile", None)
            else:
                sys.modules["mediafile"] = old_mediafile

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

        class FakeMediaFile:
            instance = None

            def __init__(self, path):
                self.artist = "AiritOut JuJu"
                self.albumartist = "AiritOut JuJu"
                self.title = "Track One"
                self.album = "Trap Is In Session"
                self.year = 2024
                self.saved = False
                FakeMediaFile.instance = self

            def save(self):
                self.saved = True

        fake_module = types.ModuleType("mediafile")
        fake_module.MediaFile = FakeMediaFile
        old_mediafile = sys.modules.get("mediafile")
        sys.modules["mediafile"] = fake_module
        try:
            ns["_enrich_playlist_file_tags"](
                Path("track.mp3"),
                {"artist": "AiritOut JuJu", "title": "Track One"},
                [],
            )
            mf = FakeMediaFile.instance
            self.assertEqual(mf.album, "Trap Is In Session")
            self.assertEqual(mf.year, 2024)
            self.assertFalse(mf.saved)
        finally:
            if old_mediafile is None:
                sys.modules.pop("mediafile", None)
            else:
                sys.modules["mediafile"] = old_mediafile

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
        self.assertIn("_enrich_playlist_file_tags(destination, track, log)", APP_SOURCE)
        self.assertIn("elif action == \"import_downloaded\":\n                result = _playlist_run_import_downloaded", APP_SOURCE)
        self.assertIn("_playlist_run_import_downloaded(name, state[\"log\"]", APP_SOURCE)
        self.assertIn("full = action in {\"run_full\", \"resume\"}", APP_SOURCE)


if __name__ == "__main__":
    unittest.main()