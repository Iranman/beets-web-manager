import tempfile
import unittest
from pathlib import Path

from backend.transaction_engine import TransactionStore, metadata_diff


class TransactionEngineTests(unittest.TestCase):
    def test_metadata_diff_only_marks_changed_fields(self):
        rows = metadata_diff(
            {"artist": "311", "genre": "Rock", "year": None},
            {"artist": "311", "genre": "Alternative Rock", "year": 2019},
        )
        fields = {row["field"]: row for row in rows}
        self.assertNotIn("artist", fields)
        self.assertEqual(fields["genre"]["old"], "Rock")
        self.assertEqual(fields["genre"]["new"], "Alternative Rock")
        self.assertTrue(fields["year"]["changed"])

    def test_store_persists_lists_and_exports_transactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(tmp)
            tx = store.create(
                operation_type="Metadata Update",
                initiating_user="tester",
                status="Preview",
                dry_run=True,
                summary="Preview title fix",
                reason="Track title differs by one character.",
                source="MusicBrainz",
                confidence={"overall": 0.91, "musicbrainz": 0.98},
                changes=[{
                    "artist": "311",
                    "album": "Voyager",
                    "track": "Good Feelin'",
                    "metadata_diff": metadata_diff(
                        {"title": "Good Feelin'"},
                        {"title": "Good Feeling"},
                    ),
                    "filesystem": [{
                        "operation": "Rename",
                        "old": "01 Good Feelin.mp3",
                        "new": "01 Good Feeling.flac",
                    }],
                    "reason": "Track title differs by one character.",
                    "source": "MusicBrainz recording",
                }],
            )

            detail = store.get(tx["id"], limit=10)
            self.assertEqual(detail["changes_total"], 1)
            self.assertEqual(detail["changes"][0]["album"], "Voyager")

            rows, total = store.list(query="Voyager")
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["id"], tx["id"])

            markdown, markdown_type = store.export(tx["id"], "markdown")
            self.assertEqual(markdown_type, "text/markdown")
            self.assertIn("Good Feeling", markdown)

            csv_payload, csv_type = store.export(tx["id"], "csv")
            self.assertEqual(csv_type, "text/csv")
            self.assertIn("311", csv_payload)

    def test_settings_are_configurable_and_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(tmp)
            settings = store.save_settings({
                "dry_run_by_default": True,
                "automatic_approval_threshold": 5,
                "backup_retention_days": -1,
            })
            self.assertTrue(settings["dry_run_by_default"])
            self.assertEqual(settings["automatic_approval_threshold"], 1.0)
            self.assertEqual(settings["backup_retention_days"], 0)
            self.assertTrue((Path(tmp) / "settings.json").exists())


if __name__ == "__main__":
    unittest.main()
