"""SEC-007 / #325 (ARCH-030 Finding A): the recovery staging copy in
``backend.slskd.stage_selected_audio_files`` must be symlink-safe.

ARCH-030 made the staged folder name PREDICTABLE (``rec-<16hex>``) and added
reuse-overwrite, so a symlink pre-planted at a per-file dest -- or at a
``CD NN`` disc subdir -- by a local writer to the app-private staging dir, or
via a race between recovery retries, must never be followed by the copy. These
tests prove the pre-planted symlink is replaced / refused and its target is
left untouched. On hosts without dir_fd support staging fails closed (a
RuntimeError), which is acceptable; the tests skip there.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import backend.slskd as slskd


def _symlinks_work() -> bool:
    d = tempfile.mkdtemp()
    try:
        tgt = os.path.join(d, "t")
        os.mkdir(tgt)
        os.symlink(tgt, os.path.join(d, "l"), target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


FD = slskd._staging_fd_supported()


@unittest.skipUnless(FD and _symlinks_work(),
                     "needs O_NOFOLLOW/dir_fd support and working symlinks")
class StagingSymlinkSafetyTests(unittest.TestCase):
    PREFIX = "rec0123456789abcd"  # deterministic -> reuse/overwrite path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.downloads = root / "downloads"
        self.downloads.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.src = self.downloads / "src"
        self.src.mkdir()
        # The attacker's target, OUTSIDE the staged folder, must never be written.
        self.secret = self.outside / "secret.txt"
        self.secret.write_bytes(b"SECRET")
        safe_album = "Artist - Album"
        self.stage = self.downloads / "_beets_missing_import" / f"{self.PREFIX}-{safe_album}"
        self.stage.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, name, data=b"realbytes"):
        p = self.src / name
        p.write_bytes(data)
        return p

    def _stage(self, files, targets):
        return slskd.stage_selected_audio_files(
            self.downloads, [".flac"], str(self.src), files, "Artist", "Album",
            [], stage_prefix=self.PREFIX, target_tracks=targets)

    def test_symlink_pre_planted_at_dest_file_is_replaced_not_followed(self):
        f = self._mk("a.flac", b"DOWNLOADED")
        # Pre-plant a symlink at the predictable dest, pointing at the secret.
        dest = self.stage / "01 One.flac"
        os.symlink(str(self.secret), str(dest))

        out = self._stage([f], [{"track": 1, "disc": 1, "title": "One"}])

        self.assertEqual(Path(out), self.stage)
        # The secret (the symlink target) was NOT overwritten with the download.
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        # The dest is now a real regular file holding the downloaded bytes.
        self.assertFalse(dest.is_symlink())
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"DOWNLOADED")

    def test_symlink_pre_planted_at_intermediate_staging_dir_is_refused(self):
        # F1/C5: O_NOFOLLOW guards ONLY a path's final component, so the
        # intermediate ``_beets_missing_import`` dir must be descended
        # fd-relative from the downloads root, never followed in a single
        # os.open of the full stage path. Point it at a decoy that is still
        # UNDER the downloads root, so the separate resolve() containment check
        # PASSES and only the fd-walk's O_NOFOLLOW can refuse the swap.
        f = self._mk("c.flac", b"DOWNLOADED")
        mi = self.downloads / "_beets_missing_import"
        shutil.rmtree(mi)
        decoy = self.downloads / "decoy"
        decoy.mkdir()
        os.symlink(str(decoy), str(mi), target_is_directory=True)

        with self.assertRaises(RuntimeError):
            self._stage([f], [{"track": 3, "disc": 1, "title": "Three"}])

        # The download was NOT written through the symlinked intermediate.
        self.assertEqual(list(decoy.rglob("*.flac")), [])

    def test_symlink_pre_planted_at_cd_disc_subdir_is_refused(self):
        f = self._mk("b.flac", b"DOWNLOADED")
        evil = self.outside / "evil_dir"
        evil.mkdir()
        # Pre-plant the disc subdir as a symlink to an outside directory.
        os.symlink(str(evil), str(self.stage / "CD 02"), target_is_directory=True)

        with self.assertRaises(RuntimeError):
            self._stage([f], [{"track": 2, "disc": 2, "title": "Two"}])

        # Nothing was written through the symlink into the outside directory.
        self.assertEqual(list(evil.iterdir()), [])


@unittest.skipUnless(FD and _symlinks_work(),
                     "needs O_NOFOLLOW/dir_fd support and working symlinks")
class SidecarNofollowTests(unittest.TestCase):
    """ARCH-030 F4: the original-names sidecar I/O (``_write_text_nofollow`` /
    ``_read_text_nofollow``) must descend fd-relative so neither an intermediate
    parent-dir symlink swap nor a planted symlink/hardlink at the sidecar leaf is
    followed or truncated. Previously both opened the full path with O_NOFOLLOW
    (guards only the final component) and the write used O_TRUNC (truncates a
    planted hardlink)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.stage = root / "stage"
        self.stage.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_bytes(b"SECRET")
        self.sidecar = self.stage / ".sidecar.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_at_symlinked_leaf_replaces_not_followed(self):
        os.symlink(str(self.secret), str(self.sidecar))
        slskd._write_text_nofollow(self.sidecar, "DATA")
        self.assertEqual(self.secret.read_bytes(), b"SECRET",
                         "the symlink target must not be written through")
        self.assertFalse(self.sidecar.is_symlink())
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"), "DATA")

    def test_write_does_not_truncate_planted_hardlink(self):
        os.link(str(self.secret), str(self.sidecar))  # hardlink: shares the inode
        slskd._write_text_nofollow(self.sidecar, "DATA")
        # temp+rename gives the sidecar name a NEW inode; the other link keeps its
        # content (O_TRUNC would have zeroed the shared inode).
        self.assertEqual(self.secret.read_bytes(), b"SECRET")
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"), "DATA")

    def test_write_refuses_symlinked_parent_dir(self):
        decoy = Path(self.tmp.name) / "decoy"
        decoy.mkdir()
        linkdir = Path(self.tmp.name) / "linkdir"
        os.symlink(str(decoy), str(linkdir), target_is_directory=True)
        with self.assertRaises(OSError):
            slskd._write_text_nofollow(linkdir / ".sidecar.json", "DATA")
        self.assertEqual(list(decoy.iterdir()), [],
                         "nothing written through the symlinked parent dir")

    def test_read_refuses_symlinked_leaf(self):
        os.symlink(str(self.secret), str(self.sidecar))
        with self.assertRaises(OSError):
            slskd._read_text_nofollow(self.sidecar)


@unittest.skipIf(FD, "exercises the no-dir_fd fail-closed branch only")
class StagingFailClosedTests(unittest.TestCase):
    def test_refuses_when_fd_support_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d) / "downloads"
            src = downloads / "src"
            src.mkdir(parents=True)
            f = src / "a.flac"
            f.write_bytes(b"x")
            with self.assertRaises(RuntimeError):
                slskd.stage_selected_audio_files(
                    downloads, [".flac"], str(src), [f], "Artist", "Album",
                    [], stage_prefix="rec0123456789abcd",
                    target_tracks=[{"track": 1, "disc": 1, "title": "One"}])


if __name__ == "__main__":
    unittest.main()
