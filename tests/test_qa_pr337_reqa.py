"""QA re-check for PR #337 (fe72c14): rollback data-source binding edges."""

import hashlib
import os
import re
import shutil
import unittest
from pathlib import Path

from tests.test_deploy_truenas_rollout import VersionedStackFixture

LEGACY_FILES = {".env.bak", "auth_token.bak", "authoritative-db-metadata.txt", "container-inspect-before.json",
                "docker-compose.yml.bak", "previous-image-labels.json", "previous-image.txt",
                "resolved-compose-config.json", "stale-database", "token-metadata.txt"}


class RollbackDataSourceReQA(VersionedStackFixture):
    def _canon(self, p):
        return os.path.realpath(p).replace("\\", "/")

    def _sha(self, p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    def _live(self):
        return {n: Path(self.webmgr_dir, n).read_bytes() for n in sorted(os.listdir(self.webmgr_dir))
                if Path(self.webmgr_dir, n).is_file()}

    def _set_mount_source(self, src):
        st = self.load_state()
        cid = st["service_containers"]["beets-web-manager"]
        for m in st["containers"][cid]["Mounts"]:
            if m["Destination"] == "/web-manager-data":
                m["Source"] = src
        self.save_state(st)

    def _make_legacy(self, token_path=None):
        """Turn the backup into the shape the 2026-09-29 script wrote (the
        host's 20261004 backup): no state-manifest.txt, no web-manager-data/
        or beets-config/, token-metadata.txt with persistent_token_path."""
        b = self.backup_dir()
        for n in os.listdir(b):
            if n not in LEGACY_FILES:
                p = os.path.join(b, n)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        meta = Path(b, "token-metadata.txt")
        text = meta.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^persistent_token_path=")
        if token_path is not None:
            text = re.sub(r"(?m)^persistent_token_path=.*$", f"persistent_token_path={token_path}", text)
        meta.write_text(text, encoding="utf-8")
        return b

    def _refused_untouched(self, res, state_before, live_before):
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           backup_data_source_mismatch", res.stdout + res.stderr)
        self.assertEqual(Path(self.state_path).read_text(encoding="utf-8"), state_before, "a container was touched")
        self.assertEqual(self._live(), live_before)

    # 1. legacy backups ------------------------------------------------------
    def test_qa_legacy_backup_with_matching_token_path_rolls_back(self):
        self.deploy()
        b = self._make_legacy()
        live = self._live()
        res = self.run_script("--rollback", b, "--allow-legacy-backup")
        self.assertEqual(res.returncode, 0, res.stderr[-3000:])
        self.assertIn("Backup data folder matches the current one", res.stderr)
        self.assertEqual(self.webmgr_container()["Image"], "sha256:oldimageid")
        self.assertEqual(self._live(), live, "a legacy rollback changes no Web Manager state file")

    def test_qa_legacy_backup_from_another_folder_is_refused_before_stop(self):
        self.deploy()
        other = os.path.join(self.stack_dir, "anonymous-volume")
        os.makedirs(other)
        b = self._make_legacy(token_path=self._canon(other) + "/.auth_token")
        state, live = Path(self.state_path).read_text(encoding="utf-8"), self._live()
        self._refused_untouched(self.run_script("--rollback", b, "--allow-legacy-backup"), state, live)

    # 2. an edited webmgr_data_src line -------------------------------------
    def test_qa_edited_data_src_line_only_refuses(self):
        self.deploy()
        m = Path(self.backup_dir(), "state-manifest.txt")
        for bogus in ("/elsewhere", self._canon(self.webmgr_dir) + "/../x", "$(touch /tmp/qa-pwned)",
                      self._canon(self.webmgr_dir) + "/", ""):
            m.write_text(re.sub(r"(?m)^webmgr_data_src=.*$", f"webmgr_data_src={bogus}",
                                m.read_text(encoding="utf-8")), encoding="utf-8")
            state, live = Path(self.state_path).read_text(encoding="utf-8"), self._live()
            res = self.run_script("--rollback", self.backup_dir())
            if bogus == "":  # empty: falls back to token-metadata, which matches
                self.assertEqual(res.returncode, 0, res.stderr[-2000:])
                return
            self._refused_untouched(res, state, live)
            self.assertFalse(os.path.exists("/tmp/qa-pwned"))

    # 3. canonical comparison ----------------------------------------------
    def test_qa_symlinked_and_trailing_slash_mount_source_still_matches(self):
        self.deploy()
        link = os.path.join(self.stack_dir, "wm-link")
        os.symlink(self.webmgr_dir, link)
        for src in (link, self.webmgr_dir + "/", link + "/"):
            self._set_mount_source(src)
            res = self.run_script("--dry-run", env=self.env())
            self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self._set_mount_source(link + "/")
        res = self.run_script("--rollback", self.backup_dir())
        self.assertEqual(res.returncode, 0, res.stderr[-3000:])
        self.assertIn("Backup data folder matches the current one", res.stderr)

    def test_qa_symlink_retargeted_since_backup_is_refused(self):
        real_a = self._canon(self.webmgr_dir)
        link = os.path.join(self.stack_dir, "wm-link")
        os.symlink(real_a, link)
        self._set_mount_source(link)
        self.deploy()
        other = os.path.join(self.stack_dir, "wm-other")
        shutil.copytree(self.webmgr_dir, other)
        os.remove(link)
        os.symlink(other, link)  # same spelled path, another folder
        state = Path(self.state_path).read_text(encoding="utf-8")
        res = self.run_script("--rollback", self.backup_dir())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           backup_data_source_mismatch", res.stdout + res.stderr)
        self.assertEqual(Path(self.state_path).read_text(encoding="utf-8"), state)

    # 4. restore-only never moves live files --------------------------------
    def test_qa_restore_only_never_moves_live_files(self):
        self.deploy()
        b = self.backup_dir()
        m = Path(b, "state-manifest.txt")
        meta = Path(b, "token-metadata.txt")
        old_sha = self._sha(meta)
        meta.write_text(re.sub(r"(?m)^persistent_token_path=.*\n", "", meta.read_text(encoding="utf-8")),
                        encoding="utf-8")
        text = re.sub(r"(?m)^webmgr_data_src=.*\n", "", m.read_text(encoding="utf-8"))
        m.write_text(text.replace(f"token-metadata.txt sha256={old_sha}",
                                  f"token-metadata.txt sha256={self._sha(meta)}"), encoding="utf-8")
        absent = [ln.split("/", 1)[1].rsplit(" ", 1)[0] for ln in m.read_text(encoding="utf-8").splitlines()
                  if ln.startswith("web-manager-data/") and ln.endswith(" absent")]
        self.assertTrue(absent, "fixture needs a state file the backup recorded as absent")
        for n in absent:
            Path(self.webmgr_dir, n).write_text("live-" + n, encoding="utf-8")
        Path(self.webmgr_dir, ".env").write_text("AI_MODEL=after-deploy\n", encoding="utf-8")
        res = self.run_script("--rollback", b)
        self.assertEqual(res.returncode, 0, res.stderr[-3000:])
        self.assertIn("does not record which folder", res.stderr)
        self.assertNotIn("aside", res.stderr.split("does not record which folder", 1)[1].split("Rollback complete")[0]
                         .replace("files it did not hold are left in place", ""))
        for n in absent:
            self.assertEqual(Path(self.webmgr_dir, n).read_text(encoding="utf-8"), "live-" + n)
        self.assertEqual(Path(self.webmgr_dir, ".env").read_text(encoding="utf-8"), "AI_MODEL=before-deploy\n")


if __name__ == "__main__":
    unittest.main()
