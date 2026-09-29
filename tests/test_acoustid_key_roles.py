"""AcoustID has two credential types: the application key (lookups,
ACOUSTID_API_KEY) and the per-account user key (submissions,
ACOUSTID_USER_KEY). Found live: one variable served both, so a user key
made every production lookup fail with 'invalid API key'.

Deliberately does NOT import routes_submissions/app: importing app at this
point in alphabetical test order would initialize it before later suites
configure their own data directories. The function is evaluated from source.
"""

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_acoustid_key():
    src = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_acoustid_key")
    namespace = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "routes_submissions.py", "exec"), namespace)
    return namespace["_acoustid_key"]


class AcoustidKeyRoleTests(unittest.TestCase):
    def setUp(self):
        self.acoustid_key = _load_acoustid_key()

    def _key(self, **env):
        base = {"ACOUSTID_USER_KEY": "", "ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""}
        base.update(env)
        with mock.patch.dict(os.environ, base):
            return self.acoustid_key()

    def test_submission_prefers_explicit_user_key(self):
        self.assertEqual(self._key(ACOUSTID_USER_KEY="user-k", ACOUSTID_API_KEY="app-k"), "user-k")

    def test_submission_falls_back_to_legacy_single_variable(self):
        self.assertEqual(self._key(ACOUSTID_API_KEY="legacy-k"), "legacy-k")
        self.assertEqual(self._key(ACOUSTID_KEY="alias-k"), "alias-k")

    def test_lookup_never_reads_the_user_key(self):
        src = (ROOT / "helpers_mb.py").read_text(encoding="utf-8")
        # The key is read by the typed lookup the compatibility wrapper uses.
        start = src.index("def acoustid_lookup_outcome(")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn('os.environ.get("ACOUSTID_API_KEY")', body)
        self.assertNotIn("ACOUSTID_USER_KEY", body)


if __name__ == "__main__":
    unittest.main()
