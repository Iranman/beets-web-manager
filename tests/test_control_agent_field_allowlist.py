"""Regression tests for the PATCH /items and /albums column-name allowlist.

PATCH /items/<id> and /albums/<id> build a SQL SET clause from the JSON
`fields` keys the caller supplies: `set_clause = ", ".join(f"{k} = ?" for k
in fields.keys())`. SQL parameters can only bind values, not column names,
so before this fix an arbitrary key -- e.g. `"x=1); DROP TABLE items; --"`
-- was interpolated straight into the SQL text unfiltered (CodeQL
py/sql-injection). The web-manager layer (app.py's EDITABLE_FIELDS) already
filtered this before this fix, but the control agent is a separate,
independently-authenticated network service and must not depend solely on
the caller's honesty. `_invalid_field_names()` is the fix: it rejects any
key not in a fixed per-table allowlist before the SET clause is built.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.beets_control_agent import (  # noqa: E402
    ALBUM_EDITABLE_FIELDS,
    ITEM_EDITABLE_FIELDS,
    _invalid_field_names,
)


class FieldAllowlistTests(unittest.TestCase):
    def test_known_item_fields_pass(self):
        # SEC-002 / ARCH-003 Wave 23 final review, findings #6/#38:
        # mb_trackid (and the other MusicBrainz identity fields) were
        # removed from this allowlist -- canonical identity mutation must
        # go through a controlled transaction family, never this generic,
        # untransacted endpoint. See test_identity_fields_are_not_editable
        # below for the regression coverage of that removal.
        fields = {"title": "New Title", "year": 2020, "genre": "Rock"}
        self.assertEqual(_invalid_field_names(fields, ITEM_EDITABLE_FIELDS), [])

    def test_identity_fields_are_not_editable(self):
        """SEC-002 / ARCH-003 Wave 23 final review, findings #6/#7/#38:
        MusicBrainz identity fields must never be editable through this
        generic, untransacted PATCH endpoint -- no Plan/Apply/Verify/
        Rollback, no TOCTOU, no identity-conflict check, just a direct SQL
        UPDATE under a filesystem lock. Canonical identity mutation must go
        through a controlled transaction family instead."""
        item_identity_fields = {"mb_trackid", "mb_albumid", "mb_artistid", "mb_releasegroupid"}
        album_identity_fields = {"mb_albumid", "mb_albumartistid", "mb_releasegroupid"}
        self.assertEqual(item_identity_fields & ITEM_EDITABLE_FIELDS, set())
        self.assertEqual(album_identity_fields & ALBUM_EDITABLE_FIELDS, set())

    def test_known_album_fields_pass(self):
        fields = {"album": "New Album", "genre": "Rock"}
        self.assertEqual(_invalid_field_names(fields, ALBUM_EDITABLE_FIELDS), [])

    def test_sql_injection_shaped_key_is_rejected_for_items(self):
        fields = {"title = 'x' WHERE 1=1; DROP TABLE items; --": "value"}
        invalid = _invalid_field_names(fields, ITEM_EDITABLE_FIELDS)
        self.assertEqual(len(invalid), 1)

    def test_sql_injection_shaped_key_is_rejected_for_albums(self):
        fields = {"1=1) OR (1=1": "value"}
        invalid = _invalid_field_names(fields, ALBUM_EDITABLE_FIELDS)
        self.assertEqual(len(invalid), 1)

    def test_internal_bookkeeping_column_is_not_editable(self):
        # id/path columns must never be reachable through this endpoint,
        # even though they are real column names (not a SQL-shaped attack).
        for internal in ("id", "path", "added"):
            self.assertNotIn(internal, ITEM_EDITABLE_FIELDS)
            self.assertNotIn(internal, ALBUM_EDITABLE_FIELDS)

    def test_mixed_valid_and_invalid_keys_reports_only_invalid(self):
        fields = {"title": "ok", "'; DROP TABLE items; --": "bad"}
        invalid = _invalid_field_names(fields, ITEM_EDITABLE_FIELDS)
        self.assertEqual(invalid, ["'; DROP TABLE items; --"])

    def test_empty_fields_has_no_invalid_keys(self):
        self.assertEqual(_invalid_field_names({}, ITEM_EDITABLE_FIELDS), [])


if __name__ == "__main__":
    unittest.main()
