import unittest
from pathlib import Path

from project_docs import read_operator_docs


class ImportReviewDeleteFolderTests(unittest.TestCase):
    def test_needs_mbid_delete_folder_stays_guarded(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        album_folder_source = app_source[
            app_source.index("def _album_folder_for_album_id"):
            app_source.index("def _source_audio_missing_track_scan")
        ]
        delete_source = app_source[
            app_source.index("def _pending_review_has_path"):
            app_source.index("def _delete_album_ids_from_db")
        ]
        queue_source = app_source[
            app_source.index('@app.get("/api/import/review-queue")'):
            app_source.index("# \u2500\u2500 AI matching")
        ]
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        review_source = (
            root / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
        ).read_text(encoding="utf-8")
        docs_source = read_operator_docs(root)

        self.assertIn("folder = fpath.parent", album_folder_source)
        self.assertIn("folder = folder.parent", album_folder_source)

        self.assertIn("def _library_no_mb_album_matches_folder", delete_source)
        self.assertIn("if not _path_is_under(folder, MUSIC_ROOT):", delete_source)
        self.assertIn("SELECT mb_albumid FROM albums WHERE id=?", delete_source)
        self.assertIn('_s(album_row["mb_albumid"]).strip()', delete_source)
        self.assertIn("SELECT path FROM items WHERE album_id=? AND path IS NOT NULL", delete_source)
        self.assertIn("if not _path_is_under(abs_path, folder):", delete_source)
        self.assertIn(
            "missing_mbid_album_match = _library_no_mb_album_matches_folder(album_id, str(resolved))",
            delete_source,
        )
        self.assertIn("or a Needs MB ID album row", delete_source)
        self.assertIn('album_id = int(payload.get("album_id") or 0)', delete_source)
        self.assertIn("album_id=album_id", delete_source)

        self.assertIn('album_folder = _album_folder_for_album_id(int(r["id"] or 0))', queue_source)
        self.assertIn('"path": album_folder', queue_source)
        self.assertIn('"folder": str(Path(album_folder).parent) if album_folder else ""', queue_source)
        self.assertIn('"folder_name": Path(album_folder).name if album_folder else ""', queue_source)

        self.assertIn("function canDeleteFolder(item: ReviewItem): boolean", review_source)
        self.assertIn("item.type === 'pending_ai' || item.type === 'library_no_mb'", review_source)
        self.assertIn(
            "deleteReviewFolder(item.path, isMusicLibraryPath(item.path), item.album_id)",
            review_source,
        )
        self.assertIn("album_id: albumId || undefined", client_source)

        self.assertIn("Needs MB ID (`library_no_mb`) Import Review rows", docs_source)
        self.assertIn("confirmed-wrong-library-folder approval", docs_source)
        self.assertIn("verify the album still has no `mb_albumid`", docs_source)


if __name__ == "__main__":
    unittest.main()
