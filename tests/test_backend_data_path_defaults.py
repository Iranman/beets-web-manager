"""#251: backend defaults carry no maintainer-specific /data/... paths; they
derive from MUSIC_ROOT / DOWNLOADS_ROOT, and acquisition only creates folders
under a validated DOWNLOADS_ROOT (#235 QA F-1 and security N1)."""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

def _data_literals(path: Path):
    """Non-docstring string literals containing "/data/" (comments and
    docstring examples are not defaults)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "/data/" in node.value and id(node) not in docstrings
    ]


def _backend_files():
    # backend/** plus every repo-root module (app.py, helpers_mb.py,
    # job_engine.py, routes_*.py, ...). The "/data" state-mount default for
    # WEB_MANAGER_DATA_DIR in app_runtime has no trailing slash and is not a
    # library path, so it does not match.
    files = sorted(REPO.glob("backend/**/*.py")) + sorted(REPO.glob("*.py"))
    return {p.relative_to(REPO).as_posix(): p for p in files}


class NoDataPathLiteralGuard(unittest.TestCase):
    def test_no_data_literal_in_backend_defaults(self):
        self.assertIn("app.py", _backend_files())
        offenders = {
            name: lines for name, path in _backend_files().items()
            if (lines := _data_literals(path))
        }
        self.assertEqual(offenders, {}, "derive the path from MUSIC_ROOT/DOWNLOADS_ROOT (config_layers)")


class CleanupRootErrorNamesConfiguredRoots(unittest.TestCase):
    def test_error_lists_the_configured_roots(self):
        import backend.cleanup_service as cs
        with tempfile.TemporaryDirectory() as tmp:
            music, dl = Path(tmp, "music"), Path(tmp, "dl")
            with mock.patch.object(cs, "FOLDER_CLEAN_ROOTS", [music, dl]), \
                    self.assertRaises(RuntimeError) as ctx:
                cs._folder_clean_root(str(Path(tmp, "elsewhere")))
        self.assertEqual(str(ctx.exception), f"Root must be under {music} or {dl}.")


class SerializersUseValidatedDownloadRoots(unittest.TestCase):
    """An unsafe DOWNLOADS_ROOT (here one around the library, so it is dropped
    from DOWNLOADS_ALLOWED_ROOTS) is neither a cleanup root nor a "Downloads"
    origin label for library paths."""

    def test_dropped_downloads_root_is_not_allowed_or_labelled(self):
        import backend.serializers as ser
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            music = base / "music"
            folder = music / "Artist" / "Album"
            folder.mkdir(parents=True)
            # create=True: serializers no longer reads the raw setting at all.
            with mock.patch.object(ser, "DOWNLOADS_ROOT", base, create=True), \
                    mock.patch.object(ser, "MUSIC_ROOT", music), \
                    mock.patch.object(ser, "_DOWNLOADS_ROOTS", []), \
                    mock.patch.object(ser, "TORRENT_SOURCE_ROOTS", ()), \
                    mock.patch.object(ser, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", ()):
                roots = ser._import_review_cleanup_roots(allow_music=True)
                hint = ser._path_origin_hint(folder.as_posix())
        self.assertNotIn(base, roots)
        self.assertEqual(hint, {"source_folder": folder.as_posix()})

    def test_dropped_playlist_root_is_not_a_cleanup_root(self):
        import backend.serializers as ser
        raw = Path(tempfile.gettempdir()).resolve() / "playlist"
        with mock.patch.object(ser, "PLAYLIST_DOWNLOAD_ROOT", raw), \
                mock.patch.object(ser, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", ()), \
                mock.patch.object(ser, "_DOWNLOADS_ROOTS", []), \
                mock.patch.object(ser, "TORRENT_SOURCE_ROOTS", ()):
            self.assertEqual(ser._import_review_cleanup_roots(allow_music=False), [])


def _runtime_defaults(env_overrides):
    """Import app_runtime/dedup_service in a fresh interpreter (they read the
    environment at import time) and return the derived defaults."""
    code = (
        "import json, backend.app_runtime as rt, backend.dedup_service as d;"
        "print(json.dumps({'aliases': rt.PLAYLIST_PATH_ROOT_ALIASES,"
        " 'browse': [p.as_posix() for p in d._BROWSE_ALLOWED_ROOTS],"
        " 'playlist': rt.PLAYLIST_DOWNLOAD_ROOT.as_posix()}))"
    )
    with tempfile.TemporaryDirectory() as data_dir:
        env = {k: v for k, v in os.environ.items()
               if k not in {"PLAYLIST_PATH_ROOT_ALIASES", "PLEX_MUSIC_ROOT", "MUSIC_ROOT",
                            "DOWNLOADS_ROOT", "MUSIC_LIBRARY_PATH", "BEETS_MUSIC_DIR", "DOWNLOAD_PATH",
                            "PLAYLIST_DOWNLOAD_ROOT"}}
        env.update({"WEB_MANAGER_DATA_DIR": data_dir, **env_overrides})
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=120, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


class RuntimeDefaultsFromSettings(unittest.TestCase):
    def test_documented_defaults(self):
        got = _runtime_defaults({})
        self.assertEqual(got["aliases"], ["/music"])
        self.assertEqual([Path(p) for p in got["browse"]], [Path("/music"), Path("/downloads")])

    def test_aliases_follow_music_root(self):
        got = _runtime_defaults({"MUSIC_ROOT": "/srv/library"})
        self.assertEqual(got["aliases"], ["/srv/library"])

    def test_unsafe_downloads_root_adds_no_scan_root(self):
        got = _runtime_defaults({"DOWNLOADS_ROOT": "/"})
        self.assertEqual([Path(p) for p in got["browse"]], [Path("/music")])

    def test_playlist_download_root_is_never_the_working_directory(self):
        # #269 F-3: "" or a relative value used to mean the process CWD.
        for raw in ("", "relative/dir", "."):
            with self.subTest(raw=raw):
                got = _runtime_defaults({"PLAYLIST_DOWNLOAD_ROOT": raw})
                self.assertEqual(got["playlist"], "/downloads/music/Playlist Downloads")


class AcquisitionUsesValidatedDownloadsRoot(unittest.TestCase):
    def test_unsafe_downloads_root_fails_closed_before_any_job(self):
        import backend.acquisition_service as acq
        import backend.app_runtime as rt
        from backend import config_layers
        with mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", ()), \
                mock.patch.object(acq, "jobs") as jobs:
            body, _status = acq.start_album_download({"artist": "A", "album": "B", "method": "slskd"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], config_layers.UNSAFE_DOWNLOADS_ROOT_MESSAGE)
        jobs.start_python.assert_not_called()

    def test_validated_root_is_the_allowed_root(self):
        import backend.app_runtime as rt
        with mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (Path("/dl"),)):
            self.assertEqual(rt.validated_downloads_root(), Path("/dl"))


class LibraryServiceDownloadRoots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.downloads = self.root / "downloads"
        self.music = self.root / "music"

    def _patch(self, allowed, torrent_roots=()):
        import backend.library_service as lib
        return (
            mock.patch.object(lib, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(lib, "DOWNLOADS_ALLOWED_ROOTS", allowed),
            mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", torrent_roots),
            mock.patch.object(lib, "TORRENT_SOURCE_MOVE_ALLOWED", False),
            mock.patch.object(lib, "MUSIC_ROOT", self.music),
            mock.patch.object(lib, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", (self.root / "playlist",)),
        )

    def _run(self, fn, path, allowed, torrent_roots=()):
        patches = self._patch(allowed, torrent_roots)
        for p in patches:
            p.start()
        try:
            return fn(path)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_managed_staging_needs_a_validated_root(self):
        import backend.library_service as lib
        staged = self.downloads / "_beets_missing_import" / "slskd-x"
        self.assertTrue(self._run(lib._app_managed_download_path, staged, (self.downloads,)))
        self.assertFalse(self._run(lib._app_managed_download_path, staged, ()))

    def test_no_safe_root_preserves_the_source(self):
        import backend.library_service as lib
        source = self.downloads / "Artist" / "Album"
        self.assertTrue(self._run(lib._preserve_torrent_source_path, source, ()))
        # Unchanged with a safe root: a folder under it is preserved, others are not.
        self.assertTrue(self._run(lib._preserve_torrent_source_path, source, (self.downloads,)))
        self.assertFalse(self._run(lib._preserve_torrent_source_path, self.root / "x", (self.downloads,)))


class PlexRootsFromSettings(unittest.TestCase):
    def test_default_roots_are_settings_only(self):
        import backend.plex_service as plex
        with mock.patch.object(plex, "PLAYLIST_PATH_ROOT_ALIASES", ["/music"]), \
                mock.patch.object(plex, "MUSIC_ROOT", Path("/music")):
            roots = plex._plex_music_roots({"plex_music_roots": ""})
            keys = plex._playlist_path_keys("/music/Artist/Album/01.flac")
        self.assertEqual(roots, ["/music"])
        self.assertIn("artist/album/01.flac", keys)
        self.assertNotIn("artist/album/01.flac", plex._playlist_path_keys("/data/media/music/Artist/Album/01.flac"))


class MatchingPrefixesUnchanged(unittest.TestCase):
    """The layout markers still strip the same prefix from /data/... paths."""

    CASES = {
        "/data/media/music/Aaliyah (2001)/Aaliyah/01 - Aaliyah - We Need a Resolution.flac":
            ["aaliyah 2001", "aaliyah", "01"],
        "/data/torrents/music/Artist [0383dadf-2a4e-4d10-a46a-e9e041da8eb3]/Album (1999)/02 Artist - Song.mp3":
            ["artist", "album 1999", "album", "02 artist"],
        "/data/downloads/music/X/Y/03 - Z.flac": ["x", "y", "03"],
        "/downloads/music/A/B/01 A - b.flac": ["a", "b", "01 a"],
    }

    def test_track_path_prefixes(self):
        from backend.matching import track_path_prefixes
        for path, expected in self.CASES.items():
            self.assertEqual(track_path_prefixes(path), expected, path)

    def test_album_track_path_prefixes(self):
        import backend.matching_service as ms
        for path, expected in self.CASES.items():
            self.assertEqual(ms._album_track_path_prefixes(path), expected, path)


if __name__ == "__main__":
    unittest.main()
