"""QA #267: upgrade-safety pins for the #251 path-default change."""

import unittest
from pathlib import Path
from unittest import mock


class PlexMappingWithoutDataAliases(unittest.TestCase):
    """Plex at /data/media/music still maps with only MUSIC_ROOT=/music:
    the Plex section location, not a built-in alias, supplies the root."""

    def test_section_location_translates_beets_path(self):
        import backend.plex_service as plex
        settings = {"plex_music_roots": "", "beets_music_root": "/music"}
        with mock.patch.object(plex, "PLAYLIST_PATH_ROOT_ALIASES", ["/music"]), \
                mock.patch.object(plex, "MUSIC_ROOT", Path("/music")):
            got = plex._plex_translate_beets_path(
                "/music/Artist/Album/01 - Song.flac", settings,
                section_locations=["/data/media/music"])
        self.assertEqual(got["relative_path"], "Artist/Album/01 - Song.flac")
        self.assertEqual(got["translated_path"], "/data/media/music/Artist/Album/01 - Song.flac")


class LayoutMarkersOutsideData(unittest.TestCase):
    """Intended divergence: "<any>/media/music/" and "<any>/torrents/music/"
    now split like their /data/ forms (main returned the mount segments)."""

    CASES = {
        "/mnt/media/music/Aaliyah (2001)/Aaliyah/01 - Aaliyah - We Need a Resolution.flac":
            ["aaliyah 2001", "aaliyah", "01"],
        "/srv/torrents/music/X/Y/03 - Z.flac": ["x", "y", "03"],
    }

    def test_track_path_prefixes(self):
        from backend.matching import track_path_prefixes
        for path, expected in self.CASES.items():
            self.assertEqual(track_path_prefixes(path), expected, path)


if __name__ == "__main__":
    unittest.main()
