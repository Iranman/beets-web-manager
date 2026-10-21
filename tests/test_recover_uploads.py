"""Recover My Uploads (increment 1) tests.

No real credentials, no network, no live retrieval: the yt-dlp module is faked
and the SSRF/allowlist guard's DNS resolution is monkeypatched. These prove the
guard still rejects bad targets on the new entry, that URL and creator targets
are built through the unchanged guard, cookie-state messages, the integrity
gate, and the error taxonomy (a format/HLS/auth failure is never labelled DRM).
"""
import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
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
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
            "note": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret",
        }
        sanitized = recover_service.sanitize_diagnostic_report(raw_lab_report)
        self.assertTrue(sanitized["ok"])
        self.assertEqual(sanitized["schema"], "beets.recover.diagnostic.v1")
        self.assertFalse(sanitized["decryption_supported"])
        self.assertFalse(sanitized["widevine_decryptable"])
        self.assertTrue(len(sanitized["redacted_fields"]) > 0)
        self.assertIn("org.w3.clearkey", sanitized["key_systems"])
        self.assertIn("com.widevine.alpha", sanitized["key_systems"])
        self.assertEqual(sanitized["key_statuses"], ["expired", "usable"])
        self.assertEqual(sanitized["provenance_status"], "genuinely-DRM")

        dumped = str(sanitized)
        self.assertNotIn("00112233445566778899aabbccddeeff", dumped)
        self.assertNotIn("ffeeddccbbaa99887766554433221100", dumped)
        self.assertNotIn("deadbeefcafebabe0011223344556677", dumped)
        self.assertNotIn("eyJrZXlzIjpbXX0=", dumped)
        self.assertNotIn("secret_cookie_value", dumped)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", dumped)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", dumped)

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
                res = recover_service.verify_and_match_staged_upload(audio_path)
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
                     "acoustid_status": "no_match",
                     "acoustid_match_score": 0.0,
                 }), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]) as mock_mb_search:
                res = recover_service.verify_and_match_staged_upload(audio_path)
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
                res_reject = recover_service.verify_and_match_staged_upload(audio_path)
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
                res_review = recover_service.verify_and_match_staged_upload(audio_path)
            self.assertFalse(res_review["ok"])
            self.assertEqual(res_review["status"], "review_required")
            self.assertEqual(res_review["final_action"], "review")


class SafeStagingAndBeetsImport(unittest.TestCase):
    """Phase 2 (9-10): safe staging validation, non-destructive Beets import,
    existing library file protection, and playlist coverage refresh."""

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
                out = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter)

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

            with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
                 mock.patch.object(recover_service, "recover_integrity_check",
                                   return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
                 mock.patch.object(recover_service, "inspect_audio_file",
                                   return_value={"ok": True, "codec": "flac"}), \
                 mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                     "acoustid_status": "no_match",
                     "acoustid_match_score": 0.0,
                 }), \
                 mock.patch("helpers_mb._mb_recording_search", return_value=[]):
                out = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter)

            self.assertTrue(out["imported"], out)
            self.assertEqual(out["imported_item_id"], 99)
            self.assertEqual(out["status"], "imported")
            self.assertEqual(out["provenance_status"], "imported")
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
                out_skip = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter_skip)
            self.assertFalse(out_skip["imported"])
            self.assertEqual(out_skip["status"], "import_failed")
            self.assertIn("blank_identity", out_skip["error"])

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
                out_noread = recover_service._recover_import_one(str(staged), [], adapter=fake_adapter_noread)
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


if __name__ == "__main__":
    unittest.main()
