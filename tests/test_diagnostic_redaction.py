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

from flask import Flask

from backend.app_runtime import _redact_security_text


def _load(real_first_pass):
    saved = {name: sys.modules.get(name) for name in ("app", "routes_setup")}
    stub = types.ModuleType("app")
    stub.__routes_setup_test_stub__ = True
    stub.app = Flask(__name__)
    if real_first_pass:
        stub._redact_security_text = _redact_security_text
    sys.modules["app"] = stub
    sys.modules.pop("routes_setup", None)
    try:
        return importlib.import_module("routes_setup")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class DiagnosticRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.redactors = {flag: _load(flag)._redact_diagnostic_text for flag in (True, False)}

    def test_issue_524_values_are_masked_whole(self):
        for text in (
            "password='{x} {y}'", "password='{x},{y}'", "password={x},{y}",
            "Authorization: '{x} {y}'", "authorization='Bearer {x} {y}'",
            "password={x};{y}", "password={x}}}{y}", "password={x}]{y}", "password={x}&{y}",
            "password={x}'{y} {z}'", "token: {x},{y}", "cookie={x},{y}", "Authorization: token {x} {y}",
            'api_key="{x} {y}",{z}',
        ):
            text = text.format(x="FAKEX", y="FAKEY", z="FAKEZ")
            for real, redact in self.redactors.items():
                out = redact(text)
                self.assertNotIn("FAKE", out, (real, text))
                self.assertEqual(redact(out), out, (real, text))

    def test_surrounding_text_is_kept(self):
        redact = self.redactors[True]
        self.assertEqual(redact("connect failed: password=x,y next"), "connect failed: password=[REDACTED] next")
        self.assertEqual(redact("ok\nAuthorization: Bearer x y\nnext"), "ok\nAuthorization: [REDACTED]\nnext")
        self.assertEqual(redact("https://u:x@host/p"), "https://[redacted]@host/p")

    def test_crafted_input_is_linear(self):
        for n in (100_000, 1_000_000):
            for text in (
                "a-" * (n // 2), "a://" * (n // 4), "a:" * (n // 2), "a" * n, "-" * n,
                "token" * (n // 5), "password " * (n // 9), "password=" + " " * n,
                "x://" + "b:" * (n // 2), "https://" + "a-" * (n // 2), "password='" + "\\" * n,
                "Authorization:" + " " * n, "a_" * (n // 2), "pass-" * (n // 5),
            ):
                for real, redact in self.redactors.items():
                    start = time.perf_counter()
                    redact(text)
                    elapsed = time.perf_counter() - start
                    self.assertLess(elapsed, 1.0, (real, n, text[:20]))


if __name__ == "__main__":
    unittest.main()
