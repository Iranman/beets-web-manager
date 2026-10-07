"""#268 (security review of #267): album download folders stay strictly inside
the validated DOWNLOADS_ROOT (S-1); staging, submission and playlist allowlists
use validated roots only (S-2, S-3, S-4)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


class _Stop(Exception):
    pass


class DownloadDestContainment(unittest.TestCase):
    """S-1. Started from the security reviewer's probe, which showed ".."
    artist/album names escaping the root; such names are now refused."""

    root = Path("/dl/root")

    def _start(self, artist, album, year=""):
        import backend.acquisition_service as acq
        import backend.app_runtime as rt
        seen = {}

        def fake_download(_artist, _album, _year, dest_dir, log, **_kw):
            seen["dest"] = dest_dir
            raise _Stop()

        with mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (self.root,)), \
                mock.patch.object(acq, "jobs") as jobs, \
                mock.patch.object(acq, "_ytdlp_album_download", side_effect=fake_download):
            body, _ = acq.start_album_download(
                {"artist": artist, "album": album, "year": year, "method": "ytdlp", "auto_import": False})
            if jobs.start_python.called:
                try:
                    jobs.start_python.call_args[0][0]([], None)
                except Exception:
                    pass
        return body, jobs, seen.get("dest")

    def test_dot_segments_are_refused_before_any_job(self):
        import backend.acquisition_service as acq
        for artist, album in (("..", ".."), ("..", "music"), (".", ".."), (" .. ", "x"),
                              ("x", " . "), ("x", "..")):
            with self.subTest(artist=artist, album=album):
                body, jobs, dest = self._start(artist, album, year="1999")
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"], acq._DOWNLOAD_FOLDER_ERROR)
                jobs.start_python.assert_not_called()
                self.assertIsNone(dest)

    def test_benign_names_keep_their_folder(self):
        safe = lambda s: re.sub(r'[\\/:*?"<>|]', '_', s).strip()  # the pre-#268 rule
        for artist, album in (("AC/DC", "Back in Black"), ("Metallica", "...And Justice for All"),
                              ("Mr. Big", "Lean into It"), ("Björk", "Homogenic"),
                              ("Sigur Rós", "( )"), (".hack", "Album."), ("A.", "...B")):
            with self.subTest(artist=artist, album=album):
                body, _jobs, dest = self._start(artist, album, year="1999")
                self.assertTrue(body.get("ok", True), body)
                self.assertEqual(Path(dest), self.root / safe(artist) / f"{safe(album)} (1999)")

    def test_dot_only_titles_other_than_dot_dot_are_kept(self):
        # QA #269: "..." and ". . ." are real titles, not traversal.
        for artist, album in (("...", "x"), ("x", "...."), ("x", ". . .")):
            with self.subTest(artist=artist, album=album):
                body, _jobs, dest = self._start(artist, album)
                self.assertTrue(body.get("ok", True), body)
                self.assertEqual(Path(dest), self.root / artist / album)

    def test_symlinked_artist_folder_out_of_root_gets_escape_message(self):
        import backend.acquisition_service as acq
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp, "root"), Path(tmp, "outside")
            root.mkdir()
            outside.mkdir()
            try:
                (root / "A").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks not available")
            with self.assertRaises(ValueError) as ctx:
                acq._download_dest_under(root, "A", "B")
            self.assertEqual(str(ctx.exception), acq._DOWNLOAD_ESCAPE_ERROR)

    def test_year_cannot_add_path_components(self):
        body, _jobs, dest = self._start("A", "B", year="1/../../..")
        self.assertTrue(body.get("ok", True), body)
        self.assertEqual(Path(dest).parent, self.root / "A")

    def test_missing_track_folder_stays_inside_the_root(self):
        """QA #269: the missing-track folder (_direct_dest_dir) is contained too."""
        import backend.acquisition_service as acq
        import backend.app_runtime as rt
        seen = []

        def fake_missing(_artist, _album, _year, dest_dir, log, *_a, **_kw):
            seen.append(dest_dir)
            raise _Stop()

        for artist, album, year in (("./..", "../", ".."), ("A", "B", "1/../../..")):
            with self.subTest(artist=artist, album=album, year=year), \
                    mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (self.root,)), \
                    mock.patch.object(acq, "jobs") as jobs, \
                    mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=fake_missing):
                del seen[:]
                body, _ = acq.start_album_download({
                    "artist": artist, "album": album, "year": year, "method": "ytdlp",
                    "auto_import": False, "missing_tracks": [{"title": "T", "track": 1}]})
                self.assertTrue(body.get("ok", True), body)
                try:
                    jobs.start_python.call_args[0][0]([], None)
                except Exception:
                    pass
                self.assertEqual(len(seen), 1)
                dest = Path(os.path.normpath(seen[0]))
                self.assertEqual(dest.parent.parent, self.root)
                self.assertIn(" missing ", dest.name)

    def test_dest_must_be_strictly_inside_the_root(self):
        import backend.acquisition_service as acq
        for segments in (("a", ".."), ("..", "a"), ("",), ("a", "")):
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                acq._download_dest_under(self.root, *segments)
        self.assertEqual(acq._download_dest_under(self.root, "a", "b"), self.root / "a" / "b")


class StagingUsesValidatedRoot(unittest.TestCase):
    """S-2 / #267 QA finding 1: import staging never mkdirs under an unsafe root."""

    def test_unsafe_root_fails_closed(self):
        import backend.app_runtime as rt
        import backend.import_service as imp
        from backend import config_layers
        with mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", ()), \
                mock.patch.object(imp, "_stage_selected_audio_files_impl") as impl:
            with self.assertRaises(RuntimeError) as ctx:
                imp._stage_selected_audio_files("/src", [Path("/src/01.flac")], "A", "B", [])
        self.assertEqual(str(ctx.exception), config_layers.UNSAFE_DOWNLOADS_ROOT_MESSAGE)
        impl.assert_not_called()

    def test_safe_root_is_passed_through(self):
        import backend.app_runtime as rt
        import backend.import_service as imp
        with mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (Path("/dl"),)), \
                mock.patch.object(imp, "_stage_selected_audio_files_impl", return_value="x") as impl:
            imp._stage_selected_audio_files("/src", [Path("/src/01.flac")], "A", "B", [])
        self.assertEqual(impl.call_args[0][0], Path("/dl"))


class PlaylistRootsValidated(unittest.TestCase):
    """S-4: unsafe download roots are neither resolvable nor app-managed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_resolve_allowlist_uses_validated_roots(self):
        import backend.plex_service as plex
        dl, pl = self.root / "downloads", self.root / "playlists"
        track = dl / "A" / "01.flac"
        with mock.patch.object(plex, "MUSIC_ROOT", self.root / "music"), \
                mock.patch.object(plex, "PLAYLIST_PATH_ROOT_ALIASES", []), \
                mock.patch.object(plex, "DOWNLOADS_ROOT", dl), \
                mock.patch.object(plex, "PLAYLIST_DOWNLOAD_ROOT", pl):
            with mock.patch.object(plex, "DOWNLOADS_ALLOWED_ROOTS", (dl,)), \
                    mock.patch.object(plex, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", (pl,)):
                self.assertEqual(plex._playlist_resolve_item_path(str(track)), track)
                self.assertEqual(plex._playlist_resolve_item_path(str(pl / "x.flac")), pl / "x.flac")
            with mock.patch.object(plex, "DOWNLOADS_ALLOWED_ROOTS", ()), \
                    mock.patch.object(plex, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", ()):
                self.assertEqual(plex._playlist_resolve_item_path(str(track)), plex._PLAYLIST_UNRESOLVED_PATH)
                self.assertEqual(plex._playlist_resolve_item_path(str(pl / "x.flac")),
                                 plex._PLAYLIST_UNRESOLVED_PATH)

    def test_unsafe_playlist_root_is_not_app_managed(self):
        import backend.library_service as lib
        pl = self.root / "playlists"
        with mock.patch.object(lib, "DOWNLOADS_ALLOWED_ROOTS", ()), \
                mock.patch.object(lib, "PLAYLIST_DOWNLOAD_ROOT", pl):
            with mock.patch.object(lib, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", (pl,)):
                self.assertTrue(lib._app_managed_download_path(pl / "k" / "downloads"))
            with mock.patch.object(lib, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", ()):
                self.assertFalse(lib._app_managed_download_path(pl / "k" / "downloads"))

    def test_unsafe_playlist_root_is_never_a_staging_folder(self):
        # QA #269: staging and staged-track deletion used the raw root.
        import backend.playlist_service as ps
        pl = self.root / "playlists"
        with mock.patch.object(ps, "PLAYLIST_DOWNLOAD_ROOT", pl), \
                mock.patch.object(ps, "_playlist_key", lambda *_a, **_k: "k"):
            with mock.patch.object(ps, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", (pl,)):
                self.assertEqual(ps._playlist_downloads_dir("Mix").parent.parent, pl)
            with mock.patch.object(ps, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", ()):
                for call in (lambda: ps._playlist_downloads_dir("Mix"),
                             lambda: ps.get_playlist_staging_root("Mix")):
                    with self.assertRaises(RuntimeError) as ctx:
                        call()
                    self.assertIn("PLAYLIST_DOWNLOAD_ROOT", str(ctx.exception))


def _import_roots(env_overrides):
    """S-3 / S-4 module constants as a fresh interpreter computes them."""
    code = (
        "import json, app, routes_submissions as s, backend.app_runtime as rt;"
        "print(json.dumps({'submission': [p.as_posix() for p in s._SUBMISSION_ALLOWED_ROOTS],"
        " 'playlist': [p.as_posix() for p in rt.PLAYLIST_DOWNLOAD_ALLOWED_ROOTS]}))"
    )
    with tempfile.TemporaryDirectory() as data_dir:
        env = {k: v for k, v in os.environ.items()
               if k not in {"MUSIC_ROOT", "DOWNLOADS_ROOT", "PLAYLIST_DOWNLOAD_ROOT", "MUSIC_LIBRARY_PATH",
                            "BEETS_MUSIC_DIR", "DOWNLOAD_PATH"}}
        env.update({"WEB_MANAGER_DATA_DIR": data_dir, **env_overrides})
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=180, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


class ModuleRootsFromValidatedSettings(unittest.TestCase):
    def test_documented_defaults(self):
        got = _import_roots({})
        self.assertEqual([Path(p) for p in got["submission"]], [Path("/music"), Path("/downloads")])
        self.assertEqual([Path(p) for p in got["playlist"]], [Path("/downloads/music/Playlist Downloads")])

    def test_unsafe_roots_add_nothing(self):
        got = _import_roots({"DOWNLOADS_ROOT": "/", "PLAYLIST_DOWNLOAD_ROOT": "/music/Playlist Downloads"})
        self.assertEqual([Path(p) for p in got["submission"]], [Path("/music")])
        self.assertEqual(got["playlist"], [])


if __name__ == "__main__":
    unittest.main()
