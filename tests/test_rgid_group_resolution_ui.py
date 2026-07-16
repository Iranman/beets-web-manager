"""Tests for the Same Release Group ID card's inline resolution UI.

Static-analysis over source text, consistent with the rest of this suite.
"""
import unittest
from pathlib import Path


def _panel_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (
        root / "frontend" / "src" / "features" / "libraryHealth" / "LibraryHealthPanel.tsx"
    ).read_text(encoding="utf-8")


def _client_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")


def _types_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


class NoLongerJustRedirectsToLibraryTests(unittest.TestCase):
    """'Review details' used to be the only action and just navigated away."""

    def setUp(self):
        self._panel = _panel_source()

    def test_review_details_button_label_removed_from_rgid_row(self):
        rgid_row = self._panel[
            self._panel.index("function RgidDupGroupRow("):
        ]
        self.assertNotIn("Review details", rgid_row)

    def test_open_in_library_kept_as_secondary_link(self):
        rgid_row = self._panel[
            self._panel.index("function RgidDupGroupRow("):
        ]
        self.assertIn("Open in Library", rgid_row)

    def test_resolve_cluster_expands_inline_panel(self):
        rgid_row = self._panel[
            self._panel.index("function RgidDupGroupRow("):
        ]
        self.assertIn("Resolve cluster", rgid_row)
        self.assertIn("setExpanded((v) => !v)", rgid_row)
        self.assertIn("<RgidDetailPanel", rgid_row)

    def test_stays_on_current_page_no_forced_navigate_on_expand(self):
        rgid_row = self._panel[
            self._panel.index("function RgidDupGroupRow("):
            self._panel.index("function RgidDupGroupRow(") + self._panel[self._panel.index("function RgidDupGroupRow("):].index("Open in Library")
        ]
        # The only navigate() call left in the row is the explicit "Open in Library" link.
        self.assertEqual(rgid_row.count("navigate("), 1)


class FiveFixActionsTests(unittest.TestCase):
    """Requirement: Merge, Keep separate, Choose representative release, Relink, Repair partial import."""

    def setUp(self):
        self._panel = _panel_source()
        self._detail_panel = self._panel[
            self._panel.index("function RgidDetailPanel("):
            self._panel.index("function RgidDupGroupRow(")
        ]

    def test_merge_action_present(self):
        self.assertIn("handleMerge", self._detail_panel)
        self.assertIn("mergeRgidGroup(", self._detail_panel)

    def test_keep_separate_action_present_and_persists(self):
        self.assertIn("handleKeepSeparate", self._detail_panel)
        self.assertIn("keepRgidGroupSeparate(", self._detail_panel)

    def test_choose_representative_release_action_present(self):
        self.assertIn("handleAssign", self._detail_panel)
        self.assertIn("assignRgidRepresentativeRelease(", self._detail_panel)
        self.assertIn("candidate_releases", self._detail_panel)

    def test_relink_action_present(self):
        self.assertIn("handleRelink", self._detail_panel)
        self.assertIn("relinkRgidAlbum(", self._detail_panel)

    def test_repair_partial_import_action_present(self):
        self.assertIn("handleRepairPartial", self._detail_panel)
        self.assertIn("sendRgidAlbumToRepair(", self._detail_panel)
        self.assertIn("isPartial", self._detail_panel)


class KeepSeparatePersistenceUiTests(unittest.TestCase):
    """A 'keep separate' decision must be visible and undoable, not silently repeatable."""

    def setUp(self):
        self._detail_panel = _panel_source()
        self._detail_panel = self._detail_panel[
            self._detail_panel.index("function RgidDetailPanel("):
            self._detail_panel.index("function RgidDupGroupRow(")
        ]

    def test_shows_persisted_resolution_state(self):
        self.assertIn("detail?.resolution", self._detail_panel)
        self.assertIn("Resolved: kept as separate editions", self._detail_panel)

    def test_undo_resolution_available(self):
        self.assertIn("handleUndoResolution", self._detail_panel)
        self.assertIn("undoRgidGroupResolution(", self._detail_panel)

    def test_keep_separate_form_hidden_once_resolved(self):
        self.assertIn("{!detail?.resolution && (", self._detail_panel)


class MergeSafetyGatingUiTests(unittest.TestCase):
    def setUp(self):
        self._detail_panel = _panel_source()
        self._detail_panel = self._detail_panel[
            self._detail_panel.index("function RgidDetailPanel("):
            self._detail_panel.index("function RgidDupGroupRow(")
        ]

    def test_merge_disabled_with_reason_when_unsafe(self):
        self.assertIn("detail?.merge_safe ?", self._detail_panel)
        self.assertIn("Merge disabled:", self._detail_panel)
        self.assertIn("merge_blockers", self._detail_panel)

    def test_merge_requires_confirmation(self):
        self.assertIn("window.confirm(", self._detail_panel)


class ApiClientWiringTests(unittest.TestCase):
    def setUp(self):
        self._client = _client_source()
        self._types = _types_source()

    def test_client_exposes_all_six_rgid_functions(self):
        for fn in (
            "export function getRgidGroupDetail(",
            "export function mergeRgidGroup(",
            "export function keepRgidGroupSeparate(",
            "export function undoRgidGroupResolution(",
            "export function assignRgidRepresentativeRelease(",
            "export function relinkRgidAlbum(",
            "export function sendRgidAlbumToRepair(",
        ):
            self.assertIn(fn, self._client)

    def test_client_functions_hit_expected_routes(self):
        for route in (
            "/api/clean/rgid-group/${encodeURIComponent(rgid)}",
            "/api/clean/rgid-group/merge",
            "/api/clean/rgid-group/keep-separate",
            "/api/clean/rgid-group/undo-resolution",
            "/api/clean/rgid-group/assign-representative-release",
            "/api/clean/rgid-group/relink",
            "/api/clean/rgid-group/send-to-repair",
        ):
            self.assertIn(route, self._client)

    def test_detail_response_type_has_cluster_fields(self):
        self.assertIn("export interface RgidGroupDetailResponse", self._types)
        for field in (
            "merge_safe?: boolean;",
            "merge_blockers?: string[];",
            "resolution?: RgidResolution | null;",
            "candidate_releases: RgidCandidateRelease[];",
        ):
            self.assertIn(field, self._types)


if __name__ == "__main__":
    unittest.main()
