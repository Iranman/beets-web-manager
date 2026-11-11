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
        self.data = self.tmp / "data"  # WEB_MANAGER_DATA_DIR: holds the marker key
        env = mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": str(self.data)})
        env.start()
        self.addCleanup(env.stop)

    def require_fd(self):
        if not slskd._staging_fd_supported():
            self.skipTest("markers need the no-follow fd primitives (POSIX)")

    def audio(self, folder, name="01.flac"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x")
        return folder

    def app_folder(self, rel):
        self.require_fd()
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
        self.require_fd()
        name ="A - soundcloud missing 1793260626691__slot_deferred"
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

    def test_folder_reached_through_an_outside_symlink_is_no_proof(self):
        """Only the walked path's own location counts: a symlink outside the
        downloads root into an app folder (any depth) never proves it."""
        self.app_folder("Artist/Album (2021)")
        _symlink(self.dl / "Artist" / "Album (2021)", self.outside / "deep", True)
        _symlink(self.dl, self.outside / "dl", True)
        self.assertFalse(self.managed(self.outside / "deep"))
        self.assertFalse(self.managed(self.outside / "dl" / "Artist" / "Album (2021)"))
        self.assertTrue(self.managed(self.dl / "Artist" / "Album (2021)"))

    def test_symlink_inside_the_root_is_walked_at_every_level(self):
        """A path that reads as an app folder but resolves to a user folder
        ("..", or a link back up the tree) never borrows the app folder's marker."""
        self.app_folder("owned")
        self.audio(self.dl / "a" / "b" / "owned")  # the user's, unmarked
        (self.dl / "a" / "b" / "c").mkdir()
        _symlink(self.dl / "a" / "b" / "c", self.dl / "jump", True)
        _symlink(self.dl, self.dl / "a" / "back", True)
        self.assertFalse(self.managed(self.dl / "jump" / ".." / "owned"))
        self.assertFalse(self.managed(self.dl / "a" / "back" / "owned"))
        self.assertFalse(self.managed(self.dl / "a" / "b" / "owned"))
        self.assertTrue(self.managed(self.dl / "owned"))

    def test_symlinked_downloads_root_still_proves_by_either_spelling(self):
        self.require_fd()
        real = self.tmp / "real-dl"
        real.mkdir()
        alias = self.tmp / "dl-alias"
        _symlink(real, alias, True)
        with mock.patch.object(lib, "DOWNLOADS_ALLOWED_ROOTS", [alias]), \
                mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", [alias]):
            slskd.make_app_download_dir(alias / "Album (2021)")
            self.assertTrue(self.managed(alias / "Album (2021)"))
            self.assertTrue(self.managed(real / "Album (2021)"))

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

    def setUp(self):
        super().setUp()
        self.require_fd()

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
    def _clean(self, folder):
        log = []
        with mock.patch.object(imp, "_move_artwork_to_target"), \
                mock.patch.object(imp.composite_workflows, "delete_file",
                                  side_effect=lambda p: os.unlink(p)) as delete, \
                mock.patch.object(imp.composite_workflows, "plan_folder_cleanup",
                                  return_value={"ok": True, "operation_id": "op"}) as plan, \
                mock.patch.object(imp.composite_workflows, "apply_folder_cleanup",
                                  return_value={"ok": True}):
            imp._folder_import_clean_source(log, str(folder), [1], False, [])
        return log, delete, plan

    def test_source_cleanup_removes_marker_then_empty_folder(self):
        self.require_fd()
        folder = self.dl / "Artist" / "Album (2021)"
        slskd.make_app_download_dir(folder)
        log, delete, plan = self._clean(folder)
        delete.assert_called_once_with(str(folder / slskd.APP_DOWNLOAD_MARKER))
        plan.assert_called_once()
        self.assertIn("Source folder removed", "\n".join(log))

    def test_forged_marker_is_content_and_keeps_the_folder(self):
        folder = self.dl / "Torrents" / "Album"
        folder.mkdir(parents=True)
        (folder / slskd.APP_DOWNLOAD_MARKER).write_text("beets-web-manager\n")
        log, delete, plan = self._clean(folder)
        delete.assert_not_called()
        plan.assert_not_called()
        self.assertTrue((folder / slskd.APP_DOWNLOAD_MARKER).is_file())

    def test_marker_that_is_no_longer_a_regular_file_is_never_deleted(self):
        """F6: lstat right before the delete; a directory is never removed."""
        folder = self.dl / "Artist" / "Album (2021)"
        (folder / slskd.APP_DOWNLOAD_MARKER / "inner").mkdir(parents=True)
        with mock.patch.object(imp, "app_download_marker_valid", return_value=True), \
                mock.patch.object(Path, "rglob", lambda self, pat: iter([folder / slskd.APP_DOWNLOAD_MARKER])), \
                mock.patch.object(Path, "is_file", lambda self: True):
            log, delete, plan = self._clean(folder)
        delete.assert_not_called()
        self.assertTrue((folder / slskd.APP_DOWNLOAD_MARKER / "inner").is_dir())


class MarkerForgeryTests(_Roots):
    """#325 F1: a marker shipped by downloaded content proves nothing."""

    FORGED = ("beets-web-manager\n", "0" * 64 + "\n", "")

    def plant(self, folder, text):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / slskd.APP_DOWNLOAD_MARKER).write_text(text)

    def assert_preserved(self, folder):
        self.assertFalse(self.managed(folder))
        self.assertTrue(lib._preserve_torrent_source_path(folder))
        self.assertTrue(lib._preserve_torrent_source_file(folder / "01.flac"))

    def test_forged_marker_in_torrent_folder(self):
        for text in self.FORGED:
            folder = self.audio(self.dl / "Torrents" / f"Album {len(text)}")
            self.plant(folder, text)
            self.assert_preserved(folder)

    def test_forged_marker_in_category_folder(self):
        for text in self.FORGED:
            cat = self.dl / f"music-{len(text)}"
            self.plant(cat, text)
            self.assert_preserved(self.audio(cat / "Artist - Album"))

    def test_forged_marker_at_torrent_source_root(self):
        qbit = self.dl / "qbittorrent"
        with mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", [qbit]):
            for text in self.FORGED:
                self.plant(qbit, text)
                self.assert_preserved(self.audio(qbit / "Artist - Album"))
                self.assert_preserved(self.audio(qbit / "music" / "Artist - Album"))

    def test_valid_marker_at_or_above_a_torrent_root_is_no_proof(self):
        self.require_fd()
        qbit = self.dl / "qbittorrent"
        qbit.mkdir()
        self.plant(qbit, slskd.app_download_marker_text(qbit, create_key=True))
        self.assertTrue(slskd.app_download_marker_valid(qbit))
        with mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", [qbit / "complete"]):
            torrent = self.audio(qbit / "complete" / "Artist - Album")
            self.assert_preserved(torrent)
        with mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", [qbit]):
            self.assert_preserved(torrent)

    def test_copied_marker_from_a_real_app_folder_is_no_proof(self):
        app = self.app_folder("Artist/Album (2021)")
        self.assertTrue(self.managed(app))
        torrent = self.audio(self.dl / "Torrents" / "Album")
        shutil.copy2(app / slskd.APP_DOWNLOAD_MARKER, torrent / slskd.APP_DOWNLOAD_MARKER)
        self.assert_preserved(torrent)

    def test_marker_without_the_install_key_is_no_proof(self):
        self.require_fd()
        app = self.app_folder("Artist/Album (2021)")
        slskd.app_download_key_path().unlink()
        self.assertFalse(self.managed(app))
        # A fresh key does not revive markers written under the old one.
        slskd.app_download_marker_text(self.outside, create_key=True)
        self.assertFalse(self.managed(app))

    def test_symlinked_key_file_is_never_used(self):
        self.require_fd()
        planted = self.dl / "planted-key"
        planted.write_text("a" * 64 + "\n")
        self.data.mkdir()
        _symlink(planted, slskd.app_download_key_path(), False)
        folder = self.audio(self.dl / "Torrents" / "Album")
        import hashlib, hmac
        forged = hmac.new(b"a" * 64, b"bwm-app-download\x00" + os.fsencode(os.path.realpath(folder)),
                          hashlib.sha256).hexdigest()
        self.plant(folder, forged)
        self.assert_preserved(folder)
        made = self.dl / "Artist" / "New"
        slskd.make_app_download_dir(made)
        self.assertFalse((made / slskd.APP_DOWNLOAD_MARKER).exists())

    def test_oversized_marker_is_no_proof(self):
        app = self.app_folder("Artist/Album (2021)")
        marker = app / slskd.APP_DOWNLOAD_MARKER
        marker.write_text(marker.read_text().strip() + " " * 200 + "\n")
        self.assertFalse(self.managed(app))

    def test_key_inside_a_downloads_root_is_never_trusted(self):
        app = self.app_folder("Artist/Album (2021)")
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": str(self.dl / "data")}):
            folder = self.dl / "Artist" / "Other"
            slskd.make_app_download_dir(folder)  # writes a key under downloads
            self.assertTrue(slskd.app_download_marker_valid(folder))
            self.assertFalse(self.managed(folder))
        self.assertTrue(self.managed(app))

    def test_key_file_is_private_and_stable(self):
        self.require_fd()
        first = slskd.app_download_marker_text(self.outside, create_key=True)
        self.assertEqual(slskd.app_download_marker_text(self.outside), first)
        self.assertEqual(os.stat(slskd.app_download_key_path()).st_mode & 0o777, 0o600)

    def test_real_app_folder_and_disc_subfolder_stay_managed(self):
        app = self.app_folder("Artist/Album (2021)")
        disc = self.audio(app / "CD1")
        for folder in (app, disc):
            self.assertTrue(self.managed(folder))
            self.assertFalse(lib._preserve_torrent_source_path(folder))

    def test_marker_write_fails_closed_without_nofollow_primitives(self):
        """F5: no plain-write fallback; the folder stays unmarked (the user's)."""
        folder = self.dl / "Artist" / "Album (2021)"
        with mock.patch.object(slskd, "_staging_fd_supported", return_value=False):
            slskd.make_app_download_dir(folder)
        self.assertTrue(folder.is_dir())
        self.assertFalse((folder / slskd.APP_DOWNLOAD_MARKER).exists())
        self.assertFalse(self.managed(folder))


class LegacyNameTests(_Roots):
    NAME = "A - soundcloud missing 1793260626691__slot_deferred"

    def test_legacy_name_proof_ends(self):
        folder = self.audio(self.dl / "Artist" / self.NAME)
        self.assertTrue(self.managed(folder))
        with mock.patch.object(lib, "APP_DOWNLOAD_LEGACY_NAME_UNTIL", lib.date.today()):
            self.assertFalse(self.managed(folder))

    def test_legacy_name_under_a_separate_torrent_root_is_no_proof(self):
        qbit = self.dl / "qbittorrent"
        folder = self.audio(qbit / self.NAME)
        with mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", [qbit]):
            self.assertFalse(self.managed(folder))
            self.assertTrue(lib._preserve_torrent_source_path(folder))



class PrivateRootOwnershipTests(_Roots):
    """#566: ``_beets_missing_import`` and playlist roots are locations, not
    ownership proof. A folder there is the app's only with a valid marker and
    no link on the walked path; otherwise it is the user's (copied, kept)."""

    def setUp(self):
        super().setUp()
        self.stage_root = self.dl / "_beets_missing_import"
        self.pl_in = self.dl / "Playlist Downloads"   # the default: under downloads
        self.pl_out = self.tmp / "playlists"          # configured outside downloads
        self.pl_in.mkdir()
        self.pl_out.mkdir()
        p = patch_app_family(APP, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", [self.pl_in, self.pl_out])
        p.start()
        self.addCleanup(p.stop)

    def test_unmarked_folder_under_staging_root_is_not_app_managed(self):
        folder = self.audio(self.stage_root / "planted-Album")
        self.assertFalse(self.managed(folder))
        self.assertFalse(self.managed(self.audio(folder / "CD 02")))
        self.assertTrue(lib._preserve_torrent_source_path(folder))
        self.assertTrue(lib._preserve_torrent_source_file(folder / "01.flac"))
        log = []
        self.assertFalse(imp._delete_staged_import_folder(str(folder), log))
        self.assertTrue((folder / "01.flac").is_file(), log)

    def test_unmarked_folder_under_a_playlist_root_is_not_app_managed(self):
        for root in (self.pl_in, self.pl_out):
            folder = self.audio(root / "key" / "downloads")
            self.assertFalse(self.managed(folder), root)
            self.assertFalse(self.managed(root / "key"), root)
        self.assertTrue(lib._preserve_torrent_source_path(self.pl_in / "key" / "downloads"))

    def test_unmarked_folder_under_a_playlist_root_outside_downloads_is_kept(self):
        """#583: unproven there too, so copied and kept, never moved."""
        folder = self.audio(self.pl_out / "key" / "downloads")
        self.assertTrue(lib._preserve_torrent_source_path(folder))
        self.assertTrue(lib._preserve_torrent_source_file(folder / "01.flac"))

    def test_resolved_staging_path_under_a_linked_downloads_root_gets_no_name_bridge(self):
        """#583: DOWNLOADS_ROOT set through a symlink; the resolved spelling of a
        legacy-named folder under _beets_missing_import is still private."""
        real = self.tmp / "real-dl"
        real.mkdir()
        alias = self.tmp / "dl-alias"
        _symlink(real, alias, True)
        name = slskd.direct_download_folder_name("A", "ytdlp", "1793260626691")
        folder = self.audio(real / "_beets_missing_import" / name)
        with mock.patch.object(lib, "DOWNLOADS_ALLOWED_ROOTS", [alias]), \
                mock.patch.object(lib, "TORRENT_SOURCE_ROOTS", [alias]):
            self.assertTrue(lib._in_app_private_root(folder))
            self.assertFalse(self.managed(folder))
            self.assertTrue(lib._preserve_torrent_source_path(folder))
            self.assertTrue(self.managed(real / "Artist" / name))  # the bridge holds elsewhere

    def test_private_roots_still_stay_out_of_a_user_scan(self):
        """Skipping moves nothing, so the location alone still excludes."""
        user = self.audio(self.dl / "Artist" / "User Album")
        self.audio(self.stage_root / "planted-Album")
        self.audio(self.pl_in / "key" / "downloads")
        self.assertTrue(lib._in_app_private_root(self.stage_root / "planted-Album"))
        self.assertTrue(lib._in_app_private_root(self.pl_out / "key"))
        self.assertFalse(lib._in_app_private_root(user))
        self.assertEqual(ai._ai_batch_find_audio_dirs(str(self.dl), []), [str(user)])
        roots = ai._qbit_source_roots()
        self.assertEqual(ai._qbit_source_dir_reason(str(self.stage_root / "planted-Album"), roots)[1],
                         "app_managed")

    def test_staging_root_linked_to_downloads_root_proves_nothing(self):
        user = self.audio(self.dl / "Artist" / "User Album")
        _symlink(self.dl, self.stage_root, True)
        via_link = self.stage_root / "Artist" / "User Album"
        self.assertFalse(self.managed(via_link))
        self.assertFalse(self.managed(user))
        self.assertTrue(lib._preserve_torrent_source_path(via_link))
        self.assertFalse(imp._delete_staged_import_folder(str(via_link), []))
        self.assertTrue((user / "01.flac").is_file())

    def test_staging_root_linked_to_a_marked_app_folder_proves_nothing(self):
        app = self.app_folder("Artist/Album (2021)")
        _symlink(app.parent, self.stage_root, True)
        self.assertFalse(self.managed(self.stage_root / "Album (2021)"))
        self.assertTrue(self.managed(app))

    def test_linked_component_under_a_playlist_root_proves_nothing(self):
        for root in (self.pl_in, self.pl_out):
            user = self.audio(self.dl / "Artist" / f"User {root.name}")
            _symlink(user.parent, root / "key", True)
            self.assertFalse(self.managed(root / "key" / user.name), root)
            self.assertFalse(self.managed(root / "key"), root)
        # Even a marked folder reached through the link proves nothing.
        app = self.app_folder("Owned/Album")
        _symlink(app.parent, self.pl_out / "owned", True)
        self.assertFalse(self.managed(self.pl_out / "owned" / "Album"))

    def test_app_staging_and_playlist_folders_are_marked_and_managed(self):
        self.require_fd()
        src = self.audio(self.outside / "src")
        staged = Path(slskd.stage_selected_audio_files(
            self.dl, {".flac"}, str(src), [src / "01.flac"], "Artist", "Album", [], force_stage=True))
        self.assertEqual(staged.parent, self.stage_root)
        self.assertTrue((staged / slskd.APP_DOWNLOAD_MARKER).is_file())
        self.assertTrue(self.managed(staged))
        self.assertFalse(lib._preserve_torrent_source_path(staged))
        for root in (self.pl_in, self.pl_out):
            dl_dir = root / "key" / "downloads"
            from backend import playlist_service
            copied = playlist_service._playlist_copy_source_files(
                [src / "01.flac"], dl_dir, "Artist", "Title", lambda *_: None)
            self.assertEqual(len(copied), 1, root)
            self.assertTrue(self.managed(dl_dir), root)
            self.assertTrue(self.managed(Path(copied[0]).parent), root)

    def test_preexisting_stage_folder_is_reused_but_never_marked(self):
        self.require_fd()
        src = self.audio(self.outside / "src")
        planted = self.audio(self.stage_root / "fixedprefix-Artist - Album", "user.flac")
        staged = Path(slskd.stage_selected_audio_files(
            self.dl, {".flac"}, str(src), [src / "01.flac"], "Artist", "Album", [],
            stage_prefix="fixedprefix", force_stage=True))
        self.assertEqual(staged, planted)
        self.assertFalse((planted / slskd.APP_DOWNLOAD_MARKER).exists())
        self.assertFalse(self.managed(planted))

    def test_legacy_generated_name_proves_nothing_under_private_roots(self):
        name = slskd.direct_download_folder_name("A", "ytdlp", "1793260626691")
        self.assertTrue(self.managed(self.dl / "Artist" / name))  # the bridge still holds elsewhere
        for root in (self.stage_root, self.pl_in, self.pl_out):
            self.assertFalse(self.managed(self.audio(root / name)), root)

    def test_cleanup_never_follows_a_link_to_a_marked_stage(self):
        self.require_fd()
        stage = self.stage_root / "real-stage"
        self.stage_root.mkdir()
        slskd.make_app_download_dir(stage)
        self.audio(stage)
        _symlink(stage, self.stage_root / "link", True)
        with mock.patch.object(imp, "_pending_review_has_path_or_overlap", return_value=False):
            self.assertFalse(imp._delete_staged_import_folder(str(self.stage_root / "link"), []))
        self.assertTrue((stage / "01.flac").is_file())

    def test_linked_staging_root_is_refused_before_creating_the_stage(self):
        target = self.dl / "elsewhere"
        target.mkdir()
        _symlink(target, self.stage_root, True)
        src = self.audio(self.outside / "src")
        with self.assertRaises(RuntimeError):
            slskd.stage_selected_audio_files(
                self.dl, {".flac"}, str(src), [src / "01.flac"], "Artist", "Album", [], force_stage=True)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
