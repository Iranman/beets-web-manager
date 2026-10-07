"""QA #243 round 3 (F-3): a client-supplied ``album_id`` is not the
library-delete gate -- the engine cannot prove the album owns the target.
Only the explicit gate (set by the route after it verified the album)
lets an in-library delete through."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    TransactionStore, execute_import_review_cleanup_apply, execute_import_review_cleanup_plan)


class AlbumIdGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name).resolve() / "data"
        self.music = self.data / "media" / "music"
        self.track = self.music / "Artist" / "Album" / "01.flac"
        self.track.parent.mkdir(parents=True)
        self.track.write_bytes(b"audio")
        self.store = TransactionStore(root=str(Path(tmp.name) / "tx"))
        env = mock.patch.dict(os.environ, {"MUSIC_ROOT": str(self.music)})
        env.start()
        self.addCleanup(env.stop)

    def _plan_then_apply(self, extra):
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete", **extra},
            [str(self.music)], music_root=str(self.data / "elsewhere"))
        self.assertTrue(res.get("ok"), res)
        op = res["operation_id"]
        self.store.update(op, status="Approved", metadata={"music_root": str(self.data / "elsewhere")})
        return execute_import_review_cleanup_apply(self.store, op, quarantine_root=str(self.data / "q"))

    def test_without_gate_apply_refuses(self):
        out = self._plan_then_apply({})
        self.assertEqual(out.get("code"), "import_review_library_delete_refused", out)
        self.assertTrue(self.track.exists())

    def test_arbitrary_album_id_does_not_open_the_gate(self):
        out = self._plan_then_apply({"album_id": 999999})
        self.assertEqual(out.get("code"), "import_review_library_delete_refused", out)
        self.assertTrue(self.track.exists())

    def test_explicit_gate_still_deletes(self):
        out = self._plan_then_apply({"allow_library_delete": True})
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(self.track.exists())


if __name__ == "__main__":
    unittest.main()
