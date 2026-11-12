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
        # #654: a header value runs to the end of the line; the scheme is kept.
        self.assert_masked("Authorization: Bearer abc tail\nnext", "Authorization: Bearer [REDACTED]\nnext")

    def test_digest_and_token_authorization(self):
        # #654: only the scheme word was masked.
        self.assert_masked('Authorization: Digest username="u", response="fake-s1"\nnext',
                           "Authorization: Digest [REDACTED]\nnext")
        self.assert_masked("Proxy-Authorization: Token fake-s2 more", "Proxy-Authorization: Token [REDACTED]")
        self.assert_masked("authorization=Token fake-s3 more", "authorization=Token [REDACTED]")

    def test_multi_pair_cookie_header(self):
        # #654: pairs after the first one were shown.
        self.assert_masked("Cookie: a=fake-s4; session=fake-s5\nnext", "Cookie: [REDACTED]\nnext")
        self.assert_masked("Set-Cookie: sid=fake-s6; Path=/; HttpOnly", "Set-Cookie: [REDACTED]")

    def test_quoted_header_value_in_json(self):
        self.assert_masked('{"authorization": "Bearer fake-s7", "x": 1}', '{"authorization": [REDACTED] "x": 1}')

    def test_prefixed_secret_names(self):
        # #654: "\b" and a narrow key list missed these.
        self.assert_masked("access_token=fake-s8 db_password=fake-s9 passwd=fake-s10 pwd=fake-s11 x",
                           "access_token=[REDACTED] db_password=[REDACTED] passwd=[REDACTED] pwd=[REDACTED] x")

    def test_query_value_stops_at_ampersand(self):
        # #654: following parameters survive (test_recover_uploads needs "&keep=1").
        self.assert_masked("GET https://h.invalid/x?oauth_token=fake-s12&keep=1#f failed",
                           "GET https://h.invalid/x?oauth_token=[REDACTED]&keep=1#f failed")
        self.assert_masked("https://h.invalid/?a=1&db_password=fake-s13&b=2",
                           "https://h.invalid/?a=1&db_password=[REDACTED]&b=2")
        self.assert_masked('?token="fake-s16 fake-s17"&x=1', "?token=[REDACTED]&x=1")

    def test_url_password_with_at_sign(self):
        # #654: the part after the first "@" was shown.
        self.assert_masked("GET https://u:p@fake-s14@example.test/x failed",
                           "GET https://[REDACTED]@example.test/x failed")
        self.assert_masked("https://u@x:fake-s15@example.test/", "https://[REDACTED]@example.test/")

    def test_unclosed_quote_stops_at_newline(self):
        # #654: an unclosed quote hid the rest of a traceback.
        self.assert_masked("token: can't parse\nTraceback (most recent call last):\n  File x",
                           "token: [REDACTED]\nTraceback (most recent call last):\n  File x")

    def test_non_secret_names_untouched(self):
        for text in ("bypass=1 compass=2 author=x sort_key=a", "https://h.invalid/?page=2&keep=1"):
            self.assertEqual(redact(text), text)

    def test_url_userinfo_masked(self):
        self.assert_masked("GET https://user:pw@example.test/x failed", "GET https://[REDACTED]@example.test/x failed")

    def test_plain_text_untouched(self):
        for text in ("Connection refused (errno 111)", "tokenizer failed: bad input", "a-a-a-a"):
            self.assertEqual(redact(text), text)

    def test_linear_time(self):
        for text in ("a-" * 50000, "password=" + '"\\' * 50000, "token=" + "'a" * 50000,
                     "&token" * 30000, "?" + "a" * 100000, "://a:" + "@" * 100000,
                     "Cookie" * 30000, "x_" * 50000 + "="):
            start = time.perf_counter()
            redact(text)
            self.assertLess(time.perf_counter() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
