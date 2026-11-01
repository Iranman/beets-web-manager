"""Characterization of the playlist download pipeline (modernization M3, #505).

Each scenario drives the real ``start_playlist_download`` job body with fake
providers, a fake library and a real on-disk checkpoint. The complete trace --
provider/import/Plex calls, every checkpoint save, the job log, final per-track
statuses -- is compared with ``tests/fixtures/playlist_pipeline_golden.json``,
which was generated from the code BEFORE the pipeline was split into steps.

Regenerate only for a deliberate behavior change:
    UPDATE_PLAYLIST_GOLDEN=1 python -m unittest tests.test_playlist_pipeline_characterization
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.playlist_service as ps

GOLDEN = Path(__file__).parent / "fixtures" / "playlist_pipeline_golden.json"
PID = "pl_" + "a" * 32


def _t(title, **extra):
    return {"artist": "Artist", "title": title, **extra}


class Pipeline:
    """A fake world around the pipeline: providers, library, manifest, Plex."""

    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.library = set()          # titles Beets can match
        self.offers = {}              # title -> "ok" | "review" | "none" | "boom"
        self.import_error = None
        self.import_ignores = set()   # imported, yet the library never matches them
        self.mismatch = set()         # matched in Beets, AcoustID disagrees
        self.placement = {}
        self.manifest = {"desired_tracks": []}
        self.manifest_states = {}
        self.parsed = None
        self.cancel_after = None      # (event name, title or None)
        self.pause = False
        self.cancel = threading.Event()
        self.events = []
        self.saves = []
        self.state = None
        self.contract = mock.Mock()
        self.enter_error = None
        self.safe_staged = True

    def _event(self, *row):
        self.events.append(list(row))
        trigger = self.cancel_after
        if trigger and row[0] == trigger[0] and (trigger[1] is None or row[1] == trigger[1]):
            if self.pause and self.state is not None:
                self.state["pause_requested"] = True
            self.cancel.set()

    # providers
    def slskd(self, artist, title, *_a, **_k):
        self._event("slskd", title)
        offer = self.offers.get(title, "none")
        if offer == "boom":
            raise RuntimeError("provider down")
        return [f"{self.tmp}/{offer}-{title}.flac"] if offer in {"ok", "review"} else []

    def validate(self, path, artist, title, expected="", **_k):
        self.events.append(["validate", title, expected])
        ok = "/review-" not in path
        return {
            "audio_allowed": True,
            "size": 123,
            "match": {
                "ok": ok,
                "identity": {"final_action": "accept" if ok else "review",
                             "decision_reason": "accepted" if ok else "fingerprint unclear"},
            },
        }

    # library
    def match(self, tracks, verify_acoustid=False):
        self._event("match", [t["title"] for t in tracks], verify_acoustid)
        matched, missing = [], []
        for track in tracks:
            if track["title"] in self.library:
                row = dict(track, path=f"/lib/{track['title']}.flac")
                if track["title"] in self.mismatch:
                    row["acoustid_status"] = "mismatch"
                matched.append(row)
            else:
                missing.append(dict(track))
        return matched, missing

    def run_import(self, name, log, cancel_event=None, playlist_id=None):
        self._event("import", name, playlist_id)
        if self.import_error:
            raise RuntimeError(self.import_error)
        count = 0
        for row in self.manifest_states.values():
            if row.get("status") == "waiting_import":
                count += 1
                if row["title"] not in self.import_ignores:
                    self.library.add(row["title"])
                    row["status"] = "available"
        return {"imported_files": count, "placement": self.placement}

    # manifest
    def store(self, name, track, status, **fields):
        key = ps._playlist_status_id(track)
        row = {"artist": track.get("artist"), "title": track.get("title"), "status": status}
        row.update({k: v for k, v in fields.items() if k in {"staged_path", "message"}})
        self.manifest_states[key] = row

    def write_manifest(self, name, tracks, matched_tracks=None, missing_tracks=None, source="", content="", log=None):
        self.events.append(["write_manifest", [t["title"] for t in tracks], source, content])
        self.manifest["desired_tracks"] = list(tracks)

    def staged_entries(self, name, playlist_id=None):
        return [
            ({"artist": row["artist"], "title": row["title"]}, Path(self.tmp) / f"{row['title']}.flac")
            for row in self.manifest_states.values() if row.get("status") == "waiting_import"
        ]

    def outputs(self, name, items, log=None, replace_plex=True, wait_for_plex_seconds=0,
                desired_tracks=None, missing_tracks=None, source="", content=""):
        self.events.append(["outputs", [t["title"] for t in items], [t["title"] for t in desired_tracks or []],
                            [t["title"] for t in missing_tracks or []], source, content, replace_plex])
        return {"plex": dict(self.plex_result)}

    plex_result = {"status": "synced"}

    def parse(self, payload):
        self.events.append(["parse", payload["source"], payload["content"]])
        return self.parsed

    def save_spy(self, real):
        def save(state):
            self.state = state
            self.saves.append([state.get("status"), state.get("phase"), state.get("current"),
                               state.get("done"), state.get("failed"), state.get("total"),
                               state.get("round"), state.get("waiting_for_import")])
            real(state)
        return save

    def run(self, payload, *, job_dir, batch=0, max_rounds="8"):
        """Start the job, run its body inline; (http body, http status, result, error)."""
        real_save = ps._playlist_save_job_state
        captured = {}

        def start_python(fn, **kwargs):
            captured["fn"] = fn
            captured["kwargs"] = kwargs
            return mock.Mock(job_id="job-1")

        self.saves.clear()
        self.events.clear()
        patches = {
            "_playlist_resolve_stable_id": mock.Mock(return_value=PID),
            "_playlist_key": mock.Mock(return_value="pkey"),
            "_playlist_ensure_staging_dirs": mock.Mock(),
            "_playlist_downloads_dir": mock.Mock(return_value=Path(self.tmp)),
            "_playlist_imports_dir": mock.Mock(return_value=Path(self.tmp) / "imports"),
            "_playlist_running_pipeline_job_id": mock.Mock(return_value=""),
            "_playlist_resolve_operation_key": mock.Mock(return_value="pkey"),
            "_playlist_filter_preview_downloads": lambda paths, log: list(paths),
            "_playlist_stamp_download_tags": mock.Mock(),
            "_playlist_reusable_download_files": mock.Mock(return_value=[]),
            "_is_safe_playlist_staged_file": lambda *_a, **_k: self.safe_staged,
            "_playlist_slskd_download_track": self.slskd,
            "_playlist_validate_staged_download": self.validate,
            "_match_playlist_tracks": self.match,
            "_playlist_run_import_downloaded": self.run_import,
            "_playlist_store_track_state": self.store,
            "_playlist_read_manifest": lambda *_a, **_k: self.manifest,
            "_playlist_write_manifest": self.write_manifest,
            "_playlist_manifest_track_states": lambda *_a, **_k: self.manifest_states,
            "_playlist_staged_entries": self.staged_entries,
            "_playlist_state_for_track": lambda name, track, **_k: self.manifest_states.get(
                ps._playlist_status_id(track)) or {},
            "_create_playlist_outputs": lambda *a, **k: self.outputs(*a, **k),
            "parse_playlist_request": self.parse,
            "_json_from_flask_response": lambda value: value,
            "_invalidate_lib_cache": mock.Mock(),
            "_playlist_record_pipeline": lambda name, **fields: self.events.append(
                ["pipeline", {k: fields[k] for k in sorted(fields)}]),
            "_playlist_save_job_state": self.save_spy(real_save),
            "PLAYLIST_JOB_STATE_DIR": Path(job_dir),
            "PLAYLIST_DOWNLOAD_BATCH_SIZE": batch,
            "PLAYLIST_DOWNLOAD_METHODS": "slskd",
        }
        started = [mock.patch.object(ps, key, value) for key, value in patches.items()]
        started += [
            mock.patch.object(ps.jobs, "start_python", start_python),
            mock.patch.object(ps.job_contract, "enter", return_value=self.contract, side_effect=self.enter_error),
            mock.patch.object(ps.time, "sleep"),
            mock.patch.object(ps.composite_workflows, "list_playlist_staged_files",
                              return_value={"ok": True, "files": []}),
            mock.patch.object(ps.composite_workflows, "delete_playlist_staged_track"),
            mock.patch.dict(os.environ, {"PLAYLIST_DOWNLOAD_MAX_ROUNDS": max_rounds}),
        ]
        for patcher in started:
            patcher.start()
        try:
            body, status = ps.start_playlist_download(payload)
            result, error = None, None
            if "fn" in captured:
                try:
                    result = captured["fn"]([], self.cancel, mock.Mock())
                except Exception as exc:  # the job engine records it
                    error = f"{type(exc).__name__}: {exc}"
            self.state = ps._pl_dl_jobs.get(body.get("job_id"), self.state)
            return body, status, result, error
        finally:
            for patcher in reversed(started):
                patcher.stop()

    def trace(self, outcome):
        body, status, result, error = outcome
        state = self.state or {}
        statuses = {
            key: {k: row.get(k) for k in ("title", "status", "method", "message", "path",
                                          "download_attempt_count", "file_size", "source_artist") if k in row}
            for key, row in sorted((state.get("track_statuses") or {}).items())
        }
        text = json.dumps({
            "http": [{k: v for k, v in body.items() if k not in {"job_id"}}, status],
            "result": {k: v for k, v in (result or {}).items() if k != "playlist_job_id"},
            "error": error,
            "events": self.events,
            "saves": self.saves,
            "log": state.get("log"),
            "statuses": statuses,
            "final": {k: state.get(k) for k in (
                "status", "phase", "current", "done", "failed", "total", "round", "max_rounds", "resumed",
                "matched_initial", "missing_initial", "matched_after_import", "missing_after_import",
                "review_required", "waiting_for_import", "import_returncode", "download_batch_remaining")},
            "matched": [t.get("title") for t in state.get("matched") or []],
            "missing": [t.get("title") for t in state.get("missing") or []],
        }, sort_keys=True, default=str)
        return json.loads(text.replace(self.tmp.replace("\\", "\\\\"), "<TMP>").replace(self.tmp, "<TMP>").replace("<TMP>\\\\", "<TMP>/"))


class PipelineCase(unittest.TestCase):
    update = bool(os.environ.get("UPDATE_PLAYLIST_GOLDEN"))
    collected = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.jobs = self.tmp / "jobs"
        self.jobs.mkdir()
        self.dl = self.tmp / "dl"
        self.dl.mkdir()
        self.world = Pipeline(self.dl)
        ps._pl_dl_jobs.clear()
        self.addCleanup(ps._pl_dl_jobs.clear)

    def go(self, payload, **kw):
        return self.world.run(payload, job_dir=self.jobs, **kw)

    def golden(self, scenario, outcome):
        trace = self.world.trace(outcome)
        if self.update:
            data = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
            data[scenario] = trace
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            return trace
        self.maxDiff = None
        self.assertEqual(json.loads(GOLDEN.read_text(encoding="utf-8"))[scenario], trace)
        return trace

    def payload(self, **kw):
        base = {"name": "Mix", "tracks": [_t("A"), _t("B")], "all_tracks": [_t("A"), _t("B"), _t("C")],
                "sync_after_import": True, "methods": ["slskd"]}
        base.update(kw)
        return base

    @staticmethod
    def titles(trace, event):
        return [row for row in trace["events"] if row[0] == event]


class SourceResolution(PipelineCase):
    def test_parse_writes_manifest_and_drops_tombstoned_tracks(self):
        w = self.world
        w.library = {"Kept"}
        w.manifest["removed_tracks"] = [_t("Gone")]
        w.parsed = {"ok": True, "tracks": [_t("Kept"), _t("Want"), _t("Gone")],
                    "matched": [_t("Kept")], "missing": [_t("Want"), _t("Gone")]}
        w.offers = {"Want": "ok"}
        trace = self.golden("parse_fresh", self.go(self.payload(
            tracks=[], all_tracks=[], source="url", content="https://example.test/list")))
        self.assertEqual(self.titles(trace, "write_manifest")[0][1], ["Kept", "Want"])
        self.assertEqual([row[1] for row in self.titles(trace, "slskd")], ["Want"])

    def test_parse_failure_is_fatal_and_recorded(self):
        self.world.parsed = {"ok": False, "error": "source gone"}
        trace = self.golden("parse_error", self.go(self.payload(
            tracks=[], all_tracks=[], source="url", content="https://example.test/list")))
        self.assertEqual(trace["error"], "RuntimeError: source gone")
        self.assertEqual(self.titles(trace, "pipeline")[-1][1]["status"], "failed")
        self.world.contract.close.assert_called_once()

    def test_sync_only_checks_every_track_and_downloads_nothing(self):
        self.world.library = {"A"}
        trace = self.golden("sync_only", self.go(self.payload(tracks=[], all_tracks=[_t("A"), _t("B")])))
        self.assertEqual(self.titles(trace, "slskd"), [])
        self.assertEqual(self.titles(trace, "outputs")[0][1], ["A"])

    def test_no_tracks_refused_before_a_job_exists(self):
        body, status = ps.start_playlist_download({"name": "Mix"})
        self.assertEqual((body["ok"], status), (False, 200))


class Acquisition(PipelineCase):
    def test_full_pipeline_keeps_wanted_context_and_membership(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "ok"}
        wanted = _t("A", mb_trackid="mbid-a", album="SoundCloud Missing", source_artist="Uploader")
        trace = self.golden("full", self.go(self.payload(tracks=[wanted, _t("B")],
                                                         all_tracks=[wanted, _t("B"), _t("C")])))
        # The wanted recording ID reaches validation; a source album label is not identity.
        self.assertIn(["validate", "A", "mbid-a"], trace["events"])
        output = self.titles(trace, "outputs")[-1]
        self.assertCountEqual(output[1], ["A", "B", "C"])
        self.assertEqual(output[2], ["A", "B", "C"])  # membership stays in order
        # Each wanted track is fetched once.
        self.assertEqual([row[1] for row in self.titles(trace, "slskd")], ["A", "B"])
        self.assertEqual(trace["final"]["status"], "done")

    def test_placeholder_album_is_not_carried_into_final_tracks(self):
        w = self.world
        w.offers = {"A": "ok"}
        seen = {}
        original = w.outputs

        def outputs(name, items, **kw):
            seen["desired"] = kw["desired_tracks"]
            return original(name, items, **kw)

        w.outputs = outputs
        wanted = _t("A", album="SoundCloud Missing", mb_trackid="mbid-a")
        self.go(self.payload(tracks=[wanted], all_tracks=[wanted]))
        # The source label is dropped; the wanted recording ID survives.
        self.assertNotIn("album", seen["desired"][0])
        self.assertEqual(seen["desired"][0]["mb_trackid"], "mbid-a")

    def test_partial_completion_imports_verified_and_holds_review(self):
        w = self.world
        w.offers = {"A": "ok", "B": "review"}
        trace = self.golden("partial", self.go(self.payload(all_tracks=[_t("A"), _t("B")])))
        self.assertEqual(len(self.titles(trace, "import")), 1)
        self.assertEqual(self.titles(trace, "outputs")[-1][1], ["A"])
        self.assertEqual(trace["final"]["review_required"], 1)

    def test_only_review_stops_before_import(self):
        self.world.offers = {"A": "review", "B": "review"}
        trace = self.golden("only_review", self.go(self.payload(all_tracks=[_t("A"), _t("B")])))
        self.assertEqual(self.titles(trace, "import"), [])
        self.assertEqual(trace["error"], None)

    def test_nothing_matched_is_an_error(self):
        trace = self.golden("no_match", self.go(self.payload()))
        self.assertIn("No playlist tracks matched", trace["error"])
        self.world.contract.close.assert_called_once()

    def test_provider_failure_falls_through_without_aborting(self):
        self.world.library = {"C"}
        self.world.offers = {"A": "boom", "B": "ok"}
        trace = self.golden("provider_boom", self.go(self.payload()))
        self.assertEqual(trace["final"]["status"], "done")
        self.assertEqual(trace["final"]["done"], 1)
        # A is retried in the round after B's import made progress.
        self.assertEqual([row[1] for row in self.titles(trace, "slskd")], ["A", "B", "A"])

    def test_unreduced_missing_list_does_not_retry_forever(self):
        w = self.world
        w.offers = {"A": "ok", "B": "none"}
        w.import_ignores = {"A"}
        w.library = {"C"}
        trace = self.golden("no_progress", self.go(self.payload()))
        # C's match counts as progress for round 1; round 2 changes nothing and stops.
        self.assertEqual([row[1] for row in self.titles(trace, "slskd")], ["A", "B", "A", "B"])
        self.assertEqual(trace["final"]["round"], 2)

    def test_max_rounds_bounds_the_loop(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "ok"}
        trace = self.golden("batched_max_rounds", self.go(self.payload(), batch=1, max_rounds="1"))
        self.assertEqual([row[1] for row in self.titles(trace, "slskd")], ["A"])

    def test_batches_round_robin_until_done(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "ok"}
        trace = self.golden("batched", self.go(self.payload(), batch=1))
        self.assertEqual([row[1] for row in self.titles(trace, "slskd")], ["A", "B"])

    def test_download_only_stops_after_staging(self):
        self.world.offers = {"A": "ok", "B": "ok"}
        trace = self.golden("download_only", self.go(self.payload(
            sync_after_import=False, download_only=True)))
        self.assertEqual(self.titles(trace, "import"), [])
        self.assertEqual(self.titles(trace, "outputs"), [])

    def test_mismatch_and_failed_placement_go_to_review(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "ok"}
        w.mismatch = {"A"}
        w.placement = {"failed": 1, "placed": 0, "matched_candidates": 1}
        trace = self.golden("review_after_import", self.go(self.payload()))
        rows = {row["title"]: row["status"] for row in trace["statuses"].values()}
        self.assertEqual((rows["A"], rows["B"]), ("review_required", "review_required"))
        self.assertEqual(self.titles(trace, "outputs")[-1][1], ["C"])

    def test_plex_partial_and_failure_are_reported_not_fatal(self):
        w = self.world
        w.library = {"C"}
        for scenario, plex in (("plex_partial", {"status": "partial"}), ("plex_error", {"error": "down"})):
            w.plex_result = plex
            trace = self.golden(scenario, self.go(self.payload(tracks=[], all_tracks=[_t("C")])))
            self.assertEqual(trace["final"]["status"], "done")


class CancelAndResume(PipelineCase):
    def test_cancel_before_a_round_stops(self):
        self.world.cancel.set()
        trace = self.golden("cancel_before", self.go(self.payload()))
        self.assertEqual(trace["final"]["status"], "stopped")
        self.assertEqual(self.titles(trace, "slskd"), [])

    def test_pause_during_download_resumes_only_the_rest(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "ok"}
        w.cancel_after = ("slskd", "A")
        w.pause = True
        first = self.golden("pause_first", self.go(self.payload()))
        self.assertEqual(first["final"]["status"], "paused")
        self.assertEqual(self.titles(first, "import"), [])
        # A new run of the same job: A was staged (waiting_import), B was not.
        w.cancel = threading.Event()
        w.cancel_after = None
        ps._pl_dl_jobs.clear()
        second = self.golden("pause_resume", self.go(self.payload()))
        self.assertTrue(second["final"]["resumed"])
        # A is imported from its staged file, not fetched again. C was never
        # prechecked before the pause, so resume treats it as missing (known quirk,
        # reported on #505, pinned so a refactor cannot change it by accident).
        self.assertEqual([row[1] for row in self.titles(second, "slskd")], ["B", "C"])
        self.assertEqual(trace_output_titles(second), ["A", "B", "C"])

    def test_resume_with_parse_uses_manifest_without_refetching(self):
        w = self.world
        w.library = {"Kept"}
        w.offers = {"Want": "none"}
        w.parsed = {"ok": True, "tracks": [_t("Kept"), _t("Want")], "matched": [_t("Kept")], "missing": [_t("Want")]}
        payload = self.payload(tracks=[], all_tracks=[], source="url", content="https://example.test/list")
        self.go(payload)
        ps._pl_dl_jobs.clear()
        w.offers = {"Want": "ok"}
        trace = self.golden("parse_resume", self.go(payload))
        self.assertEqual(self.titles(trace, "parse"), [])
        self.assertEqual(trace_output_titles(trace), ["Kept", "Want"])

    def test_resume_with_empty_manifest_falls_back_to_fetch(self):
        w = self.world
        w.parsed = {"ok": True, "tracks": [_t("X")], "matched": [], "missing": [_t("X")]}
        payload = self.payload(tracks=[], all_tracks=[], source="url", content="https://example.test/list",
                               sync_after_import=False)
        self.go(payload)
        ps._pl_dl_jobs.clear()
        w.manifest["desired_tracks"] = []
        trace = self.golden("parse_resume_empty", self.go(payload))
        self.assertEqual(len(self.titles(trace, "parse")), 1)

    def test_resume_import_failure_only_warns(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "none"}
        w.cancel_after = ("slskd", "B")
        self.go(self.payload())
        w.cancel = threading.Event()
        w.cancel_after = None
        ps._pl_dl_jobs.clear()
        w.import_error = "import busy"
        trace = self.golden("resume_import_warning", self.go(self.payload()))
        self.assertTrue(any("pre-download import warning" in line for line in trace["log"]))

    def test_cancel_during_resume_import_stops_at_checkpoint(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "none"}
        w.cancel_after = ("slskd", "B")
        self.go(self.payload())
        w.cancel = threading.Event()
        w.cancel_after = ("import", None)
        ps._pl_dl_jobs.clear()
        trace = self.golden("resume_cancel_in_import", self.go(self.payload()))
        self.assertEqual(trace["final"]["status"], "stopped")
        self.assertEqual(self.titles(trace, "slskd"), [])

    def test_stale_staged_file_goes_back_to_pending_on_resume(self):
        w = self.world
        w.library = {"C"}
        w.offers = {"A": "ok", "B": "none"}
        w.cancel_after = ("slskd", "B")
        self.go(self.payload())
        w.cancel = threading.Event()
        w.cancel_after = None
        ps._pl_dl_jobs.clear()
        w.safe_staged = False  # the checkpointed file vanished
        trace = self.golden("resume_stale_staged", self.go(self.payload()))
        self.assertTrue(any("will re-download" in line for line in trace["log"]))
        self.assertIn("A", [row[1] for row in self.titles(trace, "slskd")])


class Locks(PipelineCase):
    def test_second_pipeline_for_a_playlist_is_refused_and_lock_is_released(self):
        lock = ps._playlist_pipeline_runtime_lock("Mix")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            trace = self.golden("locked", self.go(self.payload()))
        finally:
            lock.release()
        self.assertIn(ps._PLAYLIST_DUPLICATE_JOB_MESSAGE, trace["error"])
        self.world.contract.close.assert_not_called()

    def test_contract_refusal_releases_the_runtime_lock(self):
        self.world.enter_error = RuntimeError("held elsewhere")
        trace = self.golden("contract_refused", self.go(self.payload()))
        self.assertEqual(trace["error"], "RuntimeError: held elsewhere")
        self.assertEqual(trace["final"]["status"], "error")
        lock = ps._playlist_pipeline_runtime_lock("Mix")
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_lock_is_free_after_a_run(self):
        self.go(self.payload(tracks=[], all_tracks=[_t("Q")], sync_after_import=False))
        lock = ps._playlist_pipeline_runtime_lock("Mix")
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()


class SummaryPayloads(unittest.TestCase):
    """Exact list/detail payloads, so consolidating their builders changes no key."""

    ZERO = {"quality_bad": 0, "quality_review": 0, "downloaded": 0, "imported": 0, "failed": 0,
            "review_required": 0, "removed": 0, "excluded": 0, "plex_synced_count": 0}
    MANIFEST = {
        "desired_tracks": [_t("A"), _t("B"), _t("C"), _t("D")],
        "track_states": {
            "a": {"status": "available"}, "b": {"status": "waiting_import"},
            "c": {"status": "review_required"}, "d": {"status": "plex_synced"},
            "e": {"status": "downloaded"}, "f": {"status": "failed"}, "g": {"status": "imported"},
            "h": "not a row",
        },
        "removed_tracks": [_t("X")],
        "excluded_tracks": [_t("Y"), _t("Z")],
        "source": "url",
        "content": "https://example.test/list",
        "last_plex": {"status": "partial"},
        "last_pipeline": {"status": "done"},
    }

    def setUp(self):
        patcher = mock.patch.multiple(
            ps,
            _playlist_apply_tombstones=lambda name, tracks, manifest=None: tracks,
            _playlist_existing_key=lambda *_a, **_k: "pkey",
            _playlist_manifest_path=lambda *_a, **_k: Path("/state/mix.playlist.json"),
            _playlist_manifest_exists_no_create=lambda *_a, **_k: True,
            _playlist_latest_job_state_summary=lambda *_a, **_k: {},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_manifest_list_summary(self):
        self.assertEqual(ps._playlist_manifest_list_summary("Mix", self.MANIFEST), {
            **self.ZERO, "tracks": 4, "available": 3, "missing": 1, "quality_review": 1, "m3u_tracks": 4,
            "desired_source": "url", "downloaded": 2, "imported": 2, "failed": 1, "review_required": 1,
            "removed": 1, "excluded": 2, "plex_synced_count": 1,
        })

    def test_manifest_list_summary_keeps_stored_counts(self):
        manifest = {**self.MANIFEST, "matched_count": 9, "missing_count": 1}
        summary = ps._playlist_manifest_list_summary("Mix", manifest)
        self.assertEqual((summary["tracks"], summary["available"], summary["missing"]), (10, 9, 1))

    def test_checkpoint_list_summary(self):
        checkpoint = {"checkpoint_tracks": 5, "checkpoint_missing": 2, "checkpoint_waiting_for_import": 1}
        with mock.patch.object(ps, "_playlist_latest_job_state_summary", return_value=checkpoint), \
                mock.patch.object(ps.composite_workflows, "read_playlist_m3u", return_value={"ok": True, "exists": False}):
            summary = ps._playlist_checkpoint_list_summary("Mix")
        self.assertEqual(summary, {
            **self.ZERO, **checkpoint, "tracks": 5, "available": 3, "missing": 2, "m3u_tracks": 0,
            "desired_source": "checkpoint", "downloaded": 1,
        })

    def test_engine_m3u_count_summary(self):
        with mock.patch.object(ps.composite_workflows, "read_playlist_m3u",
                               return_value={"ok": True, "exists": True, "items": [{}, {}, "bad"]}):
            self.assertEqual(ps._playlist_engine_m3u_count_summary("Mix"), {
                **self.ZERO, "tracks": 2, "available": 0, "missing": 2, "m3u_tracks": 2, "desired_source": "m3u",
            })

    def test_m3u_row_summary(self):
        rows = {"tracks": [_t("A"), _t("B"), _t("C")], "m3u_tracks": 3, "desired_source": "m3u",
                "matched": [dict(_t("A"), quality="bad"), dict(_t("B"), quality="review")], "missing": [_t("C")]}
        counts = {"downloaded": 1, "waiting_import": 1, "imported": 1, "plex_synced": 1, "failed": 1,
                  "review_required": 1, "removed": 1, "excluded": 1}
        with mock.patch.object(ps, "_playlist_rows_for_saved_playlist", return_value=rows), \
                mock.patch.object(ps, "_playlist_pipeline_counts", return_value=counts), \
                mock.patch.object(ps.composite_workflows, "read_playlist_m3u", return_value={"ok": True, "exists": True}):
            summary = ps._playlist_m3u_summary("Mix", {"loaded": True}, manifest={})
        self.assertEqual(summary, {
            **self.ZERO, "tracks": 3, "available": 2, "missing": 1, "quality_bad": 1, "quality_review": 1,
            "m3u_tracks": 3, "desired_source": "m3u", "downloaded": 2, "imported": 2, "failed": 1,
            "review_required": 1, "removed": 1, "excluded": 1, "plex_synced_count": 1,
        })

    def common(self, **over):
        return {
            "ok": True, "name": "Mix", "m3u": "engine:pkey.m3u", "manifest": str(Path("/state/mix.playlist.json")),
            "manifest_tracks": 4, "source": "url", "source_content": "https://example.test/list",
            "last_plex": {"status": "partial"}, "last_pipeline": {"status": "done"}, "last_sync_status": "partial",
            **over,
        }

    def test_detail_summary_payload(self):
        with mock.patch.object(ps, "_playlist_read_manifest", return_value=self.MANIFEST):
            payload = ps._playlist_detail_summary_payload("Mix")
        counts = payload.pop("counts")
        self.assertEqual((counts["waiting_import"], counts["removed"], counts["excluded"]), (1, 1, 2))
        self.assertEqual(payload, self.common(
            m3u_tracks=4, desired_source="url", tracks=[], matched=[], missing=[], removed_excluded=[],
            available=3, missing_count=1, total=4, detail_mode="summary", tracks_loaded=False))

    def test_detail_full_payload(self):
        rows = {"tracks": [_t("A"), _t("B")], "m3u_tracks": 2, "desired_source": "manifest",
                "matched": [_t("A")], "missing": [_t("B")]}
        with mock.patch.object(ps, "_playlist_read_manifest", return_value=self.MANIFEST), \
                mock.patch.object(ps, "_playlist_rows_for_saved_playlist", return_value=rows), \
                mock.patch.object(ps, "_playlist_manifest_track_states", return_value={}), \
                mock.patch.object(ps, "_playlist_track_with_state", side_effect=lambda n, row, *_a: row), \
                mock.patch.object(ps, "_playlist_tombstone_rows", return_value=[]), \
                mock.patch.object(ps, "_playlist_pipeline_counts", return_value={"failed": 1}):
            payload = ps._playlist_detail_payload("Mix", {"loaded": True})
        self.assertEqual(payload, self.common(
            m3u_tracks=2, desired_source="manifest", tracks=rows["tracks"], matched=[_t("A")], missing=[_t("B")],
            removed_excluded=[], counts={"failed": 1}, available=1, missing_count=1, total=2,
            detail_mode="full", tracks_loaded=True))


def trace_output_titles(trace):
    outputs = [row for row in trace["events"] if row[0] == "outputs"]
    return sorted(outputs[-1][1])


if __name__ == "__main__":
    unittest.main()
