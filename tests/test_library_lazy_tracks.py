import unittest
from pathlib import Path


class LibraryLazyTracksTests(unittest.TestCase):
    def test_library_summary_defers_tracks_but_album_tracks_include_disk_only_rows(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        types_source = (root / "frontend" / "src" / "types" / "api.ts").read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        library_page = (root / "frontend" / "src" / "views" / "Library.tsx").read_text(encoding="utf-8")
        library_health = (root / "frontend" / "src" / "lib" / "libraryHealth.ts").read_text(encoding="utf-8")
        discography_panel = (root / "frontend" / "src" / "features" / "discography" / "DiscographyPanel.tsx").read_text(encoding="utf-8")

        self.assertIn("def _library_payload_for_response", app_source)
        self.assertIn('next_album["tracks_deferred"] = True', app_source)
        self.assertIn("def _library_album_is_disk_only", app_source)
        self.assertIn("include_disk_only", app_source)
        self.assertIn("def _library_stats_for_artists", app_source)
        self.assertIn('include_tracks = request.args.get("include_tracks", "0") == "1"', app_source)
        self.assertIn('@app.get("/api/albums/<int:aid>/tracks")', app_source)
        self.assertIn('"ok": False,', app_source)
        self.assertIn('"missing": False,', app_source)
        self.assertIn('p.suffix.lower() in AUDIO_EXT', app_source)

        self.assertIn("tracks?: LibraryTrack[];", types_source)
        self.assertIn("tracks_deferred?: boolean;", types_source)
        self.assertIn("getAlbumTracks", client_source)
        self.assertIn("await getAlbumTracks(albumId)", library_page)
        self.assertIn("if (album.tracks_deferred) return false;", library_health)
        self.assertIn("Exact MusicBrainz completeness gaps are not the same as local missing", library_health)
        self.assertIn("mbEditionGap", discography_panel)
        self.assertIn("Local files are not marked missing", discography_panel)
        self.assertIn("Track rows are deferred in the fast library payload.", library_page)
        self.assertIn("No passive issue evidence in the current library summary.", library_page)
        self.assertIn("function NextRepairPanel", library_page)
        self.assertIn("albumSuggestedStatus", library_page)
        self.assertIn("compareRepairRows", library_page)
        self.assertIn("Show issue group", library_page)


if __name__ == "__main__":
    unittest.main()
