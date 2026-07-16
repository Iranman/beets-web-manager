import unittest

from backend.beets_config import filter_job_plugins


class BeetsJobConfigTests(unittest.TestCase):
    def test_discpath_is_kept_for_path_templates(self):
        plugins = ["fetchart", "discpath", "plexsync", "musicbrainz"]

        filtered = filter_job_plugins(plugins)

        self.assertIn("discpath", filtered.split())
        self.assertNotIn("plexsync", filtered.split())


if __name__ == "__main__":
    unittest.main()
