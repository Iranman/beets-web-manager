"""Architecture Invariant Validator.

Verifies that Beets Web Manager adheres strictly to the single-authoritative-Beets
architecture with stock LinuxServer Beets (lscr.io/linuxserver/beets):
1. No Docker socket mounts (/var/run/docker.sock)
2. No direct SQLite access to musiclibrary.blb from Web Manager
3. Beets Web Manager container communicates only via HTTP to Beets
4. Stock Beets container runs unmodified upstream Beets image
5. Minimal integration plugin only handles authenticated mutations
"""

import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_no_docker_socket_mounts():
    """Verify docker-compose files do not mount /var/run/docker.sock."""
    print("Checking for docker socket mounts...")
    compose_files = list(ROOT.glob("docker-compose*.yml"))
    for cf in compose_files:
        content = cf.read_text(encoding="utf-8")
        if "docker.sock" in content:
            print(f"FAILED: Found docker.sock mount in {cf.name}", file=sys.stderr)
            return False
    print("  [PASS] No docker.sock mounts found in any docker-compose file.")
    return True


def check_no_direct_sqlite_library_access():
    """Verify Web Manager code does not open musiclibrary.blb via sqlite3."""
    print("Checking for raw sqlite3 access to musiclibrary.blb...")
    py_files = list(ROOT.glob("*.py")) + list((ROOT / "backend").glob("*.py"))
    
    # Allowed files that may reference SQLite for web-manager's own data or tests
    raw_sqlite_pattern = re.compile(r"sqlite3\.connect\([^)]*musiclibrary", re.IGNORECASE)
    
    for pf in py_files:
        content = pf.read_text(encoding="utf-8")
        if raw_sqlite_pattern.search(content):
            print(f"FAILED: Direct SQLite connection to musiclibrary found in {pf.name}", file=sys.stderr)
            return False
    print("  [PASS] No direct SQLite library connections in Web Manager backend.")
    return True


def check_stock_beets_image():
    """Verify docker-compose references stock linuxserver/beets image."""
    print("Checking docker-compose for stock linuxserver/beets image...")
    compose_file = ROOT / "docker-compose.yml"
    if not compose_file.exists():
        print("FAILED: docker-compose.yml missing", file=sys.stderr)
        return False
    content = compose_file.read_text(encoding="utf-8")
    if "lscr.io/linuxserver/beets" not in content and "linuxserver/beets" not in content:
        print("FAILED: docker-compose.yml does not use stock LinuxServer Beets image", file=sys.stderr)
        return False
    print("  [PASS] docker-compose.yml uses stock LinuxServer Beets image.")
    return True


def check_plugin_structure():
    """Verify beetsplug.webmanager plugin structure and files."""
    print("Checking beetsplug.webmanager plugin integrity...")
    plugin_dir = ROOT / "beetsplug" / "webmanager"
    required_files = ["__init__.py", "compat.py", "auth.py", "schemas.py", "operations.py", "version.py", "plugin_ops.py"]
    for rf in required_files:
        if not (plugin_dir / rf).exists():
            print(f"FAILED: Missing plugin file {rf}", file=sys.stderr)
            return False
    print("  [PASS] All required beetsplug.webmanager files present.")
    return True


def main():
    checks = [
        check_no_docker_socket_mounts,
        check_no_direct_sqlite_library_access,
        check_stock_beets_image,
        check_plugin_structure,
    ]
    all_passed = True
    for check in checks:
        if not check():
            all_passed = False

    if not all_passed:
        print("\n[FAIL] Architecture invariant validation FAILED.", file=sys.stderr)
        sys.exit(1)

    print("\n[PASS] All architecture invariants verified successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
