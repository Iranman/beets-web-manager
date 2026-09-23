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


if __name__ == "__main__":
    unittest.main()
