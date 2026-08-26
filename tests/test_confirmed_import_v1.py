"""SEC-002 / ARCH-003 Wave 25 Round 3: confirmed_import_v1.

A distinct controlled family for FRESH REVIEWED imports of previously-
UNTAGGED source audio, built after the Wave 25 Docker acceptance round
found that routing fresh imports through reimport_source_atomic() /
verify_deterministic_identity() was a genuine architecture mismatch: that
gate requires the source's OWN embedded MusicBrainz tags to already match
the target, which untagged fresh downloads never have by construction.

These tests use fake inspect_source_fn/run_native_import_fn/
acoustid_lookup_fn callables against a real sqlite Beets-shaped database --
no Docker required. The genuinely-untagged-fixture requirement from the
Round 3 brief is honored throughout: no test pre-tags a fixture with
MusicBrainz identity just to make it pass.
"""
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.transaction_engine import (
    TransactionStore,
    create_confirmed_import_plan,
    execute_confirmed_import_apply,
)

TARGET_RELEASE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TARGET_RG = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OTHER_RG = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
MB_TRACKS = [
    {"track": 1, "title": "Track One", "mb_trackid": "t1", "duration_ms": 200000},
    {"track": 2, "title": "Track Two", "mb_trackid": "t2", "duration_ms": 210000},
]


def _sig(entries):
    parts = sorted(f"{e['relative_path']}:{e['size']}:{e['mtime_ns']}" for e in entries)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _inspect(path, tag_overrides=None):
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return {"ok": False, "error_code": "invalid_path"}
    entries = []
    for f in sorted(p.iterdir()):
        st = f.stat()
        props = {}
        if tag_overrides and f.name in tag_overrides:
            props = tag_overrides[f.name]
        entries.append({"relative_path": f.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "properties": props})
    return {"ok": True, "canonical_path": str(p), "audio_files": entries, "source_signature": _sig(entries)}


class ConfirmedImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.music_root = self.root / "music"
        self.music_root.mkdir()
        self.lib_db = self.root / "lib.blb"
        con = sqlite3.connect(str(self.lib_db))
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, mb_albumid TEXT, mb_releasegroupid TEXT, album TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        con.commit()
        con.close()
        self.store = TransactionStore(root=str(self.root / "transactions"))

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_source(self, name, filenames):
        src = self.staging / name
        src.mkdir()
        for fn in filenames:
            (src / fn).write_bytes(b"fake-audio-bytes")
        return src

    def _base_payload(self, source_folder, **overrides):
        payload = {
            "source_folder": str(source_folder),
            "mb_albumid": TARGET_RELEASE,
            "mb_releasegroupid": TARGET_RG,
            "mb_release_group_resolved": TARGET_RG,
            "mb_tracks": MB_TRACKS,
        }
        payload.update(overrides)
        return payload

    def _plan(self, payload, inspect_fn=_inspect, acoustid_fn=None):
        return create_confirmed_import_plan(
            self.store, payload,
            staging_allowed_roots=[str(self.staging)],
            music_allowed_roots=[str(self.music_root)],
            inspect_source_fn=inspect_fn,
            acoustid_lookup_fn=acoustid_fn,
        )

    def _apply(self, op_id, inspect_fn=_inspect, native_import_fn=None):
        return execute_confirmed_import_apply(
            self.store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.lib_db),
            inspect_source_fn=inspect_fn,
            run_native_import_fn=native_import_fn,
        )

    def _fake_native_import(self, mb_albumid=TARGET_RELEASE, mb_rgid=TARGET_RG, filenames=("Track One.flac", "Track Two.flac")):
        def _run(source_path, target_mb_albumid, *, use_move):
            con = sqlite3.connect(str(self.lib_db))
            con.execute(
                "INSERT INTO albums (mb_albumid, mb_releasegroupid, album) VALUES (?,?,?)",
                (mb_albumid, mb_rgid, "Fresh Album"),
            )
            aid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            for fn in filenames:
                dest = self.music_root / fn
                dest.write_bytes(b"fake-audio-bytes")
                con.execute("INSERT INTO items (album_id, path) VALUES (?,?)", (aid, str(dest)))
            con.commit()
            con.close()
            return {"ok": True}
        return _run


class TestFreshUntaggedImportSucceeds(ConfirmedImportTestCase):
    """Round 3 brief §26: genuinely untagged fixture, no pre-tagging."""

    def test_plan_and_apply_succeed_for_untagged_source(self):
        src = self._seed_source("Fresh Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)
        self.assertEqual(plan["track_coverage"]["present_track_count"], 2)
        self.assertEqual(plan["track_coverage"]["missing_track_count"], 0)

        result = self._apply(plan["operation_id"], native_import_fn=self._fake_native_import())
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["status"], "Completed")
        self.assertGreater(result["album_id"], 0)
        self.assertEqual(len(result["item_ids"]), 2)

    def test_plan_rejects_missing_release_id(self):
        src = self._seed_source("No Release", ["a.flac"])
        plan = self._plan(self._base_payload(src, mb_albumid=""))
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_invalid_release_id")

    def test_plan_rejects_empty_tracklist(self):
        src = self._seed_source("No Tracks", ["a.flac"])
        plan = self._plan(self._base_payload(src, mb_tracks=[]))
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_missing_tracklist")

    def test_plan_rejects_no_track_alignment_at_all(self):
        src = self._seed_source("Totally Unrelated", ["zzz_nonsense_file.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_no_track_alignment")

    def test_plan_allows_incomplete_album(self):
        """§9: missing tracks alone must not block a reviewed partial import."""
        src = self._seed_source("Partial Album", ["Track One.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)
        self.assertEqual(plan["track_coverage"]["present_track_count"], 1)
        self.assertEqual(plan["track_coverage"]["missing_track_count"], 1)


class TestEmbeddedIdentityConflict(ConfirmedImportTestCase):
    """Round 3 brief §27: missing tags != conflicting tags."""

    def test_conflicting_embedded_mb_albumid_is_rejected(self):
        src = self._seed_source("Conflict Album", ["Track One.flac"])

        def inspect_with_conflict(path):
            r = _inspect(path)
            r["audio_files"][0]["properties"] = {"mb_albumid": "cccccccc-cccc-cccc-cccc-cccccccccccc"}
            return r

        plan = self._plan(self._base_payload(src), inspect_fn=inspect_with_conflict)
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_embedded_identity_conflict")

    def test_missing_embedded_tags_is_not_a_conflict(self):
        src = self._seed_source("Untagged Album", ["Track One.flac", "Track Two.flac"])

        def inspect_no_tags(path):
            r = _inspect(path)
            for e in r["audio_files"]:
                e["properties"] = {}
            return r

        plan = self._plan(self._base_payload(src), inspect_fn=inspect_no_tags)
        self.assertTrue(plan.get("ok"), plan)

    def test_conflicting_embedded_releasegroup_is_rejected(self):
        src = self._seed_source("RG Conflict Album", ["Track One.flac"])

        def inspect_with_rg_conflict(path):
            r = _inspect(path)
            r["audio_files"][0]["properties"] = {"mb_releasegroupid": OTHER_RG}
            return r

        plan = self._plan(self._base_payload(src), inspect_fn=inspect_with_rg_conflict)
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_embedded_identity_conflict")


class TestWrongTarget(ConfirmedImportTestCase):
    """Round 3 brief §28: approved Release/RGID must not be silently substituted."""

    def test_release_group_mismatch_between_explicit_and_resolved_is_rejected(self):
        src = self._seed_source("Wrong RG Album", ["Track One.flac"])
        plan = self._plan(self._base_payload(src, mb_releasegroupid=TARGET_RG, mb_release_group_resolved=OTHER_RG))
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_release_group_mismatch")

    def test_invalid_releasegroup_uuid_format_rejected(self):
        src = self._seed_source("Bad RG Format", ["Track One.flac"])
        plan = self._plan(self._base_payload(src, mb_releasegroupid="not-a-uuid"))
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_invalid_rgid")


class TestSourceChangedIsStale(ConfirmedImportTestCase):
    """Round 3 brief §29: source changed after Plan -> Apply must reject."""

    def test_apply_rejects_when_source_content_changed_since_plan(self):
        src = self._seed_source("Stale Album", ["Track One.flac"])
        plan = self._plan(self._base_payload(src, mb_tracks=[{"track": 1, "title": "Track One", "mb_trackid": "t1"}]))
        self.assertTrue(plan.get("ok"), plan)

        (src / "Track One.flac").write_bytes(b"modified-content-different-size-now")

        calls = {"n": 0}

        def _should_not_run(*a, **kw):
            calls["n"] += 1
            raise AssertionError("native import must not run against a stale plan")

        result = self._apply(plan["operation_id"], native_import_fn=_should_not_run)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["code"], "confirmed_import_stale_plan")
        self.assertEqual(calls["n"], 0)

    def test_apply_fails_closed_when_source_removed_and_no_result_exists(self):
        src = self._seed_source("Vanished Album", ["Track One.flac"])
        plan = self._plan(self._base_payload(src, mb_albumid="ffffffff-ffff-ffff-ffff-ffffffffffff",
                                              mb_releasegroupid="", mb_release_group_resolved="",
                                              mb_tracks=[{"track": 1, "title": "Track One", "mb_trackid": "t1"}]))
        self.assertTrue(plan.get("ok"), plan)
        import shutil
        shutil.rmtree(src)

        def _should_not_run(*a, **kw):
            raise AssertionError("native import must not run when the source is gone")

        result = self._apply(plan["operation_id"], native_import_fn=_should_not_run)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["code"], "confirmed_import_source_unavailable")
        self.assertNotEqual(result.get("status"), "Recovery Required")


class TestIdempotencyAndCrashResume(ConfirmedImportTestCase):
    """Round 3 brief §21/§22/§32/§33: retry and crash/resume must never
    duplicate an album, and must never re-run native import once a
    verified result already exists for the target Release ID."""

    def test_reapplying_the_same_operation_id_is_idempotent(self):
        src = self._seed_source("Idempotent Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)
        native = self._fake_native_import()

        first = self._apply(plan["operation_id"], native_import_fn=native)
        self.assertTrue(first.get("ok"), first)

        second = self._apply(plan["operation_id"], native_import_fn=native)
        self.assertTrue(second.get("ok"), second)
        self.assertEqual(second["album_id"], first["album_id"])
        self.assertEqual(sorted(second["item_ids"]), sorted(first["item_ids"]))

        con = sqlite3.connect(str(self.lib_db))
        count = con.execute("SELECT COUNT(*) FROM albums WHERE mb_albumid=?", (TARGET_RELEASE,)).fetchone()[0]
        con.close()
        self.assertEqual(count, 1, "must never create a duplicate album row")

    def test_crash_resume_via_new_plan_does_not_rerun_native_import(self):
        """Simulates: native import already succeeded for this target
        Release ID (from a prior, now-completed transaction); a brand new
        Plan/Apply pair for the SAME target must resume, not reimport."""
        src1 = self._seed_source("Original Album", ["Track One.flac", "Track Two.flac"])
        plan1 = self._plan(self._base_payload(src1))
        self.assertTrue(plan1.get("ok"), plan1)
        first = self._apply(plan1["operation_id"], native_import_fn=self._fake_native_import())
        self.assertTrue(first.get("ok"), first)

        src2 = self._seed_source("Retried Album Attempt", ["Track One.flac", "Track Two.flac"])
        plan2 = self._plan(self._base_payload(src2))
        self.assertTrue(plan2.get("ok"), plan2)

        calls = {"n": 0}

        def _should_not_run(*a, **kw):
            calls["n"] += 1
            raise AssertionError("native import must not run a second time for an already-completed target")

        second = self._apply(plan2["operation_id"], native_import_fn=_should_not_run)
        self.assertTrue(second.get("ok"), second)
        self.assertTrue(second.get("resumed"))
        self.assertEqual(calls["n"], 0)
        self.assertEqual(second["album_id"], first["album_id"])


class TestAcoustidConflict(ConfirmedImportTestCase):
    """Round 3 brief §30: strong contradictory fingerprint evidence must
    block, but ABSENCE of fingerprint evidence must never itself block."""

    def test_no_acoustid_lookup_configured_does_not_block(self):
        src = self._seed_source("No Fingerprint Available", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src), acoustid_fn=None)
        self.assertTrue(plan.get("ok"), plan)

    def test_strong_conflicting_fingerprint_on_extra_file_blocks(self):
        src = self._seed_source("Fingerprint Conflict", ["Track One.flac", "Track Two.flac", "Bonus Unrelated.flac"])

        def fake_acoustid(file_path):
            if "Bonus" in file_path:
                return [{"mb_trackid": "totally-different-recording-id", "score": 95, "title": "Some Other Song"}]
            return []

        plan = self._plan(self._base_payload(src), acoustid_fn=fake_acoustid)
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan["code"], "confirmed_import_acoustid_conflict")

    def test_low_confidence_fingerprint_hit_does_not_block(self):
        src = self._seed_source("Weak Fingerprint Hit", ["Track One.flac", "Track Two.flac", "Bonus Unrelated.flac"])

        def fake_acoustid_weak(file_path):
            if "Bonus" in file_path:
                return [{"mb_trackid": "totally-different-recording-id", "score": 40, "title": "Some Other Song"}]
            return []

        plan = self._plan(self._base_payload(src), acoustid_fn=fake_acoustid_weak)
        self.assertTrue(plan.get("ok"), plan)


class TestNativeImportSkipVsPartialMutation(ConfirmedImportTestCase):
    """Round 4 brief: the real Docker failure was --quiet-fallback asis
    silently importing the source under an unreviewed identity when Beets
    could not confidently apply the approved release (an album/items got
    created, but confirmed_import_v1 could not verify them against the
    planned Release ID). Fixed by switching to --quiet-fallback skip and
    distinguishing, via the native runner's albums_before/albums_after
    counts, a clean skip (Failed, no mutation) from a genuine partial
    mutation under the wrong identity (Recovery Required)."""

    def test_clean_skip_reports_failed_not_recovery_required(self):
        src = self._seed_source("Skipped Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)

        def _native_import_skips(source_path, target_mb_albumid, *, use_move):
            # Beets declined to apply the candidate; nothing changed.
            return {"ok": True, "returncode": 0, "albums_before": 1, "albums_after": 1,
                    "stdout_excerpt": "", "stderr_excerpt": ""}

        result = self._apply(plan["operation_id"], native_import_fn=_native_import_skips)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["status"], "Failed")
        self.assertEqual(result["code"], "confirmed_import_skipped_no_confident_match")

    def test_partial_mutation_under_wrong_identity_reports_recovery_required(self):
        src = self._seed_source("Partial Mutation Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)

        def _native_import_creates_unverifiable_album(source_path, target_mb_albumid, *, use_move):
            # Simulates the original bug: Beets imported something (album
            # count went up) but not under the planned Release ID.
            con = sqlite3.connect(str(self.lib_db))
            con.execute("INSERT INTO albums (mb_albumid, mb_releasegroupid, album) VALUES (?,?,?)",
                        ("ffffffff-0000-0000-0000-000000000000", "", "Asis Album"))
            con.commit()
            con.close()
            return {"ok": True, "returncode": 0, "albums_before": 1, "albums_after": 2,
                    "stdout_excerpt": "", "stderr_excerpt": ""}

        result = self._apply(plan["operation_id"], native_import_fn=_native_import_creates_unverifiable_album)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["status"], "Recovery Required")
        self.assertEqual(result["code"], "confirmed_import_verification_failed")

    def test_missing_album_count_diagnostics_falls_back_to_recovery_required(self):
        """Backward-compatible: a runner that doesn't report album counts
        (e.g. an older/simpler fake) must not crash, and defaults to the
        safer Recovery Required outcome rather than assuming a clean skip."""
        src = self._seed_source("No Diagnostics Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)

        def _native_import_no_diagnostics(source_path, target_mb_albumid, *, use_move):
            return {"ok": True}

        result = self._apply(plan["operation_id"], native_import_fn=_native_import_no_diagnostics)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["status"], "Recovery Required")


class TestPartialFileCopyIsRejected(ConfirmedImportTestCase):
    """Round 4, corrected (real Docker acceptance finding): a real fresh
    import against a realistic multi-track fixture reported Apply success
    and a `confirmed_import_v1 verified result`, but some of the album's
    item rows pointed at files that were never actually copied -- the
    acceptance script's own from-scratch check of every item path (never
    loosened to "at least one") caught what the engine's own
    `_capture_and_verify(require_files_present=True)` did not: it only
    required `files_present != 0`, so a real album with e.g. 10 of 12
    items missing their file still verified as `ok`. "Confirmed" must mean
    every item this result claims to have imported is actually present on
    disk, not merely that the album is not completely empty."""

    def test_apply_fails_when_some_but_not_all_items_are_missing_their_file(self):
        src = self._seed_source("Partially Copied Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)

        def _native_import_partial_copy(source_path, target_mb_albumid, *, use_move):
            con = sqlite3.connect(str(self.lib_db))
            con.execute(
                "INSERT INTO albums (mb_albumid, mb_releasegroupid, album) VALUES (?,?,?)",
                (target_mb_albumid, TARGET_RG, "Fresh Album"),
            )
            aid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Track One really gets copied...
            real_dest = self.music_root / "Track One.flac"
            real_dest.write_bytes(b"fake-audio-bytes")
            con.execute("INSERT INTO items (album_id, path) VALUES (?,?)", (aid, str(real_dest)))
            # ...but Track Two's row is created without a backing file --
            # the exact shape of the real Docker finding (a genuine
            # partial-copy failure, not a clean quiet-fallback skip).
            missing_dest = self.music_root / "Track Two.flac"
            con.execute("INSERT INTO items (album_id, path) VALUES (?,?)", (aid, str(missing_dest)))
            con.commit()
            con.close()
            return {"ok": True, "returncode": 0, "albums_before": 0, "albums_after": 1,
                    "stdout_excerpt": "", "stderr_excerpt": ""}

        result = self._apply(plan["operation_id"], native_import_fn=_native_import_partial_copy)
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result["status"], "Recovery Required")
        self.assertEqual(result["code"], "confirmed_import_verification_failed")

    def test_apply_succeeds_when_every_item_has_a_real_file(self):
        # Control: the existing full-copy fake fixture must still pass --
        # this fix must not reject a genuinely complete import.
        src = self._seed_source("Fully Copied Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)
        result = self._apply(plan["operation_id"], native_import_fn=self._fake_native_import())
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["status"], "Completed")


class TestAcceptanceFailpoint(ConfirmedImportTestCase):
    """Round 4 brief section 23-25: a deterministic, narrowly-named
    failpoint for the Docker acceptance crash/resume scenario -- fires
    after the native_import_invoked checkpoint is durably persisted, before
    verification, simulating a crash in exactly that window without a
    timing race."""

    def test_failpoint_fires_after_native_import_before_verification(self):
        src = self._seed_source("Failpoint Album", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)

        native = self._fake_native_import()
        result = execute_confirmed_import_apply(
            self.store, plan["operation_id"],
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.lib_db),
            inspect_source_fn=_inspect,
            run_native_import_fn=native,
            acceptance_failpoint="confirmed_import_after_native",
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["status"], "Recovery Required")
        self.assertEqual(result["code"], "confirmed_import_acceptance_failpoint")

        # Beets really did import (the fake runner created a real album) --
        # the failpoint fired AFTER real work happened, not instead of it.
        con = sqlite3.connect(str(self.lib_db))
        count = con.execute("SELECT COUNT(*) FROM albums WHERE mb_albumid=?", (TARGET_RELEASE,)).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)

    def test_retry_after_failpoint_resumes_without_calling_native_import_again(self):
        src1 = self._seed_source("Failpoint Original", ["Track One.flac", "Track Two.flac"])
        plan1 = self._plan(self._base_payload(src1))
        self.assertTrue(plan1.get("ok"), plan1)
        first = execute_confirmed_import_apply(
            self.store, plan1["operation_id"],
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.lib_db),
            inspect_source_fn=_inspect,
            run_native_import_fn=self._fake_native_import(),
            acceptance_failpoint="confirmed_import_after_native",
        )
        self.assertEqual(first["status"], "Recovery Required")

        # Retry: a brand-new Plan for the same target, no failpoint this
        # time -- must discover the real album the first attempt's native
        # import already created, and must NOT invoke native import again.
        src2 = self._seed_source("Failpoint Retry", ["Track One.flac", "Track Two.flac"])
        plan2 = self._plan(self._base_payload(src2))
        self.assertTrue(plan2.get("ok"), plan2)

        calls = {"n": 0}

        def _should_not_run(*a, **kw):
            calls["n"] += 1
            raise AssertionError("native import must not run again after the failpoint-simulated crash")

        second = self._apply(plan2["operation_id"], native_import_fn=_should_not_run)
        self.assertTrue(second.get("ok"), second)
        self.assertTrue(second.get("resumed"))
        self.assertEqual(calls["n"], 0)

        con = sqlite3.connect(str(self.lib_db))
        count = con.execute("SELECT COUNT(*) FROM albums WHERE mb_albumid=?", (TARGET_RELEASE,)).fetchone()[0]
        con.close()
        self.assertEqual(count, 1, "must never create a duplicate album row")

    def test_failpoint_is_a_no_op_when_not_the_recognized_name(self):
        """Only the exact recognized failpoint name has any effect --
        this is not a generic arbitrary failure-injection API."""
        src = self._seed_source("Unrecognized Failpoint", ["Track One.flac", "Track Two.flac"])
        plan = self._plan(self._base_payload(src))
        self.assertTrue(plan.get("ok"), plan)
        result = execute_confirmed_import_apply(
            self.store, plan["operation_id"],
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.lib_db),
            inspect_source_fn=_inspect,
            run_native_import_fn=self._fake_native_import(),
            acceptance_failpoint="some_other_unrecognized_value",
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["status"], "Completed")


class TestAcceptanceModeGating(unittest.TestCase):
    """Round 4 brief section 23: the failpoint must be inert unless the
    container was booted with BEETS_ACCEPTANCE_MODE=1 -- never settable by
    a real client against a real deployment."""

    def test_acceptance_mode_defaults_off(self):
        import importlib
        import backend.beets_control_agent as bca
        # Reload isolated from any test-runtime env pollution: the module
        # constant is read once at import time from the real environment.
        if "BEETS_ACCEPTANCE_MODE" not in __import__("os").environ:
            self.assertFalse(bca.ACCEPTANCE_MODE)

    def test_acceptance_route_ignores_failpoint_field_when_mode_is_off(self):
        src_text = (ROOT / "backend" / "beets_control_agent.py").read_text(encoding="utf-8")
        idx = src_text.index('if path == "/imports/confirmed/apply":')
        end_idx = src_text.index("\n        if path ==", idx + 10)
        window = src_text[idx:end_idx]
        self.assertIn("if ACCEPTANCE_MODE:", window)
        self.assertIn("_acceptance_failpoint", window)


class TestReimportSecurityModelUnchanged(unittest.TestCase):
    """Round 3 brief §2/§12: verify_deterministic_identity() and
    reimport_source_atomic() were not weakened -- no new bypass flags."""

    def test_verify_deterministic_identity_still_requires_expected_identity(self):
        from backend.beets_control_agent import verify_deterministic_identity
        result = verify_deterministic_identity({"audio_files": []}, None)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "review_required")

    def test_verify_deterministic_identity_signature_has_no_bypass_flags(self):
        import inspect
        from backend.beets_control_agent import verify_deterministic_identity
        params = set(inspect.signature(verify_deterministic_identity).parameters)
        for forbidden in ("skip_identity_check", "force", "trust_caller", "ignore_missing_tags"):
            self.assertNotIn(forbidden, params)

    def test_reimport_source_atomic_signature_has_no_bypass_flags(self):
        import inspect
        from backend.beets_control_agent import reimport_source_atomic
        params = set(inspect.signature(reimport_source_atomic).parameters)
        for forbidden in ("skip_identity_check", "force", "trust_caller", "ignore_missing_tags"):
            self.assertNotIn(forbidden, params)

    def test_confirmed_import_apply_never_calls_reimport_source_atomic(self):
        src_text = (ROOT / "backend" / "beets_control_agent.py").read_text(encoding="utf-8")
        idx = src_text.index("def run_confirmed_import_native(")
        end_idx = src_text.index("\ndef _path_has_symlink_component(", idx)
        window = src_text[idx:end_idx]
        self.assertNotIn("reimport_source_atomic(", window)
        self.assertNotIn("verify_deterministic_identity(", window)


if __name__ == "__main__":
    unittest.main()
