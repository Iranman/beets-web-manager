"""A linked ``_beets_missing_import`` root holds none of a qBittorrent unit's
stages: listing it would let stage cleanup remove folders outside the
downloads root."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.ai_service as ai


def _link_dir(target: Path, link: Path) -> str:
    """Symlink, else (Windows without the privilege) a junction; '' if neither."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
        return "junction"
    except (ImportError, AttributeError, OSError):
        return ""


class StageRootLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dl = Path(tmp.name, "downloads")
        self.outside = Path(tmp.name, "outside")
        self.dl.mkdir()
        self.victim = self.outside / (ai._ai_batch_qbit_stage_prefix("fid") + "-x")
        self.victim.mkdir(parents=True)
        p = mock.patch.object(ai, "validated_downloads_root", return_value=self.dl)
        p.start()
        self.addCleanup(p.stop)

    def test_real_stage_root_lists_the_unit_stage(self):
        (self.dl / "_beets_missing_import").mkdir()
        stage = self.dl / "_beets_missing_import" / self.victim.name
        stage.mkdir()
        self.assertEqual(ai._ai_batch_qbit_stage_dirs("fid"), [str(stage)])

    def test_linked_stage_root_lists_nothing_and_cleanup_deletes_nothing(self):
        if not _link_dir(self.outside, self.dl / "_beets_missing_import"):
            self.skipTest("neither a symlink nor a junction can be created here")
        self.assertEqual(ai._ai_batch_qbit_stage_dirs("fid"), [])
        cleanup = mock.Mock()
        ai._ai_batch_qbit_stage_cleanup("fid", cleanup, [])
        cleanup.assert_not_called()
        self.assertTrue(self.victim.is_dir())


if __name__ == "__main__":
    unittest.main()
