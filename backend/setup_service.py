"""First-run setup state, bootstrap secrets and binary status (ARCH-001).
"""

from __future__ import annotations

import os, shutil, subprocess
import logging
from pathlib import Path
from typing import Any, Dict, List
from backend.app_runtime import _app_logger, WEB_MANAGER_DATA_DIR, _plugin_install_log
from flask import request
from backend.auth_service import _GENERATED_AUTH_TOKEN_FILE, _INITIAL_BROWSER_PASSWORD_FILE, _PERSISTED_BROWSER_PASSWORD_FILE, _auth_secret_is_usable, _browser_password_is_usable, _first_config_secret, _has_explicit_browser_password, _persist_app_config_text, _persist_file_atomically, _persist_generated_auth_token, _security_auth_disabled, _security_auth_password, _security_auth_token, generate_secure_auth_token

# ── ARCH-001 extracted code ──


def _pip_install(*packages, timeout=300) -> bool:
    """Runtime package installation is intentionally disabled."""
    wanted = ", ".join(str(pkg) for pkg in packages if pkg) or "requested packages"
    _plugin_install_log.append(
        f"[blocked] runtime package installation disabled; preinstall pinned {wanted} in the image build"
    )
    return False


def _install_deno_runtime_if_needed() -> None:
    _plugin_install_log.append(
        "[blocked] Deno auto-install disabled; provide a pinned deno binary in the image build or YTDLP_JS_RUNTIMES"
    )


def _run_install_command(cmd: List[str], timeout: int = 240) -> bool:
    _plugin_install_log.append(
        "[blocked] runtime package-manager execution disabled; install required tools during image build"
    )
    return False


def _install_system_js_runtime_if_needed() -> None:
    _plugin_install_log.append(
        "[blocked] system JS runtime auto-install disabled; preinstall node/deno/quickjs in the image build"
    )


def _binary_status(name: str) -> Dict[str, Any]:
    path = shutil.which(name)
    fallback = Path("/usr/bin") / name
    if not path and fallback.exists():
        path = str(fallback)
    result: Dict[str, Any] = {
        "available": False,
        "path": path or "",
        "version": "",
    }
    if not path:
        return result
    try:
        r = subprocess.run(
            [path, "-version"],
            timeout=5,
            capture_output=True,
            text=True,
        )
        output = (r.stdout or r.stderr or "").strip()
        result.update({
            "available": r.returncode == 0,
            "version": output.splitlines()[0][:160] if output else "",
            "returncode": r.returncode,
        })
    except Exception as ex:
        logging.getLogger("app").warning("Binary version check failed for %r: %s", path, type(ex).__name__)
        result["error"] = f"Could not run {path}."
    return result


def _persist_bootstrap_secret_file(target_file: Path, content: str) -> bool:
    return _persist_app_config_text(target_file, content, is_secret=True)


_FIRST_RUN_PUBLIC_ENDPOINTS = {
    ("GET", "health"),
    ("HEAD", "health"),
    ("GET", "health_live"),
    ("HEAD", "health_live"),
    ("GET", "health_ready"),
    ("HEAD", "health_ready"),
    ("GET", "health_root"),
    ("HEAD", "health_root"),
    ("GET", "react_assets"),
    ("HEAD", "react_assets"),
    ("GET", "react_next_static"),
    ("HEAD", "react_next_static"),
    ("GET", "favicon"),
    ("HEAD", "favicon"),
    ("GET", "index"),
    ("HEAD", "index"),
    ("GET", "react_spa_fallback"),
    ("HEAD", "react_spa_fallback"),
    ("GET", "setup_status"),
    ("HEAD", "setup_status"),
    ("GET", "plugins_status"),
    ("HEAD", "plugins_status"),
    ("POST", "plugins_provision"),
    ("POST", "plugins_verify"),
    ("POST", "setup_first_run"),
    ("POST", "setup_test_ai"),
    ("POST", "setup_test_musicbrainz"),
    ("POST", "setup_test_acoustid"),
    ("POST", "setup_test_plex"),
    ("POST", "setup_test_beets"),
    ("POST", "api_login"),
    ("POST", "api_logout"),
    ("GET", "api_auth_me"),
    ("HEAD", "api_auth_me"),
}


_BROWSER_SETUP_STATE_FILE = Path(
    os.environ.get("BEETS_WEB_SETUP_STATE_FILE", str(WEB_MANAGER_DATA_DIR / ".browser_setup_state"))
)


def _read_browser_setup_state() -> str:
    try:
        if _BROWSER_SETUP_STATE_FILE.exists():
            val = _BROWSER_SETUP_STATE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if val in {"claimed", "legacy_established", "fresh"}:
                return val
    except Exception:
        pass
    return ""


def _set_browser_setup_state(state: str) -> bool:
    """Persist the durable setup-state marker. Returns True only on a
    confirmed durable write. Callers that gate a security-relevant response
    (e.g. "setup complete") on this MUST check the return value -- silently
    swallowing a failed write here previously let the endpoint claim success
    when the durable state was never actually committed."""
    ok = _persist_file_atomically(_BROWSER_SETUP_STATE_FILE, state)
    if not ok:
        try:
            _app_logger.error("Could not persist browser setup state %r", state)
        except Exception:
            pass
    return ok


def _migrate_or_initialize_setup_state() -> None:
    """Initialize or migrate durable browser setup state on application boot.

    Prevents existing v0.1.8 upgrades with .initial_admin_password from being
    mistaken for unclaimed fresh installations, and ensures established installs
    cannot re-open anonymous setup if credentials are missing or corrupted.
    """
    if _security_auth_disabled():
        return

    current_state = _read_browser_setup_state()
    if current_state in {"claimed", "legacy_established"}:
        return

    env_pwd = _first_config_secret("BEETS_WEB_PASSWORD")
    file_pwd_path = os.environ.get("BEETS_WEB_PASSWORD_FILE", "").strip()

    has_env_pwd = bool(env_pwd and _browser_password_is_usable(env_pwd))
    has_file_pwd = False
    if file_pwd_path:
        try:
            val = Path(file_pwd_path).read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            has_file_pwd = bool(val and _browser_password_is_usable(val))
        except Exception:
            pass

    has_persisted_pwd = False
    try:
        if _PERSISTED_BROWSER_PASSWORD_FILE.exists():
            val = _PERSISTED_BROWSER_PASSWORD_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            has_persisted_pwd = bool(val and _browser_password_is_usable(val))
    except Exception:
        pass

    has_legacy_initial = False
    try:
        if _INITIAL_BROWSER_PASSWORD_FILE.exists():
            val = _INITIAL_BROWSER_PASSWORD_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            has_legacy_initial = bool(val and _browser_password_is_usable(val))
    except Exception:
        pass

    if has_env_pwd or has_file_pwd or has_persisted_pwd:
        _set_browser_setup_state("claimed")
    elif has_legacy_initial:
        _set_browser_setup_state("legacy_established")
    else:
        if current_state != "fresh":
            _set_browser_setup_state("fresh")


def _first_run_setup_required() -> bool:
    if _security_auth_disabled():
        return False
    state = _read_browser_setup_state()
    if state in {"claimed", "legacy_established"}:
        return False
    return not _has_explicit_browser_password()


def _ensure_secret_file_mode(target_file: Path, expected_mode: int = 0o600) -> None:
    """Correct an existing secret file's mode in place if it has drifted from
    `expected_mode`. WebManagerConfigStore.save_text(is_secret=True) already
    chmods to 0600 on every write, but that write path only runs when the
    file is actually (re)written -- a file that's found valid and simply
    *reused* (read, never rewritten, as `.auth_token` is on every restart
    once generated) never passes through it again, so a mode set by an
    older code path before that write-time chmod existed would otherwise
    persist forever, including across container recreation. Call this
    wherever an existing secret file is read and accepted as-is."""
    if os.name != "posix":
        return
    try:
        current_mode = target_file.stat().st_mode & 0o777
        if current_mode != expected_mode:
            os.chmod(target_file, expected_mode)
    except OSError:
        pass


def _bootstrap_auth_token_if_missing() -> None:
    if _security_auth_disabled():
        return
    token = _security_auth_token()
    if _auth_secret_is_usable(token):
        return
    existing = ""
    try:
        if _GENERATED_AUTH_TOKEN_FILE.exists():
            existing = _GENERATED_AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception as ex:
        try:
            _app_logger.warning("Could not read token file %s: %s", _GENERATED_AUTH_TOKEN_FILE, ex)
        except Exception:
            pass
        existing = ""
    if _auth_secret_is_usable(existing):
        _ensure_secret_file_mode(_GENERATED_AUTH_TOKEN_FILE)
        os.environ["BEETS_WEB_AUTH_TOKEN"] = existing
        return
    generated = generate_secure_auth_token()
    _persist_generated_auth_token(generated)
    try:
        persisted_ok = _GENERATED_AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip() == generated
    except Exception:
        persisted_ok = False
    if not persisted_ok:
        raise RuntimeError(
            "Startup refused: no BEETS_WEB_AUTH_TOKEN is configured, and a newly "
            f"generated token could not be persisted to {_GENERATED_AUTH_TOKEN_FILE}. "
            "Fix the persistence problem and restart, or set BEETS_WEB_AUTH_TOKEN explicitly."
        )
    os.environ["BEETS_WEB_AUTH_TOKEN"] = generated
    print(
        "\n" + "=" * 72 +
        "\nNo BEETS_WEB_AUTH_TOKEN was configured."
        "\nGenerated a secure API token automatically so API access is not"
        "\nlocked out on first run, and persisted it to:\n"
        f"\n  {_GENERATED_AUTH_TOKEN_FILE}\n"
        "\nThe token itself is never printed to logs -- read that file to get"
        "\nit, e.g. `docker exec <container> cat " + str(_GENERATED_AUTH_TOKEN_FILE) + "`."
        "\nUse it as a Bearer token for API clients. It is NOT your browser password."
        "\nRegenerate anytime with POST /api/setup/auth-token/regenerate."
        "\n" + "=" * 72 + "\n",
        flush=True,
    )


def _bootstrap_browser_password_if_missing() -> None:
    """Fail-closed guard for an established install whose resolved browser
    password is missing/corrupt/unusable at boot.

    Fresh, not-yet-claimed installs never reach this function's generation
    logic -- they short-circuit above and wait for the user to claim
    credentials via the first-run UI. The ONLY way to reach this point with
    an unusable current_pwd is an install already marked claimed/
    legacy_established whose credential file is now missing or corrupt.
    Per the fail-closed requirement, that must NOT silently mint a fresh,
    working password (that would turn credential deletion into an anonymous
    account-takeover / privilege-recovery path via container logs) -- it
    must refuse to authenticate anyone until an operator restores the
    credential file or sets BEETS_WEB_PASSWORD explicitly.
    """
    if _security_auth_disabled() or _first_run_setup_required():
        return
    current_pwd = _security_auth_password()
    if _browser_password_is_usable(current_pwd):
        return

    try:
        _app_logger.error(
            "Browser credential is missing, unreadable, or unusable on an "
            "installation already marked established. Refusing to "
            "auto-generate a replacement (fail-closed): Basic Auth will "
            "reject all requests until the credential file is restored or "
            "BEETS_WEB_PASSWORD is set explicitly."
        )
    except Exception:
        pass
    return


def _is_first_run_public_endpoint() -> bool:
    endpoint = request.endpoint or ""
    method = request.method
    return (method, endpoint) in _FIRST_RUN_PUBLIC_ENDPOINTS
