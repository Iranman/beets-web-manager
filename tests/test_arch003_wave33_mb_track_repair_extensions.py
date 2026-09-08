"""SEC-002 / ARCH-003 Wave 33: album_mb_track_repair_v1 extensions.

Attempts (rather than re-deferring) the extension the Wave 32 audit
identified as the actual blocker for migrating app.py's
_match_tracks_from_mb_shared() -- adds, all opt-in via `payload` and
default-off so every existing caller/test for this family is unaffected
(see tests.test_sec002_wave19_mb_track_repair, which passes unchanged):

1. `target_tracks`: restrict matching to a caller-supplied subset of the
   release's tracklist, using the same shared
   release_track_matches_missing_target() guard app.py's own
   _match_tracks_from_mb_shared() already uses (backend/import_guard.py --
   confirmed, contrary to Wave 31's original claim, trivially importable
   here with zero circular-dependency risk).
2. `acoustid_verify`: cross-check a fuzzy title/position/duration match
   against the file's AcoustID fingerprint before trusting it enough to
   auto-repair, mirroring app.py's _album_track_fingerprint_check() policy.
3. `zero_unmatched`: zero the `track` column for items that align to
   nothing on the (possibly filtered) release, mirroring app.py's
   zero_unmatched contract.
4. `allow_establish_release_group`: an explicit, narrow opt-in to let a
   caller establish an album's mb_releasegroupid as part of this repair
   (refused by default -- repair_rg_not_established -- for every other
   caller). Real conflicts (repair_identity_mismatch) are never
   bypassable by this flag.
"""
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    create_album_mb_track_repair_plan,
    execute_album_mb_track_repair_apply,
    rollback_album_mb_track_repair,
)
from tests.test_sec002_wave19_mb_track_repair import (
    RG_A,
    REL_A,
    REC_TARGET_1,
    REC_TARGET_2,
    Wave19FixtureBase,
    _fake_tracklist_a,
    _write_test_audio,
)


class TargetTracksSubsetTests(Wave19FixtureBase):
    def test_target_tracks_restricts_matching_to_requested_subset(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {
                "album_id": 1,
                "target_tracks": [{"disc": 1, "track": 1, "mb_trackid": REC_TARGET_1}],
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        # Only track 1 was requested -- only 1 repair row planned, not 2.
        self.assertEqual(res["updated"], 1)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(len(tx["metadata"]["payload"]["tracks_to_repair"]), 1)
        self.assertEqual(
            tx["metadata"]["payload"]["tracks_to_repair"][0]["after"]["mb_trackid"],
            REC_TARGET_1,
        )

    def test_target_tracks_none_found_is_a_precise_rejection(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {
                "album_id": 1,
                "target_tracks": [{"disc": 9, "track": 99, "title": "Nonexistent Track"}],
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_target_tracks_not_found")

    def test_omitting_target_tracks_matches_full_release_unchanged(self):
        """Regression: not passing target_tracks at all must behave
        exactly as before this extension (both tracks matched)."""
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 2)


class AcoustidVerifyTests(Wave19FixtureBase):
    def test_acoustid_mismatch_excludes_row_from_repair(self):
        self._create_album_and_items(album_id=1)
        # Every candidate's mb_trackid is unrelated to the release, and its
        # title similarity to any release track title is low -- a clean
        # "mismatch" (score >= 70, title_score < 0.72).
        lookup = lambda _path: [
            {"mb_trackid": "99999999-9999-9999-9999-999999999999", "title": "Totally Unrelated Song", "score": 95},
        ]
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "acoustid_verify": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
            acoustid_lookup_fn=lookup,
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 0)
        self.assertEqual(res["acoustid_rejected"], 2)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(len(tx["metadata"]["payload"]["tracks_to_repair"]), 0)
        self.assertEqual(len(tx["metadata"]["payload"]["acoustid_rejected"]), 2)

    def test_acoustid_match_allows_repair_to_proceed(self):
        self._create_album_and_items(album_id=1)
        def lookup(path):
            # Confirm whichever recording id the release track actually
            # has -- a real "match" status.
            if "track1" in str(path):
                return [{"mb_trackid": REC_TARGET_1, "title": "Track 1 Fixed Title", "score": 95}]
            return [{"mb_trackid": REC_TARGET_2, "title": "Track 2 Fixed Title", "score": 95}]
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "acoustid_verify": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
            acoustid_lookup_fn=lookup,
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 2)
        self.assertEqual(res["acoustid_rejected"], 0)

    def test_acoustid_unavailable_falls_through_to_trusting_fuzzy_match(self):
        """fpcalc/lookup failure (None) must not block repair -- same
        fail-open-to-fuzzy-trust policy as app.py's version."""
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "acoustid_verify": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
            acoustid_lookup_fn=lambda _path: None,
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 2)

    def test_acoustid_verify_false_never_calls_lookup_fn(self):
        """Default-off: acoustid_verify absent must never even call the
        lookup function -- proves this is a real opt-in, not silently
        always-on."""
        self._create_album_and_items(album_id=1)
        lookup = mock.Mock(return_value=[])
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
            acoustid_lookup_fn=lookup,
        )
        self.assertTrue(res.get("ok"), res)
        lookup.assert_not_called()


class ZeroUnmatchedTests(Wave19FixtureBase):
    def test_zero_unmatched_zeroes_track_for_unaligned_item(self):
        self._create_album_and_items(album_id=1)
        # Restrict to only track 1 -- track 2's item now aligns to nothing
        # (target_tracks filtered it out of the release entirely).
        res = create_album_mb_track_repair_plan(
            self.store,
            {
                "album_id": 1,
                "target_tracks": [{"disc": 1, "track": 1, "mb_trackid": REC_TARGET_1}],
                "zero_unmatched": True,
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["zero_unmatched_rows"], 1)
        op_id = res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertTrue(apply_res.get("ok"), apply_res)

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT track FROM items WHERE id=2").fetchone()
        con.close()
        self.assertEqual(row["track"], 0)

    def test_zero_unmatched_rollback_restores_original_track_number(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {
                "album_id": 1,
                "target_tracks": [{"disc": 1, "track": 1, "mb_trackid": REC_TARGET_1}],
                "zero_unmatched": True,
            },
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = res["operation_id"]
        execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        rb_res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rb_res.get("ok"), rb_res)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT track FROM items WHERE id=2").fetchone()
        con.close()
        self.assertEqual(row["track"], 2)

    def test_zero_unmatched_false_never_zeroes_anything(self):
        """Regression: default-off must behave exactly as before."""
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "target_tracks": [{"disc": 1, "track": 1, "mb_trackid": REC_TARGET_1}]},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("zero_unmatched_rows", 0), 0)


class EstablishReleaseGroupTests(Wave19FixtureBase):
    def _create_blank_rg_album(self, album_id=1):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
            (album_id, "Test Album Title", "Test Artist", REL_A, "", 2024),
        )
        from tests.test_sec002_wave19_mb_track_repair import _write_test_audio
        album_dir = self.music_root / f"album_{album_id}"
        album_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in (1, 2):
            p = album_dir / f"track{i}.wav"
            _write_test_audio(p, freq=220.0 * i, mb_albumid=REL_A, title=f"Track {i} Old Title", track=i, disc=1)
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (i, album_id, f"Track {i} Old Title", "Test Artist", "Test Album Title", "Test Artist", 1, i, str(p), "", REL_A, "", 180.0),
            )
            paths.append(p)
        con.commit()
        con.close()
        return paths

    def test_blank_rg_is_still_rejected_by_default(self):
        """Regression: the family's core safety guarantee for every
        caller that does NOT opt in must be completely unchanged."""
        self._create_blank_rg_album(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_rg_not_established")

    def test_allow_establish_release_group_plans_and_applies_establishment(self):
        self._create_blank_rg_album(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "allow_establish_release_group": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("establishing_release_group"))
        op_id = res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertTrue(apply_res.get("established_release_group"))

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT mb_releasegroupid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["mb_releasegroupid"], RG_A)

    def test_establish_release_group_rollback_restores_blank(self):
        self._create_blank_rg_album(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "allow_establish_release_group": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = res["operation_id"]
        execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        rb_res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rb_res.get("ok"), rb_res)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT mb_releasegroupid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["mb_releasegroupid"], "")

    def test_allow_establish_release_group_never_bypasses_a_real_conflict(self):
        """The opt-in flag only ever covers "not yet set" -- a genuine
        conflict (album already has a DIFFERENT RG) must still be
        refused, exactly as before this extension."""
        self._create_album_and_items(album_id=1, rg_id=RG_A)
        RG_B = "bbbbbbbb-0000-0000-0000-000000000000"
        REL_B = "22222222-2222-2222-2222-222222222222"
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "mb_albumid": REL_B, "allow_establish_release_group": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(rel_id=REL_B, rg_id=RG_B),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_identity_mismatch")

    def test_apply_fails_closed_if_rg_established_by_someone_else_since_plan(self):
        """TOCTOU: if the album's RG stopped being blank between Plan and
        Apply (a concurrent, unrelated edit), Apply must refuse rather
        than silently overwrite it."""
        self._create_blank_rg_album(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "allow_establish_release_group": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = res["operation_id"]

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET mb_releasegroupid=? WHERE id=1", ("cccccccc-0000-0000-0000-000000000000",))
        con.commit()
        con.close()

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_toctou_mismatch")

        # And the concurrent edit's value must survive untouched.
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT mb_releasegroupid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["mb_releasegroupid"], "cccccccc-0000-0000-0000-000000000000")


class StampReleaseMetadataTests(Wave19FixtureBase):
    """album_mb_track_repair_v1's stamp_release_metadata option (Wave 33
    part 2): the year/country album-field stamping
    _match_tracks_from_mb_shared() needs to migrate for real, closing the
    year/month/day/country gap found while tracing its exact SQL."""

    def _tracklist_with_metadata(self, date="2020-05-15", country="US"):
        return {**_fake_tracklist_a(), "date": date, "country": country}

    def test_stamps_year_and_country_when_they_differ(self):
        self._create_album_and_items(album_id=1)  # year=2024 by fixture default
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "stamp_release_metadata": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_with_metadata(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(sorted(res["release_metadata_changes"].keys()), ["country", "year"])
        op_id = res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(sorted(apply_res["release_metadata_changes"]), ["country", "year"])

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT year, country FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["year"], 2020)
        self.assertEqual(row["country"], "US")

    def test_no_op_when_values_already_match(self):
        self._create_album_and_items(album_id=1)
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET year=2020, country='US' WHERE id=1")
        con.commit()
        con.close()
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "stamp_release_metadata": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_with_metadata(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["release_metadata_changes"], {})

    def test_omitted_flag_never_stamps_metadata(self):
        """Regression: default-off must behave exactly as before."""
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_with_metadata(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("release_metadata_changes", {}), {})
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT year, country FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["year"], 2024)
        self.assertIsNone(row["country"])

    def test_rollback_restores_original_year_and_country(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "stamp_release_metadata": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_with_metadata(),
        )
        op_id = res["operation_id"]
        execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        rb_res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rb_res.get("ok"), rb_res)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT year, country FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["year"], 2024)
        self.assertIn(row["country"], (None, ""))

    def test_apply_fails_closed_if_year_changed_by_someone_else_since_plan(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "stamp_release_metadata": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_with_metadata(),
        )
        op_id = res["operation_id"]

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET year=1999 WHERE id=1")
        con.commit()
        con.close()

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_toctou_mismatch")

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT year FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["year"], 1999)


class GreedyAlignmentAdversarialTests(Wave19FixtureBase):
    """SEC-002 / ARCH-003 Wave 33 continuation (coordinator directive):
    prove the engine's alignment procedure now matches app.py's own
    greedy, first-come-exclusive-claim behavior in exactly the ambiguous
    case (multiple candidate files competing for one track) where the OLD
    engine algorithm (summarize_mb_track_alignment's rank-based
    displacement) would have silently picked a DIFFERENT item -- the
    correctness risk this porting work exists to close, not merely
    bound. These are deliberately the sharpest tests in this wave.
    """

    def _tracklist_one_track(self):
        return {
            "ok": True,
            "release_id": REL_A,
            "release_group": RG_A,
            "release_title": "Test Album Title",
            "tracks": [
                {
                    # track=5 deliberately matches neither item's own
                    "disc": 1, "track": 5,
                    "title": "Sunshine and Rain",
                    "mb_trackid": REC_TARGET_1,
                    "duration_ms": 0,
                },
            ],
        }

    def _create_two_candidates_one_track(self, album_id=1):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
            (album_id, "Test Album Title", "Test Artist", REL_A, RG_A, 2024),
        )
        album_dir = self.music_root / f"album_{album_id}"
        album_dir.mkdir(parents=True, exist_ok=True)

        # Item A (id=1, DB-processed FIRST -- disc/track/title/id order,
        # track=1): a real but slightly-off title match, score ~0.97 --
        # lower than item B's, but still clears the 0.72 repair threshold
        # on its own.
        path_a = album_dir / "track_a.wav"
        _write_test_audio(path_a, freq=220.0, title="Sunshine and Ran", track=1, disc=1)
        # Item B (id=2, DB-processed SECOND, track=2): a perfect title
        # match, score 1.0 -- strictly higher than item A's.
        path_b = album_dir / "track_b.wav"
        _write_test_audio(path_b, freq=330.0, title="Sunshine and Rain", track=2, disc=1)

        con.execute(
            "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, album_id, "Sunshine and Ran", "Test Artist", "Test Album Title", "Test Artist", 1, 1, str(path_a), "", REL_A, "", 0.0),
        )
        con.execute(
            "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2, album_id, "Sunshine and Rain", "Test Artist", "Test Album Title", "Test Artist", 1, 2, str(path_b), "", REL_A, "", 0.0),
        )
        con.commit()
        con.close()
        return path_a, path_b

    def test_first_come_item_claims_the_track_not_the_higher_scoring_later_item(self):
        """Item A (processed first in DB order, LOWER score) must claim
        the one available track; item B (processed second, HIGHER score)
        must be left unmatched. The OLD rank-based-displacement engine
        algorithm would have picked B instead (displacing A), silently
        relabeling the wrong file with Track 5's identity. This is
        app.py's own, real, long-standing behavior -- the engine must now
        agree with it exactly, not approximate it."""
        self._create_two_candidates_one_track(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_one_track(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 1)
        tx = self.store.get(res["operation_id"])
        repaired = tx["metadata"]["payload"]["tracks_to_repair"]
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["item_id"], 1)
        self.assertEqual(repaired[0]["after"]["mb_trackid"], REC_TARGET_1)

    def test_displaced_higher_scoring_candidate_is_left_completely_untouched(self):
        """Item B (the higher-scoring but second-processed candidate)
        must never receive Track 5's identity, and must not appear in any
        repair/conflict row at all -- it is simply left alone by this
        Plan, exactly as app.py's own greedy loop would leave it."""
        self._create_two_candidates_one_track(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_one_track(),
        )
        self.assertTrue(res.get("ok"), res)
        tx = self.store.get(res["operation_id"])
        payload = tx["metadata"]["payload"]
        touched_item_ids = {t["item_id"] for t in payload["tracks_to_repair"]}
        touched_item_ids |= {t["item_id"] for t in payload["conflicts_requiring_review"]}
        self.assertNotIn(2, touched_item_ids)

    def test_zero_unmatched_zeroes_the_displaced_candidate_not_the_claimant(self):
        """With zero_unmatched=True, item B (never claimed a track) is
        the one that gets its track# zeroed -- item A (the actual
        claimant) must be left alone."""
        self._create_two_candidates_one_track(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "zero_unmatched": True},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_one_track(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["zero_unmatched_rows"], 1)
        tx = self.store.get(res["operation_id"])
        zeroed_ids = {t["item_id"] for t in tx["metadata"]["payload"]["zero_unmatched_rows"]}
        self.assertEqual(zeroed_ids, {2})

    def test_apply_actually_relabels_item_a_on_disk_not_item_b(self):
        """End-to-end through Apply (not just Plan inspection): item A's
        real on-disk mb_trackid tag becomes REC_TARGET_1; item B's is
        left completely unwritten."""
        self._create_two_candidates_one_track(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: self._tracklist_one_track(),
        )
        apply_res = execute_album_mb_track_repair_apply(
            self.store, res["operation_id"], db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row_a = con.execute("SELECT mb_trackid FROM items WHERE id=1").fetchone()
        row_b = con.execute("SELECT mb_trackid FROM items WHERE id=2").fetchone()
        con.close()
        self.assertEqual(row_a["mb_trackid"], REC_TARGET_1)
        self.assertEqual(row_b["mb_trackid"], "")


if __name__ == "__main__":
    unittest.main()
