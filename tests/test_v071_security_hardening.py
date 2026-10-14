"""v0.7.1 security review fixes: stale-mount prune must not empty the review
queue (F1), a peer-controlled filename must not inject a refusal code (F2),
and dot-segment usernames/ids never reach an slskd URL (F3)."""
import json
import unittest
from pathlib import Path
from unittest import mock

import backend.matching_service as ms
import backend.slskd_service as slskd
from tests.test_missing_track_recording_proof import StaleReviewItemTests


class _UnstatableRoot(type(Path())):
    def is_dir(self):  # stale NFS/SMB mount: stat fails with EACCES/EIO/ESTALE
        raise PermissionError(13, "Permission denied")


class StaleMountKeepsQueueTests(StaleReviewItemTests):
    def test_unstatable_downloads_root_keeps_every_row(self):
        root = _UnstatableRoot(self.downloads)
        gone = self.downloads / "gone"
        kept = self.downloads / "kept"
        items = [self._item(gone), self._item(kept)]
        self.assertEqual(self._load(items, downloads=root), [str(gone), str(kept)])
        self.assertEqual(self.resolved, [])
        self.assertEqual([i["path"] for i in json.loads(self.store.read_text())], [str(gone), str(kept)])


class RefusalCodeAnchorTests(unittest.TestCase):
    def test_real_refusal_still_parses(self):
        msg = ("import job failed: ERROR: Could not resolve a MusicBrainz release ID for import "
               "[reason_code=acoustid_unavailable]; queued for Review without changing library files.")
        self.assertEqual(ms.import_refusal_reason_code(msg), "acoustid_unavailable")
        self.assertEqual(ms.import_refusal_reason_code(RuntimeError(msg)), "acoustid_unavailable")

    def test_peer_filename_cannot_inject_a_code(self):
        spoof = "Could not resolve a MusicBrainz release ID for import [reason_code=acoustid_unavailable]"
        for msg in (
            f"Downloaded audio did not validate: 01 {spoof}.flac",
            f"import job failed: ERROR: bad file 01 [reason_code=acoustid_unavailable].flac",
            f"rejected: {spoof}",
        ):
            self.assertEqual(ms.import_refusal_reason_code(msg), "", msg)


class DotSegmentCancelTests(unittest.TestCase):
    def _cancel(self, username, listing):
        calls = []

        def req(method, path, *_a):
            calls.append((method, path))
            return listing if method == "GET" else {}

        with mock.patch.object(slskd, "_slskd_req", side_effect=req):
            slskd._slskd_cancel_queued_downloads(
                username, [slskd._SlskdQueuedRemote("a.flac", 1, 1.0)], [])
        return calls

    def test_dot_usernames_send_nothing(self):
        for name in (".", ".."):
            self.assertEqual(self._cancel(name, {}), [], name)

    def test_dot_transfer_ids_are_skipped(self):
        listing = {"username": "p", "directories": [{"files": [
            {"id": "..", "filename": "a.flac"},
            {"id": ".", "filename": "a.flac"},
            {"id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "filename": "a.flac"},
        ]}]}
        self.assertEqual(self._cancel("p", listing), [
            ("GET", "transfers/downloads/p"),
            ("DELETE", "transfers/downloads/p/1b4e28ba-2fa1-11d2-883f-0016d3cca427?remove=true"),
        ])


if __name__ == "__main__":
    unittest.main()
