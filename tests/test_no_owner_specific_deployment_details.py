"""Regression protection: public deployment/example configuration must never
ship the project owner's actual TrueNAS/Arrs host paths, LAN IP defaults, or
local dev-machine paths again -- see the "Deployment architecture rule" in
AGENTS.md/CLAUDE.md.

Scoped narrowly to deployment/config/example files (not the whole repo):
security tests elsewhere legitimately use RFC1918 addresses as fixtures, and
scripts/deploy_truenas_web_manager.sh legitimately checks for a
conventionally-named docker-compose.arrs.yml on a *real deployment host* at
runtime (not something this repo ships) -- neither is in scope here.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Deployment/config/example files a new user would actually run or copy.
# Docs are checked separately (allowed to mention the pattern only inside a
# fenced "don't do this" example, never as a live default).
DEPLOYMENT_FILES = [
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.full.yml",
    ROOT / "docker-compose.dev.yml",
    ROOT / ".env.example",
]

OWNER_PATH_PATTERNS = [
    re.compile(r"/mnt/PLEX(?:/|\b)", re.IGNORECASE),
    re.compile(r"\\\\TRUENAS\\", re.IGNORECASE),
    re.compile(r"C:\\Users\\irand\b", re.IGNORECASE),
]


def _existing(paths):
    return [p for p in paths if p.exists()]


class NoOwnerSpecificDeploymentDetailsTests(unittest.TestCase):
    def test_deployment_files_contain_no_owner_specific_paths(self):
        for path in _existing(DEPLOYMENT_FILES):
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if line.strip().startswith("#"):
                    continue  # commented-out illustrative text, not an active default
                for pattern in OWNER_PATH_PATTERNS:
                    self.assertIsNone(
                        pattern.search(line),
                        f"{path.name}:{line_no} contains an owner-specific path as an "
                        f"active default: {line.strip()!r}",
                    )

    def test_examples_directory_contains_no_owner_specific_paths(self):
        examples_dir = ROOT / "examples"
        if not examples_dir.is_dir():
            self.skipTest("no examples/ directory")
        for path in sorted(examples_dir.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if line.strip().startswith("#"):
                    continue
                for pattern in OWNER_PATH_PATTERNS:
                    self.assertIsNone(
                        pattern.search(line),
                        f"examples/{path.name}:{line_no} contains an owner-specific "
                        f"path as an active default: {line.strip()!r}",
                    )

    def test_the_owners_private_arrs_stack_file_is_not_shipped(self):
        """docker-compose.arrs.yml (the project owner's real 15+ service Arr
        stack -- Prowlarr, Radarr, Sonarr, Lidarr, Bazarr, Seerr, Autobrr,
        slskd, Soularr, Profilarr, Cleanuparr, Organizr, Digarr, Postgres,
        TheLounge -- alongside Beets) must never exist in the public tree
        again. This project owns beets + beets-web-manager only."""
        self.assertFalse((ROOT / "docker-compose.arrs.yml").exists())

    def test_deployment_files_do_not_default_timezone_to_owner_zone(self):
        for path in _existing(DEPLOYMENT_FILES):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "America/Los_Angeles", text,
                f"{path.name} must not default TZ to the project owner's timezone "
                "(use TZ:-UTC or leave unset)",
            )

    def test_frontend_dev_proxy_does_not_hardcode_owner_lan_ip(self):
        next_config = ROOT / "frontend" / "next.config.mjs"
        if not next_config.exists():
            self.skipTest("frontend/next.config.mjs not found")
        text = next_config.read_text(encoding="utf-8")
        self.assertNotIn("192.168.0.250", text)

    def test_app_py_does_not_default_plex_url_to_owner_lan_ip(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("192.168.0.250", app_source)


if __name__ == "__main__":
    unittest.main()
