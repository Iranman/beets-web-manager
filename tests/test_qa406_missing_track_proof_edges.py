"""QA #406: edge cases for the missing-track recording proof and the
stale-review prune that the PR's own tests do not cover. Synthetic data only."""
import os
import unittest
from pathlib import Path
from unittest import mock

import backend.matching_service as ms
import tests.test_missing_track_recording_proof as base
from tests.test_missing_track_recording_proof import ALBUM_ID, REL, RECS, TITLES, TRACKS, _hit


def _wanted(*numbers):
    return [dict(TRACKS[n - 1]) for n in numbers]


class ProofEdgeTests(base._Fixture):
    def test_same_track_in_two_formats_goes_to_review(self):
        mp3 = self.folder / "02 Two.mp3"
        mp3.write_bytes(b"synthetic")
        self.hits[str(mp3)] = [_hit(RECS[1])]
        self.assertRefused(self.preflight(), "duplicate_track")

    def test_nested_unverified_audio_is_checked(self):
        nested = self.folder / "extras" / "06 Bonus.flac"
        nested.parent.mkdir()
        nested.write_bytes(b"synthetic")
        self.hits[str(nested)] = []
        self.assertRefused(self.preflight(), "file_unverified")

    def test_copy_of_held_track_without_album_mbids_is_not_requested(self):
        for item in self.items:
            item["mb_trackid"] = ""
        extra = self.folder / "01 One.flac"
        extra.write_bytes(b"synthetic")
        self.hits[str(extra)] = [_hit(RECS[0])]
        self.assertRefused(self.preflight(), "track_not_requested")

    def test_repeated_recording_already_held_goes_to_review(self):
        # The release repeats track 1's recording at track 5; the album holds track 1.
        repeat = [dict(t) for t in TRACKS]
        repeat[4]["mb_trackid"] = RECS[0]
        orig = self._fetch
        self._fetch = lambda rid, log=None: {**orig(rid, log), "tracks": [dict(t) for t in repeat]}
        self.hits[self.paths[5]] = [_hit(RECS[0])]
        pre = self._run(ms._folder_release_preflight, str(self.folder), REL, existing_album_id=ALBUM_ID,
                        log=None, wanted_tracks=[dict(TRACKS[1]), dict(TRACKS[3]), dict(repeat[4])])
        self.assertRefused(pre, "duplicate_track")

    def test_empty_wanted_list_never_runs_the_proof(self):
        pre = self._run(ms._folder_release_preflight, str(self.folder), REL,
                        existing_album_id=ALBUM_ID, log=None, wanted_tracks=[])
        self.assertFalse(pre["ok"])
        self.assertNotIn("missing_track_proof", pre)

    def test_engine_inspect_path_without_local_files_goes_to_review(self):
        inspect = {"ok": True, "audio_files": [
            {"relative_path": f"0{n} {TITLES[n - 1]}.flac", "properties": {"track": n}} for n in (2, 4, 5)]}
        patches = [p for p in self._patches() if getattr(p, "attribute", "") != "DOWNLOADS_ROOT"]
        patches += [mock.patch.object(ms, "DOWNLOADS_ROOT", Path(self.tmp.name) / "elsewhere"),
                    mock.patch.object(ms.composite_workflows, "inspect_import_source", return_value=inspect)]
        for p in patches:
            p.start()
        try:
            pre = ms._folder_release_preflight(str(self.folder), REL, existing_album_id=ALBUM_ID,
                                               log=None, wanted_tracks=_wanted(2, 4, 5))
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertRefused(pre, "local_scan_unavailable")


class ImportStageAfterProofTests(base._Fixture):
    """start_reimport_disk re-runs the preflight WITHOUT wanted_tracks after the
    resolver accepts the proof, so a proven fill only continues when the
    filename-based missing-track scan also selects the files."""

    def _scan(self):
        import backend.library_service as ls
        comp = {"missing": [dict(TRACKS[n - 1]) for n in (2, 4, 5)], "expected_count": 5, "in_library": 2,
                "artist": "Synthetic Artist", "album": "Some Album"}
        with mock.patch.object(ls, "_album_mb_completeness", return_value=comp), \
                mock.patch.object(ls, "_fetch_mb_release_tracklist", side_effect=self._fetch):
            return ls._source_audio_missing_track_scan(str(self.folder), ALBUM_ID, REL, [])

    def test_second_preflight_still_refuses_but_named_files_are_staged(self):
        pre = self._run(ms._folder_release_preflight, str(self.folder), REL, existing_album_id=ALBUM_ID, log=[])
        self.assertFalse(pre["ok"])
        scan = self._scan()
        self.assertTrue(scan["ok"])
        self.assertEqual(sorted(Path(p).name for p in scan["useful_files"]),
                         sorted(Path(self.paths[n]).name for n in (2, 4, 5)))

    def test_opaque_names_are_refused_at_the_resolver_not_after_it(self):
        # Opaque names give the whole-album decision a tracklist conflict, so the
        # proof never runs; the later filename scan could not stage them either.
        for n in (2, 4, 5):
            opaque = self.folder / f"file{n}.flac"
            Path(self.paths[n]).rename(opaque)
            self.hits[str(opaque)] = self.hits.pop(self.paths[n])
        self.assertRefused(self.preflight(), "identity_conflict")
        self.assertEqual(self._scan()["useful_files"], [])


class PruneEdgeTests(base.StaleReviewItemTests):
    def test_unreadable_path_is_not_treated_as_gone(self):
        target = self.downloads / "locked" / "album"
        real_lstat = os.lstat

        def lstat(path, *a, **k):
            if str(path) == str(target):
                raise PermissionError(13, "denied")
            return real_lstat(path, *a, **k)
        with mock.patch("os.lstat", side_effect=lstat):
            self.assertEqual(self._load([self._item(target)]), [str(target)])
        self.assertEqual(self.resolved, [])

    def test_traversal_out_of_downloads_is_kept(self):
        sneaky = os.path.join(str(self.downloads), "..", "elsewhere", "missing")
        self.assertEqual(self._load([self._item(sneaky)]), [sneaky])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinks_are_kept(self):
        outside = self.root / "outside"
        outside.mkdir()
        out_link = self.downloads / "to-outside"
        out_link.symlink_to(outside, target_is_directory=True)
        dangling = self.downloads / "dangling"
        dangling.symlink_to(self.root / "nope")
        self.assertEqual(self._load([self._item(out_link), self._item(dangling)]),
                         [str(out_link), str(dangling)])


if __name__ == "__main__":
    unittest.main()
