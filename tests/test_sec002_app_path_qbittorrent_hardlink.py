"""Security and behavioral unit tests for qBittorrent hardlink repair (SEC-002 Wave 6).

Covers:
- Engine-side control agent /files/hardlink endpoint validation
- Path containment (outside-root, traversal, symlink, null bytes)
- No-overwrite policy and same-inode idempotency
- Cross-device rejection
- Path-alias mapping contract
- Error message sanitization and leak prevention
- Real endpoint invocation (ControlAgentHandler.do_POST), not just mocked
  BeetsClient._request()/create_hardlink() calls
- Source/destination role separation (music-only source, staging/tmp-only
  destination -- this is not a generic cross-root hardlink primitive)
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from io import BytesIO
from pathlib import Path

import app as APP
from backend.beets_client import beets_client
from backend import beets_control_agent as control_agent
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
        self.assertIsNone(APP._qbit_map_path("/downloads/\x00malicious"))

    def test_qbit_map_path_rejects_blank(self):
        self.assertIsNone(APP._qbit_map_path(""))
        self.assertIsNone(APP._qbit_map_path("   "))

    def test_qbit_map_path_never_returns_cwd_sentinel(self):
        # Path("") stringifies to "." (the current working directory); no
        # blank/invalid mapping may produce that or any other Path at all.
        for bad in ("", "   ", "\x00", "/downloads/\x00x"):
            self.assertIsNone(APP._qbit_map_path(bad))

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

    def test_qbit_allowed_repair_path_uses_hardened_containment(self):
        # _qbit_allowed_repair_path() must use the CodeQL-hardened _path_under(),
        # not the older _path_is_under() pattern -- prefix-string false matches
        # must not slip through the qBittorrent repair-root gate.
        with mock.patch.object(APP, "QBIT_REPAIR_ALLOWED_ROOTS", (self.staging_root,)):
            sibling = self.staging_root.parent / (self.staging_root.name + "2") / "track.flac"
            self.assertFalse(APP._qbit_allowed_repair_path(sibling))
            inside = self.staging_root / "track.flac"
            self.assertTrue(APP._qbit_allowed_repair_path(inside))

    def test_library_file_candidates_reject_path_outside_music_root(self):
        # A DB row whose stored path is not actually under MUSIC_ROOT must
        # never become an eligible hardlink source, even if it matches on
        # filename and size.
        outside_file = self.tmp_root / "not_in_library" / "track.flac"
        outside_file.parent.mkdir(parents=True, exist_ok=True)
        outside_file.write_bytes(b"X" * 10)

        class _Row(dict):
            def __getitem__(self, key):
                return dict.get(self, key)

        fake_row = _Row(id=1, path=str(outside_file), title="T", artist="A", album="Al")

        class _FakeCon:
            def execute(self, *a, **kw):
                return self

            def fetchall(self):
                return [fake_row]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(APP, "MUSIC_ROOT", self.music_root), \
             mock.patch.object(APP, "_db", return_value=_FakeCon()):
            matches = APP._library_file_candidates_for_qbit("track.flac", 10)
        self.assertEqual(matches, [])

    def test_qbit_hardlink_missing_impl_reports_already_present_separately(self):
        target_path = self.music_root / "album" / "track.flac"
        torrent = {"hash": "abc12345", "name": "Test Torrent", "save_path": str(self.music_root)}
        file_row = {"name": "album/track.flac", "size": 100, "progress": 1.0}

        with mock.patch.object(APP, "_qbit_login_cookie", return_value="cookie"):
            with mock.patch.object(APP, "_qbit_request_json") as mock_qjson:
                mock_qjson.side_effect = [[torrent], [file_row]]
                with mock.patch.object(APP, "_library_file_candidates_for_qbit") as mock_candidates:
                    mock_candidates.return_value = [{
                        "id": 1,
                        "path": str(self.music_root / "track.flac"),
                        "match_strategy": "filename_size",
                    }]
                    with mock.patch.object(APP, "QBIT_REPAIR_ALLOWED_ROOTS", [self.music_root]):
                        with mock.patch.object(beets_client, "create_hardlink") as mock_hl:
                            mock_hl.return_value = {"ok": True, "already_present": True}
                            log = []
                            res = APP._qbit_hardlink_missing_impl(
                                dry_run=False, category="music", qbit_filter="all",
                                search="", hashes=[], limit=10, recheck=False, log=log,
                            )
                            # An idempotent no-op must not be counted as a newly
                            # created link.
                            self.assertEqual(res["linked"], 0)
                            self.assertEqual(res["already_present"], 1)
                            self.assertEqual(res["actions"][0]["status"], "already_present")

    def test_qbit_hardlink_public_error_allowlist(self):
        self.assertEqual(
            APP._qbit_hardlink_public_error({"error": "Cross-device hardlink not supported"}),
            "Cross-device hardlink not supported",
        )
        # Anything not on the known-safe list collapses to the generic
        # message -- the client boundary must not assume every future
        # engine error string is safe to forward verbatim.
        self.assertEqual(
            APP._qbit_hardlink_public_error({"error": "unexpected /host/path leaked in traceback"}),
            "Could not create this hardlink.",
        )
        self.assertEqual(APP._qbit_hardlink_public_error(None), "Could not create this hardlink.")
        self.assertEqual(APP._qbit_hardlink_public_error("not a dict"), "Could not create this hardlink.")


class ControlAgentHardlinkEndpointTests(unittest.TestCase):
    """Real invocation of ControlAgentHandler.do_POST for /files/hardlink.

    Follows the established pattern (see ControlAgentLibraryRewriteTests in
    tests/test_sec002_app_path_album_folder_repair.py): construct the
    handler via __new__ to skip socket binding, feed it a real JSON body,
    and call do_POST() directly so the actual endpoint code runs end to end
    rather than being replaced by a mock.
    """

    def setUp(self):
        # Deliberately NOT placed under the OS temp directory: the "tmp"
        # root category legitimately allows any path under
        # tempfile.gettempdir(), so nesting these fixture roots there would
        # make every destination accidentally valid under "tmp" regardless
        # of the role-separation logic under test.
        self._scratch_base = Path(tempfile.mkdtemp(prefix="qbit_hardlink_test_", dir=os.getcwd())).resolve()
        self.addCleanup(shutil.rmtree, self._scratch_base, ignore_errors=True)
        self.root = self._scratch_base / "roots"
        self.music_root = self.root / "music"
        self.staging_root = self.root / "staging"
        self.outside_root = self._scratch_base / "outside"
        self.music_root.mkdir(parents=True)
        self.staging_root.mkdir(parents=True)
        self.outside_root.mkdir(parents=True)

    def post_hardlink(self, payload):
        body = json.dumps(payload).encode("utf-8")
        handler = control_agent.ControlAgentHandler.__new__(control_agent.ControlAgentHandler)
        handler.path = "/files/hardlink"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler._authenticate = lambda: True
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        with mock.patch.object(control_agent, "MUSIC_LIBRARY_PATH", str(self.music_root)), \
             mock.patch.object(control_agent, "DOWNLOAD_PATH", str(self.staging_root)), \
             mock.patch.object(control_agent, "acquire_os_lock", return_value=None), \
             mock.patch.object(control_agent, "release_os_lock"):
            handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    # -- Requests that must be rejected regardless of platform (no real
    # backing file required to prove these) --------------------------------

    def test_rejects_invalid_expected_size_types(self):
        for bad_size in ("100", 100.0, True, False, 0, -5):
            code, data = self.post_hardlink({
                "source_path": "/does/not/matter",
                "target_path": "/does/not/matter/either",
                "expected_size": bad_size,
            })
            self.assertEqual(code, 400, (bad_size, data))
            self.assertEqual(data["error"], "Invalid expected_size value")

    def test_rejects_source_outside_music_root(self):
        if os.name == "nt":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")
        outside = self.root / "outside" / "track.flac"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"data")
        target = self.staging_root / "track.flac"

        code, data = self.post_hardlink({"source_path": str(outside), "target_path": str(target)})

        self.assertEqual(code, 403, data)
        self.assertNotIn(str(outside), json.dumps(data))

    def test_rejects_music_as_destination_role(self):
        # The destination role is staging/tmp only: even a path that is
        # otherwise well-formed and under a real root must be rejected if it
        # resolves under the music library, since this endpoint must not
        # become a generic hardlink primitive that can target the
        # authoritative library.
        if os.name == "nt":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")
        source = self.music_root / "Artist" / "Album" / "track.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"data")
        target_in_music = self.music_root / "Other" / "track.flac"

        code, data = self.post_hardlink({
            "source_path": str(source),
            "target_path": str(target_in_music),
        })

        self.assertEqual(code, 403, data)

    def test_rejects_staging_as_source_role(self):
        # The source role is music-only: a staging-side file must not be
        # usable as the hardlink source, even though staging is a valid
        # root for the *destination*.
        if os.name == "nt":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")
        source = self.staging_root / "track.flac"
        source.write_bytes(b"data")
        target = self.staging_root / "repaired" / "track.flac"

        code, data = self.post_hardlink({"source_path": str(source), "target_path": str(target)})

        self.assertEqual(code, 403, data)

    # -- Requests that require a real backing file / real hardlink semantics;
    # exercised for real on Linux CI (ubuntu-latest), skipped locally on
    # Windows where resolve_safe_path's POSIX-absolute requirement can't be
    # satisfied by a real temp path. -----------------------------------------

    def _require_posix(self):
        if os.name == "nt":
            self.skipTest("requires POSIX-absolute paths against a real root; covered by Docker/Linux runtime")

    def test_creates_hardlink_for_valid_music_to_staging_request(self):
        self._require_posix()
        source = self.music_root / "Artist" / "Album" / "track.flac"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"AUDIO" * 4)
        target = self.staging_root / "repair" / "track.flac"

        code, data = self.post_hardlink({
            "source_path": str(source),
            "target_path": str(target),
            "expected_size": source.stat().st_size,
        })

        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("linked"))
        self.assertTrue(target.exists())
        self.assertEqual(target.stat().st_ino, source.stat().st_ino)
        # Least-information response: no raw path/device/inode disclosure.
        self.assertNotIn("source_path", data)
        self.assertNotIn("target_path", data)
        self.assertNotIn("st_ino", data)

    def test_idempotent_when_target_already_linked(self):
        self._require_posix()
        source = self.music_root / "track.flac"
        source.write_bytes(b"AUDIO")
        target = self.staging_root / "track.flac"
        try:
            os.link(str(source), str(target))
        except OSError:
            self.skipTest("filesystem does not support hardlinks")

        code, data = self.post_hardlink({"source_path": str(source), "target_path": str(target)})

        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("already_present"))
        self.assertNotIn("linked", data)

    def test_rejects_different_file_collision(self):
        self._require_posix()
        source = self.music_root / "track.flac"
        source.write_bytes(b"AUDIO_A")
        target = self.staging_root / "track.flac"
        target.write_bytes(b"UNRELATED_B")

        code, data = self.post_hardlink({"source_path": str(source), "target_path": str(target)})

        self.assertEqual(code, 409, data)
        self.assertFalse(target.read_bytes() == b"AUDIO_A")
        self.assertEqual(target.read_bytes(), b"UNRELATED_B")

    def test_rejects_symlink_source(self):
        self._require_posix()
        # The symlink's real target sits INSIDE the music root deliberately:
        # this proves the endpoint's explicit is_symlink() check catches a
        # symlink source even when simple containment on the resolved path
        # would otherwise pass (a symlink pointing outside the root entirely
        # is already caught earlier by resolve_safe_path's containment
        # check; this is the narrower, easy-to-miss case).
        real_target_of_symlink = self.music_root / "real_secret.flac"
        real_target_of_symlink.write_bytes(b"SECRET")
        symlink_source = self.music_root / "link.flac"
        try:
            symlink_source.symlink_to(real_target_of_symlink)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")
        dst = self.staging_root / "out.flac"

        code, data = self.post_hardlink({"source_path": str(symlink_source), "target_path": str(dst)})

        self.assertEqual(code, 400, data)
        self.assertFalse(dst.exists())

    def test_rejects_symlink_target(self):
        self._require_posix()
        source = self.music_root / "track.flac"
        source.write_bytes(b"AUDIO")
        # elsewhere lives INSIDE the staging root deliberately: a symlink
        # whose target escapes every allowed root is already caught earlier
        # by resolve_safe_path's containment check on the resolved form;
        # this proves the endpoint also rejects a symlink whose target
        # happens to resolve to a location that containment alone would
        # accept.
        elsewhere = self.staging_root / "elsewhere.flac"
        elsewhere.write_bytes(b"OTHER")
        symlink_dst = self.staging_root / "track.flac"
        try:
            symlink_dst.symlink_to(elsewhere)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")

        code, data = self.post_hardlink({"source_path": str(source), "target_path": str(symlink_dst)})

        self.assertEqual(code, 409, data)
        # The pre-existing symlink and its target must be left untouched.
        self.assertTrue(symlink_dst.is_symlink())
        self.assertEqual(elsewhere.read_bytes(), b"OTHER")

    def test_rejects_destination_parent_that_is_a_symlink(self):
        self._require_posix()
        source = self.music_root / "track.flac"
        source.write_bytes(b"AUDIO")
        # real_dir lives outside every allowed root: if the parent-symlink
        # check were missing, the endpoint would write straight through it.
        # (The pre-existing-symlink case below is already caught earlier by
        # resolve_safe_path's containment check on the fully-resolved path,
        # since realpath() resolves through symlinked parent directories
        # too -- this test documents that the endpoint rejects it, whichever
        # check catches it. The narrower race -- the parent becoming a
        # symlink *after* validation but before mkdir -- is defended by the
        # post-mkdir re-validation added alongside this test, but is not
        # practical to exercise deterministically in a synchronous test.)
        real_dir = self.outside_root / "real_dir"
        real_dir.mkdir()
        symlinked_parent = self.staging_root / "repair"
        try:
            symlinked_parent.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")
        dst = symlinked_parent / "track.flac"

        code, data = self.post_hardlink({"source_path": str(source), "target_path": str(dst)})

        # resolve_safe_path() resolves through the symlinked parent
        # directory via realpath() before the containment check runs, so a
        # pre-existing symlinked parent that escapes every allowed root is
        # rejected there (403), before ever reaching mkdir.
        self.assertEqual(code, 403, data)
        self.assertFalse((real_dir / "track.flac").exists())

    def test_no_overwrite_when_destination_created_by_concurrent_writer(self):
        self._require_posix()
        # Simulates a race: the destination is created between the initial
        # pre-lock check and the locked recheck by having it already exist
        # with different content when the (single, synchronous) call runs --
        # the important behavioral property is that a pre-existing,
        # different-identity destination is never silently overwritten.
        source = self.music_root / "track.flac"
        source.write_bytes(b"AUDIO")
        target = self.staging_root / "track.flac"
        target.write_bytes(b"RACE_WINNER_CONTENT")

        code, data = self.post_hardlink({"source_path": str(source), "target_path": str(target)})

        self.assertEqual(code, 409, data)
        self.assertEqual(target.read_bytes(), b"RACE_WINNER_CONTENT")


if __name__ == "__main__":
    unittest.main()
