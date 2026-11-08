"""App-generated download folders carry an ownership marker, so a user Import
batch over the downloads root never picks them up, and a folder name alone
never makes a user's (or a torrent's) folder movable.

Production: an Import batch over /downloads queued
"<album> - soundcloud missing <epoch>__slot_deferred" folders, because the
name-only rule ended in ``\\d{13}$``.
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    global APP, ai, lib, slskd, acq, ytdlp, recover, imp, patch_app_family
    tmp = Path(tempfile.mkdtemp(prefix="bwm_app_owned_env_"))
    os.environ.setdefault("BEETSDIR", str(tmp / "config"))
    os.environ.setdefault("LIB_PATH", str(tmp / "config" / "musiclibrary.blb"))
    os.environ.setdefault("AI_BATCH_STATE_DIR", str(tmp / "ai_batch_jobs"))
    os.environ.setdefault("METADATA_CACHE_DIR", str(tmp / "cache"))
    os.environ.setdefault("BEETS_TRANSACTION_DIR", str(tmp / "transactions"))
    os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    import app as APP  # noqa: N811
    import backend.ai_service as ai
    import backend.library_service as lib
    import backend.slskd as slskd
    import backend.acquisition_service as acq
    import backend.ytdlp_service as ytdlp
    import backend.recover_service as recover
    import backend.import_service as imp
    try:
        from _app_family import patch_app_family
    except ImportError:  # pragma: no cover
        from tests._app_family import patch_app_family


def _symlink(target, link, is_dir):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as ex:
        raise unittest.SkipTest(f"symlinks unavailable here: {ex}")


class _Roots(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_app_owned_", dir=os.getcwd())).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dl = self.tmp / "downloads"
        self.music = self.tmp / "music"
        self.outside = self.tmp / "outside"
        for p in (self.dl, self.music, self.outside):
            p.mkdir()
        for name, value in (("_DOWNLOADS_ROOTS", [str(self.dl)]), ("DOWNLOADS_ALLOWED_ROOTS", [self.dl]),
                            ("TORRENT_SOURCE_ROOTS", [self.dl]), ("TORRENT_SOURCE_MOVE_ALLOWED", False),
                            ("PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", []), ("MUSIC_ROOT", self.music),
                            ("_IMPORT_SOURCE_ALLOWED_ROOTS", (self.dl, self.music))):
            p = patch_app_family(APP, name, value)
            p.start()
            self.addCleanup(p.stop)

    def audio(self, folder, name="01.flac"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x")
        return folder

    def app_folder(self, rel):
        folder = self.dl / rel
        slskd.make_app_download_dir(folder)
        return self.audio(folder)

    def managed(self, path):
        return lib._app_managed_download_path(Path(path))


class BatchDiscoveryTests(_Roots):
    def test_production_slot_deferred_folders_are_skipped(self):
        """The two folders the production batch queued (no marker: made before it)."""
        user = self.audio(self.dl / "112" / "Part III")
        self.audio(self.dl / "112" / "Part III - soundcloud missing 1793260626691__slot_deferred")
        self.audio(self.dl / "2 Chainz" / "So Help Me God! (2020) - ytdlp missing 1793263936606__slot_deferred")
        self.audio(self.dl / "2 Chainz" / "Rec - ytdlp missing rec-0123456789abcdef")
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl), []), [str(user)])

    def test_every_app_generated_folder_is_skipped(self):
        user = self.audio(self.dl / "Artist" / "User Album (2020)")
        self.app_folder("Artist/Full Album (2021)")  # full-album direct download
        self.app_folder("Artist/Odd Name__slot_deferred")  # any slot folder made by the app
        self.app_folder("recovered/youtube/run1")  # Recover My Uploads staging
        # Folders made before the marker: the exact generated names.
        self.audio(self.dl / "112" / "Part III - soundcloud missing 1793260626691__slot_deferred")
        self.audio(self.dl / "2 Chainz" / "So Help Me God! (2020) - ytdlp missing 1793263936606__slot_deferred")
        self.audio(self.dl / "Artist" / "Rec - spotiflac missing rec-0123456789abcdef")
        self.audio(self.dl / "Artist" / "Rec - spotiflac missing rec-0123456789abcdef__slot_deferred")
        log = []
        found = ai._ai_batch_find_audio_dirs(str(self.dl), log)
        self.assertEqual(found, [str(user)])
        self.assertEqual(sum("app-managed download folder skipped" in line for line in log), 7, log)


class OwnershipProofTests(_Roots):
    def test_marker_proves_folder_and_its_subfolders(self):
        folder = self.app_folder("Artist/Album (2021)")
        self.assertTrue(self.managed(folder))
        self.assertTrue(self.managed(self.audio(folder / "CD1")))
        self.assertFalse(self.managed(self.dl / "Artist"))
        self.assertFalse(self.managed(self.dl))

    def test_legacy_names_are_exactly_the_generated_ones(self):
        for name in (slskd.direct_download_folder_name("A (2020)", "soundcloud", "1793260626691"),
                     slskd.direct_download_folder_name("A", "ytdlp", "rec-0123456789abcdef"),
                     "A - yt missing 1793260626691" + slskd.SLOT_DEFERRED_SUFFIX):
            self.assertTrue(self.managed(self.dl / "Artist" / name), name)
        for name in ("A - ytdlp missing 123", "A - ytdlp missing rec-xyz", "A__slot_deferred",
                     "A - ytdlp missing 1793260626691__slot_deferred extra"):
            self.assertFalse(self.managed(self.audio(self.dl / "Artist" / name)), name)

    def test_name_or_marker_outside_downloads_root_is_no_proof(self):
        name = "A - soundcloud missing 1793260626691__slot_deferred"
        self.assertFalse(self.managed(self.audio(self.outside / name)))
        marked = self.outside / "marked"
        slskd.make_app_download_dir(marked)
        self.assertTrue((marked / slskd.APP_DOWNLOAD_MARKER).is_file())
        self.assertFalse(self.managed(marked))
        with mock.patch.object(lib, "DOWNLOADS_ALLOWED_ROOTS", ()):
            self.assertFalse(self.managed(self.dl / "Artist" / name))

    def test_marker_must_be_a_regular_file(self):
        folder = self.audio(self.dl / "Artist" / "User")
        (folder / slskd.APP_DOWNLOAD_MARKER).mkdir()
        self.assertFalse(self.managed(folder))

    def test_symlinked_marker_is_no_proof(self):
        real = self.outside / "real-marker"
        real.write_text("x")
        folder = self.audio(self.dl / "Artist" / "User")
        _symlink(real, folder / slskd.APP_DOWNLOAD_MARKER, False)
        self.assertFalse(self.managed(folder))

    def test_symlinked_folder_is_no_proof(self):
        app = self.app_folder("Artist/Album (2021)")
        legacy = self.audio(self.dl / "Artist" / "A - yt missing 1793260626691")
        _symlink(app, self.dl / "Torrent", True)
        _symlink(legacy, self.dl / "B - yt missing 1793260626692", True)
        _symlink(self.dl / "Artist", self.dl / "Linked", True)
        self.assertFalse(self.managed(self.dl / "Torrent"))
        self.assertFalse(self.managed(self.dl / "B - yt missing 1793260626692"))
        self.assertFalse(self.managed(self.dl / "Linked" / "Album (2021)"))

    def test_existing_folder_is_never_marked(self):
        folder = self.audio(self.dl / "Artist" / "User")
        slskd.make_app_download_dir(folder)
        self.assertFalse((folder / slskd.APP_DOWNLOAD_MARKER).exists())
        self.assertFalse(self.managed(folder))


class TorrentPreservationTests(_Roots):
    def test_spoofed_torrent_folder_stays_preserved(self):
        for name in ("A__slot_deferred", "A - ytdlp missing 12345", "A - soundcloud missing x"):
            folder = self.audio(self.dl / "Torrents" / name)
            self.assertTrue(lib._preserve_torrent_source_path(folder), name)
            self.assertTrue(lib._preserve_torrent_source_file(folder / "01.flac"), name)
        real = self.outside / "real-marker"
        real.write_text("x")
        folder = self.audio(self.dl / "Torrents" / "Linked marker")
        _symlink(real, folder / slskd.APP_DOWNLOAD_MARKER, False)
        self.assertTrue(lib._preserve_torrent_source_path(folder))

    def test_marked_app_folder_is_not_preserved(self):
        folder = self.app_folder("Artist/Album (2021)")
        self.assertFalse(lib._preserve_torrent_source_path(folder))
        self.assertFalse(lib._preserve_torrent_source_path(self.audio(folder / "CD1")))


class CreationSiteTests(_Roots):
    """Each flow that creates a download folder writes the marker."""

    def _stop_after_mkdir(self, module):
        made = []
        real = slskd.make_app_download_dir

        def spy(path):
            real(path)
            made.append(Path(path))
            raise RuntimeError("stop after mkdir")

        return made, mock.patch.object(module, "make_app_download_dir", side_effect=spy)

    def _assert_marked(self, made, expected):
        self.assertEqual(made, [expected])
        self.assertTrue((expected / slskd.APP_DOWNLOAD_MARKER).is_file())
        self.assertTrue(self.managed(expected))

    def test_direct_source_folder_names(self):
        req = mock.Mock(recovery_stage_prefix=None, downloads_root=self.dl, safe_artist="Artist",
                        safe_album="Album", yr_sfx=" (2020)", method="soundcloud", dest_dir="")
        run = mock.Mock(req=req, job_wanted_tracks=[{"title": "T", "track": 1}])
        for prefix in (None, "rec-0123456789abcdef"):
            req.recovery_stage_prefix = prefix
            name = Path(acq._AlbumDownloadRun._direct_dest_dir(run, None, "soundcloud")).name
            self.assertRegex(name, slskd.APP_DOWNLOAD_NAME_RE)
            self.assertRegex(name + slskd.SLOT_DEFERRED_SUFFIX, slskd.APP_DOWNLOAD_NAME_RE)

    def test_ytdlp_downloads_mark_their_folder(self):
        ready = threading.Event()
        ready.set()
        dest = self.dl / "Artist" / "Album (2020)"
        for call in (lambda: ytdlp._ytdlp_album_download("A", "B", "", str(dest), [], source="soundcloud"),
                     lambda: ytdlp._ytdlp_missing_tracks_download(
                         "A", "B", "", str(dest), [], [{"title": "T", "track": 1}], source="soundcloud")):
            shutil.rmtree(dest, ignore_errors=True)
            made, spy = self._stop_after_mkdir(ytdlp)
            with spy, mock.patch.object(ytdlp, "_ytdlp_ready", ready), \
                    mock.patch.object(ytdlp, "_ytdlp_cookie_auths_for_source", return_value=[]), \
                    mock.patch.object(ytdlp, "_ytdlp_source_requires_js", return_value=False), \
                    mock.patch.dict(sys.modules, {"yt_dlp": mock.Mock()}):
                with self.assertRaisesRegex(RuntimeError, "stop after mkdir"):
                    call()
            self._assert_marked(made, dest)

    def test_spotiflac_download_marks_its_folder(self):
        dest = self.dl / "Artist" / "Album - spotiflac missing 1793260626691"
        made, spy = self._stop_after_mkdir(ytdlp)
        with spy, mock.patch.object(ytdlp, "_spotiflac_services", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "stop after mkdir"):
                ytdlp._spotiflac_download_url("https://example.invalid/x", str(dest), [])
        self._assert_marked(made, dest)

    def test_recover_stage_marks_its_folder(self):
        with mock.patch.object(recover, "validated_downloads_root", return_value=self.dl):
            stage = Path(recover._recover_stage_dir("youtube", "run1"))
        slskd.make_app_download_dir(stage)  # what the fetch job calls
        self.assertTrue(self.managed(stage))
        src = Path(recover.__file__).read_text(encoding="utf-8")
        self.assertIn("make_app_download_dir(stage_dir)", src)
        self.assertNotIn("os.makedirs(stage_dir", src)

    def test_slot_deferred_folder_is_marked_only_when_created(self):
        import_dir = self.audio(self.dl / "Artist" / "Album (2020)", "01 A.flac")
        deferred = import_dir / "02 B.flac"
        deferred.write_bytes(b"x")

        def _move(src, dest):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)

        with mock.patch.object(acq.composite_workflows, "move_staging_file", side_effect=_move):
            slot_dir, complete = acq._separate_deferred_slot_files(str(import_dir), [str(deferred)], [])
        self.assertTrue(complete)
        self.assertEqual(Path(slot_dir).name, "Album (2020)" + slskd.SLOT_DEFERRED_SUFFIX)
        self.assertTrue((Path(slot_dir) / slskd.APP_DOWNLOAD_MARKER).is_file())
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl), []), [str(import_dir)])

        # A pre-existing (user) folder with that name is never claimed.
        user_dir = self.audio(self.dl / "Other" / "Album", "01 A.flac")
        user_slot = self.audio(self.dl / "Other" / ("Album" + slskd.SLOT_DEFERRED_SUFFIX), "keep.flac")
        deferred = user_dir / "02 B.flac"
        deferred.write_bytes(b"x")
        with mock.patch.object(acq.composite_workflows, "move_staging_file", side_effect=_move):
            acq._separate_deferred_slot_files(str(user_dir), [str(deferred)], [])
        self.assertFalse((user_slot / slskd.APP_DOWNLOAD_MARKER).exists())


class CleanupIgnoresMarkerTests(_Roots):
    def test_source_cleanup_removes_marker_then_empty_folder(self):
        folder = self.dl / "Artist" / "Album (2021)"
        slskd.make_app_download_dir(folder)
        log = []
        with mock.patch.object(imp, "_move_artwork_to_target"), \
                mock.patch.object(imp.composite_workflows, "delete_file",
                                  side_effect=lambda p: os.unlink(p)) as delete, \
                mock.patch.object(imp.composite_workflows, "plan_folder_cleanup",
                                  return_value={"ok": True, "operation_id": "op"}) as plan, \
                mock.patch.object(imp.composite_workflows, "apply_folder_cleanup",
                                  return_value={"ok": True}):
            imp._folder_import_clean_source(log, str(folder), [1], False, [])
        delete.assert_called_once_with(str(folder / slskd.APP_DOWNLOAD_MARKER))
        plan.assert_called_once()
        self.assertIn("Source folder removed", "\n".join(log))


if __name__ == "__main__":
    unittest.main()
