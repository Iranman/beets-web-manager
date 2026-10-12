"""Reimport/import resolver identity follow-ups (#264, #261 F-6).

All MusicBrainz data is synthetic; nothing here calls a provider.
"""
import inspect
import unittest
from unittest.mock import MagicMock, patch

import backend.import_service as imp
import backend.library_service as ls

RG = "11111111-1111-1111-1111-111111111111"
REL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FOLDER = "/downloads/Artist - Album"


def _resolve(mb_input, *, source_folder=FOLDER, group=(), fitting=(), rg_first=REL_A,
             has_tracks=lambda r: r != RG):
    log, preflights = [], []
    search = MagicMock(return_value=[{"mb_albumid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                                      "artist": "Artist", "album": "Album", "tracks": 10}])

    def _pre(_f, rel, **_k):
        preflights.append(rel)
        return {"ok": rel in fitting, "matches": 10, "expected": 10, "audio_count": 10}

    with patch.object(ls, "_folder_import_track_count", return_value=10), \
         patch.object(ls, "_resolve_release_group_to_release", return_value=rg_first), \
         patch.object(ls, "_mb_release_group_candidates",
                      return_value=[{"mb_albumid": r, "track_count": 10} for r in group]), \
         patch.object(ls, "_folder_release_preflight", side_effect=_pre), \
         patch.object(ls, "_mb_release_search", search), \
         patch.object(ls, "_mb_release_search_by_folder_tracks", search), \
         patch.object(ls, "_resolve_mb_release_id", side_effect=lambda s, _l: s.lower()), \
         patch.object(ls, "_mb_release_has_tracks", side_effect=has_tracks):
        got = ls._resolve_album_release_for_import(mb_input, "Artist", "Album", "", 10, log,
                                                   source_folder=source_folder)
    return got, log, preflights, search


class TextOnlyIdentityTests(unittest.TestCase):
    """#261 F-6: identity inferred from a text search alone goes to review."""

    def test_no_folder_free_search_refuses_and_does_not_search(self):
        got, log, _, search = _resolve("", source_folder="")
        self.assertEqual(got, "")
        search.assert_not_called()
        self.assertTrue(any("text match alone never sets album identity" in line for line in log))

    def test_folder_backed_search_still_needs_a_passing_preflight(self):
        got, _, _, _ = _resolve("", fitting=())
        self.assertEqual(got, "")
        got, _, _, _ = _resolve("", fitting={"cccccccc-cccc-cccc-cccc-cccccccccccc"})
        self.assertEqual(got, "cccccccc-cccc-cccc-cccc-cccccccccccc")


class ReleaseGroupResolverTests(unittest.TestCase):
    """#264."""

    def test_bare_rg_uuid_lookup_failure_fails_closed(self):
        # MusicBrainz 503 / empty group: never the free search.
        got, log, _, search = _resolve(RG, rg_first="")
        self.assertEqual(got, "")
        search.assert_not_called()
        self.assertTrue(any("REFUSED" in line for line in log))

    def test_candidate_loop_is_capped(self):
        group = [f"{i:08x}-0000-0000-0000-000000000000" for i in range(1, 60)]
        got, log, preflights, _ = _resolve(f"https://musicbrainz.org/release-group/{RG}", group=group)
        self.assertEqual(got, "")
        # The ranked release plus at most the cap of alternatives.
        self.assertLessEqual(len(preflights), 1 + ls._RG_ALTERNATIVE_RELEASE_CAP)
        self.assertTrue(any("closest by track count" in line for line in log))

    def test_import_with_id_passes_the_release_group_url(self):
        src = inspect.getsource(imp.start_folder_import_with_id)
        self.assertIn('f"https://musicbrainz.org/release-group/{selected_releasegroupid}"', src)

    def test_reimport_disk_accepts_release_group_only(self):
        started = {}

        def _start(_fn, label="", metadata=None):
            started.update(metadata or {})
            return MagicMock(job_id="j1")

        with patch.object(imp.composite_workflows, "inspect_import_source",
                          return_value={"ok": True, "path": FOLDER, "audio_file_count": 3}), \
             patch.object(imp, "_library_album_ids_for_folder", return_value=[]), \
             patch.object(imp.jobs, "start_python", side_effect=_start):
            body, status = imp.start_reimport_disk({"aldir": FOLDER, "mb_releasegroupid": RG})
        self.assertEqual(status, 200, body)
        self.assertEqual(started["mb_albumid"], f"https://musicbrainz.org/release-group/{RG}")

    def test_reimport_disk_still_requires_an_id(self):
        body, status = imp.start_reimport_disk({"aldir": FOLDER})
        self.assertEqual(status, 400)
        self.assertIn("mb_albumid required", body["error"])


if __name__ == "__main__":
    unittest.main()
