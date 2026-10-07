"""Security review of #235: a DOWNLOADS_ROOT (or other configured download
root) that is "/" or overlaps the music library must fail closed -- it is left
out of every allowlist, logged, and blocks setup (F2/F9). Also F1 (slskd search
roots), F3 (Plex refresh error text) and F4 (no /tmp allowlist entry)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import config_layers


class UnsafeRootReasonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.music = self.root / "data" / "media" / "music"
        self.music.mkdir(parents=True)

    def test_filesystem_root_and_library_overlaps_are_unsafe(self):
        anchor = Path(os.path.realpath(str(self.root))).anchor
        for unsafe in (anchor, self.root / "data", self.music, self.music / "Artist"):
            self.assertTrue(config_layers.unsafe_root_reason(unsafe, self.music), unsafe)

    def test_separate_downloads_mount_is_safe(self):
        self.assertEqual(config_layers.unsafe_root_reason(self.root / "downloads", self.music), "")

    def test_safe_roots_drops_and_logs_unsafe_entries(self):
        with self.assertLogs("beets.config_layers", level="ERROR") as logs:
            kept = config_layers.safe_roots(
                "TORRENT_SOURCE_ROOTS", ["/", str(self.root / "data"), str(self.root / "dl")], self.music)
        self.assertEqual(kept, (self.root / "dl",))
        self.assertEqual(len(logs.output), 2)


class ImportReviewProbeTests(unittest.TestCase):
    """The security probe: DOWNLOADS_ROOT=/data and MUSIC_ROOT=/data/media/music
    planned an irreversible delete_file of a library file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data = self.root / "data"
        self.music = self.data / "media" / "music"
        self.track = self.music / "Artist" / "Album" / "01.flac"
        self.track.parent.mkdir(parents=True)
        self.track.write_bytes(b"audio")

    def _patched(self):
        import backend.serializers as ser
        patches = (
            mock.patch.object(ser, "MUSIC_ROOT", self.music),
            mock.patch.object(ser, "_DOWNLOADS_ROOTS", [str(self.data)]),
            mock.patch.object(ser, "TORRENT_SOURCE_ROOTS", (self.data,)),
            mock.patch.object(ser, "PLAYLIST_DOWNLOAD_ROOT", self.root / "playlist"),
            mock.patch.dict(os.environ, {"MUSIC_ROOT": str(self.music), "DOWNLOADS_ROOT": str(self.data),
                                         "BEETS_IMPORT_ROOTS": str(self.root / "staging")}),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ser

    def test_overlapping_roots_are_not_cleanup_roots(self):
        ser = self._patched()
        roots = ser._import_review_cleanup_roots(allow_music=False)
        self.assertEqual(roots, [(self.root / "playlist").resolve()])

    def test_probe_plans_no_delete_of_library_files(self):
        self._patched()
        from backend import composite_workflows as cw
        from backend.transaction_engine import TransactionStore
        store = TransactionStore(root=str(self.root / "transactions"))
        res = cw.plan_import_review_cleanup({"path": str(self.data / "media"), "action": "delete"}, store=store)
        self.assertFalse(res.get("ok"), res)
        self.assertTrue(self.track.exists())


class SetupBlocksUnsafeDownloadsRootTests(unittest.TestCase):
    def test_overlapping_downloads_root_blocks_setup(self):
        from tests.test_setup_music_root_issue143 import SetupMusicRootTests

        class _T(SetupMusicRootTests):
            def runTest(self):
                pass

        t = _T()
        t.setUp()
        self.addCleanup(t.doCleanups)
        music = t.root / "data" / "media" / "music"
        music.mkdir(parents=True)
        body = t._status({"MUSIC_ROOT": str(music), "DOWNLOADS_ROOT": str(t.root / "data")})
        self.assertIn(config_layers.UNSAFE_DOWNLOADS_ROOT_MESSAGE, body["blocking_reasons"])
        dl = t.root / "downloads"
        dl.mkdir()
        body = t._status({"MUSIC_ROOT": str(music), "DOWNLOADS_ROOT": str(dl)})
        self.assertNotIn(config_layers.UNSAFE_DOWNLOADS_ROOT_MESSAGE, body["blocking_reasons"])


class _StopWaiting(Exception):
    pass


class SlskdSearchRootTests(unittest.TestCase):
    """F1: the completed-download search never ranges over DOWNLOADS_ROOT's
    parent, "/" or fixed paths, and ignores transfer hints outside its roots."""

    def test_search_roots_are_only_configured_download_roots(self):
        import backend.app_runtime as rt
        import backend.slskd_service as slskd
        with tempfile.TemporaryDirectory() as d:
            dl = Path(d).resolve() / "downloads"
            dl.mkdir()
            scanned = []
            real_rglob = Path.rglob

            def rglob(self, pattern):
                scanned.append(Path(self))
                return real_rglob(self, pattern)

            with mock.patch.object(slskd, "DOWNLOADS_ROOT", dl), \
                    mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (dl,)), \
                    mock.patch.object(rt, "TORRENT_SOURCE_ROOTS", (dl,)), \
                    mock.patch.object(slskd, "_slskd_req", return_value=[]), \
                    mock.patch.object(slskd.time, "sleep", side_effect=_StopWaiting), \
                    mock.patch.object(Path, "rglob", rglob):
                # The wait loop has no deadline yet (F8, #248): stop it at its
                # first sleep, after one scan of every search root.
                with self.assertRaises(_StopWaiting):
                    slskd._find_slskd_downloaded_files(
                        "peer", [{"filename": "a\\b\\01.flac"}], str(dl / "peer" / "b"), [],
                        transfer_hints=[{"directory": str(Path(d).resolve())}])
            self.assertTrue(scanned)
            for path in scanned:
                self.assertTrue(rt._path_is_under(path, dl), path)


class PlexRefreshErrorTextTests(unittest.TestCase):
    """F3 (CodeQL #1374): the exception text never reaches the job log."""

    def test_refresh_failure_logs_a_fixed_line(self):
        import backend.plex_service as plex
        log = []
        workflow = next(iter(plex._ALLOWED_PLEX_REFRESH_WORKFLOWS))
        with mock.patch.object(plex, "_plex_settings", return_value={"url": "http://plex", "token": "t"}), \
                mock.patch.object(plex, "_plex_find_music_section", side_effect=RuntimeError("secret detail /x")), \
                self.assertLogs("app", level="WARNING"):
            self.assertFalse(plex._trigger_plex_refresh(log, workflow=workflow))
        self.assertEqual(log, ["  [plex] Refresh failed; continuing without it (see server logs)."])


class NoTmpAllowlistTests(unittest.TestCase):
    """F4: import-review cleanup may not plan deletes under /tmp."""

    def test_tmp_is_not_a_downloads_root(self):
        import backend.serializers as ser
        self.assertNotIn("/tmp", ser._DOWNLOADS_ROOTS)


if __name__ == "__main__":
    unittest.main()
