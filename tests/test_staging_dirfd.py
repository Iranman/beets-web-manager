"""#206 F1/F2/F5: staging delete/move are fd-relative from the staging root,
leave no stray directories behind, and never return absolute paths to
clients. Temp directories only; Linux (dir_fd) semantics."""

import contextlib
import errno
import os
import shutil
import unittest
from unittest import mock

import backend.composite_workflows as cw
from tests.test_s1_containment_followup import CAN_SYMLINK
from tests.test_wave0_s1_containment import _Env

DIR_FD = os.rename in os.supports_dir_fd and shutil.rmtree.avoids_symlink_attacks


def _swap_on_check(parent, victim_root):
    """Patch _same_entry -- the last check before the destructive call -- so
    that ``parent`` is swapped for a symlink to ``victim_root`` right then."""
    real = cw._same_entry

    def hook(before, now):
        if not os.path.islink(parent):
            os.rename(parent, str(parent) + ".orig")
            os.symlink(victim_root, parent, target_is_directory=True)
        return real(before, now)
    return mock.patch.object(cw, "_same_entry", side_effect=hook)


def _failing_rename(exc):
    """Patch os.rename to raise ``exc`` while keeping it in supports_dir_fd."""
    fake = mock.Mock(side_effect=exc)
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {fake}))
    stack.enter_context(mock.patch("os.rename", fake))
    return stack


def _tree(root):
    return sorted(os.path.relpath(os.path.join(d, n), root)
                  for d, ds, fs in os.walk(root) for n in ds + fs)


@unittest.skipUnless(CAN_SYMLINK and DIR_FD, "needs symlinks and dir_fd support")
class ParentSwapTests(_Env):
    def test_delete_does_not_follow_parent_swapped_after_check(self):
        (self.dl / "p" / "leaf").mkdir(parents=True)
        (self.dl / "p" / "leaf" / "x.flac").write_bytes(b"x")
        (self.music / "leaf").mkdir()
        (self.music / "leaf" / "song.flac").write_bytes(b"library")
        target = cw._validated_staging_target(self.dl / "p" / "leaf", "delete")
        with _swap_on_check(self.dl / "p", self.music):
            cw._remove_resolved(target)
        self.assertTrue((self.music / "leaf" / "song.flac").exists())
        self.assertFalse((self.dl / "p.orig" / "leaf").exists())

    def test_move_does_not_create_or_land_in_swapped_target_parent(self):
        (self.dl / "f.flac").write_bytes(b"a")
        (self.dl / "a").mkdir()
        before = _tree(self.music)
        p_src = cw._validated_staging_target(self.dl / "f.flac", "move")
        p_dst = cw._validated_staging_target(self.dl / "a" / "sub" / "f.flac", "move to")
        with _swap_on_check(self.dl / "a", self.music):
            with self.assertRaises(ValueError):
                cw._move_resolved(p_src, p_dst)
        self.assertEqual(_tree(self.music), before)
        self.assertTrue((self.dl / "f.flac").exists())
        self.assertEqual(_tree(self.dl / "a.orig"), [])


@unittest.skipUnless(DIR_FD, "needs dir_fd support")
class NoStrayDirTests(_Env):
    def test_failed_move_removes_parents_it_created(self):
        (self.dl / "f.flac").write_bytes(b"a")
        p_src = cw._validated_staging_target(self.dl / "f.flac", "move")
        p_dst = cw._validated_staging_target(self.dl / "x" / "y" / "z" / "f.flac", "move to")
        with _failing_rename(PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(OSError):
                cw._move_resolved(p_src, p_dst)
        self.assertFalse((self.dl / "x").exists())
        self.assertTrue((self.dl / "f.flac").exists())

    def test_refused_source_creates_nothing(self):
        (self.dl / "f.flac").write_bytes(b"a")
        p_src = cw._validated_staging_target(self.dl / "f.flac", "move")
        p_dst = cw._validated_staging_target(self.dl / "x" / "y" / "f.flac", "move to")
        (self.dl / "other").write_bytes(b"other")
        os.replace(self.dl / "other", self.dl / "f.flac")
        with self.assertRaises(ValueError):
            cw._move_resolved(p_src, p_dst)
        self.assertFalse((self.dl / "x").exists())


@unittest.skipUnless(DIR_FD, "needs dir_fd support")
class CrossDeviceMoveTests(_Env):
    def _exdev(self):
        return _failing_rename(OSError(errno.EXDEV, "cross-device"))

    def test_file_is_copied_then_unlinked(self):
        src = self.dl / "f.flac"
        src.write_bytes(b"audio" * 1000)
        os.chmod(src, 0o640)
        os.utime(src, ns=(1_000_000_000, 2_000_000_000))
        p_src = cw._validated_staging_target(src, "move")
        p_dst = cw._validated_staging_target(self.dl / "n" / "f.flac", "move to")
        with self._exdev():
            cw._move_resolved(p_src, p_dst)
        dst = self.dl / "n" / "f.flac"
        self.assertFalse(src.exists())
        self.assertEqual(dst.read_bytes(), b"audio" * 1000)
        self.assertEqual(os.stat(dst).st_mode & 0o777, 0o640)
        self.assertEqual(os.stat(dst).st_mtime_ns, 2_000_000_000)

    def test_directory_is_refused_and_nothing_moves(self):
        (self.dl / "d").mkdir()
        (self.dl / "d" / "a.flac").write_bytes(b"a")
        p_src = cw._validated_staging_target(self.dl / "d", "move")
        p_dst = cw._validated_staging_target(self.dl / "n" / "d", "move to")
        with self._exdev():
            with self.assertRaises(ValueError):
                cw._move_resolved(p_src, p_dst)
        self.assertTrue((self.dl / "d" / "a.flac").exists())
        self.assertFalse((self.dl / "n").exists())


class FailClosedTests(_Env):
    def test_no_dir_fd_support_refuses_without_touching(self):
        (self.dl / "f.flac").write_bytes(b"a")
        target = cw._validated_staging_target(self.dl / "f.flac", "delete")
        with mock.patch.object(shutil.rmtree, "avoids_symlink_attacks", False):
            with self.assertRaises(ValueError):
                cw._remove_resolved(target)
        self.assertTrue((self.dl / "f.flac").exists())


class StagedTrackErrorTests(_Env):
    """F5: the client error is fixed text; the path is only in the log."""

    def test_refused_delete_hides_path(self):
        song = self.music / "song.flac"
        song.write_bytes(b"x")
        with self.assertLogs("beets.workflows", "WARNING") as logs:
            res = cw.delete_playlist_staged_track("k", "t1", str(song))
        self.assertEqual(res, {"ok": False, "deleted": False, "track_id": "t1",
                               "error": "Could not delete the staged track file."})
        self.assertIn(str(song), "\n".join(logs.output))
        self.assertTrue(song.exists())


if __name__ == "__main__":
    unittest.main()
