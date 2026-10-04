"""POST /api/setup/test/acoustid classifies AcoustID answers by error code.

AcoustID error codes (acoustid-server acoustid/api/errors.py):
3 invalid fingerprint (400), 4 invalid API key (400), 5 internal error (500),
8 invalid duration (400), 13 service unavailable (503), 14 too many
requests (429). The lookup handler checks the client key before the
fingerprint, so 3/8 mean the key was accepted. The probe sends a dummy
fingerprint, so a valid key commonly comes back as code 3 over HTTP 400.

Every answer is exercised both as a JSON body on HTTP 200 and as an
urllib HTTPError carrying the same body, which is how provider_boundary
re-raises a non-2xx answer.
"""
import importlib
import io
import json
import socket
import sys
import types
import unittest
import unittest.mock as mock
import urllib.error


_MISSING = object()
_STUBBED = ("app", "routes_setup", "routes_submissions", "routes_jobs", "routes_lidarr")
_KEY = "probe-secret-key-0123"
_DIAGNOSTICS = {
    "remote_reachable": True,
    "loaded_plugins": ["chroma"],
    "fpcalc_path": "/usr/bin/fpcalc",
    "capabilities": {
        "acoustid_lookup": {
            "fpcalc_available": True,
            "chroma_loaded": True,
            "pyacoustid_available": True,
        }
    },
}


def _load(test_case):
    from flask import Flask
    snapshot = {name: sys.modules.get(name, _MISSING) for name in _STUBBED}

    def restore():
        for name, module in snapshot.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    stub = types.ModuleType("app")
    stub.__routes_setup_test_stub__ = True
    stub.app = Flask(__name__)
    sys.modules["app"] = stub
    sys.modules.pop("routes_setup", None)
    module = importlib.import_module("routes_setup")
    test_case.addCleanup(restore)
    return stub.app, module


class _Response:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _error_body(code, message="provider says something"):
    return json.dumps({"status": "error", "error": {"code": code, "message": message}}).encode()


def _http_error(status, body: bytes):
    return urllib.error.HTTPError("https://api.acoustid.org/v2/lookup", status, "err", {}, io.BytesIO(body))


class SetupAcoustidProbeTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load(self)
        self.client = self.flask_app.test_client()

    def _probe(self, *, body=None, raises=None):
        if raises is not None:
            urlopen = mock.patch.object(self.module.urllib.request, "urlopen", side_effect=raises)
        else:
            urlopen = mock.patch.object(self.module.urllib.request, "urlopen", return_value=_Response(body))
        with mock.patch.object(self.module, "_beets_plugin_diagnostics", return_value=_DIAGNOSTICS), urlopen, \
             mock.patch.object(self.flask_app.logger, "warning") as warn:
            r = self.client.post("/api/setup/test/acoustid", json={"api_key": _KEY})
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertNotIn(_KEY, r.get_data(as_text=True))
        for call in warn.call_args_list:
            self.assertNotIn(_KEY, repr(call))
        self.assertNotIn("provider says something", r.get_data(as_text=True))
        return payload

    def _both_forms(self, http_status, code):
        body = _error_body(code)
        yield "json-200", self._probe(body=body)
        yield f"http-{http_status}", self._probe(raises=_http_error(http_status, body))

    # status ok, or code 3/8 -> ready
    def test_status_ok_is_ready(self):
        body = self._probe(body=b'{"status": "ok", "results": []}')
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "ready")

    def test_invalid_fingerprint_code_3_means_key_accepted(self):
        for form, body in self._both_forms(400, 3):
            with self.subTest(form=form):
                self.assertTrue(body["ok"])
                self.assertEqual(body["status"], "ready")

    def test_invalid_duration_code_8_means_key_accepted(self):
        for form, body in self._both_forms(400, 8):
            with self.subTest(form=form):
                self.assertTrue(body["ok"])
                self.assertEqual(body["status"], "ready")

    # code 4 -> key rejected
    def test_invalid_api_key_code_4_is_rejected_key(self):
        for form, body in self._both_forms(400, 4):
            with self.subTest(form=form):
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["reason"], "auth_failed")
                self.assertIn("API key was rejected", body["error"])

    # code 5/13 or HTTP 5xx -> service unavailable
    def test_internal_error_code_5_is_service_unavailable(self):
        for form, body in self._both_forms(500, 5):
            with self.subTest(form=form):
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["reason"], "unavailable")
                self.assertIn("service unavailable", body["error"])

    def test_service_unavailable_code_13_is_service_unavailable(self):
        for form, body in self._both_forms(503, 13):
            with self.subTest(form=form):
                self.assertFalse(body["ok"])
                self.assertEqual(body["reason"], "unavailable")
                self.assertIn("service unavailable", body["error"])

    def test_http_5xx_without_json_body_is_service_unavailable(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                body = self._probe(raises=_http_error(status, b"<html>Bad Gateway</html>"))
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["reason"], "unavailable")
                self.assertIn("service unavailable", body["error"])

    # code 14 or HTTP 429 -> rate limited
    def test_too_many_requests_code_14_is_rate_limited(self):
        for form, body in self._both_forms(429, 14):
            with self.subTest(form=form):
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["reason"], "rate_limited")
                self.assertIn("rate limited", body["error"])

    def test_http_429_without_body_is_rate_limited(self):
        body = self._probe(raises=_http_error(429, b""))
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "rate_limited")
        self.assertIn("rate limited", body["error"])

    # unknown code -> failed
    def test_unknown_code_fails_without_echoing_provider_text(self):
        for form, body in self._both_forms(400, 99):
            with self.subTest(form=form):
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["reason"], "provider_error")
                self.assertIn("code 99", body["error"])

    def test_error_body_without_code_fails(self):
        body = self._probe(body=b'{"status": "error", "error": {"message": "provider says something"}}')
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "failed")

    def test_malformed_body_fails(self):
        for raw in (b"not json", b"[]", b'{"status": "weird"}'):
            with self.subTest(raw=raw):
                body = self._probe(body=raw)
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["reason"], "bad_response")

    def test_http_400_with_malformed_body_fails(self):
        body = self._probe(raises=_http_error(400, b"not json"))
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "failed")
        self.assertNotEqual(body["error"], "Could not reach AcoustID.")

    # network/timeout -> could not reach
    def test_timeout_is_unreachable(self):
        for exc in (socket.timeout("timed out"), TimeoutError("timed out")):
            with self.subTest(exc=type(exc).__name__):
                body = self._probe(raises=exc)
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["error"], "Could not reach AcoustID.")

    def test_network_error_is_unreachable(self):
        body = self._probe(raises=urllib.error.URLError("name resolution failed"))
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "Could not reach AcoustID.")


if __name__ == "__main__":
    unittest.main()
