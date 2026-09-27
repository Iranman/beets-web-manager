"""AcoustID has two credential types: the application key (lookups,
ACOUSTID_API_KEY) and the per-account user key (submissions,
ACOUSTID_USER_KEY). Found live: one variable served both, so a user key
made every production lookup fail with 'invalid API key'."""

import os
import unittest
from unittest import mock

import routes_submissions


class AcoustidKeyRoleTests(unittest.TestCase):
    def _key(self, **env):
        base = {"ACOUSTID_USER_KEY": "", "ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""}
        base.update(env)
        with mock.patch.dict(os.environ, base):
            return routes_submissions._acoustid_key()

    def test_submission_prefers_explicit_user_key(self):
        self.assertEqual(self._key(ACOUSTID_USER_KEY="user-k", ACOUSTID_API_KEY="app-k"), "user-k")

    def test_submission_falls_back_to_legacy_single_variable(self):
        self.assertEqual(self._key(ACOUSTID_API_KEY="legacy-k"), "legacy-k")
        self.assertEqual(self._key(ACOUSTID_KEY="alias-k"), "alias-k")

    def test_lookup_never_reads_the_user_key(self):
        import helpers_mb
        src = open(helpers_mb.__file__, encoding="utf-8").read()
        start = src.index("def _acoustid_lookup(")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn('os.environ.get("ACOUSTID_API_KEY")', body)
        self.assertNotIn("ACOUSTID_USER_KEY", body)


if __name__ == "__main__":
    unittest.main()
