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
SCRIPT = ROOT / "scripts" / "deploy_truenas_web_manager.sh"
FAKE_DOCKER = ROOT / "tests" / "deploy" / "fake_docker.py"
FAKE_CURL = ROOT / "tests" / "deploy" / "fake_curl.py"
SCRIPT_SOURCE = SCRIPT.read_text(encoding="utf-8")

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
        env["STACK_DIR"] = self.tmp
        env["VERSION"] = "0.1.6"
        for k, v in extra.items():
            env[k] = str(v)
        return env

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
verify_endpoints "dry-run"
rc=$?
echo "RC=$rc"
""", env=self.base_env(FAKE_CURL_STATE=state_file))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("endpoint verification reported issues", res.stderr)
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
        self.assertIn(f"ACTIVE={legacy}", res.stdout)
        self.assertIn("MIGRATION=1", res.stdout)
        self.assertIn("EXISTS=0", res.stdout)
        self.assertFalse(os.path.exists(dest), "dry-run must not create persistent token")
        self.assertNotIn("secret-legacy-token-12345", res.stdout)
        self.assertNotIn("secret-legacy-token-12345", res.stderr)

    def test_rollback_case_b_migrated_token_removed_safely(self):
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

        backup_dir = os.path.join(self.tmp, "backup")
        os.makedirs(backup_dir)
        Path(os.path.join(backup_dir, "docker-compose.yml.bak")).write_text("services: {}\n", encoding="utf-8")
        Path(os.path.join(backup_dir, "token-metadata.txt")).write_text(f"""persistent_token_existed_before=0
legacy_token_existed_before=1
token_migration_planned=1
persistent_token_path={dest}
legacy_token_path={legacy}
token_migration_performed=1
migrated_token_sha256=d344ed015c7e112d7cdd949a60e0a5c43d84693b708aa80fb65c2b53bdad58b1
""", encoding="utf-8")

        res = self.run_snippet(f"""
_compose() {{ return 0; }}
docker() {{ echo "healthy"; }}
resolve_container_id() {{ echo "cid-mock"; }}
discover_and_verify_mounts() {{ return 0; }}
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

        backup_dir = os.path.join(self.tmp, "backup")
        os.makedirs(backup_dir)
        Path(os.path.join(backup_dir, "docker-compose.yml.bak")).write_text("services: {}\n", encoding="utf-8")
        Path(os.path.join(backup_dir, "token-metadata.txt")).write_text(f"""persistent_token_existed_before=0
legacy_token_existed_before=1
token_migration_planned=1
persistent_token_path={dest}
legacy_token_path={legacy}
token_migration_performed=1
migrated_token_sha256=d344ed015c7e112d7cdd949a60e0a5c43d84693b708aa80fb65c2b53bdad58b1
""", encoding="utf-8")

        res = self.run_snippet(f"""
_compose() {{ return 0; }}
docker() {{ echo "healthy"; }}
resolve_container_id() {{ echo "cid-mock"; }}
discover_and_verify_mounts() {{ return 0; }}
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
    """Static checks on the script's own text for constraints best proven
    by absence or pattern match."""

    def test_no_wildcard_glob_on_database_filename(self):
        self.assertNotIn("musiclibrary.blb*", SCRIPT_SOURCE)

    def test_exact_db_filenames_only(self):
        self.assertIn('DB_FILENAME="musiclibrary.blb"', SCRIPT_SOURCE)
        self.assertIn('WAL_FILENAME="musiclibrary.blb-wal"', SCRIPT_SOURCE)
        self.assertIn('SHM_FILENAME="musiclibrary.blb-shm"', SCRIPT_SOURCE)

    def test_no_rm_of_database_or_token_files(self):
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
        self.assertNotIn('--force-recreate "$ENGINE_SERVICE"', SCRIPT_SOURCE)
        self.assertIn('--force-recreate "$SERVICE"', SCRIPT_SOURCE)

    def test_documentation_and_script_specify_explicit_bash_invocation(self):
        doc_source = (ROOT / "docs" / "operations" / "TRUENAS_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn("/bin/bash", doc_source)
        self.assertIn("/bin/bash", SCRIPT_SOURCE)


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
        self.assertIn("expected 'ghcr.io/iranman/beets-web-manager:0.1.3'", res.stderr)

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


if __name__ == "__main__":
    unittest.main()
