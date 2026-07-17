import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
CLIENT = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
LIB_API = (ROOT / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.arrs.yml").read_text(encoding="utf-8")
CONFIG = (ROOT / "config.yaml.example").read_text(encoding="utf-8")
PACKAGE = (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")


class SecurityHardeningTests(unittest.TestCase):
    def test_flask_auth_boundary_is_global_and_exact_allowlist(self):
        self.assertIn("@app.before_request", APP)
        self.assertIn("def _enforce_security_boundary", APP)
        self.assertIn("_AUTH_PUBLIC_ENDPOINTS = {", APP)
        self.assertIn('(\"GET\", \"health\")', APP)
        self.assertIn('(\"GET\", \"react_assets\")', APP)
        self.assertIn('(\"GET\", \"react_next_static\")', APP)
        self.assertIn('("GET", "favicon")', APP)
        self.assertIn('("HEAD", "favicon")', APP)
        self.assertIn("return (method, endpoint) in _AUTH_PUBLIC_ENDPOINTS", APP)
        self.assertNotIn("startswith(\"/api\")", APP)
        self.assertIn("_security_auth_configured()", APP)
        self.assertIn("_auth_secret_is_usable", APP)
        self.assertIn("_PLACEHOLDER_AUTH_SECRETS", APP)

    def test_csrf_and_security_headers_are_enforced(self):
        self.assertIn("def _csrf_request_allowed", APP)
        self.assertIn("X-Beets-CSRF", APP)
        self.assertIn("browser_same_origin and request.headers.get(\"X-Beets-CSRF\") == \"1\"", APP)
        self.assertNotIn("if request.headers.get(\"X-Beets-CSRF\") == \"1\":\n        return True", APP)
        self.assertIn("origin.strip().lower() == \"null\"", APP)
        self.assertIn("_explicit_authorization_header_present", APP)
        self.assertIn("def _set_security_headers", APP)
        self.assertIn("def _auth_failure_rate_limit_response", APP)
        self.assertIn("limited = _auth_failure_rate_limit_response()", APP)
        self.assertIn("def _client_ip_is_lan", APP)
        self.assertIn("if _client_ip_is_lan():", APP)
        self.assertIn("ip.is_private or ip.is_loopback", APP)
        self.assertIn("Content-Security-Policy", APP)
        self.assertIn("def _content_security_policy", APP)
        self.assertIn("def _inline_script_csp_hashes", APP)
        self.assertIn("response.get_data(as_text=True)", APP)
        self.assertIn("sha256-", APP)
        self.assertIn("X-Content-Type-Options", APP)
        self.assertIn("frame-ancestors 'self'", APP)
        self.assertIn("'X-Beets-CSRF': '1'", CLIENT)
        self.assertIn("'X-Beets-CSRF': '1'", LIB_API)


    def test_rate_limit_identity_does_not_trust_spoofed_proxy_headers(self):
        self.assertIn("def _request_client_identity", APP)
        self.assertIn("direct_peer_is_trusted(peer, _trusted_proxy_cidrs())", APP)
        self.assertIn("BEETS_TRUSTED_PROXIES", APP)
        self.assertIn("X-Forwarded-For", APP)
        self.assertIn("X-Real-IP", APP)
        self.assertIn("return peer or \"unknown\"", APP)
        self.assertIn("def _rate_limited", APP)
        self.assertIn("Rate limit exceeded; retry later", APP)
        self.assertIn("BEETS_AUTH_RATE_LIMIT", APP)
    def test_config_and_status_responses_are_redacted(self):
        self.assertIn("def _redact_config_content", APP)
        self.assertIn('"content": _redact_config_content(text)', APP)
        self.assertIn("Refusing to save redacted secret placeholders", APP)
        self.assertIn("def _redacted_ytdlp_rejection", APP)
        self.assertIn('"cookie_file": ""', APP)
        self.assertIn('"cookies_from_browser": ""', APP)
        self.assertIn('"cookie_rejection": _redacted_ytdlp_rejection(cookie_rejected)', APP)
        self.assertNotIn('"error": str(exc) or "Unexpected server error"', APP)
        self.assertIn('"error": "Unexpected server error"', APP)

    def test_public_static_routes_cannot_traverse(self):
        self.assertIn("def _safe_static_file", APP)
        self.assertIn('".." in candidate.parts', APP)
        self.assertIn("target.relative_to(root)", APP)
        self.assertIn("target = _safe_static_file(base, filename)", APP)
        self.assertIn('target = _safe_static_file(REACT_DIST_DIR / "_next" / "static", filename)', APP)
        self.assertNotIn("target = base / filename", APP)

    def test_committed_configs_do_not_contain_known_leaked_secret_patterns(self):
        combined = CONFIG + "\n" + COMPOSE
        for pattern in ("sk-proj", "K8ter", "6bHjj", "0be70", "HWxo", "f3bdb"):
            self.assertNotIn(pattern, combined)
        self.assertRegex(COMPOSE, r"BEETS_WEB_AUTH_TOKEN: \"\$\{BEETS_WEB_AUTH_TOKEN:\?set a strong owner token\}\"")
        self.assertRegex(COMPOSE, r"PUID: \$\{PUID:-1000\}")
        self.assertRegex(COMPOSE, r"PGID: \$\{PGID:-1000\}")
        self.assertIn('OPENAI_API_KEY: "${OPENAI_API_KEY:?set in .env}"', COMPOSE)
        self.assertIn('PLEX_TOKEN: "${PLEX_TOKEN:?set in .env}"', COMPOSE)
        self.assertIn('LIDARR_API_KEY: "${LIDARR_API_KEY:?set in .env}"', COMPOSE)
        self.assertIn('DIGARR_INITIAL_PASSWORD: "${DIGARR_INITIAL_PASSWORD:?set in .env}"', COMPOSE)
        self.assertIn('POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?set in .env}"', COMPOSE)

    def test_frontend_dependencies_are_pinned(self):
        self.assertNotIn('\"latest\"', PACKAGE)
        self.assertNotRegex(PACKAGE, r'\"\\^[^\"]+\"')

    def test_sensitive_routes_are_in_endpoint_inventory_source(self):
        for route in (
            '@app.get("/api/config")',
            '@app.post("/api/config")',
            '@app.post("/api/plugins/run")',
            '@app.post("/api/library/music-format/replace")',
            '@app.post("/api/import/review-folder/delete")',
            '@app.delete("/api/playlists/<path:name>")',
        ):
            self.assertIn(route, APP)
        protected_routes = re.findall(r"@app\.(?:post|delete|put|patch)\(", APP)
        self.assertGreater(len(protected_routes), 50)


if __name__ == "__main__":
    unittest.main()
