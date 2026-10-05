"""Round-trip tests for scripts/backup.sh and scripts/restore.sh.

These run the real scripts with bash against temporary directories that
stand in for a stack's ./beets (/config) and ./web-manager
(/web-manager-data) folders. The Beets database is a real SQLite file in
WAL mode with a connection held open and committed rows still in the -wal
file -- the state of a library while Beets is running -- so the test proves
the online backup captures them without modifying the live database.

Linux/macOS only (POSIX bash, tar); skipped on Windows.
"""
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

    def run_script(self, script, *args, method=None, check=True):
        env = dict(os.environ)
        if method:
            env["BWM_BACKUP_FORCE_METHOD"] = method
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


if __name__ == "__main__":
    unittest.main()
