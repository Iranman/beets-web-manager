"""Tests for the MB ID Coverage card's job-completion handling in the frontend.

Static-analysis over source text, consistent with the rest of this suite.
"""
import unittest
from pathlib import Path


def _panel_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (
        root / "frontend" / "src" / "features" / "libraryHealth" / "LibraryHealthPanel.tsx"
    ).read_text(encoding="utf-8")


def _types_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


class MbFixResultTypeTests(unittest.TestCase):
    def setUp(self):
        self._types = _types_source()

    def test_result_type_has_repaired_skipped_unresolved_failed_fields(self):
        self.assertIn("export interface MbidStickingRepairResult", self._types)
        for field in (
            "resolved_album_rows?: number;",
            "unresolved_albums?: MbidUnresolvedAlbum[];",
            "unresolved_count?: number;",
            "skipped_already_fixed?: number;",
            "failed_count?: number;",
        ):
            self.assertIn(field, self._types)

    def test_unresolved_album_has_reason_field(self):
        self.assertIn("export interface MbidUnresolvedAlbum", self._types)
        self.assertIn("reason: string;", self._types)


class MbFixJobCompletionTests(unittest.TestCase):
    """Fixing MB ID gaps must auto-rescan and show honest counts, not a static message."""

    def setUp(self):
        self._panel = _panel_source()

    def test_success_handler_reruns_coverage_check(self):
        effect = self._panel[
            self._panel.index("useEffect(() => {\n    if (mbFixJob?.status === 'success')"):
            self._panel.index("} else if (mbFixJob?.status === 'failed'")
        ]
        self.assertIn("void handleMbCheck();", effect)

    def test_success_handler_reports_honest_no_change_state(self):
        effect = self._panel[
            self._panel.index("useEffect(() => {\n    if (mbFixJob?.status === 'success')"):
            self._panel.index("} else if (mbFixJob?.status === 'failed'")
        ]
        self.assertIn("No changes made — nothing to fix.", effect)

    def test_success_handler_surfaces_unresolved_and_failed_counts(self):
        effect = self._panel[
            self._panel.index("useEffect(() => {\n    if (mbFixJob?.status === 'success')"):
            self._panel.index("} else if (mbFixJob?.status === 'failed'")
        ]
        self.assertIn("r.unresolved_count", effect)
        self.assertIn("r.failed_count", effect)
        self.assertIn("r.skipped_already_fixed", effect)

    def test_unresolved_albums_rendered_with_reason(self):
        self.assertIn("Needs manual review", self._panel)
        self.assertIn("unresolved_albums!.slice(0, 8).map((u) =>", self._panel)
        self.assertIn("{u.reason}", self._panel)

    def test_fix_button_disabled_only_while_fix_job_running(self):
        self.assertIn(
            "disabled={mbFixJob?.status === 'running'}",
            self._panel,
        )

    def test_check_button_disabled_only_while_actually_loading_or_running(self):
        self.assertIn(
            "disabled={mbLoading || mbFixJob?.status === 'running'}",
            self._panel,
        )


if __name__ == "__main__":
    unittest.main()
