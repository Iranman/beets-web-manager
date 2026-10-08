"""#299 F1: a source directly under a configured root has no artist folder,
so the root's name ("downloads") never fails the artist check."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.matching_service as ms

MB = {"ok": True, "release_title": "Plain LP", "release_artist": "Bwm Synthetic",
      "release_group": "88888888-8888-8888-8888-888888888888",
      "tracks": [{"title": "Track One", "track": 1}]}


class FolderArtistRootTests(unittest.TestCase):
    def _run(self, rel: str):
        with tempfile.TemporaryDirectory() as tmp:
            music, downloads = Path(tmp) / "music", Path(tmp) / "downloads"
            src = downloads / rel
            src.mkdir(parents=True)
            with mock.patch.object(ms, "MUSIC_ROOT", music), \
                 mock.patch.object(ms, "DOWNLOADS_ROOT", downloads), \
                 mock.patch.object(ms, "_fetch_mb_release_tracklist", return_value=MB), \
                 mock.patch.object(ms.composite_workflows, "inspect_import_source",
                                   return_value={"ok": True, "audio_files": []}):
                return ms._folder_release_preflight(str(src), "11111111-1111-1111-1111-111111111111")

    def test_matching_artist_folder(self):
        res = self._run("Bwm Synthetic/Plain LP")
        self.assertEqual(res["folder_artist"], "Bwm Synthetic")
        self.assertTrue(res["artist_ok"])

    def test_non_matching_artist_folder(self):
        res = self._run("Zzqx Unrelated/Plain LP")
        self.assertEqual(res["folder_artist"], "Zzqx Unrelated")
        self.assertFalse(res["artist_ok"])

    def test_downloads_root_parent_skips_artist_check(self):
        res = self._run("Plain LP")
        self.assertEqual(res["folder_artist"], "")
        self.assertTrue(res["artist_ok"])


if __name__ == "__main__":
    unittest.main()
