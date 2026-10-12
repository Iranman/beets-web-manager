"""Playlist sync: "Web Manager list, synced in place".

The Web Manager manifest is the master copy; Plex is edited with item
add/remove/move so its ratingKey survives; a second run with no changes
makes zero Plex writes; a run that fails halfway is finished by the next;
legacy <MUSIC_ROOT>/playlists files are imported once, read-only; the
auto-sync starts tracked jobs that never overlap a manual sync.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  -- loads the module family in order
import backend.composite_workflows as cw
import backend.playlist_service as ps
import tests.test_integrations_reliability as tir
from job_engine import DuplicateJobError
from tests.test_integrations_reliability import _FakePlex, http_error


def _keys(*keys):
    return [str(k) for k in keys]


class _Flaky:
    """Wrap a fake Plex so the ``nth`` write matching ``kind`` fails once."""

    def __init__(self, fake, kind, nth, error=None):
        self.fake, self.kind, self.nth = fake, kind, nth
        self.error = error or http_error(500)
        self.seen = 0
        self.fired = False

    def _kind(self, method, path):
        if method == "DELETE":
            return "delete"
        if method == "PUT":
            return "move" if path.endswith("/move") else "append"
        return ""

    def request(self, path, params=None, *, method="GET", **kw):
        if not self.fired and self._kind(method, path) == self.kind:
            self.seen += 1
            if self.seen == self.nth:
                self.fired = True
                raise self.error
        return self.fake.request(path, params, method=method, **kw)


class InPlaceSyncTests(unittest.TestCase):
    def _run(self, plex, desired, removable=None):
        with mock.patch.multiple(ps, _plex_request=plex.request, PLEX_PLAYLIST_CHUNK_SIZE=2):
            return ps._plex_sync_playlist_in_place("machine", "100", desired, removable=removable)

    def test_add_move_keep_duplicates_then_zero_writes(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": _keys(3, 1, 1, 9)}})
        desired = _keys(1, 2, 3, 1)
        first = self._run(fake, desired)
        # 9 was added in Plex: kept, after the Web Manager order; the
        # duplicate 1 is kept as a duplicate.
        self.assertEqual(fake.keys("100"), _keys(1, 2, 3, 1, 9))
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["removed"], 0)
        calls = len(fake.writes())
        self.assertGreater(calls, 0)

        second = self._run(fake, desired)
        self.assertEqual(second["writes"], 0)
        self.assertEqual(len(fake.writes()), calls)  # zero Plex write requests
        self.assertEqual(list(fake.playlists), ["100"])

    def test_only_tombstoned_extras_are_removed(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": _keys(1, 5, 6, 1)}})
        removed = self._run(fake, _keys(1), removable=lambda t: t.get("title") == "T5")
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(fake.keys("100"), _keys(1, 6, 1))
        # A removal is never re-sent: the next run writes nothing.
        self.assertEqual(self._run(fake, _keys(1), removable=lambda t: t.get("title") == "T5")["writes"], 0)

    def test_failure_between_steps_is_finished_by_the_next_run(self):
        start = {"100": {"title": "Mix", "items": _keys(4, 3, 7)}}
        desired = _keys(1, 2, 3, 4, 5)
        removable = (lambda t: t.get("title") == "T7")
        for kind, nth in (("delete", 1), ("append", 1), ("append", 2), ("move", 1), ("move", 2)):
            with self.subTest(kind=kind, nth=nth):
                fake = _FakePlex(start)
                flaky = _Flaky(fake, kind, nth)
                with self.assertRaises(Exception):
                    self._run(flaky, desired, removable)
                self.assertTrue(flaky.fired)
                self.assertEqual(list(fake.playlists), ["100"])  # never deleted/recreated
                self._run(fake, desired, removable)
                self.assertEqual(fake.keys("100"), desired)
                self.assertEqual(self._run(fake, desired, removable)["writes"], 0)

    def test_malformed_entries_refuse_the_edit(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": _keys(1)}})

        def request(path, params=None, *, method="GET", **kw):
            if method == "GET" and path.endswith("/items"):
                return {"MediaContainer": {"Metadata": [{"ratingKey": "1"}]}}  # no playlistItemID
            return fake.request(path, params, method=method, **kw)

        with mock.patch.multiple(ps, _plex_request=request):
            with self.assertRaises(RuntimeError):
                ps._plex_sync_playlist_in_place("machine", "100", _keys(2))
        self.assertEqual(fake.writes(), [])


class CreateOutputsIdempotencyTests(unittest.TestCase):
    """Through _create_playlist_outputs, with the manifest's stored ratingKey."""
    ITEMS = tir.PlexPlaylistReplaceTests.ITEMS
    KEYS = tir.PlexPlaylistReplaceTests.KEYS

    def _sync(self, fake, manifest, keys=None):
        return tir.PlexPlaylistReplaceTests._sync(self, fake, manifest, keys)

    def test_second_sync_makes_no_plex_writes_and_keeps_the_rating_key(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": ["13", "11"]}})
        plex, last = self._sync(fake, {"last_plex": {"rating_key": "100"}})
        self.assertEqual(plex["status"], "success")
        writes = len(fake.writes())
        self.assertGreater(writes, 0)
        plex2, last2 = self._sync(fake, {"last_plex": last})
        self.assertEqual(plex2["plex_writes"], 0)
        self.assertEqual(len(fake.writes()), writes)
        self.assertEqual(last2["rating_key"], "100")
        self.assertEqual(fake.keys("100"), self.KEYS)

    def test_tombstoned_track_is_removed_from_plex(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": ["11", "12", "13", "99"]}})
        manifest = {"last_plex": {"rating_key": "100"},
                    "removed_tracks": [{"artist": "A", "title": "T99"}]}
        plex, _ = self._sync(fake, manifest)
        self.assertEqual(plex["tracks_removed"], 1)
        self.assertEqual(fake.keys("100"), self.KEYS)

    def test_untombstoned_plex_extra_is_kept(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": ["11", "12", "13", "99"]}})
        plex, _ = self._sync(fake, {"last_plex": {"rating_key": "100"}})
        self.assertEqual(plex["tracks_removed"], 0)
        self.assertEqual(fake.keys("100"), self.KEYS + ["99"])


class SyncOneTests(unittest.TestCase):
    """The manifest is the master copy; M3U/Plex additions merge in."""

    def _item(self, i):
        return {"id": i, "artist": "A", "title": f"T{i}", "path": f"/music/{i}.flac"}

    def _run(self, *, local, plex, manifest=None, missing=()):
        matched = [self._item(1), self._item(2)]
        outputs = mock.Mock(return_value={"plex": {"created": True, "mode": "in_place", "plex_writes": 2,
                                                   "rating_key": "100", "pending_tracks": [
                                                       {"artist": "A", "title": "T2",
                                                        "reason": "file exists but Plex has not indexed it"}]}})
        with mock.patch.multiple(
                ps,
                _playlist_read_manifest=mock.Mock(return_value=dict(manifest or {})),
                _playlist_desired_tracks_for_name=mock.Mock(return_value=(
                    [{"artist": "A", "title": "T1"}, {"artist": "A", "title": "T2"}], "manifest")),
                _playlist_match_reference_tracks=mock.Mock(return_value=(matched, list(missing))),
                _playlist_m3u_items=mock.Mock(return_value=(local, 0)),
                _plex_playlist_items=mock.Mock(return_value=(plex, 0)),
                _create_playlist_outputs=outputs):
            detail = ps._playlist_sync_one("Mix", {}, [{"ratingKey": "100", "title": "Mix"}], [])
        return detail, outputs

    def test_unchanged_list_writes_nothing(self):
        same = [self._item(1), self._item(2)]
        detail, outputs = self._run(local=same, plex=same)
        outputs.assert_not_called()
        self.assertTrue(detail["plex_synced"])
        self.assertFalse(detail["updated"])

    def test_plex_and_m3u_additions_are_merged_in_after_the_saved_tracks(self):
        local = [self._item(1), self._item(2), self._item(3)]
        plex = [self._item(4), self._item(1)]
        detail, outputs = self._run(local=local, plex=plex)
        written = outputs.call_args.args[1]
        self.assertEqual([i["id"] for i in written], [1, 2, 3, 4])
        self.assertEqual(detail["external_added"], 2)
        desired = outputs.call_args.kwargs["desired_tracks"]
        self.assertEqual([t["title"] for t in desired], ["T1", "T2", "T3", "T4"])
        self.assertTrue(outputs.call_args.kwargs["replace_plex"])

    def test_tombstoned_addition_is_not_merged_back(self):
        manifest = {"removed_tracks": [{"artist": "A", "title": "T4"}]}
        detail, outputs = self._run(local=[self._item(1), self._item(2)],
                                    plex=[self._item(1), self._item(2), self._item(4)], manifest=manifest)
        written = outputs.call_args.args[1]
        self.assertEqual([i["id"] for i in written], [1, 2])
        self.assertEqual(detail["external_added"], 0)

    def test_pending_tracks_carry_a_reason(self):
        detail, _ = self._run(local=[], plex=[], missing=[{"artist": "B", "title": "Gone"}])
        reasons = {(p["title"], p["reason"]) for p in detail["pending"]}
        self.assertIn(("Gone", "not in the Beets library"), reasons)
        self.assertIn(("T2", "file exists but Plex has not indexed it"), reasons)
        self.assertEqual(detail["pending_count"], 2)


class SyncAllTests(unittest.TestCase):
    def _run(self, sync_one, names=("A", "B"), busy=""):
        patches = dict(
            _plex_status_payload=mock.Mock(return_value={"configured": True, "connected": True}),
            _playlist_ensure_state_dirs=mock.Mock(),
            _playlist_import_legacy_lists=mock.Mock(return_value=[]),
            _playlist_library_index=mock.Mock(return_value={}),
            _plex_audio_playlists=mock.Mock(return_value=[]),
            _playlist_web_manager_names=mock.Mock(return_value=list(names)),
            _playlist_running_pipeline_job_id=mock.Mock(side_effect=lambda n: "job" if n == busy else ""),
            _playlist_sync_one=sync_one,
        )
        with mock.patch.multiple(ps, **patches):
            return ps._playlist_sync_all([])

    def test_one_failing_list_does_not_stop_the_others(self):
        def sync_one(name, *_a):
            if name == "A":
                raise ValueError("boom")
            return {"name": name, "updated": True, "plex_writes": 3, "pending_count": 1}

        result = self._run(sync_one)
        self.assertEqual(result["playlists_failed"], 1)
        self.assertEqual(result["playlists_updated"], 1)
        self.assertEqual(result["plex_writes"], 3)
        failed = next(d for d in result["details"] if d["name"] == "A")
        self.assertEqual(failed["error_type"], "ValueError")
        self.assertNotIn("boom", json.dumps(result))

    def test_list_with_running_pipeline_job_is_skipped(self):
        sync_one = mock.Mock(return_value={"name": "B", "updated": False})
        result = self._run(sync_one, busy="A")
        self.assertEqual(result["playlists_skipped_busy"], 1)
        self.assertEqual([c.args[0] for c in sync_one.call_args_list], ["B"])

    def test_failed_lists_show_in_sync_status(self):
        with mock.patch.object(ps, "_playlist_sync_all",
                               return_value={"playlists_failed": 2, "details": []}):
            ps._playlist_sync_all_locked([])
        self.assertIn("2 playlist(s) failed", ps._PLAYLIST_SYNC_STATE["last_error"])
        self.assertFalse(ps._PLAYLIST_SYNC_STATE["running"])

    def test_fatal_error_is_recorded_without_its_text(self):
        with mock.patch.object(ps, "_playlist_sync_all", side_effect=OSError("/secret/path token=x")):
            with self.assertRaises(OSError):
                ps._playlist_sync_all_locked([])
        self.assertNotIn("secret", ps._PLAYLIST_SYNC_STATE["last_error"])
        self.assertTrue(ps._PLAYLIST_SYNC_STATE["last_error"])


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.legacy = base / "music" / "playlists"
        self.state = base / "state"
        self.legacy.mkdir(parents=True)
        self.state.mkdir()
        (self.legacy / "Road Trip.m3u").write_text(
            "#EXTM3U\n#EXTINF:1,A - One\n/music/a/1.flac\n#EXTINF:1,A - One\n/music/a/1.flac\n",
            encoding="utf-8")
        (self.legacy / "Chill.playlist.json").write_text(json.dumps(
            {"name": "Chill", "desired_tracks": [{"artist": "B", "title": "Two"}]}), encoding="utf-8")
        (self.legacy / "Existing.m3u").write_text("#EXTM3U\n/music/x.flac\n", encoding="utf-8")
        self.before = {p.name: p.read_bytes() for p in self.legacy.iterdir()}
        self.write = mock.Mock()

    def _import(self):
        with mock.patch.multiple(ps, PLAYLIST_LEGACY_IMPORT_DIR=self.legacy, PLAYLIST_STATE_ROOT=self.state,
                                 _playlist_web_manager_names=mock.Mock(return_value=["Existing"]),
                                 _playlist_write_manifest=self.write):
            return ps._playlist_import_legacy_lists([])

    def test_imported_once_read_only_and_never_over_an_existing_list(self):
        self.assertEqual(sorted(self._import()), ["Chill", "Road Trip"])
        by_name = {c.args[0]: c for c in self.write.call_args_list}
        self.assertNotIn("Existing", by_name)
        self.assertEqual(by_name["Road Trip"].kwargs["source"], "legacy_import")
        ledger = json.loads((self.state / "legacy_imports.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["existing"]["skipped"], "web_manager_list_exists")

        self.assertEqual(self._import(), [])  # the ledger stops a second import
        self.assertEqual(self.write.call_count, 2)
        # The legacy folder is never written to.
        self.assertEqual({p.name: p.read_bytes() for p in self.legacy.iterdir()}, self.before)

    def test_disabled_folder_imports_nothing(self):
        with mock.patch.multiple(ps, PLAYLIST_LEGACY_IMPORT_DIR=None, _playlist_write_manifest=self.write):
            self.assertEqual(ps._playlist_import_legacy_lists([]), [])
        self.write.assert_not_called()


class M3uContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(cw, "_get_playlist_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_returns_items_in_order_with_duplicates(self):
        items = [{"path": "/m/1.flac", "artist": "A", "title": "One"},
                 {"path": "/m/2.flac", "artist": "B", "title": "Two"},
                 {"path": "/m/1.flac", "artist": "A", "title": "One"}]
        cw.export_playlist_m3u("k", "Mix", items)
        got = cw.read_playlist_m3u("k")
        self.assertEqual([(i["path"], i["artist"], i["title"]) for i in got["items"]],
                         [(i["path"], i["artist"], i["title"]) for i in items])
        self.assertEqual(got["tracks"], ["/m/1.flac", "/m/2.flac", "/m/1.flac"])
        self.assertEqual(cw.read_playlist_m3u("missing")["items"], [])

    def test_unchanged_export_does_not_rewrite(self):
        items = [{"path": "/m/1.flac", "artist": "A", "title": "One"}]
        self.assertFalse(cw.export_playlist_m3u("k", "Mix", items)["unchanged"])
        with mock.patch.object(cw, "_write_text_atomic") as write:
            self.assertTrue(cw.export_playlist_m3u("k", "Mix", items)["unchanged"])
        write.assert_not_called()

    def test_refused_replace_leaks_no_temp_file(self):
        with mock.patch.object(cw.os, "replace", side_effect=PermissionError("acl")):
            with self.assertRaises(PermissionError):
                cw.export_playlist_m3u("k", "Mix", [{"path": "/m/1.flac", "artist": "A", "title": "One"}])
        self.assertEqual(os.listdir(self.dir), [])


class AtomicJsonTests(unittest.TestCase):
    def test_refused_replace_leaks_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.multiple(ps, PLAYLIST_STATE_ROOT=root), \
                    mock.patch.object(Path, "replace", side_effect=PermissionError("acl")), \
                    mock.patch.object(ps.time, "sleep"):
                with self.assertRaises(PermissionError):
                    ps._playlist_atomic_json_replace(root / "x.json", {"a": 1}, save_key="t")
            self.assertEqual(os.listdir(root), [])


class AutoSyncSchedulerTests(unittest.TestCase):
    def _tick(self, enabled=True, configured=True, start=None):
        with mock.patch.multiple(ps, PLAYLIST_AUTO_SYNC_ENABLED=enabled,
                                 _plex_configured=mock.Mock(return_value=configured),
                                 _plex_settings=mock.Mock(return_value={}),
                                 start_playlist_sync_job=start or mock.Mock()):
            return ps._playlist_auto_sync_tick()

    def test_tick_outcomes(self):
        self.assertEqual(self._tick(enabled=False), "disabled")
        self.assertEqual(self._tick(configured=False), "plex_not_configured")
        start = mock.Mock()
        self.assertEqual(self._tick(start=start), "started")
        start.assert_called_once_with(trigger="auto")
        running = mock.Mock(side_effect=DuplicateJobError(mock.Mock(label="Playlist sync", job_id="j1")))
        self.assertEqual(self._tick(start=running), "already_running")

    def test_start_error_is_logged_and_visible(self):
        ps._PLAYLIST_SYNC_STATE["last_error"] = ""
        self.assertEqual(self._tick(start=mock.Mock(side_effect=OSError("x"))), "error")
        self.assertTrue(ps._PLAYLIST_SYNC_STATE["last_error"])

    def test_sync_job_is_a_tracked_deduped_job(self):
        jobs = mock.Mock()
        jobs.start_python.return_value = mock.Mock(job_id="j9")
        with mock.patch.object(ps, "jobs", jobs):
            job = ps.start_playlist_sync_job(["Mix"], trigger="auto")
        self.assertEqual(job.job_id, "j9")
        meta = jobs.start_python.call_args.kwargs["metadata"]
        self.assertEqual(meta["dedupe_key"], "playlist-sync")
        self.assertEqual(ps._PLAYLIST_SYNC_STATE["last_job_id"], "j9")
        fn = jobs.start_python.call_args.args[0]
        with mock.patch.object(ps, "_playlist_sync_all_locked", return_value={"ok": True}) as run:
            fn([], threading.Event())
        self.assertEqual(run.call_args.kwargs["names"], ["Mix"])

    def test_scheduler_survives_a_failing_tick_and_stops(self):
        stop = threading.Event()
        calls = []

        def tick():
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            raise RuntimeError("boom")

        with mock.patch.multiple(ps, _playlist_auto_sync_tick=tick, PLAYLIST_AUTO_SYNC_INTERVAL=0):
            ps._playlist_auto_sync_scheduler(stop)
        self.assertEqual(len(calls), 2)


class SyncRouteTests(tir._RouteCase):
    def test_second_start_while_running_is_409(self):
        import routes_playlist
        running = DuplicateJobError(mock.Mock(label="Playlist sync", job_id="j1"))
        with mock.patch.object(routes_playlist, "start_playlist_sync_job", side_effect=running):
            res = self.client.post("/api/playlists/sync", json={})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "job_already_running")

    def test_start_returns_the_job_id(self):
        import routes_playlist
        with mock.patch.object(routes_playlist, "start_playlist_sync_job",
                               return_value=mock.Mock(job_id="j2")) as start:
            res = self.client.post("/api/playlists/sync", json={"names": ["Mix"]})
        self.assertEqual(res.get_json(), {"ok": True, "job_id": "j2"})
        start.assert_called_once_with(["Mix"], trigger="manual")


class QaAuditDefectTests(tir._RouteCase):
    """QA feature audit of v0.3.0: D5, D13, D14, D16, D17."""

    def test_d5_index_sees_imports_made_directly_in_beets(self):
        from backend.library_cache import library_cache
        item = mock.Mock()
        lib = mock.Mock()
        lib.items.return_value = []
        stats = mock.Mock(return_value={"items": 1, "albums": 1})
        clock = [1000.0]
        library_cache.invalidate()
        self.addCleanup(library_cache.invalidate)
        with mock.patch.object(ps, "lib", lib), \
                mock.patch.object(ps.beets_adapter, "get_stats", stats), \
                mock.patch.object(ps.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(ps, "_playlist_item_payload", return_value={"path": "/m/z.flac"}), \
                mock.patch.object(ps, "_playlist_item_text_variants", return_value=[("Zeta", "Long Song 1")]):
            ps._playlist_library_index()
            self.assertEqual(lib.items.call_count, 1)
            ps._playlist_library_index()  # within the recheck window: cached, no Beets call
            self.assertEqual((lib.items.call_count, stats.call_count), (1, 1))
            clock[0] += 60
            ps._playlist_library_index()  # same counts: still cached
            self.assertEqual(lib.items.call_count, 1)
            lib.items.return_value = [item]  # `beet import` in the Beets container
            stats.return_value = {"items": 2, "albums": 2}
            clock[0] += 60
            index = ps._playlist_library_index()
            self.assertEqual(lib.items.call_count, 2)
            self.assertIn((ps._norm("Zeta"), ps._norm("Long Song 1")), index["by_text"])
            stats.side_effect = OSError("beets down")  # Beets cannot say: keep the index
            clock[0] += 60
            self.assertIs(ps._playlist_library_index(), index)

    def test_d13_malformed_lidarr_response_is_not_unreachable(self):
        import backend.acquisition_service as acq
        import routes_lidarr
        self.assertEqual(acq._lidarr_wanted_error(json.JSONDecodeError("x", "<html>", 0)),
                         "Lidarr returned an unexpected response")
        with mock.patch.object(routes_lidarr, "_lidarr_config_error", return_value=""), \
                mock.patch.object(routes_lidarr, "_acq_fetch_lidarr_wanted",
                                  return_value=([], "Lidarr returned an unexpected response")):
            res = self.client.get("/api/wanted/lidarr")
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["error"], "Lidarr returned an unexpected response")

    def test_d14_policy_block_without_an_entry_is_not_unreachable(self):
        import backend.plex_service as plex_service
        from backend.security import OutboundPolicyError
        blocked = plex_service._plex_failure(OutboundPolicyError("outbound host resolves to a prohibited address"),
                                             "http://127.0.0.1:32400")
        self.assertEqual(blocked["reason"], "outbound_not_allowlisted")
        self.assertIn("blocked by the outbound network policy", blocked["error"])
        dns = plex_service._plex_failure(OutboundPolicyError("outbound host could not be resolved"))
        self.assertEqual(dns["reason"], "unreachable")

    def test_d16_hand_placed_m3u_is_found_and_exports_are_skipped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        own, legacy = Path(tmp.name) / "wm", Path(tmp.name) / "music" / "playlists"
        own.mkdir()
        legacy.mkdir(parents=True)
        (own / "QA Local Mix.m3u").write_text("#EXTM3U\n#EXTINF:1,Zeta - Long Song 1\n/music/z.flac\n",
                                              encoding="utf-8")
        (own / "pl1_0123456789ab.m3u").write_text("#EXTM3U\n/music/x.flac\n", encoding="utf-8")
        (legacy / "Smart.m3u").write_text("#EXTM3U\n/music/y.flac\n", encoding="utf-8")
        with mock.patch.object(ps, "PLAYLIST_LEGACY_IMPORT_DIR", legacy), \
                mock.patch.object(cw, "_get_playlist_dir", return_value=own):
            names = sorted(p.name for p in ps._playlist_legacy_files())
            self.assertEqual(names, ["QA Local Mix.m3u", "Smart.m3u"])
            with mock.patch.object(ps, "_playlist_library_index", return_value={}), \
                    mock.patch.object(ps, "_playlist_m3u_track_rows", return_value=([], [], [])), \
                    mock.patch.object(ps, "_match_playlist_tracks", side_effect=lambda t: ([], list(t))):
                body, status = ps.parse_playlist_request({"source": "local_m3u", "content": "QA Local Mix"})
        self.assertNotEqual(status, 404)
        self.assertIn({"artist": "Zeta", "title": "Long Song 1"},
                      [{"artist": t.get("artist"), "title": t.get("title")} for t in body.get("tracks", [])])

    def test_d17_plex_failure_makes_the_sync_job_fail(self):
        detail = {"name": "Mix", "updated": True, "plex_error": "Plex returned HTTP 500."}
        with mock.patch.multiple(
                ps,
                _plex_status_payload=mock.Mock(return_value={"configured": True, "connected": True}),
                _playlist_ensure_state_dirs=mock.Mock(),
                _playlist_import_legacy_lists=mock.Mock(return_value=[]),
                _playlist_library_index=mock.Mock(return_value={}),
                _plex_audio_playlists=mock.Mock(return_value=[]),
                _playlist_web_manager_names=mock.Mock(return_value=["Mix"]),
                _playlist_running_pipeline_job_id=mock.Mock(return_value=""),
                _playlist_sync_one=mock.Mock(return_value=detail)):
            jobs = mock.Mock()
            jobs.start_python.return_value = mock.Mock(job_id="j")
            with mock.patch.object(ps, "jobs", jobs):
                ps.start_playlist_sync_job()
            result = jobs.start_python.call_args.args[0]([], threading.Event())
        self.assertIs(result["ok"], False)
        self.assertEqual(result["playlists_failed"], 1)
        self.assertIn("failed", ps._PLAYLIST_SYNC_STATE["last_error"])

    def test_d17_empty_playlist_create_is_a_400(self):
        res = self.client.post("/api/playlist/create", json={"name": "Empty", "items": [], "tracks": []})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "no_tracks")


if __name__ == "__main__":
    unittest.main()
