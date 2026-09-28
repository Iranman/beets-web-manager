"""ARCH-009 structural regressions: Release Group ID is canonical album
identity; Release ID is edition evidence and is never written into a Release
Group field."""

import json
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from backend.identity_contract import album_identity_from_payload, verify_album_identity
from backend.import_reconciliation import album_identity
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RG = "11111111-1111-1111-1111-111111111111"
RG2 = "22222222-2222-2222-2222-222222222222"
REL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REL_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
AUTHORITATIVE = {REL_A: RG, REL_B: RG}


def resolve(release_id):
    return AUTHORITATIVE.get(release_id, "")


class IdentityContractTests(unittest.TestCase):
    def test_release_id_never_written_into_release_group_field(self):
        ident = verify_album_identity("", REL_A, resolve_release_group=resolve)
        self.assertEqual(ident.release_group_id, RG)
        self.assertNotEqual(ident.release_group_id, REL_A)
        unknown = verify_album_identity("", "cccccccc-cccc-cccc-cccc-cccccccccccc", resolve_release_group=resolve)
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.release_group_id, "")

    def test_different_releases_same_release_group_are_the_same_album(self):
        a = verify_album_identity("", REL_A, resolve_release_group=resolve)
        b = verify_album_identity("", REL_B, resolve_release_group=resolve)
        self.assertEqual(a.release_group_id, b.release_group_id)
        self.assertNotEqual(a.release_id, b.release_id)

    def test_release_outside_stated_release_group_is_refused(self):
        ident = verify_album_identity(RG2, REL_A, resolve_release_group=resolve)
        self.assertEqual(ident.code, "release_not_in_release_group")

    def test_unverifiable_release_fails_closed(self):
        ident = verify_album_identity(RG, REL_A, resolve_release_group=lambda rel: "")
        self.assertEqual(ident.code, "release_group_unverified")

    def test_legacy_aliases_normalize_to_explicit_fields(self):
        ident = album_identity_from_payload({"mb_albumid": REL_A, "mb_releasegroupid": RG}, resolve_release_group=resolve)
        self.assertEqual((ident.release_group_id, ident.release_id), (RG, REL_A))
        ident = album_identity_from_payload({"rgid": RG}, resolve_release_group=resolve)
        self.assertEqual((ident.release_group_id, ident.release_id), (RG, ""))

    def test_release_group_required_by_default(self):
        self.assertEqual(verify_album_identity("", "", resolve_release_group=resolve).code, "release_group_required")


class ReconciliationAlbumIdentityTests(unittest.TestCase):
    def test_same_title_different_release_group_is_a_different_album(self):
        ident = album_identity({"album": "Hits", "mb_releasegroupid": RG}, {"album": "Hits", "mb_releasegroupid": RG2})
        self.assertFalse(ident.same_album)

    def test_same_release_id_does_not_substitute_for_unknown_release_group(self):
        ident = album_identity({"mb_albumid": REL_A, "mb_releasegroupid": ""}, {"mb_albumid": REL_A})
        self.assertFalse(ident.same_album)


class AddMbidsRouteTests(unittest.TestCase):
    def test_mismatched_release_is_refused_before_any_write(self):
        client = app_module.app.test_client()
        headers = {}
        with patch_app_family(app_module, "_mb_release_group_for_release", return_value=RG2), \
                mock.patch.object(app_module.composite_workflows, "update_album_metadata") as update, \
                mock.patch.object(app_module.app, "before_request_funcs", {}):
            resp = client.post("/api/albums/5/add-mbids", headers=headers, json={
                "mb_albumartistid": "33333333-3333-3333-3333-333333333333",
                "mb_releasegroupid": RG, "mb_albumid": REL_A,
            })
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "release_not_in_release_group")
        update.assert_not_called()


class ClientPayloadShapeTests(unittest.TestCase):
    def test_frontend_never_assigns_a_release_id_into_a_release_group_field(self):
        import re
        pattern = re.compile(
            r"(mb_releasegroupid|release_group_id|rgid)\s*[:=]\s*[^,;\n]*\b(mb_albumid|release_id|representative_release_id)\b"
        )
        offenders = []
        for path in (ROOT / "frontend" / "src").rglob("*.ts*"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_import_payload_sends_release_and_release_group_separately(self):
        src = (ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")
        self.assertIn("mb_albumid: representativeId,", src)
        self.assertIn("mb_releasegroupid: releaseGroupId || undefined,", src)


if __name__ == "__main__":
    unittest.main()
