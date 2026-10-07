"""Round-trip tests for scripts/backup.sh and scripts/restore.sh.

These run the real scripts with bash against temporary directories that
stand in for a stack's ./beets (/config) and ./web-manager
(/web-manager-data) folders. The Beets database is a real SQLite file in
WAL mode with a connection held open and committed rows still in the -wal
file -- the state of a library while Beets is running -- so the test proves
the online backup captures them without modifying the live database.

Linux/macOS only (POSIX bash, tar); skipped on Windows.
"""
import datetime
import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"
BASH = shutil.which("bash") if os.name != "nt" else None


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tree(root):
    """{relative path: sha256} for every file under root, skipping
    .pre-restore-* folders."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".pre-restore-")]
        for name in filenames:
            p = os.path.join(dirpath, name)
            out[os.path.relpath(p, root)] = _sha(p)
    return out


@unittest.skipUnless(BASH, "needs a POSIX bash (Linux/macOS)")
class BackupRestoreRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bwm-backup-test-")
        self.beets = os.path.join(self.tmp, "beets")
        self.wm = os.path.join(self.tmp, "web-manager")
        self.out = os.path.join(self.tmp, "backups")
        os.makedirs(os.path.join(self.beets, "beetsplug", "webmanager"))
        os.makedirs(os.path.join(self.wm, "transactions"))
        Path(self.beets, "config.yaml").write_text("directory: /music\nlibrary: /config/musiclibrary.blb\n", encoding="utf-8")
        Path(self.beets, ".webmanager_api_key").write_text("plugin-key-for-test", encoding="utf-8")
        Path(self.beets, "beetsplug", "webmanager", "version.py").write_text('PLUGIN_VERSION = "1.5.0"\n', encoding="utf-8")
        for name, body in ((".auth_token", "token-for-test"), (".env", "AI_MODEL=x\n"),
                           (".browser_username", "admin"), (".browser_password", "pbkdf2:hash"),
                           (".flask_secret_key", "flask-key"), (".setup_complete", "1"),
                           ("transactions/txn_1.json", '{"id": 1}')):
            Path(self.wm, name).write_text(body, encoding="utf-8")
        Path(self.wm, ".web_manager_config_store.lock").write_text("", encoding="utf-8")

        self.db = os.path.join(self.beets, "musiclibrary.blb")
        # A "running Beets": WAL mode, auto-checkpoint off, connection kept
        # open, so committed rows live only in musiclibrary.blb-wal.
        self.live = sqlite3.connect(self.db)
        self.live.execute("PRAGMA journal_mode=WAL")
        self.live.execute("PRAGMA wal_autocheckpoint=0")
        self.live.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)")
        self.live.executemany("INSERT INTO items (title) VALUES (?)", [(f"t{i}",) for i in range(25)])
        self.live.commit()
        self.assertGreater(os.path.getsize(self.db + "-wal"), 0, "fixture must have rows still in the WAL")

    def tearDown(self):
        try:
            self.live.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, script, *args, method=None, check=True, env_extra=None):
        env = dict(os.environ)
        if method:
            env["BWM_BACKUP_FORCE_METHOD"] = method
        env.update(env_extra or {})
        res = subprocess.run([BASH, str(script), *args], cwd=self.tmp, env=env,
                             capture_output=True, text=True, timeout=120)
        if check:
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res

    def backup(self, method=None):
        before_main, before_wal = _sha(self.db), _sha(self.db + "-wal")
        self.run_script(BACKUP, "--beets-config", self.beets, "--web-manager-data", self.wm, "--out", self.out, method=method)
        self.assertEqual(_sha(self.db), before_main, "backup must not modify the live database")
        self.assertEqual(_sha(self.db + "-wal"), before_wal, "backup must not modify the live WAL")
        archives = sorted(n for n in os.listdir(self.out) if n.endswith(".tar.gz"))
        self.assertEqual(len(archives), 1, archives)
        return os.path.join(self.out, archives[0])

    def _round_trip(self, method):
        archive = self.backup(method)
        self.assertEqual(stat.S_IMODE(os.stat(archive).st_mode) & 0o077, 0, "archive must be owner-only")
        with tarfile.open(archive) as tf:
            names = tf.getnames()
        for expected in ("beets/musiclibrary.blb", "beets/config.yaml", "beets/.webmanager_api_key",
                         "beets/beetsplug/webmanager/version.py", "web-manager-data/.auth_token",
                         "web-manager-data/.env", "web-manager-data/.browser_password",
                         "web-manager-data/.flask_secret_key", "web-manager-data/.setup_complete",
                         "web-manager-data/transactions/txn_1.json", "MANIFEST.txt"):
            self.assertTrue(any(n.endswith(expected) for n in names), f"{expected} missing from {names}")
        self.assertFalse(any(n.endswith(".lock") for n in names))

        wm_before = _tree(self.wm)
        wm_before.pop(".web_manager_config_store.lock")
        cfg_before = _sha(os.path.join(self.beets, "config.yaml"))

        # "Lose" everything, the way a failed upgrade or disk would.
        self.live.close()
        shutil.rmtree(self.beets)
        shutil.rmtree(self.wm)
        os.makedirs(self.beets)
        os.makedirs(self.wm)

        self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm, "--yes", archive)

        con = sqlite3.connect(os.path.join(self.beets, "musiclibrary.blb"))
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM items").fetchone()[0], 25,
                             "rows that were only in the WAL must survive the round trip")
            self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            con.close()
        self.assertEqual(_sha(os.path.join(self.beets, "config.yaml")), cfg_before)
        self.assertTrue(os.path.isfile(os.path.join(self.beets, "beetsplug", "webmanager", "version.py")))
        self.assertEqual(_tree(self.wm), wm_before)

    def test_round_trip_with_python_online_backup(self):
        self._round_trip("python")

    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 command not installed")
    def test_round_trip_with_sqlite3_command(self):
        self._round_trip("sqlite3")

    def test_without_sqlite_tools_refuses_while_beets_may_be_running(self):
        res = self.run_script(BACKUP, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--out", self.out, method="stopped", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--beets-stopped", res.stderr)
        res = self.run_script(BACKUP, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--out", self.out, "--beets-stopped", method="stopped", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not empty", res.stderr)

    def test_missing_folders_give_an_actionable_error(self):
        res = self.run_script(BACKUP, "--beets-config", os.path.join(self.tmp, "nope"), check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--beets-config", res.stderr)
        res = self.run_script(BACKUP, "--beets-config", self.beets, "--web-manager-data",
                              os.path.join(self.tmp, "nope"), check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/web-manager-data", res.stderr)

    def test_restore_keeps_replaced_files_and_refuses_a_running_beets(self):
        archive = self.backup("python")
        # Beets still "running": the WAL is non-empty, so restore refuses.
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", archive, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("still running", res.stderr)

        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        Path(self.wm, ".env").write_text("AI_MODEL=changed-after-backup\n", encoding="utf-8")
        self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm, "--yes", archive)
        self.assertEqual(Path(self.wm, ".env").read_text(encoding="utf-8"), "AI_MODEL=x\n")
        kept = [d for d in os.listdir(self.wm) if d.startswith(".pre-restore-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(Path(self.wm, kept[0], ".env").read_text(encoding="utf-8"), "AI_MODEL=changed-after-backup\n")

    def test_restore_refuses_path_traversal_archives(self):
        evil = os.path.join(self.tmp, "evil.tar.gz")
        payload = os.path.join(self.tmp, "payload")
        Path(payload).write_text("x", encoding="utf-8")
        with tarfile.open(evil, "w:gz") as tf:
            tf.add(payload, arcname="../escaped")
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", evil, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("refusing to extract", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "escaped")))

    def _assert_link_archive_refused(self, add_member):
        """Build an archive in backup.sh's layout plus one link member, run
        restore, and assert it is refused before anything is copied."""
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        outside = os.path.join(self.tmp, "outside-secret")
        Path(outside).write_text("host secret", encoding="utf-8")
        evil = os.path.join(self.tmp, "links.tar.gz")
        stage = os.path.join(self.tmp, "stage", "beets-backup-x")
        os.makedirs(os.path.join(stage, "beets"))
        os.makedirs(os.path.join(stage, "web-manager-data"))
        Path(stage, "web-manager-data", ".env").write_text("AI_MODEL=from-archive", encoding="utf-8")
        with tarfile.open(evil, "w:gz") as tf:
            tf.add(stage, arcname="beets-backup-x")
            add_member(tf, outside)
        beets_before, wm_before = _tree(self.beets), _tree(self.wm)
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", evil, check=False)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("links or special files", res.stderr)
        self.assertEqual(_tree(self.beets), beets_before, "nothing may be copied into the Beets config")
        self.assertEqual(_tree(self.wm), wm_before, "nothing may be copied into the Web Manager data")
        for d in (self.beets, self.wm):
            self.assertFalse([n for n in os.listdir(d) if n.startswith(".pre-restore-")])
            for dirpath, dirnames, filenames in os.walk(d):
                for n in dirnames + filenames:
                    self.assertFalse(os.path.islink(os.path.join(dirpath, n)), n)
        self.assertEqual(Path(outside).read_text(encoding="utf-8"), "host secret")

    def test_restore_refuses_symlink_members(self):
        def add(tf, outside):
            for name, target in (("beets-backup-x/beets/config.yaml", outside),
                                 ("beets-backup-x/web-manager-data/etclink", "/etc")):
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
        self._assert_link_archive_refused(add)

    def test_restore_refuses_hardlink_members(self):
        def add(tf, outside):
            info = tarfile.TarInfo("beets-backup-x/beets/.webmanager_api_key")
            info.type = tarfile.LNKTYPE
            info.linkname = "beets-backup-x/web-manager-data/.env"
            tf.addfile(info)
        self._assert_link_archive_refused(add)

    def test_restored_credential_files_are_owner_only(self):
        archive = self.backup("python")
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        for f in ("config.yaml", ".webmanager_api_key"):
            os.chmod(os.path.join(self.beets, f), 0o644)
        self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm, "--yes", archive)
        for f in ("config.yaml", ".webmanager_api_key"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.beets, f)).st_mode), 0o600, f)

    def _rewrite_archive(self, archive, change):
        """Extract archive, apply change(root_dir) to its single top folder,
        and write it back to a new .tar.gz (returned)."""
        work = os.path.join(self.tmp, "rewrite")
        with tarfile.open(archive) as tf:
            tf.extractall(work, filter="data")
        (top,) = os.listdir(work)
        change(os.path.join(work, top))
        out = os.path.join(self.tmp, "rewritten.tar.gz")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(os.path.join(work, top), arcname=top)
        return out

    def _assert_restore_refused_untouched(self, archive, message):
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        # Change the live files after the backup so a restore would be visible.
        Path(self.wm, ".env").write_text("AI_MODEL=current\n", encoding="utf-8")
        beets_before, wm_before = _tree(self.beets), _tree(self.wm)
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", archive, check=False)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(message, res.stderr)
        self.assertEqual(_tree(self.beets), beets_before, "nothing may be restored into the Beets config")
        self.assertEqual(_tree(self.wm), wm_before, "nothing may be restored into the Web Manager data")
        for d in (self.beets, self.wm):
            self.assertFalse([n for n in os.listdir(d) if n.startswith(".pre-restore-")])

    def test_restore_refuses_a_file_that_does_not_match_the_manifest(self):
        archive = self.backup("python")

        def tamper(top):
            Path(top, "web-manager-data", ".env").write_text("AI_MODEL=tampered\n", encoding="utf-8")
        self._assert_restore_refused_untouched(self._rewrite_archive(archive, tamper), "checksum mismatch")

    def test_restore_refuses_a_file_missing_from_the_manifest(self):
        archive = self.backup("python")

        def plant(top):
            Path(top, "beets", "beetsplug", "planted.py").write_text("x = 1\n", encoding="utf-8")
        self._assert_restore_refused_untouched(self._rewrite_archive(archive, plant), "not listed in MANIFEST.txt")

    def test_restore_refuses_a_new_layout_backup_without_a_manifest(self):
        archive = self.backup("python")

        def drop(top):
            os.remove(os.path.join(top, "MANIFEST.txt"))
        self._assert_restore_refused_untouched(self._rewrite_archive(archive, drop), "no MANIFEST.txt")

    def _old_layout_archive(self):
        """An archive in the layout backup.sh wrote before manifests: the
        Beets files at the top level, no web-manager-data/, no MANIFEST.txt."""
        archive = self.backup("python")

        def to_old_layout(top):
            os.remove(os.path.join(top, "MANIFEST.txt"))
            shutil.rmtree(os.path.join(top, "web-manager-data"))
            for name in os.listdir(os.path.join(top, "beets")):
                os.rename(os.path.join(top, "beets", name), os.path.join(top, name))
            os.rmdir(os.path.join(top, "beets"))
        return self._rewrite_archive(archive, to_old_layout)

    def test_restore_refuses_an_old_backup_without_a_manifest(self):
        # #178: the old layout was restored unverified -- a downgrade path.
        self._assert_restore_refused_untouched(self._old_layout_archive(), "backup_manifest_missing")

    def test_allow_legacy_backup_restores_an_old_backup_with_a_warning(self):
        archive = self._old_layout_archive()
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        Path(self.beets, "config.yaml").write_text("changed: true\n", encoding="utf-8")
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", "--allow-legacy-backup", archive)
        self.assertIn("UNVERIFIED BACKUP", res.stderr)
        self.assertNotEqual(Path(self.beets, "config.yaml").read_text(encoding="utf-8"), "changed: true\n")

    def test_allow_legacy_backup_does_not_skip_a_new_layout_without_a_manifest(self):
        archive = self.backup("python")

        def drop(top):
            os.remove(os.path.join(top, "MANIFEST.txt"))
        rewritten = self._rewrite_archive(archive, drop)
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", "--allow-legacy-backup", rewritten, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("backup_manifest_missing", res.stderr)

    def test_backup_with_a_nested_manifest_file_restores(self):
        nested = Path(self.beets, "beetsplug", "webmanager", "MANIFEST.txt")
        nested.write_text("plugin notes\n", encoding="utf-8")
        archive = self.backup("python")
        nested.unlink()
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm, "--yes", archive)
        self.assertEqual(nested.read_text(encoding="utf-8"), "plugin notes\n")

    def _assert_manifest_path_refused(self, bad_path):
        archive = self.backup("python")

        def add(top):
            with open(os.path.join(top, "MANIFEST.txt"), "a", encoding="utf-8", newline="\n") as f:
                f.write("0" * 64 + "  " + bad_path + "\n")
        self._assert_restore_refused_untouched(self._rewrite_archive(archive, add),
                                               "MANIFEST.txt lists a path outside the backup")

    def test_restore_refuses_a_manifest_path_starting_with_dotdot(self):
        self._assert_manifest_path_refused("../x")

    def test_restore_refuses_a_manifest_path_with_an_inner_dotdot(self):
        self._assert_manifest_path_refused("./beets/../../x")

    def test_restore_refuses_an_absolute_manifest_path(self):
        self._assert_manifest_path_refused("/etc/hostname")

    def test_restore_reports_verified_checksums(self):
        archive = self.backup("python")
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm, "--yes", archive)
        self.assertRegex(res.stderr, r"Verified \d+ file checksums against MANIFEST.txt")

    def test_backup_name_and_manifest_use_utc(self):
        env = dict(os.environ, TZ="America/Los_Angeles", BWM_BACKUP_FORCE_METHOD="python")
        before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
        subprocess.run([BASH, str(BACKUP), "--beets-config", self.beets, "--web-manager-data", self.wm,
                        "--out", self.out], cwd=self.tmp, env=env, check=True, capture_output=True, timeout=120)
        after = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
        (name,) = [n for n in os.listdir(self.out) if n.endswith(".tar.gz")]
        stamp = datetime.datetime.strptime(name[len("beets-backup-"):-len(".tar.gz")], "%Y%m%d-%H%M%S")
        self.assertTrue(before <= stamp <= after, f"{stamp} is not between {before} and {after} UTC")
        with tarfile.open(os.path.join(self.out, name)) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("MANIFEST.txt"))
            manifest = tf.extractfile(member).read().decode()
        self.assertIn(f"created={stamp:%Y%m%d-%H%M%S} (UTC)", manifest)

    def _backup_into_quoted_folder(self, method):
        out = os.path.join(self.tmp, "it's backups")
        self.run_script(BACKUP, "--beets-config", self.beets, "--web-manager-data", self.wm,
                        "--out", out, method=method)
        archives = [n for n in os.listdir(out) if n.endswith(".tar.gz")]
        self.assertEqual(len(archives), 1, archives)
        with tarfile.open(os.path.join(out, archives[0])) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("beets/musiclibrary.blb"))
            data = tf.extractfile(member).read()
        self.assertTrue(data.startswith(b"SQLite format 3"))
        # Nothing was written next to the backup folder by a broken .backup target.
        self.assertEqual(sorted(os.listdir(self.tmp)), sorted(["beets", "web-manager", "it's backups"]))

    def test_backup_into_a_folder_with_a_quote_python(self):
        self._backup_into_quoted_folder("python")

    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 command not installed")
    def test_backup_into_a_folder_with_a_quote_uses_python_instead_of_sqlite3(self):
        self._backup_into_quoted_folder("sqlite3")

    # --- check-then-use (TOCTOU) races during a restore (#177) -------------

    def _shim(self, name, body):
        """Put a wrapper for command `name` first on PATH; it runs `body`
        (bash), then the real command. Returns env entries for run_script."""
        real = shutil.which(name)
        self.assertIsNotNone(real, name)
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(fakebin, exist_ok=True)
        path = os.path.join(fakebin, name)
        with open(path, "w", newline="\n") as f:
            f.write(f'#!/usr/bin/env bash\n{body}\nexec "{real}" "$@"\n')
        os.chmod(path, 0o755)
        return {"PATH": fakebin + os.pathsep + os.environ["PATH"]}

    def _closed_live(self):
        archive = self.backup("python")
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        return archive

    def test_restore_refuses_a_staging_folder_swapped_for_a_link(self):
        archive = self._closed_live()
        victim = os.path.join(self.tmp, "host-file")
        Path(victim).write_text("host-only-content", encoding="utf-8")
        atk = os.path.join(self.tmp, "atk")
        os.makedirs(atk)
        os.symlink(victim, os.path.join(atk, "item"))
        # A racer that can rename entries in the target folder swaps the
        # fresh staging folder for a link to its own folder, whose "item"
        # links to a host file.
        env = self._shim("mktemp", f'case "$*" in *.restore-stage.*) d="$("{shutil.which("mktemp")}" "$@")"; '
                                   f'mv "$d" "$d.away"; ln -s "{atk}" "$d"; echo "$d"; exit 0;; esac')
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", archive, check=False, env_extra=env)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("was replaced while the restore was running", res.stderr)
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "host-only-content",
                         "the restore copied through a swapped staging folder")
        self.assertEqual(os.listdir(atk), ["item"])

    def test_restore_refuses_a_keep_folder_swapped_for_a_link(self):
        archive = self._closed_live()
        victim_dir = os.path.join(self.tmp, "host-dir")
        os.makedirs(victim_dir)
        env = self._shim("mkdir", f'case "$*" in *.pre-restore-*) "{shutil.which("mkdir")}" "$@" || exit 1; '
                                  f'k="${{@: -1}}"; mv "$k" "$k.away"; ln -s "{victim_dir}" "$k"; exit 0;; esac')
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", archive, check=False, env_extra=env)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("was replaced while the restore was running", res.stderr)
        self.assertEqual(os.listdir(victim_dir), [], "a replaced file was moved through a swapped keep folder")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "chown needs root")
    def test_restored_files_keep_the_owner_of_what_they_replace(self):
        archive = self._closed_live()
        cfg = os.path.join(self.beets, "config.yaml")
        os.chown(cfg, 1234, 1235)
        os.chown(self.wm, 1000, 1001)
        new_names = [n for n in os.listdir(self.wm) if os.path.isfile(os.path.join(self.wm, n))]
        for name in new_names:
            os.remove(os.path.join(self.wm, name))
        self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm, "--yes", archive)
        st = os.stat(cfg)
        self.assertEqual((st.st_uid, st.st_gid), (1234, 1235), "config.yaml must keep its owner (PUID/PGID)")
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        restored = [n for n in new_names if os.path.exists(os.path.join(self.wm, n))]  # files in the backup
        self.assertTrue(restored)
        for name in restored:
            st = os.stat(os.path.join(self.wm, name))
            self.assertEqual((st.st_uid, st.st_gid), (1000, 1001), f"{name}: new files get the folder's owner")

    def test_restore_never_writes_through_a_link_planted_during_the_restore(self):
        archive = self.backup("python")
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        victim = os.path.join(self.tmp, "host-file")
        Path(victim).write_text("host-only-content", encoding="utf-8")
        os.chmod(victim, 0o644)
        planted = os.path.join(self.beets, "config.yaml")
        # Another writer swaps config.yaml for a link to a host file right
        # before every copy -- the window between move_aside and the copy.
        env = self._shim("cp", f'rm -f "{planted}"; ln -s "{victim}" "{planted}"')
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", archive, check=False, env_extra=env)
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "host-only-content",
                         "the restore wrote through a planted link")
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644, "chmod followed a planted link")
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("appeared while the restore was running", res.stderr)
        self.assertFalse([n for n in os.listdir(self.beets) if n.startswith(".restore-stage.")],
                         "staging folders are removed")

    def test_restore_extracts_the_same_archive_it_checked(self):
        first = self.backup("python")
        Path(self.wm, ".env").write_text("AI_MODEL=second-backup\n", encoding="utf-8")
        out2 = os.path.join(self.tmp, "backups2")
        self.run_script(BACKUP, "--beets-config", self.beets, "--web-manager-data", self.wm,
                        "--out", out2, method="python")
        (second,) = [os.path.join(out2, n) for n in os.listdir(out2) if n.endswith(".tar.gz")]
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        Path(self.wm, ".env").write_text("AI_MODEL=current\n", encoding="utf-8")
        # The archive file is replaced between the checks and the extraction.
        env = self._shim("tar", f'case " $* " in *" -xzf "*) cp "{second}" "{first}" ;; esac')
        self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                        "--yes", first, env_extra=env)
        self.assertEqual(Path(self.wm, ".env").read_text(encoding="utf-8"), "AI_MODEL=x\n",
                         "restore must extract the archive it listed and verified, not a swapped file")

    def test_restore_refuses_a_pre_restore_folder_it_did_not_create(self):
        archive = self.backup("python")
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.close()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        # Plant links for every stamp the restore could use in the next minute.
        now = datetime.datetime.now(datetime.timezone.utc)
        for sec in range(60):
            stamp = (now + datetime.timedelta(seconds=sec)).strftime("%Y%m%d-%H%M%S")
            os.symlink(elsewhere, os.path.join(self.wm, f".pre-restore-{stamp}"))
        res = self.run_script(RESTORE, "--beets-config", self.beets, "--web-manager-data", self.wm,
                              "--yes", archive, check=False)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("was not created by this restore", res.stderr)
        self.assertEqual(os.listdir(elsewhere), [], "nothing may be moved through a planted keep folder")


if __name__ == "__main__":
    unittest.main()
