"""Every production final-decision pattern hit must be reviewed and
classified (docs/arch002_caller_audit.json), and the NEEDS_MIGRATION set may
only shrink."""

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("audit_arch002_callers", ROOT / "scripts" / "audit_arch002_callers.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

KNOWN_NEEDS_MIGRATION = {
    "app.py::_merge_imported_album_into_existing",
    "backend/import_guard.py::existing_track_can_block_downloaded_replacement",
    "backend/import_guard.py::existing_track_matches_target",
}


class CallerAuditTests(unittest.TestCase):
    def test_every_hit_bearing_unit_is_classified(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = audit.main([])
        self.assertEqual(code, 0, err.getvalue())

    def test_needs_migration_set_only_shrinks(self):
        units = json.loads(audit.CLASSIFICATION.read_text(encoding="utf-8"))["units"]
        hits = audit.hit_map()
        needs = {u for u in hits if units.get(u, [""])[0] == "NEEDS_MIGRATION"}
        self.assertLessEqual(needs, KNOWN_NEEDS_MIGRATION)

    def test_recording_decision_has_no_second_decision_tree(self):
        src = (ROOT / "backend" / "matching_contract.py").read_text(encoding="utf-8")
        start = src.index("def build_recording_matching_decision(")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn("evaluate_recording_candidate(", body)
        for legacy in ("attach_eligible = bool(", "evidence_supported", "title_only_strong", "hard_conflict_names"):
            self.assertNotIn(legacy, body)


if __name__ == "__main__":
    unittest.main()
