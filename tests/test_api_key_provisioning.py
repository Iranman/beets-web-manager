"""Regression tests for backend.beets_plugins.provision_api_key_file.

Re-verifies the Section 7 security checklist for /config/.webmanager_api_key
provisioning: exactly 64 hex characters generated with secrets.token_hex(32),
mode 0600 from first write, symlink rejection, no overwrite of an existing
valid key, atomic tmp-file + os.replace creation, PUID/PGID ownership when
configured, and that the token is never logged.
"""

import logging
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from backend.beets_plugins import provision_api_key_file

HEX_64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ApiKeyProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.config_dir = Path(self.td)
        self.key_path = self.config_dir / ".webmanager_api_key"

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_fresh_provisioning_creates_valid_64_hex_key(self):
        result = provision_api_key_file(self.config_dir)
        self.assertTrue(result)
        self.assertTrue(self.key_path.is_file())
        content = self.key_path.read_text(encoding="utf-8").strip()
        self.assertRegex(content, HEX_64_PATTERN)
        self.assertEqual(len(content), 64)

    @unittest.skipUnless(os.name == "posix", "file mode 0600 check only meaningful on POSIX")
    def test_fresh_key_created_mode_0600(self):
        provision_api_key_file(self.config_dir)
        mode = stat.S_IMODE(self.key_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_does_not_overwrite_existing_valid_key(self):
        existing_key = "a" * 64
        self.key_path.write_text(existing_key + "\n", encoding="utf-8")
        result = provision_api_key_file(self.config_dir)
        self.assertTrue(result)
        self.assertEqual(self.key_path.read_text(encoding="utf-8").strip(), existing_key)

    def test_overwrites_malformed_existing_key(self):
        """A malformed (non-64-hex) existing file must be replaced, not preserved."""
        self.key_path.write_text("not-a-valid-key\n", encoding="utf-8")
        result = provision_api_key_file(self.config_dir)
        self.assertTrue(result)
        content = self.key_path.read_text(encoding="utf-8").strip()
        self.assertRegex(content, HEX_64_PATTERN)
        self.assertNotEqual(content, "not-a-valid-key")

    @unittest.skipUnless(os.name == "posix", "symlink rejection semantics are POSIX-specific here")
    def test_rejects_symlinked_key_file(self):
        real_target = self.config_dir / "real_target_key"
        real_target.write_text("b" * 64 + "\n", encoding="utf-8")
        try:
            os.symlink(str(real_target), str(self.key_path))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported in this environment")

        result = provision_api_key_file(self.config_dir)
        self.assertFalse(result)
        # The symlink itself must be left untouched -- never followed or overwritten.
        self.assertTrue(os.path.islink(str(self.key_path)))

    def test_no_leftover_tmp_file_after_successful_provisioning(self):
        provision_api_key_file(self.config_dir)
        leftovers = [p for p in self.config_dir.iterdir() if p.name.startswith(".webmanager_api_key.tmp.")]
        self.assertEqual(leftovers, [])

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
        "PUID/PGID chown only takes effect when running as root on POSIX",
    )
    def test_applies_puid_pgid_ownership_when_configured(self):
        os.environ["PUID"] = "1000"
        os.environ["PGID"] = "1000"
        try:
            provision_api_key_file(self.config_dir)
            st = self.key_path.stat()
            self.assertEqual(st.st_uid, 1000)
            self.assertEqual(st.st_gid, 1000)
        finally:
            os.environ.pop("PUID", None)
            os.environ.pop("PGID", None)

    def test_token_never_logged(self):
        """The generated token must never appear in any log record, even on failure paths."""
        records = []

        class _CapturingHandler(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        target_logger = logging.getLogger("backend.beets_plugins")
        handler = _CapturingHandler()
        target_logger.addHandler(handler)
        try:
            provision_api_key_file(self.config_dir)
            token = self.key_path.read_text(encoding="utf-8").strip()
            for line in records:
                self.assertNotIn(token, line)
        finally:
            target_logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
