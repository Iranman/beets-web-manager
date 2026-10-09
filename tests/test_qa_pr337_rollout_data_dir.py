"""QA for PR #337: rollback with a backup taken from another data source."""

import os
import unittest
from pathlib import Path

from tests.test_deploy_truenas_rollout import VersionedStackFixture


class OldBackupFromAnotherDataSourceTests(VersionedStackFixture):
    def test_qa_rollback_with_an_old_empty_source_backup_keeps_the_real_state(self):
        # A backup made the old way: the stray empty /data volume was backed up
        # (every state file recorded "absent"), while the app used /web-manager-data.
        anon = os.path.join(self.stack_dir, "anonymous-volume")
        os.makedirs(anon)
        st = self.load_state()
        cid = st["service_containers"]["beets-web-manager"]
        st["containers"][cid]["Mounts"].append({"Destination": "/data", "Source": anon})
        self.save_state(st)
        # No WEB_MANAGER_DATA_DIR, auth not enforced: backs up /data, deploys,
        # then fails post-deploy (the v0.2.0 failure the operator rolls back from).
        self.run_script(env=self.env())
        manifest = Path(self.backup_dir(), "state-manifest.txt").read_text(encoding="utf-8")
        self.assertIn("web-manager-data/.env absent", manifest)

        st = self.load_state()  # the container env the real stack always had
        cid = st["service_containers"]["beets-web-manager"]
        st["containers"][cid]["Config"]["Env"].append("WEB_MANAGER_DATA_DIR=/web-manager-data")
        self.save_state(st)
        res = self.run_script("--rollback", self.backup_dir())
        for name in (".env", ".auth_token", ".flask_secret_key", ".browser_password"):
            self.assertTrue(Path(self.webmgr_dir, name).exists(),
                            f"{name} moved out of the live data folder by the rollback:\n{res.stderr[-3000:]}")


from tests.test_deploy_truenas_rollout import DryRunTests  # noqa: E402


class DiscoveryEdgeTests(DryRunTests):
    def _layout(self, env, mounts):
        cont = self.state["containers"]["cid-webmgr"]
        dirs = {}
        for dest in mounts:
            d = os.path.join(self.stack_dir, "m" + dest.replace("/", "_"))
            os.makedirs(d, exist_ok=True)
            dirs[dest] = os.path.realpath(d).replace("\\", "/")
        cont["Mounts"] = [{"Destination": dest, "Source": dirs[dest]} for dest in mounts]
        cont["Config"]["Env"] = ["TZ=UTC", *env]
        self._save_state()
        return dirs

    def test_qa_trailing_slash_uses_the_exact_mount(self):
        dirs = self._layout(["WEB_MANAGER_DATA_DIR=/web-manager-data/"], ["/data", "/web-manager-data"])
        Path(dirs["/web-manager-data"], ".auth_token").write_text("t" * 32, encoding="utf-8")
        res = self.run_script("--dry-run", env=self.env(curl_auth_required=True))
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self.assertIn(f"web-manager data source: {dirs['/web-manager-data']}", res.stderr)

    def test_qa_nested_mount_picks_the_nested_one(self):
        dirs = self._layout(["WEB_MANAGER_DATA_DIR=/data/wm"], ["/data", "/data/wm"])
        Path(dirs["/data/wm"], ".auth_token").write_text("t" * 32, encoding="utf-8")
        res = self.run_script("--dry-run", env=self.env(curl_auth_required=True))
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self.assertIn(f"web-manager data source: {dirs['/data/wm']}", res.stderr)

    def test_qa_last_of_several_env_entries_wins_like_the_app(self):
        dirs = self._layout(["WEB_MANAGER_DATA_DIR=/data", "WEB_MANAGER_DATA_DIR=/web-manager-data"],
                            ["/data", "/web-manager-data"])
        Path(dirs["/web-manager-data"], ".auth_token").write_text("t" * 32, encoding="utf-8")
        res = self.run_script("--dry-run", env=self.env(curl_auth_required=True))
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self.assertIn(f"web-manager data source: {dirs['/web-manager-data']}", res.stderr)

    def test_qa_injection_values_are_refused_before_use(self):
        marker = os.path.join(self.tmp, "pwned")
        for value in ["/x';open(%r,'w').write('1');'" % marker, "/x$(touch %s)" % marker, "/x\nimport os",
                      "relative/path", "/x y", "/"]:
            self._layout([f"WEB_MANAGER_DATA_DIR={value}"], ["/data", "/web-manager-data"])
            res = self.run_script("--dry-run")
            self.assertNotEqual(res.returncode, 0, value)
            self.assertIn("Reason code:           webmgr_data_dir_unmounted", res.stderr, value)
            self.assertFalse(os.path.exists(marker), value)

    def test_qa_unreachable_app_fails_closed_without_token(self):
        self._layout([], ["/data"])
        res = self.run_script("--dry-run", env=self.env(curl_fail_paths=["/api/library"]))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Reason code:           webmgr_data_source_without_token", res.stderr)


def load_tests(loader, tests, pattern):  # only this module's QA tests, not the imported classes'
    suite = unittest.TestSuite()
    for cls in (OldBackupFromAnotherDataSourceTests, DiscoveryEdgeTests):
        for name in loader.getTestCaseNames(cls):
            if name.startswith("test_qa_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
