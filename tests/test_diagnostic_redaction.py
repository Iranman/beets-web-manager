"""Diagnostic redaction keeps no part of a secret and runs in linear time (#524).

routes_setup is loaded against a stub `app` (see test_routes_setup.py). The
stub carries the real `_redact_security_text`, so the real app's extra pass
runs too, as it does in production.
"""
import importlib
import sys
import time
import types
import unittest
import unittest.mock as mock

from flask import Flask

from backend.app_runtime import _redact_security_text


def _load(real_first_pass):
    stub = types.ModuleType("app")
    stub.__routes_setup_test_stub__ = True
    stub.app = Flask(__name__)
    if real_first_pass:
        stub._redact_security_text = _redact_security_text
    # The redactor imports `app` on each call, so the stub stays in place for
    # the calls too (or the real app.py would load).
    with mock.patch.dict(sys.modules, {"app": stub}):
        sys.modules.pop("routes_setup", None)
        module = importlib.import_module("routes_setup")

    def redact(text):
        with mock.patch.dict(sys.modules, {"app": stub}):
            return module._redact_diagnostic_text(text)
    return redact


class DiagnosticRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.redactors = {flag: _load(flag) for flag in (True, False)}

    def test_issue_524_values_are_masked_whole(self):
        for text in (
            "password='{x} {y}'", "password='{x},{y}'", "password={x},{y}",
            "Authorization: '{x} {y}'", "authorization='Bearer {x} {y}'",
            "password={x};{y}", "password={x}}}{y}", "password={x}]{y}", "password={x}&{y}",
            "password={x}'{y} {z}'", "token: {x},{y}", "cookie={x},{y}", "Authorization: token {x} {y}",
            'api_key="{x} {y}",{z}', "password={x} {y}",
        ):
            text = text.format(x="FAKEX", y="FAKEY", z="FAKEZ")
            for real, redact in self.redactors.items():
                out = redact(text)
                self.assertNotIn("FAKE", out, (real, text))
                self.assertEqual(redact(out), out, (real, text))

    def test_surrounding_text_is_kept(self):
        redact = self.redactors[True]
        self.assertEqual(redact("connect failed: password=x,y next"), "connect failed: password=[REDACTED] next")
        # The scheme word is masked too (the import-review tests forbid it in logs).
        self.assertEqual(redact("ok\nAuthorization: Bearer x y\nnext"), "ok\nAuthorization: [REDACTED]\nnext")
        self.assertEqual(redact("https://u:x@host/p"), "https://[redacted]@host/p")

    def test_issue_654_residuals(self):
        for text in ("https://u:FAKEX@FAKEY@host/p", "access_token=FAKEX db_password=FAKEY",
                     "Cookie: a=FAKEX; b=FAKEY", "Authorization: Digest username=u, response=FAKEX",
                     # PR #659 QA: a closed quote spans newlines.
                     'password="FAKEX\nFAKEY" tail', 'client_secret: "-----BEGIN\nFAKEX\n-----END"',
                     # #660
                     "password='FAKEX\nFAKEY'\nnext", "token=Negotiate FAKEX tail", "api_key: Digest FAKEX",
                     'x?token="FAKEX\nFAKEY"&keep=1', "x?token=FAKEX&FAKEY&keep=1",
                     # #662
                     '{"url": "x?token=FAKEX&FAKEY"}', "x?token='FAKEX\nFAKEY'&keep=1"):
            for real, redact in self.redactors.items():
                out = redact(text)
                self.assertNotIn("FAKE", out, (real, text))
                self.assertEqual(redact(out), out, (real, text))

    def test_issue_662_unclosed_quote_keeps_traceback(self):
        tail = "\nTraceback (most recent call last):\nKeyError: 'foo'\nnext"
        for real, redact in self.redactors.items():
            out = redact("password='FAKEX" + tail).replace("[REDACTED]", "[redacted]")
            self.assertEqual(out, "password=[redacted]" + tail, real)

    def test_crafted_input_is_linear(self):
        for n in (100_000, 1_000_000):
            for text in (
                "a-" * (n // 2), "a://" * (n // 4), "a:" * (n // 2), "a" * n, "-" * n,
                "token" * (n // 5), "password " * (n // 9), "password=" + " " * n,
                "x://" + "b:" * (n // 2), "https://" + "a-" * (n // 2), "password='" + "\\" * n,
                "Authorization:" + " " * n, "a_" * (n // 2), "pass-" * (n // 5),
                "token='\n" * (n // 8), "token='" + "\\'" * (n // 2), "?token=x" + "&a" * (n // 2),
            ):
                for real, redact in self.redactors.items():
                    start = time.perf_counter()
                    redact(text)
                    elapsed = time.perf_counter() - start
                    # Backtracking on 1M chars takes minutes; 5s only absorbs slow CI runners.
                    self.assertLess(elapsed, 5.0, (real, n, text[:20]))


if __name__ == "__main__":
    unittest.main()
