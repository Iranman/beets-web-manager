"""Import batch, qBittorrent source (PR 1 of the Import repair).

qBittorrent is the authority for what a qBittorrent-source batch imports: its
completed torrents (seeding included) and their own audio files. Nothing is
found by walking the downloads folder, app-made folders are never picked up,
a torrent's files are copied (never moved), a vanished source is a permanent
``source_missing`` with no Review row, and a qBittorrent that cannot be read
fails the batch closed. qBittorrent is mocked at ``_qbit_login_cookie`` /
``_qbit_request_json``; everything runs in throwaway folders.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REL = "55555555-5555-5555-5555-555555555555"
RG = "66666666-6666-6666-6666-666666666666"


def setUpModule():
    global APP, ai, acq, slskd, pb, patch_app_family
    tmp = Path(tempfile.mkdtemp(prefix="bwm_qbit_src_env_"))
    for key, value in (("BEETSDIR", tmp / "config"), ("LIB_PATH", tmp / "config" / "musiclibrary.blb"),
                       ("AI_BATCH_STATE_DIR", tmp / "ai_batch_jobs"), ("METADATA_CACHE_DIR", tmp / "cache"),
                       ("BEETS_TRANSACTION_DIR", tmp / "transactions")):
        os.environ.setdefault(key, str(value))
    os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    import app as APP  # noqa: N811
    import backend.ai_service as ai
    import backend.acquisition_service as acq
    import backend.slskd as slskd
    import backend.provider_boundary as pb
    try:
        from _app_family import patch_app_family
    except ImportError:  # pragma: no cover
        from tests._app_family import patch_app_family


def _symlink(target, link, is_dir):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as ex:
        raise unittest.SkipTest(f"symlinks unavailable here: {ex}")


def _fingerprint(path):
    st = os.stat(path)
    return (hashlib.sha256(Path(path).read_bytes()).hexdigest(), st.st_ino, st.st_size, st.st_mtime_ns)


class FakeQbit:
    """qBittorrent Web API stand-in: torrents/info and torrents/files only."""

    def __init__(self):
        self.torrents = []
        self.files = {}
        self.calls = []
        self.fail = None  # (api_path suffix, exception)

    def add(self, dl, name, files, *, progress=1.0, state="stalledUP", thash=None, content=None,
            file_progress=1.0, priority=1):
        thash = thash or hashlib.sha1(name.encode()).hexdigest()
        self.torrents.append({"hash": thash, "name": name, "progress": progress, "state": state,
                              "save_path": str(dl), "content_path": str(content or (Path(dl) / name)),
                              "category": "", "tags": "", "extra": "x" * 50})
        self.files[thash] = [{"name": f, "size": 1, "progress": file_progress, "priority": priority}
                             for f in files]
        return thash

    def request(self, api_path, params=None, cookie=""):
        self.calls.append(api_path)
        if self.fail and api_path.endswith(self.fail[0]):
            raise self.fail[1]
        if api_path.endswith("/torrents/info"):
            return [dict(t) for t in self.torrents]
        if api_path.endswith("/torrents/files"):
            return [dict(r) for r in self.files[params["hash"]]]
        raise AssertionError(f"unexpected qBittorrent call {api_path}")


class _Roots(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwm_qbit_src_", dir=os.getcwd())).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dl = self.tmp / "downloads"
        self.music = self.tmp / "music"
        self.outside = self.tmp / "outside"
        for p in (self.dl, self.music, self.outside):
            p.mkdir()
        for name, value in (("_DOWNLOADS_ROOTS", [str(self.dl)]), ("DOWNLOADS_ALLOWED_ROOTS", [self.dl]),
                            ("TORRENT_SOURCE_ROOTS", [self.dl]), ("TORRENT_SOURCE_MOVE_ALLOWED", False),
                            ("PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", []), ("MUSIC_ROOT", self.music),
                            ("_IMPORT_SOURCE_ALLOWED_ROOTS", (self.dl, self.music))):
            p = patch_app_family(APP, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.qbit = FakeQbit()
        for p in (mock.patch.object(acq, "_qbit_login_cookie", return_value="SID=test"),
                  mock.patch.object(acq, "_qbit_request_json", side_effect=self.qbit.request),
                  mock.patch.object(acq, "QBIT_PATH_ALIASES", "")):
            p.start()
            self.addCleanup(p.stop)

    def audio(self, rel, data=b"audio-bytes"):
        f = self.dl / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return f

    def torrent(self, name, rels, **kw):
        """A multi-file torrent ``name`` whose files (relative to it) exist on disk."""
        for rel in rels:
            self.audio(f"{name}/{rel}")
        return self.qbit.add(self.dl, name, [f"{name}/{rel}" for rel in rels], **kw)

    def discover(self):
        self.log = []
        units, report = ai._ai_batch_qbit_units(acq.qbit_import_inventory(), self.log)
        return units, report


class DiscoveryTests(_Roots):
    def test_app_managed_folder_is_excluded(self):
        self.torrent("User Album", ["01.flac"])
        slskd.make_app_download_dir(self.dl / "App Album (2021)")
        self.torrent("App Album (2021)", ["01.flac"])  # a torrent pointing into an app folder
        units, report = self.discover()
        self.assertEqual(list(units), [str(self.dl / "User Album")])
        self.assertEqual(report["exclusions"].get("app_managed"), 1)

    def test_slot_deferred_folder_under_torrent_save_path_is_excluded(self):
        name = "Part III - soundcloud missing 1793260626691__slot_deferred"
        self.torrent(name, ["01.flac"])
        self.torrent("Part III", ["01.flac"])
        units, report = self.discover()
        self.assertEqual(list(units), [str(self.dl / "Part III")])
        self.assertEqual(report["exclusions"]["app_managed"], 1)

    def test_only_torrent_files_of_a_mixed_folder_are_listed(self):
        self.torrent("Album", ["01.flac", "02.flac"])
        self.audio("Album/03 not from the torrent.flac")
        slskd.make_app_download_dir(self.dl / "Album" / "fill")
        self.audio("Album/fill/04.flac")
        units, _report = self.discover()
        self.assertEqual(units, {str(self.dl / "Album"): ["01.flac", "02.flac"]})

    def test_nested_artist_album_and_disc_folders(self):
        self.torrent("Artist", ["loose.flac"])
        self.qbit.add(self.dl, "Artist Album", ["Artist/Album/01.flac"], content=self.dl / "Artist")
        self.audio("Artist/Album/01.flac")
        self.torrent("Double", ["CD1/01.flac", "CD2/01.flac"])
        units, _report = self.discover()
        self.assertEqual(units, {
            str(self.dl / "Artist"): ["loose.flac"],
            str(self.dl / "Artist" / "Album"): ["01.flac"],
            str(self.dl / "Double"): ["CD1/01.flac", "CD2/01.flac"],
        })

    def test_parent_child_and_same_file_from_two_torrents_queue_once(self):
        self.torrent("Double", ["CD1/01.flac", "CD2/01.flac"])
        # A second torrent whose content is the disc folder itself: same files.
        self.qbit.add(self.dl, "CD1", ["CD1/01.flac"], content=self.dl / "Double" / "CD1",
                      thash="f" * 40)
        self.qbit.torrents[-1]["save_path"] = str(self.dl / "Double")
        units, report = self.discover()
        self.assertEqual(units, {str(self.dl / "Double"): ["CD1/01.flac", "CD2/01.flac"]})
        self.assertEqual(report["exclusions"]["duplicate"], 1)

    def test_hardlinked_copy_in_another_torrent_is_a_duplicate_and_order_is_deterministic(self):
        self.torrent("A Album", ["01.flac"])
        (self.dl / "B Album").mkdir()
        try:
            os.link(self.dl / "A Album" / "01.flac", self.dl / "B Album" / "01.flac")
        except OSError as ex:
            raise unittest.SkipTest(f"hardlinks unavailable: {ex}")
        self.qbit.add(self.dl, "B Album", ["B Album/01.flac"], thash="0" * 40)  # sorts first
        first, _ = self.discover()
        self.qbit.torrents.reverse()
        second, report = self.discover()
        self.assertEqual(first, second)
        self.assertEqual(list(first), [str(self.dl / "B Album")])
        self.assertEqual(report["exclusions"]["duplicate"], 1)

    def test_incomplete_torrents_and_files_are_skipped(self):
        self.torrent("Done", ["01.flac"], state="pausedUP", progress=0.999)  # completed state counts
        self.torrent("Downloading", ["01.flac"], progress=0.5, state="downloading")
        self.torrent("Unselected", ["01.flac"], priority=0)
        self.torrent("Partial", ["01.flac"], file_progress=0.4)
        units, report = self.discover()
        self.assertEqual(list(units), [str(self.dl / "Done")])
        self.assertEqual(report["exclusions"]["incomplete_torrent"], 2)
        self.assertEqual(report["exclusions"]["not_selected"], 1)
        # No file list is read for an incomplete torrent.
        self.assertEqual(self.qbit.calls.count("/api/v2/torrents/files"), 3)

    def test_missing_empty_and_non_audio_files(self):
        self.qbit.add(self.dl, "Gone", ["Gone/01.flac"])
        self.audio("Empty/01.flac", b"")
        self.qbit.add(self.dl, "Empty", ["Empty/01.flac"])
        self.qbit.add(self.dl, "Art", ["Art/cover.jpg"])
        units, report = self.discover()
        self.assertEqual(units, {})
        self.assertEqual(report["exclusions"], {"missing": 1, "empty_file": 1, "not_audio": 1})

    def test_unsafe_torrent_names_and_paths(self):
        for name in ("../escape.flac", "/abs/x.flac", "C:/x.flac", "a\x00b.flac", "Dir/../../x.flac"):
            self.qbit.add(self.dl, "T " + name.replace("/", "_"), [name])
        self.audio("x.flac")
        (self.outside / "Album").mkdir()
        (self.outside / "Album" / "01.flac").write_bytes(b"x")
        self.qbit.add(self.outside, "Album", ["Album/01.flac"])  # content outside every root
        self.audio("Loose.flac")
        self.qbit.add(self.dl, "Loose.flac", ["Loose.flac"], content=self.dl / "Loose.flac")
        units, report = self.discover()
        self.assertEqual(units, {})
        self.assertEqual(report["exclusions"]["unsafe_path"], 5)
        self.assertEqual(report["exclusions"]["outside_allowed_root"], 1)
        self.assertEqual(report["exclusions"]["protected_location"], 1)  # loose file in the root

    def test_symlinked_file_and_folder_are_never_followed(self):
        self.audio("real/01.flac")
        (self.dl / "Linked").mkdir()
        _symlink(self.dl / "real" / "01.flac", self.dl / "Linked" / "01.flac", False)
        self.qbit.add(self.dl, "Linked", ["Linked/01.flac"])
        (self.outside / "Elsewhere").mkdir()
        (self.outside / "Elsewhere" / "01.flac").write_bytes(b"x")
        _symlink(self.outside / "Elsewhere", self.dl / "Elsewhere", True)
        self.qbit.add(self.dl, "Elsewhere", ["Elsewhere/01.flac"])
        units, report = self.discover()
        self.assertEqual(units, {})
        self.assertEqual(report["exclusions"]["symlink"], 2)

    def test_library_is_a_protected_location(self):
        (self.music / "Album").mkdir()
        (self.music / "Album" / "01.flac").write_bytes(b"x")
        self.qbit.add(self.music, "Album", ["Album/01.flac"])
        with mock.patch.object(ai, "TORRENT_SOURCE_ROOTS", [self.dl, self.music]):
            units, report = self.discover()
        self.assertEqual(units, {})
        self.assertEqual(report["exclusions"]["protected_location"], 1)

    def test_exclusion_samples_are_bounded(self):
        for i in range(300):
            self.qbit.add(self.dl, f"Gone {i}", [f"Gone {i}/01.flac"])
        _units, report = self.discover()
        self.assertEqual(report["exclusions"]["missing"], 300)
        self.assertEqual(len(report["exclusion_samples"]), ai._QBIT_SOURCE_SAMPLES_PER_REASON)

    def test_scale_8000_torrents_bounded_memory_and_time(self):
        n = 8000
        for i in range(n):
            name = f"Artist {i % 400}/Album {i}"
            self.audio(f"{name}/01.flac", b"a")
            self.qbit.add(self.dl, f"Album {i}", [f"Album {i}/01.flac", f"Album {i}/cover.jpg"],
                          content=self.dl / name)
            self.qbit.torrents[-1]["save_path"] = str(self.dl / f"Artist {i % 400}")
        tracemalloc.start()
        started = time.perf_counter()
        units, report = self.discover()
        elapsed = time.perf_counter() - started
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"\n[qbit scale] torrents={n} units={len(units)} elapsed={elapsed:.2f}s "
              f"peak_traced={peak / 1e6:.1f}MB files_calls={self.qbit.calls.count('/api/v2/torrents/files')}",
              file=sys.stderr)
        self.assertEqual(len(units), n)
        self.assertEqual(report["eligible_files"], n)
        self.assertEqual(self.qbit.calls.count("/api/v2/torrents/info"), 1)
        self.assertEqual(self.qbit.calls.count("/api/v2/torrents/files"), n)  # one per torrent, none per file
        self.assertLess(peak, 150 * 1024 * 1024)
        self.assertLess(elapsed, 180)


class _Batch(_Roots):
    """The real batch worker over a qBittorrent source; only Beets and the
    providers are stubbed."""

    def setUp(self):
        super().setUp()
        self.batch_id = f"qbit-{id(self)}"
        self.stored = None
        self.reimport = mock.Mock(side_effect=lambda f, opts, timeout: self.reimport_result(f))
        self.result = {"ok": True, "albums_imported": 1, "imported_album_ids": [7], "skipped": [],
                       "not_matched": [], "not_matched_known": True}
        self.queued = mock.Mock(return_value="rid")
        self.plan = mock.Mock(return_value={"ok": True, "operation_id": "op"})
        status = {"capabilities": ["mbsubmit"], "fpcalc_available": True}
        self.local_walk = mock.Mock(side_effect=AssertionError("local folder walk used"))
        patches = [
            mock.patch.object(ai, "_ai_batch_find_audio_dirs", self.local_walk),
            mock.patch.object(ai, "_ai_batch_commit"),
            mock.patch.object(ai, "_ai_batch_load_state", side_effect=lambda bid: self.stored),
            mock.patch.object(ai, "_ai_batch_already_in_library", return_value=False),
            mock.patch.object(ai, "_pending_review_has_path", return_value=False),
            mock.patch.object(ai, "_pending_review_path_set", return_value=set()),
            mock.patch.object(ai, "_import_review_cleanup_roots", side_effect=lambda **k: [self.dl, self.music]),
            mock.patch.object(ai, "_validate_import_source_audio"),
            mock.patch.object(ai.composite_workflows, "reimport_source", self.reimport),
            mock.patch.object(ai.composite_workflows, "get_album", side_effect=lambda aid: {"mb_releasegroupid": RG}),
            mock.patch.object(ai.composite_workflows, "plan_confirmed_import", self.plan),
            mock.patch.object(ai.composite_workflows, "apply_confirmed_import",
                              return_value={"ok": True, "album_id": 9, "mb_releasegroupid": RG}),
            mock.patch.object(ai, "identify_import_folder", return_value={
                "candidates": [], "warnings": [], "auto_import": {
                    "eligible": True, "release_group_id": RG, "release_id": REL, "reasons": []}}),
            mock.patch.object(ai, "_ai_batch_queue_pending_review", self.queued),
            mock.patch.object(ai, "_record_recent_import"),
            mock.patch.object(ai, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "release_group": RG, "tracks": []}),
            mock.patch.object(ai, "_fetch_artwork_after_retag", return_value={
                "identity_verified": True, "artwork_status": "fetched", "artwork_retryable": False}),
            mock.patch.object(ai, "_remove_pending_review_for_path"),
            mock.patch.object(ai, "lib"),
            mock.patch.object(ai, "_auto_merge_case_duplicate_artist_folder"),
            mock.patch.object(ai, "_invalidate_lib_cache"),
            mock.patch.object(ai, "_trigger_plex_refresh"),
            mock.patch.object(ai.beets_adapter, "get_plugin_status", return_value=status),
            mock.patch.object(ai, "acoustid_api_key", return_value="configured"),
            mock.patch.object(ai, "_ai_api_key", return_value=""),
        ]
        if not slskd._staging_fd_supported():  # Windows: the real fd-relative copy cannot run
            patches.append(mock.patch.object(ai, "_stage_selected_audio_files_impl", side_effect=self.fake_stage,
                                             create=True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def reimport_result(self, folder):
        return self.result

    @staticmethod
    def fake_stage(root, exts, aldir, files, artist, album, log, stage_prefix=None, **_kw):
        present = {p.resolve() for p in Path(aldir).rglob("*") if p.is_file() and p.suffix.lower() in exts}
        if present == {Path(f).resolve() for f in files}:
            return aldir
        stage = Path(root) / "_beets_missing_import" / f"{stage_prefix}-{artist} - {album}"
        for f in files:
            dst = stage / Path(f).relative_to(aldir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        return str(stage)

    def run_batch(self, **kw):
        self.log = []
        kw.setdefault("source", "qbittorrent")
        kw.setdefault("qbit_inventory", acq.qbit_import_inventory)
        out = ai._run_ai_batch_import(self.batch_id, str(self.dl), self.log, threading.Event(), None, **kw)
        return out, {f["source_folder"]: f for f in out["folders"]}


class BatchTests(_Batch):
    def test_seeding_torrent_is_copied_and_its_bytes_never_change(self):
        self.torrent("Seeding Album", ["01.flac", "02.flac"], state="uploading")
        files = [self.dl / "Seeding Album" / n for n in ("01.flac", "02.flac")]
        before = [_fingerprint(f) for f in files]
        # Even when the folder would let a move through (operator opt-in), a
        # qBittorrent unit is copied.
        with patch_app_family(APP, "TORRENT_SOURCE_MOVE_ALLOWED", True):
            out, folders = self.run_batch()
        self.assertEqual(folders[str(self.dl / "Seeding Album")]["status"], "imported")
        opts = self.reimport.call_args.args[1]
        self.assertTrue(opts["copy"])
        self.assertFalse(opts["move"])
        self.assertEqual([_fingerprint(f) for f in files], before)
        self.assertTrue(all(f.is_file() for f in files))

    def test_pinned_import_of_a_qbit_unit_plans_a_copy_even_with_an_app_marker(self):
        self.torrent("Album", ["01.flac"])
        f = self.dl / "Album" / "01.flac"
        before = _fingerprint(f)
        self.result = {"ok": True, "albums_imported": 0, "imported_album_ids": [],
                       "skipped": [{"path": str(self.dl / "Album"), "reason": "no_strong_match"}],
                       "not_matched": [str(self.dl / "Album")], "not_matched_known": True}
        real_units = ai._ai_batch_qbit_units

        def units_then_marker(*a, **k):
            out = real_units(*a, **k)
            # A marker appearing after discovery never turns the unit into a move.
            slskd.mark_app_download_dir(self.dl / "Album")
            return out

        with mock.patch.object(ai, "_ai_batch_qbit_units", side_effect=units_then_marker):
            out, folders = self.run_batch()
        self.assertEqual(folders[str(self.dl / "Album")]["status"], "imported", self.log)
        self.assertFalse(self.plan.call_args.args[0]["use_move"])
        self.assertTrue(self.reimport.call_args.args[1]["copy"])
        self.assertEqual(_fingerprint(f), before)

    def test_mixed_folder_imports_only_the_torrent_files(self):
        self.torrent("Album", ["01.flac", "02.flac"])
        extra = self.audio("Album/03 not from the torrent.flac")
        slskd.make_app_download_dir(self.dl / "Album" / "fill")
        app_file = self.audio("Album/fill/04.flac")
        before = {p: _fingerprint(p) for p in (self.dl / "Album").rglob("*.flac")}
        out, folders = self.run_batch()
        state = folders[str(self.dl / "Album")]
        staged = Path(state["staged_folder"])
        imported_from = self.reimport.call_args.args[0]
        self.assertEqual(Path(imported_from), staged)
        self.assertEqual(sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*.flac")),
                         ["01.flac", "02.flac"])
        self.assertEqual({p: _fingerprint(p) for p in (self.dl / "Album").rglob("*.flac")}, before)
        self.assertTrue(extra.is_file() and app_file.is_file())

    def test_excluded_folders_get_no_folder_state_and_no_review_row(self):
        self.torrent("Good", ["01.flac"])
        slskd.make_app_download_dir(self.dl / "App")
        self.torrent("App", ["01.flac"])
        self.qbit.add(self.dl, "Gone", ["Gone/01.flac"])
        out, folders = self.run_batch()
        self.assertEqual(list(folders), [str(self.dl / "Good")])
        self.queued.assert_not_called()
        report = out["source_report"]
        self.assertEqual(report["exclusions"]["app_managed"], 1)
        self.assertEqual(report["exclusions"]["missing"], 1)
        summary = [line for line in self.log if line.startswith("source summary:")]
        self.assertEqual(len(summary), 1, self.log)
        for part in ("source=qbittorrent", "discovered=3", "eligible=1", "excluded_app_managed=1",
                     "excluded_duplicates=0", "missing=1", "queued=1", "ownership_verified="):
            self.assertIn(part, summary[0])
        self.assertFalse([line for line in self.log if line.startswith("folder queued:")])
        self.local_walk.assert_not_called()

    def test_source_gone_before_import_is_permanent_and_never_reviewed(self):
        self.torrent("Album", ["01.flac"])
        real_units = ai._ai_batch_qbit_units

        def units_then_delete(*a, **k):
            out = real_units(*a, **k)
            shutil.rmtree(self.dl / "Album")
            return out

        with mock.patch.object(ai, "_ai_batch_qbit_units", side_effect=units_then_delete):
            out, folders = self.run_batch()
        folder = folders[str(self.dl / "Album")]
        self.assertEqual(folder["status"], "source_missing")
        self.assertEqual(out["folders_source_missing"], 1)
        self.assertEqual(out["folders_retryable"], 0)
        self.queued.assert_not_called()
        self.reimport.assert_not_called()
        # A retry neither requeues it nor re-reads qBittorrent / the folder.
        self.stored = {k: v for k, v in out.items() if k != "folders"}
        self.stored["folder_states"] = {folder["folder_id"]: dict(folder)}
        calls = len(self.qbit.calls)
        out2, folders2 = self.run_batch(recover=True, retry_failed=True, source="local", qbit_inventory=None)
        self.assertEqual(folders2[str(self.dl / "Album")]["status"], "source_missing")
        self.assertEqual(out2["source_mode"], "qbittorrent")
        self.assertEqual(len(self.qbit.calls), calls)
        self.queued.assert_not_called()
        self.local_walk.assert_not_called()

    def test_qbittorrent_failures_fail_closed_without_a_filesystem_walk(self):
        self.torrent("Album", ["01.flac"])
        self.torrent("Other", ["01.flac"])
        failures = {
            "connection": ("/torrents/info", urllib.error.URLError("connection refused")),
            "bad json": ("/torrents/info", ValueError("Expecting value: line 1 column 1")),
            "mid-stream": ("/torrents/files", urllib.error.URLError("reset")),
        }
        cases = [(k, v) for k, v in failures.items()] + [("auth", None), ("malformed", None), ("absent", None)]
        for label, fail in cases:
            with self.subTest(label), mock.patch("os.walk", side_effect=AssertionError("os.walk")):
                self.qbit.fail = fail
                kw = {}
                patches = []
                if label == "auth":
                    patches.append(mock.patch.object(acq, "_qbit_login_cookie", side_effect=pb.ProviderError(
                        pb.ProviderOutcome.AUTHENTICATION_ERROR, "qBittorrent rejected the login",
                        status_code=403, provider="qbittorrent")))
                if label == "malformed":
                    patches.append(mock.patch.object(acq, "_qbit_request_json", return_value={"not": "a list"}))
                if label == "absent":
                    kw["qbit_inventory"] = None
                for p in patches:
                    p.start()
                try:
                    with self.assertRaises(ai.QbitSourceUnverified):
                        self.run_batch(**kw)
                finally:
                    for p in patches:
                        p.stop()
                state = ai._ai_batch_commit.call_args.args[0]
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["current_step"], "source_verification_required")
                self.assertEqual(state["source_error"]["code"], "source_verification_required")
                self.assertEqual(state["folder_states"], {})
                self.local_walk.assert_not_called()
                self.reimport.assert_not_called()
                self.queued.assert_not_called()

    def test_local_source_is_unchanged(self):
        self.audio("Local Album/01.flac")
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(self.dl / "Local Album")]
        out, folders = self.run_batch(source="local", qbit_inventory=None)
        self.local_walk.assert_called_once()
        self.assertEqual(self.qbit.calls, [])
        self.assertEqual(out["source_mode"], "local")
        self.assertEqual(folders[str(self.dl / "Local Album")]["status"], "imported")
        self.assertNotIn("source_files", folders[str(self.dl / "Local Album")])


class LocalMissingSourceTests(_Batch):
    """Uses only the batch API that already existed, so it runs against main too."""

    def test_vanished_local_folder_is_source_missing_not_a_review_row(self):
        self.audio("Moved Away/01.flac")
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(self.dl / "Moved Away")]
        self.reimport.side_effect = AssertionError("Beets called for a missing folder")
        real_process = ai._ai_batch_process_folder

        def gone_then_process(*a, **k):
            shutil.rmtree(self.dl / "Moved Away", ignore_errors=True)
            return real_process(*a, **k)

        self.log = []
        with mock.patch.object(ai, "_ai_batch_process_folder", side_effect=gone_then_process):
            out = ai._run_ai_batch_import(self.batch_id, str(self.dl), self.log, threading.Event(), None)
        folder = out["folders"][0]
        self.assertEqual(folder["status"], "source_missing", self.log)
        self.queued.assert_not_called()


class RouteTests(_Roots):
    def post(self, body):
        with APP.app.test_client() as client:
            return client.post("/api/ai-batch-import", json=body)

    def test_invalid_source_is_refused(self):
        resp = self.post({"source": "ftp"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "invalid_source")

    def test_qbittorrent_not_configured_is_refused_before_any_job(self):
        with mock.patch.object(acq, "QBIT_URL", ""), \
                patch_app_family(APP, "_start_ai_batch_job", side_effect=AssertionError("job started")):
            resp = self.post({"source": "qbittorrent"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "source_verification_required")

    def test_qbittorrent_source_reaches_the_job_and_recover_keeps_it(self):
        started = mock.Mock(return_value=({"ok": True}, 200))
        with mock.patch.object(acq, "QBIT_URL", "http://qbit.invalid"), \
                patch_app_family(APP, "DOWNLOADS_ROOT", self.dl), \
                patch_app_family(APP, "_ai_batch_latest_state", return_value=None), \
                patch_app_family(APP, "_start_ai_batch_job", started):
            self.assertEqual(self.post({"source": "qbittorrent", "path": str(self.outside)}).status_code, 200)
        self.assertEqual(started.call_args.kwargs["source"], "qbittorrent")
        self.assertEqual(Path(started.call_args.args[0]), self.dl)  # the payload path is ignored
        stored = {"batch_job_id": "b1", "source_path": str(self.dl), "source_mode": "qbittorrent",
                  "status": "failed", "folders_retryable": 1}
        started.reset_mock()
        with patch_app_family(APP, "_ai_batch_find_state", return_value=stored), \
                patch_app_family(APP, "_ai_batch_reconcile_state", side_effect=lambda s: s), \
                patch_app_family(APP, "_ai_batch_active_worker_job_id", return_value=""), \
                patch_app_family(APP, "_ai_batch_worker_registered", return_value=False), \
                patch_app_family(APP, "_start_ai_batch_job", started):
            with APP.app.test_client() as client:
                resp = client.post("/api/ai-batch-import/recover", json={"batch_job_id": "b1", "retry_failed": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(started.call_args.kwargs["source"], "qbittorrent")


if __name__ == "__main__":
    unittest.main()
