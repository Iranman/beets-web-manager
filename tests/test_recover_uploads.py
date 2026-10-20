"""Recover My Uploads (increment 1) tests.

No real credentials, no network, no live retrieval: the yt-dlp module is faked
and the SSRF/allowlist guard's DNS resolution is monkeypatched. These prove the
guard still rejects bad targets on the new entry, that URL and creator targets
are built through the unchanged guard, cookie-state messages, the integrity
gate, and the error taxonomy (a format/HLS/auth failure is never labelled DRM).
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  (boots the app family + registers routes)
from backend import recover_service
from backend.security import OutboundPolicyError


# ── a fake yt-dlp that records how it was called, never touches the network ──
class _FakeYDL:
    instances = []

    def __init__(self, opts):
        self.opts = opts
        self.extract_calls = []
        self.download_calls = []
        _FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        self.extract_calls.append((url, download))
        return dict(_FakeYDL.info)

    def download(self, targets):
        self.download_calls.append(list(targets))
        # Simulate a successful download by writing a file into the outtmpl dir.
        outtmpl = self.opts.get("outtmpl") or ""
        directory = outtmpl.split("%", 1)[0].replace("%%", "%")
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "recovered.opus").write_bytes(b"\x00" * 1024)


def _fake_module():
    return type("yt_dlp", (), {"YoutubeDL": _FakeYDL})


def _allow_public(monkey_target="backend.ytdlp_guard.resolve_public_target"):
    # Allowlisted hosts pass the public-IP check without real DNS.
    return mock.patch(monkey_target, lambda url: None)


class GuardStillRejectsOnRecoverEntry(unittest.TestCase):
    def test_non_allowlisted_host_rejected_in_probe(self):
        res = recover_service.recover_probe("https://attacker.example/watch?v=x",
                                            ydl_module=_fake_module())
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("category"), "misconfig")

    def test_loopback_host_rejected_in_enumerate(self):
        res = recover_service.recover_enumerate("http://127.0.0.1/list",
                                                ydl_module=_fake_module())
        self.assertFalse(res["ok"])

    def test_fetch_rejects_bad_url_before_job(self):
        body, status = recover_service.start_recover_fetch(
            {"urls": ["https://attacker.example/x", "file:///etc/passwd"]})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])

    def test_allowlisted_host_still_blocked_when_private_ip(self):
        # The SSRF guard remains intact: an allowlisted host that resolves to a
        # private address is still refused on the new entry.
        def _refuse(url):
            raise OutboundPolicyError("resolves to a private address")

        with mock.patch("backend.ytdlp_guard.resolve_public_target", _refuse):
            res = recover_service.recover_probe("https://youtube.com/watch?v=x",
                                                ydl_module=_fake_module())
        self.assertFalse(res["ok"])


class TargetsBuiltThroughGuard(unittest.TestCase):
    def setUp(self):
        _FakeYDL.instances = []
        _FakeYDL.info = {
            "title": "My Upload", "uploader": "Me", "duration": 200,
            "webpage_url": "https://www.youtube.com/watch?v=abc",
            "formats": [{"format_id": "251", "ext": "webm", "acodec": "opus",
                         "vcodec": "none", "abr": 160}],
        }

    def test_probe_passes_guarded_opts_and_single_url_target(self):
        with _allow_public():
            res = recover_service.recover_probe("https://www.youtube.com/watch?v=abc",
                                                ydl_module=_fake_module())
        self.assertTrue(res["ok"])
        self.assertEqual(res["platform"], "youtube")
        self.assertEqual(res["quality"], "lossy")
        self.assertFalse(res["lossless"])
        ydl = _FakeYDL.instances[-1]
        # Guard applied: generic extractor disabled.
        self.assertEqual(ydl.opts.get("allowed_extractors"), ["default", "-generic"])
        self.assertEqual(ydl.extract_calls[-1], ("https://www.youtube.com/watch?v=abc", False))

    def test_enumerate_builds_flat_listing_and_filters_allowlist(self):
        _FakeYDL.info = {
            "uploader": "Me",
            "entries": [
                {"webpage_url": "https://www.youtube.com/watch?v=1", "title": "A", "duration": 100},
                {"url": "https://attacker.example/evil", "title": "bad"},
                {"webpage_url": "https://soundcloud.com/me/track", "title": "B"},
            ],
        }
        with _allow_public():
            res = recover_service.recover_enumerate("https://www.youtube.com/@me",
                                                    ydl_module=_fake_module())
        self.assertTrue(res["ok"])
        urls = [i["url"] for i in res["items"]]
        self.assertIn("https://www.youtube.com/watch?v=1", urls)
        self.assertIn("https://soundcloud.com/me/track", urls)
        self.assertNotIn("https://attacker.example/evil", urls)
        ydl = _FakeYDL.instances[-1]
        self.assertEqual(ydl.opts.get("extract_flat"), "in_playlist")

    def test_soundcloud_original_detected_in_plan(self):
        plan = recover_service._recover_format_plan(
            "soundcloud.com",
            {"formats": [{"format_id": "download", "format_note": "Original"}]})
        self.assertEqual(plan["quality"], "original")
        self.assertTrue(plan["lossless"])
        self.assertIn("download", plan["format"])

    def test_fetch_one_builds_url_target_and_stages(self):
        _FakeYDL.instances = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(recover_service, "_recover_stage_dir", return_value=tmp), \
                 mock.patch.object(recover_service, "_require_ytdlp_js_runtime", return_value={}), \
                 mock.patch.object(recover_service, "_probe_file_duration", return_value=200.0), \
                 _allow_public():
                log = []
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", log,
                    job_id="J1", ydl_module=_fake_module())
            # Assert while the temp dir still exists.
            self.assertTrue(out["recovered"], out)
            self.assertEqual(out["quality"], "lossy")
            staged = Path(out["staged_file"])
            self.assertTrue(staged.exists())
            prov = Path(out["provenance"])
            self.assertTrue(prov.exists())
            text = prov.read_text(encoding="utf-8")
            self.assertIn("source_platform", text)
            self.assertIn("youtube", text)
            # The actual download target was the URL itself, guard-checked.
            dl = [i for i in _FakeYDL.instances if i.download_calls][-1]
            self.assertEqual(dl.download_calls[-1], ["https://www.youtube.com/watch?v=abc"])
            self.assertEqual(dl.opts.get("allowed_extractors"), ["default", "-generic"])
            # No transcode postprocessor: original container kept.
            self.assertEqual(dl.opts.get("postprocessors"), [])
            self.assertTrue(dl.opts.get("continuedl"))


class CookieStateMessages(unittest.TestCase):
    def _state(self, auths, env_path, rejection=None, smoke=None):
        p = mock.patch
        with p.object(recover_service, "_configured_ytdlp_cookie_auths", return_value=auths), \
             p.object(recover_service, "_env_cookie_path", return_value=env_path), \
             p.object(recover_service, "_ytdlp_cookie_rejection_state", return_value=rejection), \
             p.object(recover_service, "_ytdlp_auth_smoke_check", return_value=(smoke or {})):
            return recover_service.recover_cookie_state()

    def test_no_cookies_configured(self):
        st = self._state([], "")
        self.assertEqual(st["state"], "none")
        self.assertFalse(st["configured"])
        self.assertIn("No yt-dlp cookies", st["message"])

    def test_configured_but_unusable_file(self):
        st = self._state([], "/config/cookies.txt")
        self.assertEqual(st["state"], "invalid")
        self.assertTrue(st["configured"])

    def test_cookies_rejected(self):
        auth = {"mode": "file", "cookie_file": "/config/cookies.txt", "label": "cookie file"}
        st = self._state([auth], "/config/cookies.txt", rejection={"rejected_at": 1})
        self.assertEqual(st["state"], "rejected")
        self.assertIn("refresh", st["action"].lower())

    def test_cookies_ok(self):
        auth = {"mode": "file", "cookie_file": "/config/cookies.txt", "label": "cookie file"}
        st = self._state([auth], "/config/cookies.txt", rejection=None, smoke={"ok": True})
        self.assertEqual(st["state"], "ok")

    def test_cookies_loaded_none(self):
        auth = {"mode": "file", "cookie_file": "/config/cookies.txt", "label": "cookie file"}
        st = self._state([auth], "/config/cookies.txt", rejection=None,
                         smoke={"ok": False, "rejected": False})
        self.assertEqual(st["state"], "invalid")

    def test_signature_never_includes_contents(self):
        auth = {"mode": "file", "cookie_file": "/config/cookies.txt", "label": "cookie file"}
        st = self._state([auth], "/config/cookies.txt", rejection={"rejected_at": 1})
        self.assertIsNotNone(st["signature"])
        self.assertEqual(set(st["signature"].keys()), {"cookie_file", "size", "mtime_ns"})


class IntegrityGate(unittest.TestCase):
    def _file(self, tmp, name="a.opus", data=b"\x00" * 2048):
        p = Path(tmp) / name
        p.write_bytes(data)
        return str(p)

    def test_part_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp, "a.opus.part")
            res = recover_service.recover_integrity_check(f, 200.0)
        self.assertFalse(res["ok"])
        self.assertIn("partial", res["reason"])

    def test_zero_byte_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp, "a.opus", b"")
            res = recover_service.recover_integrity_check(f, 200.0)
        self.assertFalse(res["ok"])

    def test_short_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp)
            with mock.patch.object(recover_service, "_probe_file_duration", return_value=30.0):
                res = recover_service.recover_integrity_check(f, 300.0)
        self.assertFalse(res["ok"])
        self.assertIn("duration", res["reason"])

    def test_undecodable_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp)
            with mock.patch.object(recover_service, "_probe_file_duration", return_value=None):
                res = recover_service.recover_integrity_check(f, 300.0)
        self.assertFalse(res["ok"])
        self.assertIn("decode", res["reason"])

    def test_matching_duration_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp)
            with mock.patch.object(recover_service, "_probe_file_duration", return_value=298.0):
                res = recover_service.recover_integrity_check(f, 300.0)
        self.assertTrue(res["ok"])
        self.assertTrue(res["duration_verified"])

    def test_unknown_expected_passes_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp)
            with mock.patch.object(recover_service, "_probe_file_duration", return_value=120.0):
                res = recover_service.recover_integrity_check(f, None)
        self.assertTrue(res["ok"])
        self.assertFalse(res["duration_verified"])


class ErrorTaxonomy(unittest.TestCase):
    def _cat(self, text):
        return recover_service.classify_recover_error(text)["category"]

    def test_auth_needed(self):
        self.assertEqual(self._cat("ERROR: Sign in to confirm you're not a bot. Use --cookies"), "auth-needed")
        self.assertEqual(self._cat("Private video. Sign in if you've been granted access"), "auth-needed")

    def test_rate_limited(self):
        self.assertEqual(self._cat("HTTP Error 429: Too Many Requests"), "rate-limited")

    def test_expired_url(self):
        self.assertEqual(self._cat("This video is no longer available"), "expired-url")

    def test_real_drm_marked(self):
        self.assertEqual(self._cat("This video is DRM protected (Widevine)"), "genuinely-DRM")

    def test_format_failure_not_drm(self):
        cat = self._cat("Requested format is not available")
        self.assertEqual(cat, "format")
        self.assertNotEqual(cat, "genuinely-DRM")

    def test_hls_failure_not_drm(self):
        cat = self._cat("Error opening .m3u8 fragment for HLS stream; drm-ish text")
        self.assertEqual(cat, "hls-assembly")
        self.assertNotEqual(cat, "genuinely-DRM")

    def test_auth_failure_with_drm_substring_not_drm(self):
        # An auth failure that merely contains 'drm' must not be labelled DRM.
        cat = self._cat("Sign in to confirm you're not a bot; use --cookies (drm)")
        self.assertEqual(cat, "auth-needed")
        self.assertNotEqual(cat, "genuinely-DRM")

    def test_message_is_sanitized_string(self):
        info = recover_service.classify_recover_error("boom")
        self.assertIn("category", info)
        self.assertIn("action", info)
        self.assertIsInstance(info["message"], str)


if __name__ == "__main__":
    unittest.main()
