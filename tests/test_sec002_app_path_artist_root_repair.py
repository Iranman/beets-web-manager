"""Regression tests for SEC-002 app.py path-injection wave 3: the
artist-folder/root-folder repair route family
(/api/clean/artist-folders/*), covering the ten open py/path-injection
alerts rooted in the unrestricted `root` request parameter accepted by
_apply_artist_folder_groups() and the _stamp_artist_folder_* chain. See
docs/TECHNICAL_DEBT.md SEC-002 for the full alert inventory.
"""

import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import app as app_module


class ArtistFolderRepairRootBoundaryTests(unittest.TestCase):
    """Every /api/clean/artist-folders/* route accepted a `root` request
    value with only an exists()/is_dir() check -- no containment against
    MUSIC_ROOT -- before feeding it into destructive filesystem operations
    (rename, merge, rmdir-equivalent) and direct Beets DB path rewrites.
    _artist_folder_repair_root() closes this."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.music = self.root / "music"
        self.outside = self.root / "outside"
        self.music.mkdir()
        self.outside.mkdir()
        self.patches = [
            mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}),
            mock.patch.object(app_module, "MUSIC_ROOT", self.music),
            mock.patch.object(app_module, "_MUSIC_LIBRARY_ROOT", str(self.music)),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, path, payload):
        return self.client.post(path, json=payload)

    def _make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    # -- scan --------------------------------------------------------------

    def test_scan_rejects_outside_root(self):
        sentinel = self.outside / "SentinelArtist"
        sentinel.mkdir()
        resp = self._post("/api/clean/artist-folders/scan", {"root": str(self.outside)})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertTrue(sentinel.exists())

    def test_scan_accepts_music_root_itself(self):
        (self.music / "Artist A").mkdir()
        resp = self._post("/api/clean/artist-folders/scan", {"root": str(self.music)})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])

    def test_scan_rejects_subfolder_of_music_root(self):
        # The UI labels this field "Library root" with no supported concept
        # of scanning an individual artist/album folder. Accepting a
        # descendant would let the scan misinterpret that folder's own
        # children (e.g. album folders, if an artist folder were passed) as
        # "artist folders" and destructively merge unrelated albums, plus
        # rewrite the Beets DB's albumartist column with garbage. root must
        # be exactly MUSIC_ROOT.
        sub = self.music / "Artist A"
        sub.mkdir()
        resp = self._post("/api/clean/artist-folders/scan", {"root": str(sub)})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])

    def test_scan_rejects_symlink_root_component(self):
        target = self.outside / "escape"
        target.mkdir()
        link = self.music / "linked"
        self._make_symlink(link, target, directory=True)
        resp = self._post("/api/clean/artist-folders/scan", {"root": str(link)})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])

    def test_scan_rejects_root_prefix_collision(self):
        # A sibling directory whose name merely starts with the music root's
        # name/text must not be accepted as "under" it.
        collider = self.root / (self.music.name + "-not-actually-inside")
        collider.mkdir()
        (collider / "SentinelArtist").mkdir()
        resp = self._post("/api/clean/artist-folders/scan", {"root": str(collider)})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])

    def test_scan_rejects_encoded_traversal_root(self):
        resp = self._post(
            "/api/clean/artist-folders/scan",
            {"root": str(self.music) + "/%2e%2e/outside"},
        )
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])

    def test_scan_rejects_windows_and_unc_style_roots(self):
        for bad in (r"C:\data\album", r"\\server\share", "//server/share", "relative/path"):
            resp = self._post("/api/clean/artist-folders/scan", {"root": bad})
            data = resp.get_json()
            self.assertEqual(resp.status_code, 400, (bad, data))
            self.assertFalse(data["ok"])

    # -- merge ---------------------------------------------------------------

    def test_merge_rejects_outside_root(self):
        sentinel = self.outside / "SentinelArtist"
        sentinel.mkdir()
        resp = self._post(
            "/api/clean/artist-folders/merge",
            {"root": str(self.outside), "keys": [], "dry_run": True},
        )
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])
        self.assertTrue(sentinel.exists())

    def test_merge_dry_run_accepts_music_root(self):
        resp = self._post(
            "/api/clean/artist-folders/merge",
            {"root": str(self.music), "keys": [], "dry_run": True},
        )
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])

    def test_merge_job_uses_validated_root_not_raw_request_text(self):
        # Exercise the actual apply (non-dry-run) job path end to end,
        # synchronously, to prove the outer route's validated/resolved root
        # -- not the raw request string -- is what the background job
        # closure actually captures and operates on.
        outside_sentinel = self.outside / "sentinel.flac"
        outside_sentinel.write_bytes(b"x")
        payload = {"root": str(self.music) + "/", "keys": [], "dry_run": False}
        with app_module.app.test_request_context(
            "/api/clean/artist-folders/merge", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ):
            captured = {}

            def fake_start_python(fn, label=None, metadata=None):
                log = []
                fn(log, cancel_event=None)
                captured["log"] = log
                captured["metadata"] = metadata
                return mock.Mock(job_id="job-test")

            with mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
                response = app_module.clean_artist_folders_merge()
        data = response.get_json()
        self.assertTrue(data["ok"])
        # The job metadata's recorded path must be the canonicalized music
        # root, never the raw (trailing-slash) request text.
        self.assertEqual(captured["metadata"]["path"], str(self.music))
        self.assertTrue(outside_sentinel.exists())

    # -- stamp-mbid ------------------------------------------------------------

    def test_stamp_mbid_rejects_outside_root(self):
        resp = self._post(
            "/api/clean/artist-folders/stamp-mbid",
            {"root": str(self.outside), "dry_run": True},
        )
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400, data)
        self.assertFalse(data["ok"])

    def test_stamp_mbid_dry_run_accepts_music_root(self):
        resp = self._post(
            "/api/clean/artist-folders/stamp-mbid",
            {"root": str(self.music), "dry_run": True},
        )
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])


class ArtistFolderRepairRootHelperTests(unittest.TestCase):
    """Direct coverage of _artist_folder_repair_root()'s decode-depth and
    literal-percent behavior, matching the contract established by the
    wave 2 import-review path validator it reuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.music = Path(self.tmp.name).resolve() / "music"
        self.music.mkdir()
        patch = mock.patch.object(app_module, "MUSIC_ROOT", self.music)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_literal_percent_music_root_is_preserved(self):
        # MUSIC_ROOT itself may legitimately contain a literal '%' (an
        # operator-configured path, not request text); the exact-equality
        # contract must still accept it rather than mistaking it for an
        # encoded sequence.
        percent_root = Path(self.tmp.name).resolve() / "100% Legit Music"
        percent_root.mkdir()
        with mock.patch.object(app_module, "MUSIC_ROOT", percent_root):
            resolved, error = app_module._artist_folder_repair_root(str(percent_root))
        self.assertIsNone(error)
        self.assertEqual(resolved, percent_root.resolve())

    def test_subfolder_of_music_root_is_rejected(self):
        folder = self.music / "Some Artist"
        folder.mkdir()
        resolved, error = app_module._artist_folder_repair_root(str(folder))
        self.assertIsNone(resolved)
        self.assertIsNotNone(error)

    def test_double_encoded_traversal_rejected(self):
        payload = str(self.music) + "/%252e%252e%252foutside"
        resolved, error = app_module._artist_folder_repair_root(payload)
        self.assertIsNone(resolved)
        self.assertIsNotNone(error)

    def test_null_byte_rejected(self):
        resolved, error = app_module._artist_folder_repair_root(str(self.music) + "\x00")
        self.assertIsNone(resolved)
        self.assertIsNotNone(error)


class ArtistFolderMergeIdentityTests(unittest.TestCase):
    """_apply_artist_folder_groups() previously merged folders grouped by
    normalized-name equality alone, with zero identity verification when no
    MusicBrainz artist ID evidence existed for the group -- violating the
    project's non-negotiable 'do not merge on fuzzy name matching alone'
    rule. It now requires an AcoustID fingerprint check (reusing the same
    _artist_folder_fingerprint_confirms() helper already proven in the
    stamp-mbid plain-name-duplicate path) before merging any name-only
    group, and refuses on a confirmed mismatch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.music = Path(self.tmp.name).resolve() / "music"
        self.music.mkdir()
        self.patches = [
            mock.patch.object(app_module, "MUSIC_ROOT", self.music),
            mock.patch.object(app_module, "_MUSIC_LIBRARY_ROOT", str(self.music)),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_name_only_group_blocked_on_confirmed_fingerprint_mismatch(self):
        # NTFS is case-insensitive, so "Bob Marley"/"BOB MARLEY" can't
        # coexist as two real directories in a Windows test fixture; a
        # spacing variant exercises the same name-only grouping path.
        a = self.music / "Bob Marley"
        b = self.music / "Bob  Marley"
        a.mkdir()
        b.mkdir()
        (a / "track.mp3").write_bytes(b"fake-audio-a")
        (b / "track.mp3").write_bytes(b"fake-audio-b")
        log = []
        with mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=False) as fp:
            summary = app_module._apply_artist_folder_groups(
                str(self.music), None, False, log, use_musicbrainz=False,
            )
        self.assertTrue(fp.called)
        self.assertEqual(summary["files"], 0)
        self.assertTrue(any("different artist" in line for line in log))
        # Neither folder was touched: no merge, no removal.
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertTrue((a / "track.mp3").exists())
        self.assertTrue((b / "track.mp3").exists())

    def test_name_only_group_merges_when_fingerprint_confirms(self):
        a = self.music / "Bob  Marley"
        b = self.music / "Bob Marley"
        a.mkdir()
        (a / "track.mp3").write_bytes(b"fake-audio")
        b.mkdir()
        log = []
        with mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=True) as fp:
            summary = app_module._apply_artist_folder_groups(
                str(self.music), None, False, log, use_musicbrainz=False,
            )
        self.assertTrue(fp.called)
        self.assertEqual(summary["folders"], 1)
        # Exactly one of the two variant folders survives as canonical, and
        # the track that started in the source folder ends up inside it.
        surviving = [p for p in (a, b) if p.exists()]
        self.assertEqual(len(surviving), 1)
        self.assertTrue((surviving[0] / "track.mp3").exists())

    def test_group_with_musicbrainz_artist_id_still_requires_fingerprint_confirmation(self):
        # group["musicbrainz"]["id"] is derived from a MusicBrainz *text
        # search on the folder name* (_mb_canonical_for_artist_entries), not
        # from per-folder audio evidence -- two different real artists who
        # happen to share a folder name would produce the same search
        # result. It must not bypass fingerprint verification.
        src = self.music / "Bob  Marley"
        dst = self.music / "Bob Marley"
        src.mkdir()
        dst.mkdir()
        (src / "track.mp3").write_bytes(b"fake-audio")
        log = []
        with mock.patch.object(
            app_module, "_scan_artist_folder_groups",
            return_value=[{
                "key": "bobmarley",
                "canonical": {"path": str(dst), "name": "Bob Marley"},
                "sources": [{"path": str(src), "name": "Bob  Marley"}],
                "musicbrainz": {"id": "9a70dc00-46ed-4b1b-a415-a4fb6dcb4d0f"},
            }],
        ), mock.patch.object(
            app_module, "_artist_folder_fingerprint_confirms", return_value=True,
        ) as fp:
            summary = app_module._apply_artist_folder_groups(
                str(self.music), None, False, log, use_musicbrainz=True,
            )
        fp.assert_called_once()
        self.assertEqual(summary["folders"], 1)
        self.assertFalse(src.exists())

    def test_group_with_musicbrainz_artist_id_blocked_on_fingerprint_mismatch(self):
        src = self.music / "Bob  Marley"
        dst = self.music / "Bob Marley"
        src.mkdir()
        dst.mkdir()
        (src / "track.mp3").write_bytes(b"fake-audio")
        log = []
        with mock.patch.object(
            app_module, "_scan_artist_folder_groups",
            return_value=[{
                "key": "bobmarley",
                "canonical": {"path": str(dst), "name": "Bob Marley"},
                "sources": [{"path": str(src), "name": "Bob  Marley"}],
                "musicbrainz": {"id": "9a70dc00-46ed-4b1b-a415-a4fb6dcb4d0f"},
            }],
        ), mock.patch.object(
            app_module, "_artist_folder_fingerprint_confirms", return_value=False,
        ):
            summary = app_module._apply_artist_folder_groups(
                str(self.music), None, False, log, use_musicbrainz=True,
            )
        self.assertEqual(summary["files"], 0)
        self.assertTrue(src.exists())
        self.assertTrue(dst.exists())

    def test_name_only_group_blocked_when_fingerprint_unavailable(self):
        # fp_result is None (no fingerprint evidence: empty folder, no
        # fpcalc/AcoustID reachable, provider error, ambiguous result, etc.)
        # must NOT be treated as authorization -- only an explicit True
        # confirms the match. This was the actual bug in the pre-review
        # implementation: it only blocked on False, silently letting a
        # merge proceed whenever the fingerprint service simply failed to
        # produce a result.
        a = self.music / "Bob Marley"
        b = self.music / "Bob  Marley"
        a.mkdir()
        b.mkdir()
        (a / "track.mp3").write_bytes(b"fake-audio-a")
        (b / "track.mp3").write_bytes(b"fake-audio-b")
        log = []
        with mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=None) as fp:
            summary = app_module._apply_artist_folder_groups(
                str(self.music), None, False, log, use_musicbrainz=False,
            )
        self.assertTrue(fp.called)
        self.assertEqual(summary["files"], 0)
        self.assertTrue(any("no AcoustID fingerprint evidence was available" in line for line in log))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertTrue((a / "track.mp3").exists())
        self.assertTrue((b / "track.mp3").exists())


class StampMbidPlainNameDuplicateFingerprintTests(unittest.TestCase):
    """_stamp_artist_folder_scan()'s plain-name-duplicate path (merging an
    unstamped folder into a stamped one on name match alone) has the same
    tri-state fingerprint contract and the same pre-existing None-handling
    bug, fixed alongside the merge-route fix above."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.music = Path(self.tmp.name).resolve() / "music"
        self.music.mkdir()
        self.patches = [
            mock.patch.object(app_module, "MUSIC_ROOT", self.music),
            mock.patch.object(app_module, "_MUSIC_LIBRARY_ROOT", str(self.music)),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _stamped_and_plain_folders(self):
        stamped = self.music / "Bob Marley (9a70dc00-46ed-4b1b-a415-a4fb6dcb4d0f)"
        plain = self.music / "Bob Marley"
        stamped.mkdir()
        plain.mkdir()
        (stamped / "track.mp3").write_bytes(b"fake-audio-stamped")
        (plain / "track.mp3").write_bytes(b"fake-audio-plain")
        return stamped, plain

    def test_plain_duplicate_not_a_candidate_when_fingerprint_unavailable(self):
        stamped, plain = self._stamped_and_plain_folders()
        with mock.patch.object(app_module, "_stamp_artist_folder_album_mbid_counts", return_value=({}, {}, "")), \
             mock.patch.object(app_module, "_mb_artist_lookup_by_id", return_value={"name": "Bob Marley"}), \
             mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=None) as fp:
            result = app_module._stamp_artist_folder_scan(self.music)
        self.assertTrue(fp.called)
        candidate_paths = {c["path"] for c in result["candidates"]}
        self.assertNotIn(str(plain), candidate_paths)
        skip_reasons = " ".join(s["reason"] for s in result["skipped"])
        self.assertIn("no AcoustID fingerprint evidence was available", skip_reasons)

    def test_plain_duplicate_is_a_candidate_when_fingerprint_confirms(self):
        stamped, plain = self._stamped_and_plain_folders()
        with mock.patch.object(app_module, "_stamp_artist_folder_album_mbid_counts", return_value=({}, {}, "")), \
             mock.patch.object(app_module, "_mb_artist_lookup_by_id", return_value={"name": "Bob Marley"}), \
             mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=True) as fp:
            result = app_module._stamp_artist_folder_scan(self.music)
        self.assertTrue(fp.called)
        candidate_paths = {c["path"] for c in result["candidates"]}
        self.assertIn(str(plain), candidate_paths)


if __name__ == "__main__":
    unittest.main()
