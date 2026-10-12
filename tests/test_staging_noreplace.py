"""#230 follow-ups to the fd-relative staging move/delete (#206): a target
planted after the check is never replaced (S-3/N1), a source still being
written is never unlinked after a cross-filesystem copy (S-1), a parent made
concurrently is reused (N2), a delete never descends into another mount
(S-2), and refusals carry no absolute paths (N3). Temp directories only."""

import errno
import os
import shutil
import types
import unittest
from unittest import mock

import backend.composite_workflows as cw
from tests.test_staging_dirfd import DIR_FD, _failing_rename
from tests.test_wave0_s1_containment import _Env

_real_stat, _real_mkdir, _real_fstat = os.stat, os.mkdir, os.fstat


def _plant_after_absence_check(name, plant):
    """When the move looks for ``name`` and finds nothing, plant it right
    after -- the window between the check and the rename."""
    def hook(path, *a, **kw):
        try:
            return _real_stat(path, *a, **kw)
        except FileNotFoundError:
            if path == name and kw.get("dir_fd") is not None:
                plant()
            raise
    fake = mock.Mock(side_effect=hook)
    return mock.patch.multiple(os, stat=fake, supports_dir_fd=os.supports_dir_fd | {fake})


@unittest.skipUnless(DIR_FD, "needs dir_fd support")
class NoReplaceTests(_Env):
    def test_file_planted_at_target_is_not_replaced(self):
        (self.dl / "f.flac").write_bytes(b"staged")
        planted = self.dl / "out.flac"
        p_src = cw._validated_staging_target(self.dl / "f.flac", "move")
        p_dst = cw._validated_staging_target(planted, "move to")
        with _plant_after_absence_check("out.flac", lambda: planted.write_bytes(b"planted")):
            with self.assertRaises(OSError):
                cw._move_resolved(p_src, p_dst)
        self.assertEqual(planted.read_bytes(), b"planted")
        self.assertEqual((self.dl / "f.flac").read_bytes(), b"staged")

    def test_directory_planted_at_target_is_not_replaced(self):
        (self.dl / "d").mkdir()
        (self.dl / "d" / "a.flac").write_bytes(b"a")
        planted = self.dl / "out"

        def plant():
            planted.mkdir()
        p_src = cw._validated_staging_target(self.dl / "d", "move")
        p_dst = cw._validated_staging_target(planted, "move to")
        with _plant_after_absence_check("out", plant):
            with self.assertRaises(OSError):
                cw._move_resolved(p_src, p_dst)
        self.assertTrue((self.dl / "d" / "a.flac").exists())
        self.assertTrue(planted.is_dir())

    def test_plain_moves_still_work(self):
        (self.dl / "f.flac").write_bytes(b"x")
        (self.dl / "d").mkdir()
        (self.dl / "d" / "t.flac").write_bytes(b"t")
        cw._move_resolved(cw._validated_staging_target(self.dl / "f.flac", "move"),
                          cw._validated_staging_target(self.dl / "n" / "f.flac", "move to"))
        cw._move_resolved(cw._validated_staging_target(self.dl / "d", "move"),
                          cw._validated_staging_target(self.dl / "n" / "d", "move to"))
        self.assertEqual((self.dl / "n" / "f.flac").read_bytes(), b"x")
        self.assertEqual((self.dl / "n" / "d" / "t.flac").read_bytes(), b"t")
        self.assertFalse((self.dl / "f.flac").exists())
        self.assertFalse((self.dl / "d").exists())


@unittest.skipUnless(DIR_FD, "needs dir_fd support")
class CrossFsSourceStillWritingTests(_Env):
    def test_source_growing_during_copy_is_kept(self):
        src = self.dl / "f.flac"
        src.write_bytes(b"part1")
        real_copy = shutil.copyfileobj

        def copy_then_grow(fin, fout, *a):
            real_copy(fin, fout, *a)
            with open(src, "ab") as fh:  # a downloader still writing
                fh.write(b"part2")
        p_src = cw._validated_staging_target(src, "move")
        p_dst = cw._validated_staging_target(self.dl / "n" / "f.flac", "move to")
        with _failing_rename(OSError(errno.EXDEV, "cross-device")), \
                mock.patch.object(shutil, "copyfileobj", copy_then_grow):
            with self.assertRaises(ValueError):
                cw._move_resolved(p_src, p_dst)
        self.assertEqual(src.read_bytes(), b"part1part2")
        self.assertFalse((self.dl / "n" / "f.flac").exists())


@unittest.skipUnless(DIR_FD, "needs dir_fd support")
class ConcurrentParentTests(_Env):
    def test_parent_made_concurrently_is_reused(self):
        (self.dl / "f.flac").write_bytes(b"x")

        def racing_mkdir(name, *a, **kw):
            _real_mkdir(name, *a, **kw)  # the other move wins
            raise FileExistsError(errno.EEXIST, "exists")
        fake = mock.Mock(side_effect=racing_mkdir)
        p_src = cw._validated_staging_target(self.dl / "f.flac", "move")
        p_dst = cw._validated_staging_target(self.dl / "n" / "f.flac", "move to")
        with mock.patch.multiple(os, mkdir=fake, supports_dir_fd=os.supports_dir_fd | {fake}):
            cw._move_resolved(p_src, p_dst)
        self.assertEqual((self.dl / "n" / "f.flac").read_bytes(), b"x")


@unittest.skipUnless(DIR_FD and hasattr(os, "fwalk"), "needs dir_fd support")
class OtherMountTests(_Env):
    def test_delete_refuses_a_folder_holding_another_mount(self):
        (self.dl / "p" / "mnt").mkdir(parents=True)
        (self.dl / "p" / "mnt" / "keep.flac").write_bytes(b"other fs")
        mnt_ino = _real_stat(self.dl / "p" / "mnt").st_ino

        def fstat(fd):
            st = _real_fstat(fd)
            if st.st_ino == mnt_ino:  # pretend p/mnt is a bind mount
                return types.SimpleNamespace(st_dev=st.st_dev + 1, st_ino=st.st_ino, st_mode=st.st_mode)
            return st
        target = cw._validated_staging_target(self.dl / "p", "delete")
        with mock.patch.object(os, "fstat", side_effect=fstat):
            with self.assertRaises(ValueError):
                cw._remove_resolved(target)
        self.assertTrue((self.dl / "p" / "mnt" / "keep.flac").exists())


class NoAbsolutePathInErrorsTests(_Env):
    def test_refusals_name_only_the_leaf(self):
        for path in (self.music / "song.flac", self.dl):
            with self.assertRaises(ValueError) as cm:
                cw._validated_staging_target(path, "delete")
            self.assertNotIn(str(self.root), str(cm.exception))


if __name__ == "__main__":
    unittest.main()
