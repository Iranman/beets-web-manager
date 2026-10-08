"""SEC-9 image hardening guards (static; the image itself is scanned with
Trivy outside the unit suite)."""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = ROOT / "docker" / "web-manager-entrypoint.sh"


class DockerfileTests(unittest.TestCase):
    def test_base_images_are_digest_pinned(self):
        froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.M)
        self.assertTrue(froms)
        for image in froms:
            self.assertRegex(image, r"@sha256:[0-9a-f]{64}$", image)

    def test_git_is_not_installed(self):
        install = re.search(r"apt-get install[^\n]*(?:\\\n[^\n]*)*", DOCKERFILE).group(0)
        self.assertNotRegex(install, r"(?m)^\s*git\s*\\?$")

    def test_security_updates_are_applied(self):
        self.assertIn("apt-get upgrade -y", DOCKERFILE)

    def test_tests_are_not_shipped(self):
        self.assertNotRegex(DOCKERFILE, r"(?m)^COPY\s+tests/")

    def test_pip_is_removed_after_installing_requirements(self):
        # pip vendors its own urllib3/msgpack/setuptools copies; nothing
        # installs packages at runtime, so the final image must not ship it.
        self.assertRegex(DOCKERFILE, r"pip install --no-cache-dir -r requirements\.txt[\s\\]*&& pip uninstall -y pip")

    def test_tini_runs_after_privilege_drop(self):
        # D6: a root tini without CAP_KILL (hardened Compose) cannot forward
        # SIGTERM to the uid-PUID app. tini must be exec'd by gosu instead.
        entrypoint = re.search(r'^ENTRYPOINT\s+(.+)$', DOCKERFILE, re.M).group(1)
        self.assertNotIn("tini", entrypoint)
        self.assertIn('exec gosu "$RUN_AS" tini -- "$@"', ENTRYPOINT.read_text(encoding="utf-8"))

    def test_version_label_default_is_not_a_release(self):
        # D11: a stale release-number default mislabels local builds.
        self.assertRegex(DOCKERFILE, r"(?m)^ARG VERSION=dev$")


@unittest.skipUnless(shutil.which("sh"), "POSIX sh not available")
class EntrypointRootRefusalTests(unittest.TestCase):
    def run_entrypoint(self, puid, pgid):
        env = dict(os.environ, PUID=puid, PGID=pgid)
        return subprocess.run(["sh", str(ENTRYPOINT), "true"], env=env, capture_output=True, text=True, timeout=30)

    def test_root_ids_are_refused_before_any_change(self):
        for puid, pgid in (("0", "1000"), ("1000", "0"), ("00", "00")):
            with self.subTest(puid=puid, pgid=pgid):
                proc = self.run_entrypoint(puid, pgid)
                self.assertEqual(proc.returncode, 64, proc.stderr)
                self.assertIn("not allowed", proc.stderr)

    def test_non_numeric_ids_are_refused(self):
        for puid, pgid in (("root", "1000"), ("1000", "1000; id"), ("-1", "1000")):
            with self.subTest(puid=puid, pgid=pgid):
                self.assertEqual(self.run_entrypoint(puid, pgid).returncode, 64)


if __name__ == "__main__":
    unittest.main()
