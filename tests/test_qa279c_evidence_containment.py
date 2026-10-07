"""QA #279 round 3: containment holds even when the evidence gate would pass
(so the #248 tests are not vacuous now that plain strings never delete), and
the real _slskd_search_and_queue -> cleanup chain removes own files."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.app_runtime as rt
import backend.slskd as slskd_mod
import backend.slskd_service as slskd
from backend.slskd import QueuedRemote, cleanup_failed_candidate_files
from tests.test_slskd_peer_path_containment import _Spy, _Tree, _apply, _vectors


def _q(remote, size):
    return QueuedRemote(remote, size, time.time() - 60)


class EvidenceBackedContainment(unittest.TestCase):
    def _symlink_case(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        dl, lib = base / "downloads", base / "music"
        (lib / "Album").mkdir(parents=True)
        (dl / "peer" / "Real").mkdir(parents=True)
        dir_victim, file_victim = lib / "Album" / "01.flac", lib / "Album" / "02.flac"
        dir_victim.write_bytes(b"x")
        file_victim.write_bytes(b"x")
        try:
            os.symlink(lib / "Album", dl / "peer" / "Album", target_is_directory=True)
            os.symlink(file_victim, dl / "peer" / "Real" / "02.flac")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted on this host")
        remotes = [_q("Album/01.flac", 1), _q("Real/02.flac", 1)]
        return dl, remotes, dir_victim, file_victim

    def test_symlinks_with_matching_evidence_are_not_followed(self):
        dl, remotes, dir_victim, file_victim = self._symlink_case()
        removed = cleanup_failed_candidate_files(dl, "peer", remotes, [".flac"], [], [dl])
        self.assertEqual(removed, 0)
        self.assertTrue(dir_victim.exists())
        self.assertTrue(file_victim.exists())

    def test_symlink_case_is_not_vacuous(self):
        """With within_roots disabled the same inputs DO delete: the gate is
        what protects, not missing evidence."""
        dl, remotes, dir_victim, file_victim = self._symlink_case()
        with mock.patch.object(slskd_mod, "within_roots", return_value=True):
            removed = cleanup_failed_candidate_files(dl, "peer", remotes, [".flac"], [], [dl])
        self.assertGreater(removed, 0)

    def test_hostile_vectors_with_matching_evidence_stay_in_downloads(self):
        for i in range(5):
            with self.subTest(i), tempfile.TemporaryDirectory() as tmp:
                tree = _Tree(tmp)
                label, username, remote = _vectors(tree)[i]
                size = tree.library_file.stat().st_size
                spy = _Spy()
                started = _apply((*tree.patches(), *spy.patches))
                try:
                    slskd._slskd_cleanup_failed_candidate_files(username, [_q(remote, size)], [])
                finally:
                    for p in started:
                        p.stop()
                self.assertTrue(tree.library_file.exists(), label)
                for path in spy.unlinked:
                    self.assertTrue(rt._path_is_under(path, tree.downloads), f"{label}: {path}")


class RealQueueToCleanup(unittest.TestCase):
    def test_real_search_and_queue_evidence_cleans_own_flat_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp).resolve()
            response = {"username": "peer", "hasFreeUploadSlot": True,
                        "files": [{"filename": "Music\\Album\\01 Song.flac", "size": 10},
                                  {"filename": "Music\\Album\\02 Song.flac", "size": 10}]}

            def req(method, path, body=None):
                if method == "POST":
                    return {}
                if "includeResponses" in path:
                    return {"state": "completed", "responseCount": 1, "responses": [response]}
                return [response]

            log = []
            patches = (mock.patch.object(slskd, "DOWNLOADS_ROOT", dl),
                       mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (dl,)),
                       mock.patch.object(rt, "TORRENT_SOURCE_ROOTS", ()),
                       mock.patch.object(slskd, "_slskd_req", side_effect=req),
                       mock.patch.object(slskd.time, "sleep"))
            started = _apply(patches)
            try:
                user, queued, _exp, _rdir = slskd._slskd_search_and_queue("Artist", "Album", "", log)
                # slskd finishes writing after queueing (default layout). A real
                # transfer takes far longer than the filesystem's coarse
                # timestamp tick (~16 ms on Windows, a jiffy on Linux).
                own = []
                for n in ("01 Song.flac", "02 Song.flac"):
                    p = dl / "Album" / n
                    p.parent.mkdir(exist_ok=True)
                    p.write_bytes(b"0123456789")
                    now = queued[0].queued_at + 5
                    os.utime(p, (now, now))
                    own.append(p)
                foreign = dl / "Album" / "03 Song.flac"
                foreign.write_bytes(b"0123456789")
                slskd._slskd_cleanup_failed_candidate_files(user, list(queued), log)
            finally:
                for p in started:
                    p.stop()
            for p in own:
                self.assertFalse(p.exists(), (p, log))
            self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
