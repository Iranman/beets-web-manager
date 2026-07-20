"""Tests for the AI-optional matching fallback, startup integration
validation, password strength enforcement, and AUTH_TOKEN auto-generation
work.

Background: previously, `_ai_suggest_folder_internal` / `_ai_suggest_album_internal`
/ the item-level `/api/items/<iid>/ai-suggest` endpoint all bailed out with
`ok=False` the moment `OPENAI_API_KEY` was missing -- *before* running any of
the MusicBrainz search or AcoustID fingerprinting they're fully capable of
doing on their own. That meant a missing/invalid AI key didn't just disable
an enhancement, it silently disabled the entire automatic import-matching
pipeline. Same root shape of bug for a live OpenAI failure (401/403/timeout/
rate limit/refusal) discovered *after* MB/AcoustID work had already run --
that work was thrown away too. Fixed by gating only the actual OpenAI network
call on key presence, and falling back to the top-ranked MusicBrainz/AcoustID
candidate (with downgraded confidence and a clear reason string) instead of
returning failure.

Also covers: `_enforce_security_boundary`'s pre-existing 503 lockout when
neither BEETS_WEB_AUTH_TOKEN nor BEETS_WEB_PASSWORD is configured (which used
to also block the setup wizard itself, leaving no way in) -- fixed with an
automatic secure-token bootstrap; BEETS_WEB_PASSWORD strength validation
added to routes_setup.py's env-save path; and independent live-status
reporting for AI/MusicBrainz/AcoustID/Plex in the System page.
"""
import re
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
SETUP_SOURCE = (ROOT / "routes_setup.py").read_text(encoding="utf-8")
SYSTEM_SOURCE = (ROOT / "frontend" / "src" / "views" / "System.tsx").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
README_SOURCE = (ROOT / "README.md").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class ClassifyOpenAiErrorBehaviorTests(unittest.TestCase):
    """Real behavioral test: extract _classify_openai_error and run it
    against constructed exceptions, rather than just grepping for it."""

    @classmethod
    def setUpClass(cls):
        fn_src = _function_source(
            APP_SOURCE, "def _classify_openai_error(exc: Exception) -> str:", "\n\n\ndef _ai_suggest_album_internal("
        )
        import urllib.error as _urllib_error
        import urllib as _urllib
        namespace = {"urllib": _urllib}
        namespace["urllib"].error = _urllib_error
        exec(compile(fn_src, "<classify_openai_error>", "exec"), namespace)
        cls._classify = staticmethod(namespace["_classify_openai_error"])
        cls._HTTPError = _urllib_error.HTTPError
        cls._URLError = _urllib_error.URLError

    def test_401_is_classified_as_key_rejected(self):
        exc = self._HTTPError("https://api.openai.com/v1/chat/completions", 401, "Unauthorized", {}, None)
        self.assertIn("rejected the API key", self._classify(exc))

    def test_403_is_classified_as_key_rejected(self):
        exc = self._HTTPError("https://api.openai.com/v1/chat/completions", 403, "Forbidden", {}, None)
        self.assertIn("rejected the API key", self._classify(exc))

    def test_429_is_classified_as_rate_limited(self):
        exc = self._HTTPError("https://api.openai.com/v1/chat/completions", 429, "Too Many Requests", {}, None)
        self.assertIn("rate-limited", self._classify(exc))

    def test_404_is_classified_as_model_not_found(self):
        exc = self._HTTPError("https://api.openai.com/v1/chat/completions", 404, "Not Found", {}, None)
        self.assertIn("model was not found", self._classify(exc))

    def test_other_http_status_includes_code(self):
        exc = self._HTTPError("https://api.openai.com/v1/chat/completions", 500, "Server Error", {}, None)
        self.assertIn("500", self._classify(exc))

    def test_timeout_error_is_classified_as_timeout(self):
        self.assertIn("timed out", self._classify(TimeoutError("timed out")))

    def test_url_error_is_classified_as_unreachable(self):
        self.assertIn("unreachable", self._classify(self._URLError("connection refused")))

    def test_unknown_exception_still_returns_readable_string(self):
        result = self._classify(ValueError("weird provider response"))
        self.assertIn("weird provider response", result)


class FolderAiSuggestFallbackSourceTests(unittest.TestCase):
    """_ai_suggest_folder_internal is the primary automatic import-matching
    path (called from the review-queue AI-suggest route, library_import_all's
    discovery phase, and AI batch import)."""

    @classmethod
    def setUpClass(cls):
        cls.fn = _function_source(
            APP_SOURCE, "def _ai_suggest_folder_internal(folder_path: str) -> dict:", "\n\ndef _candidate_track_local_candidates("
        )

    def test_does_not_return_early_on_missing_api_key(self):
        # The historical bug: `if not api_key: return {"ok": False, ...}`
        # right after reading OPENAI_API_KEY, before any MB/AcoustID work.
        self.assertNotRegex(
            self.fn,
            r'api_key = os\.environ\.get\("OPENAI_API_KEY", ""\)\s*\n\s*if not api_key:\s*\n\s*return',
        )
        self.assertIn("ai_available = bool(api_key)", self.fn)

    def test_mb_and_acoustid_work_happens_unconditionally(self):
        # These must appear textually BEFORE the gated `if ai_available:` AI
        # call block, proving they aren't skipped when there's no key.
        ai_gate_pos = self.fn.index("if ai_available:")
        mb_search_pos = self.fn.index("_mb_release_search(")
        acoustid_pos = self.fn.index("_acoustid_multi_file(")
        self.assertLess(mb_search_pos, ai_gate_pos)
        self.assertLess(acoustid_pos, ai_gate_pos)

    def test_falls_back_to_top_mb_candidate_with_clear_reason(self):
        self.assertIn("if sug is None:", self.fn)
        self.assertIn("top = mb_candidates[0]", self.fn)
        self.assertIn("Matched using MusicBrainz and AcoustID (AI unavailable:", self.fn)

    def test_uses_shared_error_classifier_on_request_failure(self):
        self.assertIn("ai_unavailable_reason = _classify_openai_error(exc)", self.fn)

    def test_refusal_is_treated_as_unavailable_not_hard_failure(self):
        self.assertIn('ai_unavailable_reason = f"AI refusal: {msg[\'refusal\'][:200]}"', self.fn)

    def test_ai_availability_flags_are_stamped_onto_suggestion(self):
        self.assertIn('sug["ai_available"] = ai_available', self.fn)
        self.assertIn('sug["ai_unavailable_reason"] = ai_unavailable_reason', self.fn)

    def test_identity_gate_still_runs_against_fallback_candidate(self):
        # The artist-mismatch safety gate must apply regardless of whether
        # `sug` came from AI or from the MB-only fallback.
        fallback_pos = self.fn.index("if sug is None:")
        identity_gate_pos = self.fn.index("identity_rejection_reason = \"\"")
        self.assertLess(fallback_pos, identity_gate_pos)


class AlbumAiSuggestFallbackSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fn = _function_source(
            APP_SOURCE,
            "def _ai_suggest_album_internal(",
            "\n@app.post(\"/api/albums/<int:aid>/ai-suggest\")",
        )

    def test_does_not_return_early_on_missing_api_key(self):
        self.assertNotRegex(
            self.fn,
            r'api_key = os\.environ\.get\("OPENAI_API_KEY", ""\)\s*\n\s*if not api_key:\s*\n\s*return',
        )
        self.assertIn("ai_available = bool(api_key)", self.fn)

    def test_falls_back_to_top_mb_candidate(self):
        self.assertIn("if sug is None:", self.fn)
        self.assertIn("top = mb_candidates[0]", self.fn)
        self.assertIn("Matched using MusicBrainz and AcoustID (AI unavailable:", self.fn)

    def test_route_no_longer_gates_on_key_before_delegating(self):
        route_src = _function_source(
            APP_SOURCE,
            '@app.post("/api/albums/<int:aid>/ai-suggest")',
            "\n\ndef _load_album_mb_suggestions",
        )
        self.assertNotIn("OPENAI_API_KEY not configured", route_src)
        self.assertIn("_ai_suggest_album_internal(album, log, existing_album_id=aid)", route_src)


class ItemAiSuggestFallbackSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fn = _function_source(
            APP_SOURCE, "def ai_suggest(iid):", "\n\ndef _discogs_track_search("
        )

    def test_does_not_return_early_on_missing_api_key(self):
        self.assertNotRegex(
            self.fn,
            r'api_key = os\.environ\.get\("OPENAI_API_KEY", ""\)\s*\n\s*if not api_key:\s*\n\s*return',
        )
        self.assertIn("ai_available = bool(api_key)", self.fn)

    def test_acoustid_mb_discogs_gathering_happens_before_ai_gate(self):
        ai_gate_pos = self.fn.index("if ai_available:")
        acoustid_pos = self.fn.index("_acoustid_lookup_cached(item_path)")
        mb_pos = self.fn.index("_mb_recording_search(")
        discogs_pos = self.fn.index("_discogs_track_search(")
        self.assertLess(acoustid_pos, ai_gate_pos)
        self.assertLess(mb_pos, ai_gate_pos)
        self.assertLess(discogs_pos, ai_gate_pos)

    def test_falls_back_to_top_candidate_when_ai_unavailable(self):
        self.assertIn("if suggestions is None:", self.fn)
        self.assertIn("if mb_candidates:", self.fn)
        self.assertIn("Matched using MusicBrainz and AcoustID (AI unavailable:", self.fn)

    def test_handles_the_no_candidates_case_too(self):
        self.assertIn("No MusicBrainz/AcoustID candidates found, and AI is unavailable", self.fn)

    def test_stamps_availability_flags_before_returning(self):
        self.assertIn('suggestions["ai_available"] = ai_available', self.fn)
        self.assertIn('suggestions["ai_unavailable_reason"] = ai_unavailable_reason', self.fn)


class PasswordRequirementsBehaviorTests(unittest.TestCase):
    """Real behavioral test: extract _password_requirements_unmet and run it
    against real password strings."""

    @classmethod
    def setUpClass(cls):
        fn_src = _function_source(
            SETUP_SOURCE,
            "def _password_requirements_unmet(password: str) -> List[str]:",
            '\n_FALLBACK_ENV_TEMPLATE = ',
        )
        preamble = textwrap.dedent(
            """
            import re
            from typing import List
            _PASSWORD_MIN_LENGTH = 12
            _PASSWORD_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")
            _PASSWORD_UPPER_RE = re.compile(r"[A-Z]")
            _PASSWORD_LOWER_RE = re.compile(r"[a-z]")
            _PASSWORD_DIGIT_RE = re.compile(r"[0-9]")
            """
        )
        namespace: dict = {}
        exec(compile(preamble + "\n" + fn_src, "<password_requirements>", "exec"), namespace)
        cls._unmet = staticmethod(namespace["_password_requirements_unmet"])

    def test_strong_password_passes(self):
        self.assertEqual(self._unmet("Correct-Horse-Battery9!"), [])

    def test_too_short_is_rejected(self):
        self.assertIn("at least 12 characters", self._unmet("Ab1!"))

    def test_missing_uppercase_is_rejected(self):
        self.assertIn("an uppercase letter", self._unmet("lowercase123!!!!"))

    def test_missing_lowercase_is_rejected(self):
        self.assertIn("a lowercase letter", self._unmet("UPPERCASE123!!!!"))

    def test_missing_number_is_rejected(self):
        self.assertIn("a number", self._unmet("NoNumbersHere!!!"))

    def test_missing_special_char_is_rejected(self):
        self.assertIn("a special character", self._unmet("NoSpecialChars123"))

    def test_placeholder_like_weak_password_fails_multiple_checks(self):
        unmet = self._unmet("password")
        self.assertGreaterEqual(len(unmet), 3)


class PasswordValidationWiringTests(unittest.TestCase):
    def test_write_env_file_validates_password_before_persisting(self):
        fn = _function_source(SETUP_SOURCE, "def _write_env_file(", "\n\ndef _check_path(")
        self.assertIn('if updates.get("BEETS_WEB_PASSWORD"):', fn)
        self.assertIn("_password_requirements_unmet(updates[\"BEETS_WEB_PASSWORD\"])", fn)
        self.assertIn("raise ValueError", fn)

    def test_frontend_shows_password_requirements_and_strength_meter(self):
        self.assertIn("PASSWORD_REQUIREMENTS", SYSTEM_SOURCE)
        self.assertIn("At least 12 characters", SYSTEM_SOURCE)
        self.assertIn("An uppercase letter", SYSTEM_SOURCE)
        self.assertIn("A lowercase letter", SYSTEM_SOURCE)
        self.assertIn("A number", SYSTEM_SOURCE)
        self.assertIn("A special character", SYSTEM_SOURCE)
        self.assertIn("function PasswordStrengthMeter", SYSTEM_SOURCE)
        self.assertIn("variable.name === 'BEETS_WEB_PASSWORD'", SYSTEM_SOURCE)


class AuthTokenAutoGenerationTests(unittest.TestCase):
    def test_generator_uses_256_bit_entropy_source(self):
        self.assertIn("def generate_secure_auth_token() -> str:", APP_SOURCE)
        self.assertIn("secrets.token_urlsafe(32)", APP_SOURCE)

    def test_bootstrap_skips_when_auth_already_configured(self):
        fn = _function_source(
            APP_SOURCE,
            "def _bootstrap_auth_token_if_missing() -> None:",
            "\n\ndef _constant_time_equal(",
        )
        self.assertIn("if _security_auth_configured():\n        return", fn)

    def test_bootstrap_reuses_persisted_token_across_restarts(self):
        fn = _function_source(
            APP_SOURCE,
            "def _bootstrap_auth_token_if_missing() -> None:",
            "\n\ndef _constant_time_equal(",
        )
        self.assertIn("_GENERATED_AUTH_TOKEN_FILE.read_text(", fn)
        self.assertIn("_auth_secret_is_usable(existing)", fn)

    def test_bootstrap_persists_and_prints_new_token_once(self):
        fn = _function_source(
            APP_SOURCE,
            "def _bootstrap_auth_token_if_missing() -> None:",
            "\n\ndef _constant_time_equal(",
        )
        self.assertIn("_persist_generated_auth_token(token)", fn)
        self.assertIn("it will not be printed again", fn)
        self.assertIn('os.environ["BEETS_WEB_AUTH_TOKEN"] = token', fn)

    def test_bootstrap_runs_at_module_import_time(self):
        # Must be called unconditionally at module scope so it runs before
        # _enforce_security_boundary can ever reject a request.
        self.assertIn("\n_bootstrap_auth_token_if_missing()\n", APP_SOURCE)

    def test_token_file_persists_with_restricted_permissions(self):
        fn = _function_source(
            APP_SOURCE,
            "def _persist_generated_auth_token(token: str) -> None:",
            "\n\ndef _bootstrap_auth_token_if_missing",
        )
        self.assertIn("os.chmod(_GENERATED_AUTH_TOKEN_FILE, 0o600)", fn)


class AuthTokenRegenerateEndpointTests(unittest.TestCase):
    def test_route_registered(self):
        self.assertIn('@app.post("/api/setup/auth-token/regenerate")', SETUP_SOURCE)

    def test_returns_plaintext_token_exactly_once(self):
        fn = _function_source(
            SETUP_SOURCE,
            "def setup_regenerate_auth_token():",
            "\n\n@app.post(\"/api/setup/complete\")",
        )
        self.assertIn("token = generate_secure_auth_token()", fn)
        self.assertIn('"token": token', fn)
        # Must persist through the existing env-file mechanism (backups,
        # process env update) rather than a bespoke write path.
        self.assertIn('_write_env_file({"BEETS_WEB_AUTH_TOKEN": token}, [])', fn)
        self.assertIn("token_file.write_text(token", fn)
        # Falls back to a local secrets.token_urlsafe(32) if app.py's real
        # generator can't be imported (e.g. routes_setup loaded against the
        # minimal stub `app` module in tests/test_routes_setup.py).
        self.assertIn("except ImportError:", fn)
        self.assertIn("secrets.token_urlsafe(32)", fn)

    def test_masked_everywhere_else(self):
        # GET /api/setup/env must never echo the real token back -- confirms
        # the existing secret-masking path still covers BEETS_WEB_AUTH_TOKEN.
        self.assertIn('"secret": secret,', SETUP_SOURCE)
        self.assertIn('_is_secret_env(key)', SETUP_SOURCE)


class SetupStatusAuthFieldTests(unittest.TestCase):
    def test_status_reports_token_and_password_state_independently(self):
        fn = _function_source(SETUP_SOURCE, "def setup_status():", "\n\n@app.get(\"/api/setup/env\")")
        self.assertIn('"token_configured": token_configured', fn)
        self.assertIn('"token_auto_generated"', fn)
        self.assertIn('"password_configured": password_configured', fn)


class IndependentIntegrationTestEndpointsTests(unittest.TestCase):
    """Each of the four /api/setup/test/* endpoints must be fully
    independent -- a failure in one must never raise into or block another."""

    def test_all_four_endpoints_exist(self):
        for path in ("/api/setup/test/ai", "/api/setup/test/musicbrainz",
                     "/api/setup/test/acoustid", "/api/setup/test/plex"):
            self.assertIn(f'@app.post("{path}")', SETUP_SOURCE)

    def test_each_endpoint_has_its_own_try_except_and_returns_200(self):
        for fn_name, next_marker in (
            ("def setup_test_ai():", "@app.post(\"/api/setup/test/musicbrainz\")"),
            ("def setup_test_musicbrainz():", "@app.post(\"/api/setup/test/acoustid\")"),
            ("def setup_test_acoustid():", "@app.post(\"/api/setup/test/plex\")"),
            ("def setup_test_plex():", "@app.get(\"/api/setup/settings\")"),
        ):
            fn = _function_source(SETUP_SOURCE, fn_name, next_marker)
            self.assertIn("except", fn)
            self.assertIn('"status"', fn)


class FrontendIntegrationStatusUiTests(unittest.TestCase):
    def test_client_wrappers_for_all_four_tests_exist(self):
        self.assertIn("export function testSetupAi()", CLIENT_SOURCE)
        self.assertIn("export function testSetupMusicBrainz()", CLIENT_SOURCE)
        self.assertIn("export function testSetupAcoustid()", CLIENT_SOURCE)
        self.assertIn("export function testSetupPlex()", CLIENT_SOURCE)
        self.assertIn("export function regenerateAuthToken()", CLIENT_SOURCE)

    def test_types_declare_integration_test_response(self):
        self.assertIn("export interface SetupIntegrationTestResponse", TYPES_SOURCE)
        self.assertIn("export interface SetupAuthTokenRegenerateResponse", TYPES_SOURCE)
        self.assertIn("token_auto_generated: boolean", TYPES_SOURCE)

    def test_badge_shows_connected_warning_or_not_configured(self):
        self.assertIn("'Connected'", SYSTEM_SOURCE)
        self.assertIn("'Warning'", SYSTEM_SOURCE)
        self.assertIn("'Not Configured'", SYSTEM_SOURCE)

    def test_each_integration_test_updates_state_independently(self):
        fn = _function_source(SYSTEM_SOURCE, "const runIntegrationTests = useCallback(", "const markComplete = async")
        # Each call has its own .then/.catch/.finally rather than a single
        # Promise.all that would let one failure obscure the others.
        self.assertIn(".then((result) => {", fn)
        self.assertIn(".catch((err) => {", fn)
        self.assertIn(".finally(() => {", fn)
        self.assertNotIn("Promise.all(", fn)

    def test_regenerate_token_shown_exactly_once_then_masked(self):
        self.assertIn("function AuthTokenDialog", SYSTEM_SOURCE)
        self.assertIn("it will not be shown again", SYSTEM_SOURCE)
        self.assertIn("setRevealedToken(null)", SYSTEM_SOURCE)


class ReadmeDocumentationTests(unittest.TestCase):
    def test_documents_ai_optional_behavior(self):
        self.assertIn("AI is an enhancement, not a requirement", README_SOURCE)
        self.assertIn("Matched using MusicBrainz and AcoustID (AI unavailable:", README_SOURCE)

    def test_documents_password_requirements(self):
        self.assertIn("At least 12 characters", README_SOURCE)
        self.assertIn("One uppercase letter", README_SOURCE)
        self.assertIn("One special (non-alphanumeric) character", README_SOURCE)

    def test_documents_auth_token_auto_generation(self):
        self.assertIn("You never have to invent `BEETS_WEB_AUTH_TOKEN` yourself", README_SOURCE)

    def test_has_troubleshooting_section_for_ai_failure(self):
        self.assertIn("## Troubleshooting", README_SOURCE)
        self.assertIn("will my imports still work", README_SOURCE)
        self.assertIn("never stops MusicBrainz or AcoustID matching", README_SOURCE)

    def test_documents_required_vs_optional_integrations(self):
        self.assertIn("Required vs. optional integrations", README_SOURCE)
        self.assertIn("MusicBrainz", README_SOURCE)
        self.assertIn("| Plex | Optional |", README_SOURCE)


if __name__ == "__main__":
    unittest.main()
