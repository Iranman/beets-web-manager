"""#780 regression: the albumartist feat/ft/with stripper must only remove a
standalone credit token that introduces a guest, never an "ft"/"feat"/"with"
substring that is part of a real word.

Before the fix, ``_normalize_albumartist`` matched ``ft`` inside "Daft"/"Theft"
(word boundary after "ft") and stripped to end of line: "Daft Punk" -> "Da",
"Theft" -> "The". That mangled the textual-artist match tier.
"""
import unittest

from backend import acoustid_service

f = acoustid_service._normalize_albumartist


class AlbumartistFeatBoundary(unittest.TestCase):
    def test_word_internal_ft_feat_is_kept(self):
        # Real names whose words merely contain "ft"/"feat" must survive intact.
        for name in ("Daft Punk", "Theft", "Kraftwerk", "Soft Cell",
                     "Great Feat", "Aircraft", "After Forever"):
            self.assertEqual(f(name), name, name)

    def test_real_guest_credits_still_strip(self):
        self.assertEqual(f("Artist feat. Guest"), "Artist")
        self.assertEqual(f("Artist feat Guest"), "Artist")
        self.assertEqual(f("Artist ft Guest"), "Artist")
        self.assertEqual(f("Artist ft. Guest"), "Artist")
        self.assertEqual(f("Artist featuring Guest"), "Artist")
        self.assertEqual(f("Artist with Guest"), "Artist")
        self.assertEqual(f("Artist (feat. Guest)"), "Artist")
        self.assertEqual(f("Artist [ft. Guest]"), "Artist")

    def test_band_names_and_comma_lists_unchanged(self):
        # Behaviour that must not regress (shared with test_sec_redos).
        self.assertEqual(f("Earth, Wind & Fire"), "Earth, Wind & Fire")
        self.assertEqual(f("Bob Marley & The Wailers"), "Bob Marley & The Wailers")
        self.assertEqual(f("Crosby, Stills, Nash & Young"),
                         "Crosby, Stills, Nash & Young")
        self.assertEqual(f("Wiz Khalifa, Juicy J"), "Wiz Khalifa")


if __name__ == "__main__":
    unittest.main()
