"""QA #387 adversarial regression tests: Import Review delete/cleanup act on the
literal authorized path only (N1), and a Needs MB ID album id opens only that
album's own folder (N2). Real temp folders; the unlink/rename sinks are not
mocked."""

import os
import unittest
from pathlib import Path

try:
    from test_sec002_app_path_import_review_cleanup import ImportReviewCleanupPathBoundaryTests as _Base
except ImportError:  # pragma: no cover
    from tests.test_sec002_app_path_import_review_cleanup import ImportReviewCleanupPathBoundaryTests as _Base

POSIX = os.name != "nt"


class ReviewLiteralPathTests(unittest.TestCase):
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    _write_audio = _Base._write_audio
    _write_pending = _Base._write_pending
    _post_cleanup = _Base._post_cleanup
    _post_delete_folder = _Base._post_delete_folder
    _no_mb_album_patches = _Base._no_mb_album_patches
    _start = _Base._start

    def _assert_only_reviewed_deleted(self, reviewed_name, victim_names):
        reviewed = self.downloads / reviewed_name
        track = self._write_audio(reviewed / "a.flac")
        victims = [self._write_audio(self.downloads / v / "a.flac") for v in victim_names]
        self._write_pending(reviewed)
        response = self._post_delete_folder(reviewed)
        data = response.get_json()
        self.assertEqual(response.status_code, 200, data)
        self.assertFalse(track.exists(), data)
        for v in victims:
            self.assertTrue(v.exists(), f"{v} was deleted while deleting {reviewed_name!r}")

    # -- N1: encodings never redirect the delete -------------------------------
    def test_double_encoded_folder(self):
        self._assert_only_reviewed_deleted("Mix%2520Tape", ["Mix%20Tape", "Mix Tape"])

    def test_plus_is_literal(self):
        self._assert_only_reviewed_deleted("Mix+Tape", ["Mix Tape"])

    def test_mixed_case_percent_space(self):
        self._assert_only_reviewed_deleted("Mix%2fTape", ["Mix%2FTape", "Mix Tape"])

    def test_encoded_separator_in_folder_name_is_literal_or_refused(self):
        reviewed = self.downloads / "a%2Fb"
        track = self._write_audio(reviewed / "x.flac")
        victim = self._write_audio(self.downloads / "a" / "b" / "x.flac")
        self._write_pending(reviewed)
        self._post_delete_folder(reviewed)
        self.assertTrue(victim.exists())
        del track  # either deleted literally or refused; both are safe

    def test_encoded_dotdot_folder_is_literal(self):
        reviewed = self.downloads / "sub" / "%2e%2e"
        self._write_audio(reviewed / "x.flac")
        victim = self._write_audio(self.downloads / "keep" / "x.flac")
        sibling = self._write_audio(self.downloads / "sub" / "y.flac")
        self._write_pending(reviewed)
        self._post_delete_folder(reviewed)
        self.assertTrue(victim.exists())
        self.assertTrue(sibling.exists())

    def test_request_must_equal_reviewed_path_not_its_decoding(self):
        reviewed = self.downloads / "Mix Tape"
        reviewed_track = self._write_audio(reviewed / "a.flac")
        unreviewed_track = self._write_audio(self.downloads / "Mix%20Tape" / "a.flac")
        self._write_pending(reviewed)
        response = self._post_delete_folder(self.downloads / "Mix%20Tape")
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertTrue(reviewed_track.exists())
        self.assertTrue(unreviewed_track.exists())

    @unittest.skipUnless(POSIX, "case-sensitive filesystem")
    def test_case_variant_request_is_refused(self):
        reviewed = self.downloads / "mix tape"
        self._write_audio(reviewed / "a.flac")
        other = self._write_audio(self.downloads / "MIX TAPE" / "a.flac")
        self._write_pending(reviewed)
        response = self._post_delete_folder(self.downloads / "MIX TAPE")
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertTrue(other.exists())

    @unittest.skipUnless(POSIX, "backslash is a literal name character only on POSIX")
    def test_backslash_folder_name_is_literal(self):
        self._assert_only_reviewed_deleted("a\\b", ["a/b"])

    def test_trailing_dot_folder_is_distinct(self):
        if not POSIX:
            self.skipTest("Windows strips trailing dots")
        self._assert_only_reviewed_deleted("Album.", ["Album"])

    def test_cleanup_encoded_and_windows_file_names(self):
        folder = self.downloads / "Review"
        self._write_audio(folder / "keep.flac")
        outside = self._write_audio(self.downloads / "victim.flac")
        nested = self._write_audio(folder / "a" / "b.flac")
        self._write_pending(folder)
        files = ["..\\victim.flac", "%2e%2e/victim.flac", "a%2Fb.flac", "a%252Fb.flac",
                 "..%2Fvictim.flac", "/" + str(outside).lstrip("/")]
        response = self._post_cleanup(folder, files, action="delete_rejected", allow_delete=True)
        data = response.get_json()
        self.assertEqual(response.status_code, 200, data)
        self.assertEqual(data["deleted_count"], 0, data)
        self.assertTrue(outside.exists())
        self.assertTrue(nested.exists())

    # -- legitimate deletes still work -----------------------------------------
    def test_legit_folder_delete_with_spaces_brackets_unicode(self):
        self._assert_only_reviewed_deleted("Björk - Homogenic [FLAC] (1997) 100%", ["Björk - Homogenic"])

    def test_legit_cleanup_relative_names(self):
        folder = self.downloads / "Artist - Album"
        bad = self._write_audio(folder / "03 Wrong Track.flac")
        nested = self._write_audio(folder / "CD2" / "01 x.flac")
        good = self._write_audio(folder / "01 Good.flac")
        self._write_pending(folder)
        response = self._post_cleanup(folder, ["03 Wrong Track.flac", "CD2/01 x.flac"],
                                      action="delete_rejected", allow_delete=True)
        data = response.get_json()
        self.assertEqual(response.status_code, 200, data)
        # #380 F3: delete_rejected is downgraded to a restorable quarantine.
        self.assertEqual((data["deleted_count"], data["quarantined_count"]), (0, 2), data)
        self.assertFalse(bad.exists())
        self.assertFalse(nested.exists())
        self.assertTrue(good.exists())

    # -- N2 ---------------------------------------------------------------------
    def _matches(self, items, folder):
        from backend import library_service
        self._start(self._no_mb_album_patches(items))
        return library_service._library_no_mb_album_matches_folder(7, str(folder))

    def test_n2_disc_with_space_names(self):
        album = self.music / "Artist" / "Album"
        d1 = self._write_audio(album / "Disc 1" / "01.flac")
        d2 = self._write_audio(album / "Disc 2" / "01.flac")
        self.assertTrue(self._matches([d1, d2], album))

    def test_n2_single_disc_subfolder_requires_album_folder(self):
        album = self.music / "Artist" / "Album"
        d1 = self._write_audio(album / "CD1" / "01.flac")
        self.assertTrue(self._matches([d1], album))

    def test_n2_disc_folder_with_foreign_sibling_disc_refused(self):
        album = self.music / "Artist" / "Album"
        d1 = self._write_audio(album / "CD1" / "01.flac")
        self._write_audio(album / "CD2" / "01.flac")  # not an item
        self.assertFalse(self._matches([d1], album))

    def test_n2_nested_foreign_audio_refused(self):
        album = self.music / "Artist" / "Album"
        t = self._write_audio(album / "01.flac")
        self._write_audio(album / "Bonus" / "99.FLAC")
        self.assertFalse(self._matches([t], album))

    def test_n2_sidecars_allowed(self):
        album = self.music / "Artist" / "Album"
        t = self._write_audio(album / "01.flac")
        (album / "cover.jpg").write_bytes(b"x")
        (album / "rip.log").write_text("x")
        self.assertTrue(self._matches([t], album))

    def test_n2_subfolder_of_album_refused(self):
        album = self.music / "Artist" / "Album"
        t = self._write_audio(album / "01.flac")
        sub = album / "Scans"
        sub.mkdir()
        self.assertFalse(self._matches([t], sub))

    def test_n2_music_root_refused(self):
        t = self._write_audio(self.music / "Artist" / "Album" / "01.flac")
        self.assertFalse(self._matches([t], self.music))

    def test_n2_relative_item_paths(self):
        album = self.music / "Artist" / "Album"
        self._write_audio(album / "01.flac")
        self.assertTrue(self._matches(["Artist/Album/01.flac"], album))
        self.assertFalse(self._matches(["Artist/Album/01.flac"], album.parent))


if __name__ == "__main__":
    unittest.main()
