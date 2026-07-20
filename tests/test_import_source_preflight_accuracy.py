"""Tests for two real bugs found and fixed via live browser testing of the
Import Source tab (IntakePanel.tsx) on the TrueNAS deployment, 2026-07-20.

Bug 1 -- confirm dialog drastically undercounted what it was about to do.
`/api/import/preflight` caps its `folders` array to the first 100 entries
for the preview table ("showing first 100" note), but the true scope is
reported separately via `audio_folders` / `already_in_library_folders`
(the same fields PreflightSummary already used correctly for its own "New
folders" stat card). The rest of IntakePanel derived its folder count from
`newFolders.length` -- the length of that same capped 100-entry array --
and used it everywhere, including `ConfirmStartDialog`'s `folderCount`
prop. Live repro: a real downloads folder with 2217 new folders showed
"100 new folders" in the "Run Import All?" confirmation dialog immediately
before the user would commit to a real, mutating import job -- a 20x+
undercount of the actual scope, in the one place most likely to be read
carefully before an irreversible action.

Bug 2 -- a cold preflight scan of a large downloads folder can genuinely
take 40-60+ seconds (confirmed live: 2217 folders / 14461 files, ~41s
server-side), during which the UI changed nothing beyond a static
"Previewing..." button label and an indeterminate progress bar -- easy to
mistake for a hung page. Fixed with a ticking elapsed-seconds counter and
an explanatory message.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTAKE_SOURCE = (
    ROOT / "frontend" / "src" / "features" / "intake" / "IntakePanel.tsx"
).read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


class PreflightFolderCountAccuracyTests(unittest.TestCase):
    def test_authoritative_folder_count_is_derived_like_the_summary_card(self):
        # Must mirror PreflightSummary's own newFolders = audio_folders -
        # already_in_library_folders calculation, not the capped array length.
        fn = _section(INTAKE_SOURCE, "const newFolderCount = useMemo(", "\n\n  const failedImportFolders")
        self.assertIn("preflight.audio_folders - preflight.already_in_library_folders", fn)

    def test_confirm_dialog_uses_authoritative_count_not_capped_array_length(self):
        dialog_props = _section(INTAKE_SOURCE, "<ConfirmStartDialog", "/>")
        self.assertIn("folderCount={newFolderCount}", dialog_props)
        self.assertNotIn("folderCount={newFolders.length}", dialog_props)

    def test_summary_text_uses_authoritative_count(self):
        self.assertIn(
            "{newFolderCount} new folder{newFolderCount !== 1 ? 's' : ''} eligible for Import All",
            INTAKE_SOURCE,
        )
        # The old, buggy text must be gone, not just supplemented.
        self.assertNotIn("{newFolders.length} new folder", INTAKE_SOURCE)

    def test_import_all_buttons_gate_on_authoritative_count(self):
        self.assertIn("newFolderCount === 0", INTAKE_SOURCE)
        self.assertNotIn("newFolders.length === 0", INTAKE_SOURCE)

    def test_capped_array_is_still_used_only_for_the_preview_table_itself(self):
        # newFolders (the array) legitimately still exists for iterating the
        # visibly-capped preview list and the failedImportFolders sub-metric
        # -- this test just documents that scoping, so a future edit doesn't
        # accidentally reintroduce it as a stand-in for the real total.
        self.assertIn("const newFolders = useMemo(", INTAKE_SOURCE)
        self.assertIn(
            "newFolders.filter((folder) => folder.path.includes('/failed_imports/')).length",
            INTAKE_SOURCE,
        )


class ScanProgressFeedbackTests(unittest.TestCase):
    def test_elapsed_seconds_counter_exists_and_ticks_while_scanning(self):
        fn = _section(INTAKE_SOURCE, "const [scanElapsedSeconds, setScanElapsedSeconds]", "\n  }, [scanning]);")
        self.assertIn("if (!scanning)", fn)
        self.assertIn("window.setInterval(() => {", fn)
        self.assertIn("window.clearInterval(intervalId)", fn)

    def test_button_label_shows_elapsed_time_while_scanning(self):
        self.assertIn("`Previewing... ${scanElapsedSeconds}s`", INTAKE_SOURCE)

    def test_explanatory_message_shown_while_scanning(self):
        self.assertIn("This is still working.", INTAKE_SOURCE)
        self.assertIn("a large", INTAKE_SOURCE)


if __name__ == "__main__":
    unittest.main()
