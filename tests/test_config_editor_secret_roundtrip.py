"""RC 0.2.0 B1: the Config page must be able to save the config.yaml it shows.

GET /api/config used to redact every secret-key line, empty ones included, and
POST /api/config refused any "[REDACTED]" placeholder, so a fresh install (four
empty secret keys in config.yaml.example) and any install with a real secret
could never save. Now empty secrets are shown as-is and an unchanged
placeholder keeps the stored value, matched by full section path.
"""
import json
import os
import stat
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from unittest import mock

import yaml

from backend import config_manager
from backend.config_service import (
    ConfigSecretMergeError,
    _redact_config_content,
    _restore_redacted_config_secrets,
)

ROOT = Path(__file__).resolve().parents[1]
R = "[REDACTED]"

STORED = (
    "plex:\n"
    "    host: http://plex.example:32400\n"
    "    token: plex-SECRET-1\n"
    "listenbrainz:\n"
    "    token: lb-SECRET-2   # personal\n"
    "discogs:\n"
    "    user_token: \"\"\n"
    "aisauce:\n"
    "    providers:\n"
    "        - id: openai\n"
    "          api_key: ai-SECRET-3\n"
    "        - id: other\n"
    "          api_key: ai-SECRET-4\n"
    "        - api_key: ai-SECRET-5\n"
)
SECRETS = ("plex-SECRET-1", "lb-SECRET-2", "ai-SECRET-3", "ai-SECRET-4", "ai-SECRET-5")


class ConfigEditorRoundTripTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.yaml"

    def _save(self, stored, edit):
        """GET -> edit -> POST through the real save path; returns the file."""
        self.path.write_text(stored, encoding="utf-8")
        current = config_manager.get_config(self.path)
        shown = _redact_config_content(current["content"])
        submitted = edit(shown)
        config_manager.save_config(
            submitted,
            expected_revision=current["revision"],
            config_path=self.path,
            merge_stored=partial(_restore_redacted_config_secrets, submitted),
        )
        return self.path.read_text(encoding="utf-8")

    def test_fresh_install_round_trips(self):
        text = (ROOT / "config.yaml.example").read_text(encoding="utf-8")
        shown = _redact_config_content(text)
        self.assertNotIn(R, shown)  # nothing secret to hide
        self.assertEqual(shown, text)
        saved = self._save(text, lambda s: s + "\n# operator edit\n")
        self.assertEqual(saved, text + "\n# operator edit\n")

    def test_get_never_leaks_a_value(self):
        shown = _redact_config_content(STORED)
        for secret in SECRETS:
            self.assertNotIn(secret, shown)
        self.assertIn('user_token: ""', shown)  # empty stays empty
        self.assertNotIn("personal", shown)

    def test_get_redacts_list_item_quoted_and_multiline_secrets(self):
        text = (
            "a:\n  - token: LIST-SECRET\n"
            "b:\n  \"password\": QUOTED-SECRET\n"
            "c:\n  apikey: |\n    BLOCK-SECRET\n\n    BLOCK-SECRET-2\n  other: 1\n"
        )
        shown = _redact_config_content(text)
        for secret in ("LIST-SECRET", "QUOTED-SECRET", "BLOCK-SECRET"):
            self.assertNotIn(secret, shown)
        self.assertIn("other: 1", shown)
        saved = self._save(text, lambda s: s)
        self.assertEqual(yaml.safe_load(saved), yaml.safe_load(text))

    def test_unchanged_placeholder_keeps_stored_secret(self):
        saved = self._save(STORED, lambda s: s.replace("http://plex.example", "http://plex2.example"))
        self.assertEqual(saved, STORED.replace("http://plex.example", "http://plex2.example"))
        self.assertNotIn(R, saved)

    def test_same_key_in_two_sections_never_swaps(self):
        # Reorder sections in the editor: each placeholder still gets its own.
        def swap(s):
            plex, rest = s.split("listenbrainz:\n", 1)
            lb, tail = rest.split("discogs:\n", 1)
            return "listenbrainz:\n" + lb + plex + "discogs:\n" + tail
        data = yaml.safe_load(self._save(STORED, swap))
        self.assertEqual(data["plex"]["token"], "plex-SECRET-1")
        self.assertEqual(data["listenbrainz"]["token"], "lb-SECRET-2")
        self.assertEqual([p["api_key"] for p in data["aisauce"]["providers"]],
                         ["ai-SECRET-3", "ai-SECRET-4", "ai-SECRET-5"])

    def test_edit_a_secret(self):
        saved = self._save(STORED, lambda s: s.replace(f'    token: "{R}"\nlistenbrainz', "    token: NEW-plex\nlistenbrainz", 1))
        data = yaml.safe_load(saved)
        self.assertEqual(data["plex"]["token"], "NEW-plex")
        self.assertEqual(data["listenbrainz"]["token"], "lb-SECRET-2")

    def test_clear_a_secret(self):
        saved = self._save(STORED, lambda s: s.replace(f'    token: "{R}"\nlistenbrainz', '    token: ""\nlistenbrainz', 1))
        data = yaml.safe_load(saved)
        self.assertEqual(data["plex"]["token"], "")
        self.assertEqual(data["listenbrainz"]["token"], "lb-SECRET-2")

    def test_partial_placeholder_is_refused(self):
        with self.assertRaises(ConfigSecretMergeError) as ctx:
            self._save(STORED, lambda s: s.replace(f'"{R}"', f'"{R}x"', 1))
        self.assertEqual(ctx.exception.error_code, "config_redacted_placeholder")
        self.assertEqual(self.path.read_text(encoding="utf-8"), STORED)

    def test_duplicate_stored_key_is_refused(self):
        stored = "plex:\n  token: A-SECRET\n  token: B-SECRET\n"
        with self.assertRaises(ConfigSecretMergeError) as ctx:
            self._save(stored, lambda s: s)
        self.assertEqual(ctx.exception.error_code, "config_secret_ambiguous")
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), stored)

    def test_duplicated_placeholder_line_is_refused(self):
        with self.assertRaises(ConfigSecretMergeError):
            self._save(STORED, lambda s: s.replace("plex:\n", f'plex:\n    token: "{R}"\n', 1))

    def test_placeholder_under_a_key_path_with_no_stored_secret_is_refused(self):
        # Covers a key-path change only. A placeholder pasted inside another
        # key's block scalar that the line scanner maps to the same path as
        # a stored secret is restored, not refused (accepted in review of #320).
        with self.assertRaises(ConfigSecretMergeError) as ctx:
            self._save(STORED, lambda s: s + f'lastfm:\n    token: "{R}"\n')
        self.assertIn("lastfm.token", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_pathological_dash_runs_stay_linear(self):
        # Security review of #320 (F1): one "- " per list level used to copy
        # the rest of the line, O(k*L) per GET on a saved block scalar.
        # Asserts the growth rate, not a wall-clock budget (shared CI runners
        # vary): 8x the input must cost well under 64x (quadratic) the time.
        # Locally: 512 KB redact ~0.2 s, restore ~0.4 s; before the fix ~5 s.
        def best_of_3(fn, text):
            times = []
            for _ in range(3):
                start = time.perf_counter()
                fn(text)
                times.append(time.perf_counter() - start)
            return min(times)

        def redact(text):
            self.assertEqual(_redact_config_content(text), text)

        def restore(text):
            _restore_redacted_config_secrets(f'token: "{R}"\n' + text, "token: abc\n" + text)

        small = "note: |\n  " + "- " * 32768 + "x\n"  # 64 KB
        big = "note: |\n  " + "- " * 262144 + "x\n"  # 512 KB, valid YAML
        for fn in (redact, restore):
            t_small, t_big = best_of_3(fn, small), best_of_3(fn, big)
            self.assertLess(t_big, 5.0, fn.__name__)
            self.assertLess(t_big, 24 * t_small + 0.05, f"{fn.__name__}: {t_small:.3f}s -> {t_big:.3f}s")

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_save_keeps_private_mode_of_config_with_secrets(self):
        self.path.write_text(STORED, encoding="utf-8")
        os.chmod(self.path, 0o600)
        self._save(STORED, lambda s: s + "# edit\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([p.name for p in self.path.parent.glob(".config.yaml.*")], [])
        rev = config_manager.get_config(self.path)["revision"]
        config_manager.revert_config(expected_revision=rev, config_path=self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), STORED)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_save_route_merges_and_refuses_with_400(self):
        import app as app_module

        self.path.write_text(STORED, encoding="utf-8")
        real_save = config_manager.save_config

        def post(content):
            rev = config_manager.get_config(self.path)["revision"]
            with app_module.app.test_request_context(
                "/api/config", method="POST",
                data=json.dumps({"content": content, "expected_revision": rev}),
                content_type="application/json",
            ), mock.patch.object(app_module.composite_workflows, "save_config",
                                 side_effect=partial(real_save, config_path=self.path)):
                response = app_module.save_config()
            if isinstance(response, tuple):
                return response[1], response[0].get_json()
            return response.status_code, response.get_json()

        shown = _redact_config_content(STORED)
        status, body = post(shown)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.path.read_text(encoding="utf-8"), STORED)

        status, body = post(shown + f'lastfm:\n    token: "{R}"\n')
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "config_secret_ambiguous")
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(body))
        self.assertEqual(self.path.read_text(encoding="utf-8"), STORED)


if __name__ == "__main__":
    unittest.main()
