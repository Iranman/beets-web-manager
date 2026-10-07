"""#212: scripts/validate_compose_security.py must check every third-party
service image in the compose files, not only beets/beets-web-manager."""
import contextlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("validate_compose_security", ROOT / "scripts" / "validate_compose_security.py")
vcs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vcs)

FULL = (ROOT / "docker-compose.full.yml").read_text(encoding="utf-8")


def _run_with_full_compose(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "docker-compose.full.yml"
        path.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(vcs, "FULL_COMPOSE", path), contextlib.redirect_stdout(out):
            rc = vcs.main()
    return rc, json.loads(out.getvalue())["errors"]


class ThirdPartyImageTests(unittest.TestCase):
    def test_repository_compose_files_pass(self):
        rc, errors = _run_with_full_compose(FULL)
        self.assertEqual((rc, errors), (0, []))

    def test_sidecar_without_digest_fails(self):
        unpinned = re.sub(r"(brainicism/bgutil-ytdlp-pot-provider:[^@\s]+)@sha256:[0-9a-f]+", r"\1", FULL)
        self.assertNotEqual(unpinned, FULL, "fixture: the sidecar image line was not found")
        rc, errors = _run_with_full_compose(unpinned)
        self.assertEqual(rc, 1)
        self.assertTrue(any("bgutil-provider image is not digest-pinned" in e for e in errors), errors)

    def test_any_new_third_party_service_is_checked(self):
        text = FULL.replace("services:\n", "services:\n  redis:\n    image: redis:7\n", 1)
        rc, errors = _run_with_full_compose(text)
        self.assertEqual(rc, 1)
        self.assertTrue(any("redis image is not digest-pinned" in e for e in errors), errors)

    def test_project_services_and_source_builds_are_not_third_party(self):
        errors = []
        vcs._check_third_party_images(
            "services:\n"
            "  beets:\n    image: lscr.io/linuxserver/beets:latest\n"
            "  beets-web-manager:\n    image: ghcr.io/iranman/beets-web-manager:latest\n"
            "  helper:\n    build: .\n    image: helper:dev\n"
            "volumes:\n  cache:\n",
            "x.yml", errors)
        self.assertEqual(errors, [])

    def test_service_after_a_column_0_comment_is_checked(self):
        errors = []
        vcs._check_third_party_images(
            "services:\n  beets:\n    image: lscr.io/linuxserver/beets:latest\n"
            "# optional sidecar\n  sidecar:\n    image: foo:1.0\n"
            "volumes:\n  data:\n", "x.yml", errors)
        self.assertEqual(errors, ["x.yml: sidecar image is not digest-pinned: foo:1.0"])

    def test_services_indented_by_four_spaces_are_checked(self):
        errors = []
        vcs._check_third_party_images(
            "services:\n    beets:\n        image: lscr.io/linuxserver/beets:latest\n"
            "    sidecar:\n        image: foo:1.0\n", "x.yml", errors)
        self.assertEqual(errors, ["x.yml: sidecar image is not digest-pinned: foo:1.0"])

    def test_top_level_keys_after_services_are_not_services(self):
        errors = []
        vcs._check_third_party_images(
            "services:\n  sidecar:\n    image: foo:1.0@sha256:" + "a" * 64 + "\n"
            "networks:\n  backend:\n    driver: bridge\n", "x.yml", errors)
        self.assertEqual(errors, [])

    def test_service_without_image_fails(self):
        errors = []
        vcs._check_third_party_images("services:\n  sidecar:\n    restart: always\n", "x.yml", errors)
        self.assertEqual(errors, ["x.yml: sidecar image is missing"])


if __name__ == "__main__":
    unittest.main()
