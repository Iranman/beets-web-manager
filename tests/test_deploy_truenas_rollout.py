"""Tests for scripts/deploy_truenas_web_manager.sh.

Two complementary strategies, matching what each check actually needs:

1. Function-level tests: source the script (its `main()` guard is
   `[[ "${BASH_SOURCE[0]}" == "${0}" ]]`, which is false when sourced, so
   nothing runs automatically) into a tiny wrapper script, pre-set the
   globals a function reads, call it directly, and assert on exit
   code/stderr. Used for the database/token/file safety checks -- these
   operate on real temp files and don't need Docker at all.

2. End-to-end tests: run the script for real (dry-run, full deploy,
   rollback) against a fake `docker`/`docker compose` (tests/deploy/
   fake_docker.py) and a fake `curl` (tests/deploy/fake_curl.py) driven by a
   JSON "world state" file, plus a real temp directory tree standing in for
   the TrueNAS stack directory. No real Docker daemon or TrueNAS host is
   used or required.

Every test that expects failure asserts on the actual rejection reason
(via stderr), not just a non-zero exit code, so a check silently changing
meaning wouldn't still pass.
"""
import hashlib
import http.server
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_truenas_web_manager.sh"
FAKE_DOCKER = ROOT / "tests" / "deploy" / "fake_docker.py"
FAKE_CURL = ROOT / "tests" / "deploy" / "fake_curl.py"
SCRIPT_SOURCE = SCRIPT.read_text(encoding="utf-8")


def _bash_survives_nested_subprocess(bash_path: str) -> bool:
    """The real harness has bash launch python3 as a child process (the
    fake docker/curl/lsof shims), not just run a builtin. Some bash
    installs answer a plain `-c "echo"` fine but hang on that
    nested-subprocess shape (observed with Git-for-Windows' MSYS bash
    spawning python3.exe on this codebase's Windows dev box) -- probe the
    actual pattern with a hard, tree-killed timeout so a bad bash is
    treated as "unusable here", never as a wedged test run.
    """
    probe_dir = tempfile.mkdtemp(prefix="bash-probe-")
    try:
        script = os.path.join(probe_dir, "probe.sh")
        with open(script, "w", newline="\n") as f:
            f.write('#!/usr/bin/env bash\n"$1" -c "print(\'nested-ok\')"\n')
        proc = subprocess.Popen(
            [bash_path, script, sys.executable],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            out, _err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return False
        return proc.returncode == 0 and "nested-ok" in out
    except Exception:
        return False
    finally:
        import shutil as _shutil
        _shutil.rmtree(probe_dir, ignore_errors=True)


def _detect_working_bash() -> str:
    """Return the path to a bash that can actually execute this test
    file's real script-plus-subprocess pattern, or "" if none is
    available/trustworthy.

    On Windows, `bash` on PATH commonly resolves to the WSL launcher stub
    (`%SystemRoot%\\system32\\bash.exe`), which prints a WSL relay error
    and exits nonzero when no Linux distro is registered -- that's a
    broken *launcher*, not proof no usable bash exists on the box. An
    explicit `ROLLOUT_TEST_BASH` is trusted directly (operator has already
    verified it works) and always wins, including on CI/Linux/macOS. With
    nothing explicit set, only a non-Windows PATH `bash` is auto-trusted;
    Git-for-Windows' bundled bash is not auto-selected because its
    nested-subprocess handling has been observed to hang indefinitely on
    this exact fake-docker/fake-curl-subprocess harness, which would wedge
    the whole test run rather than fail it -- a Windows contributor who has
    verified a specific bash.exe works can still opt in via
    ROLLOUT_TEST_BASH.
    """
    import shutil

    explicit = os.environ.get("ROLLOUT_TEST_BASH", "").strip()
    if explicit:
        resolved = explicit if (os.path.isabs(explicit) and os.path.exists(explicit)) else shutil.which(explicit)
        if resolved and _bash_survives_nested_subprocess(resolved):
            return resolved
        return ""

    if os.name == "nt":
        return ""

    resolved = shutil.which("bash") or ""
    if resolved and _bash_survives_nested_subprocess(resolved):
        return resolved
    return ""


BASH = _detect_working_bash()
_NO_BASH_REASON = (
    "No working POSIX bash interpreter found for this platform (checked "
    "ROLLOUT_TEST_BASH and, on non-Windows, PATH) -- "
    "deploy_truenas_web_manager.sh is a bash script and these tests "
    "execute it for real, including nested subprocess calls that are known "
    "to hang under Windows' bundled bash implementations. Run on Linux/"
    "macOS/a working WSL distro, or set ROLLOUT_TEST_BASH to a bash.exe "
    "you have verified handles nested subprocesses correctly."
)


def make_sqlite_db(path, items=10, albums=2):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT)")
    con.executemany("INSERT INTO items (title) VALUES (?)", [(f"t{i}",) for i in range(items)])
    con.executemany("INSERT INTO albums (album) VALUES (?)", [(f"a{i}",) for i in range(albums)])
    con.commit()
    con.close()


def corrupt_db_keep_openable(path):
    """Damage page data (not the header) so the file still opens but
    PRAGMA quick_check reports corruption rather than 'ok'."""
    with open(path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = min(200, size // 2)
        f.seek(start)
        f.write(b"\xff" * max(1, size - start))


@unittest.skipUnless(BASH, _NO_BASH_REASON)
class RolloutScriptTestBase(unittest.TestCase):
    """Shared fixture: a fakebin dir (docker/curl/lsof shims) always on
    PATH, plus a scratch dir for real temp files/DBs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rollout-test-")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        self._write_wrapper("docker", FAKE_DOCKER)
        self._write_wrapper("curl", FAKE_CURL)
        self.set_open_files([])  # default: lsof reports nothing open
        self.docker_state = os.path.join(self.tmp, "docker_state.json")
        self.set_semantic()

    def set_semantic(self, *, exec_fail=False, **fields):
        """The running engine's online semantic snapshot (what the script
        reads through the Beets web API via `docker exec`)."""
        snap = {"items": 100, "albums": 10, "listed_items": 100, "listed_albums": 10,
                "digest": "d" * 64, "plugin_version": "1.2.0"}
        snap.update(fields)
        if "items" in fields and "listed_items" not in fields:
            snap["listed_items"] = fields["items"]
        state = {"containers": {"cid-beets": {"Name": "/beets", "State": {"Status": "running"}}},
                 "service_containers": {"beets": "cid-beets"}, "images": {},
                 "semantic_snapshot": snap, "exec_should_fail": exec_fail}
        with open(self.docker_state, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _write_wrapper(self, name, target_py):
        path = os.path.join(self.fakebin, name)
        with open(path, "w", newline="\n") as f:
            f.write(f'#!/usr/bin/env bash\nexec python3 "{target_py}" "$@"\n')
        os.chmod(path, 0o755)

    def set_open_files(self, paths):
        """(Re)write the fake `lsof` shim to report exactly `paths` as open."""
        path = os.path.join(self.fakebin, "lsof")
        lines = ["#!/usr/bin/env bash", 'target="${@: -1}"', 'case "$target" in']
        for p in paths:
            lines.append(f'  "{p}") exit 0 ;;')
        lines.append("esac")
        lines.append("exit 1")
        with open(path, "w", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(path, 0o755)

    def base_env(self, **extra):
        env = dict(os.environ)
        env["PATH"] = self.fakebin + os.pathsep + env["PATH"]
        env["STACK_DIR"] = self.tmp
        env["VERSION"] = "0.1.6"
        env["FAKE_DOCKER_STATE"] = self.docker_state
        for k, v in extra.items():
            env[k] = str(v)
        return env

    def run_snippet(self, body, env=None):
        snippet_path = os.path.join(self.tmp, "snippet.sh")
        with open(snippet_path, "w", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
            f.write(f'source "{SCRIPT.as_posix()}"\n')
            f.write('ENGINE_CID="cid-beets"\n')
            f.write(body + "\n")
        return subprocess.run(
            [BASH, snippet_path], env=env or self.base_env(),
            capture_output=True, text=True, timeout=30,
        )


class VersionValidationTests(RolloutScriptTestBase):
    def test_missing_version_fails_before_mutation(self):
        res = self.run_snippet("validate_version", env=self.base_env(VERSION=""))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("set VERSION", res.stderr)

    def test_numbered_version_accepted(self):
        for valid in ("0.1.6", "1.0.0", "1.2.3-rc.1", "0.1.5"):
            res = self.run_snippet("validate_version", env=self.base_env(VERSION=valid))
            self.assertEqual(res.returncode, 0, f"Expected {valid} to be accepted, got: {res.stderr}")

    def test_latest_rejected(self):
        res = self.run_snippet("validate_version", env=self.base_env(VERSION="latest"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("explicit numbered release", res.stderr)

    def test_stable_rejected(self):
        res = self.run_snippet("validate_version", env=self.base_env(VERSION="stable"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("explicit numbered release", res.stderr)

    def test_malformed_version_rejected(self):
        res = self.run_snippet("validate_version", env=self.base_env(VERSION="bad_ver_!"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("explicit numbered release", res.stderr)


class AuthoritativeDatabaseChecksTests(RolloutScriptTestBase):
    def test_missing_authoritative_db_is_rejected(self):
        res = self.run_snippet(f"""
AUTH_DB_PATH="{self.tmp}/does-not-exist/musiclibrary.blb"
STALE_DB_PATH="{self.tmp}/also-missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("authoritative database missing", res.stderr)

    def test_online_check_reads_the_engine_api_never_the_live_sqlite_file(self):
        """While Beets runs the live file is never opened: even bytes that
        are not SQLite pass, because counts and identity come from the
        engine's own web API (the main file is only hashed, informationally)."""
        bad = os.path.join(self.tmp, "musiclibrary.blb")
        with open(bad, "wb") as f:
            f.write(b"this is not a sqlite database file at all")
        res = self.run_snippet(f"""
AUTH_DB_PATH="{bad}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
echo "ITEMS=$AUTH_ITEM_COUNT DIGEST=$AUTH_SEMANTIC_DIGEST"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ITEMS=100 DIGEST=" + "d" * 64, res.stdout)
        self.assertIn("informational only", res.stderr)

    def test_engine_api_unreachable_is_rejected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=500, albums=50)
        self.set_semantic(exec_fail=True)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("could not read the library through", res.stderr)

    def test_unhealthy_plugin_is_rejected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db)
        self.set_semantic(plugin_version="")
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("webmanager plugin is not healthy", res.stderr)

    def test_stats_and_listing_disagreement_is_rejected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db)
        self.set_semantic(items=100, listed_items=99)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("listed 99", res.stderr)

    def test_suspiciously_low_item_count_is_rejected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=2, albums=1)
        self.set_semantic(items=2, albums=1)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
""", env=self.base_env(MIN_ITEM_COUNT=10))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("suspiciously low", res.stderr)

    def test_same_canonical_path_as_stale_is_rejected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=20, albums=2)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{db}"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SAME canonical path", res.stderr)

    def test_same_inode_as_stale_is_rejected(self):
        auth_dir = os.path.join(self.tmp, "auth")
        stale_dir = os.path.join(self.tmp, "stale")
        os.makedirs(auth_dir)
        os.makedirs(stale_dir)
        db = os.path.join(auth_dir, "musiclibrary.blb")
        make_sqlite_db(db, items=20, albums=2)
        stale = os.path.join(stale_dir, "musiclibrary.blb")
        os.link(db, stale)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{stale}"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("same inode", res.stderr)

    def test_same_checksum_as_stale_is_rejected(self):
        auth_dir = os.path.join(self.tmp, "auth2")
        stale_dir = os.path.join(self.tmp, "stale2")
        os.makedirs(auth_dir)
        os.makedirs(stale_dir)
        db = os.path.join(auth_dir, "musiclibrary.blb")
        make_sqlite_db(db, items=20, albums=2)
        stale = os.path.join(stale_dir, "musiclibrary.blb")
        with open(db, "rb") as src, open(stale, "wb") as dst:
            dst.write(src.read())
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{stale}"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("identical SHA-256", res.stderr)

    def test_healthy_authoritative_db_passes_and_records_metadata(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=100, albums=10)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
echo "ITEMS=$AUTH_ITEM_COUNT ALBUMS=$AUTH_ALBUM_COUNT SHA=$AUTH_DB_SHA256"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ITEMS=100 ALBUMS=10", res.stdout)


class StaleDatabaseChecksTests(RolloutScriptTestBase):
    def _dirs(self):
        engine = os.path.join(self.tmp, "engine-config")
        webmgr = os.path.join(self.tmp, "webmgr-data")
        os.makedirs(engine, exist_ok=True)
        os.makedirs(webmgr, exist_ok=True)
        return engine, webmgr

    def test_stale_db_with_more_items_than_authoritative_is_rejected(self):
        engine, webmgr = self._dirs()
        stale = os.path.join(webmgr, "musiclibrary.blb")
        make_sqlite_db(stale, items=500, albums=5)
        res = self.run_snippet(f"""
ENGINE_CONFIG_SRC="{engine}"
WEBMGR_DATA_SRC="{webmgr}"
STALE_DB_PATH="{stale}"
AUTH_ITEM_COUNT=10
inspect_stale_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("MORE items", res.stderr)

    def test_stale_db_over_max_items_ceiling_is_rejected(self):
        engine, webmgr = self._dirs()
        stale = os.path.join(webmgr, "musiclibrary.blb")
        make_sqlite_db(stale, items=50, albums=5)
        res = self.run_snippet(f"""
ENGINE_CONFIG_SRC="{engine}"
WEBMGR_DATA_SRC="{webmgr}"
STALE_DB_PATH="{stale}"
AUTH_ITEM_COUNT=10000
inspect_stale_database
""", env=self.base_env(STALE_DB_MAX_ITEMS=10))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("exceeds STALE_DB_MAX_ITEMS", res.stderr)

    def test_stale_db_query_failure_is_rejected_not_treated_as_zero(self):
        engine, webmgr = self._dirs()
        stale = os.path.join(webmgr, "musiclibrary.blb")
        with open(stale, "wb") as f:
            f.write(b"garbage, not a database")
        res = self.run_snippet(f"""
ENGINE_CONFIG_SRC="{engine}"
WEBMGR_DATA_SRC="{webmgr}"
STALE_DB_PATH="{stale}"
AUTH_ITEM_COUNT=10000
inspect_stale_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("quick_check failed to execute", res.stderr)

    def test_disposable_stale_db_passes(self):
        engine, webmgr = self._dirs()
        stale = os.path.join(webmgr, "musiclibrary.blb")
        make_sqlite_db(stale, items=3, albums=1)
        res = self.run_snippet(f"""
ENGINE_CONFIG_SRC="{engine}"
WEBMGR_DATA_SRC="{webmgr}"
STALE_DB_PATH="{stale}"
AUTH_ITEM_COUNT=10000
inspect_stale_database
echo "STALE_ITEMS=$STALE_ITEM_COUNT EXISTS=$STALE_DB_EXISTS"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("STALE_ITEMS=3 EXISTS=1", res.stdout)

    def test_absent_stale_db_is_a_no_op(self):
        engine, webmgr = self._dirs()
        res = self.run_snippet(f"""
ENGINE_CONFIG_SRC="{engine}"
WEBMGR_DATA_SRC="{webmgr}"
STALE_DB_PATH="{webmgr}/musiclibrary.blb"
AUTH_ITEM_COUNT=10000
inspect_stale_database
echo "EXISTS=$STALE_DB_EXISTS"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("EXISTS=0", res.stdout)


class EndpointVerificationModeTests(RolloutScriptTestBase):
    def test_dry_run_endpoint_failure_returns_warning_without_exiting(self):
        state_file = os.path.join(self.tmp, "curl_state.json")
        Path(state_file).write_text(json.dumps({"fail_paths": ["/api/health"]}), encoding="utf-8")
        res = self.run_snippet("""
verify_endpoints "dry-run" || rc=$?
echo "RC=$rc"
""", env=self.base_env(FAKE_CURL_STATE=state_file))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("endpoint checks failed during dry-run", res.stderr)
        self.assertIn("RC=1", res.stdout)

    def test_post_deploy_endpoint_failure_is_fatal(self):
        state_file = os.path.join(self.tmp, "curl_state.json")
        Path(state_file).write_text(json.dumps({"fail_paths": ["/api/health"]}), encoding="utf-8")
        res = self.run_snippet("""
verify_endpoints "post-deploy"
""", env=self.base_env(FAKE_CURL_STATE=state_file))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("one or more endpoint checks failed", res.stderr)


class TokenChecksTests(RolloutScriptTestBase):
    def test_empty_token_file_is_rejected(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        token = os.path.join(webmgr, ".auth_token")
        Path(token).write_bytes(b"")
        res = self.run_snippet(f"""
TOKEN_PATH="{token}"
WEBMGR_LEGACY_CONFIG_SRC=""
inspect_auth_token
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("empty", res.stderr)

    def test_healthy_token_is_recorded_without_printing_contents(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        token = os.path.join(webmgr, ".auth_token")
        Path(token).write_text("super-secret-token-value-do-not-print", encoding="utf-8")
        os.chmod(token, stat.S_IRUSR | stat.S_IWUSR)
        res = self.run_snippet(f"""
TOKEN_PATH="{token}"
WEBMGR_LEGACY_CONFIG_SRC=""
inspect_auth_token
echo "EXISTS=$TOKEN_EXISTS SIZE=$TOKEN_SIZE ACTIVE=$ACTIVE_AUTH_TOKEN_PATH"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("super-secret-token-value-do-not-print", res.stdout)
        self.assertNotIn("super-secret-token-value-do-not-print", res.stderr)
        self.assertIn("EXISTS=1", res.stdout)
        self.assertIn(f"ACTIVE={token}", res.stdout)

    def test_migration_does_not_overwrite_existing_destination_token(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        legacy_dir = os.path.join(self.tmp, "legacy")
        os.makedirs(webmgr)
        os.makedirs(legacy_dir)
        dest = os.path.join(webmgr, ".auth_token")
        legacy = os.path.join(legacy_dir, ".auth_token")
        Path(dest).write_text("ORIGINAL_DEST_TOKEN", encoding="utf-8")
        Path(legacy).write_text("LEGACY_TOKEN_VALUE", encoding="utf-8")

        res = self.run_snippet(f"""
TOKEN_EXISTS=1
TOKEN_PATH="{dest}"
LEGACY_TOKEN_PATH="{legacy}"
NEEDS_TOKEN_MIGRATION=1
BACKUP_DIR="{self.tmp}"
migrate_token_if_needed
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(dest).read_text(encoding="utf-8"), "ORIGINAL_DEST_TOKEN")

    def test_migration_refuses_when_legacy_equals_beets_api_token(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        legacy_dir = os.path.join(self.tmp, "legacy2")
        os.makedirs(webmgr)
        os.makedirs(legacy_dir)
        dest = os.path.join(webmgr, ".auth_token")
        legacy = os.path.join(legacy_dir, ".auth_token")
        Path(legacy).write_text("shared-secret-value", encoding="utf-8")

        res = self.run_snippet(f"""
TOKEN_EXISTS=0
TOKEN_PATH="{dest}"
LEGACY_TOKEN_PATH="{legacy}"
NEEDS_TOKEN_MIGRATION=1
BACKUP_DIR="{self.tmp}"
migrate_token_if_needed
""", env=self.base_env(BEETS_API_TOKEN="shared-secret-value"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("never using the Beets engine API token", res.stderr)
        self.assertFalse(os.path.exists(dest))

    def test_migration_copies_atomically_and_verifies_checksum(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        legacy_dir = os.path.join(self.tmp, "legacy3")
        os.makedirs(webmgr)
        os.makedirs(legacy_dir)
        dest = os.path.join(webmgr, ".auth_token")
        legacy = os.path.join(legacy_dir, ".auth_token")
        Path(legacy).write_text("legacy-token-value-distinct", encoding="utf-8")

        res = self.run_snippet(f"""
TOKEN_EXISTS=0
TOKEN_PATH="{dest}"
LEGACY_TOKEN_PATH="{legacy}"
NEEDS_TOKEN_MIGRATION=1
BACKUP_DIR="{self.tmp}"
migrate_token_if_needed
""", env=self.base_env(BEETS_API_TOKEN="something-else-entirely"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(dest).read_text(encoding="utf-8"), "legacy-token-value-distinct")
        leftover_tmp = [p for p in os.listdir(webmgr) if "migrate.tmp" in p]
        self.assertEqual(leftover_tmp, [], "no orphaned migration temp file should remain")


class TokenMigrationAndRollbackTests(RolloutScriptTestBase):
    def test_dry_run_uses_legacy_token_read_only(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        legacy_dir = os.path.join(self.tmp, "legacy")
        os.makedirs(webmgr)
        os.makedirs(legacy_dir)
        dest = os.path.join(webmgr, ".auth_token")
        legacy = os.path.join(legacy_dir, ".auth_token")
        Path(legacy).write_text("secret-legacy-token-12345", encoding="utf-8")

        res = self.run_snippet(f"""
DRY_RUN=1
TOKEN_PATH="{dest}"
WEBMGR_LEGACY_CONFIG_SRC="{legacy_dir}"
inspect_auth_token
echo "ACTIVE=$ACTIVE_AUTH_TOKEN_PATH MIGRATION=$NEEDS_TOKEN_MIGRATION EXISTS=$TOKEN_EXISTS"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        # The script's own canon_path() always normalizes to forward
        # slashes (os.path.realpath(...).replace("\\", "/")) regardless of
        # platform; match that here rather than asserting the raw
        # os.path.join() result, which is backslash-separated on Windows.
        expected_active = os.path.realpath(legacy).replace("\\", "/")
        self.assertIn(f"ACTIVE={expected_active}", res.stdout)
        self.assertIn("MIGRATION=1", res.stdout)
        self.assertIn("EXISTS=0", res.stdout)
        self.assertFalse(os.path.exists(dest), "dry-run must not create persistent token")
        self.assertNotIn("secret-legacy-token-12345", res.stdout)
        self.assertNotIn("secret-legacy-token-12345", res.stderr)

    def test_rollback_case_b_migrated_token_removed_safely(self):
        import hashlib
        webmgr = os.path.join(self.tmp, "webmgr")
        legacy_dir = os.path.join(self.tmp, "legacy")
        os.makedirs(webmgr)
        os.makedirs(legacy_dir)
        dest = os.path.join(webmgr, ".auth_token")
        legacy = os.path.join(legacy_dir, ".auth_token")
        compose_file = os.path.join(self.tmp, "docker-compose.yml")
        Path(compose_file).write_text("services: {}\n", encoding="utf-8")
        Path(legacy).write_text("migrated-token-val", encoding="utf-8")
        Path(dest).write_text("migrated-token-val", encoding="utf-8")
        token_sha = hashlib.sha256(b"migrated-token-val").hexdigest()

        backup_dir = os.path.join(self.tmp, "backup")
        os.makedirs(backup_dir)
        Path(os.path.join(backup_dir, "docker-compose.yml.bak")).write_text("services: {}\n", encoding="utf-8")
        Path(os.path.join(backup_dir, "token-metadata.txt")).write_text(f"""persistent_token_existed_before=0
legacy_token_existed_before=1
token_migration_planned=1
persistent_token_path={dest}
legacy_token_path={legacy}
token_migration_performed=1
migrated_token_sha256={token_sha}
""", encoding="utf-8")

        res = self.run_snippet(f"""
_compose() {{ return 0; }}
docker() {{ echo "healthy"; }}
resolve_container_id() {{ echo "cid-mock"; }}
discover_and_verify_mounts() {{ return 0; }}
require_compose_pull_flag() {{ return 0; }}
# Token handling only: the recreate + proof step and backup verification
# (BackupManifestTests) have their own end-to-end tests.
verify_backup_manifest() {{ return 0; }}
rollback_recreate_and_verify() {{ return 0; }}
STACK_DIR="{self.tmp}"
COMPOSE_FILE="{compose_file}"
TOKEN_PATH="{dest}"
WEBMGR_DATA_SRC="{webmgr}"
ENGINE_CONFIG_SRC="{self.tmp}/engine"
ROLLBACK_DIR="{backup_dir}"
run_rollback
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(dest), "rollback must remove the migrated persistent token")
        self.assertTrue(os.path.exists(legacy), "rollback must leave the legacy token untouched")

    def test_rollback_case_b_checksum_mismatch_refuses_deletion(self):
        import hashlib
        webmgr = os.path.join(self.tmp, "webmgr")
        legacy_dir = os.path.join(self.tmp, "legacy")
        os.makedirs(webmgr)
        os.makedirs(legacy_dir)
        dest = os.path.join(webmgr, ".auth_token")
        legacy = os.path.join(legacy_dir, ".auth_token")
        compose_file = os.path.join(self.tmp, "docker-compose.yml")
        Path(compose_file).write_text("services: {}\n", encoding="utf-8")
        Path(legacy).write_text("migrated-token-val", encoding="utf-8")
        Path(dest).write_text("MODIFIED_POST_ROLLOUT_TOKEN", encoding="utf-8")
        token_sha = hashlib.sha256(b"migrated-token-val").hexdigest()

        backup_dir = os.path.join(self.tmp, "backup")
        os.makedirs(backup_dir)
        Path(os.path.join(backup_dir, "docker-compose.yml.bak")).write_text("services: {}\n", encoding="utf-8")
        Path(os.path.join(backup_dir, "token-metadata.txt")).write_text(f"""persistent_token_existed_before=0
legacy_token_existed_before=1
token_migration_planned=1
persistent_token_path={dest}
legacy_token_path={legacy}
token_migration_performed=1
migrated_token_sha256={token_sha}
""", encoding="utf-8")

        res = self.run_snippet(f"""
_compose() {{ return 0; }}
docker() {{ echo "healthy"; }}
resolve_container_id() {{ echo "cid-mock"; }}
discover_and_verify_mounts() {{ return 0; }}
require_compose_pull_flag() {{ return 0; }}
# Token handling only: the recreate + proof step and backup verification
# (BackupManifestTests) have their own end-to-end tests.
verify_backup_manifest() {{ return 0; }}
rollback_recreate_and_verify() {{ return 0; }}
STACK_DIR="{self.tmp}"
COMPOSE_FILE="{compose_file}"
TOKEN_PATH="{dest}"
WEBMGR_DATA_SRC="{webmgr}"
ENGINE_CONFIG_SRC="{self.tmp}/engine"
ROLLBACK_DIR="{backup_dir}"
run_rollback
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.exists(dest), "checksum mismatch must refuse token deletion")
        self.assertIn("refusing automatic deletion", res.stderr)

    def test_rollback_case_c_generated_token_refuses_deletion(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        dest = os.path.join(webmgr, ".auth_token")
        compose_file = os.path.join(self.tmp, "docker-compose.yml")
        Path(compose_file).write_text("services: {}\n", encoding="utf-8")
        Path(dest).write_text("NEW_APP_GENERATED_TOKEN", encoding="utf-8")

        backup_dir = os.path.join(self.tmp, "backup")
        os.makedirs(backup_dir)
        Path(os.path.join(backup_dir, "docker-compose.yml.bak")).write_text("services: {}\n", encoding="utf-8")
        Path(os.path.join(backup_dir, "token-metadata.txt")).write_text(f"""persistent_token_existed_before=0
legacy_token_existed_before=0
token_migration_planned=0
persistent_token_path={dest}
legacy_token_path=
""", encoding="utf-8")

        res = self.run_snippet(f"""
_compose() {{ return 0; }}
docker() {{ echo "healthy"; }}
resolve_container_id() {{ echo "cid-mock"; }}
discover_and_verify_mounts() {{ return 0; }}
require_compose_pull_flag() {{ return 0; }}
# Token handling only: the recreate + proof step and backup verification
# (BackupManifestTests) have their own end-to-end tests.
verify_backup_manifest() {{ return 0; }}
rollback_recreate_and_verify() {{ return 0; }}
STACK_DIR="{self.tmp}"
COMPOSE_FILE="{compose_file}"
TOKEN_PATH="{dest}"
WEBMGR_DATA_SRC="{webmgr}"
ENGINE_CONFIG_SRC="{self.tmp}/engine"
ROLLBACK_DIR="{backup_dir}"
run_rollback
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.exists(dest), "generated token without proof must refuse deletion")
        self.assertIn("refusing automatic deletion", res.stderr)


class LocalDbRecreationDetectionTests(RolloutScriptTestBase):
    def test_reappeared_local_db_is_detected(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        stale = os.path.join(webmgr, "musiclibrary.blb")
        Path(stale).write_bytes(b"a local db came back")
        res = self.run_snippet(f"""
STALE_DB_PATH="{stale}"
STALE_WAL_PATH="{webmgr}/musiclibrary.blb-wal"
STALE_SHM_PATH="{webmgr}/musiclibrary.blb-shm"
WEBMGR_DATA_SRC="{webmgr}"
assert_no_local_db_recreated
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("reappeared after deployment", res.stderr)

    def test_absence_of_local_db_passes(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        res = self.run_snippet(f"""
STALE_DB_PATH="{webmgr}/musiclibrary.blb"
STALE_WAL_PATH="{webmgr}/musiclibrary.blb-wal"
STALE_SHM_PATH="{webmgr}/musiclibrary.blb-shm"
WEBMGR_DATA_SRC="{webmgr}"
assert_no_local_db_recreated
""")
        self.assertEqual(res.returncode, 0, res.stderr)


class AuthoritativeDbUnchangedPostDeployTests(RolloutScriptTestBase):
    def test_item_count_change_is_detected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=10, albums=2)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
AUTH_ITEM_COUNT=999
AUTH_ALBUM_COUNT=10
AUTH_DB_SHA256="deadbeef"
assert_authoritative_db_unchanged
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("item count changed", res.stderr)

    def test_live_main_file_hash_change_alone_is_informational_not_a_verdict(self):
        """In WAL mode the main .blb can change (checkpoint) with no logical
        change, and stay identical while the WAL holds changes: the live
        hash is recorded, never used as proof either way."""
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=10, albums=2)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
AUTH_ITEM_COUNT=100
AUTH_ALBUM_COUNT=10
AUTH_SEMANTIC_DIGEST="{"d" * 64}"
AUTH_DB_SHA256="0000000000000000000000000000000000000000000000000000000000000000"
assert_authoritative_db_unchanged
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ONLINE SEMANTIC INTEGRITY confirmed", res.stderr)
        self.assertIn("NOT a byte-identity claim", res.stderr)

    def test_identity_digest_change_is_detected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=10, albums=2)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
AUTH_ITEM_COUNT=100
AUTH_ALBUM_COUNT=10
AUTH_SEMANTIC_DIGEST="{"e" * 64}"
AUTH_DB_SHA256="x"
assert_authoritative_db_unchanged
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("identity digest changed", res.stderr)

    def test_unchanged_db_passes(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=10, albums=2)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
assert_authoritative_db_unchanged
echo "OK"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OK", res.stdout)


class ScriptSourceSafetyInvariantTests(unittest.TestCase):
    """Static checks on the script's own text for constraints best proven
    by absence or pattern match."""

    def test_no_wildcard_glob_on_database_filename(self):
        self.assertNotIn("musiclibrary.blb*", SCRIPT_SOURCE)

    def test_exact_db_filenames_only(self):
        self.assertIn('DB_FILENAME="musiclibrary.blb"', SCRIPT_SOURCE)
        self.assertIn('WAL_FILENAME="musiclibrary.blb-wal"', SCRIPT_SOURCE)
        self.assertIn('SHM_FILENAME="musiclibrary.blb-shm"', SCRIPT_SOURCE)

    @staticmethod
    def _statements():
        """Yield (lineno, statement) for every ';'/'&'/'|'-separated
        statement in the script, comments stripped and whitespace trimmed --
        a line-anchored regex (`^rm\\s`) misses `rm` inside an indented
        if-block entirely, since the line then starts with spaces, not
        'rm'. Splitting on statement separators and stripping each piece
        finds 'rm' regardless of indentation."""
        for lineno, line in enumerate(SCRIPT_SOURCE.splitlines(), start=1):
            code = line.split("#", 1)[0]
            for statement in re.split(r"[;&|]", code):
                statement = statement.strip()
                if statement:
                    yield lineno, statement

    def test_no_rm_of_database_files(self):
        # Databases are archived (moved), never deleted -- no exception.
        for lineno, statement in self._statements():
            if re.match(r"rm\b", statement):
                self.assertNotRegex(
                    statement, r"musiclibrary|DB_PATH|WAL_PATH|SHM_PATH",
                    f"line {lineno} uses 'rm' on what looks like a database path: {statement!r}",
                )

    def test_token_rm_appears_exactly_once_and_only_in_guarded_rollback_path(self):
        """Guarded rollback intentionally removes a rollout-created token
        (Case B in run_rollback()) once persistent_token_existed_before=0,
        token_migration_performed=1, the canonical path matches exactly,
        and the checksum matches -- see TokenMigrationAndRollbackTests for
        behavioral proof. Any OTHER 'rm' touching a token anywhere in the
        script would mean deletion logic exists outside that single
        reviewed, guarded, tested path -- a real regression, not a style
        nit, so this stays a hard failure rather than a warning."""
        token_rm_statements = [
            (lineno, statement) for lineno, statement in self._statements()
            if re.match(r"rm\b", statement) and re.search(r"auth_token|TOKEN_PATH", statement)
        ]
        self.assertEqual(
            len(token_rm_statements), 1,
            f"expected exactly one token-deleting 'rm' statement in the script, found: {token_rm_statements}",
        )
        _, statement = token_rm_statements[0]
        self.assertRegex(statement, r'rm\s+-f\s+"\$TOKEN_PATH"')

        func_start = SCRIPT_SOURCE.index("run_rollback() {")
        func_end = SCRIPT_SOURCE.index("\n}\n", func_start)
        rollback_body = SCRIPT_SOURCE[func_start:func_end]
        rm_pos = rollback_body.index('rm -f "$TOKEN_PATH"')
        guard_scope = rollback_body[:rm_pos]

        for condition in (
            '"$p_existed" == "0"',
            '"$migration_performed" == "1"',
            '"$canon_dst" == "$canon_meta"',
            '"$current_sha" == "$m_sha"',
        ):
            self.assertIn(condition, guard_scope, f"token rm is not preceded by required guard: {condition}")

        # The rm must be the very next statement after its guarding `if`'s
        # `then` -- not merely present somewhere earlier in the function.
        self.assertTrue(
            guard_scope.rstrip().endswith("then"),
            "token rm must immediately follow its guarding `if ...; then`, not float free in the function body",
        )

    def test_stale_database_is_moved_not_deleted(self):
        self.assertIn("mv \"$STALE_DB_PATH\"", SCRIPT_SOURCE)

    def test_uses_set_dash_Eeuo_pipefail(self):
        self.assertIn("set -Eeuo pipefail", SCRIPT_SOURCE)

    def test_beets_engine_service_is_never_recreated(self):
        self.assertNotIn('--force-recreate "$ENGINE_SERVICE"', SCRIPT_SOURCE)
        self.assertIn('--force-recreate "$SERVICE"', SCRIPT_SOURCE)

    def test_documentation_and_script_specify_explicit_bash_invocation(self):
        doc_source = (ROOT / "docs" / "TRUENAS_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn("/bin/bash", doc_source)
        self.assertIn("/bin/bash", SCRIPT_SOURCE)


@unittest.skipUnless(BASH, _NO_BASH_REASON)
class EndToEndFixture(unittest.TestCase):
    """Full-script tests against a fake Docker/Compose world."""

    GOOD_IMAGE = "ghcr.io/iranman/beets-web-manager:0.1.3"
    GOOD_REVISION = "36bfc7554378a9ef6bd8f9c47a7d1be553647503"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rollout-e2e-")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        for name, target in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            path = os.path.join(self.fakebin, name)
            with open(path, "w", newline="\n") as f:
                f.write(f'#!/usr/bin/env bash\nexec python3 "{target}" "$@"\n')
            os.chmod(path, 0o755)
        lsof_path = os.path.join(self.fakebin, "lsof")
        with open(lsof_path, "w", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nexit 1\n")
        os.chmod(lsof_path, 0o755)

        self.stack_dir = os.path.join(self.tmp, "stack")
        self.engine_dir = os.path.join(self.stack_dir, "beets")
        self.webmgr_dir = os.path.join(self.stack_dir, "beets-web-manager")
        os.makedirs(self.engine_dir)
        os.makedirs(self.webmgr_dir)

        self.compose_file = os.path.join(self.stack_dir, "docker-compose.yml")
        Path(self.compose_file).write_text(
            "services:\n  beets:\n    image: beets-engine:local\n"
            "  beets-web-manager:\n    image: ghcr.io/iranman/beets-web-manager:0.1.3\n"
            "  lidarr:\n    image: lidarr:local\n",
            encoding="utf-8",
        )

        self.auth_db = os.path.join(self.engine_dir, "musiclibrary.blb")
        make_sqlite_db(self.auth_db, items=3144, albums=200)

        self.state_path = os.path.join(self.tmp, "state.json")
        self.state = {
            "containers": {
                "cid-beets": {
                    "Name": "/beets",
                    "State": {"Status": "running", "Health": {"Status": "healthy"}},
                    "Config": {"Image": "beets-engine:local"},
                    "Image": "sha256:enginecurrent",
                    "Mounts": [{"Destination": "/config", "Source": self.engine_dir}],
                },
                "cid-webmgr": {
                    "Name": "/beets-web-manager",
                    "State": {"Status": "running", "Health": {"Status": "healthy"}},
                    "Config": {"Image": self.GOOD_IMAGE},
                    "Image": "sha256:goodimageid",
                    "Mounts": [{"Destination": "/web-manager-data", "Source": self.webmgr_dir}],
                },
                "cid-lidarr": {
                    "Name": "/lidarr",
                    "State": {"Status": "running", "Health": {"Status": "healthy"}},
                    "Config": {"Image": "lidarr:local"},
                    "Image": "sha256:lidarrid",
                    "Mounts": [],
                },
            },
            "service_containers": {
                "beets": "cid-beets", "beets-web-manager": "cid-webmgr", "lidarr": "cid-lidarr",
            },
            "semantic_snapshot": {"items": 3144, "albums": 200, "listed_items": 3144, "listed_albums": 200,
                                  "digest": "a" * 64, "plugin_version": "1.2.0"},
            "images": {
                self.GOOD_IMAGE: {
                    "Id": "sha256:goodimageid",
                    "Config": {"Labels": {
                        "org.opencontainers.image.version": "0.1.3",
                        "org.opencontainers.image.revision": self.GOOD_REVISION,
                    }},
                },
            },
        }
        self._save_state()

    def _save_state(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f)

    def env(self, **overrides):
        curl_state = {
            "item_count": overrides.pop("curl_item_count", 3144),
            "fail_paths": overrides.pop("curl_fail_paths", []),
            "blocking_reasons_by_image": overrides.pop("curl_blocking_reasons_by_image", {}),
            "blocking_reason_codes_by_image": overrides.pop("curl_blocking_reason_codes_by_image", {}),
            "setup_status_http_by_image": overrides.pop("curl_setup_status_http_by_image", {}),
            "auth_required": overrides.pop("curl_auth_required", False),
        }
        curl_state_path = os.path.join(self.tmp, "curl_state.json")
        with open(curl_state_path, "w", encoding="utf-8") as f:
            json.dump(curl_state, f)

        e = dict(os.environ)
        e["PATH"] = self.fakebin + os.pathsep + e["PATH"]
        e["FAKE_DOCKER_STATE"] = self.state_path
        e["FAKE_CURL_STATE"] = curl_state_path
        e["STACK_DIR"] = self.stack_dir
        e["COMPOSE_FILE"] = self.compose_file
        e["HEALTH_TIMEOUT_SECONDS"] = "5"
        e["VERSION"] = "0.1.3"
        e["EXPECTED_REVISION"] = self.GOOD_REVISION
        for k, v in overrides.items():
            e[k] = str(v)
        return e

    def run_script(self, *args, env=None):
        return subprocess.run(
            [BASH, str(SCRIPT), *args], env=env or self.env(),
            capture_output=True, text=True, timeout=90,
        )


class DryRunTests(EndToEndFixture):
    def test_dry_run_happy_path_succeeds_and_touches_nothing_in_stack_dir(self):
        before = self._snapshot_stack_dir()
        res = self.run_script("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DRY RUN COMPLETE", res.stderr)
        after = self._snapshot_stack_dir()
        self.assertEqual(before, after, "dry-run must not modify anything under STACK_DIR")
        self.assertFalse(os.path.isdir(os.path.join(self.stack_dir, "_backups")))
        self.assertEqual(self.state["containers"]["cid-webmgr"]["State"]["Status"], "running")

    def _snapshot_stack_dir(self):
        snap = {}
        for root, _, files in os.walk(self.stack_dir):
            for name in files:
                p = os.path.join(root, name)
                snap[p] = (os.path.getsize(p), os.path.getmtime(p))
        return snap

    def test_data_mount_preferred_over_web_manager_data_mount(self):
        # The image declares both /data and /web-manager-data as VOLUME, but
        # app.py's own WEB_MANAGER_DATA_DIR resolution prefers /data whenever
        # it is mounted. A container with both mounts present must resolve
        # against /data -- checking /web-manager-data instead would silently
        # verify persistence (auth token, stale-DB detection) against a
        # directory the running app never actually reads from or writes to.
        # Distinct, non-prefix-colliding names for the two candidate mount
        # sources -- "beets-web-manager" is itself a string prefix of
        # "beets-web-manager-data", which would make a substring assertion
        # against the wrong path spuriously pass/fail regardless of which
        # one the script actually picked.
        real_data_dir = os.path.join(self.stack_dir, "real-data-mount")
        legacy_anon_dir = os.path.join(self.stack_dir, "legacy-anon-volume")
        os.makedirs(real_data_dir, exist_ok=True)
        os.makedirs(legacy_anon_dir, exist_ok=True)
        self.state["containers"]["cid-webmgr"]["Mounts"] = [
            {"Destination": "/data", "Source": real_data_dir},
            {"Destination": "/web-manager-data", "Source": legacy_anon_dir},
        ]
        self._save_state()
        res = self.run_script("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        # The script's own canon_path() always normalizes to forward slashes
        # (os.path.realpath(...).replace("\\", "/")) regardless of platform.
        real_data_canon = os.path.realpath(real_data_dir).replace("\\", "/")
        legacy_anon_canon = os.path.realpath(legacy_anon_dir).replace("\\", "/")
        self.assertIn(f"web-manager data source: {real_data_canon}", res.stderr)
        self.assertNotIn(legacy_anon_canon, res.stderr)

    def _anon_data_plus_bind_web_manager_data(self, env):
        """The live layout that broke v0.2.0's rollout: an anonymous volume
        at /data carried across recreates, the real bind mount at
        /web-manager-data, and WEB_MANAGER_DATA_DIR naming the bind."""
        bind_dir = os.path.join(self.stack_dir, "bind-web-manager-data")
        anon_dir = os.path.join(self.stack_dir, "anonymous-volume")
        os.makedirs(bind_dir, exist_ok=True)
        os.makedirs(anon_dir, exist_ok=True)
        cont = self.state["containers"]["cid-webmgr"]
        cont["Mounts"] = [
            {"Destination": "/web-manager-data", "Source": bind_dir},
            {"Destination": "/data", "Source": anon_dir},
        ]
        cont["Config"]["Env"] = ["TZ=UTC", *env]
        self._save_state()
        return (os.path.realpath(bind_dir).replace("\\", "/"),
                os.path.realpath(anon_dir).replace("\\", "/"))

    def test_web_manager_data_dir_env_picks_its_bind_mount_over_anonymous_data(self):
        bind_canon, anon_canon = self._anon_data_plus_bind_web_manager_data(
            ["WEB_MANAGER_DATA_DIR=/web-manager-data"])
        Path(bind_canon, ".auth_token").write_text("t" * 32, encoding="utf-8")
        res = self.run_script("--dry-run", env=self.env(curl_auth_required=True))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"web-manager data source: {bind_canon}", res.stderr)
        self.assertIn("Persistent web auth token found", res.stderr)
        self.assertNotIn(anon_canon, res.stderr)

    def test_web_manager_data_dir_env_without_matching_mount_is_refused(self):
        self._anon_data_plus_bind_web_manager_data(["WEB_MANAGER_DATA_DIR=/state"])
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           webmgr_data_dir_unmounted", res.stderr)

    def test_web_manager_data_dir_env_that_is_not_a_plain_path_is_refused(self):
        self._anon_data_plus_bind_web_manager_data(["WEB_MANAGER_DATA_DIR=/x' or 'y"])
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           webmgr_data_dir_unmounted", res.stderr)

    def test_web_manager_data_dir_env_set_but_empty_is_refused(self):
        self._anon_data_plus_bind_web_manager_data(["WEB_MANAGER_DATA_DIR="])
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           webmgr_data_dir_unmounted", res.stderr)

    def test_web_manager_data_dir_env_with_dot_segments_is_refused(self):
        for value in ("/web-manager-data/../data", "/./web-manager-data", "/web-manager-data/."):
            self._anon_data_plus_bind_web_manager_data([f"WEB_MANAGER_DATA_DIR={value}"])
            res = self.run_script("--dry-run")
            self.assertNotEqual(res.returncode, 0, value)
            self.assertIn("Reason code:           webmgr_data_dir_unmounted", res.stderr, value)

    def test_token_less_data_source_is_refused_when_auth_is_required(self):
        # No WEB_MANAGER_DATA_DIR: the legacy fallback picks the empty /data
        # volume, which has no token while the app enforces auth.
        _bind, anon_canon = self._anon_data_plus_bind_web_manager_data([])
        res = self.run_script("--dry-run", env=self.env(curl_auth_required=True))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           webmgr_data_source_without_token", res.stderr)
        self.assertIn(anon_canon, res.stderr)

    def test_token_less_data_source_passes_when_auth_is_disabled(self):
        self._anon_data_plus_bind_web_manager_data([])
        res = self.run_script("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("the app does not require one", res.stderr)

    def test_dry_run_rejects_same_mount_source_for_both_services(self):
        self.state["containers"]["cid-webmgr"]["Mounts"][0]["Source"] = self.engine_dir
        self._save_state()
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SAME path", res.stderr)

    def test_dry_run_rejects_wrong_compose_image(self):
        self.state["containers"]["cid-webmgr"]["Config"]["Image"] = "ghcr.io/iranman/beets-web-manager:0.1.2"
        self._save_state()
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           compose_image_mismatch", res.stderr)
        self.assertIn("'ghcr.io/iranman/beets-web-manager:0.1.3' (pinned to this script's VERSION)", res.stderr)

    def test_dry_run_rejects_wrong_revision_label(self):
        self.state["images"][self.GOOD_IMAGE]["Config"]["Labels"]["org.opencontainers.image.revision"] = "0000000deadbeef"
        self._save_state()
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("revision label", res.stderr)

    def test_failed_dry_run_reports_no_rollback_required_message(self):
        res = self.run_script("--dry-run", env=self.env(VERSION="invalid-version"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Dry-run only: no production mutation occurred; no rollback is required", res.stderr)
        self.assertNotIn("--rollback", res.stderr)


class FullDeployTests(EndToEndFixture):
    def test_full_deploy_happy_path_succeeds(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("initial-token-value-not-printed", encoding="utf-8")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("completed successfully", res.stderr)
        self.assertNotIn("initial-token-value-not-printed", res.stdout)
        self.assertNotIn("initial-token-value-not-printed", res.stderr)
        webmgr_cid = self.state["service_containers"]["beets-web-manager"]
        self.assertEqual(self.state["containers"][webmgr_cid]["Config"]["Image"], self.GOOD_IMAGE)
        backups = os.listdir(os.path.join(self.stack_dir, "_backups"))
        self.assertEqual(len(backups), 1)

    def test_deploy_persists_version_into_existing_env_file(self):
        """Found live: a deploy resolves the correct image for its own run via
        an in-process `export BEETS_WEB_MANAGER_VERSION`, but a stale value
        left on disk in .env silently reverts the NEXT recreation (a host
        reboot, a routine stack-wide `docker compose pull && up -d`) back to
        an old version. The deploy must durably rewrite .env's own line to
        the version it just verified healthy."""
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        env_file = os.path.join(self.stack_dir, ".env")
        Path(env_file).write_text("SOME_OTHER_VAR=1\nBEETS_WEB_MANAGER_VERSION=0.1.1\nANOTHER_VAR=2\n", encoding="utf-8")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        content = Path(env_file).read_text(encoding="utf-8")
        self.assertIn("BEETS_WEB_MANAGER_VERSION=0.1.3", content)
        self.assertNotIn("BEETS_WEB_MANAGER_VERSION=0.1.1", content)
        # Untouched surrounding lines.
        self.assertIn("SOME_OTHER_VAR=1", content)
        self.assertIn("ANOTHER_VAR=2", content)

    def test_deploy_appends_version_line_when_env_file_has_none(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        env_file = os.path.join(self.stack_dir, ".env")
        Path(env_file).write_text("SOME_OTHER_VAR=1\n", encoding="utf-8")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        content = Path(env_file).read_text(encoding="utf-8")
        self.assertIn("BEETS_WEB_MANAGER_VERSION=0.1.3", content)
        self.assertIn("SOME_OTHER_VAR=1", content)

    def test_deploy_succeeds_without_an_env_file_at_all(self):
        """The simplified single-compose deployment topology has no .env at
        all -- persistence must be a no-op warning, never fatal."""
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        env_file = os.path.join(self.stack_dir, ".env")
        self.assertFalse(os.path.exists(env_file))
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("BEETS_WEB_MANAGER_VERSION not persisted", res.stderr)

    def test_deploy_archives_stale_db_with_missing_wal_shm(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        stale = os.path.join(self.webmgr_dir, "musiclibrary.blb")
        make_sqlite_db(stale, items=2, albums=1)
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(stale))
        backup_root = os.path.join(self.stack_dir, "_backups")
        backup_dir = os.path.join(backup_root, os.listdir(backup_root)[0])
        self.assertTrue(os.path.exists(os.path.join(backup_dir, "stale-database", "musiclibrary.blb")))

    def test_deploy_refuses_a_token_less_data_source_before_any_backup_or_stop(self):
        anon_dir = os.path.join(self.stack_dir, "anonymous-volume")
        os.makedirs(anon_dir)
        self.state["containers"]["cid-webmgr"]["Mounts"].append({"Destination": "/data", "Source": anon_dir})
        self._save_state()
        res = self.run_script(env=self.env(curl_auth_required=True))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           webmgr_data_source_without_token", res.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.stack_dir, "_backups")))
        with open(self.state_path, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["containers"]["cid-webmgr"]["State"]["Status"], "running")

    def test_deploy_does_not_touch_beets_or_lidarr_containers(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        beets_cid_before = self.state["service_containers"]["beets"]
        lidarr_cid_before = self.state["service_containers"]["lidarr"]
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.state_path, encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after["service_containers"]["beets"], beets_cid_before)
        self.assertEqual(after["service_containers"]["lidarr"], lidarr_cid_before)

    def test_deploy_detects_unexpected_recreation_of_other_service(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        self.state["also_recreate_other"] = "beets"
        self._save_state()
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("changed container ID during recreate", res.stderr)

    def test_deploy_fails_closed_on_health_timeout_and_reports_rollback_command(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        self.state["never_healthy"] = True
        self._save_state()
        res = self.run_script(env=self.env(HEALTH_TIMEOUT_SECONDS=2))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("did not become healthy", res.stderr)
        self.assertIn("ROLLOUT FAILED", res.stderr)
        self.assertIn("Rollback command:", res.stderr)
        self.assertIn("--rollback", res.stderr)


class RollbackTests(EndToEndFixture):
    def test_rollback_restores_previous_service_without_touching_beets(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        deploy_res = self.run_script()
        self.assertEqual(deploy_res.returncode, 0, deploy_res.stderr)

        backup_root = os.path.join(self.stack_dir, "_backups")
        backup_dir = os.path.join(backup_root, os.listdir(backup_root)[0])

        self.state["containers"][self.state["service_containers"]["beets-web-manager"]]["Config"]["Image"] = "ghcr.io/iranman/beets-web-manager:9.9.9"
        self._save_state()
        beets_cid_before = self.state["service_containers"]["beets"]

        res = self.run_script("--rollback", backup_dir)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Rollback complete", res.stderr)

        with open(self.state_path, encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after["service_containers"]["beets"], beets_cid_before)
        self.assertEqual(
            after["containers"][after["service_containers"]["beets"]]["Config"]["Image"],
            "beets-engine:local",
            "rollback must never touch the Beets engine",
        )

    def test_rollback_succeeds_with_version_completely_absent(self):
        """VERSION must never be required for --rollback: it restores
        whatever image reference the backup itself recorded, so there is
        nothing for VERSION to mean here -- and requiring it would block
        recovery at exactly the moment an operator needs the script to just
        work without having to remember/guess which version was running."""
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        deploy_res = self.run_script()  # normal deploy; VERSION is set here
        self.assertEqual(deploy_res.returncode, 0, deploy_res.stderr)

        backup_root = os.path.join(self.stack_dir, "_backups")
        backup_dir = os.path.join(backup_root, os.listdir(backup_root)[0])

        rollback_env = self.env()
        del rollback_env["VERSION"]
        self.assertNotIn("VERSION", rollback_env)

        res = self.run_script("--rollback", backup_dir, env=rollback_env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Rollback complete", res.stderr)
        self.assertNotIn("set VERSION", res.stderr)


class VersionedStackFixture(EndToEndFixture):
    """A stack whose Compose file pins the web manager through
    ${BEETS_WEB_MANAGER_VERSION} in .env (the documented TrueNAS layout),
    currently running OLD_IMAGE, with real Web Manager state files and a
    provisioned plugin on disk. VERSION=0.1.3 deploys GOOD_IMAGE."""

    OLD_IMAGE = "ghcr.io/iranman/beets-web-manager:0.1.2"
    OLD_REVISION = "1111111111111111111111111111111111111111"

    def setUp(self):
        super().setUp()
        Path(self.compose_file).write_text(
            "services:\n  beets:\n    image: beets-engine:local\n"
            "  beets-web-manager:\n    image: ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}\n"
            "  lidarr:\n    image: lidarr:local\n",
            encoding="utf-8",
        )
        self.stack_env = os.path.join(self.stack_dir, ".env")
        Path(self.stack_env).write_text("OTHER_SETTING=keep-me\nBEETS_WEB_MANAGER_VERSION=0.1.2\n", encoding="utf-8")
        self.state["images"][self.OLD_IMAGE] = {
            "Id": "sha256:oldimageid",
            "Config": {"Labels": {
                "org.opencontainers.image.version": "0.1.2",
                "org.opencontainers.image.revision": self.OLD_REVISION,
            }},
        }
        webmgr = self.state["containers"]["cid-webmgr"]
        webmgr["Config"]["Image"] = self.OLD_IMAGE
        webmgr["Image"] = "sha256:oldimageid"
        webmgr["Config"]["Env"] = ["TZ=UTC", "PLEX_TOKEN=plex-secret-value-xyz",
                                   "ACOUSTID_API_KEY=acoustid-secret-value-xyz"]
        self.state["compose_environment"] = {
            "beets-web-manager": {"TZ": "UTC", "LIDARR_API_KEY": "lidarr-secret-value-xyz"},
            "lidarr": {"API_KEY": "lidarr-own-secret-xyz"},
        }
        self._save_state()

        Path(self.webmgr_dir, ".auth_token").write_text("tok-not-printed", encoding="utf-8")
        Path(self.webmgr_dir, ".env").write_text("AI_MODEL=before-deploy\n", encoding="utf-8")
        Path(self.webmgr_dir, ".browser_username").write_text("admin", encoding="utf-8")
        Path(self.webmgr_dir, ".browser_password").write_text("pbkdf2:hash-before", encoding="utf-8")
        Path(self.webmgr_dir, ".flask_secret_key").write_text("flask-key-before", encoding="utf-8")
        Path(self.webmgr_dir, ".setup_complete").write_text("1", encoding="utf-8")
        os.makedirs(os.path.join(self.webmgr_dir, "transactions"))
        Path(self.webmgr_dir, "transactions", "t1.json").write_text('{"id": 1}', encoding="utf-8")

        Path(self.engine_dir, "config.yaml").write_text("directory: /music\n", encoding="utf-8")
        self.plugin_dir = os.path.join(self.engine_dir, "beetsplug", "webmanager")
        os.makedirs(self.plugin_dir)
        self.set_provisioned_plugin("1.2.0")

    def set_provisioned_plugin(self, version):
        Path(self.plugin_dir, "version.py").write_text(
            f'PLUGIN_VERSION = "{version}"\nPROTOCOL_VERSION = "1.0"\n', encoding="utf-8")

    def load_state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, st):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def webmgr_container(self):
        st = self.load_state()
        return st["containers"][st["service_containers"]["beets-web-manager"]]

    def backup_dir(self):
        root = os.path.join(self.stack_dir, "_backups")
        names = [n for n in os.listdir(root) if n.startswith("web-manager-rollout-")]
        self.assertEqual(len(names), 1, names)
        return os.path.join(root, names[0])

    def deploy(self, **env):
        res = self.run_script(env=self.env(**env))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.webmgr_container()["Config"]["Image"], self.GOOD_IMAGE)
        return res


class RollbackProofTests(VersionedStackFixture):
    def test_deploy_then_rollback_lands_on_previous_image_and_pins_env(self):
        self.deploy()
        self.assertIn("BEETS_WEB_MANAGER_VERSION=0.1.3", Path(self.stack_env).read_text(encoding="utf-8"))

        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        cont = self.webmgr_container()
        self.assertEqual(cont["Config"]["Image"], self.OLD_IMAGE)
        self.assertEqual(cont["Image"], "sha256:oldimageid")
        env_text = Path(self.stack_env).read_text(encoding="utf-8")
        self.assertIn("BEETS_WEB_MANAGER_VERSION=0.1.2", env_text)
        self.assertNotIn("BEETS_WEB_MANAGER_VERSION=0.1.3", env_text)
        self.assertIn("OTHER_SETTING=keep-me", env_text, "rollback rewrites only the version line")
        self.assertIn("rollback is durable", res.stderr)
        self.assertIn("/health/live reports version 0.1.2", res.stderr)

        # What the operator (or a host reboot / stack-wide refresh) does next:
        # a plain `docker compose up -d` with nothing exported must stay put.
        env = self.env()
        env.pop("BEETS_WEB_MANAGER_VERSION", None)
        up = subprocess.run(
            [BASH, os.path.join(self.fakebin, "docker"), "compose", "-f", self.compose_file,
             "up", "-d", "beets-web-manager"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(up.returncode, 0, up.stderr)
        self.assertEqual(self.webmgr_container()["Config"]["Image"], self.OLD_IMAGE)

    def test_rollback_fails_loudly_when_the_recreate_fails(self):
        self.deploy()
        st = self.load_state()
        st["up_should_fail"] = True
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("recreating beets-web-manager on ghcr.io/iranman/beets-web-manager:0.1.2 failed", res.stderr)
        self.assertNotIn("Rollback complete", res.stderr)

    def test_rollback_fails_loudly_when_the_previous_image_is_not_what_runs(self):
        self.deploy()
        st = self.load_state()
        del st["images"][self.OLD_IMAGE]  # the recreate lands on some other image ID
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertNotEqual(res.returncode, 0)
        # Caught before the recreate now: the previous image cannot be
        # re-tagged because it is gone and no registry digest was recorded.
        self.assertIn("sha256:oldimageid", res.stderr)
        self.assertNotIn("Rollback complete", res.stderr)

    def test_rollback_works_when_the_web_manager_is_stopped(self):
        """A failed deploy or an earlier failed rollback can leave the
        service stopped; `docker compose ps -q` does not list it then."""
        self.deploy()
        st = self.load_state()
        st["containers"][st["service_containers"]["beets-web-manager"]]["State"] = {"Status": "exited"}
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.webmgr_container()["Image"], "sha256:oldimageid")

    def test_rollback_without_previous_image_record_refuses(self):
        self.deploy()
        os.remove(os.path.join(self.backup_dir(), "previous-image.txt"))
        # Drop it from the manifest too (else backup verification refuses first).
        manifest = Path(self.backup_dir(), "state-manifest.txt")
        manifest.write_text("".join(ln for ln in manifest.read_text(encoding="utf-8").splitlines(True)
                                    if not ln.startswith("previous-image.txt ")), encoding="utf-8")
        res = self.run_script("--rollback", self.backup_dir())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("cannot prove a rollback", res.stderr)

    def test_rollback_restores_web_manager_state_and_keeps_new_audit_records(self):
        self.deploy()
        # What a new version might do while it runs:
        Path(self.webmgr_dir, ".env").write_text("AI_MODEL=after-deploy\nDOWNLOADS_PATH=./downloads\n", encoding="utf-8")
        Path(self.webmgr_dir, ".flask_secret_key").write_text("flask-key-after", encoding="utf-8")
        Path(self.webmgr_dir, "transactions", "t2.json").write_text('{"id": 2}', encoding="utf-8")
        Path(self.engine_dir, "config.yaml").write_text("directory: /music\npluginpath: changed\n", encoding="utf-8")

        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(self.webmgr_dir, ".env").read_text(encoding="utf-8"), "AI_MODEL=before-deploy\n")
        self.assertEqual(Path(self.webmgr_dir, ".flask_secret_key").read_text(encoding="utf-8"), "flask-key-before")
        self.assertEqual(Path(self.engine_dir, "config.yaml").read_text(encoding="utf-8"), "directory: /music\n")
        self.assertTrue(Path(self.webmgr_dir, "transactions", "t2.json").exists(),
                        "audit records written after the deploy are never removed")
        pre = [n for n in os.listdir(self.backup_dir()) if n.startswith("pre-rollback-")]
        self.assertEqual(len(pre), 1)
        self.assertIn("after-deploy",
                      Path(self.backup_dir(), pre[0], "web-manager-data", ".env").read_text(encoding="utf-8"))

    def test_rollback_restores_beetsplug_exactly_and_removes_files_the_new_version_added(self):
        self.deploy()
        # What a newer plugin might leave behind: a changed module and a new one.
        self.set_provisioned_plugin("9.9.9")
        Path(self.plugin_dir, "added_by_new_version.py").write_text("x = 1\n", encoding="utf-8")
        os.makedirs(os.path.join(self.plugin_dir, "newpkg"))
        Path(self.plugin_dir, "newpkg", "__init__.py").write_text("", encoding="utf-8")
        outside = os.path.join(self.tmp, "outside-beetsplug.txt")
        Path(outside).write_text("untouched", encoding="utf-8")

        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(sorted(os.listdir(self.plugin_dir)), ["version.py"],
                         "files the new version added must not survive the rollback")
        self.assertIn('PLUGIN_VERSION = "1.2.0"',
                      Path(self.plugin_dir, "version.py").read_text(encoding="utf-8"))
        self.assertEqual(Path(outside).read_text(encoding="utf-8"), "untouched")
        self.assertIn("Restored Beets beetsplug/ exactly as backed up", res.stderr)
        pre = [n for n in os.listdir(self.backup_dir()) if n.startswith("pre-rollback-")]
        self.assertEqual(len(pre), 1)
        kept = os.path.join(self.backup_dir(), pre[0], "beets-config", "beetsplug", "webmanager")
        self.assertTrue(os.path.isfile(os.path.join(kept, "added_by_new_version.py")),
                        "the replaced plugin files are kept for inspection, not destroyed")

    def test_rollback_continues_and_proves_the_outcome_when_stop_fails(self):
        self.deploy()
        st = self.load_state()
        st["stop_should_fail"] = True
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("'docker compose stop beets-web-manager' failed (exit 1)", res.stderr)
        self.assertIn("continuing the rollback", res.stderr)
        self.assertEqual(self.webmgr_container()["Image"], "sha256:oldimageid")
        self.assertIn("/health/live reports version 0.1.2", res.stderr)

    def test_rollback_with_failed_stop_still_fails_when_the_proof_fails(self):
        self.deploy()
        st = self.load_state()
        st["stop_should_fail"] = True
        st["up_should_fail"] = True
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("'docker compose stop beets-web-manager' failed", res.stderr)
        self.assertNotIn("Rollback complete", res.stderr)


class RollbackDataSourceTests(VersionedStackFixture):
    """--rollback restores web-manager-data/ only into the folder the backup
    was taken from; it never moves another folder's live state aside."""

    def _manifest(self):
        return Path(self.backup_dir(), "state-manifest.txt")

    def _drop_manifest_data_src(self):
        m = self._manifest()
        lines = m.read_text(encoding="utf-8").splitlines()
        m.write_text("\n".join(l for l in lines if not l.startswith("webmgr_data_src=")) + "\n", encoding="utf-8")

    def test_backup_records_its_data_folder(self):
        self.deploy()
        canon = os.path.realpath(self.webmgr_dir).replace("\\", "/")
        self.assertIn(f"webmgr_data_src={canon}\n", self._manifest().read_text(encoding="utf-8"))

    def test_rollback_into_another_data_folder_is_refused_before_anything_changes(self):
        self.deploy()
        other = os.path.join(self.stack_dir, "other-data")
        os.makedirs(other)
        Path(other, ".env").write_text("live\n", encoding="utf-8")
        st = self.load_state()
        cont = st["containers"][st["service_containers"]["beets-web-manager"]]
        cont["Mounts"] = [{"Destination": "/web-manager-data", "Source": other}]
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           backup_data_source_mismatch", res.stderr)
        self.assertEqual(Path(other, ".env").read_text(encoding="utf-8"), "live\n")
        self.assertEqual(self.webmgr_container()["Config"]["Image"], self.GOOD_IMAGE)
        self.assertEqual(self.webmgr_container()["State"]["Status"], "running")

    def test_older_backup_falls_back_to_the_token_path_and_rolls_back_normally(self):
        self.deploy()
        self._drop_manifest_data_src()
        Path(self.webmgr_dir, ".browser_setup_state").write_text("new", encoding="utf-8")
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Backup data folder matches the current one", res.stderr)
        self.assertFalse(Path(self.webmgr_dir, ".browser_setup_state").exists(),
                         "a file created after the deploy is still moved aside")

    def test_backup_without_any_data_folder_record_restores_only_its_own_files(self):
        self.deploy()
        self._drop_manifest_data_src()
        meta = Path(self.backup_dir(), "token-metadata.txt")
        meta.write_text("\n".join(l for l in meta.read_text(encoding="utf-8").splitlines()
                                  if not l.startswith("persistent_token_path=")) + "\n", encoding="utf-8")
        digest = hashlib.sha256(meta.read_bytes()).hexdigest()
        m = self._manifest()
        m.write_text(re.sub(r"^token-metadata\.txt sha256=\w+$", f"token-metadata.txt sha256={digest}",
                            m.read_text(encoding="utf-8"), flags=re.M), encoding="utf-8")
        Path(self.webmgr_dir, ".browser_setup_state").write_text("new", encoding="utf-8")
        Path(self.webmgr_dir, ".flask_secret_key").write_text("flask-key-after", encoding="utf-8")
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("does not record which folder", res.stderr)
        self.assertEqual(Path(self.webmgr_dir, ".browser_setup_state").read_text(encoding="utf-8"), "new")
        self.assertEqual(Path(self.webmgr_dir, ".flask_secret_key").read_text(encoding="utf-8"), "flask-key-before")


class BackupManifestTests(VersionedStackFixture):
    """#178: --rollback verifies the backup against its checksum manifest
    before anything is stopped or changed; a backup without one (older
    script) needs --allow-legacy-backup."""

    def _rollback_refused_untouched(self, code, *args):
        state_before = Path(self.state_path).read_text(encoding="utf-8")
        compose_before = Path(self.compose_file).read_text(encoding="utf-8")
        env_before = Path(self.webmgr_dir, ".env").read_text(encoding="utf-8")
        res = self.run_script("--rollback", self.backup_dir(), *args)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"Reason code:           {code}", res.stdout + res.stderr)
        self.assertIn("nothing was stopped or changed", res.stderr)
        self.assertEqual(Path(self.state_path).read_text(encoding="utf-8"), state_before, "a container was touched")
        self.assertEqual(Path(self.compose_file).read_text(encoding="utf-8"), compose_before)
        self.assertEqual(Path(self.webmgr_dir, ".env").read_text(encoding="utf-8"), env_before)
        return res

    def test_backup_lists_a_checksum_for_every_file_it_restores_from(self):
        self.deploy()
        text = Path(self.backup_dir(), "state-manifest.txt").read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[0], "manifest_version=2")
        for rel in ("docker-compose.yml.bak", ".env.bak", "auth_token.bak", "previous-image.txt",
                    "web-manager-data/.env", "web-manager-data/transactions/t1.json",
                    "beets-config/config.yaml", "beets-config/beetsplug/webmanager/version.py"):
            self.assertRegex(text, rf"(?m)^{re.escape(rel)} sha256=[0-9a-f]{{64}}$")

    def test_rollback_refuses_a_backup_without_a_manifest(self):
        self.deploy()
        os.remove(os.path.join(self.backup_dir(), "state-manifest.txt"))
        res = self._rollback_refused_untouched("backup_manifest_missing")
        self.assertIn("--allow-legacy-backup", res.stderr)

    def test_rollback_refuses_an_old_script_manifest_without_checksums(self):
        self.deploy()
        Path(self.backup_dir(), "state-manifest.txt").write_text(
            "web-manager-data/.env sha256=" + "0" * 64 + "\n", encoding="utf-8")
        self._rollback_refused_untouched("backup_manifest_missing")

    def test_allow_legacy_backup_rolls_back_with_a_warning(self):
        self.deploy()
        os.remove(os.path.join(self.backup_dir(), "state-manifest.txt"))
        res = self.run_script("--rollback", self.backup_dir(), "--allow-legacy-backup")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("UNVERIFIED BACKUP", res.stderr)
        self.assertEqual(self.webmgr_container()["Image"], "sha256:oldimageid")

    def test_rollback_refuses_a_changed_backup_file(self):
        self.deploy()
        Path(self.backup_dir(), "web-manager-data", ".env").write_text("AI_MODEL=tampered\n", encoding="utf-8")
        res = self._rollback_refused_untouched("backup_manifest_mismatch")
        self.assertIn("web-manager-data/.env (missing or changed)", res.stderr)

    def test_rollback_refuses_a_file_added_to_the_backup(self):
        self.deploy()
        Path(self.backup_dir(), "web-manager-data", "transactions", "planted.json").write_text("{}", encoding="utf-8")
        res = self._rollback_refused_untouched("backup_manifest_mismatch")
        self.assertIn("web-manager-data/transactions/planted.json (not in the manifest)", res.stderr)

    def test_rollback_refuses_manifest_paths_outside_the_backup(self):
        self.deploy()
        outside = Path(self.tmp, "outside.txt")
        outside.write_text("x", encoding="utf-8")
        sha = hashlib.sha256(b"x").hexdigest()
        rel_up = os.path.relpath(outside, self.backup_dir()).replace(os.sep, "/")
        with open(os.path.join(self.backup_dir(), "state-manifest.txt"), "a", encoding="utf-8", newline="\n") as f:
            f.write(f"{rel_up} sha256={sha}\n{outside.as_posix()} sha256={sha}\n sha256={sha}\n")
        res = self._rollback_refused_untouched("backup_manifest_mismatch")
        self.assertIn(f"{rel_up} (not a path inside the backup)", res.stderr)
        self.assertIn(f"{outside.as_posix()} (not a path inside the backup)", res.stderr)
        self.assertIn("<empty path> (not a path inside the backup)", res.stderr)

    def _fail_deploy_inside_state_backup(self):
        """A deploy that dies while copying state, after the web manager was
        stopped: an unreadable transaction record makes `cp` fail."""
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-0 file")
        bad = Path(self.webmgr_dir, "transactions", "t2.json")
        bad.write_text("{}", encoding="utf-8")
        os.chmod(bad, 0)
        try:
            res = self.run_script(env=self.env())
        finally:
            os.chmod(bad, 0o600)
        self.assertNotEqual(res.returncode, 0)
        return res

    def test_a_deploy_that_fails_inside_the_state_backup_leaves_a_rollbackable_backup(self):
        # QA F1 on #241: this backup used to be refused as "modified".
        res = self._fail_deploy_inside_state_backup()
        self.assertIn("Failed stage:          backup-state", res.stderr)
        self.assertIn("the backup is incomplete", res.stderr)
        self.assertIn("incomplete_backup_stage=backup-state",
                      Path(self.backup_dir(), "state-manifest.txt").read_text(encoding="utf-8"))
        rb = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(rb.returncode, 0, rb.stderr)
        self.assertNotIn("backup_manifest", rb.stdout + rb.stderr)
        self.assertIn("INCOMPLETE BACKUP", rb.stderr)
        self.assertIn("failed during backup-state", rb.stderr)
        self.assertEqual(self.webmgr_container()["Image"], "sha256:oldimageid")

    def test_an_incomplete_backup_that_was_changed_afterwards_is_still_refused(self):
        self._fail_deploy_inside_state_backup()
        Path(self.backup_dir(), "docker-compose.yml.bak").write_text("services: {}\n", encoding="utf-8")
        self._rollback_refused_untouched("backup_manifest_mismatch")

    # S-9 on #241: a deploy that fails mid-copy must not leave a backup whose
    # rollback replaces live files with partial copies.
    def _live_snapshot(self):
        out = {}
        for dp, dns, fns in os.walk(self.stack_dir):
            dns[:] = [d for d in dns if d != "_backups" and not d.startswith(".rollback-stage")]
            for fn in fns:
                p = os.path.join(dp, fn)
                out[os.path.relpath(p, self.stack_dir)] = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        return out

    def _cp_shim_truncating(self, dst_glob, nbytes):
        """A `cp` that writes only <nbytes> of the source to a backup
        destination matching <dst_glob> and fails, like a full disk."""
        shim = Path(self.fakebin, "cp")
        shim.write_text('#!/usr/bin/env bash\nlast="${@: -1}"\n'
                        f'case "$last" in {dst_glob}) head -c {nbytes} "${{@: -2:1}}" > "$last"; exit 1;; esac\n'
                        'exec /bin/cp "$@"\n', encoding="utf-8", newline="\n")
        os.chmod(shim, 0o755)
        return shim

    def _incomplete_rollback_leaves_live_state(self, prepare, cleanup):
        prepare()
        try:
            res = self.run_script(env=self.env())
        finally:
            cleanup()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("the backup is incomplete", res.stderr)
        before = self._live_snapshot()  # the failed deploy changed nothing live
        rb = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(rb.returncode, 0, rb.stderr)
        self.assertIn("INCOMPLETE BACKUP", rb.stderr)
        self.assertIn("are NOT restored", rb.stderr)
        self.assertEqual(self.webmgr_container()["Image"], "sha256:oldimageid")
        after = self._live_snapshot()
        self.assertEqual(sorted(set(before) - set(after)), [], "a live file was moved away")
        self.assertEqual(sorted(k for k in before if before[k] != after.get(k)), [],
                         "a live file was replaced from the incomplete backup")
        self.assertEqual(list(Path(self.backup_dir()).rglob("*.part")), [])
        return rb

    def test_truncated_state_file_copy_is_not_restored(self):
        Path(self.webmgr_dir, ".flask_secret_key").write_text("flask-key-before-0123456789", encoding="utf-8")
        shim = self._cp_shim_truncating("*_backups*/web-manager-data/.flask_secret_key*", 3)
        self._incomplete_rollback_leaves_live_state(lambda: None, shim.unlink)
        self.assertFalse(Path(self.backup_dir(), "web-manager-data", ".flask_secret_key").exists(),
                         "a partial copy kept its final name")

    def test_truncated_beets_config_copy_is_not_restored(self):
        config = "directory: /music\nplugins: web webmanager\n"
        Path(self.engine_dir, "config.yaml").write_text(config, encoding="utf-8")
        shim = self._cp_shim_truncating("*_backups*/beets-config/config.yaml*", 5)
        self._incomplete_rollback_leaves_live_state(lambda: None, shim.unlink)
        self.assertFalse(Path(self.backup_dir(), "beets-config", "config.yaml").exists())
        self.assertEqual(Path(self.engine_dir, "config.yaml").read_text(encoding="utf-8"), config)

    def test_partial_beetsplug_copy_is_not_restored(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-0 file")
        extra = Path(self.plugin_dir, "zz_extra.py")
        extra.write_text("X = 1\n", encoding="utf-8")
        self._incomplete_rollback_leaves_live_state(lambda: os.chmod(extra, 0), lambda: os.chmod(extra, 0o600))
        self.assertFalse(Path(self.backup_dir(), "beets-config", "beetsplug").exists())
        self.assertEqual(extra.read_text(encoding="utf-8"), "X = 1\n")

    def test_a_tampered_incomplete_backup_state_file_is_refused(self):
        self._fail_deploy_inside_state_backup()
        Path(self.backup_dir(), "web-manager-data", ".env").write_text("AI_MODEL=tampered\n", encoding="utf-8")
        self._rollback_refused_untouched("backup_manifest_mismatch")
        self.assertEqual(Path(self.webmgr_dir, ".env").read_text(encoding="utf-8"), "AI_MODEL=before-deploy\n")

    def test_image_labels_record_must_be_listed(self):
        self.deploy()
        manifest = Path(self.backup_dir(), "state-manifest.txt")
        self.assertTrue(Path(self.backup_dir(), "previous-image-labels.json").exists())
        manifest.write_text("".join(ln for ln in manifest.read_text(encoding="utf-8").splitlines(True)
                                    if not ln.startswith("previous-image-labels.json ")), encoding="utf-8")
        res = self._rollback_refused_untouched("backup_manifest_mismatch")
        self.assertIn("previous-image-labels.json (not in the manifest)", res.stderr)

    def test_deploy_with_stale_db_token_migration_and_plugin_restart_rolls_back(self):
        # From QA on #241: every later backup write is recorded too.
        make_sqlite_db(os.path.join(self.webmgr_dir, "musiclibrary.blb"), items=2, albums=1)
        legacy = os.path.join(self.tmp, "legacy-config")
        os.makedirs(legacy)
        os.remove(os.path.join(self.webmgr_dir, ".auth_token"))
        Path(legacy, ".auth_token").write_text("legacy-tok", encoding="utf-8")
        st = self.load_state()
        st["containers"]["cid-webmgr"]["Mounts"].append({"Destination": "/config", "Source": legacy})
        self.save_state(st)
        self.set_provisioned_plugin("1.3.0")  # engine reports 1.2.0 -> plugin restart path
        res = self.run_script(env=self.env())
        bd = self.backup_dir()
        text = Path(bd, "state-manifest.txt").read_text(encoding="utf-8")
        self.assertIn("stale-database/musiclibrary.blb sha256=", text, res.stderr)
        self.assertIn("migrated_token_sha256", Path(bd, "token-metadata.txt").read_text(encoding="utf-8"))
        rb = self.run_script("--rollback", bd, env=self.env(RESTORE_STALE_DB=1))
        self.assertNotIn("backup_manifest", rb.stdout + rb.stderr, rb.stderr)
        self.assertIn("Verified", rb.stderr)

    def test_second_rollback_from_the_same_backup_still_verifies(self):
        self.deploy()
        first = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("backup_manifest", second.stdout + second.stderr)

    def test_allow_legacy_backup_does_not_skip_verification_of_a_manifest(self):
        self.deploy()
        Path(self.backup_dir(), "docker-compose.yml.bak").write_text("services: {}\n", encoding="utf-8")
        self._rollback_refused_untouched("backup_manifest_mismatch", "--allow-legacy-backup")


class LatestTagStackFixture(EndToEndFixture):
    """The shipped layout: the Compose file uses the literal
    `ghcr.io/iranman/beets-web-manager:latest`, and the .env has no version
    line. The stack currently runs 0.1.2 under :latest; the registry's
    :latest is 0.1.3 (VERSION) unless a test moves it."""

    LATEST = "ghcr.io/iranman/beets-web-manager:latest"
    REPO = "ghcr.io/iranman/beets-web-manager"
    OLD_DIGEST = REPO + "@sha256:" + "1" * 64
    NEW_DIGEST = REPO + "@sha256:" + "3" * 64
    OLD_REVISION = "1111111111111111111111111111111111111111"

    def old_entry(self):
        return {"Id": "sha256:oldimageid", "RepoDigests": [self.OLD_DIGEST],
                "Config": {"Labels": {"org.opencontainers.image.version": "0.1.2",
                                      "org.opencontainers.image.revision": self.OLD_REVISION}}}

    def new_entry(self, version="0.1.3", revision=None):
        return {"Id": "sha256:goodimageid", "RepoDigests": [self.NEW_DIGEST],
                "Config": {"Labels": {"org.opencontainers.image.version": version,
                                      "org.opencontainers.image.revision": revision or self.GOOD_REVISION}}}

    def setUp(self):
        super().setUp()
        Path(self.compose_file).write_text(
            "services:\n  beets:\n    image: beets-engine:local\n"
            f"  beets-web-manager:\n    image: {self.LATEST}\n"
            "  lidarr:\n    image: lidarr:local\n",
            encoding="utf-8",
        )
        self.stack_env = os.path.join(self.stack_dir, ".env")
        Path(self.stack_env).write_text("OTHER_SETTING=keep-me\n", encoding="utf-8")
        Path(self.webmgr_dir, ".auth_token").write_text("tok-not-printed", encoding="utf-8")
        self.state["compose_literal_images"] = True
        self.state["images"] = {self.LATEST: self.old_entry()}
        self.state["registry"] = {self.LATEST: self.new_entry()}
        webmgr = self.state["containers"]["cid-webmgr"]
        webmgr["Config"]["Image"] = self.LATEST
        webmgr["Image"] = "sha256:oldimageid"
        self._save_state()
        self.compose_before = Path(self.compose_file).read_bytes()
        self.env_before = Path(self.stack_env).read_bytes()

    def load_state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, st):
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def webmgr(self):
        st = self.load_state()
        return st["containers"][st["service_containers"]["beets-web-manager"]]

    def backup_dirs(self):
        root = os.path.join(self.stack_dir, "_backups")
        return [os.path.join(root, n) for n in os.listdir(root)] if os.path.isdir(root) else []

    def assert_compose_and_env_untouched(self):
        self.assertEqual(Path(self.compose_file).read_bytes(), self.compose_before, "the Compose file must not be edited")
        self.assertEqual(Path(self.stack_env).read_bytes(), self.env_before, "the stack .env must not be edited")


class LatestTagDeployTests(LatestTagStackFixture):
    def test_deploy_pulls_latest_verifies_its_version_and_edits_nothing(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        cont = self.webmgr()
        self.assertEqual(cont["Config"]["Image"], self.LATEST)
        self.assertEqual(cont["Image"], "sha256:goodimageid")
        self.assertIn("layout: latest", res.stderr)
        self.assertIn("Image labels verified: ghcr.io/iranman/beets-web-manager:latest version=0.1.3", res.stderr)
        self.assert_compose_and_env_untouched()
        (bdir,) = self.backup_dirs()
        record = Path(bdir, "previous-image.txt").read_text(encoding="utf-8")
        self.assertIn("previous_image_id=sha256:oldimageid", record)
        self.assertIn(f"previous_image_ref={self.LATEST}", record)
        self.assertIn(f"previous_image_repo_digest={self.OLD_DIGEST}", record)

    def test_deploy_refuses_when_latest_does_not_carry_version_yet(self):
        st = self.load_state()
        st["registry"][self.LATEST] = self.old_entry()  # 0.1.3 not published as latest yet
        self.save_state(st)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           latest_image_not_requested_version", res.stderr)
        self.assertIn("carries version '0.1.2', not '0.1.3'", res.stderr)
        self.assertIn("Failed stage:          image-pull-verification", res.stderr)
        cont = self.webmgr()
        self.assertEqual(cont["Image"], "sha256:oldimageid", "production must not change")
        self.assertEqual(cont["State"]["Status"], "running", "the web manager must not be stopped")
        self.assertEqual(self.backup_dirs(), [], "refused before any mutating step")
        self.assert_compose_and_env_untouched()

    def test_deploy_refuses_when_latest_is_newer_than_version(self):
        st = self.load_state()
        st["registry"][self.LATEST] = self.new_entry(version="0.1.4")
        self.save_state(st)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           latest_image_not_requested_version", res.stderr)
        self.assertEqual(self.webmgr()["Image"], "sha256:oldimageid")

    def test_deploy_refuses_a_latest_with_the_wrong_revision(self):
        st = self.load_state()
        st["registry"][self.LATEST] = self.new_entry(revision="0" * 40)
        self.save_state(st)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           image_revision_label_mismatch", res.stderr)
        self.assertEqual(self.webmgr()["Image"], "sha256:oldimageid")

    def test_dry_run_refuses_when_latest_does_not_carry_version(self):
        st = self.load_state()
        st["registry"][self.LATEST] = self.old_entry()
        self.save_state(st)
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           latest_image_not_requested_version", res.stderr)

    def test_compose_pinned_to_another_version_is_refused_not_edited(self):
        Path(self.compose_file).write_text(
            Path(self.compose_file).read_text(encoding="utf-8").replace(":latest", ":0.1.1"), encoding="utf-8")
        before = Path(self.compose_file).read_bytes()
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           compose_image_mismatch", res.stderr)
        self.assertEqual(Path(self.compose_file).read_bytes(), before)

    def test_a_comment_naming_the_version_variable_does_not_make_a_pin_a_variable_layout(self):
        pinned = self.REPO + ":0.1.3"
        Path(self.compose_file).write_text(
            "# Pin by setting BEETS_WEB_MANAGER_VERSION? No: edit the tag below.\n"
            + Path(self.compose_file).read_text(encoding="utf-8").replace(":latest", ":0.1.3"), encoding="utf-8")
        st = self.load_state()
        st["registry"][pinned] = self.new_entry()
        self.save_state(st)
        res = self.run_script("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("layout: pinned", res.stderr)

    # --- NB1: `up --pull never` needs Docker Compose v2.22+ ---------------

    def test_deploy_on_a_compose_without_up_pull_is_refused_before_any_change(self):
        st = self.load_state()
        st["compose_no_pull_flag"] = True
        self.save_state(st)
        for args in ((), ("--dry-run",)):
            res = self.run_script(*args)
            self.assertNotEqual(res.returncode, 0, args)
            self.assertIn("Reason code:           compose_too_old", res.stderr)
            self.assertIn("Failed stage:          compose-version-check", res.stderr)
        st = self.load_state()
        cont = self.webmgr()
        self.assertEqual((cont["Image"], cont["State"]["Status"]), ("sha256:oldimageid", "running"))
        self.assertEqual(st["images"][self.LATEST]["Id"], "sha256:oldimageid", "nothing pulled")
        self.assertNotIn("pulled", st)
        self.assertNotIn("tagged", st)
        self.assertNotIn("up_args", st)
        self.assertEqual(self.backup_dirs(), [])
        self.assert_compose_and_env_untouched()

    # --- N1: a pulled but undeployed :latest must not stay the local tag ---

    def test_refused_deploy_points_local_latest_back_at_the_previous_image(self):
        st = self.load_state()
        st["registry"][self.LATEST] = self.new_entry(version="0.1.4")
        self.save_state(st)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           latest_image_not_requested_version", res.stderr)
        self.assertEqual(self.load_state()["images"][self.LATEST]["Id"], "sha256:oldimageid",
                         "a later plain 'docker compose up -d' must not start the refused image")

    def test_dry_run_points_local_latest_back_at_the_previous_image(self):
        res = self.run_script("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.load_state()["images"][self.LATEST]["Id"], "sha256:oldimageid")
        self.assertIn("Re-tagged ghcr.io/iranman/beets-web-manager:latest back to sha256:oldimageid", res.stderr)

    # --- S3: `up` must not pull a tag that moved after verification -------

    def evil_entry(self):
        return {"Id": "sha256:evilimageid", "RepoDigests": [],
                "Config": {"Labels": {"org.opencontainers.image.version": "6.6.6",
                                      "org.opencontainers.image.revision": "e" * 40}}}

    def test_recreate_never_pulls_a_tag_that_moved_at_up_time(self):
        st = self.load_state()
        st["registry_at_up"] = {self.LATEST: self.evil_entry()}  # pull_policy: always / concurrent pull
        self.save_state(st)
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.webmgr()["Image"], "sha256:goodimageid")
        st = self.load_state()
        self.assertTrue(all("--pull" in a and a[a.index("--pull") + 1] == "never" for a in st["up_args"]), st["up_args"])

    def test_an_unverified_image_after_recreate_is_stopped_not_left_running(self):
        st = self.load_state()
        st["registry_at_up"] = {self.LATEST: self.evil_entry()}
        st["up_ignores_pull_never"] = True
        self.save_state(st)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           recreated_image_unverified", res.stderr)
        cont = self.webmgr()
        self.assertEqual(cont["Image"], "sha256:evilimageid")
        self.assertEqual(cont["State"]["Status"], "exited", "the unverified image must not keep running")


class LatestTagRollbackTests(LatestTagStackFixture):
    def deploy(self):
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        (bdir,) = self.backup_dirs()
        return bdir

    def test_rollback_retags_latest_to_the_previous_image(self):
        bdir = self.deploy()
        res = self.run_script("--rollback", bdir)
        self.assertEqual(res.returncode, 0, res.stderr)
        cont = self.webmgr()
        self.assertEqual(cont["Config"]["Image"], self.LATEST)
        self.assertEqual(cont["Image"], "sha256:oldimageid")
        st = self.load_state()
        self.assertEqual(st["images"][self.LATEST]["Id"], "sha256:oldimageid",
                         "a later plain 'docker compose up -d' must keep the previous image")
        self.assertIn(["sha256:oldimageid", self.LATEST], st["tagged"])
        self.assertIn("rollback is durable", res.stderr)
        self.assertIn("/health/live reports version 0.1.2", res.stderr)
        self.assert_compose_and_env_untouched()

    def test_migration_from_pinned_container_to_literal_latest_then_rollback(self):
        # B1: the old container was created from :0.1.2 (pinned layout); the
        # operator then switched the Compose file to the shipped :latest.
        st = self.load_state()
        pinned = self.REPO + ":0.1.2"
        st["images"] = {pinned: self.old_entry()}
        st["containers"]["cid-webmgr"]["Config"]["Image"] = pinned
        self.save_state(st)
        bdir = self.deploy()
        res = self.run_script("--rollback", bdir)
        self.assertEqual(res.returncode, 0, res.stderr)
        st = self.load_state()
        self.assertEqual(st["images"][self.LATEST]["Id"], "sha256:oldimageid",
                         "a later plain 'docker compose up -d' must stay on the previous image")
        self.assertEqual(self.webmgr()["Image"], "sha256:oldimageid")
        self.assertIn("rollback is durable", res.stderr)
        self.assertIn("/health/live reports version 0.1.2", res.stderr)
        self.assertIn("running plugin is", res.stderr)  # refresh_engine_plugin_if_stale ran
        self.assert_compose_and_env_untouched()

    def test_failed_rollback_does_not_claim_nothing_to_roll_back(self):
        bdir = self.deploy()
        st = self.load_state()
        st["up_should_fail"] = True
        self.save_state(st)
        res = self.run_script("--rollback", bdir)
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("nothing to roll back", res.stderr)
        self.assertIn("the rollback itself failed", res.stderr)

    def test_rollback_on_a_compose_without_up_pull_fails_before_stopping_anything(self):
        bdir = self.deploy()
        st = self.load_state()
        st["compose_no_pull_flag"] = True
        st.pop("tagged", None)
        st.pop("up_args", None)
        self.save_state(st)
        res = self.run_script("--rollback", bdir)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           compose_too_old", res.stderr)
        st = self.load_state()
        cont = self.webmgr()
        self.assertEqual((cont["Image"], cont["State"]["Status"]), ("sha256:goodimageid", "running"),
                         "the service must not be stopped by a rollback that cannot recreate it")
        self.assertNotIn("tagged", st)
        self.assertNotIn("up_args", st)
        self.assert_compose_and_env_untouched()

    def test_rollback_pulls_a_pruned_previous_image_back_by_digest(self):
        bdir = self.deploy()
        st = self.load_state()
        del st["images"]["sha256:oldimageid"]  # `docker image prune` removed the dangling image
        st["registry"][self.OLD_DIGEST] = self.old_entry()
        self.save_state(st)
        res = self.run_script("--rollback", bdir)
        self.assertEqual(res.returncode, 0, res.stderr)
        st = self.load_state()
        self.assertEqual(st["pulled_by_digest"], [self.OLD_DIGEST])
        self.assertEqual(self.webmgr()["Image"], "sha256:oldimageid")
        self.assert_compose_and_env_untouched()

    def test_rollback_refuses_when_the_digest_pull_returns_another_image(self):
        bdir = self.deploy()
        st = self.load_state()
        del st["images"]["sha256:oldimageid"]
        st["registry"][self.OLD_DIGEST] = self.new_entry()
        self.save_state(st)
        res = self.run_script("--rollback", bdir)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("is not the recorded previous image sha256:oldimageid", res.stderr)
        self.assertNotIn("Rollback complete", res.stderr)


class BackupContentTests(VersionedStackFixture):
    def test_backup_holds_state_and_beets_config_but_never_the_library_db(self):
        self.deploy()
        bdir = self.backup_dir()
        for rel in ("web-manager-data/.env", "web-manager-data/.browser_username",
                    "web-manager-data/.browser_password", "web-manager-data/.flask_secret_key",
                    "web-manager-data/.setup_complete", "web-manager-data/transactions/t1.json",
                    "beets-config/config.yaml", "beets-config/beetsplug/webmanager/version.py",
                    "state-manifest.txt", ".env.bak"):
            self.assertTrue(os.path.isfile(os.path.join(bdir, rel)), rel)
        for root, _dirs, files in os.walk(bdir):
            for name in files:
                self.assertNotIn("musiclibrary.blb", name, os.path.join(root, name))
                if os.name != "nt":
                    mode = stat.S_IMODE(os.stat(os.path.join(root, name)).st_mode)
                    self.assertEqual(mode & 0o077, 0, f"{name} is group/world accessible: {oct(mode)}")

    def test_backup_redacts_environment_values_but_keeps_key_names(self):
        self.deploy()
        bdir = self.backup_dir()
        inspect_text = Path(bdir, "container-inspect-before.json").read_text(encoding="utf-8")
        compose_text = Path(bdir, "resolved-compose-config.json").read_text(encoding="utf-8")
        for secret in ("plex-secret-value-xyz", "acoustid-secret-value-xyz",
                       "lidarr-secret-value-xyz", "lidarr-own-secret-xyz"):
            self.assertNotIn(secret, inspect_text)
            self.assertNotIn(secret, compose_text)
        self.assertIn("PLEX_TOKEN=<redacted>", inspect_text)
        self.assertIn("TZ=UTC", inspect_text)
        compose = json.loads(compose_text)
        self.assertEqual(compose["services"]["beets-web-manager"]["environment"]["LIDARR_API_KEY"], "<redacted>")
        self.assertEqual(compose["services"]["beets-web-manager"]["environment"]["TZ"], "UTC")
        self.assertEqual(compose["services"]["lidarr"]["environment"]["API_KEY"], "<redacted>")


    def test_backup_scrubs_secrets_from_commands_labels_and_extensions(self):
        webmgr = self.state["containers"]["cid-webmgr"]
        webmgr["Config"]["Env"].append("BEETS_WEB_URL=http://admin:url-pass-secret-xyz@beets:8337")
        webmgr["Config"]["Cmd"] = ["serve", "--api-token=cmd-secret-value-xyz", "--port=8000"]
        webmgr["Config"]["Entrypoint"] = ["/init", "PASSWORD=entry-secret-value-xyz"]
        webmgr["Config"]["Labels"] = {"plain": "ok", "app.api_key": "label-secret-value-xyz",
                                      "note": "SECRET=label-inline-secret-xyz"}
        webmgr["Config"]["Healthcheck"] = {"Test": ["CMD", "curl", "-H", "token=hc-secret-value-xyz"]}
        self.state["compose_service_extra"] = {"beets-web-manager": {
            "command": "serve --token=compose-cmd-secret-xyz ok=1",
            "entrypoint": ["/init", "pass=compose-entry-secret-xyz"],
            "healthcheck": {"test": ["CMD-SHELL", "curl -u x KEY=compose-hc-secret-xyz"]},
            "labels": {"traefik.password": "compose-label-secret-xyz", "plain": "ok"},
            "build": {"context": ".", "args": {"NPM_TOKEN": "compose-build-secret-xyz", "V": "1"}},
            "x-notes": {"db_password": "compose-xsvc-secret-xyz"},
        }}
        self.state["compose_environment"]["beets-web-manager"]["BEETS_OUTBOUND_ALLOWLIST"] = (
            "https://user:allow-pass-secret-xyz@h.example")
        self.state["compose_top_extra"] = {"x-shared": {"command": "run SECRET_KEY=compose-xtop-secret-xyz"}}
        self._save_state()
        self.deploy()
        bdir = self.backup_dir()
        inspect_text = Path(bdir, "container-inspect-before.json").read_text(encoding="utf-8")
        compose_text = Path(bdir, "resolved-compose-config.json").read_text(encoding="utf-8")
        for secret in ("url-pass-secret-xyz", "cmd-secret-value-xyz", "entry-secret-value-xyz",
                       "label-secret-value-xyz", "label-inline-secret-xyz", "hc-secret-value-xyz"):
            self.assertNotIn(secret, inspect_text)
        for secret in ("compose-cmd-secret-xyz", "compose-entry-secret-xyz", "compose-hc-secret-xyz",
                       "compose-label-secret-xyz", "compose-build-secret-xyz", "compose-xsvc-secret-xyz",
                       "allow-pass-secret-xyz", "compose-xtop-secret-xyz"):
            self.assertNotIn(secret, compose_text)
        inspect = json.loads(inspect_text)[0]
        self.assertIn("BEETS_WEB_URL=http://<redacted>@beets:8337", inspect["Config"]["Env"])
        self.assertIn("--port=8000", inspect["Config"]["Cmd"])
        self.assertEqual(inspect["Config"]["Labels"]["plain"], "ok")
        svc = json.loads(compose_text)["services"]["beets-web-manager"]
        self.assertEqual(svc["environment"]["BEETS_OUTBOUND_ALLOWLIST"], "https://<redacted>@h.example")
        self.assertIn("ok=1", svc["command"])
        self.assertEqual(svc["build"]["args"]["V"], "1")
        self.assertEqual(svc["labels"]["plain"], "ok")


@unittest.skipIf(os.name == "nt", "symbolic links need a POSIX host")
class RestoreBeetsplugGuardTests(RolloutScriptTestBase):
    """restore_state_files clears beetsplug/ before copying the backup in.
    If beetsplug/ is a link at that moment (swapped after the symlink
    check), the rollback must stop rather than write through it."""

    def test_rollback_refuses_to_clear_a_beetsplug_that_is_a_link(self):
        engine = os.path.join(self.tmp, "engine")
        data = os.path.join(self.tmp, "data")
        rb = os.path.join(self.tmp, "rollback")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        for d in (engine, data, elsewhere, os.path.join(rb, "beets-config", "beetsplug", "webmanager")):
            os.makedirs(d, exist_ok=True)
        Path(rb, "beets-config", "beetsplug", "webmanager", "version.py").write_text("v = 1\n", encoding="utf-8")
        Path(elsewhere, "keep.txt").write_text("untouched", encoding="utf-8")
        os.symlink(elsewhere, os.path.join(engine, "beetsplug"))
        res = self.run_snippet(
            f'ENGINE_CONFIG_SRC="{engine}"; WEBMGR_DATA_SRC="{data}"; ROLLBACK_DIR="{rb}"\n'
            "tree_has_symlink() { return 1; }  # simulate the link appearing after the check\n"
            "restore_state_files"
        )
        self.assertNotEqual(res.returncode, 0, res.stderr)
        self.assertIn("beetsplug", res.stderr)
        self.assertEqual(sorted(os.listdir(elsewhere)), ["keep.txt"],
                         "nothing may be deleted or written through the link")


@unittest.skipIf(os.name == "nt", "symbolic links need a POSIX host")
class RollbackCopyRaceTests(RolloutScriptTestBase):
    """#177: a path that passed the link check can be swapped for a link
    before the copy runs. The rollback copies into a private staging folder
    and renames into place, so a link planted in that window is replaced,
    never written through. `cp` is wrapped to plant the link right before
    every copy -- the worst-case timing."""

    def _victim(self):
        victim = os.path.join(self.tmp, "host-file")
        Path(victim).write_text("host-only-content", encoding="utf-8")
        return victim

    def test_copy_regular_file_does_not_write_through_a_link_planted_before_the_copy(self):
        victim = self._victim()
        src = os.path.join(self.tmp, "src.txt")
        Path(src).write_text("restored", encoding="utf-8")
        dst = os.path.join(self.tmp, "data", "settings")
        os.makedirs(os.path.dirname(dst))
        Path(dst).write_text("current", encoding="utf-8")
        res = self.run_snippet(
            f'cp() {{ command rm -f -- "{dst}"; command ln -s "{victim}" "{dst}"; command cp "$@"; }}\n'
            f'copy_regular_file "{src}" "{dst}" 600'
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "host-only-content")
        self.assertFalse(os.path.islink(dst))
        self.assertEqual(Path(dst).read_text(encoding="utf-8"), "restored")
        self.assertEqual(os.stat(dst).st_mode & 0o777, 0o600)
        self.assertEqual([n for n in os.listdir(os.path.dirname(dst)) if n.startswith(".rollback-stage.")], [])

    def test_copy_regular_file_refuses_a_source_swapped_for_a_link(self):
        victim = self._victim()
        src = os.path.join(self.tmp, "src.txt")
        Path(src).write_text("restored", encoding="utf-8")
        dst = os.path.join(self.tmp, "dst.txt")
        res = self.run_snippet(
            f'cp() {{ command rm -f -- "{src}"; command ln -s "{victim}" "{src}"; command cp "$@"; }}\n'
            f'if copy_regular_file "{src}" "{dst}"; then echo COPIED; else echo REFUSED; fi'
        )
        self.assertIn("REFUSED", res.stdout, res.stderr)
        self.assertFalse(os.path.lexists(dst), "nothing may be placed from a swapped source")

    def test_beetsplug_restore_does_not_write_through_a_link_planted_inside_it(self):
        victim = self._victim()
        engine = os.path.join(self.tmp, "engine")
        data = os.path.join(self.tmp, "data")
        rb = os.path.join(self.tmp, "rollback")
        os.makedirs(os.path.join(engine, "beetsplug", "webmanager"))
        os.makedirs(data)
        os.makedirs(os.path.join(rb, "beets-config", "beetsplug", "webmanager"))
        Path(rb, "beets-config", "beetsplug", "webmanager", "version.py").write_text("v = 1\n", encoding="utf-8")
        Path(engine, "beetsplug", "webmanager", "version.py").write_text("v = 2\n", encoding="utf-8")
        planted = os.path.join(engine, "beetsplug", "webmanager", "version.py")
        res = self.run_snippet(
            f'ENGINE_CONFIG_SRC="{engine}"; WEBMGR_DATA_SRC="{data}"; ROLLBACK_DIR="{rb}"\n'
            f'cp() {{ command mkdir -p "{os.path.dirname(planted)}"; command rm -f -- "{planted}"; '
            f'command ln -s "{victim}" "{planted}"; command cp "$@"; }}\n'
            "restore_state_files"
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "host-only-content",
                         "the rollback wrote through a link planted inside beetsplug/")
        self.assertFalse(os.path.islink(planted))
        self.assertEqual(Path(planted).read_text(encoding="utf-8"), "v = 1\n")
        self.assertEqual([n for n in os.listdir(engine) if n.startswith(".rollback-stage.")], [])


    def _stage_swap(self, victim_target):
        """A `mktemp` wrapper that does what a racer owning the target folder
        can do the moment a staging folder exists: rename it away and plant a
        link to an attacker folder whose entries link to a host path."""
        atk = os.path.join(self.tmp, "atk")
        os.makedirs(atk, exist_ok=True)
        for name in ("item", "new"):
            os.symlink(victim_target, os.path.join(atk, name))
        return (f'mktemp() {{ local d; d="$(command mktemp "$@")" || return 1; '
                f'case "$d" in */.rollback-stage.*) command mv "$d" "$d.away"; command ln -s "{atk}" "$d";; esac; '
                f'printf "%s\\n" "$d"; }}\n')

    def test_copy_regular_file_refuses_a_staging_folder_swapped_for_a_link(self):
        victim = self._victim()
        src = os.path.join(self.tmp, "token.bak")
        Path(src).write_text("pre-rollout-token", encoding="utf-8")
        dst = os.path.join(self.tmp, "data", "auth_token")
        os.makedirs(os.path.dirname(dst))
        Path(dst).write_text("current", encoding="utf-8")
        res = self.run_snippet(self._stage_swap(victim)
                               + f'if copy_regular_file "{src}" "{dst}" 600; then echo COPIED; else echo REFUSED; fi')
        self.assertIn("REFUSED", res.stdout, res.stderr)
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "host-only-content",
                         "the copy was redirected through a swapped staging folder")
        self.assertEqual(Path(dst).read_text(encoding="utf-8"), "current")

    def test_transactions_restore_never_follows_a_folder_planted_after_the_link_check(self):
        # R1: a racer plants transactions/zz -> <host dir> once the one-time
        # tree_has_symlink check has passed; a backed-up record under
        # transactions/zz/etc/ must not be written through it.
        victim = os.path.join(self.tmp, "host-root")
        os.makedirs(os.path.join(victim, "etc"))
        engine = os.path.join(self.tmp, "engine")
        data = os.path.join(self.tmp, "data")
        rb = os.path.join(self.tmp, "rollback")
        os.makedirs(engine)
        os.makedirs(os.path.join(data, "transactions"))
        os.makedirs(os.path.join(rb, "web-manager-data", "transactions", "zz", "etc"))
        Path(rb, "web-manager-data", "transactions", "txn_a.json").write_text("{}", encoding="utf-8")
        Path(rb, "web-manager-data", "transactions", "zz", "etc", "ld.so.preload").write_text("/evil.so\n", encoding="utf-8")
        planted = os.path.join(data, "transactions", "zz")
        res = self.run_snippet(
            f'ENGINE_CONFIG_SRC="{engine}"; WEBMGR_DATA_SRC="{data}"; ROLLBACK_DIR="{rb}"\n'
            f'cp() {{ [[ -L "{planted}" ]] || command ln -s "{victim}" "{planted}"; command cp "$@"; }}\n'
            "restore_state_files"
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.listdir(os.path.join(victim, "etc")), [],
                         "the rollback wrote through a folder planted inside transactions/")
        self.assertEqual(Path(data, "transactions", "txn_a.json").read_text(encoding="utf-8"), "{}")

    def test_beetsplug_restore_refuses_a_staging_folder_swapped_for_a_link(self):
        victim_dir = os.path.join(self.tmp, "host-dir")
        os.makedirs(victim_dir)
        engine = os.path.join(self.tmp, "engine")
        data = os.path.join(self.tmp, "data")
        rb = os.path.join(self.tmp, "rollback")
        os.makedirs(os.path.join(engine, "beetsplug", "webmanager"))
        os.makedirs(data)
        os.makedirs(os.path.join(rb, "beets-config", "beetsplug", "webmanager"))
        Path(rb, "beets-config", "beetsplug", "webmanager", "version.py").write_text("v = 1\n", encoding="utf-8")
        Path(engine, "beetsplug", "webmanager", "version.py").write_text("v = 2\n", encoding="utf-8")
        res = self.run_snippet(self._stage_swap(victim_dir)
                               + f'ENGINE_CONFIG_SRC="{engine}"; WEBMGR_DATA_SRC="{data}"; ROLLBACK_DIR="{rb}"\n'
                               "restore_state_files")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("was replaced while the rollback ran", res.stderr)
        self.assertEqual(os.listdir(victim_dir), [], "the rollback wrote into a swapped staging folder")
        self.assertEqual(Path(engine, "beetsplug", "webmanager", "version.py").read_text(encoding="utf-8"), "v = 2\n")

    def test_transactions_folder_flipped_to_a_link_gets_no_staging_folder(self):
        # #221 R2: a racer flips transactions/ itself to a link to another
        # folder just while the staging folder is made, then flips it back.
        # No staging folder may be made, or left, in the link's target.
        victim_dir = os.path.join(self.tmp, "host-dir")
        os.makedirs(victim_dir)
        txn = os.path.join(self.tmp, "data", "transactions")
        os.makedirs(txn)
        src = os.path.join(self.tmp, "txn_a.json")
        Path(src).write_text("{}", encoding="utf-8")
        res = self.run_snippet(
            f'mktemp() {{ case "$*" in *.rollback-stage.*) ;; *) command mktemp "$@"; return;; esac\n'
            f'  local d rc=0; command mv "{txn}" "{txn}.away"; command ln -s "{victim_dir}" "{txn}"\n'
            f'  d="$(command mktemp "$@")" || rc=$?\n'
            f'  command rm "{txn}"; command mv "{txn}.away" "{txn}"\n'
            f'  [[ "$rc" -eq 0 ]] && printf "%s\\n" "$d"; return "$rc"; }}\n'
            f'if copy_regular_file "{src}" "{txn}/txn_a.json"; then echo COPIED; else echo REFUSED; fi'
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.listdir(victim_dir), [], "a staging folder was made or left through the flipped link")
        self.assertEqual([n for n in os.listdir(txn) if n.startswith(".rollback-stage.")], [])
        if "COPIED" in res.stdout:
            self.assertEqual(Path(txn, "txn_a.json").read_text(encoding="utf-8"), "{}")


class AuthTokenNotOnArgvTests(RolloutScriptTestBase):
    """#221 S6: the Web Manager token must never be on curl's command line
    (any local user can read it in the process list). It goes in a 0600
    header file that is gone once curl returns."""

    TOKEN = "tok-SECRET-0123456789abcdef"

    def _run(self, call, content=None, symlink=False, expect_rc=0):
        token = os.path.join(self.tmp, "auth_token")
        target = os.path.join(self.tmp, "real_token") if symlink else token
        Path(target).write_text(self.TOKEN + "\n" if content is None else content, encoding="utf-8")
        if symlink:
            os.symlink(target, token)
        log = os.path.join(self.tmp, "curl.log")
        Path(log).touch()
        # A curl stand-in that records its argv and, for -H @file, the file's
        # mode and content while curl "runs".
        res = self.run_snippet(
            f'ACTIVE_AUTH_TOKEN_PATH="{token}"\n'
            f'curl() {{ printf "ARGV %s\\n" "$*" >> "{log}"; local a; for a in "$@"; do case "$a" in '
            f'@*) printf "HDR %s %s %s\\n" "${{a#@}}" "$(stat -c %a "${{a#@}}")" "$(cat "${{a#@}}")" >> "{log}";; esac; done; '
            f'echo 200; }}\n'
            + call
        )
        self.assertEqual(res.returncode, expect_rc, res.stderr)
        self.stderr = res.stderr
        return Path(log).read_text(encoding="utf-8").splitlines()

    def _assert_no_token_sent(self, lines):
        self.assertEqual([ln for ln in lines if ln.startswith("HDR ")], [])
        self.assertFalse(any(self.TOKEN in ln for ln in lines))
        self.assertNotIn(self.TOKEN, self.stderr)

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_symlinked_token_is_not_sent(self):
        lines = self._run("fetch_setup_status >/dev/null", symlink=True)
        self._assert_no_token_sent(lines)
        self.assertIn("sending no Authorization header", self.stderr)

    def test_multi_line_token_is_not_sent(self):
        # An LF inside the token would add a header of the caller's choosing.
        lines = self._run("fetch_setup_status >/dev/null",
                          content=self.TOKEN + "\nX-Injected: yes\n")
        self._assert_no_token_sent(lines)
        self.assertFalse(any("X-Injected" in ln for ln in lines))
        self.assertIn("sending no Authorization header", self.stderr)

    def test_short_token_is_not_sent(self):
        lines = self._run("fetch_setup_status >/dev/null", content="short\n")
        self.assertEqual([ln for ln in lines if ln.startswith("HDR ")], [])

    def test_redirect_following_is_refused(self):
        lines = self._run("rc=0; curl_auth -sSL http://127.0.0.1:9/x || rc=$?; exit $rc", expect_rc=2)
        self.assertEqual(lines, [], "curl must not run when asked to follow redirects")
        self.assertIn("refuses to follow redirects", self.stderr)

    def test_every_redirect_option_form_is_refused(self):
        # S-10 on #241: a bare -L and clusters such as -Ls used to get through.
        for opt in ("-L", "-Ls", "-sL", "-fL", "-sSfL", "--location", "--location-trusted"):
            with self.subTest(opt=opt):
                lines = self._run(f"rc=0; curl_auth {opt} http://127.0.0.1:9/x || rc=$?; exit $rc", expect_rc=2)
                self.assertEqual(lines, [], f"curl ran with {opt}")
                self.assertIn("refuses to follow redirects", self.stderr)

    def _assert_header_file_only(self, lines):
        argv = [ln for ln in lines if ln.startswith("ARGV ")]
        hdrs = [ln.split(" ", 3) for ln in lines if ln.startswith("HDR ")]
        self.assertTrue(argv)
        for ln in argv:
            self.assertNotIn(self.TOKEN, ln, "the token was passed on curl's command line")
        self.assertEqual(len(hdrs), len(argv), "every authenticated call must use a header file")
        for _, path, mode, content in hdrs:
            self.assertEqual(content, f"Authorization: Bearer {self.TOKEN}")
            if os.name != "nt":
                self.assertEqual(mode, "600")
            self.assertFalse(os.path.exists(path), "the header file was left behind")

    def test_authenticated_probe_uses_a_private_header_file(self):
        self._assert_header_file_only(self._run("probe_endpoint /api/setup/status 1 >/dev/null"))

    def test_setup_status_fetch_uses_a_private_header_file(self):
        self._assert_header_file_only(self._run("fetch_setup_status >/dev/null"))

    def test_unauthenticated_probe_sends_no_token(self):
        lines = self._run("probe_endpoint /api/health 0 >/dev/null")
        self.assertEqual([ln for ln in lines if ln.startswith("HDR ")], [])
        self.assertFalse(any(self.TOKEN in ln for ln in lines))

    def test_no_bearer_header_is_built_on_a_command_line(self):
        self.assertNotIn('-H "Authorization', SCRIPT_SOURCE)
        self.assertNotIn("tok_arg", SCRIPT_SOURCE)


class _SlowStatusHandler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _SlowStatusHandler.seen.append(self.headers.get("Authorization"))
        time.sleep(1.5)  # keep curl running while /proc is scanned
        body = b'{"status":"ready","blocking_reasons":[]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("curl") and os.path.isdir("/proc"), "needs a real curl and /proc")
class RealCurlTokenTests(RolloutScriptTestBase):
    """From QA on #241: with the real curl, no process's command line ever
    holds the token, the server still gets it, and no header file is left."""

    TOKEN = "qa-SECRET-token-9f8e7d6c5b4a"

    def test_token_never_in_any_cmdline_with_real_curl(self):
        _SlowStatusHandler.seen = []
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowStatusHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        token = os.path.join(self.tmp, "auth_token")
        Path(token).write_text(self.TOKEN + "\n", encoding="utf-8")
        hits, stop = [], threading.Event()

        def scan():
            while not stop.is_set():
                for pid in os.listdir("/proc"):
                    if pid.isdigit():
                        try:
                            data = Path("/proc", pid, "cmdline").read_bytes()
                        except OSError:
                            continue
                        if self.TOKEN.encode() in data:
                            hits.append(data)
        scanner = threading.Thread(target=scan, daemon=True)
        scanner.start()
        os.remove(os.path.join(self.fakebin, "curl"))  # use the real curl
        tmpdir = os.path.join(self.tmp, "hdrtmp")
        os.makedirs(tmpdir)
        env = self.base_env(TMPDIR=tmpdir)
        res = self.run_snippet(
            f'ACTIVE_AUTH_TOKEN_PATH="{token}"\nENDPOINT_BASE_URL="http://127.0.0.1:{srv.server_port}"\n'
            'fetch_setup_status\nprobe_endpoint /api/x 1\n'
            'curl_auth -sS --max-time 10 "${ENDPOINT_BASE_URL}/api/library?limit=1"\n',
            env=env)
        stop.set()
        scanner.join()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(hits, [])
        self.assertEqual(_SlowStatusHandler.seen, [f"Bearer {self.TOKEN}"] * 3)
        self.assertEqual(os.listdir(tmpdir), [], "a header file was left behind")


@unittest.skipIf(os.name == "nt", "symbolic links need a POSIX host")
class SymlinkSafetyTests(VersionedStackFixture):
    """The script runs as root: a link planted in a container-writable data
    folder must never redirect a backup or restore copy to another path."""

    def outside(self, name, text):
        path = os.path.join(self.tmp, name)
        Path(path).write_text(text, encoding="utf-8")
        return path

    def test_symlinked_state_file_is_skipped_by_the_backup(self):
        target = self.outside("host-secret.txt", "host-only-content")
        os.remove(os.path.join(self.webmgr_dir, ".flask_secret_key"))
        os.symlink(target, os.path.join(self.webmgr_dir, ".flask_secret_key"))
        res = self.deploy()
        bdir = self.backup_dir()
        self.assertFalse(os.path.lexists(os.path.join(bdir, "web-manager-data", ".flask_secret_key")))
        self.assertIn("web-manager-data/.flask_secret_key skipped (symbolic link)",
                      Path(bdir, "state-manifest.txt").read_text(encoding="utf-8"))
        self.assertIn("symbolic link", res.stderr)

    def test_backup_drops_links_inside_copied_folders(self):
        target = self.outside("host-file.txt", "host-only-content")
        os.symlink(target, os.path.join(self.webmgr_dir, "transactions", "evil.json"))
        self.deploy()
        tx = os.path.join(self.backup_dir(), "web-manager-data", "transactions")
        self.assertTrue(os.path.isfile(os.path.join(tx, "t1.json")))
        self.assertFalse(os.path.lexists(os.path.join(tx, "evil.json")))

    def test_rollback_replaces_a_symlinked_destination_without_writing_through_it(self):
        self.deploy()
        target = self.outside("host-file.txt", "host-only-content")
        dst = os.path.join(self.webmgr_dir, ".flask_secret_key")
        os.remove(dst)
        os.symlink(target, dst)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Path(target).read_text(encoding="utf-8"), "host-only-content")
        self.assertFalse(os.path.islink(dst))
        self.assertEqual(Path(dst).read_text(encoding="utf-8"), "flask-key-before")

    def test_rollback_refuses_a_symlinked_transactions_folder(self):
        self.deploy()
        outside_dir = os.path.join(self.tmp, "host-dir")
        os.makedirs(outside_dir)
        tx = os.path.join(self.webmgr_dir, "transactions")
        for name in os.listdir(tx):
            os.remove(os.path.join(tx, name))
        os.rmdir(tx)
        os.symlink(outside_dir, tx)
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(os.listdir(outside_dir), [], "nothing may be written through the link")
        self.assertIn("transactions/ is or contains a symbolic link", res.stderr)

    def test_symlinked_token_path_stops_the_deploy(self):
        target = self.outside("host-token.txt", "host-only-content")
        token = os.path.join(self.webmgr_dir, ".auth_token")
        os.remove(token)
        os.symlink(target, token)
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("is a symbolic link", res.stderr)
        self.assertEqual(Path(target).read_text(encoding="utf-8"), "host-only-content")
        self.assertEqual(self.webmgr_container()["Config"]["Image"], self.OLD_IMAGE)


class SetupStatusGateTests(VersionedStackFixture):
    def test_new_blocking_reason_fails_the_deploy_with_rollback_guidance(self):
        reason = "Cannot write to downloads/staging path downloads"
        res = self.run_script(env=self.env(curl_blocking_reasons_by_image={self.GOOD_IMAGE: [reason]}))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"NEW setup blocking reason after deploy: {reason}", res.stderr)
        self.assertIn("Reason code:           setup_new_blocking_reason", res.stderr)
        self.assertIn("--rollback", res.stderr)
        self.assertIn("BEETS_WEB_MANAGER_VERSION=0.1.2", Path(self.stack_env).read_text(encoding="utf-8"),
                      "an unverified version must not be persisted to .env")

    # --- #179: compare by stable reason codes, not message text ------------

    def test_reworded_reason_with_the_same_code_does_not_fail_the_deploy(self):
        res = self.deploy(
            curl_blocking_reasons_by_image={self.OLD_IMAGE: ["Music library path /music is not accessible"],
                                            self.GOOD_IMAGE: ["Cannot read the music library at /music"]},
            curl_blocking_reason_codes_by_image={self.OLD_IMAGE: ["music_path_not_accessible"],
                                                 self.GOOD_IMAGE: ["music_path_not_accessible"]})
        self.assertIn("Setup blocking reasons compared by reason code.", res.stderr)
        self.assertIn("No new setup blocking reasons.", res.stderr)

    def test_new_reason_code_fails_the_deploy_and_reports_both_codes(self):
        same_text = "Music library path /music is not accessible"
        res = self.run_script(env=self.env(
            curl_blocking_reasons_by_image={self.OLD_IMAGE: [same_text], self.GOOD_IMAGE: [same_text]},
            curl_blocking_reason_codes_by_image={self.OLD_IMAGE: ["music_path_not_accessible"],
                                                 self.GOOD_IMAGE: ["downloads_not_writable"]}))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("(reason_code=downloads_not_writable)", res.stderr)
        self.assertIn("Reason code:           setup_new_blocking_reason", res.stderr)
        self.assertIn("Failed stage:          setup-status-after", res.stderr)

    def test_control_characters_in_reasons_cannot_forge_log_lines(self):
        forged = "x\n[00:00:00] === Rollback complete (forged) ===\x1b[31m\tcode-in-message"
        res = self.run_script(env=self.env(
            curl_blocking_reasons_by_image={self.GOOD_IMAGE: [forged]},
            curl_blocking_reason_codes_by_image={self.GOOD_IMAGE: ["a\tb\nc"]}))
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("\x1b", res.stderr)
        self.assertFalse([l for l in res.stderr.splitlines() if l.startswith("[00:00:00] === Rollback complete (forged)")],
                         "a reason must not start a log line of its own")
        (line,) = [l for l in res.stderr.splitlines() if "NEW setup blocking reason after deploy" in l]
        self.assertIn("x\\u000a[00:00:00] === Rollback complete (forged) ===\\u001b[31m\\u0009code-in-message", line)
        self.assertIn("(reason_code=a\\u0009b\\u000ac)", line)

    def test_previous_version_without_codes_falls_back_to_message_text(self):
        reason = "Music library path /music is not accessible"
        res = self.deploy(
            curl_blocking_reasons_by_image={self.OLD_IMAGE: [reason], self.GOOD_IMAGE: [reason]},
            curl_blocking_reason_codes_by_image={self.GOOD_IMAGE: ["music_path_not_accessible"]})
        self.assertIn("compared by exact message text", res.stderr)
        self.assertIn("No new setup blocking reasons.", res.stderr)

    def test_malformed_codes_are_ignored_in_favor_of_message_text(self):
        reason = "Music library path /music is not accessible"
        res = self.deploy(
            curl_blocking_reasons_by_image={self.OLD_IMAGE: [reason], self.GOOD_IMAGE: [reason]},
            curl_blocking_reason_codes_by_image={self.OLD_IMAGE: ["music_path_not_accessible"],
                                                 self.GOOD_IMAGE: []})
        self.assertIn("compared by exact message text", res.stderr)

    def test_unreadable_status_before_fails_on_any_coded_reason(self):
        reason = "Music library path /music is not accessible"
        res = self.run_script(env=self.env(
            curl_setup_status_http_by_image={self.OLD_IMAGE: "503"},
            curl_blocking_reasons_by_image={self.OLD_IMAGE: [reason], self.GOOD_IMAGE: [reason]},
            curl_blocking_reason_codes_by_image={self.OLD_IMAGE: ["music_path_not_accessible"],
                                                 self.GOOD_IMAGE: ["music_path_not_accessible"]}))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("(reason_code=music_path_not_accessible)", res.stderr)
        self.assertIn("Reason code:           setup_new_blocking_reason", res.stderr)

    def test_blocking_reason_that_already_existed_does_not_fail_the_deploy(self):
        reason = "Music library path /music is not accessible"
        self.deploy(curl_blocking_reasons_by_image={self.OLD_IMAGE: [reason], self.GOOD_IMAGE: [reason]})

    def test_unreadable_status_before_makes_any_blocking_reason_fail_the_deploy(self):
        reason = "Music library path /music is not accessible"
        res = self.run_script(env=self.env(
            curl_setup_status_http_by_image={self.OLD_IMAGE: "503"},
            curl_blocking_reasons_by_image={self.OLD_IMAGE: [reason], self.GOOD_IMAGE: [reason]}))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("could not read /api/setup/status before the deploy", res.stderr)
        self.assertIn(f"NEW setup blocking reason after deploy: {reason}", res.stderr)
        self.assertIn("--rollback", res.stderr)

    def test_unreadable_status_before_with_clean_status_after_succeeds(self):
        res = self.deploy(curl_setup_status_http_by_image={self.OLD_IMAGE: "503"})
        self.assertIn("could not read /api/setup/status before the deploy", res.stderr)
        self.assertIn("No new setup blocking reasons.", res.stderr)

    def test_status_not_200_after_the_deploy_fails_with_rollback_guidance(self):
        res = self.run_script(env=self.env(curl_setup_status_http_by_image={self.GOOD_IMAGE: "503"}))
        self.assertNotEqual(res.returncode, 0)
        # Endpoint verification (which probes /api/setup/status and expects
        # 200) runs before the blocking-reason gate, so it is the stage that
        # fails; either way the deploy must stop with rollback guidance.
        self.assertIn("/api/setup/status attempt 1: HTTP 503 (expected 200)", res.stderr)
        self.assertIn("Failed stage:          endpoint-verification", res.stderr)
        self.assertNotIn("No new setup blocking reasons.", res.stderr)
        self.assertIn("--rollback", res.stderr)
        self.assertIn("BEETS_WEB_MANAGER_VERSION=0.1.2", Path(self.stack_env).read_text(encoding="utf-8"),
                      "an unverified version must not be persisted to .env")


class EnginePluginRefreshTests(VersionedStackFixture):
    def test_stale_running_plugin_restarts_only_the_engine(self):
        self.set_provisioned_plugin("1.3.0")
        self.state["plugin_version_after_restart"] = "1.3.0"
        self._save_state()
        beets_cid = self.state["service_containers"]["beets"]
        res = self.deploy()
        st = self.load_state()
        self.assertIn("beets", st.get("restarted", []))
        self.assertEqual(st["service_containers"]["beets"], beets_cid, "engine is restarted, never recreated")
        self.assertIn("restarting beets only", res.stderr)
        self.assertIn("plugin_after=1.3.0", Path(self.backup_dir(), "engine-plugin.txt").read_text(encoding="utf-8"))

    def test_matching_plugin_does_not_restart_the_engine(self):
        res = self.deploy()
        self.assertNotIn("beets", self.load_state().get("restarted", []))
        self.assertIn("no engine restart needed", res.stderr)

    def test_library_change_across_engine_restart_fails(self):
        self.set_provisioned_plugin("1.3.0")
        self.state["plugin_version_after_restart"] = "1.3.0"
        self.state["digest_after_restart"] = "b" * 64
        self._save_state()
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("digest changed across the engine restart", res.stderr)


class BackupRetentionTests(VersionedStackFixture):
    def _make_backup(self, name, stale=False):
        d = os.path.join(self.stack_dir, "_backups", name)
        os.makedirs(d)
        Path(d, "docker-compose.yml.bak").write_text("x", encoding="utf-8")
        if stale:
            os.makedirs(os.path.join(d, "stale-database"))
        return d

    def test_prune_is_opt_in_keeps_newest_and_archived_stale_databases(self):
        old = self._make_backup("web-manager-rollout-20200101-000000")
        old_stale = self._make_backup("web-manager-rollout-20200102-000000", stale=True)
        unrelated = self._make_backup("my-own-notes-20200101")
        newest = self._make_backup("web-manager-rollout-20200103-000000")

        dry = self.run_script("--prune-backups-older-than", "30", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("would delete", dry.stderr)
        self.assertTrue(os.path.isdir(old), "--dry-run deletes nothing")

        res = self.run_script("--prune-backups-older-than", "30")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(old_stale), "a backup holding an archived stale DB is never pruned automatically")
        self.assertTrue(os.path.isdir(unrelated), "only this script's own backup directories are candidates")
        self.assertTrue(os.path.isdir(newest), "the newest backup is always kept")

    def test_prune_requires_a_positive_day_count(self):
        for bad in ("0", "-1", "abc", ""):
            res = self.run_script("--prune-backups-older-than", bad)
            self.assertNotEqual(res.returncode, 0, bad)
            self.assertIn("whole number of days", res.stderr)

    def test_a_normal_deploy_never_prunes(self):
        old = self._make_backup("web-manager-rollout-20200101-000000")
        self.deploy()
        self.assertTrue(os.path.isdir(old))


@unittest.skipUnless(BASH, _NO_BASH_REASON)
class HelpAndCliLifecycleTests(unittest.TestCase):
    """--help must work standalone: no STACK_DIR, no VERSION, nothing else
    configured. It exits inside argument parsing, before any of the
    required-configuration checks that gate the real modes."""

    def test_help_works_with_zero_environment_configured(self):
        env = {k: v for k, v in os.environ.items() if k not in ("STACK_DIR", "VERSION")}
        res = subprocess.run(
            [BASH, str(SCRIPT), "--help"], env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage:", res.stdout)
        self.assertNotIn("STACK_DIR", res.stderr)
        self.assertNotIn("VERSION", res.stderr)

    def test_short_help_flag_behaves_the_same(self):
        env = {k: v for k, v in os.environ.items() if k not in ("STACK_DIR", "VERSION")}
        res = subprocess.run(
            [BASH, str(SCRIPT), "-h"], env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage:", res.stdout)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(BASH, _NO_BASH_REASON)
class OfflineDbIdentityTests(EndToEndFixture):
    """--offline-db-identity: stop Beets, hash only a settled database,
    always restart it, and re-verify semantics."""

    def _state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def _sha(self):
        import hashlib
        with open(self.auth_db, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_settled_database_is_hashed_and_engine_restarted(self):
        res = self.run_script("--offline-db-identity")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("OFFLINE BYTE IDENTITY", res.stdout)
        self.assertIn("WAL settled by engine:    yes", res.stdout)
        self.assertIn(self._sha(), res.stdout)
        self.assertIn("beets", self._state().get("started", []))
        self.assertEqual(self._state()["containers"]["cid-beets"]["State"]["Status"], "running")

    def test_baseline_comparison(self):
        res = self.run_script("--offline-db-identity", env=self.env(BASELINE_DB_SHA256=self._sha()))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IDENTICAL to baseline", res.stdout)

    def test_unsettled_wal_is_settled_on_a_copy_never_on_the_live_file(self):
        """Beets leaves its WAL unsettled on stop: the check settles a
        private copy, hashes that, and proves the originals were untouched."""
        import sqlite3
        con = sqlite3.connect(self.auth_db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("INSERT INTO items (title) VALUES ('only-in-wal')")
        con.commit()
        # Snapshot main + WAL while the connection is open (the committed row
        # lives only in the WAL), then leave them behind as an engine that
        # never closes would.
        wal = self.auth_db + "-wal"
        with open(wal, "rb") as f:
            wal_bytes = f.read()
        with open(self.auth_db, "rb") as f:
            main_bytes = f.read()
        con.close()
        with open(self.auth_db, "wb") as f:
            f.write(main_bytes)
        with open(wal, "wb") as f:
            f.write(wal_bytes)
        main_before = self._sha()
        res = self.run_script("--offline-db-identity")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("settled on a private copy", res.stdout)
        self.assertIn("hashed:                   settled copy", res.stdout)
        self.assertEqual(self._sha(), main_before)          # live file untouched
        with open(wal, "rb") as f:
            self.assertEqual(f.read(), wal_bytes)           # live WAL untouched
        self.assertNotIn(main_before, res.stdout.split("database sha256:")[1].splitlines()[0])
        self.assertIn("beets", self._state().get("started", []))

    def test_digest_change_across_restart_fails_and_engine_is_left_running(self):
        state = self._state()
        snap = dict(state["semantic_snapshot"])
        state["semantic_snapshots"] = [snap, {**snap, "digest": "b" * 64}]
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        res = self.run_script("--offline-db-identity")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("digest differs", res.stderr)
        self.assertEqual(self._state()["containers"]["cid-beets"]["State"]["Status"], "running")


class DbIntegrityWordingTests(unittest.TestCase):
    """A live main-file hash is never presented as proof of DB identity."""

    def _body(self, name):
        m = re.search(r"^%s\(\) \{\n(.*?)^\}" % re.escape(name), SCRIPT_SOURCE, re.S | re.M)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def test_online_checks_never_open_sqlite(self):
        for fn in ("verify_authoritative_database", "assert_authoritative_db_unchanged"):
            self.assertNotIn("sqlite_ro_query", self._body(fn), fn)

    def test_live_hash_is_not_a_verdict(self):
        self.assertNotIn("authoritative database SHA-256 changed", SCRIPT_SOURCE)
        self.assertNotIn("byte-identical", SCRIPT_SOURCE.lower())
        self.assertIn("informational only", self._body("assert_authoritative_db_unchanged"))

    def test_offline_hash_requires_a_stopped_engine_and_settled_wal(self):
        body = self._body("run_offline_db_identity")
        stop, hash_at = body.index("stop -t 60"), body.index('sha="$(sha256_file')
        self.assertLess(stop, hash_at)
        self.assertIn('if [[ "$settled" -eq 1 ]]', body)
        self.assertIn("settle_copy_and_hash", body)
        self.assertNotIn("wal_checkpoint", body)  # never on the authoritative file
        self.assertIn('_compose start "$ENGINE_SERVICE"', body)
