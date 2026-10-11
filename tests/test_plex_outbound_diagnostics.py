"""T2: user-facing diagnostics for operator-configured services the outbound
policy refuses, Plex URL redaction and validation, one Plex settings
resolution for status/probe/workflows, and fixed Plex playlist-sync errors.

Before: an OutboundPolicyError surfaced as "Could not reach Plex." (status,
setup probe) or "Lidarr returned an unexpected response" (it is a
ValueError); /api/plex/status echoed PLEX_URL verbatim (user:pass@ or a
?X-Plex-Token= query included); the setup probe and the integration status
read only os.environ while the Plex workflows also read the Beets config
plex: stanza; playlist sync stored str(exc) and ran with a token but no URL.
"""
import contextlib
import http.server
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from backend import config_layers as cl
from backend import provider_boundary
from backend.security import OutboundPolicyError

try:
    from test_routes_setup import _load_routes_setup_against_stub_app
    from test_sec_outbound_configured_trust import _Net
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app
    from tests.test_sec_outbound_configured_trust import _Net

import backend.plex_service as plex_service
import backend.playlist_service as ps

TOKEN = "plex-token-fixture-must-not-leak-91c2"
ENTRY = "192.168.0.250:32400"


def _blocked(entry=ENTRY):
    return OutboundPolicyError("outbound host resolves to a prohibited address", entry=entry)


def _settings(url="http://192.168.0.250:32400", token=TOKEN):
    return {"url": url, "token": token, "section": "", "plex_music_roots": "", "beets_music_root": "/music",
            "plex_scan_timeout": "60", "plex_index_timeout": "60"}


class _Resolvers(unittest.TestCase):
    def use_configured(self, **urls):
        patcher = mock.patch.dict(provider_boundary._CONFIGURED_URL_RESOLVERS,
                                  {k: (lambda v=v: v) for k, v in urls.items()})
        patcher.start()
        self.addCleanup(patcher.stop)


class OutboundBlockHelperTests(_Resolvers):
    def test_other_failures_are_not_a_block(self):
        for exc in (ValueError("x"), OSError("x"), _blocked(entry="")):  # no entry: DNS failure, metadata ...
            self.assertIsNone(provider_boundary.outbound_block(exc, "plex", "http://h:1"))

    def test_block_on_the_configured_url_names_the_entry(self):
        self.use_configured(plex="http://192.168.0.250:32400")
        block = provider_boundary.outbound_block(_blocked(), "plex")
        self.assertEqual(block["reason"], "outbound_not_allowlisted")
        self.assertEqual(block["hint"], f"add {ENTRY} to BEETS_OUTBOUND_ALLOWLIST")
        self.assertEqual(block["error"], f"Plex is blocked by the outbound network policy. "
                                         f"To allow it, add {ENTRY} to BEETS_OUTBOUND_ALLOWLIST.")

    def test_block_on_another_url_also_suggests_the_setting(self):
        self.use_configured(lidarr="http://lidarr.lan:8686")
        block = provider_boundary.outbound_block(_blocked("127.0.0.1:8686"), "lidarr", "http://127.0.0.1:8686/api")
        self.assertEqual(block["hint"], "add 127.0.0.1:8686 to BEETS_OUTBOUND_ALLOWLIST or set LIDARR_URL to it")

    def test_hint_never_carries_credentials_path_or_query(self):
        self.use_configured(plex="")
        block = provider_boundary.outbound_block(
            _blocked(), "plex", f"http://u:{TOKEN}@192.168.0.250:32400/library?X-Plex-Token={TOKEN}#f")
        self.assertNotIn(TOKEN, json.dumps(block))
        self.assertIn(ENTRY, block["hint"])

    def test_provider_health_records_the_reason(self):
        provider_boundary.reset_provider_health()
        self.addCleanup(provider_boundary.reset_provider_health)
        self.use_configured(plex="")
        with mock.patch("urllib.request.urlopen", side_effect=_blocked()):
            with self.assertRaises(OutboundPolicyError):
                with provider_boundary.opened("plex", "http://192.168.0.250:32400/identity", max_attempts=1):
                    pass
        row = provider_boundary.provider_health()["plex"]
        self.assertEqual(row["last_reason"], "outbound_not_allowlisted")
        self.assertEqual(row["last_outcome"], "rejected")
        self.assertIn(ENTRY, row["last_message"])


class PlexStatusTests(unittest.TestCase):
    def status(self, settings, side_effect=None, result=("m", "3", "Music")):
        find = mock.Mock(side_effect=side_effect, return_value=result)
        with mock.patch.object(plex_service, "_plex_settings", return_value=settings), \
                mock.patch.object(plex_service, "_plex_find_music_section", find), \
                mock.patch.object(plex_service, "_plex_section_locations", return_value=[]), \
                mock.patch.object(provider_boundary, "configured_url", return_value=settings["url"]):
            return plex_service._plex_status_payload(force=True)

    def test_outbound_block_gives_reason_and_hint_and_no_secret(self):
        with self.assertLogs(plex_service._app_logger, level="WARNING") as logs:
            payload = self.status(_settings(), side_effect=_blocked())
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["reason"], "outbound_not_allowlisted")
        self.assertEqual(payload["hint"], f"add {ENTRY} to BEETS_OUTBOUND_ALLOWLIST")
        self.assertIn("blocked by the outbound network policy", payload["error"])
        self.assertNotIn(TOKEN, json.dumps(payload) + "\n".join(logs.output))

    def test_other_failures_keep_fixed_messages(self):
        cases = [(urllib.error.HTTPError("u", 401, "x", {}, None), "auth_failed", "Plex token is invalid or expired."),
                 (urllib.error.HTTPError("u", 500, "x", {}, None), "http_error", "Plex returned HTTP 500."),
                 (TimeoutError("slow"), "timeout", "Plex did not answer in time."),
                 (urllib.error.URLError("refused"), "unreachable", "Could not reach Plex."),
                 (_blocked(entry=""), "unreachable", "Could not reach Plex."),
                 (KeyError("LEAK_MARKER"), "plex_error", "The Plex operation failed; see the server log.")]
        for exc, reason, error in cases:
            with self.subTest(reason=reason):
                payload = self.status(_settings(), side_effect=exc)
                self.assertEqual((payload["reason"], payload["error"]), (reason, error))
                self.assertNotIn("LEAK_MARKER", json.dumps(payload))

    def test_url_is_returned_without_userinfo_query_or_fragment(self):
        url = f"http://admin:{TOKEN}@plex.lan:32400/base?X-Plex-Token={TOKEN}#frag"
        payload = self.status(_settings(url=url))
        self.assertEqual(payload["url"], "http://plex.lan:32400/base")
        self.assertNotIn(TOKEN, json.dumps(payload))

    def test_not_configured_without_url(self):
        payload = self.status(_settings(url=""))
        self.assertEqual((payload["configured"], payload["reason"]), (False, "not_configured"))


class _StubSetup(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in ("PLEX_URL", "PLEX_TOKEN"):
            os.environ.pop(key, None)
        self.csrf = mock.patch.object(self.module, "_setup_csrf_failure", return_value=None)
        self.csrf.start()
        self.addCleanup(self.csrf.stop)


class SetupProbeDiagnosticsTests(_StubSetup):
    def raising_opened(self, exc):
        sent = []

        @contextlib.contextmanager
        def fake(provider, req, **kw):
            sent.append((provider, req.full_url, dict(req.header_items())))
            raise exc
            yield  # pragma: no cover

        patcher = mock.patch.object(provider_boundary, "opened", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent

    def test_plex_probe_blocked_returns_reason_and_hint_without_the_token(self):
        sent = self.raising_opened(_blocked())
        with mock.patch.object(plex_service, "_plex_settings", return_value=_settings(url="http://plex.lan:32400")):
            resp = self.client.post("/api/setup/test/plex", json={"url": "http://192.168.0.250:32400", "token": TOKEN})
        body = resp.get_json()
        self.assertEqual((body["ok"], body["status"], body["reason"]), (False, "failed", "outbound_not_allowlisted"))
        self.assertEqual(body["hint"], f"add {ENTRY} to BEETS_OUTBOUND_ALLOWLIST or set PLEX_URL to it")
        self.assertNotIn(TOKEN, resp.get_data(as_text=True))
        self.assertEqual(sent[0][1], "http://192.168.0.250:32400/library/sections")

    def test_plex_probe_uses_the_beets_config_stanza_like_the_workflows(self):  # #7
        sent = self.raising_opened(urllib.error.HTTPError("u", 401, "x", {}, None))
        with mock.patch.object(plex_service, "_plex_settings", return_value=_settings(url="http://plex.lan:32400")), \
                mock.patch("backend.auth_service.probe_may_use_stored_secret", return_value=True):
            body = self.client.post("/api/setup/test/plex", json={}).get_json()
        self.assertEqual(body["reason"], "auth_failed")
        self.assertEqual(sent[0][1], "http://plex.lan:32400/library/sections")
        self.assertEqual(sent[0][2].get("X-plex-token"), TOKEN)

    def test_integration_configured_flag_uses_the_same_resolution(self):  # #7
        with mock.patch.object(plex_service, "_plex_settings", return_value=_settings(url="http://plex.lan:32400")):
            self.assertTrue(self.module._plex_integration_configured())
        with mock.patch.object(plex_service, "_plex_settings", return_value=_settings(url="")):
            self.assertFalse(self.module._plex_integration_configured())

    def test_ai_probe_blocked_returns_reason_and_hint(self):
        self.raising_opened(_blocked("10.0.0.7:11434"))
        resp = self.client.post("/api/setup/test/ai", json={"base_url": "http://10.0.0.7:11434/v1", "api_key": TOKEN})
        body = resp.get_json()
        self.assertEqual(body["reason"], "outbound_not_allowlisted")
        self.assertIn("add 10.0.0.7:11434 to BEETS_OUTBOUND_ALLOWLIST", body["hint"])
        self.assertNotIn(TOKEN, resp.get_data(as_text=True))


class PlexUrlSaveValidationTests(_StubSetup):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / ".env"
        self.env_file.write_text("", encoding="utf-8")
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = Path(tmp.name) / ".env.example"
        pinned = mock.patch.object(cl, "_BOOT_ENV_PINNED", frozenset())
        pinned.start()
        self.addCleanup(pinned.stop)

    def save(self, url):
        return self.client.post("/api/setup/env", json={"variables": {"PLEX_URL": url}})

    def test_credentials_query_and_fragment_are_refused_without_echo(self):
        for url in (f"http://admin:{TOKEN}@plex.lan:32400", f"http://plex.lan:32400/?X-Plex-Token={TOKEN}",
                    "http://plex.lan:32400#x", "http://plex.lan:32400?", "plex.lan:32400", "ftp://plex.lan"):
            with self.subTest(url=url):
                resp = self.save(url)
                self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
                self.assertEqual(resp.get_json()["error"], self.module.PLEX_URL_INVALID_MESSAGE)
                self.assertNotIn(TOKEN, resp.get_data(as_text=True))
                self.assertNotIn("PLEX_URL", self.env_file.read_text(encoding="utf-8"))

    def test_plain_url_and_clearing_still_save(self):
        self.assertEqual(self.save("http://192.168.0.250:32400").status_code, 200)
        self.assertIn("PLEX_URL=http://192.168.0.250:32400", self.env_file.read_text(encoding="utf-8"))
        self.assertEqual(self.save("").status_code, 200)


class OtherProviderDiagnosticsTests(_Resolvers):
    def test_lidarr_route_message_names_the_entry_not_unexpected_response(self):
        import routes_lidarr
        self.use_configured(lidarr="http://192.168.0.9:8686")
        message, status = routes_lidarr._http_error_message(_blocked("192.168.0.9:8686"))
        self.assertEqual(status, 502)
        self.assertIn("Lidarr is blocked by the outbound network policy", message)
        self.assertIn("add 192.168.0.9:8686 to BEETS_OUTBOUND_ALLOWLIST", message)

    def test_lidarr_wanted_reason_names_the_entry(self):
        import backend.acquisition_service as acq
        self.use_configured(lidarr="http://192.168.0.9:8686")
        self.assertIn("add 192.168.0.9:8686", acq._lidarr_wanted_error(_blocked("192.168.0.9:8686")))
        self.assertEqual(acq._lidarr_wanted_error(ValueError("x")), "Lidarr returned an unexpected response")

    def test_slskd_errors_carry_the_fixed_block_message(self):
        import backend.slskd_service as slskd
        self.use_configured(slskd="http://slskd:5030")
        with mock.patch.object(slskd, "SLSKD_API_KEY", TOKEN), \
                mock.patch("urllib.request.urlopen", side_effect=_blocked("slskd:5030")):
            with self.assertRaises(RuntimeError) as ctx:
                slskd._slskd_req("GET", "application")
        self.assertIn("slskd is blocked by the outbound network policy", str(ctx.exception))
        self.assertIn("add slskd:5030 to BEETS_OUTBOUND_ALLOWLIST", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_qbittorrent_status_url_is_redacted(self):
        import backend.acquisition_service as acq
        with mock.patch.object(acq, "QBIT_URL", f"http://u:{TOKEN}@qbit.lan:8080/?k={TOKEN}"):
            self.assertEqual(acq._qbit_status_payload()["url"], "http://qbit.lan:8080/")


class PlaylistSyncPlexTests(unittest.TestCase):
    ITEMS = [{"id": 1, "artist": "A", "title": "T1", "path": "/music/a/1.flac"}]

    def sync(self, settings, find):
        stored = {}
        patches = {
            "_playlist_ensure_state_dirs": mock.Mock(),
            "_playlist_ensure_stable_id": mock.Mock(return_value="pid1"),
            "_playlist_resolve_stable_id": mock.Mock(return_value="pid1"),
            "_playlist_key": mock.Mock(return_value="key1"),
            "_playlist_write_manifest": mock.Mock(return_value={}),
            "_playlist_read_manifest": mock.Mock(return_value={}),
            "_playlist_replace_manifest": mock.Mock(side_effect=lambda n, m: stored.update(m) or m),
            "_playlist_manifest_path": mock.Mock(return_value=Path("m.json")),
            "_playlist_store_track_state": mock.Mock(),
            "_playlist_manifest_track_states": mock.Mock(return_value={}),
            "_playlist_other_live_pids_with_name": mock.Mock(return_value=[]),
            "_plex_settings": mock.Mock(return_value=settings),
            "_plex_find_music_section": find,
            "_plex_request": mock.Mock(side_effect=AssertionError("no Plex call expected")),
        }
        log = []
        with mock.patch.multiple(ps, **patches), \
                mock.patch.object(provider_boundary, "configured_url", return_value=settings["url"]), \
                mock.patch.object(ps.composite_workflows, "export_playlist_m3u", return_value={"ok": True}):
            result = ps._create_playlist_outputs("Mix", self.ITEMS, log=log)
        return result["plex"], log

    def test_token_without_url_does_not_sync(self):  # #7
        find = mock.Mock(side_effect=AssertionError("must not call Plex"))
        plex, _ = self.sync(_settings(url=""), find)
        find.assert_not_called()
        self.assertFalse(plex.get("error"))

    def test_failure_stores_fixed_message_reason_and_hint(self):  # #8
        plex, log = self.sync(_settings(), mock.Mock(side_effect=_blocked()))
        self.assertEqual(plex["status"], "failed")
        self.assertEqual(plex["reason"], "outbound_not_allowlisted")
        self.assertEqual(plex["hint"], f"add {ENTRY} to BEETS_OUTBOUND_ALLOWLIST")
        self.assertEqual(plex["action_needed"], plex["hint"])
        self.assertNotIn("prohibited address", json.dumps(plex) + "\n".join(log))

    def test_unexpected_failure_text_never_reaches_manifest_or_log(self):  # #8
        plex, log = self.sync(_settings(), mock.Mock(side_effect=RuntimeError("LEAK_MARKER /internal/path")))
        self.assertEqual((plex["reason"], plex["error"]),
                         ("plex_error", "The Plex operation failed; see the server log."))
        self.assertNotIn("LEAK_MARKER", json.dumps(plex) + "\n".join(log))


class _PlexHandler(http.server.BaseHTTPRequestHandler):
    """A tiny Plex: sections, a track search, playlist create and append."""

    def _reply(self, payload):
        self.server.seen.append((self.command, self.path, self.headers.get("X-Plex-Token")))
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.headers.get("X-Plex-Token") != TOKEN:
            self.send_error(401)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/library/sections":
            self._reply({"MediaContainer": {"machineIdentifier": "mid1", "Directory": [
                {"type": "movie", "key": "1", "title": "Films"},
                {"type": "artist", "key": "3", "title": "Music"}]}})
        elif path == "/library/sections/3/all":
            self._reply({"MediaContainer": {"Metadata": [
                {"ratingKey": "900", "grandparentTitle": "Other", "title": "Song"},
                {"ratingKey": "901", "grandparentTitle": "Artist", "title": "Song"}]}})
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        self._reply({"MediaContainer": {"Metadata": [{"ratingKey": "555"}]}})

    def do_PUT(self):  # noqa: N802
        self._reply({})

    def log_message(self, *args):
        pass


class PlexEndToEndWithoutAllowlistTests(unittest.TestCase):
    """The production failure: Plex on a private LAN address and no
    BEETS_OUTBOUND_ALLOWLIST entry for it. Through the real opened() ->
    secure_urlopen path (DNS and the socket faked, the policy real)."""

    def test_sections_track_match_and_playlist_write_need_no_allowlist_entry(self):
        env = mock.patch.dict(os.environ, {"PLEX_URL": "http://plex.lan:32400", "PLEX_TOKEN": TOKEN,
                                           "BEETS_OUTBOUND_ALLOWLIST": "127.0.0.1:8337"})
        env.start()
        self.addCleanup(env.stop)
        for patcher in (mock.patch.object(plex_service, "_plex_config_from_beets", return_value={}),
                        mock.patch.object(ps, "PLEX_PLAYLIST_CHUNK_SIZE", 1)):
            patcher.start()
            self.addCleanup(patcher.stop)
        net = _Net(self, {"plex.lan": ["192.168.50.10"]})
        net.server.RequestHandlerClass = _PlexHandler
        provider_boundary.reset_provider_health()
        self.addCleanup(provider_boundary.reset_provider_health)

        self.assertEqual(plex_service._plex_find_music_section(force=True), ("mid1", "3", "Music"))
        self.assertEqual(plex_service._plex_find_track("3", "Artist", "Song"), "901")
        added, key = ps._plex_create_audio_playlist("mid1", "Mix", ["901", "902"])

        self.assertEqual((added, key), (2, "555"))
        self.assertEqual({c[:2] for c in net.connected}, {("192.168.50.10", 32400)})
        self.assertEqual([(m, urllib.parse.urlsplit(p).path) for m, p, _ in net.server.seen], [
            ("GET", "/library/sections"), ("GET", "/library/sections/3/all"),
            ("POST", "/playlists"), ("PUT", "/playlists/555/items")])
        for _method, path, header in net.server.seen:
            self.assertEqual(header, TOKEN)
            self.assertNotIn(TOKEN, path)
            self.assertNotIn("X-Plex-Token", path)
        self.assertEqual(provider_boundary.provider_health()["plex"]["last_outcome"], "confirmed")


if __name__ == "__main__":
    unittest.main()
