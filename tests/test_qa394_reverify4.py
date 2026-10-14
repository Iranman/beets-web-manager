"""QA re-verify of PR #394 at 08c1766: the pre-import check and hardlink copy,
with the real configured roots (only Beets and providers stubbed)."""
import os
import unittest
from pathlib import Path
from unittest import mock

import tests.test_import_batch_phase_b as pb
import tests.test_qa383_import_batch_e2e as e2e
from tests.test_import_batch_phase_b import RG


def setUpModule():
    pb.setUpModule()
    e2e.setUpModule()


class RealRootsBatch(e2e._Roots):
    def start_batch(self, scan_root=None, torrent_roots=()):
        self.calls = {}

        def fake_reimport(folder, opts, timeout):
            self.calls[folder] = dict(opts)
            return {"ok": True, "albums_imported": 1, "imported_album_ids": [1], "skipped": [],
                    "not_matched": [], "not_matched_known": True}

        patches = [
            e2e.patch_app_family(e2e.APP, "TORRENT_SOURCE_ROOTS", list(torrent_roots)),
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
        self.log = []
        out = e2e.ai._run_ai_batch_import(f"qa394-r4-{id(self)}", str(scan_root or self.dl), self.log, None, None)
        return None, out


class NormalFoldersStillImport(RealRootsBatch):
    def test_plain_multidisc_and_artwork_folders_import_with_move(self):
        self.audio("A/Plain/01.flac")
        (self.dl / "A/Plain/cover.jpg").write_bytes(b"jpg")
        self.audio("A/Double/CD1/01.flac")
        self.audio("A/Double/CD2/01.flac")
        (self.dl / "A/Double/Scans").mkdir()
        (self.dl / "A/Double/Scans/back.png").write_bytes(b"png")
        _, out = self.start_batch()
        self.assertEqual(sorted(self.calls), sorted([str(self.dl / "A/Plain"), str(self.dl / "A/Double")]),
                         "\n".join(self.log))
        for opts in self.calls.values():
            self.assertEqual((opts["copy"], opts["move"]), (False, True))
        self.assertEqual({f["status"] for f in out["folders"]}, {"imported"}, "\n".join(self.log))


class HardlinkedAudioEndToEnd(RealRootsBatch):
    def test_hardlinked_folder_is_copied_and_its_neighbour_moved(self):
        linked = self.audio("A/Seeding/01.flac")
        self.audio("A/Plain/01.flac")
        try:
            os.link(linked, self.outside / "torrent_copy.flac")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create hardlinks here")
        self.start_batch()
        seeding, plain = str(self.dl / "A/Seeding"), str(self.dl / "A/Plain")
        self.assertEqual((self.calls[seeding]["copy"], self.calls[seeding]["move"]), (True, False))
        self.assertEqual((self.calls[plain]["copy"], self.calls[plain]["move"]), (False, True))
        self.assertTrue(linked.exists())
        self.assertTrue(any("Hardlinked source" in line for line in self.log), self.log)

    def test_stat_failure_means_copy(self):
        f = self.audio("A/X/01.flac")
        real_lstat = os.lstat

        def flaky(p, *a, **k):
            if str(p) == str(f):
                raise PermissionError(13, "denied", str(p))
            return real_lstat(p, *a, **k)

        # N11: the check walks the folder and stats every file (lstat).
        with mock.patch.object(e2e.ai.os, "lstat", side_effect=flaky):
            self.assertTrue(e2e.ai._has_hardlinked_file(str(f.parent)))
        self.assertFalse(e2e.ai._has_hardlinked_file(str(f.parent)))


class PreImportCheckRealRoots(RealRootsBatch):
    def test_folder_outside_every_root_never_reaches_beets(self):
        self.audio("Album/01.flac", base=self.outside)
        files, conflict = e2e.ai._ai_batch_preimport_check(str(self.outside / "Album"))
        self.assertEqual(conflict["conflict"], "unverifiable_folder")

    def test_the_root_itself_is_refused(self):
        self.audio("01.flac")
        _, conflict = e2e.ai._ai_batch_preimport_check(str(self.dl))
        self.assertEqual(conflict["conflict"], "unverifiable_folder")

    def test_library_folder_is_under_a_root(self):
        self.audio("Artist/Album/01.flac", base=self.music)
        files, conflict = e2e.ai._ai_batch_preimport_check(str(self.music / "Artist/Album"))
        self.assertIsNone(conflict)
        self.assertEqual(len(files), 1)

    def test_link_spelling_of_the_root_fails_closed(self):
        # Discovery stores real paths; a stored path spelled through a link to
        # the root is not under a (resolved) root and is refused, never imported.
        self.audio("Album/01.flac")
        link = self.outside / "dl_link"
        self.symlink(link, self.dl)
        _, conflict = e2e.ai._ai_batch_preimport_check(str(link / "Album"))
        self.assertIsNotNone(conflict)

    def test_dotdot_escape_is_refused(self):
        self.audio("Album/01.flac", base=self.outside)
        _, conflict = e2e.ai._ai_batch_preimport_check(str(self.dl / ".." / "outside" / "Album"))
        self.assertEqual(conflict["conflict"], "unverifiable_folder")

    def test_scanning_the_link_spelling_of_the_root_imports_real_paths(self):
        self.audio("A/Album/01.flac")
        link = self.outside / "dl_link"
        self.symlink(link, self.dl)
        _, out = self.start_batch(scan_root=link)
        self.assertEqual(list(self.calls), [str(self.dl / "A/Album")], "\n".join(self.log))


if __name__ == "__main__":
    unittest.main()
