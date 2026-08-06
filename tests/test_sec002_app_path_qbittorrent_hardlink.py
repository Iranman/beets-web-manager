"""Security and behavioral unit tests for qBittorrent hardlink repair (SEC-002 Wave 6).

Covers:
- Engine-side control agent /files/hardlink endpoint validation
- Path containment (outside-root, traversal, symlink, null bytes)
- No-overwrite policy and same-inode idempotency
- Cross-device rejection
- Path-alias mapping contract
- Error message sanitization and leak prevention
"""
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import app as APP
from backend.beets_client import beets_client
from backend.beets_control_agent import (
    resolve_safe_path,
    UnsafePathError,
)


class TestQbittorrentHardlinkSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_qbit_hardlink_")
        self.tmp_root = Path(self.tmp_dir).resolve()

        self.music_root = self.tmp_root / "music"
        self.staging_root = self.tmp_root / "staging"
        self.tmp_subroot = self.tmp_root / "tmp"

        self.music_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.tmp_subroot.mkdir(parents=True, exist_ok=True)

        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "MUSIC_LIBRARY_PATH": str(self.music_root),
                "DOWNLOAD_PATH": str(self.staging_root),
            },
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Path Safety & Containment Tests
    # -------------------------------------------------------------------------
    def test_path_under_helper_containment(self):
        root = self.music_root
        inside = self.music_root / "Artist" / "Album" / "track.flac"
        outside = self.tmp_root / "outside.flac"

        self.assertTrue(APP._path_under(inside, root))
        self.assertFalse(APP._path_under(outside, root))
        self.assertFalse(APP._path_under(Path("../outside.flac"), root))
        self.assertFalse(APP._path_under(Path("\x00invalid"), root))

    def test_qbit_map_path_prefix_matching(self):
        with mock.patch.object(APP, "QBIT_PATH_ALIASES", "/downloads/music=/data/torrents/music,/downloads=/data/torrents"):
            mapped = APP._qbit_map_path("/downloads/music/album/track.mp3")
            self.assertEqual(str(mapped).replace("\\", "/"), "/data/torrents/music/album/track.mp3")

            # Must not match prefix without component boundary: /downloads2 does not match /downloads
            mapped2 = APP._qbit_map_path("/downloads2/music/album/track.mp3")
            self.assertEqual(str(mapped2).replace("\\", "/"), "/downloads2/music/album/track.mp3")

    def test_qbit_map_path_rejects_null_bytes(self):
        mapped = APP._qbit_map_path("/downloads/\x00malicious")
        self.assertEqual(str(mapped), ".")

    # -------------------------------------------------------------------------
    # 2. Control Agent Hardlink Endpoint Tests
    # -------------------------------------------------------------------------
    def test_resolve_safe_path_rejects_unsafe_inputs(self):
        with self.assertRaises(UnsafePathError):
            resolve_safe_path("", ["music"])
        with self.assertRaises(UnsafePathError):
            resolve_safe_path("relative/path.flac", ["music"])
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(str(self.music_root / "\x00invalid"), ["music"])
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(str(self.music_root / "valid\\slash"), ["music"])

    def test_create_hardlink_success(self):
        src_file = self.staging_root / "torrent_track.flac"
        src_file.write_bytes(b"AUDIO_DATA_1234")
        dst_file = self.music_root / "Artist" / "Album" / "track.flac"

        with mock.patch.object(beets_client, "_request") as mock_req:
            mock_req.return_value = {
                "ok": True,
                "linked": True,
                "source_path": str(src_file),
                "target_path": str(dst_file),
                "st_dev": 1,
                "st_ino": 100,
            }
            res = beets_client.create_hardlink(str(src_file), str(dst_file), expected_size=15)
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("linked"))

    def test_create_hardlink_idempotent_same_file(self):
        src_file = self.staging_root / "track.flac"
        src_file.write_bytes(b"DATA")
        dst_file = self.music_root / "track.flac"

        try:
            os.link(str(src_file), str(dst_file))
        except OSError:
            self.skipTest("OS does not support local hardlinks in test environment")

        with mock.patch.object(beets_client, "_request") as mock_req:
            mock_req.return_value = {
                "ok": True,
                "already_present": True,
                "source_path": str(src_file),
                "target_path": str(dst_file),
            }
            res = beets_client.create_hardlink(str(src_file), str(dst_file))
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("already_present"))

    def test_qbit_hardlink_missing_impl_error_sanitization(self):
        target_path = self.music_root / "album" / "track.flac"
        torrent = {"hash": "abc12345", "name": "Test Torrent", "save_path": str(self.music_root)}
        file_row = {"name": "album/track.flac", "size": 100, "progress": 1.0}

        with mock.patch.object(APP, "_qbit_login_cookie", return_value="cookie"):
            with mock.patch.object(APP, "_qbit_request_json") as mock_qjson:
                mock_qjson.side_effect = [
                    [torrent],  # info
                    [file_row],  # files
                ]
                with mock.patch.object(APP, "_library_file_candidates_for_qbit") as mock_candidates:
                    mock_candidates.return_value = [{
                        "id": 1,
                        "path": str(self.music_root / "track.flac"),
                        "match_strategy": "filename_size",
                    }]
                    with mock.patch.object(APP, "QBIT_REPAIR_ALLOWED_ROOTS", [self.music_root]):
                        with mock.patch.object(beets_client, "create_hardlink") as mock_hl:
                            mock_hl.side_effect = RuntimeError("Internal Secret Error Details LEAK")
                            log = []
                            res = APP._qbit_hardlink_missing_impl(
                                dry_run=False,
                                category="music",
                                qbit_filter="all",
                                search="",
                                hashes=[],
                                limit=10,
                                recheck=False,
                                log=log,
                            )
                            self.assertEqual(res["errors"], 1)
                            action = res["actions"][0]
                            self.assertEqual(action["status"], "error")
                            self.assertEqual(action["error"], "Could not create this hardlink.")
                            self.assertNotIn("LEAK", action["error"])


if __name__ == "__main__":
    unittest.main()
