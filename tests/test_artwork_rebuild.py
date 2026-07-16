import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
JOBS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Jobs.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")


class ArtworkRebuildTests(unittest.TestCase):
    def test_full_rebuild_endpoint_requires_confirmation(self):
        self.assertIn('@app.post("/api/rebuild-album-art")', APP_SOURCE)
        self.assertIn('Confirmation is required before rebuilding album art', APP_SOURCE)
        self.assertIn('metadata={"type": "album-art-rebuild", "mode": "full_rebuild"}', APP_SOURCE)

    def test_rebuild_quarantines_and_restores_existing_art(self):
        self.assertIn('def _album_art_quarantine_current', APP_SOURCE)
        self.assertIn('def _album_art_restore_quarantine', APP_SOURCE)
        self.assertIn('album-art-rebuild-trash', APP_SOURCE)
        self.assertIn('restored_current_art', APP_SOURCE)
        self.assertIn('Current art is quarantined first and restored if no fresh cover is confirmed.', APP_SOURCE)

    def test_repair_album_art_supports_force_rebuild(self):
        self.assertIn('force: bool = False', APP_SOURCE)
        self.assertIn('force=True, trash_root=trash_root', APP_SOURCE)
        self.assertIn('removed current art before fresh fetch', APP_SOURCE)
        self.assertIn('confirmed fresh art', APP_SOURCE)

    def test_frontend_exposes_confirmed_full_rebuild_action(self):
        self.assertIn('rebuildAlbumArt', CLIENT_SOURCE)
        self.assertIn("/api/rebuild-album-art", CLIENT_SOURCE)
        self.assertIn("jsonRequest('POST', { confirmed: true })", CLIENT_SOURCE)
        self.assertIn("label: 'Full rebuild'", JOBS_SOURCE)
        self.assertIn("dangerous: true", JOBS_SOURCE)
        self.assertIn("Rebuild album art for the entire library?", JOBS_SOURCE)
        self.assertIn("'full-rebuild': rebuildAlbumArt", JOBS_SOURCE)


if __name__ == "__main__":
    unittest.main()