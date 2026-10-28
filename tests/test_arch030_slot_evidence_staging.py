"""ARCH-030 round-2 (B1/B2/B3): genuine slot evidence + mixed-batch staging.

Covers the integrations/automation producer side of the Option-C hybrid double-
corroborate gate. The GATE itself (music-identity, backend/matching*) consumes
the two evidence maps this branch produces; here we prove the PRODUCER delivers
exactly what the gate needs, and that the deferral/isolation plumbing is correct.

B1  stage_selected_audio_files renames each staged file to ``{track:02d}
    {title}`` from the REQUESTED track, so title/position read back off the
    staged filename are MANUFACTURED, not evidence. The fix preserves two
    independent, GENUINE sources per staged path:
      * file_original_names  -- original pre-rename download basename (sidecar
        written at rename time; slskd.original_download_names_for_folder reads it)
      * file_embedded_ids    -- {title, mb_releasegroupid, mb_albumartistid,
        mb_artistid} read from the ACTUAL staged file's tags (survive the rename;
        acoustid_service.embedded_identity_for_file / _map_for_folder), {} when
        the file carries no readable MB tag.
B2  a mixed batch (>=1 verified + >=1 deferred) must import ONLY the verified
    file inline; the deferred file is separated out and never reaches Beets.
B3  real AcoustID "could not ask" statuses (not_configured/auth_failed/
    lookup_failed) must DEFER (UNAVAILABLE), not delete -- driven through the
    real _audio_identity_decision, which is what production emits.

Synthetic identity only; no network, no real Beets, no real fpcalc.
"""
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

acq = slskd = acs = recovery_carrier = recovery_service = pb = None

RGID = "17a64abf-6d31-473f-a740-38ffa548f225"          # the album's canonical RG
OTHER_RGID = "99999999-6d31-473f-a740-38ffa548f225"    # a different release group
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ARTISTID = "11111111-2222-3333-4444-555555555555"
ALBUMARTISTID = "66666666-7777-8888-9999-000000000000"
RECID = "aaaaaaaa-1111-2222-3333-444444444444"          # the target recording
OTHER_RECID = "bbbbbbbb-1111-2222-3333-444444444444"    # a different recording


def _fd_staging_supported():
    """Mirror of slskd._staging_fd_supported using only ``os`` so it is
    evaluable at class-decoration time (slskd is imported in setUpModule)."""
    return (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
            and os.stat in os.supports_dir_fd and os.rename in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd)


def setUpModule():
    global acq, slskd, acs, recovery_carrier, recovery_service, pb
    import app  # noqa: F401  -- loads the module family in order
    import backend.acquisition_service as acq
    import backend.slskd as slskd
    import backend.acoustid_service as acs
    import backend.recovery_carrier as recovery_carrier
    import backend.recovery_service as recovery_service
    import backend.provider_boundary as pb


def _inject_media_tags(test, tags_by_path):
    """Inject a fake ``beets.mediafile`` so the REAL _read_file_media_tags beets
    branch returns the given per-path tag dicts (the branch the source documents
    as the unit-test path). Exercises the new mb_albumartistid/mb_artistid/title
    fields end to end through embedded_identity_for_file."""
    fields = ("title", "artist", "album", "albumartist", "year", "track", "disc",
              "mb_trackid", "mb_albumid", "mb_releasegroupid",
              "mb_albumartistid", "mb_artistid", "genre")

    class _MediaFile:
        def __init__(self, path):
            d = tags_by_path.get(str(path), {})
            for k in fields:
                setattr(self, k, d.get(k, ""))

    fake = types.ModuleType("beets.mediafile")
    fake.MediaFile = _MediaFile
    p = mock.patch.dict(sys.modules, {"beets.mediafile": fake})
    p.start()
    test.addCleanup(p.stop)


class EmbeddedIdentityProducer(unittest.TestCase):
    """B1: embedded MB identity read from the ACTUAL staged file (survives the
    rename); {} when the file carries no readable MB tag."""

    def _file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "01 Song.flac"   # the LAUNDERED staged name
        f.write_bytes(b"x")
        return tmp.name, f

    def test_genuine_file_exposes_all_identity_fields(self):
        _, f = self._file()
        _inject_media_tags(self, {str(f): {
            "title": "Song", "mb_releasegroupid": RGID.upper(),  # case-folded by producer
            "mb_albumartistid": ALBUMARTISTID, "mb_artistid": ARTISTID,
            "mb_trackid": RECID.upper(), "track": "3", "disc": "1"}})
        got = acs.embedded_identity_for_file(f)
        self.assertEqual(got, {
            "title": "Song",
            "mb_releasegroupid": RGID,
            "mb_albumartistid": ALBUMARTISTID,
            "mb_artistid": ARTISTID,
            "mb_recording_id": RECID,   # F1-RECID: recording id now carried
            "track": "3",               # F1-POSITION: independent position
            "disc": "1",
        })

    def test_wrong_version_embeds_disagreeing_release_group(self):
        _, f = self._file()
        _inject_media_tags(self, {str(f): {
            "title": "Song (Live 2019)", "mb_releasegroupid": OTHER_RGID,
            "mb_albumartistid": ALBUMARTISTID, "mb_artistid": ARTISTID}})
        got = acs.embedded_identity_for_file(f)
        # The producer carries the GENUINE embedded identity; it disagrees with
        # the album RG -> the gate has grounds to refuse auto-insert (Option C).
        self.assertEqual(got["mb_releasegroupid"], OTHER_RGID)
        self.assertNotEqual(got["mb_releasegroupid"], RGID)
        self.assertEqual(got["title"], "Song (Live 2019)")

    def test_no_mb_tag_yields_empty_cannot_corroborate(self):
        _, f = self._file()
        _inject_media_tags(self, {str(f): {"title": "Song"}})  # title only, no MB id
        self.assertEqual(acs.embedded_identity_for_file(f), {},
                         "a bare title with no MB id cannot corroborate -> {}")

    def test_unreadable_file_yields_empty(self):
        _inject_media_tags(self, {})
        self.assertEqual(acs.embedded_identity_for_file("/no/such/file.flac"), {})

    def test_folder_map_includes_every_audio_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        good = Path(tmp.name) / "01 Song.flac"
        bare = Path(tmp.name) / "02 Intro.flac"
        good.write_bytes(b"x")
        bare.write_bytes(b"x")
        _inject_media_tags(self, {
            str(good): {"title": "Song", "mb_releasegroupid": RGID,
                        "mb_albumartistid": ALBUMARTISTID, "mb_artistid": ARTISTID},
            str(bare): {"title": "Intro"}})
        m = acs.embedded_identity_map_for_folder(tmp.name)
        self.assertEqual(m[str(good)]["mb_releasegroupid"], RGID)
        self.assertEqual(m[str(bare)], {}, "no-MB-tag file maps to {} (cannot corroborate)")


class OriginalNamesSidecarRoundTrip(unittest.TestCase):
    """B1: the original pre-rename download name survives staging as a sidecar."""

    def test_sidecar_round_trip_maps_staged_abs_path_to_original(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stage = Path(tmp.name)
        (stage / "01 Song.flac").write_bytes(b"x")
        (stage / "CD 02").mkdir()
        (stage / "CD 02" / "03 Other.flac").write_bytes(b"x")
        slskd._write_original_names_sidecar(stage, {
            "01 Song.flac": "Song (Live 2019) [YouTube].flac",
            "CD 02/03 Other.flac": "Other - official audio.flac",
        })
        got = slskd.original_download_names_for_folder(str(stage))
        self.assertEqual(got[str(stage / "01 Song.flac")],
                         "Song (Live 2019) [YouTube].flac")
        self.assertEqual(got[str(stage / "CD 02" / "03 Other.flac")],
                         "Other - official audio.flac")

    def test_merge_preserves_prior_entries_on_reuse(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stage = Path(tmp.name)
        slskd._write_original_names_sidecar(stage, {"01 A.flac": "A orig.flac"})
        slskd._write_original_names_sidecar(stage, {"02 B.flac": "B orig.flac"})
        got = slskd.original_download_names_for_folder(str(stage))
        self.assertEqual(got[str(stage / "01 A.flac")], "A orig.flac")
        self.assertEqual(got[str(stage / "02 B.flac")], "B orig.flac")

    def test_absent_sidecar_returns_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(slskd.original_download_names_for_folder(tmp.name), {})


@unittest.skipUnless(_fd_staging_supported(),
                     "fd-relative symlink-safe staging unsupported on this host "
                     "(Linux-only; the authoritative Docker suite exercises it)")
class StageRenameWritesGenuineEvidence(unittest.TestCase):
    """B1 end to end: the REAL slskd stage rename launders the filename to
    ``{track:02d} {title}`` from the REQUESTED track, and the sidecar preserves
    the original download name as independent evidence."""

    def _stage(self, src_name, target):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name, "downloads").resolve()
        aldir = root / "src"
        aldir.mkdir(parents=True)
        (aldir / src_name).write_bytes(b"x")
        log = []
        staged = slskd.stage_selected_audio_files(
            root, {".flac"}, str(aldir), [aldir / src_name],
            "Artist", "Album", log, target_tracks=[target])
        return staged

    def test_wrong_version_rename_is_laundered_but_original_survives(self):
        staged = self._stage("Song (Live 2019) [YouTube].flac",
                              {"disc": 1, "track": 1, "title": "Song"})
        files = [p.name for p in Path(staged).glob("*.flac")]
        self.assertEqual(files, ["01 Song.flac"],
                         "the staged filename is MANUFACTURED from the requested track")
        names = slskd.original_download_names_for_folder(staged)
        self.assertEqual(names[str(Path(staged) / "01 Song.flac")],
                         "Song (Live 2019) [YouTube].flac",
                         "the GENUINE original name must survive the laundering rename")

    def test_restage_carries_genuine_original_not_the_laundered_name(self):
        # ARCH-030 F2: restaging the still-missing subset FROM an already
        # app-staged/renamed folder must carry the GENUINE original download name
        # forward (from the source sidecar), not relabel the laundered "01 Song"
        # staged name as the "original" -- which would collapse Option-C's two
        # independent sources (original-name + embedded-tags) to embedded-only.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name, "downloads").resolve()
        aldir = root / "src"
        aldir.mkdir(parents=True)
        (aldir / "Song (Live 2019) [YouTube].flac").write_bytes(b"x")
        log = []
        target = {"disc": 1, "track": 1, "title": "Song"}
        staged1 = slskd.stage_selected_audio_files(
            root, {".flac"}, str(aldir),
            [aldir / "Song (Live 2019) [YouTube].flac"],
            "Artist", "Album", log, target_tracks=[target])
        s1 = Path(staged1) / "01 Song.flac"
        self.assertTrue(s1.exists(), "first stage produced the laundered staged name")

        staged2 = slskd.stage_selected_audio_files(
            root, {".flac"}, staged1, [s1], "Artist", "Album", log,
            target_tracks=[target])
        self.assertNotEqual(Path(staged2), Path(staged1), "restage uses a new folder")
        names = slskd.original_download_names_for_folder(staged2)
        self.assertEqual(names[str(Path(staged2) / "01 Song.flac")],
                         "Song (Live 2019) [YouTube].flac",
                         "restage must carry the genuine original forward, not '01 Song.flac'")


class OptionCEvidenceDiscriminator(unittest.TestCase):
    """B1: the two produced sources carry exactly the discriminator Option C
    uses (title + RG + artist corroboration). The accept/review VERDICT is the
    music-identity gate's; here we prove the producer feeds it the right facts
    for each of the mandatory scenarios (a)/(b)/(c)."""

    TARGET_TITLE = "Song"
    STAGED = "01 Song.flac"   # what the rename produced for every case below

    def _evidence(self, *, original_name, embedded_tags):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stage = Path(tmp.name)
        staged_file = stage / self.STAGED
        staged_file.write_bytes(b"x")
        slskd._write_original_names_sidecar(stage, {self.STAGED: original_name})
        _inject_media_tags(self, {str(staged_file): embedded_tags} if embedded_tags else {})
        original_names = slskd.original_download_names_for_folder(str(stage))
        embedded = acs.embedded_identity_map_for_folder(str(stage))
        return str(staged_file), original_names, embedded

    @staticmethod
    def _title_corroborates(name_or_title, target):
        t = Path(name_or_title).stem.lower()
        return target.lower() in t and "(live" not in t and "live 2019" not in t

    def test_a_wrong_version_evidence_disagrees(self):
        # 'Song (Live 2019)' audio renamed to '01 Song.flac', NO_RESULT, near
        # duration. BOTH genuine sources disagree with the target -> the gate
        # must NOT auto-insert (manual/refuse).
        path, names, embedded = self._evidence(
            original_name="Song (Live 2019) [YouTube].flac",
            embedded_tags={"title": "Song (Live 2019)", "mb_releasegroupid": OTHER_RGID,
                           "mb_albumartistid": ALBUMARTISTID, "mb_artistid": ARTISTID})
        self.assertFalse(self._title_corroborates(names[path], self.TARGET_TITLE),
                         "original name reveals the live version -> does not corroborate")
        self.assertNotEqual(embedded[path].get("mb_releasegroupid"), RGID,
                            "embedded RG disagrees with the album RG")

    def test_b_genuine_file_both_sources_corroborate(self):
        path, names, embedded = self._evidence(
            original_name="Song.flac",
            embedded_tags={"title": "Song", "mb_releasegroupid": RGID,
                           "mb_albumartistid": ALBUMARTISTID, "mb_artistid": ARTISTID})
        self.assertTrue(self._title_corroborates(names[path], self.TARGET_TITLE),
                        "original download name corroborates the target title")
        self.assertEqual(embedded[path].get("mb_releasegroupid"), RGID,
                         "embedded RG corroborates the album RG")
        self.assertEqual(embedded[path].get("title").lower(), self.TARGET_TITLE.lower())

    def test_c_no_embedded_tags_cannot_corroborate(self):
        path, names, embedded = self._evidence(
            original_name="Song.flac", embedded_tags=None)
        # Original name corroborates, but with NO embedded MB identity the second
        # independent source is absent -> Option C requires BOTH -> manual review.
        self.assertTrue(self._title_corroborates(names[path], self.TARGET_TITLE))
        self.assertEqual(embedded[path], {},
                         "absent embedded identity -> cannot corroborate -> manual")


class B3RealUnavailableStatusesDefer(unittest.TestCase):
    """B3: the REAL _audio_identity_decision emits not_configured/auth_failed/
    lookup_failed for 'could not ask' outcomes, and _download_identity_deferral
    must map every one of them to UNAVAILABLE (defer), never to delete."""

    def _decide(self, outcome):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "01 Song.flac"
        f.write_bytes(b"x")
        res = pb.ProviderResult("acoustid", outcome)
        with mock.patch.object(acs, "_acoustid_lookup_cached_outcome", return_value=res):
            return acs._audio_identity_decision(
                str(f), expected_artist="Artist", expected_title="Song",
                expected_mb_trackid="rec-6")

    def _defer(self, acoustid_status):
        return acq._download_identity_deferral({"identity": {"acoustid_status": acoustid_status}})

    def test_not_configured_defers(self):
        d = self._decide(pb.ProviderOutcome.NOT_CONFIGURED)
        self.assertEqual(d["acoustid_status"], "not_configured")
        self.assertEqual(self._defer(d["acoustid_status"]), "unavailable")

    def test_auth_failed_defers(self):
        d = self._decide(pb.ProviderOutcome.AUTHENTICATION_ERROR)
        self.assertEqual(d["acoustid_status"], "auth_failed")
        self.assertEqual(self._defer(d["acoustid_status"]), "unavailable")

    def test_lookup_failed_defers(self):
        # Any other unanswered outcome (outage / throttle / missing fpcalc).
        d = self._decide(pb.ProviderOutcome.UNAVAILABLE)
        self.assertEqual(d["acoustid_status"], "lookup_failed")
        self.assertEqual(self._defer(d["acoustid_status"]), "unavailable")

    def test_rate_limited_defers(self):
        d = self._decide(pb.ProviderOutcome.RATE_LIMITED)
        self.assertEqual(d["acoustid_status"], "lookup_failed")
        self.assertEqual(self._defer(d["acoustid_status"]), "unavailable")

    def test_no_result_defers_and_candidate_deletes(self):
        # NO_RESULT -> defer; a returned (contradicting) candidate -> delete.
        self.assertEqual(self._defer("no_result"), "no_result")
        self.assertEqual(self._defer("candidate"), "",
                         "a wrong-audio candidate is still deleted, not deferred")


@unittest.skipUnless(_fd_staging_supported(),
                     "the deferred-file separation uses the fd-relative hardened "
                     "staging move (Linux-only; the Docker suite exercises it)")
class B2MixedBatchIsolatesDeferredFromBeets(unittest.TestCase):
    """B2 end to end: through the real start_album_download job, a mixed batch
    (1 verified + 1 NO_RESULT) imports ONLY the verified file inline; the
    NO_RESULT file is separated out (via the SEC-007-hardened staging move) and
    never reaches the Beets import."""

    PAYLOAD = {
        "artist": "Artist", "album": "Album", "year": "2024", "method": "ytdlp",
        "auto_import": True, "existing_album_id": 42, "mb_releasegroupid": RGID,
        "missing_tracks": [
            {"track": 1, "title": "Alpha", "recording_id": "rec-1"},
            {"track": 6, "title": "Last Song", "recording_id": "rec-6"},
        ],
    }

    def _matcher(self, path_value, _artist, title, _mbid=""):
        stem = Path(path_value).stem
        if "Alpha" in stem and title == "Alpha":
            return {"ok": True, "title_score": 1.0, "artist_score": 1.0,
                    "identity": {"final_action": "accept", "acoustid_status": "confirmed",
                                 "acoustid_match_score": 0.95}}
        return {"ok": False, "title_score": 1.0 if stem == title else 0.0, "artist_score": 1.0,
                "identity": {"final_action": "review", "acoustid_status": "no_result",
                             "acoustid_match_score": 0.0}}

    def test_mixed_batch_imports_only_verified_defers_no_result(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name, "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        state_dir = Path(tmp.name, "carrier_state")

        def _fake_ytdlp(artist, album, year, yt_dir, log, tracks=None, *a, **k):
            Path(yt_dir).mkdir(parents=True, exist_ok=True)
            (Path(yt_dir) / "Alpha.flac").write_bytes(b"x")
            (Path(yt_dir) / "Last Song.flac").write_bytes(b"x")
            return 2

        def _forbid_delete(p):
            raise AssertionError(f"no delete in a verified+NO_RESULT mixed batch: {p}")

        env = dict(os.environ)
        env["RECOVERY_CARRIER_STATE_DIR"] = str(state_dir)
        # Align the hardened staging move's allowed root (config_layers.
        # downloads_root) with this test's temp downloads tree.
        env["DOWNLOADS_ROOT"] = str(downloads)
        reimport = mock.Mock(return_value="imp-1")
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "validated_downloads_root", return_value=downloads),
            mock.patch.object(acq, "slskd_configured", return_value=False),
            mock.patch.object(acq, "_resolve_album_release_for_import", return_value=RELEASE),
            mock.patch.object(acq, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "tracks": [1, 2, 3, 4, 5, 6],
                                            "release_group": RGID}),
            mock.patch.object(acq, "_wanted_tracks_not_in_album",
                              return_value=[{"disc": 1, "track": 1, "title": "Alpha",
                                             "mb_trackid": "rec-1", "duration_ms": 180000},
                                            {"disc": 1, "track": 6, "title": "Last Song",
                                             "mb_trackid": "rec-6", "duration_ms": 190000}]),
            mock.patch.object(acq, "_album_folder_for_album_id", return_value="/music/Artist/Album"),
            mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_playlist_download_match", side_effect=self._matcher),
            mock.patch.object(acq, "_playlist_download_audio_allowed", return_value=True),
            mock.patch.object(acq, "_playlist_stamp_download_tags"),
            mock.patch.object(acq, "_playlist_identity_log", side_effect=lambda m: str(m)),
            mock.patch.object(acq, "_validate_import_source_audio"),
            mock.patch.object(acq, "_start_reimport_disk_job_internal", reimport),
            mock.patch.object(acq, "_wait_for_child_job", return_value={"ok": True}),
            mock.patch.object(acq, "acquisition_queue_mark_done"),
            mock.patch.object(acq.composite_workflows, "delete_file", side_effect=_forbid_delete),
        ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        jobs = started[1]
        jobs.start_python.return_value.job_id = "job-xyz"

        body, status = acq.start_album_download(self.PAYLOAD)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

        job_fn = jobs.start_python.call_args[0][0]
        log = []
        result = job_fn(log, threading.Event())

        # Inline Beets import ran exactly once, on the folder with ONLY the
        # verified track.
        reimport.assert_called_once()
        import_dir = Path(reimport.call_args[0][0])
        imported_wanted = reimport.call_args.kwargs["wanted_tracks"]
        self.assertEqual([t["title"] for t in imported_wanted], ["Alpha"])
        self.assertTrue((import_dir / "Alpha.flac").exists(), log)
        self.assertFalse((import_dir / "Last Song.flac").exists(),
                         "the NO_RESULT file must be separated OUT of the inline-import folder")

        # The deferred NO_RESULT file survives in the sibling slot-gate folder.
        slot_dir = import_dir.parent / f"{import_dir.name}__slot_deferred"
        self.assertTrue((slot_dir / "Last Song.flac").exists(),
                        "the NO_RESULT file must be routed to the slot-gate folder")
        self.assertTrue(result.get("imported"), result)
        self.assertEqual([t["title"] for t in result.get("undelivered_tracks", [])],
                         ["Last Song"])


class F8DeferredSeparationFailsClosed(unittest.TestCase):
    """ARCH-030 F8: _separate_deferred_slot_files must report whether EVERY
    deferred file was relocated. A file whose move RAISED stays in import_dir, so
    ``complete`` is False and the caller must NOT inline-import it (bypassing the
    slot gate) and must NOT delete the retained audio. Patches the hardened move
    so the test needs no fd_dir support."""

    def _run(self, fail_name):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import_dir = Path(tmp.name) / "imp"
        import_dir.mkdir()
        a = import_dir / "01 Alpha.flac"
        b = import_dir / "06 Last Song.flac"
        a.write_bytes(b"x")
        b.write_bytes(b"x")

        def _move(src, dest):
            if fail_name in Path(src).name:
                raise OSError("simulated collision / symlink refusal / I/O")
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)

        with mock.patch.object(acq.composite_workflows, "move_staging_file",
                               side_effect=_move):
            slot_dir, complete = acq._separate_deferred_slot_files(
                str(import_dir), [str(a), str(b)], [])
        return import_dir, slot_dir, complete

    def test_incomplete_when_one_deferred_move_fails(self):
        import_dir, slot_dir, complete = self._run("Last Song")
        self.assertFalse(complete,
                         "one unmovable deferred file -> incomplete -> caller fails closed")
        # The unmovable deferred file is RETAINED in import_dir (never deleted).
        self.assertTrue((import_dir / "06 Last Song.flac").exists(),
                        "the leftover deferred file must stay staged, not be lost")

    def test_complete_when_all_deferred_move(self):
        import_dir, slot_dir, complete = self._run("__nope__")
        self.assertTrue(complete, "all deferred files relocated -> complete -> inline import may run")
        self.assertTrue(slot_dir)


@unittest.skipUnless(_fd_staging_supported(),
                     "the deferred-file separation uses the fd-relative hardened "
                     "staging move (Linux-only; the Docker suite exercises it)")
class F8MixedBatchFailsClosedEndToEnd(unittest.TestCase):
    """ARCH-030 F8 end to end: through the real start_album_download job, a mixed
    batch (1 verified + 2 deferred) where one deferred file CANNOT be separated
    must NOT run the inline Beets import on the folder and must NOT delete the
    retained acquisition; the whole carrier is left staged for the slot gate."""

    PAYLOAD = {
        "artist": "Artist", "album": "Album", "year": "2024", "method": "ytdlp",
        "auto_import": True, "existing_album_id": 42, "mb_releasegroupid": RGID,
        "missing_tracks": [
            {"track": 1, "title": "Alpha", "recording_id": "rec-1"},
            {"track": 3, "title": "Beta", "recording_id": "rec-3"},
            {"track": 6, "title": "Last Song", "recording_id": "rec-6"},
        ],
    }

    def _matcher(self, path_value, _artist, title, _mbid=""):
        stem = Path(path_value).stem
        if "Alpha" in stem and title == "Alpha":
            return {"ok": True, "title_score": 1.0, "artist_score": 1.0,
                    "identity": {"final_action": "accept", "acoustid_status": "confirmed",
                                 "acoustid_match_score": 0.95}}
        return {"ok": False, "title_score": 1.0 if stem == title else 0.0, "artist_score": 1.0,
                "identity": {"final_action": "review", "acoustid_status": "no_result",
                             "acoustid_match_score": 0.0}}

    def test_incomplete_separation_blocks_inline_import_and_keeps_audio(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name, "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        state_dir = Path(tmp.name, "carrier_state")

        def _fake_ytdlp(artist, album, year, yt_dir, log, tracks=None, *a, **k):
            Path(yt_dir).mkdir(parents=True, exist_ok=True)
            (Path(yt_dir) / "Alpha.flac").write_bytes(b"x")
            (Path(yt_dir) / "Beta.flac").write_bytes(b"x")
            (Path(yt_dir) / "Last Song.flac").write_bytes(b"x")
            return 3

        def _forbid_delete(p):
            raise AssertionError(f"no delete when a deferred file cannot be separated: {p}")

        real_move = acq.composite_workflows.move_staging_file

        def _move(src, dest):
            # One of the two deferred files cannot be separated out.
            if "Last Song" in Path(src).name:
                raise OSError("simulated collision / symlink refusal / I/O")
            return real_move(src, dest)

        env = dict(os.environ)
        env["RECOVERY_CARRIER_STATE_DIR"] = str(state_dir)
        env["DOWNLOADS_ROOT"] = str(downloads)
        reimport = mock.Mock(return_value="imp-1")
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "validated_downloads_root", return_value=downloads),
            mock.patch.object(acq, "slskd_configured", return_value=False),
            mock.patch.object(acq, "_resolve_album_release_for_import", return_value=RELEASE),
            mock.patch.object(acq, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "tracks": [1, 2, 3, 4, 5, 6],
                                            "release_group": RGID}),
            mock.patch.object(acq, "_wanted_tracks_not_in_album",
                              return_value=[{"disc": 1, "track": 1, "title": "Alpha",
                                             "mb_trackid": "rec-1", "duration_ms": 180000},
                                            {"disc": 1, "track": 3, "title": "Beta",
                                             "mb_trackid": "rec-3", "duration_ms": 185000},
                                            {"disc": 1, "track": 6, "title": "Last Song",
                                             "mb_trackid": "rec-6", "duration_ms": 190000}]),
            mock.patch.object(acq, "_album_folder_for_album_id", return_value="/music/Artist/Album"),
            mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_playlist_download_match", side_effect=self._matcher),
            mock.patch.object(acq, "_playlist_download_audio_allowed", return_value=True),
            mock.patch.object(acq, "_playlist_stamp_download_tags"),
            mock.patch.object(acq, "_playlist_identity_log", side_effect=lambda m: str(m)),
            mock.patch.object(acq, "_validate_import_source_audio"),
            mock.patch.object(acq, "_start_reimport_disk_job_internal", reimport),
            mock.patch.object(acq, "_wait_for_child_job", return_value={"ok": True}),
            mock.patch.object(acq, "acquisition_queue_mark_done"),
            mock.patch.object(acq.composite_workflows, "move_staging_file", side_effect=_move),
            mock.patch.object(acq.composite_workflows, "delete_file", side_effect=_forbid_delete),
        ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        jobs = started[1]
        jobs.start_python.return_value.job_id = "job-xyz"

        body, status = acq.start_album_download(self.PAYLOAD)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

        job_fn = jobs.start_python.call_args[0][0]
        log = []
        result = job_fn(log, threading.Event())

        # Fail closed: no inline Beets import ran on the folder with a leftover
        # unverified file.
        reimport.assert_not_called()
        self.assertFalse(result.get("imported"), result)
        self.assertTrue(result.get("staged_for_slot_gate"), result)
        # The retained audio still exists on disk (nothing was deleted). The
        # ytdlp download path keeps the original basenames (no slskd rename).
        remaining = {p.name for p in downloads.rglob("*.flac")}
        self.assertIn("Last Song.flac", remaining,
                      "the un-separable deferred file must be retained, not deleted")


if __name__ == "__main__":
    unittest.main()
