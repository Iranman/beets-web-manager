"""Every production final-decision pattern hit must be reviewed and
classified (docs/arch002_caller_audit.json), and the NEEDS_MIGRATION set may
only shrink."""

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("audit_arch002_callers", ROOT / "scripts" / "audit_arch002_callers.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

#: ARCH-002 is closed: no production final decision may bypass canonical
#: matching. A new independent decision site fails this suite until it is
#: migrated -- it can never be admitted by adding it here.
KNOWN_NEEDS_MIGRATION: set = set()


class CallerAuditTests(unittest.TestCase):
    def test_every_hit_bearing_unit_is_classified(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = audit.main([])
        self.assertEqual(code, 0, err.getvalue())

    def test_needs_migration_is_zero(self):
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


class ReconciliationAuthorityTests(unittest.TestCase):
    def test_merge_delegates_to_reconciliation_service(self):
        src = app_family_source()
        start = src.index("def _merge_imported_album_into_existing(")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn("_import_reconciliation.plan_reconciliation(", body)
        self.assertIn("_import_reconciliation.album_identity(", body)
        for legacy in ("title_score", "repair_threshold", "duplicate_threshold", "_album_track_score",
                       "_guard_existing_track"):
            self.assertNotIn(legacy, body)

    def test_text_threshold_guards_are_gone(self):
        guard = (ROOT / "backend" / "import_guard.py").read_text(encoding="utf-8")
        self.assertNotIn("def existing_track_matches_target", guard)
        self.assertNotIn("def existing_track_can_block_downloaded_replacement", guard)
