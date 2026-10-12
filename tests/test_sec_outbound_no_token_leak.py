"""v0.3.0 outbound regression: a refused request never puts a token from the
URL (userinfo, query, fragment) into the error or the log, on the operator
path (validate_outbound_url) and on the public path (resolve_public_target)."""
import logging
import socket
import unittest
from unittest import mock

from backend.security import (
    OutboundPolicy, OutboundPolicyError, parse_outbound_allowlist, resolve_public_target, validate_outbound_url,
)

TOKEN = "tok-must-not-leak-5e81"


def _dns(*ips):
    return lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]


class NoTokenInRefusalTests(unittest.TestCase):
    URLS = (
        (f"http://192.168.1.9:32400/library?X-Plex-Token={TOKEN}#{TOKEN}", "192.168.1.9"),
        (f"http://metadata.example/latest?token={TOKEN}", "169.254.169.254"),
        (f"http://localhost:8686/api?apikey={TOKEN}", "127.0.0.1"),
        (f"http://user:{TOKEN}@10.0.0.4:8080/", "10.0.0.4"),
    )

    def test_operator_and_public_paths(self):
        for check in (lambda u: validate_outbound_url(u, policy=OutboundPolicy(parse_outbound_allowlist(""))),
                      resolve_public_target):
            for url, ip in self.URLS:
                with self.subTest(url=url), mock.patch("backend.security.socket.getaddrinfo", _dns(ip)):
                    with self.assertLogs("beets_web.security", level="DEBUG") as logs, \
                            self.assertRaises(OutboundPolicyError) as ctx:
                        logging.getLogger("beets_web.security").debug("start")  # assertLogs needs one record
                        check(url)
                    self.assertNotIn(TOKEN, str(ctx.exception))
                    self.assertNotIn(TOKEN, repr(getattr(ctx.exception, "entry", "")))
                    self.assertNotIn(TOKEN, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
