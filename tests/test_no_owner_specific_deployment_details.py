"""Regression protection: public deployment/example configuration must never
ship a maintainer's real host paths, LAN defaults, or local dev-machine paths
-- see the "Deployment Configuration" rule in AGENTS.md/CLAUDE.md.

Detection is deliberately generic (structural path/address *classes*, not a
denylist of any specific person's former literal values): a denylist of exact
former values would itself have to contain those values, defeating the point
of a public regression test. A synthetic-fixture self-test below proves the
detector actually catches invented examples of each class.

Scoped narrowly to deployment/config/example files (not the whole repo):
security tests elsewhere legitimately use RFC1918 addresses as fixtures, and
scripts/deploy_truenas_web_manager.sh legitimately operates against a real
deployment host's own paths at runtime (not something this repo ships) --
neither is in scope here.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Deployment/config/example files a new user would actually run or copy.
DEPLOYMENT_FILES = [
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.full.yml",
    ROOT / "docker-compose.dev.yml",
    ROOT / ".env.example",
]

# Structural classes of "this looks like somebody's real machine", not any
# one person's specific former value. Each pattern must also fire on a
# fabricated example (see the self-test), proving it is not accidentally
# scoped to a single literal.
_GENERIC_HOST_PATH_PATTERNS = {
    "absolute /mnt/<pool>/... host mount": re.compile(r"/mnt/[A-Za-z0-9_.-]+/"),
    "absolute /home/<user>/... path": re.compile(r"/home/[A-Za-z0-9_.-]+/"),
    "Windows C:\\Users\\<user>\\... path": re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9_.-]+", re.IGNORECASE),
    "UNC \\\\<host>\\... path": re.compile(r"\\\\[A-Za-z0-9_.-]+\\"),
    "hardcoded RFC1918 host address": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b"
    ),
}

# A fixed, non-UTC personal timezone default is a deployment-specific detail;
# the generic policy is simply "must default to UTC (or be left unset)",
# checked directly below rather than via a name-based pattern.
_NON_UTC_TZ_DEFAULT_RE = re.compile(r"TZ[:=]\s*\$\{TZ:-([A-Za-z]+/[A-Za-z_]+)\}")


def _existing(paths):
    return [p for p in paths if p.exists()]


def _scan_lines_for_patterns(text, patterns):
    """Yield (line_no, line, label) for every non-comment line matching any
    pattern in `patterns` (a {label: compiled_pattern} mapping)."""
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue  # commented-out illustrative text, not an active default
        for label, pattern in patterns.items():
            if pattern.search(line):
                yield line_no, line, label


class NoOwnerSpecificDeploymentDetailsTests(unittest.TestCase):
    def test_deployment_files_contain_no_host_specific_paths_or_lan_defaults(self):
        for path in _existing(DEPLOYMENT_FILES):
            text = path.read_text(encoding="utf-8")
            for line_no, line, label in _scan_lines_for_patterns(text, _GENERIC_HOST_PATH_PATTERNS):
                self.fail(
                    f"{path.name}:{line_no} looks like a {label} used as an "
                    f"active default (deployment/example files must be "
                    f"host-agnostic): {line.strip()!r}"
                )

    def test_examples_directory_contains_no_host_specific_paths(self):
        examples_dir = ROOT / "examples"
        if not examples_dir.is_dir():
            self.skipTest("no examples/ directory")
        for path in sorted(examples_dir.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for line_no, line, label in _scan_lines_for_patterns(text, _GENERIC_HOST_PATH_PATTERNS):
                self.fail(
                    f"examples/{path.name}:{line_no} looks like a {label} used "
                    f"as an active default: {line.strip()!r}"
                )

    def test_the_private_aggregate_stack_file_is_not_shipped(self):
        """A private, multi-service deployment stack composed alongside Beets
        must never exist in the public tree: this project ships beets +
        beets-web-manager only. (The former file removed by this change
        combined Beets with over a dozen unrelated services specific to one
        maintainer's personal deployment; the point of this test is that no
        such aggregate file returns, not the identity of that old file.)"""
        self.assertFalse((ROOT / "docker-compose.arrs.yml").exists())

    def test_deployment_files_default_timezone_to_utc(self):
        for path in _existing(DEPLOYMENT_FILES):
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if line.strip().startswith("#"):
                    continue
                match = _NON_UTC_TZ_DEFAULT_RE.search(line)
                if match:
                    self.fail(
                        f"{path.name}:{line_no} defaults TZ to a fixed non-UTC "
                        f"zone ({match.group(1)!r}) -- deployment/example "
                        f"files must default to UTC (or leave TZ unset), not "
                        f"any one maintainer's personal timezone: "
                        f"{line.strip()!r}"
                    )

    def test_frontend_dev_proxy_does_not_hardcode_a_lan_address(self):
        next_config = ROOT / "frontend" / "next.config.mjs"
        if not next_config.exists():
            self.skipTest("frontend/next.config.mjs not found")
        text = next_config.read_text(encoding="utf-8")
        for line_no, line, label in _scan_lines_for_patterns(
            text, {"hardcoded RFC1918 host address": _GENERIC_HOST_PATH_PATTERNS["hardcoded RFC1918 host address"]}
        ):
            self.fail(f"next.config.mjs:{line_no} hardcodes a {label} as the dev proxy default: {line.strip()!r}")

    def test_app_py_does_not_default_plex_url_to_a_lan_address(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        for line_no, line, label in _scan_lines_for_patterns(
            app_source, {"hardcoded RFC1918 host address": _GENERIC_HOST_PATH_PATTERNS["hardcoded RFC1918 host address"]}
        ):
            if "PLEX_URL" in line:
                self.fail(f"app.py:{line_no} defaults PLEX_URL to a {label}: {line.strip()!r}")


class GenericDetectorSelfTest(unittest.TestCase):
    """Proves the detector above catches fabricated examples of each
    violation class, using entirely invented values -- not this project's
    former real ones. If these fail, the real-file tests above are not
    actually testing anything."""

    def _assert_detects(self, sample_text, expected_label):
        hits = list(_scan_lines_for_patterns(sample_text, _GENERIC_HOST_PATH_PATTERNS))
        labels = [label for _, _, label in hits]
        self.assertIn(
            expected_label, labels,
            f"detector did not flag a synthetic {expected_label!r} example: {sample_text!r}",
        )

    def test_detects_synthetic_mnt_pool_mount(self):
        self._assert_detects(
            "      - /mnt/examplepool/apps/media:/data/media",
            "absolute /mnt/<pool>/... host mount",
        )

    def test_detects_synthetic_home_directory(self):
        self._assert_detects(
            "      - /home/exampleuser/music:/data/media",
            "absolute /home/<user>/... path",
        )

    def test_detects_synthetic_windows_user_path(self):
        self._assert_detects(
            r"      - C:\Users\example-user\project:/data/media",
            "Windows C:\\Users\\<user>\\... path",
        )

    def test_detects_synthetic_unc_path(self):
        self._assert_detects(
            r"      - \\example-nas\apps\media:/data/media",
            "UNC \\\\<host>\\... path",
        )

    def test_detects_synthetic_rfc1918_address(self):
        self._assert_detects(
            "BEETS_API_URL=http://192.168.50.25:8338",
            "hardcoded RFC1918 host address",
        )

    def test_does_not_flag_env_var_driven_defaults(self):
        """The whole point: an env-var-driven default with no embedded host
        literal must pass cleanly."""
        clean_examples = [
            "      - ${MUSIC_LIBRARY_PATH:-./data/music}:/data/media/music",
            "BEETS_API_URL=${BEETS_API_URL:?set BEETS_API_URL in .env}",
            "TZ: ${TZ:-UTC}",
        ]
        for sample in clean_examples:
            hits = list(_scan_lines_for_patterns(sample, _GENERIC_HOST_PATH_PATTERNS))
            self.assertEqual(hits, [], f"false positive on a clean env-driven default: {sample!r}")


if __name__ == "__main__":
    unittest.main()
