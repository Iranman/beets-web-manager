"""Recover My Uploads (increment 1) tests.

No real credentials, no network, no live retrieval: the yt-dlp module is faked
and the SSRF/allowlist guard's DNS resolution is monkeypatched. These prove the
guard still rejects bad targets on the new entry, that URL and creator targets
are built through the unchanged guard, cookie-state messages, the integrity
gate, and the error taxonomy (a format/HLS/auth failure is never labelled DRM).
"""
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  (boots the app family + registers routes)
from backend import recover_service
from backend.security import OutboundPolicyError


def _outtmpl_dir(opts):
    return (opts.get("outtmpl") or "").split("%", 1)[0].replace("%%", "%")


# ── a fake yt-dlp that records how it was called, never touches the network ──
class _FakeYDL:
    instances = []
    filename = "recovered.opus"
    content = b"\x00" * 4096

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
        directory = _outtmpl_dir(self.opts)
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / _FakeYDL.filename).write_bytes(_FakeYDL.content)


def _fake_module():
    return type("yt_dlp", (), {"YoutubeDL": _FakeYDL})


def _make_fake(info=None, filename="recovered.opus", content=b"\x00" * 4096):
    """A self-contained fake yt-dlp module (no shared class state), for
    concurrency and isolation tests."""
    info = info or {
        "title": "My Upload", "uploader": "Me", "duration": 200,
        "webpage_url": "https://www.youtube.com/watch?v=abc", "formats": [],
    }

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return dict(info)

        def download(self, targets):
            directory = _outtmpl_dir(self.opts)
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / filename).write_bytes(content)

    return type("yt_dlp", (), {"YoutubeDL": FakeYDL})


def _allow_public(monkey_target="backend.ytdlp_guard.resolve_public_target"):
    # Allowlisted hosts pass the public-IP check without real DNS.
    return mock.patch(monkey_target, lambda url: None)


@contextlib.contextmanager
def _fetch_patches(stage_dir, duration=200.0, codec="opus"):
    """Patch the fetch seams so a fake download stages into an isolated dir
    without a network, a JS runtime, or a real ffprobe."""
    with mock.patch.object(recover_service, "_recover_stage_dir", return_value=stage_dir), \
         mock.patch.object(recover_service, "_require_ytdlp_js_runtime", return_value={}), \
         mock.patch.object(recover_service, "_probe_file_duration", return_value=duration), \
         mock.patch.object(recover_service, "inspect_audio_file", return_value={"ok": True, "codec": codec}), \
         _allow_public():
        yield


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
            {"urls": ["https://attacker.example/x", "file:///etc/passwd"],
             "authorization": {"basis": "original_creation", "note": "mine"}})
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

    def test_host_classification_is_dot_bounded(self):
        # Lookalike hosts must never be classified as a real platform
        # (py/incomplete-url-substring-sanitization). The allowlist guard
        # rejects them too, but classification must not be fooled either.
        self.assertTrue(recover_service._is_youtube_host("music.youtube.com"))
        self.assertTrue(recover_service._is_youtube_host("youtube.com"))
        self.assertFalse(recover_service._is_youtube_host("evilyoutube.com"))
        self.assertFalse(recover_service._is_youtube_host("youtube.com.attacker.net"))
        self.assertTrue(recover_service._is_soundcloud_host("m.soundcloud.com"))
        self.assertFalse(recover_service._is_soundcloud_host("notsoundcloud.com"))
        self.assertEqual(recover_service._platform_for_host("evilyoutube.com"), "other")

    def test_soundcloud_original_lossless_only_for_lossless_codec(self):
        # F4: an owner-enabled original is "original" quality, but lossless ONLY
        # when its codec/ext is lossless. An original MP3/AAC stays lossy.
        flac = recover_service._recover_format_plan(
            "soundcloud.com",
            {"formats": [{"format_id": "download", "format_note": "Original", "ext": "flac", "acodec": "flac"}]})
        self.assertEqual(flac["quality"], "original")
        self.assertTrue(flac["lossless"])

        mp3 = recover_service._recover_format_plan(
            "soundcloud.com",
            {"formats": [{"format_id": "download", "format_note": "Original", "ext": "mp3", "acodec": "mp3"}]})
        self.assertEqual(mp3["quality"], "original")
        self.assertFalse(mp3["lossless"])

        aac = recover_service._recover_format_plan(
            "soundcloud.com",
            {"formats": [{"format_id": "download", "format_note": "Original", "ext": "m4a", "acodec": "aac"}]})
        self.assertFalse(aac["lossless"])

        stream = recover_service._recover_format_plan(
            "soundcloud.com", {"formats": [{"format_id": "http_mp3_128", "ext": "mp3"}]})
        self.assertEqual(stream["quality"], "stream")
        self.assertFalse(stream["lossless"])
        self.assertIn("download", flac["format"])

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
                    run_id="J1", ydl_module=_fake_module())
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

    def test_unsupported_scheme_not_drm(self):
        # "scheme" contains the substring "eme"; a generic protocol error must
        # not be mislabeled as DRM (word-boundary EME match only).
        cat = self._cat('ERROR: Unsupported URL scheme "file"')
        self.assertNotEqual(cat, "genuinely-DRM")

    def test_standalone_eme_still_marked_drm(self):
        self.assertEqual(self._cat("Protected content: EME required"), "genuinely-DRM")

    # ── F1: the specific strings QA found, each to its correct NON-DRM class ──
    def test_401_entitlement_is_auth_not_drm(self):
        cat = self._cat("HTTP Error 401: Unauthorized; entitlement required")
        self.assertEqual(cat, "auth-needed")
        self.assertNotEqual(cat, "genuinely-DRM")

    def test_manifest_parse_is_hls_not_drm(self):
        cat = self._cat("Element not found while parsing the manifest")
        self.assertEqual(cat, "hls-assembly")
        self.assertNotEqual(cat, "genuinely-DRM")

    def test_eme_substring_words_are_not_drm(self):
        # "placEMEnt", "requirEMEnt", "agrEEMEnt", "ElEMEnt" all contain "eme"
        # as a substring but not as a standalone token; none is DRM.
        for text in (
            "codec placement failed",
            "requirement not satisfied",
            "agreement with the server",
            "Element not found",
        ):
            self.assertNotEqual(self._cat(text), "genuinely-DRM", text)

    def test_genuine_drm_evidence_maps_to_drm(self):
        for text in (
            "This content is protected by Widevine",
            "FairPlay streaming is required",
            "PlayReady license required",
            "This stream uses EME",
            "encrypted media extensions are required to play this",
        ):
            self.assertEqual(self._cat(text), "genuinely-DRM", text)

    def test_message_is_sanitized_string(self):
        info = recover_service.classify_recover_error("boom")
        self.assertIn("category", info)
        self.assertIn("action", info)
        self.assertIsInstance(info["message"], str)


class SingleItemFetchRejectsCollections(unittest.TestCase):
    """F3: a channel/playlist/set URL is refused in the single-item fetch path
    (enumerate first), so no un-gated child is staged."""

    _PLAYLIST = {"_type": "playlist", "title": "Chan", "uploader": "Me",
                 "entries": [{"webpage_url": "https://www.youtube.com/watch?v=1"},
                             {"webpage_url": "https://www.youtube.com/watch?v=2"}]}

    def test_probe_flags_collection(self):
        with _allow_public():
            res = recover_service.recover_probe(
                "https://www.youtube.com/@me", ydl_module=_make_fake(info=self._PLAYLIST))
        self.assertTrue(res["ok"])
        self.assertTrue(res["is_collection"])
        self.assertEqual(res["entry_count"], 2)

    def test_fetch_one_refuses_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/@me", [], run_id="r",
                    ydl_module=_make_fake(info=self._PLAYLIST))
            self.assertFalse(out["recovered"])
            self.assertEqual(out["category"], "collection")
            # Nothing was staged.
            self.assertEqual(list(Path(tmp).glob("*")), [])


class ProvenanceFailureFailsItem(unittest.TestCase):
    """F11: if provenance cannot be persisted the item FAILS and the orphan
    staged file is removed."""

    def test_provenance_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp), \
                 mock.patch.object(recover_service, "_write_provenance", return_value=""):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", [], run_id="r",
                    ydl_module=_make_fake())
            self.assertFalse(out["recovered"])
            self.assertIn("provenance", out["message"].lower())
            # The orphan was removed — no audio left behind.
            self.assertEqual([p for p in Path(tmp).iterdir() if p.suffix == ".opus"], [])


class AggregateStatus(unittest.TestCase):
    """F12: a job where every item failed reports ok=False; a partial stays
    ok=True with partial=True and per-item counts."""

    def test_all_failed_reports_not_ok(self):
        res = recover_service.recover_fetch_job(
            ["https://attacker.example/x"], [], run_id="r")
        self.assertFalse(res["ok"])
        self.assertEqual(res["recovered_count"], 0)
        self.assertEqual(res["failed_count"], 1)

    def test_partial_is_ok_but_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp):
                res = recover_service.recover_fetch_job(
                    ["https://www.youtube.com/watch?v=abc", "https://attacker.example/x"],
                    [], run_id="r", ydl_module=_make_fake())
        self.assertTrue(res["ok"])
        self.assertTrue(res["partial"])
        self.assertEqual(res["recovered_count"], 1)
        self.assertEqual(res["failed_count"], 1)


class OutputDiscoveryExtensions(unittest.TestCase):
    """F10: a bare .aac original is discovered, not reported 'no audio produced'."""

    def test_aac_original_is_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp, codec="aac"):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", [], run_id="r",
                    ydl_module=_make_fake(filename="original.aac"))
            self.assertTrue(out["recovered"], out)
            self.assertTrue(out["staged_file"].endswith(".aac"))
            self.assertFalse(out["lossless"])  # aac is lossy


class ConcurrentJobsAreIsolated(unittest.TestCase):
    """F8: two concurrent recover-fetch runs on the same platform stage into
    separate run dirs and never select/delete/mislabel each other's files."""

    def test_two_concurrent_runs_do_not_interfere(self):
        with tempfile.TemporaryDirectory() as tmp:
            def stage_for(platform, run_id):
                return os.path.join(tmp, platform, run_id)

            results = {}

            def worker(rid, fname):
                fake = _make_fake(filename=fname)
                results[rid] = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", [], run_id=rid, ydl_module=fake)

            with mock.patch.object(recover_service, "_recover_stage_dir", side_effect=stage_for), \
                 mock.patch.object(recover_service, "_require_ytdlp_js_runtime", return_value={}), \
                 mock.patch.object(recover_service, "_probe_file_duration", return_value=200.0), \
                 mock.patch.object(recover_service, "inspect_audio_file", return_value={"ok": True, "codec": "opus"}), \
                 _allow_public():
                t1 = threading.Thread(target=worker, args=("runA", "a.opus"))
                t2 = threading.Thread(target=worker, args=("runB", "b.opus"))
                t1.start(); t2.start(); t1.join(); t2.join()

            self.assertTrue(results["runA"]["recovered"])
            self.assertTrue(results["runB"]["recovered"])
            dir_a = Path(results["runA"]["staged_file"]).parent
            dir_b = Path(results["runB"]["staged_file"]).parent
            self.assertNotEqual(dir_a, dir_b)
            # Each run dir holds only its own audio file.
            self.assertEqual([p.name for p in dir_a.glob("*.opus")], ["a.opus"])
            self.assertEqual([p.name for p in dir_b.glob("*.opus")], ["b.opus"])


class RetryFindsExistingStagedFile(unittest.TestCase):
    """F5/F13: a crash between download and provenance is retried safely — the
    isolated dir is re-scanned and the finished file is validated, not reported
    'no audio produced'."""

    def test_existing_file_validated_on_download_error(self):
        class _CrashFake:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return {"title": "T", "uploader": "Me", "duration": 200,
                        "webpage_url": url, "formats": []}

            def download(self, targets):
                raise RuntimeError("network blip after the file was already written")

        crash_module = type("yt_dlp", (), {"YoutubeDL": _CrashFake})
        with tempfile.TemporaryDirectory() as tmp:
            # A prior crashed run left a complete file in this run's dir.
            (Path(tmp) / "prior.opus").write_bytes(b"\x00" * 4096)
            with _fetch_patches(tmp):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", [], run_id="r",
                    ydl_module=crash_module)
            self.assertTrue(out["recovered"], out)
            self.assertTrue(out["staged_file"].endswith("prior.opus"))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe required for real-media integrity test")
class IntegrityGateRealMedia(unittest.TestCase):
    """F9/F7: the integrity gate is exercised against REAL ffmpeg-generated
    media (not a mocked duration), including a sub-2s-style truncation that the
    old 2.0s floor would have let through."""

    def _gen(self, path, duration):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={duration}",
             "-c:a", "pcm_s16le", str(path)],
            check=True, capture_output=True, timeout=60)

    def test_healthy_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "ok.wav"
            self._gen(f, 5)
            res = recover_service.recover_integrity_check(str(f), 5.0)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["duration_verified"])

    def test_truncated_file_rejected(self):
        # 1.2 s of real audio claimed to be 3.0 s: the old max(_, 2.0 s) floor
        # allowed a 1.8 s gap; the tightened band rejects it.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "short.wav"
            self._gen(f, 1.2)
            res = recover_service.recover_integrity_check(str(f), 3.0)
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["duration_verified"])

    def test_garbage_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "garbage.opus"
            f.write_bytes(b"not audio at all" * 64)
            res = recover_service.recover_integrity_check(str(f), 5.0)
        self.assertFalse(res["ok"])
        self.assertIn("decode", res["reason"])


class StreamingFormatAndManifestDiagnostics(unittest.TestCase):
    """Phase 2 (1-3, 6): streaming-format detection, HLS/DASH/SABR manifest
    diagnostics, DRM/EME detection, and highest-quality non-DRM selection."""

    def test_hls_dash_sabr_and_progressive_manifest_diagnostics(self):
        info = {
            "formats": [
                {
                    "format_id": "hls-audio-128",
                    "protocol": "m3u8_native",
                    "manifest_url": "https://cf-hls-media.sndcdn.com/playlist.m3u8",
                    "ext": "m4a",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "abr": 128,
                    "asr": 44100,
                    "fragments": [{"url": "seg1.ts"}, {"url": "seg2.ts"}],
                },
                {
                    "format_id": "dash-opus-160",
                    "protocol": "http_dash_segments",
                    "manifest_url": "https://manifest.googlevideo.com/api/manifest/dash/id/123.mpd",
                    "ext": "webm",
                    "acodec": "opus",
                    "vcodec": "none",
                    "abr": 160,
                    "asr": 48000,
                },
                {
                    "format_id": "251",
                    "protocol": "https",
                    "ext": "webm",
                    "acodec": "opus",
                    "vcodec": "none",
                    "abr": 160,
                    "asr": 48000,
                },
            ]
        }
        diag = recover_service.inspect_stream_diagnostics(info, platform="youtube")
        self.assertTrue(diag["has_hls"])
        self.assertTrue(diag["has_dash"])
        self.assertTrue(diag["has_progressive"])
        self.assertEqual(diag["fragmented_format_count"], 2)
        self.assertEqual(diag["drm_status"], "none")
        self.assertFalse(diag["eme_detected"])
        self.assertEqual(diag["accessible_audio_format_count"], 3)
        # Progressive 160k opus beats DASH 160k opus due to transport rank.
        self.assertEqual(diag["best_audio_format"]["format_id"], "251")
        self.assertEqual(diag["best_audio_format"]["transport"], "progressive")

    def test_partial_vs_protected_drm_detection_and_key_systems(self):
        partial_info = {
            "formats": [
                {
                    "format_id": "drm-wv",
                    "protocol": "http_dash_segments",
                    "ext": "m4a",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "has_drm": True,
                    "drm": "widevine",
                    "abr": 256,
                },
                {
                    "format_id": "140",
                    "protocol": "https",
                    "ext": "m4a",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "abr": 128,
                },
            ]
        }
        partial_diag = recover_service.inspect_stream_diagnostics(partial_info, platform="youtube")
        self.assertEqual(partial_diag["drm_status"], "partial")
        self.assertTrue(partial_diag["eme_detected"])
        self.assertIn("widevine", partial_diag["key_systems"])
        self.assertEqual(partial_diag["accessible_audio_format_count"], 1)
        self.assertEqual(partial_diag["best_audio_format"]["format_id"], "140")

        protected_info = {
            "formats": [
                {
                    "format_id": "drm-only",
                    "protocol": "http_dash_segments",
                    "ext": "m4a",
                    "acodec": "mp4a.40.2",
                    "has_drm": True,
                    "drm": "com.widevine.alpha",
                }
            ]
        }
        prot_diag = recover_service.inspect_stream_diagnostics(protected_info, platform="youtube")
        self.assertEqual(prot_diag["drm_status"], "protected")
        self.assertTrue(prot_diag["eme_detected"])
        self.assertEqual(prot_diag["accessible_audio_format_count"], 0)
        self.assertIsNone(prot_diag["best_audio_format"])

    def test_fetch_one_refuses_genuinely_drm_protected_stream_before_download(self):
        drm_info = {
            "title": "Protected Track",
            "uploader": "Me",
            "duration": 180,
            "webpage_url": "https://www.youtube.com/watch?v=drm1",
            "formats": [
                {
                    "format_id": "wv-1",
                    "protocol": "http_dash_segments",
                    "ext": "m4a",
                    "acodec": "mp4a.40.2",
                    "has_drm": True,
                    "drm": "widevine",
                }
            ],
        }
        fake_mod = _make_fake(info=drm_info)
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=drm1", [], run_id="r_drm",
                    ydl_module=fake_mod)
        self.assertFalse(out["recovered"])
        self.assertEqual(out["category"], "genuinely-DRM")
        self.assertEqual(out["provenance_status"], "genuinely-DRM")


class ProviderAuthAndProvenanceDiagnostics(unittest.TestCase):
    """Phase 2 (4-5) & Phase 3: provider auth diagnostics and 7-state provenance."""

    def test_auth_diagnostics_never_expose_cookie_contents(self):
        cookie_state = {
            "state": "ok",
            "configured": True,
            "signature": "size=512:mtime=1700000000",
            "message": "Cookies file present and passed smoke check.",
        }
        diag = recover_service.recover_auth_diagnostics(
            info={"availability": "private"},
            error_text="",
            cookie_info=cookie_state,
        )
        self.assertEqual(diag["cookie_state"], "ok")
        self.assertTrue(diag["authenticated_session_likely"])
        self.assertTrue(diag["auth_required_by_target"])
        self.assertEqual(diag["session_status"], "ready")
        serialized = str(diag)
        self.assertNotIn("SAPISID", serialized)
        self.assertNotIn("oauth_token", serialized)

    def test_provenance_distinguishes_all_required_states(self):
        # 1. accessible
        p_acc = recover_service.classify_media_provenance(
            probe={"ok": True, "stream_diagnostics": {"drm_status": "none", "total_format_count": 2, "accessible_audio_format_count": 2}},
        )
        self.assertEqual(p_acc, "accessible")

        # 2. auth-needed
        p_auth = recover_service.classify_media_provenance(
            error_category="auth-needed",
        )
        self.assertEqual(p_auth, "auth-needed")

        # 3. unsupported-format
        p_unsup = recover_service.classify_media_provenance(
            probe={"ok": True, "stream_diagnostics": {"drm_status": "none", "total_format_count": 2, "accessible_audio_format_count": 0}},
        )
        self.assertEqual(p_unsup, "unsupported-format")

        # 4. genuinely-DRM
        p_drm = recover_service.classify_media_provenance(
            probe={"ok": True, "stream_diagnostics": {"drm_status": "protected", "total_format_count": 1, "accessible_audio_format_count": 0}},
        )
        self.assertEqual(p_drm, "genuinely-DRM")

        # 5. unavailable
        p_unavail = recover_service.classify_media_provenance(
            error_category="expired-url",
        )
        self.assertEqual(p_unavail, "unavailable")

        # 6. recovered
        p_rec = recover_service.classify_media_provenance(staged=True)
        self.assertEqual(p_rec, "recovered")

        # 7. imported
        p_imp = recover_service.classify_media_provenance(staged=True, imported=True)
        self.assertEqual(p_imp, "imported")


class Phase3ResearchDiagnosticCompatibility(unittest.TestCase):
    """Phase 3: consume diagnostic reports from Streaming Security Lab while
    stripping all secret keys, license payloads, PEM blocks, and cookies, and
    never treating ClearKey interception as Widevine decryptability."""

    def test_sanitize_diagnostic_report_strips_keys_licenses_and_cookies(self):
        raw_lab_report = {
            "source_url": "https://www.youtube.com/watch?v=drm_test",
            "source_platform": "youtube",
            "key_systems": ["org.w3.clearkey", "com.widevine.alpha"],
            "session_types": ["temporary"],
            "message_types": ["license-request", "license-release"],
            "key_statuses": ["usable", "expired", "malicious-status-injected"],
            # Forbidden fields that MUST be stripped:
            "recovered_keys": [
                {"kid": "00112233445566778899aabbccddeeff", "k": "ffeeddccbbaa99887766554433221100"}
            ],
            "content_key": "deadbeefcafebabe0011223344556677",
            "license_response": "eyJrZXlzIjpbXX0=",
            "pssh": "AAAANHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7Q==",
            "cookies": "SID=secret_cookie_value; SAPISID=secret_sapisid",
            # Non-secret sentinel: sanitize_diagnostic_report redacts this by
            # FIELD NAME ("private_key"), so the fixture needs no key-shaped
            # value (and none belongs in the repo / its secret scanner).
            "private_key": "SENTINEL_private_key_value_not_a_real_key",
            "note": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret",
        }
        sanitized = recover_service.sanitize_diagnostic_report(raw_lab_report)
        self.assertTrue(sanitized["ok"])
        self.assertEqual(sanitized["schema"], "beets.recover.diagnostic.v1")
        self.assertFalse(sanitized["decryption_supported"])
        self.assertFalse(sanitized["widevine_decryptable"])
        self.assertTrue(len(sanitized["redacted_fields"]) > 0)
        # Canonical ids only (raw key-system strings are never echoed back).
        self.assertIn("clearkey", sanitized["key_systems"])
        self.assertIn("widevine", sanitized["key_systems"])
        self.assertNotIn("org.w3.clearkey", sanitized["key_systems"])

        self.assertEqual(sanitized["key_statuses"], ["expired", "usable"])
        self.assertEqual(sanitized["provenance_status"], "genuinely-DRM")

        dumped = str(sanitized)
        self.assertNotIn("00112233445566778899aabbccddeeff", dumped)
        self.assertNotIn("ffeeddccbbaa99887766554433221100", dumped)
        self.assertNotIn("deadbeefcafebabe0011223344556677", dumped)
        self.assertNotIn("eyJrZXlzIjpbXX0=", dumped)
        self.assertNotIn("secret_cookie_value", dumped)
        self.assertNotIn("SENTINEL_private_key_value_not_a_real_key", dumped)
        # private_key is redacted under the fixed "key_material" category (F3a).
        self.assertIn("key_material", sanitized["redacted_fields"])
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", dumped)

    def test_sanitizer_does_not_leak_event_keysystem_or_url_tokens_and_tolerates_bad_types(self):
        # F-3: a secret in an event keySystem must NOT be echoed into key_systems.
        o = recover_service.sanitize_diagnostic_report({
            "events": [{"keySystem": "bearer eyJsecret_in_event_leak"}],
            "key_system": "com.widevine.alpha",
        })
        self.assertEqual(o["key_systems"], ["widevine"])
        self.assertNotIn("bearer", str(o["key_systems"]).lower())
        # F-5: a URL query token must be stripped from the echoed source_url.
        o2 = recover_service.sanitize_diagnostic_report(
            {"source_url": "https://x.com/watch?v=1&access_token=LEAKED_TOKEN123"})
        self.assertNotIn("LEAKED_TOKEN123", str(o2))
        # F-9: malformed field types must not crash the sanitizer.
        for bad in ({"key_systems": 1}, {"message_types": 1}, {"session_types": 5},
                    {"key_statuses": True}, {"events": [{"statuses": True}]},
                    {"formats": 1}, {"drm_families": True}):  # F3b: also inside inspect_stream_diagnostics
            self.assertTrue(recover_service.sanitize_diagnostic_report(bad)["ok"])
        # F3a: redacted_fields must be FIXED categories, never raw field names.
        o = recover_service.sanitize_diagnostic_report(
            {"secretKID_LEAKED_0123456789ABCDEF": "x", "private_key_SECRETNAME_DEADBEEF": "y"})
        self.assertNotIn("LEAKED", str(o))
        self.assertNotIn("SECRETNAME", str(o))
        self.assertTrue(set(o["redacted_fields"]) <= {
            "cookie", "license", "credential", "secret", "key_material",
            "inline_secret_value", "key_statuses.kid", "events.statuses.kid"}, o["redacted_fields"])

    def test_validator_rejects_hardlinked_staged_file(self):
        # Finding F-1 regression: a hard link under recovered/ (nlink>1) that could
        # point at a library file must be refused before any in-place tag write.
        with tempfile.TemporaryDirectory() as tmp:
            recovered = Path(tmp) / "recovered" / "yt" / "r1"
            recovered.mkdir(parents=True, exist_ok=True)
            target = recovered / "a.flac"
            target.write_bytes(b"\x00" * 4096)
            link = recovered / "b.flac"
            try:
                os.link(target, link)  # hard link -> nlink == 2
            except OSError:
                self.skipTest("hard links not supported on this filesystem")
            Path(str(link) + ".recover.json").write_text('{"source_url": "https://x/y"}', encoding="utf-8")
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp):
                resolved, sidecar, err = recover_service._validate_staged_recover_file(str(link))
            self.assertIsNone(resolved)
            self.assertIn("hard link", err)

    def test_diagnostics_route_returns_sanitized_report(self):
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "_security_auth_disabled", return_value=True):
            resp = client.post(
                "/api/recover/diagnostics",
                json={
                    "report": {
                        "source_platform": "soundcloud",
                        "formats": [{"format_id": "hls_mp3", "protocol": "m3u8_native", "acodec": "mp3"}],
                        "recovered_keys": [{"k": "secret_key_material_must_not_survive"}],
                    }
                },
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["decryption_supported"])
        self.assertFalse(data["widevine_decryptable"])
        self.assertEqual(data["provenance_status"], "accessible")
        self.assertNotIn("secret_key_material_must_not_survive", str(data))


class RecoverJobPersistenceAndRetries(unittest.TestCase):
    """Phase 2 (7): persistent recovery run history and retry of failed items."""

    def test_persist_list_and_retry_recover_run(self):
        from backend.web_manager_config_store import WebManagerConfigStore
        with tempfile.TemporaryDirectory() as tmp:
            fake_store = WebManagerConfigStore(data_root=Path(tmp))
            with mock.patch("backend.web_manager_config_store.get_config_store", return_value=fake_store):
                recover_service._upsert_recover_run({
                    "run_id": "run_101",
                    "job_id": "job_101",
                    "status": "partial",
                    "urls": [
                        "https://www.youtube.com/watch?v=ok1",
                        "https://www.youtube.com/watch?v=fail_auth",
                        "https://www.youtube.com/watch?v=drm_locked",
                    ],
                    "recovered": [
                        {
                            "url": "https://www.youtube.com/watch?v=ok1",
                            "recovered": True,
                            "provenance_status": "recovered",
                        },
                    ],
                    "failed": [
                        {
                            "url": "https://www.youtube.com/watch?v=fail_auth",
                            "recovered": False,
                            "category": "auth-needed",
                            "provenance_status": "auth-needed",
                        },
                        {
                            "url": "https://www.youtube.com/watch?v=drm_locked",
                            "recovered": False,
                            "category": "genuinely-DRM",
                            "provenance_status": "genuinely-DRM",
                        },
                    ],
                })
                listed = recover_service.recover_list_runs()
                self.assertEqual(listed["total"], 1)
                loaded = recover_service.recover_get_run("run_101")
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["run_id"], "run_101")

                # Retry should pick up fail_auth (and skip ok1 + drm_locked).
                with _allow_public(), \
                     mock.patch.object(recover_service, "start_recover_fetch",
                                       return_value=({"ok": True, "job_id": "job_retry_1", "run_id": "run_102"}, 200)) as mock_fetch:
                    retry_body, status = recover_service.start_recover_retry({"run_id": "run_101"})
                self.assertEqual(status, 200)
                self.assertTrue(retry_body["ok"])
                self.assertEqual(retry_body["retried_urls"], ["https://www.youtube.com/watch?v=fail_auth"])
                mock_fetch.assert_called_once()

    def test_runs_route_and_retry_route(self):
        from backend.web_manager_config_store import WebManagerConfigStore
        client = app_module.app.test_client()
        with tempfile.TemporaryDirectory() as tmp:
            fake_store = WebManagerConfigStore(data_root=Path(tmp))
            with mock.patch("backend.web_manager_config_store.get_config_store", return_value=fake_store), \
                 mock.patch.object(app_module, "_security_auth_disabled", return_value=True):
                recover_service._upsert_recover_run({
                    "run_id": "run_route_1",
                    "status": "failed",
                    "urls": ["https://soundcloud.com/me/private-track"],
                    "recovered": [],
                    "failed": [
                        {"url": "https://soundcloud.com/me/private-track", "recovered": False, "category": "auth-needed"}
                    ],
                })
                r_get = client.get("/api/recover/runs?run_id=run_route_1")
                self.assertEqual(r_get.status_code, 200)
                self.assertEqual(r_get.get_json()["run"]["run_id"], "run_route_1")

                with _allow_public(), \
                     mock.patch.object(recover_service, "start_recover_fetch",
                                       return_value=({"ok": True, "job_id": "j_retry", "run_id": "run_route_2"}, 200)):
                    r_retry = client.post(
                        "/api/recover/retry",
                        json={"run_id": "run_route_1"},
                    )
                self.assertEqual(r_retry.status_code, 200)
                rdata = r_retry.get_json()
                self.assertTrue(rdata["ok"])
                self.assertEqual(rdata["retried_urls"], ["https://soundcloud.com/me/private-track"])


class AudioVerificationFingerprintingAndMatching(unittest.TestCase):
    """Phase 2 (8): audio verification, AcoustID fingerprinting, MusicBrainz
    matching, and singleton fallback for original/unreleased uploads."""

    def setUp(self):
        # The staged sidecar carries the immutable channel_id "UCdjself000"; an
        # own_account claim is independently verified against this allowlist
        # (a stable id, NOT the mutable display name) so these identity-gate
        # tests reach the AcoustID/MusicBrainz logic (verified==True).
        patcher = mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCdjself000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stage_with_sidecar(self, tmp_dir: str, fname: str = "track.flac") -> str:
        stage_root = Path(tmp_dir) / "recovered"
        run_dir = stage_root / "youtube" / "run1"
        run_dir.mkdir(parents=True, exist_ok=True)
        audio_path = run_dir / fname
        audio_path.write_bytes(b"\x00" * 4096)
        sidecar = Path(str(audio_path) + ".recover.json")
        sidecar.write_text(
            '{"source_url": "https://www.youtube.com/watch?v=abc", '
            '"source_platform": "youtube", "uploader": "DJ Self", '
            '"channel_id": "UCdjself000", '
            '"title": "My Unreleased VIP", "duration": 200.0, '
            '"codec": "flac", "quality": "original"}',
            encoding="utf-8",
        )
        return str(audio_path)

    def test_acoustid_and_mb_match_selects_acoustid_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = self._stage_with_sidecar(tmp)
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "matched",
                     "acoustid_match_score": 0.96,
                     "mb_recording_id_candidate": "rec-123",
                     "mb_release_group_id_candidate": "rg-456",
                     "candidates": [{"artist": "DJ Self", "title": "My Unreleased VIP", "album": "Self Released EP"}],
                 }):
                res = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["match_source"], "acoustid")
        self.assertEqual(res["mb_trackid"], "rec-123")
        self.assertEqual(res["mb_releasegroupid"], "rg-456")
        self.assertEqual(res["album"], "Self Released EP")

    def test_unmatched_original_upload_uses_singleton_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = self._stage_with_sidecar(tmp)
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     # REAL no-candidate decision shape from acoustid_service: a
                     # genuinely-original upload returns conflicts=["no_acoustid_result"].
                     # (Finding 2 regression — the old gate rejected on ANY conflicts.)
                     "fingerprint_status": "no_result",
                     "acoustid_status": "no_result",
                     "metadata_agreement": "degraded",
                     "conflicts": ["no_acoustid_result"],
                     "acoustid_match_score": 0.0,
                 }), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]) as mock_mb_search:
                res = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["match_source"], "upload_metadata_singleton")
        self.assertEqual(res["mb_trackid"], "")
        # Verify _mb_recording_search is called with (title, artist, limit=5)
        mock_mb_search.assert_called_once_with("My Unreleased VIP", "DJ Self", limit=5)

    def test_acoustid_reject_and_review_decisions_block_singleton_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = self._stage_with_sidecar(tmp)
            # 1. acoustid mismatch / final_action="reject" despite high raw score
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "mismatch",
                     "identity_status": "conflict",
                     "final_action": "reject",
                     "acoustid_match_score": 0.95,
                     "mb_recording_id_candidate": "wrong-rec-999",
                     "conflicts": ["acoustid_artist_mismatch"],
                 }):
                res_reject = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
            self.assertFalse(res_reject["ok"])
            self.assertEqual(res_reject["status"], "identity_conflict")
            self.assertEqual(res_reject["final_action"], "reject")

            # 2. acoustid unavailable / final_action="review" blocks silent singleton fallback
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "unavailable",
                     "identity_status": "unverified",
                     "final_action": "review",
                     "acoustid_match_score": 0.0,
                 }):
                res_review = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
            self.assertFalse(res_review["ok"])
            self.assertEqual(res_review["status"], "review_required")
            self.assertEqual(res_review["final_action"], "review")

    def test_genuinely_original_upload_real_producer_shape_imports(self):
        # REAL _audio_identity_decision shape for a no-candidate original upload:
        # final_action="review" + acoustid_status="no_result" (see
        # acoustid_service._audio_identity_decision lines ~186-187/245-254). This
        # must fall through to the singleton fallback, NOT get parked in review.
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = self._stage_with_sidecar(tmp)
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "no_result",
                     "identity_status": "review_required",
                     "final_action": "review",
                     "conflicts": ["no_acoustid_result"],
                     "acoustid_match_score": 0.0,
                 }), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                res = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["match_source"], "upload_metadata_singleton")
        self.assertEqual(res["mb_trackid"], "")

    def test_provider_outage_routes_to_review_not_reject_or_import(self):
        # Could-not-verify (AcoustID lookup failed): acoustid_ -PREFIXED conflict
        # token + failed status. A provider outage must NOT auto-import unverified
        # audio and must NOT hard-reject as an identity conflict -> review.
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = self._stage_with_sidecar(tmp)
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "failed",
                     "identity_status": "review_required",
                     "final_action": "review",
                     "conflicts": ["acoustid_lookup_failed"],
                     "acoustid_match_score": 0.0,
                 }):
                res = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["status"], "review_required")
        self.assertNotEqual(res.get("match_source"), "upload_metadata_singleton")

    def test_acoustid_unconfigured_or_auth_failed_routes_to_review_not_reject(self):
        # The producer emits not_configured (default when ACOUSTID_API_KEY unset)
        # and auth_failed as could-not-verify. These must route to REVIEW, never a
        # false identity_conflict — otherwise a fresh install rejects every upload.
        for status in ("not_configured", "auth_failed"):
            with tempfile.TemporaryDirectory() as tmp:
                audio_path = self._stage_with_sidecar(tmp)
                with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                     mock.patch.object(recover_service, "recover_integrity_check",
                                       return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
                     mock.patch.object(recover_service, "inspect_audio_file",
                                       return_value={"ok": True, "codec": "flac"}), \
                     mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                         "acoustid_status": status,
                         "identity_status": "review_required",
                         "final_action": "review",
                         "conflicts": [f"acoustid_{status}"],
                         "acoustid_match_score": 0.0,
                     }):
                    res = recover_service.verify_and_match_staged_upload(audio_path, auth_basis="own_account")
            self.assertFalse(res["ok"], (status, res))
            self.assertEqual(res["status"], "review_required", (status, res))
            self.assertNotEqual(res.get("match_source"), "upload_metadata_singleton")


class SafeStagingAndBeetsImport(unittest.TestCase):
    """Phase 2 (9-10): safe staging validation, non-destructive Beets import,
    existing library file protection, and playlist coverage refresh."""

    def setUp(self):
        # Staged sidecars here carry the immutable channel_id "UCmine000";
        # own_account is verified against this allowlist (a stable id, NOT the
        # mutable display name) so the import path proceeds to the
        # existing-library / Beets-import logic under test (verified==True).
        patcher = mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCmine000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_import_rejects_paths_outside_staging_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.flac"
            outside.write_bytes(b"\x00" * 1024)
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=os.path.join(tmp, "downloads")):
                resolved, sidecar, err = recover_service._validate_staged_recover_file(str(outside))
                self.assertIsNone(resolved)
                self.assertIsNone(sidecar)
                self.assertIn("DOWNLOADS_ROOT/recovered", err)

    def test_import_never_overwrites_existing_library_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage_root = Path(tmp) / "recovered" / "youtube" / "run2"
            stage_root.mkdir(parents=True, exist_ok=True)
            staged = stage_root / "song.opus"
            staged.write_bytes(b"\x00" * 4096)
            Path(str(staged) + ".recover.json").write_text(
                '{"source_url": "https://www.youtube.com/watch?v=dup1", '
                '"source_platform": "youtube", "uploader": "Me", '
                '"channel_id": "UCmine000", '
                '"title": "Already In Library", "duration": 180.0}',
                encoding="utf-8",
            )
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = [
                {
                    "id": 42,
                    "title": "Already In Library",
                    "artist": "Me",
                    "length": 180.2,
                    "path": "/music/Me/Album/01 - Already In Library.opus",
                }
            ]
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 180.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "opus"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "no_match",
                     "acoustid_match_score": 0.0,
                 }), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter, auth_basis="own_account")

            self.assertFalse(out["imported"])
            self.assertEqual(out["status"], "skipped_existing")
            self.assertEqual(out["existing_item_id"], 42)
            # Crucial invariant: run_import was NEVER called and staged file remains untouched.
            fake_adapter.run_import.assert_not_called()
            self.assertTrue(staged.exists())

    def test_safe_singleton_import_copies_without_moving_and_updates_playlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage_root = Path(tmp) / "recovered" / "soundcloud" / "run3"
            stage_root.mkdir(parents=True, exist_ok=True)
            staged = stage_root / "my_track.flac"
            staged.write_bytes(b"\x00" * 4096)
            Path(str(staged) + ".recover.json").write_text(
                '{"source_url": "https://soundcloud.com/me/my-track", '
                '"source_platform": "soundcloud", "uploader": "DJ Me", '
                '"channel_id": "UCmine000", '
                '"title": "My Track", "duration": 210.0, '
                '"codec": "flac", "quality": "original"}',
                encoding="utf-8",
            )
            fake_adapter = mock.MagicMock()
            # Pre-import conflict check returns empty; post-import read-back returns item #99.
            fake_adapter.find_items_by_query.side_effect = [
                [],
                [{"id": 99, "title": "My Track", "artist": "DJ Me", "path": "/music/DJ Me/My Track.flac"}],
            ]
            fake_adapter.run_import.return_value = {
                "ok": True, "success": True, "returncode": 0, "stdout": "imported", "stderr": ""
            }

            # Finding 1 regression: inject a REAL TransactionStore so the import
            # must create a valid, audited transaction record. The old code called
            # transactions.create()/update_status() with a signature the real store
            # rejects (TypeError, swallowed) -> transaction_id stayed None.
            from backend.transaction_engine import TransactionStore
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch("backend.app_runtime.transactions", tx_store), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "fingerprint_status": "no_result",
                     "acoustid_status": "no_result",
                     "metadata_agreement": "degraded",
                     "conflicts": ["no_acoustid_result"],
                     "acoustid_match_score": 0.0,
                 }), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter, auth_basis="own_account")

            self.assertTrue(out["imported"], out)
            self.assertEqual(out["imported_item_id"], 99)
            self.assertEqual(out["status"], "imported")
            self.assertEqual(out["provenance_status"], "imported")
            # Audited: a transaction record was created and marked Completed.
            self.assertTrue(out.get("transaction_id"), "import must create a transaction record")
            tx_rec = tx_store._read(out["transaction_id"])
            self.assertEqual(tx_rec["operation_type"], "Import")
            self.assertEqual(tx_rec["status"], "Completed")
            # Verify copy=True, move=False, duplicate_action="skip", singletons=True,
            # autotag=False, quiet_fallback="skip", search_ids=None
            _, kwargs = fake_adapter.run_import.call_args
            self.assertTrue(kwargs["copy"])
            self.assertFalse(kwargs["move"])
            self.assertFalse(kwargs["autotag"])
            self.assertEqual(kwargs["quiet_fallback"], "skip")
            self.assertIsNone(kwargs["search_ids"])
            self.assertEqual(kwargs["duplicate_action"], "skip")
            self.assertTrue(kwargs["singletons"])
            # Staged file is preserved
            self.assertTrue(staged.exists())

            # Verify playlist coverage refresh matches playlist referencing recovered track
            pl_dir = Path(tmp) / "playlists"
            pl_dir.mkdir(parents=True, exist_ok=True)
            pl_manifest_path = pl_dir / "my-sc-uploads.playlist.json"
            pl_manifest_path.write_text('{"name": "My SC Uploads"}', encoding="utf-8")
            fake_manifest = {
                "name": "My SC Uploads",
                "desired_tracks": [
                    {"title": "My Track", "artist": "DJ Me", "source_url": "https://soundcloud.com/me/my-track"}
                ],
            }
            updated_manifest = {
                "name": "My SC Uploads",
                "desired_tracks": [
                    {"title": "My Track", "artist": "DJ Me", "source_url": "https://soundcloud.com/me/my-track", "state": "imported"}
                ],
            }
            with mock.patch.object(recover_service, "PLAYLIST_MANIFESTS_DIR", pl_dir), \
                 mock.patch("backend.matching_service._invalidate_lib_cache"), \
                 mock.patch("backend.playlist_service._playlist_ensure_state_dirs"), \
                 mock.patch("backend.playlist_service._playlist_manifest_name_from_file", return_value=("My SC Uploads", fake_manifest)), \
                 mock.patch("backend.playlist_service._playlist_find_track_row", return_value=(0, fake_manifest["desired_tracks"][0])), \
                 mock.patch("backend.playlist_service._playlist_store_track_state") as mock_store_state, \
                 mock.patch("backend.playlist_service._playlist_read_manifest", return_value=updated_manifest) as mock_read_man, \
                 mock.patch("backend.playlist_service._playlist_write_local_membership") as mock_write_mem:
                cov = recover_service.refresh_playlists_for_recovered_tracks([out])
            self.assertEqual(cov["playlists_updated"], ["My SC Uploads"])
            self.assertEqual(cov["tracks_covered"], 1)
            mock_store_state.assert_called_once()
            mock_read_man.assert_called_once_with("My SC Uploads")
            mock_write_mem.assert_called_once_with("My SC Uploads", updated_manifest)

    def test_skipped_import_and_missing_readback_are_not_marked_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage_root = Path(tmp) / "recovered" / "soundcloud" / "run4"
            stage_root.mkdir(parents=True, exist_ok=True)
            staged = stage_root / "skip_track.flac"
            staged.write_bytes(b"\x00" * 4096)
            Path(str(staged) + ".recover.json").write_text(
                '{"source_url": "https://soundcloud.com/me/skip-track", '
                '"source_platform": "soundcloud", "uploader": "DJ Me", '
                '"channel_id": "UCmine000", '
                '"title": "Skip Track", "duration": 210.0}',
                encoding="utf-8",
            )
            # Case A: Beets skips due to blank_identity -> status="import_failed", imported=False
            fake_adapter_skip = mock.MagicMock()
            fake_adapter_skip.find_items_by_query.return_value = []
            fake_adapter_skip.run_import.return_value = {
                "success": True,
                "skipped_paths": [str(staged)],
                "skipped": [{"path": str(staged), "reason": "blank_identity"}],
            }
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={"acoustid_status": "no_match"}), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out_skip = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter_skip, auth_basis="own_account")
            self.assertFalse(out_skip["imported"])
            self.assertEqual(out_skip["status"], "import_failed")
            self.assertIn("blank_identity", out_skip["error"])
            # F9: the non-duplicate Beets-skip outcome carries the authorization
            # record too, like every other outcome.
            self.assertIn("authorization", out_skip)
            self.assertEqual(out_skip["authorization"]["asserted_basis"], "own_account")

            # Case B: run_import succeeds without skip, but post-import readback finds nothing -> import_failed
            fake_adapter_noread = mock.MagicMock()
            fake_adapter_noread.find_items_by_query.return_value = []
            fake_adapter_noread.run_import.return_value = {"success": True, "skipped_paths": [], "skipped": []}
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={"acoustid_status": "no_match"}), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out_noread = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter_noread, auth_basis="own_account")
            self.assertFalse(out_noread["imported"])
            self.assertEqual(out_noread["status"], "import_failed")
            self.assertIn("without adding item to library", out_noread["error"])

    def test_persisted_run_and_provenance_strip_url_query_tokens_and_nested_secrets(self):
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "song.m4a"
            staged.write_bytes(b"\x00" * 128)
            sidecar_path = recover_service._write_provenance(str(staged), {
                "source_url": "https://user:pass@www.youtube.com/watch?v=abc123&token=SECRET_TOK&sig=DEAD",
                "title": "Clean Song",
                "nested": {"cookie": "SID=secret", "safe": "value"},
            })
            sidecar_text = Path(sidecar_path).read_text(encoding="utf-8")
            self.assertNotIn("SECRET_TOK", sidecar_text)
            self.assertNotIn("DEAD", sidecar_text)
            self.assertNotIn("user:pass", sidecar_text)
            self.assertNotIn("SID=secret", sidecar_text)
            self.assertIn("v=abc123", sidecar_text)

            clean_run = recover_service._sanitize_persisted_run({
                "run_id": "r_sec",
                "status": "failed",
                "urls": ["https://soundcloud.com/me/track?oauth_token=SC_SECRET&keep=1"],
                "failed": [
                    {
                        "url": "https://soundcloud.com/me/track?oauth_token=SC_SECRET",
                        "message": "Forbidden: Cookie: SAPISID=12345",
                        "secret_key": "deadbeef",
                    }
                ],
                "unexpected_top_level_key": "should_be_dropped",
            })
            serialized = _json.dumps(clean_run)
            self.assertNotIn("SC_SECRET", serialized)
            self.assertNotIn("SAPISID=12345", serialized)
            self.assertNotIn("deadbeef", serialized)
            self.assertNotIn("unexpected_top_level_key", clean_run)
            self.assertEqual(clean_run["urls"], ["https://soundcloud.com/me/track?keep=1"])

    def test_auto_import_failure_updates_run_status_and_retry_recovers_from_staging(self):
        from backend.web_manager_config_store import WebManagerConfigStore

        with tempfile.TemporaryDirectory() as tmp:
            fake_store = WebManagerConfigStore(data_root=Path(tmp) / "cfg")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            fake_adapter.run_import.return_value = {"ok": False, "error": "engine busy"}

            with _fetch_patches(tmp), \
                 mock.patch("backend.web_manager_config_store.get_config_store", return_value=fake_store), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={"acoustid_status": "no_match"}), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                summary = recover_service.recover_fetch_job(
                    ["https://www.youtube.com/watch?v=abc"],
                    [],
                    run_id="run_autoimp_fail",
                    ydl_module=_make_fake(),
                    auto_import=True,
                    adapter=fake_adapter,
                    auth_basis="own_account",
                )
                self.assertFalse(summary["ok"])
                self.assertTrue(summary["partial"])
                self.assertEqual(summary["import_failed_count"], 1)

                persisted = recover_service.recover_get_run("run_autoimp_fail")
                self.assertEqual(persisted["status"], "failed")
                self.assertEqual(persisted["import_failed_count"], 1)

                # Retry when all URLs were staged but import failed should retry from staging
                with mock.patch.object(
                    recover_service,
                    "start_recover_import",
                    return_value=({"ok": True, "job_id": "job_imp_retry", "run_id": "run_autoimp_fail"}, 200),
                ) as mock_imp:
                    retry_body, r_status = recover_service.start_recover_retry({"run_id": "run_autoimp_fail"})
                self.assertEqual(r_status, 200)
                self.assertTrue(retry_body["ok"])
                self.assertEqual(len(retry_body["retried_staged_files"]), 1)
                mock_imp.assert_called_once()


class AuthorizationAndProvenance(unittest.TestCase):
    """Authorization + provenance workflow: a required, validated asserted basis
    kept DISTINCT from independently verified evidence, gating import."""

    def _stage(self, tmp, *, platform="soundcloud", uploader="DJ Me",
               fname="t.flac", title="My Track",
               source_url="https://soundcloud.com/me/my-track",
               source_license="", channel_id="UCdjme000", uploader_url=""):
        import json as _json
        run_dir = Path(tmp) / "recovered" / platform / "run1"
        run_dir.mkdir(parents=True, exist_ok=True)
        audio = run_dir / fname
        audio.write_bytes(b"\x00" * 4096)
        Path(str(audio) + ".recover.json").write_text(
            _json.dumps({
                "source_url": source_url, "source_platform": platform,
                "uploader": uploader, "title": title, "duration": 200.0,
                "codec": "flac", "quality": "original",
                "source_license": source_license,
                # Immutable identifiers are the only own_account evidence.
                "channel_id": channel_id, "uploader_url": uploader_url,
            }),
            encoding="utf-8",
        )
        return str(audio)

    @contextlib.contextmanager
    def _import_patches(self, tmp, *, identity=None):
        identity = identity or {"acoustid_status": "no_result",
                                "conflicts": ["no_acoustid_result"],
                                "acoustid_match_score": 0.0}
        with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
             mock.patch.object(recover_service, "recover_integrity_check",
                               return_value={"ok": True, "duration": 200.0, "duration_verified": True}), \
             mock.patch.object(recover_service, "inspect_audio_file",
                               return_value={"ok": True, "codec": "flac"}), \
             mock.patch("backend.acoustid_service._audio_identity_decision", return_value=identity), \
             mock.patch("helpers_mb._mb_recording_search", return_value=[]):
            yield

    # ── secret-hygiene regressions (F3/F5) ──
    def test_credential_in_authorization_note_is_redacted(self):
        # F3: a Bearer/PEM/API-key pasted into the free-text note or evidence
        # must not persist verbatim in the sanitized record.
        rec = recover_service._sanitize_authorization_record({
            "asserted_basis": "licensed_permission",
            "asserted_note": "x Bearer abcdef0123456789ABCDEF0123456789 token",
            "verified": True, "verified_basis": "open_license",
            "verified_evidence": "Bearer ABCDEF0123456789abcdef0123 token",
        })
        self.assertNotIn("Bearer abcdef", rec["asserted_note"])
        self.assertEqual(rec["asserted_note"], "[REDACTED]")
        self.assertNotIn("Bearer ABCDEF0123456789", rec["verified_evidence"] or "")

    def test_license_url_signed_params_stripped_from_sidecar(self):
        # F5: a signed query param in the platform license_url must be sanitized.
        u = recover_service._sanitize_persisted_url(
            "https://cdn.example.com/lic?X-Amz-Signature=deadbeefSIGVALUE123&x=1")
        self.assertNotIn("deadbeefSIGVALUE123", u)
        self.assertNotIn("X-Amz-Signature", u)

    def test_signed_url_in_authorization_note_is_stripped(self):
        # F5: a signed URL pasted inside the free-text authorization note must
        # have its signature params stripped before persistence (the note runs
        # through _sanitize_persisted_url, not only the key=value redactor).
        rec = recover_service._sanitize_authorization_record({
            "asserted_basis": "licensed_permission",
            "asserted_note": "proof: https://cdn.example.com/lic?X-Amz-Signature=deadbeefSIGVALUE123&ok=1 thanks",
            "verified": True, "verified_basis": "own_account",
            "verified_evidence": "https://youtube.com/channel/UCx?X-Amz-Signature=SECRETSIGVALUE999",
        })
        self.assertNotIn("X-Amz-Signature", rec["asserted_note"])
        self.assertNotIn("deadbeefSIGVALUE123", rec["asserted_note"])
        # The non-signed remainder of the note survives.
        self.assertIn("cdn.example.com/lic", rec["asserted_note"])
        self.assertIn("thanks", rec["asserted_note"])
        self.assertNotIn("SECRETSIGVALUE999", rec["verified_evidence"] or "")

    def test_parenthesised_signed_url_in_note_is_stripped(self):
        # F5: the URL-shaped scrubber stops at "(", so a parenthesised URL left
        # the signature behind. The name=value redactor strips it regardless of
        # URL shape.
        rec = recover_service._sanitize_authorization_record({
            "asserted_basis": "licensed_permission",
            "asserted_note": "see https://cdn.example.com/lic(v2)?X-Amz-Signature=deadbeefSIGVAL123&ok=1 end",
            "verified": False,
        })
        self.assertNotIn("deadbeefSIGVAL123", rec["asserted_note"])
        self.assertNotIn("X-Amz-Signature=deadbeef", rec["asserted_note"])
        self.assertIn("end", rec["asserted_note"])

    def test_scheme_relative_signed_url_in_note_is_stripped(self):
        # F5: a scheme-relative "//host/..." URL has no scheme, so the http(s)
        # URL scrubber never matched it. The name=value redactor still strips the
        # signature.
        rec = recover_service._sanitize_authorization_record({
            "asserted_basis": "licensed_permission",
            "asserted_note": "proof //host/p?X-Amz-Signature=SCHEMELESSSIG777&a=b done",
            "verified": True, "verified_basis": "own_account",
            "verified_evidence": "//cdn/v?sig=EVIDENCESIG555&token=TKN888",
        })
        self.assertNotIn("SCHEMELESSSIG777", rec["asserted_note"])
        self.assertIn("done", rec["asserted_note"])
        self.assertNotIn("EVIDENCESIG555", rec["verified_evidence"] or "")
        self.assertNotIn("TKN888", rec["verified_evidence"] or "")

    def test_signed_param_name_cannot_defang_bearer_pem_redaction(self):
        # F-1 regression: the signed-param redactor must not consume a Bearer/PEM
        # marker as a param "value" and leave the secret. The inline-secret
        # full-redact runs FIRST, so `<signedname>=Bearer <token>` fully redacts.
        for note in ("sig=Bearer SECRETTOKEN12345ABCDEF",
                     "auth=Bearer SECRETTOKEN12345ABCDEF",
                     "key=Bearer SECRETTOKEN12345ABCDEF"):
            rec = recover_service._sanitize_authorization_record({
                "asserted_basis": "licensed_permission", "asserted_note": note,
                "verified": False,
            })
            self.assertEqual(rec["asserted_note"], "[REDACTED]", note)
            self.assertNotIn("SECRETTOKEN12345", rec["asserted_note"])
        # and the legitimate F5 signed-param stripping still works; prose untouched
        rec2 = recover_service._sanitize_authorization_record({
            "asserted_basis": "open_license",
            "asserted_note": "see //cdn/p?X-Amz-Signature=PLAINSIG999 the key to success",
            "verified": False,
        })
        self.assertNotIn("PLAINSIG999", rec2["asserted_note"])
        self.assertIn("the key to success", rec2["asserted_note"])

    # ── (1) a valid basis is required; an invalid basis is rejected ──
    def test_fetch_and_import_require_a_valid_basis(self):
        f_none, s1 = recover_service.start_recover_fetch(
            {"urls": ["https://www.youtube.com/watch?v=x"]})
        self.assertEqual(s1, 200)
        self.assertFalse(f_none["ok"])
        self.assertIn("authorization basis required", f_none["error"])

        f_bad, _ = recover_service.start_recover_fetch(
            {"urls": ["https://www.youtube.com/watch?v=x"],
             "authorization": {"basis": "because_i_feel_like_it"}})
        self.assertFalse(f_bad["ok"])
        self.assertIn("authorization basis required", f_bad["error"])
        # The error message enumerates all four accepted bases.
        self.assertIn("open_license", f_none["error"])
        self.assertEqual(
            recover_service.RECOVER_AUTH_BASES,
            {"original_creation", "own_account", "licensed_permission", "open_license"},
        )

        i_none, s2 = recover_service.start_recover_import({"staged_files": ["/x/y.flac"]})
        self.assertEqual(s2, 200)
        self.assertFalse(i_none["ok"])
        self.assertIn("authorization basis required", i_none["error"])

        i_bad, _ = recover_service.start_recover_import(
            {"staged_files": ["/x/y.flac"], "authorization": {"basis": "nope"}})
        self.assertFalse(i_bad["ok"])
        self.assertIn("authorization basis required", i_bad["error"])

    # ── (2) own_account WITH allowlisted account -> verified -> auto-import ──
    def test_own_account_allowlisted_is_verified_and_auto_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, uploader="DJ Me")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.side_effect = [
                [],
                [{"id": 7, "title": "My Track", "artist": "DJ Me", "path": "/music/DJ Me/My Track.flac"}],
            ]
            fake_adapter.run_import.return_value = {"ok": True, "success": True}
            from backend.transaction_engine import TransactionStore
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCdjme000"}), \
                 mock.patch("backend.app_runtime.transactions", tx_store):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account",
                    auth_note="my soundcloud")
            self.assertTrue(out["imported"], out)
            self.assertEqual(out["status"], "imported")
            fake_adapter.run_import.assert_called_once()
            # (6) transaction metadata records asserted + verified distinctly.
            tx_rec = tx_store._read(out["transaction_id"])
            auth = tx_rec["metadata"]["authorization"]
            self.assertEqual(auth["asserted_basis"], "own_account")
            self.assertEqual(auth["asserted_note"], "my soundcloud")
            self.assertTrue(auth["verified"])
            self.assertEqual(auth["verified_basis"], "own_account")
            # Evidence is the matched IMMUTABLE identifier, not a display name.
            self.assertEqual(auth["verified_evidence"], "UCdjme000")
            # The asserted note is never copied into the verified evidence.
            self.assertNotIn("my soundcloud", auth["verified_evidence"])

    # ── (3) own_account NOT on allowlist -> unverified -> review, no import ──
    def test_own_account_not_allowlisted_is_review_required_no_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Staged channel_id "UCdjme000"; the allowlist holds a DIFFERENT
            # stable id, so the stable-id evidence does not match -> unverified.
            staged = self._stage(tmp, uploader="Someone Else", channel_id="UCdjme000")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCnotme111"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertFalse(out["imported"])
            self.assertEqual(out["status"], "review_required")
            self.assertFalse(out["authorization"]["verified"])
            fake_adapter.run_import.assert_not_called()

    # ── (4) original_creation / licensed_permission are asserted-only -> review ──
    def test_asserted_only_bases_are_review_required_no_import(self):
        for basis in ("original_creation", "licensed_permission"):
            with tempfile.TemporaryDirectory() as tmp:
                staged = self._stage(tmp, uploader="DJ Me")
                fake_adapter = mock.MagicMock()
                fake_adapter.find_items_by_query.return_value = []
                # Even with the uploader on the allowlist, these never verify.
                with self._import_patches(tmp), \
                     mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "dj me"}):
                    out = recover_service._recover_import_one(
                        staged, [], adapter=fake_adapter, auth_basis=basis)
                self.assertFalse(out["imported"], basis)
                self.assertEqual(out["status"], "review_required", basis)
                self.assertFalse(out["authorization"]["verified"], basis)
                self.assertEqual(out["authorization"]["asserted_basis"], basis)
                self.assertIsNone(out["authorization"]["verified_basis"], basis)
                fake_adapter.run_import.assert_not_called()

    # ── open_license WITH platform CC license -> verified -> auto-import ──
    def test_open_license_with_cc_evidence_is_verified_and_auto_imports(self):
        cc = "Creative Commons Attribution license (reuse allowed)"
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, uploader="Someone Else", source_license=cc)
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.side_effect = [
                [],
                [{"id": 11, "title": "My Track", "artist": "Someone Else",
                  "path": "/music/Someone Else/My Track.flac"}],
            ]
            fake_adapter.run_import.return_value = {"ok": True, "success": True}
            from backend.transaction_engine import TransactionStore
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            # No owned-accounts allowlist entry: verification must come ONLY from
            # the platform license evidence, not the uploader.
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": ""}), \
                 mock.patch("backend.app_runtime.transactions", tx_store):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="open_license",
                    auth_note="it is public domain, trust me")
            self.assertTrue(out["imported"], out)
            self.assertEqual(out["status"], "imported")
            fake_adapter.run_import.assert_called_once()
            auth = out["authorization"]
            self.assertEqual(auth["asserted_basis"], "open_license")
            self.assertTrue(auth["verified"])
            self.assertEqual(auth["verified_basis"], "open_license")
            self.assertEqual(auth["verified_evidence"], cc)
            # The asserted note is never promoted into the verified evidence.
            self.assertNotIn("trust me", auth["verified_evidence"])

    # ── open_license WITHOUT platform license evidence -> review, no import ──
    def test_open_license_without_evidence_is_review_required_no_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, uploader="Someone Else", source_license="")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": ""}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="open_license")
            self.assertFalse(out["imported"])
            self.assertEqual(out["status"], "review_required")
            self.assertFalse(out["authorization"]["verified"])
            self.assertIsNone(out["authorization"]["verified_basis"])
            fake_adapter.run_import.assert_not_called()

    # ── (5) DRM-protected is refused on fetch regardless of basis ──
    def test_drm_protected_fetch_is_refused_regardless_of_basis(self):
        drm_info = {
            "title": "Protected", "uploader": "DJ Me", "duration": 180,
            "webpage_url": "https://www.youtube.com/watch?v=drm",
            "formats": [{"format_id": "wv", "protocol": "http_dash_segments",
                         "ext": "m4a", "acodec": "mp4a.40.2",
                         "has_drm": True, "drm": "widevine"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "dj me"}):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=drm", [], run_id="r",
                    ydl_module=_make_fake(info=drm_info),
                    auth_basis="own_account")
        self.assertFalse(out["recovered"])
        self.assertEqual(out["category"], "genuinely-DRM")

    # ── (6) provenance sidecar records asserted + verified distinctly ──
    def test_fetch_sidecar_records_asserted_and_verified_distinctly(self):
        import json as _json
        # The probe captures the immutable channel_id; own_account verifies
        # against the allowlisted stable id, never the display "uploader".
        info = {"title": "Mine", "uploader": "DJ Me", "duration": 200,
                "channel_id": "UCdjme000",
                "channel_url": "https://www.youtube.com/channel/UCdjme000",
                "webpage_url": "https://www.youtube.com/watch?v=abc", "formats": []}
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCdjme000"}):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", [], run_id="r",
                    ydl_module=_make_fake(info=info, filename="mine.opus"),
                    auth_basis="own_account", auth_note="my channel upload")
            self.assertTrue(out["recovered"], out)
            sidecar = _json.loads(Path(out["provenance"]).read_text(encoding="utf-8"))
            # The immutable identifiers are persisted as independent evidence.
            self.assertEqual(sidecar["channel_id"], "UCdjme000")
            auth = sidecar["authorization"]
            self.assertEqual(auth["asserted_basis"], "own_account")
            self.assertEqual(auth["asserted_note"], "my channel upload")
            self.assertTrue(auth["verified"])
            self.assertEqual(auth["verified_basis"], "own_account")
            self.assertEqual(auth["verified_evidence"], "UCdjme000")
            self.assertNotIn("my channel upload", auth["verified_evidence"])

    def test_asserted_only_fetch_sidecar_is_unverified(self):
        import json as _json
        info = {"title": "Mine", "uploader": "DJ Me", "duration": 200,
                "webpage_url": "https://www.youtube.com/watch?v=abc", "formats": []}
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "dj me"}):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=abc", [], run_id="r",
                    ydl_module=_make_fake(info=info, filename="mine.opus"),
                    auth_basis="original_creation", auth_note="I made this")
            sidecar = _json.loads(Path(out["provenance"]).read_text(encoding="utf-8"))
            auth = sidecar["authorization"]
            self.assertEqual(auth["asserted_basis"], "original_creation")
            self.assertFalse(auth["verified"])
            self.assertIsNone(auth["verified_basis"])
            self.assertIsNone(auth["verified_evidence"])

    def test_fetch_captures_platform_license_and_verifies_open_license(self):
        import json as _json
        cc = "Creative Commons Attribution license (reuse allowed)"
        info = {"title": "Freely", "uploader": "Rando", "duration": 200,
                "webpage_url": "https://www.youtube.com/watch?v=cc1",
                "license": cc, "license_url": "https://creativecommons.org/licenses/by/3.0/",
                "formats": []}
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": ""}):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/watch?v=cc1", [], run_id="r",
                    ydl_module=_make_fake(info=info, filename="freely.opus"),
                    auth_basis="open_license", auth_note="CC licensed")
            self.assertTrue(out["recovered"], out)
            sidecar = _json.loads(Path(out["provenance"]).read_text(encoding="utf-8"))
            # Platform license metadata captured as independent evidence.
            self.assertEqual(sidecar["source_license"], cc)
            auth = sidecar["authorization"]
            self.assertEqual(auth["asserted_basis"], "open_license")
            self.assertTrue(auth["verified"])
            self.assertEqual(auth["verified_basis"], "open_license")
            self.assertEqual(auth["verified_evidence"], cc)

    # ── P1: impersonation via mutable display name must NOT verify ──
    def test_impersonation_display_name_is_review_required_no_import(self):
        # The allowlisted token appears INSIDE the mutable display uploader, but
        # the immutable channel_id differs -> own_account is not verified, the
        # item goes to review, and nothing is imported.
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(
                tmp, uploader="realchannel Official (reupload)",
                channel_id="UCimposter999")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "realchannel"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertFalse(out["imported"])
            self.assertEqual(out["status"], "review_required")
            self.assertFalse(out["authorization"]["verified"])
            fake_adapter.run_import.assert_not_called()

    # ── P1: a genuine stable channel-URL match DOES verify -> auto-import ──
    def test_own_account_channel_url_match_is_verified_and_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No channel_id; verification must come from the canonical profile URL
            # (allowlist has www + trailing slash, sidecar does not -> canonicalized).
            staged = self._stage(
                tmp, uploader="Someone", channel_id="",
                uploader_url="https://soundcloud.com/myprofile")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.side_effect = [
                [],
                [{"id": 21, "title": "My Track", "artist": "Someone",
                  "path": "/music/Someone/My Track.flac"}],
            ]
            fake_adapter.run_import.return_value = {"ok": True, "success": True}
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ,
                                 {"RECOVER_OWNED_ACCOUNTS": "https://soundcloud.com/myprofile/"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertTrue(out["imported"], out)
            self.assertEqual(out["status"], "imported")
            fake_adapter.run_import.assert_called_once()
            auth = out["authorization"]
            self.assertTrue(auth["verified"])
            self.assertEqual(auth["verified_basis"], "own_account")
            self.assertEqual(auth["verified_evidence"], "https://soundcloud.com/myprofile")

    # ── P1: an allowlist entry shorter than 3 chars never verifies ──
    def test_short_allowlist_entry_never_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Even an exact-equal 2-char stable id is dropped from the allowlist.
            staged = self._stage(tmp, uploader="Me", channel_id="ab")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "ab"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertFalse(out["imported"])
            self.assertEqual(out["status"], "review_required")
            self.assertFalse(out["authorization"]["verified"])
            fake_adapter.run_import.assert_not_called()

    # ── P3: authorization is attached to EVERY import outcome (F4) ──
    def test_identity_gate_review_outcome_carries_authorization(self):
        # own_account is VERIFIED, but the identity gate routes a could-not-verify
        # AcoustID outage to review. The authorization record must still ride
        # along (previously dropped on identity-gate returns, F4).
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, channel_id="UCdjme000")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            with self._import_patches(tmp, identity={
                        "acoustid_status": "lookup_failed",
                        "conflicts": ["acoustid_lookup_failed"]}), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCdjme000"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertEqual(out["status"], "review_required")
            self.assertIsInstance(out["authorization"], dict)
            self.assertEqual(out["authorization"]["asserted_basis"], "own_account")
            self.assertTrue(out["authorization"]["verified"])
            fake_adapter.run_import.assert_not_called()

    def test_identity_gate_reject_outcome_carries_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, channel_id="UCdjme000")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            with self._import_patches(tmp, identity={
                        "acoustid_status": "mismatch", "identity_status": "conflict",
                        "final_action": "reject", "acoustid_match_score": 0.95,
                        "mb_recording_id_candidate": "wrong-999",
                        "conflicts": ["acoustid_artist_mismatch"]}), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCdjme000"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertEqual(out["status"], "identity_conflict")
            self.assertIsInstance(out["authorization"], dict)
            self.assertEqual(out["authorization"]["asserted_basis"], "own_account")

    def test_import_failed_outcome_carries_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, channel_id="UCdjme000")
            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []
            fake_adapter.run_import.return_value = {"ok": False, "error": "engine busy"}
            with self._import_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCdjme000"}):
                out = recover_service._recover_import_one(
                    staged, [], adapter=fake_adapter, auth_basis="own_account")
            self.assertEqual(out["status"], "import_failed")
            self.assertIsInstance(out["authorization"], dict)
            self.assertEqual(out["authorization"]["asserted_basis"], "own_account")

    # ── P3: a fetch non-recovered outcome also carries authorization ──
    def test_fetch_collection_refusal_carries_authorization(self):
        info = {"_type": "playlist", "entries": [{}, {}],
                "webpage_url": "https://www.youtube.com/@chan", "title": "chan"}
        with tempfile.TemporaryDirectory() as tmp:
            with _fetch_patches(tmp), \
                 mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": ""}):
                out = recover_service._recover_fetch_one(
                    "https://www.youtube.com/@chan", [], run_id="r",
                    ydl_module=_make_fake(info=info),
                    auth_basis="original_creation", auth_note="mine")
        self.assertFalse(out["recovered"])
        self.assertEqual(out["category"], "collection")
        self.assertIsInstance(out["authorization"], dict)
        self.assertEqual(out["authorization"]["asserted_basis"], "original_creation")

    # ── P3: retry forwards the parent run's asserted authorization ──
    def test_retry_forwards_parent_authorization(self):
        captured = {}

        def _fake_fetch(payload):
            captured.update(payload)
            return ({"ok": True, "job_id": "j", "run_id": "r2"}, 200)

        parent = {
            "run_id": "p1",
            "urls": ["https://www.youtube.com/watch?v=x"],
            "recovered": [],
            "failed": [{"url": "https://www.youtube.com/watch?v=x",
                        "category": "rate-limited"}],
            "authorization": {"asserted_basis": "own_account",
                              "asserted_note": "my channel"},
        }
        with mock.patch.object(recover_service, "recover_get_run", return_value=parent), \
             mock.patch.object(recover_service, "start_recover_fetch", side_effect=_fake_fetch):
            body, status = recover_service.start_recover_retry({"run_id": "p1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        # The parent's asserted basis/note is forwarded verbatim, not dropped
        # or replaced by a stale run-level record.
        self.assertEqual(captured["authorization"],
                         {"basis": "own_account", "note": "my channel"})


class OwnAccountImmutableIdentityMatching(unittest.TestCase):
    """P1: own_account verifies ONLY against immutable channel/uploader
    identifiers, by exact token / canonical-URL match — never a substring of a
    mutable display name."""

    def _eval(self, allow, **kw):
        with mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": allow}):
            return recover_service.evaluate_recover_authorization("own_account", "", **kw)

    def test_exact_channel_id_match_verifies(self):
        r = self._eval("UCabc123xyz", channel_id="UCabc123xyz",
                       uploader="A Totally Different Display Name")
        self.assertTrue(r["verified"])
        self.assertEqual(r["verified_basis"], "own_account")
        self.assertEqual(r["verified_evidence"], "UCabc123xyz")

    def test_user_path_segment_does_not_verify_across_platforms(self):
        # F3: an allowlisted youtube.com/user/alice must NEVER verify a DIFFERENT
        # platform's source whose bare uploader_id happens to be "alice" -- the
        # /user/<name> path segment is host-namespaced, not emitted bare.
        r = self._eval("https://youtube.com/user/alice",
                       uploader_id="alice", channel_id="UCotherplatform000")
        self.assertFalse(r["verified"])
        self.assertIsNone(r["verified_basis"])
        # The same uploader on the SAME platform URL still verifies.
        ok = self._eval("https://youtube.com/user/alice",
                        channel_url="https://www.youtube.com/user/alice")
        self.assertTrue(ok["verified"])

    def test_exact_uploader_id_match_verifies(self):
        r = self._eval("12345678", uploader_id="12345678")
        self.assertTrue(r["verified"])

    def test_display_name_substring_is_not_evidence(self):
        # The allowlist token is a substring of the mutable display name, but the
        # immutable channel_id differs -> impersonation must NOT verify.
        r = self._eval("realchannel", channel_id="UCimposter999",
                       uploader="realchannel official (reupload)",
                       channel="realchannel official")
        self.assertFalse(r["verified"])
        self.assertIsNone(r["verified_basis"])
        self.assertIsNone(r["verified_evidence"])

    def test_display_uploader_exact_equal_is_never_evidence(self):
        # Even an exact allowlist==display-name match must not verify (no stable
        # identifier is present).
        r = self._eval("my display name", uploader="My Display Name",
                       channel="My Display Name")
        self.assertFalse(r["verified"])

    def test_channel_url_handle_boundary_match_verifies(self):
        r = self._eval("https://www.youtube.com/@mychannel",
                       channel_url="https://youtube.com/@mychannel")
        self.assertTrue(r["verified"])

    def test_bare_handle_matches_channel_url_segment(self):
        r = self._eval("@mychannel", channel_url="https://www.youtube.com/@mychannel")
        self.assertTrue(r["verified"])

    def test_channel_path_id_segment_match(self):
        r = self._eval("UCpathid000",
                       channel_url="https://www.youtube.com/channel/UCpathid000")
        self.assertTrue(r["verified"])

    def test_soundcloud_profile_url_match(self):
        r = self._eval("https://soundcloud.com/myprofile",
                       uploader_url="https://soundcloud.com/myprofile")
        self.assertTrue(r["verified"])

    def test_prefix_of_stable_id_does_not_match(self):
        # Token equality, not substring: a prefix of the real id must not verify.
        r = self._eval("UCabc", channel_id="UCabc123")
        self.assertFalse(r["verified"])

    def test_entry_shorter_than_3_chars_never_verifies(self):
        r = self._eval("ab", channel_id="ab", uploader_id="ab")
        self.assertFalse(r["verified"])

    def test_handle_boundary_not_substring(self):
        # Allowlisted "@me" must not match a different handle "@melody" (the path
        # segment is compared exactly, not as a substring).
        r = self._eval("@me", channel_url="https://www.youtube.com/@melody")
        self.assertFalse(r["verified"])


def _ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30)
        return name in (out.stdout or "")
    except Exception:
        return False


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and _ffmpeg_has_encoder("libopus"),
    "ffmpeg + ffprobe + libopus required for real-webm remux tests")
class WebmRemuxBeforeBeetsImport(unittest.TestCase):
    """A YouTube bestaudio opus-in-.webm stages fine but Beets adds NO item from
    it; the identical audio as .opus imports. _recover_import_one must remux the
    .webm (lossless) to an importable container BEFORE run_import, verify the
    converted audio, import the CONVERTED path, and preserve the ORIGINAL."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCtest000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _gen_opus_webm(self, path, duration=5):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={duration}",
             "-c:a", "libopus", "-b:a", "96k", str(path)],
            check=True, capture_output=True, timeout=60)

    def _gen_flac(self, path, duration=5):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={duration}",
             "-c:a", "flac", str(path)],
            check=True, capture_output=True, timeout=60)

    def _stage(self, tmp, fname, duration=5.0, codec="opus"):
        run_dir = Path(tmp) / "recovered" / "youtube" / "runwebm"
        run_dir.mkdir(parents=True, exist_ok=True)
        audio = run_dir / fname
        Path(str(audio) + ".recover.json").write_text(
            '{"source_url": "https://www.youtube.com/watch?v=abc", '
            '"source_platform": "youtube", "uploader": "Me", '
            '"channel_id": "UCtest000", '
            f'"title": "Webm Upload", "duration": {duration}, '
            f'"codec": "{codec}", "quality": "lossy"}}',
            encoding="utf-8")
        return audio

    def _singleton_identity(self):
        return {
            "fingerprint_status": "no_result",
            "acoustid_status": "no_result",
            "metadata_agreement": "degraded",
            "conflicts": ["no_acoustid_result"],
            "acoustid_match_score": 0.0,
        }

    def test_webm_opus_is_remuxed_verified_and_imported_from_converted_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, "upload.webm")
            self._gen_opus_webm(staged, duration=5)

            fake_adapter = mock.MagicMock()
            # Pre-import conflict check -> empty; post-import read-back -> item #7.
            fake_adapter.find_items_by_query.side_effect = [
                [],
                [{"id": 7, "title": "Webm Upload", "artist": "Me",
                  "path": "/music/Me/Webm Upload.opus"}],
            ]
            fake_adapter.run_import.return_value = {"ok": True, "success": True}

            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch("backend.acoustid_service._audio_identity_decision",
                            return_value=self._singleton_identity()), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out = recover_service._recover_import_one(
                    str(staged), [], adapter=fake_adapter, auth_basis="own_account")

            self.assertTrue(out["imported"], out)
            self.assertEqual(out["status"], "imported")
            self.assertNotEqual(out.get("error"), "beets import completed without adding item to library")
            # run_import received the CONVERTED path, not the .webm.
            _, kwargs = fake_adapter.run_import.call_args
            import_path = kwargs["paths"][0]
            self.assertFalse(import_path.endswith(".webm"), import_path)
            self.assertTrue(import_path.endswith(".opus"), import_path)
            self.assertNotEqual(import_path, str(staged))
            # Conversion is recorded on the outcome: lossless remux, webm->opus.
            conv = out["conversion"]
            self.assertTrue(conv["converted"])
            self.assertTrue(conv["lossless_remux"])
            self.assertIn("webm", conv["original_container"])
            self.assertEqual(conv["converted_container"], "opus")
            # Recorded in provenance too (sidecar of the preserved original).
            sidecar = json.loads(Path(str(staged) + ".recover.json").read_text(encoding="utf-8"))
            self.assertTrue(sidecar.get("conversion", {}).get("converted"))

    def test_original_webm_is_preserved_after_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, "keepme.webm")
            self._gen_opus_webm(staged, duration=5)
            before = staged.read_bytes()

            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.side_effect = [
                [], [{"id": 8, "title": "Webm Upload", "artist": "Me",
                      "path": "/music/Me/Webm Upload.opus"}]]
            fake_adapter.run_import.return_value = {"ok": True, "success": True}

            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch("backend.acoustid_service._audio_identity_decision",
                            return_value=self._singleton_identity()), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out = recover_service._recover_import_one(
                    str(staged), [], adapter=fake_adapter, auth_basis="own_account")

            self.assertTrue(out["imported"], out)
            # The ORIGINAL .webm is untouched (exists, byte-identical).
            self.assertTrue(staged.exists())
            self.assertEqual(staged.read_bytes(), before)

    def test_already_opus_or_flac_is_not_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A real .flac (container "flac") and a real .opus (container "ogg")
            # are both importable — neither is touched by the converter.
            flac = Path(tmp) / "recovered" / "youtube" / "runwebm"
            flac.mkdir(parents=True, exist_ok=True)
            flac_file = flac / "song.flac"
            self._gen_flac(flac_file, duration=5)
            res_flac = recover_service._convert_staged_for_beets(
                str(flac_file), codec="flac", reference_duration=5.0, log=[])
            self.assertEqual(res_flac, {"converted": False})

            opus_file = flac / "song.opus"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=5", "-c:a", "libopus", str(opus_file)],
                check=True, capture_output=True, timeout=60)
            res_opus = recover_service._convert_staged_for_beets(
                str(opus_file), codec="opus", reference_duration=5.0, log=[])
            self.assertEqual(res_opus, {"converted": False})

    def test_verify_converted_audio_rejects_undecodable_and_wrong_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "garbage.opus"
            junk.write_bytes(b"not audio at all" * 64)
            bad = recover_service._verify_converted_audio(str(junk), 5.0)
            self.assertFalse(bad["ok"])

            good = Path(tmp) / "good.opus"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=5", "-c:a", "libopus", str(good)],
                check=True, capture_output=True, timeout=60)
            # Real 5s audio claimed to be 30s: duration check rejects it.
            wrong = recover_service._verify_converted_audio(str(good), 30.0)
            self.assertFalse(wrong["ok"])
            self.assertIn("duration", wrong["error"])
            ok = recover_service._verify_converted_audio(str(good), 5.0)
            self.assertTrue(ok["ok"], ok)

    def test_corrupt_conversion_is_rejected_import_failed_original_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._stage(tmp, "bad.webm")
            self._gen_opus_webm(staged, duration=5)
            before = staged.read_bytes()

            fake_adapter = mock.MagicMock()
            fake_adapter.find_items_by_query.return_value = []

            # Simulate a conversion whose output fails verification (corrupt/
            # undecodable): the item must NOT be imported and the original kept.
            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "_verify_converted_audio",
                                   return_value={"ok": False, "error": "converted file could not be decoded by ffprobe"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision",
                            return_value=self._singleton_identity()), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out = recover_service._recover_import_one(
                    str(staged), [], adapter=fake_adapter, auth_basis="own_account")

            self.assertFalse(out["imported"])
            self.assertEqual(out["status"], "import_failed")
            self.assertIn("decode", out["error"].lower())
            fake_adapter.run_import.assert_not_called()
            # Original .webm preserved, byte-identical.
            self.assertTrue(staged.exists())
            self.assertEqual(staged.read_bytes(), before)


class _FakeRecoverLib:
    """Minimal in-memory Beets library for recover-import rollback tests.

    run_import copies the staged file into MUSIC_ROOT and adds exactly one
    item (what a real singleton import does); remove() drops only the named
    rows and deletes a file only when delete_files=True (the rollback never
    asks for that -- it deletes the recorded file itself, under guard).
    """

    def __init__(self, music_root):
        self.music_root = str(music_root)
        self.items = {}
        self._next = 1
        self.remove_calls = []

    def item_count(self):
        return len(self.items)

    def _rows(self):
        return [dict(it) for it in self.items.values()]

    def find_items_by_query(self, query):
        # Models stock Beets: a flexattr query may return extra rows; the caller
        # (_find_recovered_item_by_token) filters by exact token equality.
        return self._rows()

    def find_all_items_by_mbid(self, mbid):
        return [dict(it) for it in self.items.values() if it.get("mb_trackid") == mbid]

    def get_item(self, iid):
        it = self.items.get(int(iid))
        return dict(it) if it else None

    def run_import(self, paths=None, set_fields=None, **kw):
        sf = set_fields or {}
        src = (list(paths) if not isinstance(paths, str) else [paths])[0]
        artist = sf.get("artist") or "Unknown"
        title = sf.get("title") or "Unknown"
        dest_dir = os.path.join(self.music_root, artist)
        os.makedirs(dest_dir, exist_ok=True)
        iid = self._next
        self._next += 1
        # Unique per item so two imports of the SAME artist/title still produce
        # two distinct files (a real singleton import never collides them).
        dest = os.path.join(dest_dir, f"{title} [{iid}]" + os.path.splitext(src)[1])
        shutil.copy2(src, dest)
        self.items[iid] = {
            "id": iid, "artist": artist, "albumartist": artist, "title": title,
            "mb_trackid": sf.get("mb_trackid", ""), "path": dest,
            # The unique provenance token stamped by set_fields (F1).
            "webmanager_recover_token": sf.get("webmanager_recover_token", ""),
        }
        return {"ok": True, "success": True}

    def remove(self, item_ids=None, album_ids=None, delete_files=False):
        self.remove_calls.append({"item_ids": list(item_ids or []), "delete_files": delete_files})
        removed = 0
        for iid in item_ids or []:
            it = self.items.pop(int(iid), None)
            if it is None:
                continue
            removed += 1
            if delete_files:
                try:
                    os.unlink(it["path"])
                except OSError:
                    pass
        return {"success": True, "removed_items": removed}


class _PluginAllowlistRecoverLib(_FakeRecoverLib):
    """Faithful to the REAL Beets boundary: run_import passes set_fields through
    the webmanager plugin's validate_fields (as operations.run_import does)
    BEFORE any field reaches the item, and persists every surviving flexattr.
    The plain _FakeRecoverLib skips validate_fields, which is why the earlier
    tests missed #1 -- a provenance token the allowlist drops still 'survived'
    in the fake and the read-back falsely passed."""

    def run_import(self, paths=None, set_fields=None, **kw):
        from beetsplug.webmanager.schemas import validate_fields
        sf = validate_fields(set_fields or {}, is_album=False)
        res = super().run_import(paths=paths, set_fields=sf, **kw)
        if self.items:  # persist ALL surviving flexattrs, as a real import does
            self.items[max(self.items)].update(sf)
        return res


class RecoverProvenanceAllowlistTests(unittest.TestCase):
    """#1 regression (BLOCKING): the recover provenance flexattrs must survive
    the plugin's validate_fields allowlist so the post-import token read-back
    binds the created item. Dropping them made a real import falsely report
    failure and record no rollback. Exercises the REAL validate_fields."""

    def test_provenance_fields_survive_real_validate_fields(self):
        from beetsplug.webmanager.schemas import validate_fields
        tok = uuid.uuid4().hex
        safe = validate_fields({
            "artist": "A", "albumartist": "A", "title": "T", "album": "Alb",
            "source_platform": "soundcloud", "webmanager_recover_token": tok,
        }, is_album=False)
        self.assertEqual(safe.get("webmanager_recover_token"), tok)
        self.assertEqual(safe.get("source_platform"), "soundcloud")

    def test_token_readback_binds_created_item_through_plugin_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _PluginAllowlistRecoverLib(music_root)
            src = os.path.join(tmp, "s.flac")
            with open(src, "wb") as fh:
                fh.write(b"\x00" * 4096)
            tok = uuid.uuid4().hex
            res = fake.run_import(paths=[src], set_fields={
                "artist": "A", "albumartist": "A", "title": "T", "album": "Alb",
                "source_platform": "soundcloud", "webmanager_recover_token": tok,
            })
            self.assertTrue(res["ok"], res)
            found = recover_service._find_recovered_item_by_token(tok, adapter=fake)
            self.assertIsNotNone(found)
            self.assertEqual(found["webmanager_recover_token"], tok)
            self.assertEqual(found["id"], 1)  # exactly the created item
            self.assertEqual(found["source_platform"], "soundcloud")

    def test_lookup_fails_closed_on_single_row_with_a_different_token(self):
        # #2: a single returned row carrying a DIFFERENT non-empty token is NOT
        # this import's item -- fail closed (never a fuzzy single-row fallback).
        class _OneWrongRow:
            def find_items_by_query(self, query):
                return [{"id": 7, "webmanager_recover_token": "some-other-token",
                         "path": "/music/x.flac"}]
        self.assertIsNone(
            recover_service._find_recovered_item_by_token("the-real-token", adapter=_OneWrongRow()))

    def test_lookup_trusts_single_row_with_no_token_field(self):
        # The fail-closed change must NOT over-reject: a single row with no token
        # field at all (an adapter that doesn't echo the flexattr) is still
        # trusted -- the server-side query already narrowed to one row.
        class _OneNoToken:
            def find_items_by_query(self, query):
                return [{"id": 9, "path": "/music/y.flac"}]
        found = recover_service._find_recovered_item_by_token("tok", adapter=_OneNoToken())
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 9)


class RecoverImportRollbackTests(unittest.TestCase):
    """Safe, audited rollback of a newly-imported recovery singleton, with hard
    safeguards against touching any pre-existing library content."""

    _IDENTITY = {
        "fingerprint_status": "no_result",
        "acoustid_status": "no_result",
        "metadata_agreement": "degraded",
        "conflicts": ["no_acoustid_result"],
        "acoustid_match_score": 0.0,
    }

    def setUp(self):
        # own_account is only independently verified when the sidecar's
        # immutable channel_id is a configured owned account (UCmine000).
        patcher = mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCmine000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _import_one(self, tmp, fake, *, title, uploader, fname, run="run"):
        stage_root = Path(tmp) / "recovered" / "soundcloud" / run
        stage_root.mkdir(parents=True, exist_ok=True)
        staged = stage_root / fname
        staged.write_bytes(b"\x00" * 4096)
        Path(str(staged) + ".recover.json").write_text(json.dumps({
            "source_url": f"https://soundcloud.com/me/{run}",
            "source_platform": "soundcloud", "uploader": uploader,
            "channel_id": "UCmine000", "title": title, "duration": 210.0,
            "codec": "flac", "quality": "original",
        }), encoding="utf-8")
        from backend.transaction_engine import TransactionStore
        tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
        with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
             mock.patch("backend.app_runtime.transactions", tx_store), \
             mock.patch("backend.config_layers.music_root", return_value=fake.music_root), \
             mock.patch.object(recover_service, "recover_integrity_check",
                               return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
             mock.patch.object(recover_service, "inspect_audio_file", return_value={"ok": True, "codec": "flac"}), \
             mock.patch("backend.acoustid_service._audio_identity_decision", return_value=dict(self._IDENTITY)), \
             mock.patch("helpers_mb._mb_recording_search", return_value=[]):
            out = recover_service._recover_import_one(
                str(staged), [], adapter=fake, auth_basis="own_account")
        return tx_store, out, staged

    def test_rollback_removes_only_the_recorded_item_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            before_items = fake.item_count()
            tx_store, out, staged = self._import_one(
                tmp, fake, title="My Track", uploader="DJ Me", fname="my_track.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            after_items = fake.item_count()

            # Rollback data was captured on the audited Import transaction.
            rec = tx_store._read(tx_id)
            self.assertEqual(rec["operation_type"], "Import")
            self.assertEqual(rec["status"], "Completed")
            self.assertEqual(rec["metadata"]["mutation_family"], "recover_import_v1")
            self.assertTrue(rec["rollback"]["available"])
            ops = rec["rollback"]["operations"]
            self.assertEqual(len(ops), 1)
            self.assertEqual(ops[0]["type"], "remove_recovered_item")
            self.assertEqual(ops[0]["item_id"], out["imported_item_id"])
            self.assertTrue(ops[0]["sha256"] and ops[0]["size"] > 0)
            imported_path = ops[0]["path"]
            self.assertTrue(os.path.isfile(imported_path))

            # Counts: before=0, after import=1.
            self.assertEqual(before_items, 0)
            self.assertEqual(after_items, 1)

            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=music_root)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["status"], "Rolled Back")
            self.assertEqual(res["removed_item_id"], out["imported_item_id"])

            # Item row gone, imported file gone, counts back to pre-import.
            self.assertEqual(fake.item_count(), before_items)
            self.assertIsNone(fake.get_item(out["imported_item_id"]))
            self.assertFalse(os.path.exists(imported_path))
            # delete_files was NEVER requested of Beets (file handled under guard).
            self.assertTrue(all(c["delete_files"] is False for c in fake.remove_calls))
            # Audited terminal state.
            rec2 = tx_store._read(tx_id)
            self.assertEqual(rec2["status"], "Rolled Back")
            self.assertFalse(rec2["rollback"]["available"])
            self.assertTrue(any("Removed Beets item" in m for m in rec2["logs"]))
            # Staged original under DOWNLOADS_ROOT is preserved through rollback.
            self.assertTrue(staged.exists())
            self.assertEqual(staged.read_bytes(), b"\x00" * 4096)

    def test_rollback_never_touches_preexisting_library_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            # A PRE-EXISTING library item and its real file, present before the
            # recovery import (different identity so no import-time conflict).
            pre_dir = os.path.join(music_root, "Existing Artist")
            os.makedirs(pre_dir, exist_ok=True)
            pre_path = os.path.join(pre_dir, "Old Song.flac")
            pre_bytes = b"PREEXISTING-CONTENT" * 64
            with open(pre_path, "wb") as fh:
                fh.write(pre_bytes)
            fake.items[500] = {"id": 500, "artist": "Existing Artist", "albumartist": "Existing Artist",
                               "title": "Old Song", "mb_trackid": "", "path": pre_path}
            fake._next = 501

            tx_store, out, staged = self._import_one(
                tmp, fake, title="Recovered Track", uploader="New Uploader", fname="rec.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            self.assertEqual(fake.item_count(), 2)

            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=music_root)
            self.assertTrue(res["ok"], res)

            # Pre-existing item + file UNTOUCHED; only the recovered one removed.
            self.assertEqual(fake.item_count(), 1)
            self.assertIsNotNone(fake.get_item(500))
            self.assertTrue(os.path.isfile(pre_path))
            with open(pre_path, "rb") as fh:
                self.assertEqual(fh.read(), pre_bytes)

    def test_rollback_refuses_when_recorded_file_was_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            tx_store, out, staged = self._import_one(
                tmp, fake, title="Tamper Track", uploader="DJ Me", fname="t.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            imported_path = tx_store._read(tx_id)["rollback"]["operations"][0]["path"]

            # The recorded file is changed/replaced after import (someone added
            # to or modified it): hash/size no longer match what was recorded.
            with open(imported_path, "ab") as fh:
                fh.write(b"EXTRA-BYTES-ADDED-LATER")

            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=music_root)
            self.assertFalse(res["ok"], res)
            self.assertEqual(res["code"], "recover_rollback_tampered")

            # Nothing removed: item + (tampered) file both still present.
            self.assertEqual(fake.item_count(), 1)
            self.assertIsNotNone(fake.get_item(out["imported_item_id"]))
            self.assertTrue(os.path.isfile(imported_path))
            self.assertEqual(fake.remove_calls, [])
            self.assertEqual(tx_store._read(tx_id)["status"], "Completed")

    def test_rollback_resumes_after_crash_between_db_and_file_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            tx_store, out, staged = self._import_one(
                tmp, fake, title="Crash Track", uploader="DJ Me", fname="c.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            iid = out["imported_item_id"]
            imported_path = tx_store._read(tx_id)["rollback"]["operations"][0]["path"]

            # Simulate a crash mid-rollback: the Beets row was already removed
            # and progress persisted, but the file delete had not happened yet,
            # and the transaction was left Running.
            fake.items.pop(int(iid), None)
            rec = tx_store._read(tx_id)
            op = rec["rollback"]["operations"][0]
            op["progress"] = {"db_removed": True, "file_deleted": False}
            tx_store.update(tx_id, status="Running", rollback={"operations": [op]})
            self.assertTrue(os.path.isfile(imported_path))

            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=music_root)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["status"], "Rolled Back")
            self.assertFalse(os.path.exists(imported_path))
            self.assertEqual(tx_store._read(tx_id)["status"], "Rolled Back")

    def test_store_rollback_dispatches_to_recover_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)
            tx_store, out, staged = self._import_one(
                tmp, fake, title="Dispatch Track", uploader="DJ Me", fname="d.flac")
            tx_id = out["transaction_id"]
            imported_path = tx_store._read(tx_id)["rollback"]["operations"][0]["path"]
            # The generic store entry point must route to the family executor
            # (it uses the default adapter, so point it at the fake lib).
            with mock.patch("backend.beets_adapter.beets_adapter", fake), \
                 mock.patch("backend.config_layers.music_root", return_value=music_root):
                res = tx_store.rollback(tx_id)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["status"], "Rolled Back")
            self.assertFalse(os.path.exists(imported_path))

    def test_concurrent_same_title_imports_record_distinct_items(self):
        # F1: provenance for rollback is captured authoritatively (by a unique
        # per-import token), never by a fuzzy (artist,title) re-query. Even under
        # the concurrency window the guard normally blocks -- here forced by
        # stubbing the pre-import conflict check to None -- two imports of the
        # SAME (artist,title) each bind their OWN created item, so rolling one
        # back can never delete the other's file.
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)
            with mock.patch.object(recover_service, "_find_existing_library_conflict", return_value=None):
                tx_a, out_a, _ = self._import_one(
                    tmp, fake, title="Same Title", uploader="DJ Me", fname="a.flac", run="A")
                tx_b, out_b, _ = self._import_one(
                    tmp, fake, title="Same Title", uploader="DJ Me", fname="b.flac", run="B")
            self.assertTrue(out_a["imported"] and out_b["imported"], (out_a, out_b))
            ida, idb = out_a["imported_item_id"], out_b["imported_item_id"]
            self.assertNotEqual(ida, idb)
            path_a = tx_a._read(out_a["transaction_id"])["rollback"]["operations"][0]["path"]
            path_b = tx_b._read(out_b["transaction_id"])["rollback"]["operations"][0]["path"]
            self.assertNotEqual(path_a, path_b)
            self.assertTrue(os.path.isfile(path_a) and os.path.isfile(path_b))

            # Roll back A: only A's item + file go; B is wholly untouched.
            res = txe.rollback_recover_import(tx_a, out_a["transaction_id"], adapter=fake, music_root=music_root)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["removed_item_id"], ida)
            self.assertIsNone(fake.get_item(ida))
            self.assertFalse(os.path.exists(path_a))
            self.assertIsNotNone(fake.get_item(idb))
            self.assertTrue(os.path.isfile(path_b))

            # Roll back B independently: only now does B's item + file go.
            res_b = txe.rollback_recover_import(tx_b, out_b["transaction_id"], adapter=fake, music_root=music_root)
            self.assertTrue(res_b["ok"], res_b)
            self.assertEqual(res_b["removed_item_id"], idb)
            self.assertFalse(os.path.exists(path_b))

    def test_rollback_resumes_when_crash_left_progress_unpersisted(self):
        # F7: a crash BETWEEN adapter.remove and the progress persist leaves the
        # Beets row gone but op.progress.db_removed still False. Resume must still
        # complete -- guard B treats the missing row as already-removed and the
        # content hash still gates the unlink.
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            tx_store, out, staged = self._import_one(
                tmp, fake, title="Crash Unpersisted", uploader="DJ Me", fname="cu.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            iid = out["imported_item_id"]
            imported_path = tx_store._read(tx_id)["rollback"]["operations"][0]["path"]

            # Crash: row already removed, progress NOT persisted (db_removed stays
            # False), transaction left Running.
            fake.items.pop(int(iid), None)
            tx_store.update(tx_id, status="Running")
            self.assertFalse(tx_store._read(tx_id)["rollback"]["operations"][0]["progress"]["db_removed"])
            self.assertTrue(os.path.isfile(imported_path))

            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=music_root)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["status"], "Rolled Back")
            self.assertFalse(os.path.exists(imported_path))
            self.assertEqual(tx_store._read(tx_id)["status"], "Rolled Back")

    def test_rollback_refuses_missing_row_on_a_completed_transaction(self):
        # F7 hardening: a missing Beets row is inferred to be a crash mid-rollback
        # ONLY from a resume status. On a Completed transaction no rollback ever
        # started, so a vanished row is unexplained -- guard B must REFUSE rather
        # than infer crash-resume and proceed to unlink the file.
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            tx_store, out, staged = self._import_one(
                tmp, fake, title="Completed Missing", uploader="DJ Me", fname="cm.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            iid = out["imported_item_id"]
            imported_path = tx_store._read(tx_id)["rollback"]["operations"][0]["path"]

            # Row vanished, but the transaction is still Completed (no rollback
            # ever ran, db_removed stays False).
            fake.items.pop(int(iid), None)
            self.assertEqual(tx_store._read(tx_id)["status"], "Completed")
            self.assertFalse(tx_store._read(tx_id)["rollback"]["operations"][0]["progress"]["db_removed"])

            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=music_root)
            self.assertFalse(res["ok"], res)
            self.assertEqual(res["code"], "recover_rollback_item_missing")
            self.assertFalse(res["mutated"])
            # File left intact; transaction unchanged.
            self.assertTrue(os.path.isfile(imported_path))
            self.assertEqual(tx_store._read(tx_id)["status"], "Completed")

    def test_rollback_refuses_when_music_root_changed(self):
        # F2: the recorded op["music_root"] never widens the trust boundary. When
        # MUSIC_ROOT changed so the recorded path is no longer under the CURRENT
        # root, rollback is refused rather than trusting the recorded root.
        with tempfile.TemporaryDirectory() as tmp:
            from backend import transaction_engine as txe
            music_root = os.path.join(tmp, "music")
            os.makedirs(music_root, exist_ok=True)
            fake = _FakeRecoverLib(music_root)

            tx_store, out, staged = self._import_one(
                tmp, fake, title="Root Track", uploader="DJ Me", fname="r.flac")
            self.assertTrue(out["imported"], out)
            tx_id = out["transaction_id"]
            imported_path = tx_store._read(tx_id)["rollback"]["operations"][0]["path"]

            new_root = os.path.join(tmp, "music_new")
            os.makedirs(new_root, exist_ok=True)
            res = txe.rollback_recover_import(tx_store, tx_id, adapter=fake, music_root=new_root)
            self.assertFalse(res["ok"], res)
            self.assertEqual(res["code"], "recover_rollback_path_unsafe")
            # Nothing removed; recorded file + item still present; still Completed.
            self.assertEqual(fake.item_count(), 1)
            self.assertTrue(os.path.isfile(imported_path))
            self.assertEqual(fake.remove_calls, [])
            self.assertEqual(tx_store._read(tx_id)["status"], "Completed")


if __name__ == "__main__":
    unittest.main()
