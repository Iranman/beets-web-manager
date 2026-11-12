"""_redact_security_text masks the whole secret value in one pass (#647)."""
import time
import unittest

from backend.app_runtime import _redact_security_text as redact


class SecurityTextRedactionTests(unittest.TestCase):
    def assert_masked(self, text, expected):
        out = redact(text)
        self.assertEqual(out, expected)
        self.assertEqual(redact(out), out, "not idempotent in one pass")

    def test_bare_value_runs_to_whitespace(self):
        self.assert_masked("password=x,S tail", "password=[REDACTED] tail")

    def test_quoted_value_runs_to_closing_quote(self):
        self.assert_masked("token='x S' tail", "token=[REDACTED] tail")

    def test_json_value_with_escaped_quote(self):
        out = redact('{"password":"x\\"S"} tail')
        self.assertNotIn("S", out.replace("REDACTED", ""))
        self.assertEqual(redact(out), out)

    def test_nbsp_stays_inside_value(self):
        self.assert_masked("password=x\xa0S tail", "password=[REDACTED] tail")

    def test_already_redacted_with_tail_is_remasked(self):
        self.assert_masked("password=[REDACTED],S tail", "password=[REDACTED] tail")

    def test_bearer_header(self):
        self.assert_masked("Authorization: Bearer abc tail", "Authorization: [REDACTED] tail")

    def test_url_userinfo_masked(self):
        self.assert_masked("GET https://user:pw@example.test/x failed", "GET https://[REDACTED]@example.test/x failed")

    def test_plain_text_untouched(self):
        for text in ("Connection refused (errno 111)", "tokenizer failed: bad input", "a-a-a-a"):
            self.assertEqual(redact(text), text)

    def test_linear_time(self):
        for text in ("a-" * 50000, "password=" + '"\\' * 50000, "token=" + "'a" * 50000):
            start = time.perf_counter()
            redact(text)
            self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
