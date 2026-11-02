"""ARCH-030 Step-3 (integrations) regressions:

(i)   two retries of the SAME recovery reuse ONE deterministic staged folder
      instead of minting a fresh ``_beets_missing_import/<hash>`` family;
(ii)  the deterministic staging prefix stays inside the validated downloads
      root and sanitises/rejects hostile identity input (SEC-007/#325
      containment preserved);
(iv)  /api/health/detail surfaces a REJECTED AI key as unhealthy via
      provider_health while the env-presence ``checks`` bool stays separate;
(v)   item ai-suggest with ai_contributed=False emits a structured ai_status
      and no AI-attributed confidence.
"""
import os
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import app as app_module
import backend.provider_boundary as provider_boundary
import backend.slskd as slskd
import routes_library
from backend import item_ai_suggest_service

AUDIO = {".flac"}
_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}
# a 64-char hex stand-in for a recovery identity key -> "rec-<16hex>" prefix
IDENT = "ab" * 32
PREFIX = "rec-" + IDENT[:16]


class DeterministicStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.downloads = self.root / "downloads"
        self.stage_root = self.downloads / "_beets_missing_import"
        self.downloads.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def _source(self, n):
        src = self.downloads / "slskd" / f"peer{n}" / "Album"
        src.mkdir(parents=True)
        files = []
        for name in ("06 last song.flac",):
            f = src / name
            f.write_bytes(b"synthetic-audio")
            files.append(f)
        return src, files

    def _families(self):
        return sorted(p.name for p in self.stage_root.iterdir()) if self.stage_root.exists() else []

    def _stage(self, src, files, prefix):
        return slskd.stage_selected_audio_files(
            self.downloads, AUDIO, str(src), files, "Artist", "Album", [],
            stage_prefix=prefix,
            target_tracks=[{"track": 6, "title": "Last Song"}])

    def test_two_retries_reuse_one_deterministic_folder(self):
        # Attempt 1 and attempt 2 of the SAME recovery (same identity prefix) use
        # different source downloads but must stage into ONE folder.
        src1, files1 = self._source(1)
        staged1 = self._stage(src1, files1, PREFIX)
        src2, files2 = self._source(2)
        staged2 = self._stage(src2, files2, PREFIX)
        self.assertEqual(staged1, staged2)
        self.assertEqual(len(self._families()), 1)          # no second family
        self.assertTrue(Path(staged1).name.startswith(PREFIX + "-"))
        # the re-staged track REPLACED the prior copy (no name.1 sibling)
        self.assertEqual(sorted(p.name for p in Path(staged1).rglob("*.flac")),
                         ["06 Last Song.flac"])

    def test_distinct_recoveries_get_distinct_folders(self):
        src1, files1 = self._source(1)
        src2, files2 = self._source(2)
        self._stage(src1, files1, PREFIX)
        self._stage(src2, files2, "rec-" + ("cd" * 8))
        self.assertEqual(len(self._families()), 2)

    def test_random_prefix_when_none(self):
        src1, files1 = self._source(1)
        src2, files2 = self._source(2)
        a = self._stage(src1, files1, None)
        b = self._stage(src2, files2, None)
        self.assertNotEqual(a, b)                            # uuid each time
        self.assertEqual(len(self._families()), 2)

    def test_hostile_prefix_is_sanitised_and_contained(self):
        src, files = self._source(1)
        staged = self._stage(src, files, "../../../etc/evil\x00boom")
        staged_resolved = Path(staged).resolve()
        root_resolved = self.stage_root.resolve()
        self.assertTrue(staged_resolved.is_relative_to(root_resolved))
        self.assertNotIn("..", Path(staged).relative_to(self.stage_root).parts)

    def test_preplanted_symlink_at_deterministic_name_is_rejected(self):
        # A predictable staged name lets an attacker pre-create a symlink that
        # redirects the copy out of the root; containment must reject it.
        self.stage_root.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        link = self.stage_root / f"{PREFIX}-Artist - Album"
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted on this platform")
        src, files = self._source(1)
        with self.assertRaises(RuntimeError):
            self._stage(src, files, PREFIX)
        # nothing was copied through the symlink into the outside directory
        self.assertEqual(list(outside.rglob("*.flac")), [])


class HealthProviderValidityTests(unittest.TestCase):
    def setUp(self):
        provider_boundary.reset_provider_health()
        self.addCleanup(provider_boundary.reset_provider_health)
        p = mock.patch.object(app_module, "_security_auth_disabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_rejected_ai_key_is_unhealthy_but_presence_stays_separate(self):
        # Simulate a present-but-rejected AI key: env present (configured) yet
        # the boundary classified a 401 (invalid).
        provider_boundary._record("ai", provider_boundary.ProviderOutcome.AUTHENTICATION_ERROR,
                                  attempts=1, status_code=401)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-present-but-bad"}):
            with mock.patch.object(app_module, "OPENAI_API_KEY", "sk-present-but-bad", create=True):
                resp = app_module.app.test_client().get("/api/health/detail")
        body = resp.get_json()
        # env-PRESENCE (configured) is reported True and separate ...
        self.assertTrue(body["checks"]["openai_key"])
        # ... while provider_health (VALIDITY) shows the rejection.
        ai = body["provider_health"]["ai"]
        self.assertEqual(ai["last_outcome"], "authentication_error")
        self.assertEqual(ai["last_status_code"], 401)
        self.assertGreaterEqual(ai["failures"], 1)


class AiSuggestStatusTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(app_module, "_security_auth_disabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _fake_item(self):
        return types.SimpleNamespace(
            title="Some Song", artist="Some Artist", album="", albumartist="",
            year=2020, genre="", track=1, label="",
            mb_trackid="", mb_albumid="", mb_releasegroupid="",
            path="", length=200.0)

    def test_rejected_key_emits_ai_status_and_no_ai_confidence(self):
        http401 = urllib.error.HTTPError("https://api.openai.test", 401, "Unauthorized", {}, None)
        with mock.patch.object(routes_library.lib, "get_item", return_value=self._fake_item()), \
             mock.patch.object(item_ai_suggest_service, "_ai_api_key", return_value="sk-present-but-bad"), \
             mock.patch.object(item_ai_suggest_service, "_mb_recording_search", return_value=[]), \
             mock.patch.object(item_ai_suggest_service, "_discogs_track_search", return_value=[]), \
             mock.patch.object(routes_library.provider_boundary, "opened", side_effect=http401):
            resp = app_module.app.test_client().post("/api/items/7/ai-suggest", headers=_CSRF)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        s = body["suggestions"]
        status = s["ai_status"]
        self.assertTrue(status["configured"])          # key was present
        self.assertFalse(status["contributed"])         # but AI did not answer
        self.assertFalse(status["available"])
        self.assertIn("rejected the API key", status["unavailable_reason"])
        # the confidence shown is NOT attributed to AI
        self.assertNotEqual(s.get("confidence_source"), "ai")


if __name__ == "__main__":
    unittest.main()
