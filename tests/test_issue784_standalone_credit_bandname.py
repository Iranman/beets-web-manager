"""#784 regression: the matching normalizers must not truncate a band name
whose last/standalone word is a credit word ("Little Feat", "Great Feat") or a
capitalised bare "With" that is part of the name ("Sleeping With Sirens").

This is the mirror of #780. #780 anchored _normalize_albumartist's feat/ft on
both word boundaries (fixing word-internal "ft" in "Daft"); here two remaining
truncations are closed:

* acoustid_service._normalize_albumartist stripped a bare "with" to end-of-line
  even when it was part of a name: "Sleeping With Sirens" -> "Sleeping".
* matching.normalize._strip_feature_suffix (a SEPARATE token stripper) dropped a
  trailing feat/ft/featuring token: normalize_artist("Little Feat") -> "little".

A credit token is a separator only when a guest actually follows it. Real
credits ("Artist feat. Guest", "Artist with Guest") must still strip.
"""
import unittest

from backend import acoustid_service
from backend.matching.normalize import normalize_artist

albumartist = acoustid_service._normalize_albumartist


class StandaloneCreditBandnameAlbumartist(unittest.TestCase):
    """Site A: acoustid_service._normalize_albumartist."""

    def test_bandname_with_standalone_credit_word_kept(self):
        self.assertEqual(albumartist("Sleeping With Sirens"), "Sleeping With Sirens")
        self.assertEqual(albumartist("Little Feat"), "Little Feat")
        self.assertEqual(albumartist("Great Feat"), "Great Feat")

    def test_real_guest_credits_still_strip(self):
        self.assertEqual(albumartist("Artist feat. Guest"), "Artist")
        self.assertEqual(albumartist("Artist featuring Guest"), "Artist")
        self.assertEqual(albumartist("Artist ft Guest"), "Artist")
        self.assertEqual(albumartist("Artist with Guest"), "Artist")
        self.assertEqual(albumartist("Artist (feat. Guest)"), "Artist")
        self.assertEqual(albumartist("Artist [ft. Guest]"), "Artist")
        # A bracketed "with" is an explicit credit marker -> strips in any case.
        self.assertEqual(albumartist("Artist (with Guest)"), "Artist")
        self.assertEqual(albumartist("Artist (With Guest)"), "Artist")


class StandaloneCreditBandnameMatchingTier(unittest.TestCase):
    """Site B: matching.normalize.normalize_artist (token-based stripper)."""

    def test_bandname_with_trailing_credit_word_kept(self):
        self.assertEqual(normalize_artist("Little Feat"), "little feat")
        self.assertEqual(normalize_artist("Great Feat"), "great feat")
        self.assertEqual(normalize_artist("Sleeping With Sirens"), "sleeping with sirens")

    def test_real_guest_credits_still_strip(self):
        self.assertEqual(normalize_artist("Artist feat. Guest"), "artist")
        self.assertEqual(normalize_artist("Artist featuring Guest"), "artist")
        self.assertEqual(normalize_artist("Artist ft Guest"), "artist")
        self.assertEqual(normalize_artist("Artist [feat. Guest]"), "artist")


if __name__ == "__main__":
    unittest.main()
