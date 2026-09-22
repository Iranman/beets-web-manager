"""Migration & Architecture Invariant Tests.

Guarantees that:
1. Architecture invariant validator passes with zero violations.
2. Web Manager contains NO direct SQLite access to musiclibrary.blb.
3. No Docker socket mounts exist.
4. Stock LinuxServer Beets image is used in docker-compose.
5. In Phase 5, requirements.txt will fail this test if Beets runtime is not retired.
"""

import os
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_architecture_invariants_script():
    """Verify scripts/validate_architecture_invariants.py exits cleanly."""
    script_path = ROOT / "scripts" / "validate_architecture_invariants.py"
    assert script_path.exists()
    res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert res.returncode == 0, f"Validator failed:\nstdout: {res.stdout}\nstderr: {res.stderr}"


def test_requirements_phase5_marker():
    """Documents Beets runtime dependency status.
    
    In Phase 1-4: Beets runtime is retained in requirements.txt as migration scaffolding.
    In Phase 5: Beets runtime MUST be removed from requirements.txt.
    """
    req_file = ROOT / "requirements.txt"
    content = req_file.read_text(encoding="utf-8")
    
    # Check if Phase 5 migration flag is set
    phase5_active = os.environ.get("PHASE5_RETIRE_BEETS_RUNTIME") == "1"
    if phase5_active:
        assert "beets==" not in content, "Phase 5 Invariant Violation: beets runtime must be removed from requirements.txt"
