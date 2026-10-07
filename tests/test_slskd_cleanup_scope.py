"""#277: failed-candidate cleanup deletes only this transfer's files in the
peer folder, never a same-named file belonging to another download."""
import tempfile
import unittest
from pathlib import Path

from backend.slskd import cleanup_failed_candidate_files

EXTS = [".flac", ".mp3"]


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


class CleanupScopeTests(unittest.TestCase):
    def _run(self, dl: Path, remotes):
        log = []
        return cleanup_failed_candidate_files(dl, "peer", remotes, EXTS, log, [dl]), log

    def test_same_named_file_in_other_download_folder_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp)
            own_full = _touch(dl / "peer" / "Music" / "Album" / "01.flac")
            own_flat = _touch(dl / "peer" / "Album" / "02.flac")
            other_flat = _touch(dl / "Album" / "01.flac")
            other_nested = _touch(dl / "Music" / "Album" / "02.flac")
            other_peer = _touch(dl / "otherpeer" / "Album" / "01.flac")

            removed, log = self._run(dl, [r"Music\Album\01.flac", r"Music\Album\02.flac"])

            self.assertEqual(removed, 2)
            self.assertFalse(own_full.exists())
            self.assertFalse(own_flat.exists())
            for survivor in (other_flat, other_nested, other_peer):
                self.assertTrue(survivor.exists(), survivor)
            self.assertIn("Removed 2 partial file", "\n".join(log))

    def test_same_name_in_another_folder_of_same_peer_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp)
            own = _touch(dl / "peer" / "Album A" / "01.flac")
            sibling = _touch(dl / "peer" / "Album A" / "Bonus" / "01.flac")
            other_album = _touch(dl / "peer" / "Album B" / "01.flac")

            removed, _ = self._run(dl, [r"Album A\01.flac"])

            self.assertEqual(removed, 1)
            self.assertFalse(own.exists())
            self.assertTrue(sibling.exists())
            self.assertTrue(other_album.exists())

    def test_non_audio_and_unqueued_files_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp)
            own = _touch(dl / "peer" / "Album" / "01.mp3")
            unqueued = _touch(dl / "peer" / "Album" / "03.mp3")
            cover = _touch(dl / "peer" / "Album" / "cover.jpg")

            removed, _ = self._run(dl, ["Album/01.mp3", "Album/cover.jpg"])

            self.assertEqual(removed, 1)
            self.assertFalse(own.exists())
            self.assertTrue(unqueued.exists())
            self.assertTrue(cover.exists())


if __name__ == "__main__":
    unittest.main()
