import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THEME_SOURCE = (ROOT / "frontend" / "src" / "theme.ts").read_text(encoding="utf-8")
IMPORT_REVIEW_SOURCE = (
    ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
).read_text(encoding="utf-8")




def _hex_token(name: str) -> str:
    import re

    match = re.search(rf"{name}: '([^']+)'", THEME_SOURCE)
    if not match:
        raise AssertionError(f"Missing button color token {name}")
    return match.group(1)


def _relative_luminance(hex_color: str) -> float:
    raw = hex_color.lstrip("#")
    rgb = [int(raw[idx:idx + 2], 16) / 255 for idx in (0, 2, 4)]

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = [channel(value) for value in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(foreground: str, background: str) -> float:
    light = max(_relative_luminance(foreground), _relative_luminance(background))
    dark = min(_relative_luminance(foreground), _relative_luminance(background))
    return (light + 0.05) / (dark + 0.05)
class ImportReviewButtonContrastTests(unittest.TestCase):
    def test_warning_panel_actions_exist(self):
        self.assertIn("Import blocked", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("Verify with fingerprint", IMPORT_REVIEW_SOURCE)
        self.assertIn("automatic verification", IMPORT_REVIEW_SOURCE)
        self.assertIn("Quarantine", IMPORT_REVIEW_SOURCE)
        self.assertIn("Find Match", IMPORT_REVIEW_SOURCE)
        self.assertIn("Enter MusicBrainz ID", IMPORT_REVIEW_SOURCE)
        self.assertIn("Validate ID", IMPORT_REVIEW_SOURCE)
        self.assertIn("Import with ID", IMPORT_REVIEW_SOURCE)

    def test_buttons_use_explicit_readable_tokens(self):
        for token in (
            "secondaryBg",
            "secondaryText",
            "secondaryBorder",
            "disabledBg",
            "disabledText",
            "disabledBorder",
            "warningBg",
            "warningText",
            "dangerBg",
            "dangerText",
        ):
            self.assertIn(token, THEME_SOURCE)

    def test_button_token_contrast_pairs_are_readable(self):
        for text_token, bg_token in (
            ("secondaryText", "secondaryBg"),
            ("disabledText", "disabledBg"),
            ("warningText", "warningBg"),
            ("dangerText", "dangerBg"),
        ):
            ratio = _contrast_ratio(_hex_token(text_token), _hex_token(bg_token))
            self.assertGreaterEqual(ratio, 4.5, f"{text_token} on {bg_token} contrast is {ratio:.2f}")

    def test_warning_and_disabled_button_states_are_not_opacity_only(self):
        self.assertIn("&.MuiButton-outlinedWarning", THEME_SOURCE)
        self.assertIn("&.MuiButton-containedWarning", THEME_SOURCE)
        self.assertIn("&.MuiButton-textWarning", THEME_SOURCE)
        self.assertIn("&.Mui-disabled", THEME_SOURCE)
        self.assertIn("opacity: 1", THEME_SOURCE)
        self.assertIn("backgroundColor: button.disabledBg", THEME_SOURCE)
        self.assertIn("borderColor: button.disabledBorder", THEME_SOURCE)
        self.assertIn("color: button.disabledText", THEME_SOURCE)

    def test_dark_panel_danger_and_focus_states_remain_visible(self):
        self.assertIn("&.MuiButton-outlinedError", THEME_SOURCE)
        self.assertIn("&.MuiButton-containedError", THEME_SOURCE)
        self.assertIn("&.Mui-focusVisible", THEME_SOURCE)
        self.assertIn("boxShadow: `0 0 0 3px", THEME_SOURCE)

    def test_import_review_dark_queue_controls_stay_readable(self):
        self.assertIn("text-zinc-200", IMPORT_REVIEW_SOURCE)
        self.assertIn("text-zinc-100 transition hover:bg-graphite-700", IMPORT_REVIEW_SOURCE)
        self.assertIn("focus-visible:outline-sky-300", IMPORT_REVIEW_SOURCE)
        self.assertIn("disabled:text-zinc-500", IMPORT_REVIEW_SOURCE)
    def test_blocked_panel_actions_use_local_contrast_styles(self):
        self.assertIn("blockedPanelActionButtonSx", IMPORT_REVIEW_SOURCE)
        self.assertIn("blockedPanelWarningButtonSx", IMPORT_REVIEW_SOURCE)
        self.assertIn("sx={blockedPanelActionButtonSx}", IMPORT_REVIEW_SOURCE)
        self.assertIn("sx={blockedPanelWarningButtonSx}", IMPORT_REVIEW_SOURCE)
        self.assertIn("backgroundColor: '#1f2530'", IMPORT_REVIEW_SOURCE)
        self.assertIn("backgroundColor: '#78350f'", IMPORT_REVIEW_SOURCE)
        self.assertIn("opacity: 1", IMPORT_REVIEW_SOURCE)


if __name__ == "__main__":
    unittest.main()
