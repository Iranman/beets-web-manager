import unittest
from pathlib import Path

from project_docs import read_operator_docs


SINGLE_TRACK_TEMPLATE = (
    "$albumartist%if{$mb_albumartistid, ($mb_albumartistid),}/"
    "$album (%left{$year,4})%if{$mb_releasegroupid, {$mb_releasegroupid$}}/"
    "$artist - $album - %right{00$track,2} - $title ($disc)%if{$mb_artistid,{$mb_artistid$}}"
)
DEFAULT_TEMPLATE = (
    "$albumartist%if{$mb_albumartistid, ($mb_albumartistid),}/"
    "$album (%left{$year,4})%if{$mb_releasegroupid, {$mb_releasegroupid$}}/"
    "$albumartist - $album - %right{00$track,2} - $title"
)


class SingleTrackPathFormatTests(unittest.TestCase):
    def test_single_track_template_is_shared(self):
        root = Path(__file__).resolve().parents[1]
        config_path = root / "config.yaml"
        if not config_path.exists():
            config_path = root / "config.yaml.example"
        config_source = config_path.read_text(encoding="utf-8")
        app_source = (root / "app.py").read_text(encoding="utf-8")
        docs_source = read_operator_docs(root)

        self.assertIn(f'singleton: "{SINGLE_TRACK_TEMPLATE}"', config_source)
        self.assertIn(f'default: "{DEFAULT_TEMPLATE}"', config_source)
        self.assertIn("def _write_playlist_import_beets_config", app_source)
        self.assertIn('_ARTIST_FOLDER_PATH_TEMPLATE = "$albumartist%if{$mb_albumartistid, ($mb_albumartistid),}"', app_source)
        self.assertIn("_SINGLE_TRACK_PATH_TEMPLATE = _ARTIST_FOLDER_PATH_TEMPLATE +", app_source)
        self.assertIn("$albumartist%if{$mb_albumartistid, ($mb_albumartistid),}", app_source)
        self.assertIn(SINGLE_TRACK_TEMPLATE, docs_source)
        self.assertIn(
            "The Album Artist (Album ArtistMbId)/The Album Title (2026) {Release Group MbId}/The Artist Name - The Album Title - 03 - Track Title (1){Track ArtistMbId}",
            docs_source,
        )
        self.assertNotIn('singleton: "$album (%left{$year,4})', config_source)
        self.assertNotIn("singleton: Non-Album/$albumartist - $title", config_source)
        self.assertNotIn("{$mb_releasegroupid},}", config_source)
        self.assertNotIn("$disc_subfolder", config_source)

    def test_template_token_cleanup_catches_leaked_filename_template(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")

        self.assertIn("albumartist|album|artist|title|track|disc|year", app_source)
        self.assertIn("_album_template_token_cleanup_candidates", app_source)
        self.assertIn("_template_token_row_target", app_source)
        self.assertIn("_clean_malformed_release_group_stamps", app_source)
        self.assertIn("Lidarr ID in track filename", app_source)
        self.assertIn("Rename to clean MusicBrainz title", app_source)
        self.assertIn("_track_filename_has_source_id_suffix", app_source)
        self.assertIn("duplicate_check_required", app_source)
        self.assertIn("_maintenance_same_file_hash", app_source)
        self.assertIn("filename_cleanup", app_source)


if __name__ == "__main__":
    unittest.main()

