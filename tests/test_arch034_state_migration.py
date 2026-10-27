"""ARCH-034: the AI review/history state files moved from /config to
WEB_MANAGER_DATA_DIR. _migrate_legacy_config_state() must move existing data
to the new home exactly once, with no data loss and no clobbering of newer
data. Existing deployments have real data in the old /config location, so a
botched migration would lose a user's import/match history."""
import tempfile
import unittest
from pathlib import Path

from backend.ai_service import _migrate_legacy_config_state


class Arch034StateMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.old = base / "config" / "ai_match_history.json"
        self.new = base / "data" / "ai_match_history.json"
        self.old.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_old_exists_new_absent_migrates(self):
        self.old.write_text('[{"album": "A"}]', encoding="utf-8")
        _migrate_legacy_config_state(self.old, self.new)
        # Data moved to the new home, byte for byte; old file gone.
        self.assertTrue(self.new.exists())
        self.assertEqual(self.new.read_text(encoding="utf-8"), '[{"album": "A"}]')
        self.assertFalse(self.old.exists())

    def test_both_exist_keeps_new(self):
        self.old.write_text('["old"]', encoding="utf-8")
        self.new.parent.mkdir(parents=True, exist_ok=True)
        self.new.write_text('["new"]', encoding="utf-8")
        _migrate_legacy_config_state(self.old, self.new)
        # Newer new-location data wins; nothing is overwritten or lost.
        self.assertEqual(self.new.read_text(encoding="utf-8"), '["new"]')
        self.assertEqual(self.old.read_text(encoding="utf-8"), '["old"]')

    def test_neither_exists_is_noop(self):
        _migrate_legacy_config_state(self.old, self.new)
        self.assertFalse(self.new.exists())
        self.assertFalse(self.old.exists())

    def test_idempotent(self):
        self.old.write_text('["data"]', encoding="utf-8")
        _migrate_legacy_config_state(self.old, self.new)
        # A second (and third) call changes nothing: old is gone, new stays.
        _migrate_legacy_config_state(self.old, self.new)
        _migrate_legacy_config_state(self.old, self.new)
        self.assertTrue(self.new.exists())
        self.assertEqual(self.new.read_text(encoding="utf-8"), '["data"]')
        self.assertFalse(self.old.exists())


if __name__ == "__main__":
    unittest.main()
