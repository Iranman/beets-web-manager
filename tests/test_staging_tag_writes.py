"""DUP-1: write_staging_tags (the playlist pre-import tag hint) may only touch
regular files under a staging root -- never MUSIC_ROOT, protected data, a
traversal or symlink escape, or an entry swapped after validation."""

import os
import unittest
from unittest import mock

import mediafile

import backend.composite_workflows as cw
from backend import playlist_service
from tests.test_s1_containment_followup import CAN_SYMLINK
from tests.test_wave0_s1_containment import _Env
from tests.test_webmanager_replace_item_file import write_flac

FD_OK = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd


def _title(path):
    return mediafile.MediaFile(str(path)).title


class StagingTagWriteTests(_Env):
    def setUp(self):
        super().setUp()
        self.lib_file = self.music / "A" / "t.flac"
        self.lib_file.parent.mkdir()
        write_flac(self.lib_file)
        self.lib_bytes = self.lib_file.read_bytes()

    def assertLibraryUntouched(self):
        self.assertEqual(self.lib_file.read_bytes(), self.lib_bytes)

    @unittest.skipUnless(FD_OK, "fd-relative operations unavailable")
    def test_writes_tags_under_downloads_and_playlist_staging(self):
        for folder in (self.dl, self.data / "playlist_staging"):
            folder.mkdir(exist_ok=True)
            f = folder / "song.flac"
            write_flac(f)
            res = cw.write_staging_tags(str(f), {"title": "Hello", "artist": "X"})
            self.assertTrue(res["ok"], res)
            self.assertEqual(_title(f), "Hello")

    def test_music_root_file_is_refused(self):
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(self.lib_file), {"title": "Bad"})
        self.assertLibraryUntouched()

    def test_traversal_out_of_downloads_is_refused(self):
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(self.dl / ".." / "music" / "A" / "t.flac"), {"title": "Bad"})
        self.assertLibraryUntouched()

    def test_path_outside_every_root_is_refused(self):
        other = self.root / "elsewhere.flac"
        write_flac(other)
        before = other.read_bytes()
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(other), {"title": "Bad"})
        self.assertEqual(other.read_bytes(), before)

    def test_protected_data_is_refused(self):
        db = self.data / "transactions.db"
        db.write_bytes(b"db")
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(db), {"title": "Bad"})
        self.assertEqual(db.read_bytes(), b"db")

    def test_directory_or_missing_file_is_not_written(self):
        (self.dl / "dir").mkdir()
        for target in (self.dl / "dir", self.dl / "missing.flac"):
            with self.assertRaises(FileNotFoundError):
                cw.write_staging_tags(str(target), {"title": "Bad"})

    @unittest.skipUnless(FD_OK, "fd-relative operations unavailable")
    def test_hardlink_to_library_file_is_refused(self):
        try:
            os.link(self.lib_file, self.dl / "seed.flac")
        except OSError:
            self.skipTest("hard links unavailable")
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(self.dl / "seed.flac"), {"title": "Bad"})
        self.assertLibraryUntouched()

    @unittest.skipUnless(CAN_SYMLINK, "symlinks unavailable")
    def test_symlinked_file_into_library_is_refused(self):
        os.symlink(self.lib_file, self.dl / "link.flac")
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(self.dl / "link.flac"), {"title": "Bad"})
        self.assertLibraryUntouched()

    @unittest.skipUnless(CAN_SYMLINK, "symlinks unavailable")
    def test_symlinked_directory_into_library_is_refused(self):
        os.symlink(self.music / "A", self.dl / "linkdir", target_is_directory=True)
        with self.assertRaises(ValueError):
            cw.write_staging_tags(str(self.dl / "linkdir" / "t.flac"), {"title": "Bad"})
        self.assertLibraryUntouched()

    @unittest.skipUnless(CAN_SYMLINK and FD_OK, "symlinks or fd-relative operations unavailable")
    def test_file_swapped_for_symlink_after_validation_is_refused(self):
        f = self.dl / "song.flac"
        write_flac(f)
        real = cw._validated_staging_target

        def validate_then_swap(path, what):
            resolved = real(path, what)
            os.rename(f, self.dl / "song.orig")
            os.symlink(self.lib_file, f)
            return resolved

        with mock.patch.object(cw, "_validated_staging_target", validate_then_swap):
            with self.assertRaises(ValueError):
                cw.write_staging_tags(str(f), {"title": "Bad"})
        self.assertLibraryUntouched()

    @unittest.skipUnless(CAN_SYMLINK and FD_OK, "symlinks or fd-relative operations unavailable")
    def test_parent_swapped_for_symlink_after_validation_is_refused(self):
        sub = self.dl / "sub"
        sub.mkdir()
        write_flac(sub / "t.flac")
        real = cw._validated_staging_target

        def validate_then_swap(path, what):
            resolved = real(path, what)
            os.rename(sub, self.dl / "sub.orig")
            os.symlink(self.music / "A", sub, target_is_directory=True)
            return resolved

        with mock.patch.object(cw, "_validated_staging_target", validate_then_swap):
            with self.assertRaises(ValueError):
                cw.write_staging_tags(str(sub / "t.flac"), {"title": "Bad"})
        self.assertLibraryUntouched()

    def test_playlist_caller_logs_and_leaves_library_file_alone(self):
        lines = []
        playlist_service._playlist_stamp_download_tags(str(self.lib_file), "X", "Bad", lines.append)
        self.assertTrue(lines and "could not stamp playlist tags" in lines[0], lines)
        self.assertLibraryUntouched()


if __name__ == "__main__":
    unittest.main()
