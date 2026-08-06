"""Tests for scripts/deploy_truenas_web_manager_0_1_3.sh.

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
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_truenas_web_manager_0_1_3.sh"
FAKE_DOCKER = ROOT / "tests" / "deploy" / "fake_docker.py"
FAKE_CURL = ROOT / "tests" / "deploy" / "fake_curl.py"
SCRIPT_SOURCE = SCRIPT.read_text(encoding="utf-8")

# Requires a working POSIX `bash` on PATH (true in any real Linux
# environment, and in a correctly configured Git-Bash-first Windows PATH).
# On a Windows dev box where a `C:\WINDOWS\system32\bash.exe` WSL-launcher
# stub shadows Git for Windows' real bash on PATH (observed when invoked
# from a plain PowerShell session with no Linux distro registered), point
# BASH at the real Git-for-Windows bash explicitly via this env var:
#   $env:ROLLOUT_TEST_BASH = 'C:\Program Files\Git\bin\bash.exe'
BASH = os.environ.get("ROLLOUT_TEST_BASH", "bash")


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
        for k, v in extra.items():
            env[k] = str(v)
        return env

    # -- function-level snippet runner ------------------------------------
    def run_snippet(self, body, env=None):
        snippet_path = os.path.join(self.tmp, "snippet.sh")
        with open(snippet_path, "w", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
            f.write(f'source "{SCRIPT.as_posix()}"\n')
            f.write(body + "\n")
        return subprocess.run(
            [BASH, snippet_path], env=env or self.base_env(),
            capture_output=True, text=True, timeout=30,
        )


class AuthoritativeDatabaseChecksTests(RolloutScriptTestBase):
    def test_missing_authoritative_db_is_rejected(self):
        res = self.run_snippet(f"""
AUTH_DB_PATH="{self.tmp}/does-not-exist/musiclibrary.blb"
STALE_DB_PATH="{self.tmp}/also-missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("authoritative database missing", res.stderr)

    def test_query_failure_on_non_sqlite_file_is_rejected(self):
        bad = os.path.join(self.tmp, "musiclibrary.blb")
        with open(bad, "wb") as f:
            f.write(b"this is not a sqlite database file at all")
        res = self.run_snippet(f"""
AUTH_DB_PATH="{bad}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("quick_check failed to execute", res.stderr)

    def test_corrupted_but_openable_db_fails_quick_check(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=500, albums=50)
        corrupt_db_keep_openable(db)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
STALE_DB_PATH="{self.tmp}/missing/musiclibrary.blb"
verify_authoritative_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("quick_check", res.stderr)

    def test_suspiciously_low_item_count_is_rejected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=2, albums=1)
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
        os.link(db, stale)  # same inode, different path
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
        # Distinct inode (real copy), identical bytes -> checksum collision.
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


class ArchiveStaleDatabaseTests(RolloutScriptTestBase):
    def test_missing_wal_and_shm_are_accepted(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        stale = os.path.join(webmgr, "musiclibrary.blb")
        make_sqlite_db(stale, items=3, albums=1)
        backup = os.path.join(self.tmp, "backup")

        res = self.run_snippet(f"""
STALE_DB_EXISTS=1
STALE_DB_PATH="{stale}"
STALE_WAL_PATH="{webmgr}/musiclibrary.blb-wal"
STALE_SHM_PATH="{webmgr}/musiclibrary.blb-shm"
BACKUP_DIR="{backup}"
archive_stale_database
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.exists(os.path.join(backup, "stale-database", "musiclibrary.blb")))
        self.assertFalse(os.path.exists(stale), "stale DB must be moved, not left in place")

    def test_open_stale_db_is_rejected_and_not_moved(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        stale = os.path.join(webmgr, "musiclibrary.blb")
        make_sqlite_db(stale, items=3, albums=1)
        backup = os.path.join(self.tmp, "backup")
        self.set_open_files([stale])

        res = self.run_snippet(f"""
STALE_DB_EXISTS=1
STALE_DB_PATH="{stale}"
STALE_WAL_PATH="{webmgr}/musiclibrary.blb-wal"
STALE_SHM_PATH="{webmgr}/musiclibrary.blb-shm"
BACKUP_DIR="{backup}"
archive_stale_database
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("still open", res.stderr)
        self.assertTrue(os.path.exists(stale), "an open stale DB must never be moved")

    def test_archive_moves_exact_filenames_never_deletes(self):
        webmgr = os.path.join(self.tmp, "webmgr")
        os.makedirs(webmgr)
        stale = os.path.join(webmgr, "musiclibrary.blb")
        wal = os.path.join(webmgr, "musiclibrary.blb-wal")
        shm = os.path.join(webmgr, "musiclibrary.blb-shm")
        make_sqlite_db(stale, items=3, albums=1)
        Path(wal).write_bytes(b"wal-data")
        Path(shm).write_bytes(b"shm-data")
        backup = os.path.join(self.tmp, "backup")

        res = self.run_snippet(f"""
STALE_DB_EXISTS=1
STALE_DB_PATH="{stale}"
STALE_WAL_PATH="{wal}"
STALE_SHM_PATH="{shm}"
BACKUP_DIR="{backup}"
archive_stale_database
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        for name in ("musiclibrary.blb", "musiclibrary.blb-wal", "musiclibrary.blb-shm"):
            self.assertTrue(os.path.exists(os.path.join(backup, "stale-database", name)))
            self.assertFalse(os.path.exists(os.path.join(webmgr, name)))


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
echo "EXISTS=$TOKEN_EXISTS SIZE=$TOKEN_SIZE"
""")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("super-secret-token-value-do-not-print", res.stdout)
        self.assertNotIn("super-secret-token-value-do-not-print", res.stderr)
        self.assertIn("EXISTS=1", res.stdout)

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
AUTH_ALBUM_COUNT=2
AUTH_DB_SHA256="deadbeef"
assert_authoritative_db_unchanged
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("item count changed", res.stderr)

    def test_checksum_change_is_detected(self):
        db = os.path.join(self.tmp, "musiclibrary.blb")
        make_sqlite_db(db, items=10, albums=2)
        res = self.run_snippet(f"""
AUTH_DB_PATH="{db}"
AUTH_ITEM_COUNT=10
AUTH_ALBUM_COUNT=2
AUTH_DB_SHA256="0000000000000000000000000000000000000000000000000000000000000000"
assert_authoritative_db_unchanged
""")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("SHA-256 changed", res.stderr)

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
    """Static checks on the script's own text for the constraints that are
    best proven by absence rather than behavior: no wildcard deletion, no
    `rm` of database files, exact filenames only."""

    def test_no_wildcard_glob_on_database_filename(self):
        self.assertNotIn("musiclibrary.blb*", SCRIPT_SOURCE)

    def test_exact_db_filenames_only(self):
        self.assertIn('DB_FILENAME="musiclibrary.blb"', SCRIPT_SOURCE)
        self.assertIn('WAL_FILENAME="musiclibrary.blb-wal"', SCRIPT_SOURCE)
        self.assertIn('SHM_FILENAME="musiclibrary.blb-shm"', SCRIPT_SOURCE)

    def test_no_rm_of_database_or_token_files(self):
        # `rm` may still be used for unrelated scratch/temp files (e.g. a
        # curl probe response body) -- what must never happen is `rm` being
        # used on anything database- or token-shaped. Database files are
        # moved (archived), never deleted.
        for lineno, line in enumerate(SCRIPT_SOURCE.splitlines(), start=1):
            code = line.split("#", 1)[0]
            if re.search(r"(^|[;&|]\s*)rm\s", code):
                self.assertNotRegex(
                    code, r"musiclibrary|auth_token|DB_PATH|WAL_PATH|SHM_PATH|TOKEN_PATH",
                    f"line {lineno} uses 'rm' on what looks like a database/token path: {line!r}",
                )

    def test_stale_database_is_moved_not_deleted(self):
        self.assertIn("mv \"$STALE_DB_PATH\"", SCRIPT_SOURCE)

    def test_uses_set_dash_Eeuo_pipefail(self):
        self.assertIn("set -Eeuo pipefail", SCRIPT_SOURCE)

    def test_beets_engine_service_is_never_recreated(self):
        # --force-recreate must only ever be invoked against $SERVICE, never
        # against $ENGINE_SERVICE or a literal "beets".
        self.assertNotIn('--force-recreate "$ENGINE_SERVICE"', SCRIPT_SOURCE)
        self.assertIn('--force-recreate "$SERVICE"', SCRIPT_SOURCE)


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
            f.write("#!/usr/bin/env bash\nexit 1\n")  # nothing ever open
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
        # No real backup root created under the stack dir either.
        self.assertFalse(os.path.isdir(os.path.join(self.stack_dir, "_backups")))
        # The web-manager container must never have been stopped/recreated.
        self.assertEqual(self.state["containers"]["cid-webmgr"]["State"]["Status"], "running")

    def _snapshot_stack_dir(self):
        snap = {}
        for root, _, files in os.walk(self.stack_dir):
            for name in files:
                p = os.path.join(root, name)
                snap[p] = (os.path.getsize(p), os.path.getmtime(p))
        return snap

    def test_dry_run_rejects_same_mount_source_for_both_services(self):
        # Point beets-web-manager's /web-manager-data at the SAME host path
        # as the Beets engine's /config -- the classic misconfiguration this
        # script must refuse to proceed past.
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
        self.assertIn("expected 'ghcr.io/iranman/beets-web-manager:0.1.3'", res.stderr)

    def test_dry_run_rejects_wrong_revision_label(self):
        self.state["images"][self.GOOD_IMAGE]["Config"]["Labels"]["org.opencontainers.image.revision"] = "0000000deadbeef"
        self._save_state()
        res = self.run_script("--dry-run")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("revision label", res.stderr)


class FullDeployTests(EndToEndFixture):
    def test_full_deploy_happy_path_succeeds(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("initial-token-value-not-printed", encoding="utf-8")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("completed successfully", res.stderr)
        self.assertNotIn("initial-token-value-not-printed", res.stdout)
        self.assertNotIn("initial-token-value-not-printed", res.stderr)
        # Recreated onto the pinned image.
        webmgr_cid = self.state["service_containers"]["beets-web-manager"]
        self.assertEqual(self.state["containers"][webmgr_cid]["Config"]["Image"], self.GOOD_IMAGE)
        # A backup directory was actually created under STACK_DIR/_backups.
        backups = os.listdir(os.path.join(self.stack_dir, "_backups"))
        self.assertEqual(len(backups), 1)

    def test_deploy_archives_stale_db_with_missing_wal_shm(self):
        token = os.path.join(self.webmgr_dir, ".auth_token")
        Path(token).write_text("tok", encoding="utf-8")
        stale = os.path.join(self.webmgr_dir, "musiclibrary.blb")
        make_sqlite_db(stale, items=2, albums=1)  # small, disposable
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(stale))
        backup_root = os.path.join(self.stack_dir, "_backups")
        backup_dir = os.path.join(backup_root, os.listdir(backup_root)[0])
        self.assertTrue(os.path.exists(os.path.join(backup_dir, "stale-database", "musiclibrary.blb")))

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

        # Simulate a bad follow-up state to roll back from.
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


if __name__ == "__main__":
    unittest.main()
