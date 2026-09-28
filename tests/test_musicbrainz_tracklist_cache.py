import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class MusicBrainzTracklistCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.app_source = app_family_source()
        start = cls.app_source.index("def _mb_release_tracklist_cache_path(")
        end = cls.app_source.index("def _album_track_score(")
        cls.tracklist_source = cls.app_source[start:end]

    def test_disk_cache_constants_exist(self):
        self.assertIn("_MB_RELEASE_TRACKLIST_DISK_CACHE_TTL", self.app_source)
        self.assertIn("_MB_RELEASE_TRACKLIST_CACHE_DIR = METADATA_CACHE_ROOT", self.app_source)
        self.assertIn("mb-release-tracklists", self.app_source)

    def test_disk_cache_helpers_read_and_write_payloads(self):
        self.assertIn("def _mb_release_tracklist_read_disk(", self.tracklist_source)
        self.assertIn("def _mb_release_tracklist_write_disk(", self.tracklist_source)
        self.assertIn("json.loads(cache_path.read_text", self.tracklist_source)
        self.assertIn("copy.deepcopy(payload)", self.tracklist_source)
        self.assertIn("tmp_path.replace(cache_path)", self.tracklist_source)

    def test_fetch_uses_disk_cache_before_network(self):
        disk_idx = self.tracklist_source.index("disk_cached = _mb_release_tracklist_read_disk")
        url_idx = self.tracklist_source.index("https://musicbrainz.org/ws/2/release")
        self.assertLess(disk_idx, url_idx)
        self.assertIn("return disk_cached", self.tracklist_source)

    def test_fetch_writes_successful_tracklist_to_disk(self):
        self.assertIn("payload_copy = copy.deepcopy(result)", self.tracklist_source)
        self.assertIn("_mb_release_tracklist_write_disk(mb_albumid, payload_copy)", self.tracklist_source)


if __name__ == "__main__":
    unittest.main()
