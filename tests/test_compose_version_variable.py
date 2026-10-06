"""The shipped Compose files and the install docs deploy the Web Manager
image by the literal moving tag `ghcr.io/iranman/beets-web-manager:latest`
(a product decision: users update with `docker compose pull`, and pin by
editing the tag themselves). No shipped Compose file may pick the tag through
`${BEETS_WEB_MANAGER_VERSION}`; the rollout script supports the literal tag
without editing the Compose file or .env."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_IMAGE = "ghcr.io/iranman/beets-web-manager:latest"
IMAGE_LINE = re.compile(r"^\s*image:\s*(ghcr\.io/iranman/beets-web-manager\S*)\s*$", re.M)
SHIPPED_COMPOSE = ("docker-compose.yml", "docker-compose.full.yml", "examples/docker-compose.external-beets.yml")
DOCS = ("README.md", "docs/INSTALLATION.md", "docs/EXAMPLES.md", "docs/TROUBLESHOOTING.md", "docs/TRUENAS_ROLLOUT.md")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class ComposeLatestImageTests(unittest.TestCase):
    def test_shipped_compose_files_use_the_literal_latest_image(self):
        for rel in SHIPPED_COMPOSE:
            with self.subTest(file=rel):
                self.assertEqual(IMAGE_LINE.findall(_read(rel)), [EXPECTED_IMAGE])

    def test_no_shipped_compose_file_uses_the_version_variable(self):
        for rel in SHIPPED_COMPOSE:
            with self.subTest(file=rel):
                self.assertNotIn("${BEETS_WEB_MANAGER_VERSION", _read(rel))

    def test_documented_compose_samples_match(self):
        for rel in ("README.md", "docs/INSTALLATION.md", "docs/EXAMPLES.md"):
            with self.subTest(file=rel):
                images = IMAGE_LINE.findall(_read(rel))
                self.assertTrue(images, f"{rel} has no Web Manager compose sample")
                self.assertEqual(set(images), {EXPECTED_IMAGE})

    def test_no_doc_or_env_example_selects_the_image_through_the_variable(self):
        for rel in DOCS + (".env.example",):
            with self.subTest(file=rel):
                text = _read(rel)
                self.assertNotIn("beets-web-manager:${BEETS_WEB_MANAGER_VERSION", text)
                self.assertNotIn("beets-web-manager:stable", text)
        self.assertNotRegex(_read(".env.example"), r"(?m)^BEETS_WEB_MANAGER_VERSION=")


if __name__ == "__main__":
    unittest.main()
