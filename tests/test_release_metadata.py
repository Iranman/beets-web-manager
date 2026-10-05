"""Tests for scripts/release_metadata.py and its wiring in docker-build.yml."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_metadata.py"
WORKFLOW = (ROOT / ".github" / "workflows" / "docker-build.yml").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "scripts"))
import release_metadata as rm  # noqa: E402

CHANGELOG = """# Changelog

## Unreleased

### Fixed
- Something not released yet.

## v1.2.0 - 2026-10-05

### Upgrade Notes
- Do the thing → then the other thing.

### Fixed
- A fix.

## v1.1.9 - 2026-10-01

### Fixed
- Older fix.
"""


class ReleaseMetadataTests(unittest.TestCase):
    def repo(self, version="1.2.0", changelog=CHANGELOG):
        d = Path(tempfile.mkdtemp(prefix="relmeta-"))
        (d / "VERSION").write_text(version + "\n", encoding="utf-8")
        (d / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        return d

    def run_cli(self, root, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                              capture_output=True, timeout=30)

    def test_matching_version_and_tag_pass(self):
        root = self.repo()
        self.assertEqual(self.run_cli(root, "check").returncode, 0)
        self.assertEqual(self.run_cli(root, "check", "--tag", "v1.2.0").returncode, 0)

    def test_version_ahead_of_changelog_fails(self):
        res = self.run_cli(self.repo(version="1.2.1"), "check")
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"newest CHANGELOG release heading is v1.2.0", res.stderr)

    def test_changelog_ahead_of_version_fails(self):
        res = self.run_cli(self.repo(version="1.1.9"), "check")
        self.assertEqual(res.returncode, 1)

    def test_tag_mismatch_fails(self):
        res = self.run_cli(self.repo(), "check", "--tag", "v1.2.1")
        self.assertEqual(res.returncode, 1)
        self.assertIn(b"expected v1.2.0", res.stderr)

    def test_non_semver_version_fails(self):
        with self.assertRaises(rm.ReleaseMetadataError):
            rm.read_version(self.repo(version="1.2"))

    def test_empty_section_fails(self):
        root = self.repo(changelog="# Changelog\n\n## v1.2.0 - 2026-10-05\n\n## v1.1.9 - 2026-10-01\n- x\n")
        self.assertEqual(self.run_cli(root, "check").returncode, 1)
        self.assertEqual(self.run_cli(root, "notes", "--tag", "v1.2.0").returncode, 1)

    def test_notes_are_exactly_the_tagged_section_in_utf8(self):
        root = self.repo()
        out = root / "notes.md"
        res = self.run_cli(root, "notes", "--tag", "v1.2.0", "--output", str(out))
        self.assertEqual(res.returncode, 0, res.stderr)
        body = out.read_text(encoding="utf-8")
        self.assertTrue(body.startswith("### Upgrade Notes"))
        self.assertIn("→", body)
        self.assertIn("- A fix.", body)
        self.assertNotIn("Older fix", body)
        self.assertNotIn("not released yet", body)
        self.assertNotIn("## v1.2.0", body)

    def test_notes_for_unknown_tag_fail(self):
        self.assertEqual(self.run_cli(self.repo(), "notes", "--tag", "v9.9.9").returncode, 1)

    def test_this_repository_is_consistent(self):
        self.assertEqual(self.run_cli(ROOT, "check").returncode, 0)


class WorkflowWiringTests(unittest.TestCase):
    def job(self, name):
        m = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:\n|\Z)", WORKFLOW)
        self.assertIsNotNone(m, f"job {name} missing from docker-build.yml")
        return m.group(1)

    def test_release_metadata_check_gates_publishing(self):
        job = self.job("release-metadata")
        self.assertIn("scripts/release_metadata.py check", job)
        self.assertIn("--tag", job)
        self.assertRegex(self.job("publish-ghcr"), r"needs: \[[^\]]*release-metadata")

    def test_github_release_runs_only_on_tags_after_publish(self):
        job = self.job("github-release")
        self.assertIn("needs: [publish-ghcr]", job)
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", job)
        self.assertIn("contents: write", job)
        self.assertIn("--verify-tag", job)
        self.assertIn("scripts/release_metadata.py notes", job)
        # The tag name reaches the shell only through an env var, never by
        # direct ${{ }} interpolation inside `run:` (script injection).
        for run in re.findall(r"run: \|\n((?:\s{10,}.*\n)+)", job):
            self.assertNotIn("${{", run)

    def test_other_jobs_keep_read_only_contents(self):
        self.assertIn("permissions:\n  contents: read", WORKFLOW)
        self.assertEqual(WORKFLOW.count("contents: write"), 1)


if __name__ == "__main__":
    unittest.main()
