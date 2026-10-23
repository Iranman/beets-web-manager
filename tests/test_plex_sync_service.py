"""Unit tests for backend.plex_sync_service: POSIX-ACL readability
classification, album-folder naming verdicts, filesystem walk aggregation,
beets dir join, and the opt-in heal application. All filesystem access is
crafted or confined to temporary directories; no network is touched."""

import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WEB_MANAGER_DATA_DIR", "/tmp/bwm-test-data-plex-sync")

from backend import plex_sync_service as svc  # noqa: E402


def encode_acl(named_groups: dict, group_obj=None, mask=None) -> bytes:
    wire = struct.pack("<H", 0x0001)
    if group_obj is not None:
        wire += struct.pack("<HHI", 0x04, group_obj, 0xFFFFFFFF)
    for gid, perm in sorted(named_groups.items()):
        wire += struct.pack("<HHI", 0x08, perm, gid)
    if mask is not None:
        wire += struct.pack("<HHI", 0x10, mask, 0xFFFFFFFF)
    return wire


def fake_stat_result(mode: int, gid: int) -> os.stat_result:
    now = 1700000000.0
    return os.stat_result((mode, 0, 0, 1, 0, gid, 128, now, now, now))


class AclDecodeTests(unittest.TestCase):
    def test_named_group_and_mask_round_trip(self):
        acl = svc._decode_posix_acl(encode_acl({568: 0o6}, group_obj=0o0, mask=0o7))
        self.assertEqual(acl["named_groups"], {568: 0o6})
        self.assertEqual(acl["group_obj"], 0o0)
        self.assertEqual(acl["mask"], 0o7)

    def test_bad_version_and_short_data_return_none(self):
        self.assertIsNone(svc._decode_posix_acl(b"\x02\x00" + b"\x00" * 10))
        self.assertIsNone(svc._decode_posix_acl(b""))
        self.assertIsNone(svc._decode_posix_acl(None))

    def test_extra_trailing_garbage_is_ignored(self):
        raw = encode_acl({568: 0o6}, group_obj=0o4, mask=0o6) + b"\x00"
        acl = svc._decode_posix_acl(raw)
        self.assertEqual(acl["named_groups"], {568: 0o6})


class ReadabilityTests(unittest.TestCase):
    """The kernel's judgment for a process whose primary group is Plex's."""

    def test_slskd_shape_mask_hides_named_entry(self):
        st = fake_stat_result(0o100700, 0)
        acl = svc._decode_posix_acl(encode_acl({568: 0o6}, group_obj=0o0, mask=0o0))
        readable, why = svc._file_readable_for_gid(st, acl, 568, is_dir=False)
        self.assertFalse(readable)
        self.assertEqual(why, "posixacl_named_group_denied")
        # named entry HAS read; the zero mask hides it → chmod repairs.
        plan = svc._repair_plan(st, acl, 568, is_dir=False)
        self.assertEqual(plan["status"], "repairable")
        self.assertEqual(plan["chmod"], 0o100700 | stat.S_IRGRP)
        self.assertNotIn("chown_gid", plan)

    def test_named_entry_without_read_is_blocked(self):
        st = fake_stat_result(0o100700, 0)
        acl = svc._decode_posix_acl(encode_acl({568: 0o2}, group_obj=0o0, mask=0o7))
        plan = svc._repair_plan(st, acl, 568, is_dir=False)
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("setfacl", plan["reason"])

    def test_acl_without_entry_for_plex_group_is_blocked(self):
        st = fake_stat_result(0o100700, 0)
        acl = svc._decode_posix_acl(encode_acl({}, group_obj=0o0, mask=0o7))
        readable, why = svc._file_readable_for_gid(st, acl, 568, is_dir=False)
        self.assertFalse(readable)
        plan = svc._repair_plan(st, acl, 568, is_dir=False)
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("setfacl", plan["reason"])

    def test_plain_mode_wrong_group_is_chown_repairable(self):
        st = fake_stat_result(0o100700, 0)
        plan = svc._repair_plan(st, None, 568, is_dir=False)
        self.assertEqual(plan["status"], "repairable")
        self.assertEqual(plan["chown_gid"], 568)
        self.assertEqual(plan["chmod"], 0o100700 | stat.S_IRGRP)

    def test_plain_mode_right_group_read_is_fine(self):
        st = fake_stat_result(0o100644, 568)
        readable, _ = svc._file_readable_for_gid(st, None, 568, is_dir=False)
        self.assertTrue(readable)

    def test_dir_needs_traverse_bit(self):
        st = fake_stat_result(0o040700, 568)
        readable, why = svc._file_readable_for_gid(st, None, 568, is_dir=True)
        self.assertFalse(readable)
        self.assertEqual(why, "group_bits_missing")
        plan = svc._repair_plan(st, None, 568, is_dir=True)
        # Exactly the kernel's need is added: group read + traverse.
        self.assertEqual(plan["chmod"], 0o040750)

    def test_acl_directory_named_group_with_mask_is_ok(self):
        st = fake_stat_result(0o040750, 0)
        acl = svc._decode_posix_acl(encode_acl({568: 0o7}, group_obj=0o5, mask=0o7))
        readable, _ = svc._file_readable_for_gid(st, acl, 568, is_dir=True)
        self.assertTrue(readable)


class NamingClassificationTests(unittest.TestCase):
    def test_literal_album_mbid_token(self):
        result = svc._classify_dir_name("WWCD (2019){Album MbId}")
        self.assertEqual(result["status"], "literal_token")
        self.assertEqual(result["guid_suffix"], "")

    def test_unresolved_template_token(self):
        result = svc._classify_dir_name("B Sides $album Collection")
        self.assertEqual(result["status"], "unresolved_token")

    def test_malformed_release_group_stamp(self):
        result = svc._classify_dir_name(
            "X (2011) {61adf521-7f5d-48e9-a0b0-2b1f6ecd2c52,}")
        self.assertEqual(result["status"], "malformed_stamp")

    def test_real_guid_suffix_is_ok(self):
        guid = "1d497ca3-a316-4a0b-8363-96a3ab2d0aaf"
        result = svc._classify_dir_name("WWCD (2019) {%s}" % guid)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["guid_suffix"], guid)

    def test_plain_name_is_ok(self):
        result = svc._classify_dir_name("WWCD (2019)")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["guid_suffix"], "")

    def test_severity_helper(self):
        self.assertEqual(svc._worse_name_status("ok", "literal_token"), "literal_token")
        self.assertEqual(svc._worse_name_status("literal_token", "unresolved_token"),
                         "literal_token")


class TempTreeTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "music"
        self.artist = self.root / "Artist (f7e3b0b4-2c1d-4a2b-9c3d-111122223333)"
        self.addCleanup(mock.patch.object(svc, "MUSIC_ROOT", self.root).start or None)
        self._patch = mock.patch.object(svc, "MUSIC_ROOT", self.root)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.root.mkdir(parents=True)
        self.artist.mkdir()

    def add_album(self, name: str, tracks: int = 2) -> Path:
        album = self.artist / name
        album.mkdir()
        for index in range(1, tracks + 1):
            (album / ("Track %02d.flac" % index)).write_bytes(b"fLaC" + b"\x00" * 8)
        return album


class FilesystemFindingsTests(TempTreeTestBase):
    """Walk aggregation with crafted stat/ACL answers (host-agnostic)."""

    def setUp(self):
        super().setUp()
        self._stat_patch = mock.patch.object(svc, "_stat", self._fake_stat)
        self._stat_patch.start()
        self.addCleanup(self._stat_patch.stop)
        self._getx_patch = mock.patch.object(svc, "_getxattr", self._fake_getxattr)
        self._getx_patch.start()
        self.addCleanup(self._getx_patch.stop)
        self._gid_patch = mock.patch.object(svc, "PLEX_FS_GID", 568)
        self._gid_patch.start()
        self.addCleanup(self._gid_patch.stop)
        self.crafted: dict = {}

    def _fake_stat(self, path):
        key = str(path)
        if key in self.crafted:
            return fake_stat_result(*self.crafted[key])
        path_obj = Path(key)
        if not path_obj.exists():
            return None
        if path_obj.is_dir():
            return fake_stat_result(0o040750, 568)
        return fake_stat_result(0o100644, 568)

    def _fake_getxattr(self, path, attr):
        return None

    def test_counts_and_issues_are_aggregated(self):
        album = self.add_album("Album (2001) {Album MbId}", tracks=2)
        self.crafted[str(album / "Track 01.flac")] = (0o100700, 0)
        findings = svc.filesystem_findings()
        totals = findings["totals"]
        self.assertEqual(totals["artist_dirs"], 1)
        self.assertEqual(totals["album_dirs"], 1)
        self.assertEqual(totals["audio_files"], 2)
        self.assertEqual(totals["literal_token_dirs"], 1)
        self.assertEqual(totals["unreadable_files"], 1)
        # Both sampled files get judged: one crafted-unreadable, one readable.
        self.assertEqual(totals["files_checked"], 2)
        self.assertEqual(totals["repairable_paths"], 1)
        artist = findings["artists"][0]
        self.assertEqual(artist["issues"]["folder_naming"], [album.name])
        album_entry = artist["albums"][0]
        self.assertEqual(album_entry["status"], "literal_token")
        self.assertEqual(album_entry["unreadable_files"], 1)
        self.assertEqual(album_entry["repairs"][0]["file"], "Track 01.flac")
        self.assertEqual(album_entry["repairs"][0]["plan"]["chown_gid"], 568)

    def test_readable_tree_has_no_repairs(self):
        self.add_album("Album (2002) {11111111-2222-4333-8444-555566667777}", tracks=1)
        findings = svc.filesystem_findings()
        totals = findings["totals"]
        self.assertEqual(totals["unreadable_files"], 0)
        self.assertEqual(totals["repairable_paths"], 0)
        self.assertEqual(totals["guid_suffix_dirs"], 1)
        self.assertEqual(findings["artists"][0]["issues"], {})

    def test_sampled_files_are_capped(self):
        album = self.add_album("Album (2003) {Album MbId}", tracks=4)
        for index in range(1, 5):
            self.crafted[str(album / ("Track %02d.flac" % index))] = (0o100700, 0)
        findings = svc.filesystem_findings(sample_limit=2)
        self.assertEqual(findings["totals"]["files_checked"], 2)
        self.assertEqual(findings["totals"]["unreadable_files"], 2)
        findings_full = svc.filesystem_findings(sample_limit=None)
        self.assertEqual(findings_full["totals"]["files_checked"], 4)


class BeetsAlbumDirJoinTests(unittest.TestCase):
    def test_dirs_grouped_and_unsafe_relative_skipped(self):
        items = [
            {"path": "/m/Artist/Album A/t1.flac", "album": "Album A",
             "albumartist": "Artist", "year": 2001,
             "mb_releasegroupid": "1d497ca3-a316-4a0b-8363-96a3ab2d0aaf"},
            {"path": "/m/artist/album A/t2.flac", "album": "Album A",
             "albumartist": "Artist", "year": 2001, "mb_releasegroupid": ""},
            {"path": "Album B/t1.flac", "album": "Album B", "albumartist": "X",
             "year": 0, "mb_releasegroupid": ""},
        ]
        with mock.patch.object(svc.beets_adapter, "get_items", return_value=items):
            by_dir = svc._beets_album_dirs()
        self.assertIn("/m/artist/album a", by_dir)
        row = by_dir["/m/artist/album a"]
        self.assertEqual(row["mb_releasegroupid"],
                         "1d497ca3-a316-4a0b-8363-96a3ab2d0aaf")
        self.assertNotIn("album b", by_dir)


class PlexPresenceTests(unittest.TestCase):
    def _presence(self):
        return {
            "settings": {"url": "http://plex:32400", "token": "t"},
            "path_exact": {"/data/music/Artist/Album (2001) {abc}/t1.flac": "rk1"},
            "path_case": {"/data/music/artist/album (2001) {abc}/t1.flac": "rk1"},
            "suffix_paths": {"artist/album (2001) {abc}/t1.flac": {"rk1"}},
            "tracks_by_key": {"rk1": {"artist": "Artist", "album": "Album (2001)"}},
            "roots": ["/data/music"],
            "fetched": 1,
            "section_title": "Music",
            "section_key": "3",
        }

    def test_exact_suffix_and_case_paths_match(self):
        presence = self._presence()
        with mock.patch.object(svc.plex_service, "_plex_beets_path_candidates",
                               return_value=["/data/music/Artist/Album (2001) {abc}/t1.flac"]):
            self.assertTrue(svc._path_in_plex("/music/Artist/Album (2001) {abc}/t1.flac", presence))

    def test_unknown_path_is_absent(self):
        presence = self._presence()
        with mock.patch.object(svc.plex_service, "_plex_beets_path_candidates",
                               return_value=["/data/music/Nope/none.flac"]), \
             mock.patch.object(svc.plex_service, "_plex_norm_path",
                               return_value="/data/music/Nope/none.flac"), \
             mock.patch.object(svc.plex_service, "_plex_path_case_key",
                               return_value="/data/music/nope/none.flac"), \
             mock.patch.object(svc.plex_service, "_plex_suffix_keys_for_path",
                               return_value=set()):
            self.assertFalse(svc._path_in_plex("/music/Nope/none.flac", presence))


class HealTests(TempTreeTestBase):
    def setUp(self):
        super().setUp()
        self._gid_patch = mock.patch.object(svc, "PLEX_FS_GID", 568)
        self._gid_patch.start()
        self.addCleanup(self._gid_patch.stop)
        self.calls = {"chmod": [], "chown": [], "utime": []}
        self._orig_stat = svc._stat
        self._chmod_patch = mock.patch.object(
            svc, "_chmod", lambda path, mode: self.calls["chmod"].append((str(path), mode)))
        self._chmod_patch.start()
        self.addCleanup(self._chmod_patch.stop)
        self._chown_patch = mock.patch.object(
            svc, "_chown", lambda path, uid, gid: self.calls["chown"].append((str(path), gid)))
        self._chown_patch.start()
        self.addCleanup(self._chown_patch.stop)
        self._utime_patch = mock.patch.object(
            svc, "_utime", lambda path: self.calls["utime"].append(str(path)))
        self._utime_patch.start()
        self.addCleanup(self._utime_patch.stop)
        self._stat_patch = mock.patch.object(svc, "_stat", self._fake_stat)
        self._stat_patch.start()
        self.addCleanup(self._stat_patch.stop)
        self.crafted: dict = {}

    def _fake_stat(self, path):
        key = str(path)
        if key in self.crafted:
            return fake_stat_result(*self.crafted[key])
        st = self._orig_stat(path)
        return st

    def _fake_acl_free(self, path, attr):
        return None

    def test_all_disabled_returns_note_without_mutation(self):
        album = self.add_album("Album (2001) {Album MbId}", tracks=1)
        self.crafted[str(album / "Track 01.flac")] = (0o100700, 0)
        results = svc.apply_plex_sync_heal(log=[], allow_chmod=False,
                                           allow_touch=False, allow_refresh=False)
        self.assertEqual(results.get("note"), "no repair actions enabled")
        self.assertEqual(results["chmod_applied"], 0)
        self.assertEqual(self.calls["chmod"], [])

    def test_heal_repairs_chown_chmod_and_touches_dirs(self):
        album = self.add_album("Album (2001) {Album MbId}", tracks=2)
        self.crafted[str(album / "Track 01.flac")] = (0o100700, 0)
        self.crafted[str(album / "Track 02.flac")] = (0o100700, 0)
        self.crafted[str(album)] = (0o040750, 568)
        self.crafted[str(album.parent)] = (0o040750, 568)
        log = []
        with mock.patch.object(svc, "_getxattr", lambda path, attr: None):
            results = svc.apply_plex_sync_heal(log, allow_chmod=True,
                                               allow_touch=True, allow_refresh=False)
        self.assertEqual(results["chown_applied"], 2)
        self.assertEqual(results["chmod_applied"], 2)
        self.assertEqual(results["dirs_touched"], 1)
        self.assertIn(str(album), self.calls["utime"])
        self.assertIn(str(album.parent), self.calls["utime"])
        self.assertEqual(results["refresh"], False)
        self.assertTrue(any("heal executed" in line for line in log))

    def test_heal_regroups_unowned_directories_too(self):
        album = self.add_album("Album (2005) {Album MbId}", tracks=1)
        self.crafted[str(album)] = (0o040700, 0)
        self.crafted[str(album.parent)] = (0o040750, 568)
        self.crafted[str(album / "Track 01.flac")] = (0o100644, 568)
        with mock.patch.object(svc, "_getxattr", lambda path, attr: None):
            results = svc.apply_plex_sync_heal(log=[], allow_chmod=True,
                                               allow_touch=True, allow_refresh=False)
        self.assertEqual(results["chmod_applied"], 1)
        self.assertEqual(results["chown_applied"], 1)
        self.assertIn((str(album), 568), self.calls["chown"])
        self.assertEqual(results["dirs_touched"], 1)

    def test_blocked_counts_and_no_chmod(self):
        album = self.add_album("Album (2001) {Album MbId}", tracks=1)
        self.crafted[str(album / "Track 01.flac")] = (0o100700, 0)
        acl_bytes = encode_acl({568: 0o2}, group_obj=0o0, mask=0o7)
        with mock.patch.object(svc, "_getxattr", lambda path, attr: acl_bytes):
            results = svc.apply_plex_sync_heal(log=[], allow_chmod=True,
                                               allow_touch=True, allow_refresh=False)
        self.assertEqual(results["chmod_applied"], 0)
        # The fake ACL covers every path: artist dir + album dir + file, and
        # none of the named entries grants what Plex needs.
        self.assertEqual(results["blocked_paths"], 3)

    def test_refresh_gated_by_repair_and_flag(self):
        album = self.add_album("Album (2001) {Album MbId}", tracks=1)
        self.crafted[str(album / "Track 01.flac")] = (0o100700, 0)
        log = []
        with mock.patch.object(svc, "_getxattr", lambda path, attr: None), \
             mock.patch.object(svc.plex_service, "_trigger_plex_refresh",
                               return_value=True) as refresh:
            results = svc.apply_plex_sync_heal(log, allow_chmod=True,
                                               allow_touch=True, allow_refresh=True)
        self.assertTrue(results["refresh"])
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs.get("workflow"), "sync")

    def test_no_repairs_means_no_refresh(self):
        album = self.add_album("Album (2002) {Album MbId}", tracks=1)
        self.crafted[str(album)] = (0o040750, 568)
        self.crafted[str(album.parent)] = (0o040750, 568)
        self.crafted[str(album / "Track 01.flac")] = (0o100644, 568)
        with mock.patch.object(svc.plex_service, "_trigger_plex_refresh",
                               return_value=True) as refresh:
            results = svc.apply_plex_sync_heal(log=[], allow_chmod=True,
                                               allow_touch=True, allow_refresh=True)
        self.assertFalse(results["refresh"])
        refresh.assert_not_called()


class JobEntriesTests(unittest.TestCase):
    def test_audit_job_calls_audit_and_wraps_report(self):
        with mock.patch.object(svc, "audit_plex_sync",
                               return_value={"filesystem": {"totals": {}}}) as audit:
            log = []
            result = svc.plex_sync_audit_job(log)
        audit.assert_called_once_with(log)
        self.assertEqual(result["report"]["filesystem"]["totals"], {})

    def test_heal_job_runs_apply_with_defaults(self):
        with mock.patch.object(svc, "apply_plex_sync_heal",
                               return_value={"note": "no repair actions enabled"}) as heal:
            log = []
            result = svc.plex_sync_heal_job(log)
        heal.assert_called_once_with(log)
        self.assertEqual(result["heal"]["note"], "no repair actions enabled")


if __name__ == "__main__":
    unittest.main()
