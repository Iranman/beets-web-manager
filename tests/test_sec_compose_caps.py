"""BI-8: hardened compose variants must start the entrypoint with exactly
the capabilities its root phase needs, and the entrypoint must cope with a
read-only root filesystem (no /etc/passwd edits)."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (
    ROOT / "docker-compose.full.yml",
    ROOT / "examples" / "docker-compose.external-beets.yml",
)
ENTRYPOINT = (ROOT / "docker" / "web-manager-entrypoint.sh").read_text(encoding="utf-8")
REQUIRED_CAPS = {"CHOWN", "SETUID", "SETGID"}


def web_manager_service(path):
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]
    for service in services.values():
        if "beets-web-manager" in str(service.get("image", "")):
            return service
    raise AssertionError(f"no beets-web-manager service in {path}")


class HardenedComposeCapabilityTests(unittest.TestCase):
    def test_caps_are_dropped_then_minimal_set_added(self):
        for path in COMPOSE_FILES:
            with self.subTest(compose=path.name):
                svc = web_manager_service(path)
                self.assertEqual(svc.get("cap_drop"), ["ALL"])
                self.assertEqual(set(svc.get("cap_add") or []), REQUIRED_CAPS)

    def test_hardening_keys_are_kept(self):
        for path in COMPOSE_FILES:
            with self.subTest(compose=path.name):
                svc = web_manager_service(path)
                self.assertIs(svc.get("read_only"), True)
                self.assertIn("no-new-privileges:true", svc.get("security_opt") or [])


class EntrypointReadOnlyRootTests(unittest.TestCase):
    def test_falls_back_to_numeric_ids_when_passwd_is_read_only(self):
        self.assertIn('[ -w /etc/passwd ] && [ -w /etc/group ]', ENTRYPOINT)
        self.assertIn('RUN_AS="$PUID:$PGID"', ENTRYPOINT)
        self.assertIn('exec gosu "$RUN_AS" "$@"', ENTRYPOINT)

    def test_config_is_only_chowned_when_mounted(self):
        # The external-Beets example has no /config mount; chowning the
        # image's read-only /config aborted startup before BI-8.
        self.assertNotIn('chown -R "$RUN_AS" /web-manager-data /config', ENTRYPOINT)
        self.assertIn('$5 == "/config"', ENTRYPOINT)

    def test_owned_private_tree_is_not_walked(self):
        # #282: root without DAC_READ_SEARCH cannot list the app's 0700
        # tree, so a failed walk is tolerated only when the tree was already
        # ours; the walk itself always runs so default-capability starts still
        # repair stray root-owned files.
        self.assertIn("""[ "$(stat -c '%u:%g' "$1")" = "$PUID:$PGID" ]""", ENTRYPOINT)
        self.assertIn('if ! err="$(chown -R "$RUN_AS" "$1" 2>&1)"; then', ENTRYPOINT)
        self.assertIn('[ "$owned" = 1 ] && return 0', ENTRYPOINT)
        own_tree_body = ENTRYPOINT.split("own_tree() {", 1)[1].split("\n}", 1)[0]
        self.assertLess(own_tree_body.index("chown -R"), own_tree_body.index("return 0"))
        self.assertIn("own_tree /web-manager-data", ENTRYPOINT)
        self.assertIn("own_tree /config", ENTRYPOINT)
        workflow = (ROOT / ".github" / "workflows" / "docker-build.yml").read_text(encoding="utf-8")
        self.assertIn("docker/acceptance/hardened_restart.sh", workflow)

    def test_media_mount_chown_is_not_recursive(self):
        self.assertIn('chown "$RUN_AS" /music /downloads', ENTRYPOINT)
        self.assertNotIn("chown -R \"$RUN_AS\" /music", ENTRYPOINT)


if __name__ == "__main__":
    unittest.main()
