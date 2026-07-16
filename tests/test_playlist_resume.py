"""Tests for playlist pipeline resume: reconcile, import-before-download, per-track fields."""
import re
import unittest
from pathlib import Path


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app.py").read_text(encoding="utf-8")


def _reconcile_fn_source(src: str) -> str:
    return src[
        src.index("def _playlist_reconcile_staged_files("):
        src.index("def _playlist_download_missing_tracks(")
    ]


def _set_track_status_source(src: str) -> str:
    return src[
        src.index("def _playlist_set_track_status("):
        src.index("def _playlist_safe_filename(")
    ]


def _download_missing_source(src: str) -> str:
    return src[
        src.index("def _playlist_download_missing_tracks("):
        src.index("@app.post(\"/api/playlist/download\")")
    ]


def _staged_entries_source(src: str) -> str:
    return src[
        src.index("def _playlist_staged_entries("):
        src.index("def _playlist_run_import_downloaded(")
    ]


def _run_fn_source(src: str) -> str:
    start = src.index("def _run(job_log: Optional[List[str]] = None, cancel_event=None):")
    end = src.index("    job = jobs.start_python(", start)
    return src[start:end]


def _state_summary_source(src: str) -> str:
    return src[
        src.index("def _playlist_latest_job_state_summary("):
        src.index("def _playlist_missing_track_label(")
    ]


def _playlist_download_route_source(src: str) -> str:
    start = src.index("@app.post(\"/api/playlist/download\")")
    # ends at the next route or top-level def/class after the function
    end = src.index("@app.get(\"/api/playlist/", start)
    return src[start:end]


class ReconcileFunctionDefinitionTests(unittest.TestCase):
    """_playlist_reconcile_staged_files is defined with the correct shape."""

    def setUp(self):
        self._src = _app_source()
        self._fn = _reconcile_fn_source(self._src)

    def test_function_is_defined(self):
        self.assertIn(
            "def _playlist_reconcile_staged_files(name: str, dl_dir: Path,",
            self._fn,
        )

    def test_reads_manifest_track_states(self):
        self.assertIn("_playlist_manifest_track_states(clean_name)", self._fn)

    def test_resets_stale_staged_paths_to_pending(self):
        self.assertIn('"pending"', self._fn)
        self.assertIn('staged_path=""', self._fn)
        self.assertIn("staged file missing on resume", self._fn)

    def test_scans_dl_dir_with_audio_files_helper(self):
        self.assertIn("_audio_files_in_dir(str(dl_dir))", self._fn)

    def test_fingerprint_verified_match_waits_for_import(self):
        self.assertIn('"waiting_import"', self._fn)
        self.assertIn('identity.get("final_action") == "accept"', self._fn)
        self.assertIn("_playlist_identity_status_fields(best_match)", self._fn)

    def test_low_confidence_match_becomes_review_required(self):
        self.assertIn('"review_required"', self._fn)
        self.assertIn("score >= 0.50", self._fn)
        self.assertIn('failure_reason=reason if new_status == "review_required" else ""', self._fn)

    def test_match_uses_shared_acoustid_identity_decision(self):
        self.assertIn("_playlist_score_download_candidates(", self._fn)
        self.assertIn("_audio_identity_decision(", self._fn)
        self.assertIn("_acoustid_lookup_cached(audio_path)", self._fn)

    def test_tag_candidates_extracted_once_per_file_not_per_track(self):
        # Regression: this loop used to call _playlist_download_match(audio_path, ...)
        # inside the `for trk in pending_tracks` loop, which re-opened and re-parsed
        # the SAME audio file's tags once per pending track (O(files * tracks) disk
        # I/O on every resume). Candidates must now be extracted once per audio_path,
        # outside the inner track loop, and reused for scoring against every track.
        outer_pos = self._fn.index("for audio_path in unmatched:")
        candidates_pos = self._fn.index(
            "candidates = _playlist_download_text_candidates(audio_path)"
        )
        inner_pos = self._fn.index("for trk in pending_tracks:")
        self.assertLess(outer_pos, candidates_pos)
        self.assertLess(candidates_pos, inner_pos)
        # The inner loop must not re-open the file per track.
        inner_block = self._fn[inner_pos:self._fn.index("if best_trk is None:")]
        self.assertNotIn("_playlist_download_text_candidates(", inner_block)
        self.assertIn("_playlist_score_download_candidates(\n                candidates,", inner_block)


class SetTrackStatusExtraKwargsTests(unittest.TestCase):
    """_playlist_set_track_status accepts **extra kwargs."""

    def setUp(self):
        self._fn = _set_track_status_source(_app_source())

    def test_signature_has_extra_kwargs(self):
        self.assertIn("**extra: Any", self._fn)

    def test_extra_keys_stored_in_row(self):
        self.assertIn("for xk, xv in extra.items()", self._fn)

    def test_extra_forwarded_to_store_track_state(self):
        self.assertIn("**extra,", self._fn)


class DownloadAttemptTrackingTests(unittest.TestCase):
    """download_attempt_count and file_size are tracked per-track in _playlist_download_missing_tracks."""

    def setUp(self):
        self._fn = _download_missing_source(_app_source())

    def test_attempt_count_computed_before_method_loop(self):
        attempt_pos = self._fn.index("attempt_count = int(prev_row.get(\"download_attempt_count\"")
        method_loop_pos = self._fn.index("for method in methods:")
        self.assertLess(attempt_pos, method_loop_pos)

    def test_staged_reuse_check_happens_before_pending_write(self):
        persisted_pos = self._fn.index("persisted = _playlist_state_for_track")
        queued_pos = self._fn.index('_playlist_set_track_status(state, trk, "queued"')
        self.assertLess(persisted_pos, queued_pos)

    def test_attempt_count_passed_to_searching_status(self):
        searching_block = self._fn[
            self._fn.index("\"searching\", method=method"):
            self._fn.index("log(f\"  trying {_download_method_label(method)}\")")
        ]
        self.assertIn("download_attempt_count=attempt_count", searching_block)

    def test_file_size_stored_on_successful_download(self):
        self.assertIn("file_size=_file_size", self._fn)
        self.assertIn(".stat().st_size", self._fn)

    def test_downloaded_files_become_waiting_import(self):
        self.assertIn('state, trk, "waiting_import", method="resume"', self._fn)
        self.assertIn('state, trk, "waiting_import", method=method', self._fn)



class ReviewRequiredDownloadAccountingTests(unittest.TestCase):
    """Review-held downloads are real activity, but are not auto-imported."""

    def setUp(self):
        src = _app_source()
        self._download_fn = _download_missing_source(src)
        self._run_fn = _run_fn_source(src)

    def test_review_required_downloads_are_counted_as_round_activity(self):
        self.assertIn("before_review = _playlist_review_required_count_from_state(state)", self._download_fn)
        self.assertIn("\"review_required\": review_required_count", self._download_fn)
        self.assertIn("\"activity\": downloaded_count + review_required_count", self._download_fn)

    def test_round_with_review_required_stops_before_directory_import(self):
        review_guard = self._run_fn.index("if review_downloaded > 0:")
        import_phase = self._run_fn.index("state[\"phase\"] = \"import\"", review_guard)
        self.assertLess(review_guard, import_phase)
        guard_block = self._run_fn[review_guard:self._run_fn.index("if verified_downloaded <= 0:", review_guard)]
        self.assertIn("state[\"phase\"] = \"review\"", guard_block)
        self.assertIn("state[\"waiting_for_import\"] = waiting_total", guard_block)
        self.assertIn("_playlist_waiting_import_count_from_state(state)", guard_block)
        self.assertIn("stopping automatic import for this round", guard_block)

    def test_final_sync_keeps_review_required_tracks_and_does_not_raise(self):
        self.assertIn("if existing_status == \"review_required\":\n                        continue", self._run_fn)
        self.assertIn("if not matched and not review_total:", self._run_fn)
        self.assertIn("No playlist tracks matched Beets yet", self._run_fn)

class ResumePhaseInRunTests(unittest.TestCase):
    """The resume-phase block exists inside _run() and has the correct structure."""

    def setUp(self):
        self._fn = _run_fn_source(_app_source())

    def test_reconcile_called_when_resumed(self):
        self.assertIn("_playlist_reconcile_staged_files(name, dl_dir, active_tracks, _log)", self._fn)

    def test_reconcile_only_when_resumed(self):
        reconcile_pos = self._fn.index("_playlist_reconcile_staged_files(")
        guard_region = self._fn[max(0, reconcile_pos - 500):reconcile_pos]
        self.assertIn('state.get("resumed")', guard_region)

    def test_import_staged_before_max_rounds_loop(self):
        import_pos = self._fn.index("_playlist_run_import_downloaded(name, state[\"log\"]")
        max_rounds_pos = self._fn.index("max_rounds = max(1,")
        self.assertLess(import_pos, max_rounds_pos)

    def test_pre_import_accepts_waiting_import_status(self):
        self.assertIn('state["waiting_for_import"] = _playlist_waiting_import_count_from_state(state)', self._fn)
        self.assertIn('_playlist_run_import_downloaded(', self._fn)
        self.assertIn('name, state["log"], cancel_event=cancel_event', self._fn)

    def test_waiting_for_import_cleared_after_import(self):
        self.assertIn('state["waiting_for_import"] = 0', self._fn)

    def test_active_tracks_updated_after_pre_import(self):
        self.assertIn("active_tracks = _missing_pre", self._fn)


class JobStateFieldsTests(unittest.TestCase):
    """Job state dict and summary contain the new fields."""

    def setUp(self):
        self._src = _app_source()

    def test_waiting_for_import_in_initial_state_dict(self):
        route_src = _playlist_download_route_source(self._src)
        self.assertIn('"waiting_for_import": 0', route_src)

    def test_checkpoint_waiting_for_import_in_summary(self):
        summary_src = _state_summary_source(self._src)
        self.assertIn('"checkpoint_waiting_for_import"', summary_src)
        self.assertIn('latest.get("waiting_for_import")', summary_src)
        self.assertIn("_playlist_waiting_import_count_from_state(latest)", summary_src)

    def test_stale_complete_checkpoint_is_hidden_when_current_playlist_has_no_work(self):
        self.assertIn("def _playlist_visible_checkpoint_summary", self._src)
        self.assertIn('"checkpoint_stale_complete": True', self._src)
        self.assertIn('"checkpoint_interrupted": False', self._src)
        self.assertIn("_playlist_summary_waiting_count(summary) > 0", self._src)

    def test_detail_payload_normalizes_checkpoint_before_response(self):
        self.assertIn("visible_checkpoint = _playlist_apply_checkpoint_summary(summary, checkpoint)", self._src)
        self.assertIn("_playlist_visible_checkpoint_summary({", self._src)
        self.assertIn('"missing_count": len(missing)', self._src)

    def test_per_playlist_concurrent_job_rejected(self):
        route_src = _playlist_download_route_source(self._src)
        self.assertIn("A pipeline is already running for this playlist.", self._src)
        self.assertIn("running_job_id", route_src)
        self.assertIn('_s(_other_st.get("playlist_name")', route_src)


class StagedEntriesImportBoundaryTests(unittest.TestCase):
    """Only verified staged files are auto-imported."""

    def setUp(self):
        self._fn = _staged_entries_source(_app_source())

    def test_waiting_import_entries_are_importable(self):
        self.assertIn('{"downloaded", "waiting_import", "importing", "failed"}', self._fn)

    def test_review_required_entries_are_not_auto_imported(self):
        status_guard = self._fn[
            self._fn.index("if status not in"):
            self._fn.index("raw_path =", self._fn.index("if status not in"))
        ]
        self.assertNotIn("review_required", status_guard)
