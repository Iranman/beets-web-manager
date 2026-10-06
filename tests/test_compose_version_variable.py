"""The published Compose files, .env.example and the install docs agree on
how the Web Manager image tag is chosen: BEETS_WEB_MANAGER_VERSION, default
`stable`. The rollout script and rollback rewrite that variable in the stack
.env, so a Compose file with a hard-coded tag would silently ignore them."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_IMAGE = "ghcr.io/iranman/beets-web-manager:${BEETS_WEB_MANAGER_VERSION:-stable}"
IMAGE_LINE = re.compile(r"^\s*image:\s*(ghcr\.io/iranman/beets-web-manager\S*)\s*$", re.M)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class ComposeVersionVariableTests(unittest.TestCase):
    def test_compose_files_select_the_image_with_the_version_variable(self):
        for rel in ("docker-compose.yml", "docker-compose.full.yml", "examples/docker-compose.external-beets.yml"):
            with self.subTest(file=rel):
                images = IMAGE_LINE.findall(_read(rel))
                self.assertEqual(images, [EXPECTED_IMAGE])

    def test_documented_compose_samples_match(self):
        for rel in ("README.md", "docs/INSTALLATION.md", "docs/EXAMPLES.md"):
            with self.subTest(file=rel):
                images = IMAGE_LINE.findall(_read(rel))
                self.assertTrue(images, f"{rel} has no Web Manager compose sample")
                self.assertEqual(set(images), {EXPECTED_IMAGE})

    def test_env_example_and_configuration_document_the_variable(self):
        self.assertRegex(_read(".env.example"), r"(?m)^BEETS_WEB_MANAGER_VERSION=stable$")
        self.assertIn("`BEETS_WEB_MANAGER_VERSION`", _read("docs/CONFIGURATION.md"))

    def test_no_doc_hard_codes_the_stable_tag(self):
        for rel in ("README.md", "docs/INSTALLATION.md", "docs/EXAMPLES.md", "docs/TROUBLESHOOTING.md", ".env.example"):
            with self.subTest(file=rel):
                self.assertNotIn("beets-web-manager:stable", _read(rel))


if __name__ == "__main__":
    unittest.main()
