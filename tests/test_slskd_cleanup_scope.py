"""#277: failed-candidate cleanup deletes only files proven to be this
transfer's, never a same-named file belonging to another download."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from backend.slskd import QueuedRemote, cleanup_failed_candidate_files

EXTS = [".flac", ".mp3"]


def _touch(path: Path, data: bytes = b"x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _ticks(epoch: float) -> int:
    """.NET DateTime.UtcNow.Ticks for an epoch time (slskd's rename suffix)."""
    return int(epoch * 10_000_000) + 621355968000000000


class CleanupScopeTests(unittest.TestCase):
    def _run(self, dl: Path, remotes):
        # The real queue gives every remote its queued size and queue time.
        log, queued_at = [], time.time() - 60
        remotes = [QueuedRemote(r, 1, queued_at) for r in remotes]
        return cleanup_failed_candidate_files(dl, "peer", remotes, EXTS, log, [dl]), log

    def test_same_named_file_in_other_download_folder_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp)
            own_full = _touch(dl / "peer" / "Music" / "Album" / "01.flac")
            own_flat = _touch(dl / "peer" / "Album" / "02.flac")
            other_flat = _touch(dl / "Album" / "01.flac", b"other")
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


class DefaultLayoutEvidenceTests(unittest.TestCase):
    """slskd's default layout <dl>/<remote folder>/<file> has no peer folder:
    a file there is removed only with the queued size and a write time at or
    after the queue time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dl = Path(self.tmp.name)
        self.queued_at = time.time() - 60

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, remotes):
        log = []
        return cleanup_failed_candidate_files(self.dl, "peer", remotes, EXTS, log, [self.dl]), log

    def _q(self, name, size=4):
        return QueuedRemote(name, size, self.queued_at)

    def test_own_flat_files_are_removed(self):
        own = [_touch(self.dl / "Album" / n, b"abcd") for n in ("01.flac", "02.flac")]
        removed, _ = self._run([self._q(r"Music\Album\01.flac"), self._q(r"Music\Album\02.flac")])
        self.assertEqual(removed, 2)
        for p in own:
            self.assertFalse(p.exists(), p)

    def test_flat_file_with_other_size_survives_and_is_logged(self):
        other = _touch(self.dl / "Album" / "01.flac", b"abcdef")
        removed, log = self._run([self._q(r"Music\Album\01.flac")])
        self.assertEqual(removed, 0)
        self.assertTrue(other.exists())
        self.assertIn(repr(str(Path("Album") / "01.flac")), "\n".join(log))

    def test_flat_file_older_than_queue_time_survives(self):
        other = _touch(self.dl / "Album" / "01.flac", b"abcd", mtime=self.queued_at - 3600)
        removed, _ = self._run([self._q(r"Music\Album\01.flac")])
        self.assertEqual(removed, 0)
        self.assertTrue(other.exists())

    def test_flat_file_without_evidence_survives(self):
        other = _touch(self.dl / "Album" / "01.flac", b"abcd")
        no_size = QueuedRemote(r"Music\Album\01.flac", 0, self.queued_at)
        removed, log = self._run([r"Music\Album\01.flac", no_size])
        self.assertEqual(removed, 0)
        self.assertTrue(other.exists())
        self.assertIn("Left", "\n".join(log))

    def test_own_renamed_copy_is_removed_and_original_survives(self):
        original = _touch(self.dl / "Album" / "01.flac", b"zz", mtime=self.queued_at - 3600)
        renamed = _touch(self.dl / "Album" / f"01_{_ticks(self.queued_at + 5)}.flac", b"abcd")
        older_copy = _touch(self.dl / "Album" / f"01_{_ticks(self.queued_at - 600)}.flac", b"abcd")
        wrong_size = _touch(self.dl / "Album" / f"01_{_ticks(self.queued_at + 9)}.flac", b"abcdef")
        removed, _ = self._run([self._q(r"Music\Album\01.flac")])
        self.assertEqual(removed, 1)
        self.assertFalse(renamed.exists())
        for p in (original, older_copy, wrong_size):
            self.assertTrue(p.exists(), p)

    def test_peer_folder_file_also_needs_proof(self):
        """The peer name is peer-chosen: <dl>/<peer> can be another
        download's album folder, so the peer-folder forms need proof too."""
        foreign = _touch(self.dl / "peer" / "01.flac", b"abcdef")
        own = _touch(self.dl / "peer" / "Album" / "01.flac", b"abcd")
        removed, log = self._run([self._q("01.flac"), self._q(r"Music\Album\01.flac")])
        self.assertEqual(removed, 1)
        self.assertTrue(foreign.exists())
        self.assertFalse(own.exists())
        self.assertIn(repr(str(Path("peer") / "01.flac")), "\n".join(log))

    def test_renamed_copy_in_peer_folder_is_removed(self):
        renamed = _touch(self.dl / "peer" / "Music" / "Album" / f"01_{_ticks(self.queued_at + 5)}.flac", b"abcd")
        removed, _ = self._run([self._q(r"Music\Album\01.flac")])
        self.assertEqual(removed, 1)
        self.assertFalse(renamed.exists())


class QueueEvidenceTests(unittest.TestCase):
    def test_search_and_queue_returns_size_and_queue_time(self):
        from tests.test_slskd_peer_path_containment import SearchAndQueueContainmentTests, _Tree
        with tempfile.TemporaryDirectory() as tmp:
            before = time.time()
            result, _posts, _log = SearchAndQueueContainmentTests._run(self, _Tree(tmp), "peer", "Music/Album")
            _user, queued, _expected, _remote = result
            self.assertEqual(list(queued), ["Music/Album/01 Song.flac"])
            self.assertIsInstance(queued[0], QueuedRemote)
            self.assertEqual(queued[0].size, 10)
            self.assertGreaterEqual(queued[0].queued_at, before)


if __name__ == "__main__":
    unittest.main()
