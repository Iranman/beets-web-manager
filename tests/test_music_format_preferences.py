import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.audio_preferences import (
    DEFAULT_MUSIC_FORMAT_PREFERENCES,
    can_remove_original_after_replacement,
    handle_rejected_download,
    inspect_audio_file,
    load_music_format_preferences,
    normalize_music_format_preferences,
    save_music_format_preferences,
    sort_candidates_by_preferences,
    validate_audio_properties,
)


def props(**overrides):
    base = {
        "ok": True,
        "codec": "flac",
        "format": "flac",
        "channels": 2,
        "channel_layout": "stereo",
        "layout": "stereo",
        "is_atmos": False,
    }
    base.update(overrides)
    return base


class MusicFormatPreferencesTests(unittest.TestCase):
    def test_defaults_accept_stereo_21_and_atmos(self):
        prefs = normalize_music_format_preferences({})
        self.assertTrue(validate_audio_properties(props(layout="stereo", channels=2), prefs)["ok"])
        self.assertTrue(validate_audio_properties(props(layout="2.1", channels=3), prefs)["ok"])
        atmos = props(codec="eac3", format="eac3", channels=8, layout="atmos", is_atmos=True)
        self.assertTrue(validate_audio_properties(atmos, prefs)["ok"])

    def test_51_and_71_rejected_when_disabled(self):
        prefs = normalize_music_format_preferences({})
        self.assertFalse(validate_audio_properties(props(layout="5.1", channels=6), prefs)["ok"])
        self.assertFalse(validate_audio_properties(props(layout="7.1", channels=8), prefs)["ok"])

    def test_atmos_rejected_when_disabled_even_if_acoustid_confirmed(self):
        prefs = normalize_music_format_preferences({"allow_atmos": False})
        result = validate_audio_properties(
            props(codec="eac3", format="eac3", channels=8, layout="atmos", is_atmos=True, acoustid_confirmed=True),
            prefs,
        )
        self.assertFalse(result["ok"])
        self.assertIn("Atmos audio is disabled", result["reasons"][0])

    def test_unknown_channel_layout_rejected(self):
        result = validate_audio_properties(props(layout="unknown", channels=None), normalize_music_format_preferences({}))
        self.assertFalse(result["ok"])
        self.assertIn("unknown", " ".join(result["reasons"]).lower())

    def test_custom_max_channel_count_allows_other_layouts_only_when_enabled(self):
        blocked = validate_audio_properties(props(layout="custom", channels=4), normalize_music_format_preferences({}))
        allowed = validate_audio_properties(props(layout="custom", channels=4), normalize_music_format_preferences({"custom_max_channels": 4}))
        self.assertFalse(blocked["ok"])
        self.assertTrue(allowed["ok"])

    def test_preferred_format_order_is_respected(self):
        prefs = normalize_music_format_preferences({"preferred_formats": ["mp3", "flac", "aac"]})
        ordered = sort_candidates_by_preferences([
            {"format": "aac", "channels": 2},
            {"format": "flac", "channels": 2},
            {"format": "mp3", "channels": 2},
        ], prefs)
        self.assertEqual([row["format"] for row in ordered], ["mp3", "flac", "aac"])

    def test_settings_save_load_with_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "prefs.json")
            saved = save_music_format_preferences({"allowed_layouts": {"mono": True}, "preferred_formats": ["mp3"]}, path)
            loaded = load_music_format_preferences(path)
        self.assertTrue(saved["allowed_layouts"]["mono"])
        self.assertTrue(loaded["allowed_layouts"]["stereo"])
        self.assertEqual(loaded["preferred_formats"], ["mp3"])


    def test_inspect_audio_file_uses_fast_probe_after_full_probe_timeout(self):
        full_timeout = subprocess.TimeoutExpired(["ffprobe"], 20)
        fast_probe = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({
                "streams": [{
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "sample_rate": "44100",
                    "bit_rate": "192000",
                }],
                "format": {"bit_rate": "192000"},
            }),
            "stderr": "",
        })()
        with mock.patch("backend.audio_preferences.subprocess.run", side_effect=[full_timeout, fast_probe]) as run:
            result = inspect_audio_file("/music/slow.mp3", ffprobe_bin="ffprobe")
        self.assertTrue(result["ok"])
        self.assertEqual(result["format"], "mp3")
        self.assertEqual(result["layout"], "stereo")
        self.assertEqual(run.call_count, 2)
        self.assertIn("-read_intervals", run.call_args_list[1].args[0])
    def test_rejected_download_delete_or_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            doomed = Path(tmp) / "bad.flac"
            doomed.write_text("x", encoding="utf-8")
            handle_rejected_download(str(doomed), {**DEFAULT_MUSIC_FORMAT_PREFERENCES, "rejected_download_handling": "delete"})
            self.assertFalse(doomed.exists())

    def test_existing_file_not_removed_until_verified_replacement_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "replacement.flac"
            ok_validation = {"ok": True}
            self.assertFalse(can_remove_original_after_replacement(ok_validation, str(final)))
            final.write_text("audio", encoding="utf-8")
            self.assertTrue(can_remove_original_after_replacement(ok_validation, str(final)))
            self.assertFalse(can_remove_original_after_replacement({"ok": False}, str(final)))

    def test_replacement_fallback_keeps_current_and_marks_retry(self):
        prefs = normalize_music_format_preferences({"replacement_fallback": {"keep_current": False, "queue_retry": True}})
        self.assertTrue(prefs["replacement_fallback"]["keep_current"])
        self.assertTrue(prefs["replacement_fallback"]["mark_needs_replacement"])
        self.assertTrue(prefs["replacement_fallback"]["queue_retry"])


if __name__ == "__main__":
    unittest.main()