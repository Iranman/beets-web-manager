"""CI guard enforcing the stock-Beets architecture invariant.

Beets Web Manager builds and publishes exactly one image,
ghcr.io/iranman/beets-web-manager. There is no custom Beets engine image --
Beets itself is the unmodified, official lscr.io/linuxserver/beets image.
This test fails if any ACTIVE GitHub Actions workflow tries to build or
publish a custom Beets engine image again.

It deliberately inspects PARSED YAML structure (PyYAML discards comments),
not raw file text: Dockerfile.beets and prose references to the removed
custom-engine CI jobs are allowed to remain as migration scaffolding during
Phase 1. This test fails only when an active workflow job actually tries to
USE the custom engine -- building/pushing it, referencing its Dockerfile as
a build context, or naming a job/step after the removed custom-engine jobs.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import unittest

import yaml

WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows")

VIOLATING_IMAGE_SUBSTRING = "ghcr.io/iranman/beets-engine"
VIOLATING_DOCKERFILE = "dockerfile.beets"
VIOLATING_JOB_NAME_SUBSTRINGS = (
    "beets-engine-verification",
    "beets-engine-latest-compat",
)
VIOLATING_STEP_NAME_SUBSTRINGS = (
    "build beets engine image",
    "build and push beets engine image",
    "verify beets engine image",
)


def _iter_workflow_files():
    files = glob.glob(os.path.join(WORKFLOWS_DIR, "*.yml"))
    files += glob.glob(os.path.join(WORKFLOWS_DIR, "*.yaml"))
    return sorted(files)


def _stringify(value) -> str:
    """Flatten a parsed-YAML value (str/dict/list) into one lowercase search string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        return " ".join(_stringify(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_stringify(v) for v in value)
    return str(value).lower()


class ArchitectureInvariantTests(unittest.TestCase):
    def test_no_active_workflow_builds_or_publishes_custom_beets_engine(self):
        violations = []
        workflow_files = _iter_workflow_files()
        self.assertTrue(workflow_files, "No workflow files found under .github/workflows -- test setup is broken")

        for path in workflow_files:
            with open(path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            if not isinstance(doc, dict):
                continue
            jobs = doc.get("jobs") or {}
            if not isinstance(jobs, dict):
                continue

            for job_key, job_def in jobs.items():
                job_key_lower = str(job_key).lower()
                for bad in VIOLATING_JOB_NAME_SUBSTRINGS:
                    if bad in job_key_lower:
                        violations.append(f"{path}: job key '{job_key}' matches a removed custom-engine job name")

                if not isinstance(job_def, dict):
                    continue

                job_name_lower = str(job_def.get("name", "")).lower()
                for bad in VIOLATING_JOB_NAME_SUBSTRINGS:
                    if bad in job_name_lower:
                        violations.append(f"{path}: job '{job_key}' has display name matching a removed custom-engine job")

                steps = job_def.get("steps") or []
                if not isinstance(steps, list):
                    continue

                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    step_label = step.get("name", "<unnamed step>")
                    step_name_lower = str(step_label).lower()
                    for bad in VIOLATING_STEP_NAME_SUBSTRINGS:
                        if bad in step_name_lower:
                            violations.append(
                                f"{path}: job '{job_key}' step '{step_label}' matches a removed custom-engine build/verify step"
                            )

                    combined = " ".join([_stringify(step.get("run")), _stringify(step.get("with"))])
                    if VIOLATING_IMAGE_SUBSTRING in combined:
                        violations.append(
                            f"{path}: job '{job_key}' step '{step_label}' references {VIOLATING_IMAGE_SUBSTRING}"
                        )
                    if VIOLATING_DOCKERFILE in combined:
                        violations.append(
                            f"{path}: job '{job_key}' step '{step_label}' actively uses Dockerfile.beets as a build input"
                        )

        self.assertEqual(
            violations,
            [],
            "Active CI must not build or publish a custom Beets engine image "
            "(the sole Beets runtime is the unmodified lscr.io/linuxserver/beets "
            "image):\n" + "\n".join(violations),
        )


ROOT = os.path.join(os.path.dirname(__file__), "..")
# Stock LinuxServer Beets, pinned to an exact LinuxServer build tag AND its
# manifest digest, so a Beets upgrade can only arrive as a reviewed PR.
PINNED_BEETS_RE = re.compile(r"lscr\.io/linuxserver/beets:(\d+\.\d+\.\d+)-ls\d+@sha256:[0-9a-f]{64}")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class PinnedStockBeetsImageTests(unittest.TestCase):
    def test_beets_image_is_stock_linuxserver_and_pinned_everywhere(self):
        compose_pin = re.search(r"^    image: (\S+)$", _read("docker-compose.yml"), re.M).group(1)
        self.assertRegex(compose_pin, "^" + PINNED_BEETS_RE.pattern + "$")
        # Every shipped Compose file and copy-paste install example uses the
        # same pin as docker-compose.yml (the single source of truth CI reads).
        for rel in ("docker-compose.full.yml", "docker-compose.dev.yml", "README.md",
                    "docs/INSTALLATION.md", "docs/EXAMPLES.md", "docs/CONFIGURATION.md"):
            refs = {m.group(0) for m in PINNED_BEETS_RE.finditer(_read(rel))}
            self.assertEqual(refs, {compose_pin}, rel)
            if rel != "docs/CONFIGURATION.md":  # documents `latest` as a user's own choice
                self.assertNotIn("linuxserver/beets:latest", _read(rel), rel)

    def test_shipped_python_does_not_describe_beets_latest(self):
        # Shipped code and scripts (not tests, whose fixtures may use :latest).
        paths = glob.glob(os.path.join(ROOT, "*.py"))
        for d in ("backend", "beetsplug", "scripts"):
            paths += glob.glob(os.path.join(ROOT, d, "**", "*.py"), recursive=True)
        offenders = [p for p in paths if "linuxserver/beets:latest" in _read(os.path.relpath(p, ROOT))]
        self.assertEqual(offenders, [])

    def test_beets_test_fixture_matches_pinned_image_version(self):
        version = PINNED_BEETS_RE.search(_read("docker-compose.yml")).group(1)
        pin = rf"beets=={re.escape(version)}\s"
        self.assertRegex(_read("requirements-dev.txt"), pin)
        self.assertIn(f'BEETS_TEST_FIXTURE_VERSION: "{version}"', _read(".github/workflows/docker-build.yml"))
        for wf in (".github/workflows/unit-tests.yml", ".github/workflows/security.yml"):
            self.assertRegex(_read(wf), "pip install " + pin, wf)

    @unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "needs POSIX bash")
    def test_ci_pull_script_refuses_an_unpinned_image_without_opt_in(self):
        # Fails before any docker login or pull: only the canary may float.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STOCK_BEETS_", "GHCR_TOKEN"))}
        env["STOCK_BEETS_PIN_OVERRIDE"] = "lscr.io/linuxserver/beets:latest"
        proc = subprocess.run(["bash", os.path.join(ROOT, "scripts", "ci_pull_stock_beets.sh")],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("is not the docker-compose.yml pin", proc.stderr)

    def test_compose_validator_rejects_floating_beets_latest(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            import validate_compose_security as vcs
        finally:
            sys.path.pop(0)
        errors = []
        vcs._check_image_digest_semantics("x: beets", "lscr.io/linuxserver/beets:latest", False, errors)
        self.assertEqual(errors, ["x: beets image uses latest: lscr.io/linuxserver/beets:latest"])


if __name__ == "__main__":
    unittest.main()
