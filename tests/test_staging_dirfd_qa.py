"""QA regression for #206 F1 (PR #225): a parent swapped for a symlink at the
moment of the destructive syscall itself (after every check has passed) must
not redirect the delete/move into MUSIC_ROOT. Path-based code fails these;
fd-relative code operates on the originally validated entry."""

import contextlib
import os
import shutil
import unittest
from unittest import mock

import backend.composite_workflows as cw
from tests.test_s1_containment_followup import CAN_SYMLINK
from tests.test_wave0_s1_containment import _Env

DIR_FD = os.rename in os.supports_dir_fd and shutil.rmtree.avoids_symlink_attacks
_real_rename, _real_symlink = os.rename, os.symlink


def _swap(parent, victim):
    if not os.path.islink(parent):
        _real_rename(parent, str(parent) + ".orig")
        _real_symlink(victim, parent, target_is_directory=True)


def _swap_at_syscall(target, attr, parent, victim):
    """Patch ``target.attr`` so ``parent`` is swapped right before the real call."""
    real = getattr(target, attr)

    def hook(*a, **kw):
        _swap(parent, victim)
        return real(*a, **kw)
    fake = mock.Mock(side_effect=hook)
    if hasattr(real, "avoids_symlink_attacks"):
        fake.avoids_symlink_attacks = real.avoids_symlink_attacks
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(os, "supports_dir_fd", os.supports_dir_fd | {fake}))
    stack.enter_context(mock.patch.object(target, attr, fake))
    return stack


@unittest.skipUnless(CAN_SYMLINK and DIR_FD, "needs symlinks and dir_fd support")
class SwapAtSyscallTests(_Env):
    def test_rmtree_with_grandparent_swapped(self):
        leaf = self.dl / "a" / "b" / "leaf"
        leaf.mkdir(parents=True)
        (leaf / "x.flac").write_bytes(b"x")
        (self.music / "b" / "leaf").mkdir(parents=True)
        (self.music / "b" / "leaf" / "song.flac").write_bytes(b"library")
        target = cw._validated_staging_target(leaf, "delete")
        with _swap_at_syscall(shutil, "rmtree", self.dl / "a", self.music):
            cw._remove_resolved(target)
        self.assertTrue((self.music / "b" / "leaf" / "song.flac").exists())
        self.assertFalse((self.dl / "a.orig" / "b" / "leaf").exists())

    def test_unlink_with_parent_swapped(self):
        (self.dl / "p").mkdir()
        (self.dl / "p" / "song.flac").write_bytes(b"staged")
        (self.music / "song.flac").write_bytes(b"library")
        target = cw._validated_staging_target(self.dl / "p" / "song.flac", "delete")
        with _swap_at_syscall(os, "unlink", self.dl / "p", self.music):
            cw._remove_resolved(target)
        self.assertTrue((self.music / "song.flac").exists(), "library file was deleted")
        self.assertFalse((self.dl / "p.orig" / "song.flac").exists())

    def test_move_source_parent_swapped_at_rename(self):
        (self.dl / "p").mkdir()
        (self.dl / "p" / "f.flac").write_bytes(b"staged")
        (self.music / "f.flac").write_bytes(b"library")
        p_src = cw._validated_staging_target(self.dl / "p" / "f.flac", "move")
        p_dst = cw._validated_staging_target(self.dl / "out.flac", "move to")
        with _swap_at_syscall(os, "rename", self.dl / "p", self.music):
            try:
                cw._move_resolved(p_src, p_dst)
            except (ValueError, OSError):
                pass
        self.assertTrue((self.music / "f.flac").exists(), "library file was moved out")
        if (self.dl / "out.flac").exists():
            self.assertEqual((self.dl / "out.flac").read_bytes(), b"staged")

    def test_move_target_parent_swapped_at_rename(self):
        (self.dl / "f.flac").write_bytes(b"staged")
        (self.dl / "a" / "sub").mkdir(parents=True)
        (self.music / "sub").mkdir()
        before = sorted(os.listdir(self.music / "sub"))
        p_src = cw._validated_staging_target(self.dl / "f.flac", "move")
        p_dst = cw._validated_staging_target(self.dl / "a" / "sub" / "f.flac", "move to")
        with _swap_at_syscall(os, "rename", self.dl / "a", self.music):
            try:
                cw._move_resolved(p_src, p_dst)
            except (ValueError, OSError):
                pass
        self.assertEqual(sorted(os.listdir(self.music / "sub")), before)


if __name__ == "__main__":
    unittest.main()
