"""Plugin 1.13.0: POST /webmanager/mbsync/library runs Beets' own mbsync
(MBSyncPlugin.albums/singletons, the real plugin class) over the whole
library. MusicBrainz is replaced by a stub source plugin behind Beets' real
metadata_plugins lookups (and their error handling)."""

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import beets.metadata_plugins as metadata_plugins
import beets.plugins as beets_plugins_mod
from beets import config as beets_config
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.library import Item, Library
from beetsplug.mbsync import MBSyncPlugin
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
import beetsplug.webmanager.operations as ops_mod
import beetsplug.webmanager.plugin_ops as plugin_ops

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
RG = "11111111-1111-1111-1111-111111111111"


def _album_info(album_id, title):
    tracks = [TrackInfo(title=f"{title} track {n}", track_id=f"rec-{album_id}-{n}", index=n,
                        medium=1, medium_index=n, release_track_id=f"rt-{album_id}-{n}") for n in (1, 2)]
    return AlbumInfo(tracks=tracks, album=title, album_id=album_id, artist="Synced Artist",
                     artist_id="art-1", releasegroup_id=RG)


class MbsyncLibraryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        # Files live outside the library directory and write is off, so
        # mbsync neither moves nor writes files here: only the DB changes.
        self.files = os.path.join(self.td, "files")
        os.makedirs(self.files)
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=os.path.join(self.td, "music"))
        key_file = os.path.join(self.td, "key")
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(TOKEN + "\n")
        WebManagerPlugin()
        set_api_key_file(key_file)
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self._saved_instances = list(beets_plugins_mod._instances)
        beets_plugins_mod._instances.clear()
        beets_plugins_mod._instances.append(MBSyncPlugin())
        self._saved_import = {k: beets_config["import"][k].get() for k in ("write", "move", "copy")}
        beets_config["import"]["write"] = False
        beets_config["import"]["move"] = False
        beets_config["import"]["copy"] = False
        self._saved_raise = beets_config["raise_on_error"].get()
        beets_config["raise_on_error"] = False  # Beets' default

    def tearDown(self):
        beets_config["raise_on_error"] = self._saved_raise
        for k, v in self._saved_import.items():
            beets_config["import"][k] = v
        beets_plugins_mod._instances[:] = self._saved_instances
        set_api_key_file(None)
        with ops_mod._library_sync_lock:
            ops_mod._library_sync.clear()
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _add_album(self, mb_albumid, title="Old Title", root=None):
        items = []
        for n in (1, 2):
            items.append(Item(path=os.path.join(root or self.files, f"{mb_albumid or 'x'}-{n}.mp3").encode(),
                              title=f"old {n}", artist="Old Artist", albumartist="Old Artist", album=title,
                              track=n, disc=1, mb_trackid=f"rec-{mb_albumid}-{n}" if mb_albumid else "",
                              mb_releasetrackid=f"rt-{mb_albumid}-{n}" if mb_albumid else ""))
        album = self.lib.add_album(items)
        album.mb_albumid = mb_albumid
        album.store()
        return album

    def _source(self, album=None, track=None):
        """Patch in a MusicBrainz source plugin, so lookups go through Beets'
        real metadata_plugins.album_for_id/track_for_id and its
        maybe_handle_plugin_error wrapper (raise_on_error off, Beets'
        default). ``album``/``track`` is a function of the ID, or an
        exception the source raises."""
        def call(fn, _id):
            if isinstance(fn, BaseException):
                raise fn
            return fn(_id)

        class Source:
            data_source = "MusicBrainz"

            def album_for_id(self, aid):
                return call(album, aid)

            def track_for_id(self, tid):
                return call(track, tid)

        metadata_plugins.get_metadata_source.cache_clear()
        self.addCleanup(metadata_plugins.get_metadata_source.cache_clear)
        return mock.patch.object(metadata_plugins, "find_metadata_source_plugins", return_value=[Source()])

    def _wait(self, op_id, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            op = self.client.get(f"/webmanager/operations/{op_id}", headers=self.auth).get_json()
            if op["status"] != "running":
                return op
            time.sleep(0.02)
        self.fail("library sync did not finish")

    def test_runs_beets_mbsync_and_records_what_changed(self):
        synced = self._add_album("rel-a")
        no_mbid = self._add_album("")
        single = Item(path=os.path.join(self.files, "single.mp3").encode(), title="old single",
                      artist="Old", mb_trackid="rec-single")
        self.lib.add(single)
        with self._source(album=lambda aid: _album_info(aid, "New Title"),
                          track=lambda tid: TrackInfo(title="new single", track_id=tid, artist="Single Artist")):
            res = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={})
            self.assertEqual(res.status_code, 202)
            body = res.get_json()
            self.assertEqual((body["write"], body["move"]), (False, False))
            op = self._wait(body["operation_id"])
        self.assertEqual(op["status"], "succeeded")
        r = op["result"]
        self.assertEqual((r["targets"], r["processed"], r["skipped_no_id"], r["not_found"]), (2, 2, 1, 0))
        self.assertEqual((r["changed_albums"], r["changed_singletons"], r["changed_items"]), (1, 1, 3))
        self.assertFalse(r["cancelled"])
        # Beets itself stored the MusicBrainz values.
        self.assertEqual(self.lib.get_album(synced.id).album, "New Title")
        self.assertEqual(self.lib.get_album(synced.id).mb_releasegroupid, RG)
        self.assertEqual(self.lib.get_album(no_mbid.id).album, "Old Title")
        self.assertEqual(self.lib.get_item(single.id).title, "new single")
        entry = next(c for c in r["changes"] if c["kind"] == "album")
        self.assertEqual(entry["album_fields"]["album"], ["Old Title", "New Title"])
        self.assertEqual(entry["items"][0]["fields"]["title"], ["old 1", "New Title track 1"])
        self.assertNotIn("mtime", entry["items"][0]["fields"])

    def test_second_start_is_refused_and_cancel_stops_after_the_current_album(self):
        first, second = self._add_album("rel-a"), self._add_album("rel-b")
        entered, release = threading.Event(), threading.Event()

        def slow(aid, *a, **k):
            entered.set()
            release.wait(5)
            return _album_info(aid, "New Title")

        with self._source(album=slow):
            op_id = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={}).get_json()["operation_id"]
            self.assertTrue(entered.wait(5))
            again = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={})
            self.assertEqual(again.status_code, 409)
            self.assertEqual(again.get_json()["error_code"], "ALREADY_RUNNING")
            self.assertEqual(again.get_json()["operation_id"], op_id)
            # The same key replays the running operation instead.
            replay = self.client.post("/webmanager/mbsync/library", headers={**self.auth, "Idempotency-Key": op_id}, json={})
            self.assertEqual(replay.status_code, 202)
            cancel = self.client.post(f"/webmanager/mbsync/library/{op_id}/cancel", headers=self.auth)
            self.assertEqual(cancel.status_code, 202)
            release.set()
            op = self._wait(op_id)
        self.assertEqual(op["status"], "succeeded")
        self.assertTrue(op["result"]["cancelled"])
        self.assertEqual(op["result"]["processed"], 1)
        titles = sorted(self.lib.get_album(a.id).album for a in (first, second))
        self.assertEqual(titles, ["New Title", "Old Title"])  # the album in progress finished; the next never ran
        late = self.client.post(f"/webmanager/mbsync/library/{op_id}/cancel", headers=self.auth)
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.get_json()["error_code"], "NOT_RUNNING")
        # A new sync can start once the first one ended.
        with self._source(album=lambda aid: None):
            res = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={})
            self.assertEqual(res.status_code, 202)
            r = self._wait(res.get_json()["operation_id"])["result"]
            self.assertEqual((r["unchanged"], r["not_found"], r["failed_count"]), (0, 2, 0))

    def test_partial_failure_records_the_album_and_goes_on_with_progress_per_album(self):
        bad, good = self._add_album("rel-bad"), self._add_album("rel-good")

        def lookup(aid, *a, **k):
            if aid == "rel-bad":
                raise RuntimeError("MusicBrainz 503")
            return _album_info(aid, "New Title")

        seen = []
        with self._source(album=lookup):
            res = plugin_ops.run_mbsync_library(self.lib, False, threading.Event(), threading.RLock(),
                                                progress=lambda r: seen.append(r["processed"] + r["failed_count"]))
        self.assertEqual((res["processed"], res["failed_count"], res["changed_albums"], res["aborted"]), (1, 1, 1, False))
        self.assertEqual(res["failed"][0]["id"], bad.id)
        self.assertEqual(self.lib.get_album(good.id).album, "New Title")
        self.assertEqual(self.lib.get_album(bad.id).album, "Old Title")
        self.assertEqual(seen, [0, 1])  # reported before each album, after the one before it

    def test_tag_write_failures_are_reported_not_silent_and_files_never_move(self):
        beets_config["import"]["write"] = True
        beets_config["import"]["move"] = True  # ignored: MBSync All never moves files
        # In the library directory, where import.move would move them; the
        # files do not exist, so Item.try_write() fails.
        album = self._add_album("rel-a", root=os.path.join(self.td, "music", "Old"))
        paths = sorted(i.path for i in album.items())
        with self._source(album=lambda aid, *a, **k: _album_info(aid, "New Title")):
            body = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={}).get_json()
            self.assertEqual((body["write"], body["move"]), (True, False))
            op = self._wait(body["operation_id"])
        r = op["result"]
        self.assertEqual((r["changed_items"], r["write_failed_count"]), (2, 2))
        self.assertEqual(sorted(f["item_id"] for f in r["write_failed"]), sorted(i.id for i in album.items()))
        self.assertTrue(all(i["write_failed"] for i in r["changes"][0]["items"]))
        self.assertEqual(self.lib.get_album(album.id).album, "New Title")  # the DB did change
        self.assertEqual(sorted(i.path for i in self.lib.get_album(album.id).items()), paths)

    def test_successful_tag_writes_are_not_reported_as_failures(self):
        beets_config["import"]["write"] = True
        self._add_album("rel-a")
        with self._source(album=lambda aid, *a, **k: _album_info(aid, "New Title")), \
             mock.patch.object(Item, "try_write", autospec=True, return_value=True) as tw:
            r = plugin_ops.run_mbsync_library(self.lib, True, threading.Event(), threading.RLock())
        self.assertEqual((tw.call_count, r["write_failed_count"], r["changed_items"]), (2, 0, 2))

    def test_failures_are_recorded_and_a_run_of_them_aborts(self):
        for n in range(plugin_ops.MAX_CONSECUTIVE_FAILURES + 2):
            self._add_album(f"rel-{n}")
        with self._source(album=RuntimeError("MusicBrainz down")):
            op_id = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={}).get_json()["operation_id"]
            op = self._wait(op_id)
        self.assertEqual(op["status"], "failed")
        self.assertEqual(op["error_code"], "MBSYNC_ABORTED")
        self.assertTrue(op["result"]["aborted"])
        self.assertEqual(op["result"]["failed_count"], plugin_ops.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("MusicBrainz down", op["result"]["failed"][0]["error"])
        self.assertIn("album_for_id", op["result"]["failed"][0]["error"])
        self.assertEqual(op["result"]["not_found"], 0)

    def test_a_missing_release_is_not_found_and_an_outage_is_a_failure(self):
        gone, down = self._add_album("rel-gone"), self._add_album("rel-down")

        def lookup(aid):
            if aid == "rel-down":
                raise ConnectionError("Connection refused")
            return None  # MusicBrainz answered: no such Release

        with self._source(album=lookup):
            r = plugin_ops.run_mbsync_library(self.lib, False, threading.Event(), threading.RLock())
        self.assertEqual((r["not_found"], r["failed_count"], r["unchanged"], r["processed"]), (1, 1, 0, 1))
        self.assertEqual(r["failed"][0]["id"], down.id)
        self.assertEqual(self.lib.get_album(gone.id).album, "Old Title")

    def test_lookup_errors_from_other_threads_are_ignored(self):
        beets_log = __import__("logging").getLogger("beets")
        with plugin_ops._catch_lookup_errors() as lookup:
            t = threading.Thread(target=beets_log.error, args=("Error in 'MusicBrainz.album_for_id': other thread",))
            t.start()
            t.join()
            beets_log.error("Error in 'MusicBrainz.track_for_id': this thread")
            beets_log.error("Error in 'MusicBrainz.candidates': not a by-ID lookup")
        self.assertEqual(lookup.errors, ["Error in 'MusicBrainz.track_for_id': this thread"])
        self.assertNotIn(lookup, beets_log.handlers)

    def test_an_unexpected_tag_write_error_is_recorded_and_the_run_goes_on(self):
        beets_config["import"]["write"] = True
        first, second = self._add_album("rel-a"), self._add_album("rel-b")
        with self._source(album=lambda aid: _album_info(aid, "New Title")), \
             mock.patch.object(Item, "try_write", autospec=True, side_effect=RuntimeError("boom")):
            r = plugin_ops.run_mbsync_library(self.lib, True, threading.Event(), threading.RLock())
        self.assertEqual((r["changed_albums"], r["write_failed_count"], r["failed_count"]), (2, 4, 0))
        self.assertIn("RuntimeError: boom", r["write_failed"][0]["error"])
        self.assertEqual({self.lib.get_album(a.id).album for a in (first, second)}, {"New Title"})

    def test_empty_album_rows_are_skipped_not_failed(self):
        album = self._add_album("rel-a")
        for item in album.items():
            item.remove(with_album=False)
        with mock.patch.object(metadata_plugins, "album_for_id") as lookup:
            op_id = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={}).get_json()["operation_id"]
            op = self._wait(op_id)
        lookup.assert_not_called()
        self.assertEqual((op["result"]["skipped_empty"], op["result"]["failed_count"]), (1, 0))

    def test_refused_without_the_mbsync_plugin(self):
        beets_plugins_mod._instances.clear()
        res = self.client.post("/webmanager/mbsync/library", headers=self.auth, json={})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "CAPABILITY_UNAVAILABLE")
        self.assertNotIn("mbsync_library", ops_mod.get_capabilities())

    def test_requires_auth(self):
        self.assertEqual(self.client.post("/webmanager/mbsync/library", json={}).status_code, 401)


class AdapterToPluginTests(MbsyncLibraryEndpointTests):
    """Web Manager's workflow and adapter driving the real plugin endpoint
    (the adapter's HTTP is routed into the Flask test client)."""

    def setUp(self):
        super().setUp()
        from backend.beets_adapter import BeetsAdapter, BeetsAdapterError, BeetsAdapterNotFoundError
        self.adapter = BeetsAdapter(base_url="http://beets.invalid:8337", api_key=TOKEN)
        client = self.client

        def request(method, path, params=None, json_data=None, headers=None, timeout=None):
            resp = client.open(path, method=method, json=json_data, headers={**self.auth, **(headers or {})})
            body = resp.get_json() or {}
            if resp.status_code == 404:
                raise BeetsAdapterNotFoundError("nf", error_code=body.get("error_code") or "BEETS_NOT_FOUND")
            if resp.status_code >= 400:
                raise BeetsAdapterError("err", status_code=resp.status_code, response_data=body,
                                        error_code=body.get("error_code") or "BEETS_UPSTREAM_ERROR")
            return body
        self.adapter._request = request

    def _sync(self, cancel=None):
        from backend.transaction_engine import TransactionStore
        import backend.composite_workflows as cw
        store = TransactionStore(os.path.join(self.td, "tx"))
        tx = store.create(operation_type="MusicBrainz Match", status="Running")["id"]
        log = []
        res = cw.mbsync_library(log, cancel, transaction_id=tx, adapter=self.adapter, store=store, poll_seconds=0.02)
        return res, store.get(tx), log

    def test_workflow_runs_beets_mbsync_end_to_end(self):
        album = self._add_album("rel-a")
        with self._source(album=lambda aid, *a, **k: _album_info(aid, "New Title")):
            res, tx, log = self._sync()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.lib.get_album(album.id).album, "New Title")
        self.assertEqual(tx["metadata"]["engine_result"]["changed_albums"], 1)
        self.assertIn({"field": "album", "old": "Old Title", "new": "New Title", "changed": True},
                      tx["changes"][0]["metadata_diff"])

    def test_workflow_cancel_reaches_beets(self):
        from job_engine import CancelSignal
        self._add_album("rel-a")
        self._add_album("rel-b")
        cancel, entered, release = CancelSignal(), threading.Event(), threading.Event()

        def slow(aid, *a, **k):
            entered.set()
            cancel.set()
            release.wait(5)
            return _album_info(aid, "New Title")

        threading.Timer(0.3, release.set).start()
        with self._source(album=slow):
            res, tx, log = self._sync(cancel)
        self.assertTrue(res["cancelled"], log)
        self.assertEqual(res["summary"]["processed"], 1)
        self.assertTrue(cancel.observed)
        self.assertTrue(tx["metadata"]["engine_result"]["cancelled"])

    def test_workflow_fails_the_job_on_partial_failure_with_counts(self):
        beets_config["import"]["write"] = True
        self._add_album("rel-a")  # files missing: tag writes fail
        with self._source(album=lambda aid, *a, **k: _album_info(aid, "New Title")):
            res, tx, log = self._sync()
        self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "MBSYNC_PARTIAL", True))
        self.assertEqual(res["summary"]["write_failed_count"], 2)
        self.assertIn("2 tracks' tags could not be written", res["error"])
        self.assertNotIn("could not be synced", res["error"])
        self.assertEqual(len(tx["metadata"]["engine_result"]["write_failed"]), 2)
        self.assertTrue(any("tags not written" in line for line in log))

    def test_workflow_reports_a_missing_mbsync_plugin(self):
        beets_plugins_mod._instances.clear()
        res, _, _ = self._sync()
        self.assertEqual((res["ok"], res["code"], res["mutated"]), (False, "CAPABILITY_UNAVAILABLE", False))


if __name__ == "__main__":
    unittest.main()
