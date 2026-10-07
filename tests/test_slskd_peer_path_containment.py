"""#248 / #251: peer-supplied slskd usernames and remote paths never reach a
path outside DOWNLOADS_ROOT, for scans or for failed-candidate unlinks, and
the wait for completed files is bounded.

Real temp tree: <base>/a/b/c/downloads is DOWNLOADS_ROOT and
<base>/a/srv/music/Artist/Album/01.flac stands in for a library file that
every hostile vector aims at and that must survive.
"""

import itertools
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.app_runtime as rt
import backend.slskd_service as slskd
from backend.slskd import _remote_path, cleanup_failed_candidate_files, safe_peer_username


class _Tree:
    def __init__(self, tmp: str):
        base = Path(tmp).resolve()
        self.downloads = base / "a" / "b" / "c" / "downloads"
        self.downloads.mkdir(parents=True)
        self.album = base / "a" / "srv" / "music" / "Artist" / "Album"
        self.album.mkdir(parents=True)
        self.library_file = self.album / "01.flac"
        self.library_file.write_bytes(b"library")

    def patches(self):
        return (
            mock.patch.object(slskd, "DOWNLOADS_ROOT", self.downloads),
            mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (self.downloads,)),
            mock.patch.object(rt, "TORRENT_SOURCE_ROOTS", (self.downloads,)),
        )


def _vectors(tree: _Tree):
    """(label, username, queued remote file) for the five #248 vectors."""
    abs_album = str(tree.album.relative_to(tree.album.anchor)).replace("/", "\\")
    return [
        ("username ../../..", "../../..", "srv\\music\\Artist\\Album\\01.flac"),
        ("username /", "/", abs_album + "\\01.flac"),
        ("username a/../../../../srv", "a/../../../../srv", "music\\Artist\\Album\\01.flac"),
        ("rdir ../../../../srv/music", "peer", "../../../../srv/music/Artist/Album/01.flac"),
        ("remote file ..\\..\\..\\..\\srv\\...", "peer", "..\\..\\..\\..\\srv\\music\\Artist\\Album\\01.flac"),
    ]


class _Spy:
    """Records every rglob root and unlink target."""

    def __init__(self):
        self.scanned = []
        self.unlinked = []
        real_rglob, real_unlink = Path.rglob, Path.unlink
        spy = self

        def rglob(self, pattern):
            spy.scanned.append(Path(self))
            return real_rglob(self, pattern)

        def unlink(self, missing_ok=False):
            spy.unlinked.append(Path(self))
            return real_unlink(self, missing_ok=missing_ok)

        self.patches = (mock.patch.object(Path, "rglob", rglob), mock.patch.object(Path, "unlink", unlink))


def _apply(patches):
    stack = []
    for p in patches:
        p.start()
        stack.append(p)
    return stack


class PeerPathValidationTests(unittest.TestCase):
    def test_unsafe_usernames_are_refused(self):
        for name in ("", ".", "..", "/", "../../..", "a/../../../../srv", "a\\b", "a\0b", None):
            self.assertEqual(safe_peer_username(name), "", repr(name))

    def test_benign_usernames_pass(self):
        for name in ("peer", "DJ Böb", "..hidden", "a.b", "名前 user"):
            self.assertEqual(safe_peer_username(name), name)

    def test_remote_path_drops_climbing_segments(self):
        self.assertEqual(
            _remote_path("..\\..\\..\\..\\srv\\music\\Artist\\Album\\01.flac"),
            Path("srv", "music", "Artist", "Album", "01.flac"),
        )
        self.assertEqual(_remote_path("C:\\x\\.\\..\\y//z.flac"), Path("x", "y", "z.flac"))
        self.assertEqual(_remote_path("/../.."), Path("."))

    def test_remote_path_drops_drive_segments_anywhere(self):
        # Security F2: "C:" mid-path would be a drive-relative join on Windows.
        self.assertEqual(_remote_path("Music\\C:\\Album\\d:x\\01.flac"), Path("Music", "Album", "01.flac"))

    def test_remote_path_drops_control_character_segments(self):
        self.assertEqual(_remote_path("Music\\a\nb\\Album\x7f\\01.flac"), Path("Music", "01.flac"))

    def test_drive_and_control_character_usernames_are_refused(self):
        # Security F1 (log-line forging) and F2 (drive-relative join).
        for name in ("C:", "c:x", "a:b", "a\nb", "a\rb", "a\x1bb", "a\x7fb", "a\tb"):
            with self.subTest(name=repr(name)):
                self.assertEqual(safe_peer_username(name), "")


class SymlinkContainmentTests(unittest.TestCase):
    """Security review of PR #259: a symlink under downloads that points into
    the library is never followed to a delete."""

    def test_symlinks_into_library_are_never_followed_to_a_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dl, lib = base / "downloads", base / "music"
            (lib / "Album").mkdir(parents=True)
            (dl / "peer" / "Real").mkdir(parents=True)
            dir_victim = lib / "Album" / "01.flac"
            file_victim = lib / "Album" / "02.flac"
            dir_victim.write_bytes(b"x")
            file_victim.write_bytes(b"x")
            try:
                os.symlink(lib / "Album", dl / "peer" / "Album", target_is_directory=True)
                os.symlink(file_victim, dl / "peer" / "Real" / "02.flac")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not permitted on this host")

            removed = cleanup_failed_candidate_files(
                dl, "peer", ["Album/01.flac", "Real/02.flac"], [".flac"], [], [dl])

            self.assertEqual(removed, 0)
            self.assertTrue(dir_victim.exists())
            self.assertTrue(file_victim.exists())


class FailedCandidateCleanupContainmentTests(unittest.TestCase):
    """S5: a failed candidate never unlinks outside DOWNLOADS_ROOT."""

    def test_hostile_vectors_delete_nothing_outside_downloads(self):
        for i in range(5):
            with self.subTest(i), tempfile.TemporaryDirectory() as tmp:
                tree = _Tree(tmp)
                label, username, remote = _vectors(tree)[i]
                spy = _Spy()
                started = _apply((*tree.patches(), *spy.patches))
                try:
                    log = []
                    slskd._slskd_cleanup_failed_candidate_files(username, [remote], log)
                    roots = slskd._slskd_download_candidate_roots(username, [remote])
                finally:
                    for p in started:
                        p.stop()
                self.assertTrue(tree.library_file.exists(), label)
                for path in (*spy.scanned, *spy.unlinked, *roots):
                    self.assertTrue(rt._path_is_under(path, tree.downloads), f"{label}: {path}")

    def test_empty_allowlist_scans_and_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _Tree(tmp)
            partial = tree.downloads / "peer" / "Album" / "01.flac"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"x")
            spy = _Spy()
            started = _apply((mock.patch.object(slskd, "DOWNLOADS_ROOT", tree.downloads),
                              mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", ()), *spy.patches))
            try:
                slskd._slskd_cleanup_failed_candidate_files("peer", ["Album\\01.flac"], [])
            finally:
                for p in started:
                    p.stop()
            self.assertTrue(partial.exists())
            self.assertEqual(spy.scanned + spy.unlinked, [])

    def test_benign_unicode_candidate_is_still_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _Tree(tmp)
            remote = "Music\\Artist Name\\Album Ünï\\01 Sóng.flac"
            partial = tree.downloads / "DJ Böb" / "Music" / "Artist Name" / "Album Ünï" / "01 Sóng.flac"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"x")
            started = _apply(tree.patches())
            try:
                log = []
                slskd._slskd_cleanup_failed_candidate_files("DJ Böb", [remote], log)
            finally:
                for p in started:
                    p.stop()
            self.assertFalse(partial.exists())
            self.assertTrue(tree.library_file.exists())
            self.assertIn("Removed 1 partial file", "\n".join(log))


class _Clock:
    """time.time that jumps past any deadline after ``ticks`` calls."""

    def __init__(self, ticks: int):
        self._it = itertools.chain(itertools.repeat(1000.0, ticks), itertools.repeat(10 ** 9))

    def __call__(self):
        return next(self._it)


class DownloadedFileSearchContainmentTests(unittest.TestCase):
    """S4: the completed-file search never scans outside the allowed roots,
    and #251: the wait for files to appear ends at its deadline."""

    def _find(self, tree, username, remote, expected, hints=None, ticks=3):
        spy = _Spy()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 50:  # main: no deadline; fail instead of hanging
                raise AssertionError("wait loop ignored its deadline")

        started = _apply((
            *tree.patches(), *spy.patches,
            mock.patch.object(slskd, "_slskd_req", return_value=[]),
            mock.patch.object(slskd.time, "sleep", side_effect=sleep),
            mock.patch.object(slskd.time, "time", side_effect=_Clock(ticks)),
        ))
        try:
            log = []
            result = slskd._find_slskd_downloaded_files(
                username, [remote], expected, log, transfer_hints=hints or [])
        finally:
            for p in started:
                p.stop()
        return result, spy, log

    def test_hostile_vectors_scan_nothing_outside_downloads(self):
        for i in range(5):
            with self.subTest(i), tempfile.TemporaryDirectory() as tmp:
                tree = _Tree(tmp)
                label, username, remote = _vectors(tree)[i]
                # The expected dir a caller derives from the same peer data.
                expected = str(tree.downloads / username / Path(remote.replace("\\", "/")).parent)
                hint = {"directories": [{"localDirectory": str(tree.album),
                                         "files": [{"filename": remote}]}]}
                (folder, files), spy, _log = self._find(tree, username, remote, expected, [hint])
                self.assertEqual(files, [], label)
                self.assertTrue(tree.library_file.exists())
                for path in spy.scanned:
                    self.assertTrue(rt._path_is_under(path, tree.downloads), f"{label}: {path}")

    def test_benign_unicode_files_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _Tree(tmp)
            remote = "Music\\Artist Name\\Album Ünï\\01 Sóng.flac"
            local = tree.downloads / "DJ Böb" / "Music" / "Artist Name" / "Album Ünï"
            local.mkdir(parents=True)
            (local / "01 Sóng.flac").write_bytes(b"x")
            expected = str(slskd._slskd_peer_download_dir(tree.downloads, "DJ Böb", "Music\\Artist Name\\Album Ünï"))
            (folder, files), _spy, _log = self._find(tree, "DJ Böb", remote, expected)
            self.assertEqual(Path(folder), local)
            self.assertEqual([f.name for f in files], ["01 Sóng.flac"])

    def test_benign_slskd_default_layout_is_found(self):
        # slskd's own layout: <downloads>/<remote parent dir name>/<file>, no
        # username folder; Windows remote path from a peer with a space.
        with tempfile.TemporaryDirectory() as tmp:
            tree = _Tree(tmp)
            remote = r"D:\Shares\Rips\Artist - Album (2001)\01 - Intro.flac"
            local = tree.downloads / "Artist - Album (2001)"
            local.mkdir(parents=True)
            (local / "01 - Intro.flac").write_bytes(b"x")
            expected = str(slskd._slskd_peer_download_dir(
                tree.downloads, "some peer", r"D:\Shares\Rips\Artist - Album (2001)"))
            (folder, files), _spy, _log = self._find(tree, "some peer", remote, expected)
            self.assertEqual(Path(folder), local)
            self.assertEqual([f.name for f in files], ["01 - Intro.flac"])

    def test_wait_for_files_ends_at_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _Tree(tmp)
            (folder, files), _spy, log = self._find(
                tree, "peer", "Music\\Album\\01.flac", str(tree.downloads / "peer" / "Music" / "Album"))
            self.assertEqual(files, [])
            text = "\n".join(log)
            self.assertIn("Gave up waiting for queued files", text)
            self.assertIn("Could not locate completed queued files", text)


class SearchAndQueueContainmentTests(unittest.TestCase):
    """The expected dir returned for a queued candidate stays under
    DOWNLOADS_ROOT, and an unsafe username refuses the candidate."""

    def _run(self, tree, username, remote_dir):
        response = {"username": username, "hasFreeUploadSlot": True,
                    "files": [{"filename": remote_dir + "/01 Song.flac", "size": 10}]}
        queued = []

        def req(method, path, body=None):
            if method == "POST" and path.startswith("transfers/"):
                queued.append(path)
                return {}
            if method == "POST":
                return {}
            if "includeResponses" in path:
                return {"state": "completed", "responseCount": 1, "responses": [response]}
            return [response]

        log = []
        started = _apply((*tree.patches(), mock.patch.object(slskd, "_slskd_req", side_effect=req),
                          mock.patch.object(slskd.time, "sleep")))
        try:
            try:
                result = slskd._slskd_search_and_queue("Artist", "Album", "", log)
            except RuntimeError as ex:
                result = ex
        finally:
            for p in started:
                p.stop()
        return result, queued, log

    def test_climbing_rdir_expected_dir_stays_under_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = _Tree(tmp)
            result, _queued, _log = self._run(tree, "peer", "../../../../srv/music")
            username, _files, expected_dir, _remote = result
            self.assertTrue(rt._path_is_under(Path(expected_dir), tree.downloads), expected_dir)
            self.assertEqual(Path(expected_dir), tree.downloads / "peer" / "srv" / "music")

    def test_unsafe_username_refuses_the_candidate(self):
        for username in ("../../..", "/", "a/../../../../srv"):
            with self.subTest(username), tempfile.TemporaryDirectory() as tmp:
                tree = _Tree(tmp)
                result, queued, log = self._run(tree, username, "Music/Album")
                self.assertIsInstance(result, RuntimeError)
                self.assertEqual(queued, [])
                self.assertIn("Refused 1 candidate(s) with an unsafe peer username.", "\n".join(log))


if __name__ == "__main__":
    unittest.main()
