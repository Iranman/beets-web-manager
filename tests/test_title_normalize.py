import unittest

from backend.title_normalize import restore_time_colon_title
from helpers_mb import _clean_for_mb


class TitleNormalizeTests(unittest.TestCase):
    def test_restores_time_like_album_title_colon(self):
        self.assertEqual(restore_time_colon_title("14-59"), "14:59")
        self.assertEqual(restore_time_colon_title("Sugar Ray - 14-59"), "Sugar Ray - 14:59")
        self.assertEqual(restore_time_colon_title("14-59 - Sugar Ray"), "14:59 - Sugar Ray")

    def test_leaves_non_time_hyphen_titles_alone(self):
        self.assertEqual(restore_time_colon_title("blink-182"), "blink-182")
        self.assertEqual(restore_time_colon_title("1999-09"), "1999-09")
        self.assertEqual(restore_time_colon_title("25-99"), "25-99")
        self.assertEqual(restore_time_colon_title("B.O.A.T.S. II #METIME"), "B.O.A.T.S. II #METIME")

    def test_musicbrainz_cleaning_uses_colon_restoration(self):
        title, artist = _clean_for_mb("14-59", "Sugar Ray")

        self.assertEqual(title, "14:59")
        self.assertEqual(artist, "Sugar Ray")


if __name__ == "__main__":
    unittest.main()
