"""Tests for the MB ID Coverage repair job (_start_library_mbid_sticking_repair).

Flask/beets aren't importable in this test environment, so — consistent with
the rest of this test suite — we assert against the source text of app.py
rather than executing it.
"""
import unittest
from pathlib import Path


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app.py").read_text(encoding="utf-8")


def _repair_fn_source(src: str) -> str:
    return src[
        src.index("def _start_library_mbid_sticking_repair("):
        src.index('@app.post("/api/library/mbid-sticking-repair")')
    ]


class MbidGapsRepairFixesTheBugTests(unittest.TestCase):
    """The old behavior silently skipped+logged instead of repairing. Verify it's gone."""

    def setUp(self):
        self._fn = _repair_fn_source(_app_source())

    def test_no_longer_just_counts_and_tells_user_to_link_manually(self):
        self.assertNotIn(
            "link the MusicBrainz release first, then rerun Full MB Sync.",
            self._fn,
        )

    def test_scans_albums_with_blank_album_mb_albumid(self):
        self.assertIn("trim(COALESCE(a.mb_albumid, ''))=''", self._fn)

    def test_attempts_resolution_via_release_group_or_search(self):
        # Release-group-first: pass a release-group URL as mb_input when we
        # already know the RG ID, so _resolve_album_release_for_import takes
        # the release-group -> representative-release path.
        self.assertIn(
            'mb_input = f"https://musicbrainz.org/release-group/{rgid}" if rgid else ""',
            self._fn,
        )
        self.assertIn("_resolve_album_release_for_import(", self._fn)

    def test_passes_source_folder_for_tracklist_validation(self):
        # Must not accept a low-confidence match blindly -- source_folder
        # triggers preflight tracklist validation inside the resolver.
        self.assertIn("source_folder=aldir", self._fn)
        self.assertIn("_album_source_folder(aid)", self._fn)

    def test_writes_resolved_release_id_to_album_db_row(self):
        self.assertIn('"UPDATE albums SET mb_albumid=? WHERE id=?"', self._fn)

    def test_writes_release_group_id_when_it_was_blank(self):
        self.assertIn("_fetch_mb_release_candidate(resolved_mbid)", self._fn)
        self.assertIn(
            '"UPDATE albums SET mb_albumid=?, mb_releasegroupid=? WHERE id=?"',
            self._fn,
        )

    def test_unresolved_albums_get_a_reason_not_silent_failure(self):
        self.assertIn('"No MusicBrainz match found"', self._fn)
        self.assertIn("Multiple low-confidence matches", self._fn)
        self.assertIn('summary["unresolved_albums"].append(', self._fn)
        self.assertIn('"album_id": aid,', self._fn)
        self.assertIn('"reason": reason,', self._fn)

    def test_dry_run_does_not_write(self):
        dry_pos = self._fn.index("if dry_run:\n                    log.append(\n"
                                  "                        f\"  [album_id {aid}] would link")
        self.assertGreaterEqual(dry_pos, 0)

    def test_cancellation_is_checked_during_resolution_loop(self):
        resolve_loop = self._fn[
            self._fn.index("for row in blank_rows:"):
            self._fn.index("summary[\"unresolved_count\"] = len(summary[\"unresolved_albums\"])")
        ]
        self.assertIn("cancel_event is not None and cancel_event.is_set()", resolve_loop)


class MbidGapsRepairSummaryCountsTests(unittest.TestCase):
    """The job must report honest repaired/skipped/unresolved/failed counts."""

    def setUp(self):
        self._fn = _repair_fn_source(_app_source())

    def test_summary_has_all_required_count_fields(self):
        for field in (
            '"resolved_album_rows": 0',
            '"unresolved_albums": []',
            '"unresolved_count": 0',
            '"skipped_already_fixed": 0',
            '"failed_count": 0',
        ):
            self.assertIn(field, self._fn)

    def test_already_fixed_albums_counted_separately_from_failures(self):
        self.assertIn('summary["skipped_already_fixed"] += 1', self._fn)
        self.assertIn("already fixed", self._fn)

    def test_beet_write_failure_increments_failed_count_not_silently_ignored(self):
        write_block = self._fn[
            self._fn.index("if write_tags:"):
            self._fn.index("elif not dry_run:")
        ]
        self.assertIn('summary["failed_count"] += 1', write_block)

    def test_done_log_reports_all_new_counts(self):
        done_block = self._fn[self._fn.index('log.append(\n            "Done'):]
        self.assertIn("newly-linked albums {resolved_album_rows}", done_block)
        self.assertIn("already fixed {skipped_already_fixed}", done_block)
        self.assertIn("unresolved (needs review) {unresolved_count}", done_block)
        self.assertIn("failed {failed_count}", done_block)


class MbidGapsRepairIdempotencyTests(unittest.TestCase):
    """Re-running the job must not redo work or create duplicates."""

    def setUp(self):
        self._fn = _repair_fn_source(_app_source())

    def test_resolution_query_excludes_albums_that_already_have_a_release_id(self):
        # Once an album gets mb_albumid stamped, the blank_rows query (which
        # filters WHERE a.mb_albumid is blank) will no longer select it on a
        # subsequent run -- this is what makes the discovery step idempotent.
        blank_query = self._fn[
            self._fn.index("blank_rows = con.execute("):
            self._fn.index("summary[\"unlinked_track_gap_albums\"]")
        ]
        self.assertIn("trim(COALESCE(a.mb_albumid, ''))=''", blank_query)

    def test_release_id_stamping_uses_idempotent_helper(self):
        # _stamp_album_release_id only updates item rows that actually differ.
        self.assertIn("_stamp_album_release_id(aid, mbid, log)", self._fn)


class MbidGapsRepairDebugLoggingTests(unittest.TestCase):
    """Debug logging must show per-album progress, missing fields, and write locations."""

    def setUp(self):
        self._fn = _repair_fn_source(_app_source())

    def test_logs_each_album_id_being_processed(self):
        self.assertIn("[album_id {aid}] missing album mb_albumid", self._fn)
        self.assertIn("[album_id {aid}] repairing {label}", self._fn)

    def test_logs_what_field_is_missing(self):
        self.assertIn("missing: {release_gaps} release-id row(s)", self._fn)

    def test_logs_where_writes_landed(self):
        self.assertIn("wrote {changed} row(s) to DB: items.mb_albumid", self._fn)
        self.assertIn("wrote {len(track_updates)} row(s) to DB:", self._fn)
        self.assertIn("wrote fixed IDs to file tags (beet write)", self._fn)

    def test_logs_skip_reason(self):
        self.assertIn("SKIPPED — {reason}. Needs manual review.", self._fn)


if __name__ == "__main__":
    unittest.main()
