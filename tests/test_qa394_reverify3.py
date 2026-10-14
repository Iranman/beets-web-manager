"""QA re-verify of PR #394 at cce1711: probes (1)-(3) from the coordinator."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.test_import_batch_phase_b as pb
import tests.test_qa383_import_batch_e2e as e2e
from tests.test_import_batch_phase_b import RG, _Batch


def setUpModule():
    pb.setUpModule()
    e2e.setUpModule()
    global ai, ii
    ai = pb.ai
    import backend.import_identity as ii


class PartlyUnreadableFolder(_Batch):
    def test_unreadable_subfolder_next_to_readable_audio_is_refused_not_imported_in_part(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        sub = Path(self.folders[0]) / "CD2"
        sub.mkdir()
        (sub / "02.flac").write_bytes(b"x")
        sub.chmod(0)
        self.addCleanup(sub.chmod, 0o755)
        _, folders = self.run_batch()
        called = [c.args[0] for c in self.stubs["reimport_source"].call_args_list]
        self.assertNotIn(self.folders[0], called)
        # Security review I3: an unreadable part is unverifiable, not a link.
        self.assertEqual(folders[self.folders[0]]["failure_reason"], "unverifiable_folder")
        self.assertEqual(folders[self.folders[1]]["status"], "imported")


class LegitimatePathForms(_Batch):
    def test_trailing_slash_stored_path_is_not_refused(self):
        self.folders[0] = self.folders[0] + "/"
        _, folders = self.run_batch()
        self.assertEqual(folders[self.folders[0]]["status"], "imported", self.log)

    def test_relative_stored_path_is_refused(self):
        # Discovery only stores absolute paths; a relative one (tampered
        # state) failing closed is the safe outcome.
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.folders[0] = "A"
        _, folders = self.run_batch()
        self.assertEqual(folders["A"]["failure_reason"], "unverifiable_folder")
        self.assertNotIn("A", [c.args[0] for c in self.stubs["reimport_source"].call_args_list])


class SymlinkedDownloadRootEndToEnd(e2e._Roots):
    """(2) NAS-style: the configured downloads root is itself a symlink. Real
    discovery; only Beets and providers are stubbed."""

    def test_folders_under_a_symlinked_download_root_still_import(self):
        real = self.tmp / "pool_downloads"
        real.mkdir()
        (real / "Artist" / "Album").mkdir(parents=True)
        (real / "Artist" / "Album" / "01.flac").write_bytes(b"x")
        link = self.tmp / "dl_link"
        self.symlink(link, real)
        calls = []

        def fake_reimport(folder, opts, timeout):
            calls.append(folder)
            return {"ok": True, "albums_imported": 1, "imported_album_ids": [1], "skipped": [],
                    "not_matched": [], "not_matched_known": True}

        patches = [
            e2e.patch_app_family(e2e.APP, "_DOWNLOADS_ROOTS", [str(link)]),
            e2e.patch_app_family(e2e.APP, "TORRENT_SOURCE_ROOTS", [link]),
            e2e.patch_app_family(e2e.APP, "_IMPORT_SOURCE_ALLOWED_ROOTS", (link, self.music)),
            e2e.patch_app_family(e2e.APP, "_ai_api_key", return_value=""),
            mock.patch.object(e2e.ai, "_ai_batch_already_in_library", return_value=False),
            mock.patch.object(e2e.ai, "_validate_import_source_audio"),
            mock.patch.object(e2e.ai.composite_workflows, "reimport_source", side_effect=fake_reimport),
            mock.patch.object(e2e.ai.composite_workflows, "get_album", return_value={"mb_releasegroupid": RG}),
            mock.patch.object(e2e.ai, "_record_recent_import_for_album"),
            mock.patch.object(e2e.ai.beets_adapter, "get_plugin_status",
                              return_value={"capabilities": ["mbsubmit"], "fpcalc_available": True}),
            mock.patch.object(e2e.ai, "_invalidate_lib_cache"),
            mock.patch.object(e2e.ai, "_trigger_plex_refresh"),
            e2e.patch_app_family(e2e.APP, "_AI_PENDING_FILE", self.tmp / "pending.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        log = []
        # Scanning the resolved root: discovered folders are real paths, so the
        # Stage 1 symlink/canonical check never refuses them.
        out = e2e.ai._run_ai_batch_import("qa394-r3-real", str(real), log, None, None)
        self.assertEqual(calls, [str(real / "Artist" / "Album")], "\n".join(log))
        self.assertEqual([f["status"] for f in out["folders"]], ["imported"])
        # N7 (fixed in #394): the symlinked spelling of the root is that root;
        # the scan runs on the resolved path, so the folder imports the same way.
        calls.clear()
        (real / "Artist" / "Album2").mkdir()
        (real / "Artist" / "Album2" / "01.flac").write_bytes(b"x")
        out = e2e.ai._run_ai_batch_import("qa394-r3-link", str(link), log, None, None)
        self.assertIn(str(real / "Artist" / "Album2"), calls, "\n".join(log))
        self.assertTrue(all(c.startswith(str(real)) for c in calls), calls)


class RecursiveAudioNormalFolders(unittest.TestCase):
    """(3) Fail-closed _recursive_audio is unchanged for readable folders."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_qa394_ra_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_plain_album(self):
        for n in ("01.flac", "02.MP3", "cover.jpg"):
            (self.tmp / n).write_bytes(b"x")
        files, linked = ii._recursive_audio(str(self.tmp))
        self.assertEqual((len(files), linked), (2, False))

    def test_multi_disc_and_hidden_and_empty_subfolders(self):
        for rel in ("CD1/01.flac", "CD2/01.flac", ".hidden/x.txt", "empty/.keep"):
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / rel).write_bytes(b"x")
        files, linked = ii._recursive_audio(str(self.tmp))
        self.assertEqual((len(files), linked), (2, False))

    def test_folder_without_audio_is_not_flagged_linked(self):
        (self.tmp / "notes.txt").write_bytes(b"x")
        self.assertEqual(ii._recursive_audio(str(self.tmp)), ([], False))

    def test_missing_folder_fails_closed(self):
        self.assertEqual(ii._recursive_audio(str(self.tmp / "gone")), ([], True))

    def test_identify_does_not_flag_a_normal_folder(self):
        (self.tmp / "01.flac").write_bytes(b"x")
        ev = {"audio_files": [str(self.tmp / "01.flac")], "folder_track_count": 1}
        with mock.patch.object(ii, "_build_folder_evidence", return_value=ev), \
                mock.patch.object(ii, "_acoustid_lookup_cached_outcome", side_effect=RuntimeError("offline")), \
                mock.patch.object(ii, "_embedded_mb_ids", return_value={"mb_trackid": "", "mb_albumid": "",
                                                                       "mb_releasegroupid": ""}), \
                mock.patch.object(ii, "_text_search", return_value=[]):
            out = ii.identify_import_folder(str(self.tmp), "no_strong_match")
        self.assertNotIn("symlinked_audio_file", out["conflicts"])
        self.assertFalse(any("symlinked" in r for r in out["auto_import"]["reasons"]), out["auto_import"])


if __name__ == "__main__":
    unittest.main()
