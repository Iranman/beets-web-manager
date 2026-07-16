"""First-run setup wizard API — registered on app after app.py initializes.

Read-only status/test endpoints plus a single settings-persistence endpoint.
Does not change how app.py itself loads config: env vars and config.yaml
remain authoritative. This module only adds:
  - GET  /api/setup/status         readiness snapshot for the wizard/health page
  - POST /api/setup/test/ai        live AI provider connectivity test
  - POST /api/setup/test/musicbrainz
  - POST /api/setup/test/acoustid  fpcalc + AcoustID API test
  - POST /api/setup/test/plex
  - GET/POST /api/setup/settings   persisted settings not covered by env/config.yaml
  - GET  /health, /health/live, /health/ready   standard Docker/k8s-style probes
    (in addition to the existing /api/health — these use the unprefixed
    convention most container orchestrators expect by default)
"""
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict

from flask import jsonify, request

# Imported after app.py has already defined app (circular-but-OK pattern,
# matches routes_jobs.py / routes_lidarr.py).
from app import app  # noqa: E402

_SETTINGS_FILE = Path(os.environ.get("SETUP_SETTINGS_FILE", "/config/app_settings.json"))
_SETUP_COMPLETE_MARKER = Path(os.environ.get("SETUP_COMPLETE_FILE", "/config/.setup_complete"))


def _app_version() -> str:
    for candidate in (Path(__file__).parent / "VERSION", Path("/app/VERSION")):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    return "unknown"


_APP_VERSION = _app_version()


def _load_settings() -> Dict[str, Any]:
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(data: Dict[str, Any]) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_SETTINGS_FILE)


def _mask(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _check_path(path_value: str, *, require_writable: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"path": path_value, "exists": False, "writable": False, "error": ""}
    if not path_value:
        result["error"] = "not configured"
        return result
    p = Path(path_value)
    try:
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        result["exists"] = p.is_dir() or p.is_file()
        if require_writable:
            probe = p / ".setup_write_test" if p.is_dir() else p
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            result["writable"] = True
        else:
            result["writable"] = os.access(str(p), os.W_OK)
    except Exception as ex:
        result["error"] = str(ex)
    return result


@app.get("/api/setup/status")
def setup_status():
    """Readiness snapshot: paths, beets config, fpcalc, and each optional
    integration's configured/not-configured state (does not make live network
    calls — use the /api/setup/test/* endpoints for that)."""
    settings = _load_settings()

    config_check = _check_path(os.environ.get("BEETSDIR", "/config"), require_writable=True)
    music_check = _check_path("/data/media/music", require_writable=False)
    downloads_check = _check_path("/data/torrents", require_writable=True)
    beets_config_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))

    fpcalc_path = shutil.which("fpcalc")

    integrations = {
        "ai": {
            "configured": bool(
                os.environ.get("OPENAI_API_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or os.environ.get("AI_API_KEY")
            ),
            "required": False,
        },
        "musicbrainz": {"configured": True, "required": True},  # public API, no key needed
        "acoustid": {
            "configured": bool(os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACOUSTID_KEY")),
            "required": False,
            "note": "Works without a key via a shared, rate-limited test key.",
        },
        "plex": {
            "configured": bool(os.environ.get("PLEX_URL") and os.environ.get("PLEX_TOKEN")),
            "required": False,
        },
    }

    blocking: list = []
    if not config_check["writable"]:
        blocking.append(f"Cannot write to config path {config_check['path']}")
    if not music_check["exists"]:
        blocking.append(f"Music library path {music_check['path']} is not accessible")
    if not downloads_check["writable"]:
        blocking.append(f"Cannot write to downloads/staging path {downloads_check['path']}")
    if not beets_config_path.exists():
        blocking.append(
            f"Beets config not found at {beets_config_path} — copy config.yaml.example to config.yaml"
        )
    if not fpcalc_path:
        blocking.append("fpcalc (chromaprint) not found on PATH — AcoustID fingerprinting will not work")

    ready = not blocking
    return jsonify({
        "ok": True,
        "status": "ready" if ready else "warning",
        "version": _APP_VERSION,
        "setup_complete": _SETUP_COMPLETE_MARKER.exists(),
        "blocking_reasons": blocking,
        "paths": {
            "config": config_check,
            "music_library": music_check,
            "downloads": downloads_check,
            "beets_config": {"path": str(beets_config_path), "exists": beets_config_path.exists()},
        },
        "fpcalc": {"available": bool(fpcalc_path), "path": fpcalc_path or ""},
        "integrations": integrations,
        "settings": {k: (_mask(v) if "key" in k.lower() or "token" in k.lower() else v)
                     for k, v in settings.items()},
    })


@app.post("/api/setup/test/ai")
def setup_test_ai():
    """Live connectivity test against the configured (or posted) AI provider."""
    payload = request.get_json(silent=True) or {}
    api_key = payload.get("api_key") or os.environ.get("OPENAI_API_KEY") or os.environ.get("AI_API_KEY")
    base_url = payload.get("base_url") or os.environ.get("AI_BASE_URL") or "https://api.openai.com/v1"
    model = payload.get("model") or os.environ.get("AI_MODEL") or "gpt-4o-mini"
    if not api_key:
        return jsonify({"ok": False, "status": "not_configured",
                         "error": "No AI API key configured. Set OPENAI_API_KEY (or your provider's key) and retry."}), 200
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models/{model}" if "openai.com" in base_url else f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return jsonify({"ok": True, "status": "ready", "model": model})
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            error = "The AI provider rejected the API key. Verify the key, selected provider, base URL, and model."
        elif exc.code == 404:
            error = f"Model {model!r} was not found for this provider/account."
        else:
            error = f"AI provider returned HTTP {exc.code}."
        return jsonify({"ok": False, "status": "failed", "error": error}), 200
    except Exception as ex:
        return jsonify({"ok": False, "status": "failed",
                         "error": f"Could not reach the AI provider: {ex}"}), 200


@app.post("/api/setup/test/musicbrainz")
def setup_test_musicbrainz():
    """MusicBrainz needs no API key for lookups — this just confirms reachability
    and a well-formed User-Agent (MusicBrainz blocks generic/missing UAs)."""
    try:
        req = urllib.request.Request(
            "https://musicbrainz.org/ws/2/release/?query=release:test&limit=1&fmt=json",
            headers={"User-Agent": "BeetsWebManager/1.0 (+https://github.com/)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read())
        return jsonify({"ok": True, "status": "ready"})
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            error = "MusicBrainz rate-limited this request (max ~1 req/sec per IP). Try again shortly."
        else:
            error = f"MusicBrainz returned HTTP {exc.code}."
        return jsonify({"ok": False, "status": "failed", "error": error}), 200
    except Exception as ex:
        return jsonify({"ok": False, "status": "failed", "error": f"Could not reach MusicBrainz: {ex}"}), 200


@app.post("/api/setup/test/acoustid")
def setup_test_acoustid():
    """Distinguishes fpcalc availability from API-key validity — a key string
    existing is not treated as 'configured' without a real lookup."""
    payload = request.get_json(silent=True) or {}
    api_key = payload.get("api_key") or os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACOUSTID_KEY")
    fpcalc_path = shutil.which("fpcalc")
    result: Dict[str, Any] = {"fpcalc_available": bool(fpcalc_path)}
    if not fpcalc_path:
        result.update({"ok": False, "status": "failed",
                       "error": "fpcalc (chromaprint) is not installed or not on PATH."})
        return jsonify(result), 200
    try:
        proc = subprocess.run([fpcalc_path, "-version"], capture_output=True, text=True, timeout=5)
        result["fpcalc_version"] = (proc.stdout or proc.stderr or "").strip()
    except Exception as ex:
        result.update({"ok": False, "status": "failed", "error": f"fpcalc failed to run: {ex}"})
        return jsonify(result), 200
    if not api_key:
        result.update({"ok": False, "status": "not_configured",
                       "error": "No AcoustID API key configured — lookups will use a shared, rate-limited test key."})
        return jsonify(result), 200
    try:
        params = urllib.parse.urlencode({
            "client": api_key, "format": "json",
            "duration": "1", "fingerprint": "AQAAA0mUaEkSRZEeJk-eHtWMh4",  # tiny placeholder for a key/connectivity check
        })
        req = urllib.request.Request(f"https://api.acoustid.org/v2/lookup?{params}")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == "error":
            result.update({"ok": False, "status": "failed",
                           "error": data.get("error", {}).get("message", "AcoustID rejected the request.")})
        else:
            result.update({"ok": True, "status": "ready"})
    except Exception as ex:
        result.update({"ok": False, "status": "failed", "error": f"Could not reach AcoustID: {ex}"})
    return jsonify(result), 200


@app.post("/api/setup/test/plex")
def setup_test_plex():
    payload = request.get_json(silent=True) or {}
    plex_url = (payload.get("url") or os.environ.get("PLEX_URL") or "").rstrip("/")
    plex_token = payload.get("token") or os.environ.get("PLEX_TOKEN")
    if not plex_url or not plex_token:
        return jsonify({"ok": False, "status": "not_configured",
                         "error": "PLEX_URL and PLEX_TOKEN are both required to test Plex."}), 200
    try:
        req = urllib.request.Request(f"{plex_url}/library/sections", headers={"X-Plex-Token": plex_token})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        libraries = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
            libraries = [d.get("title") for d in root.findall("Directory") if d.get("type") == "artist"]
        except Exception:
            pass
        return jsonify({"ok": True, "status": "ready", "music_libraries": libraries})
    except urllib.error.HTTPError as exc:
        error = "Plex token is invalid or expired." if exc.code in (401, 403) else f"Plex returned HTTP {exc.code}."
        return jsonify({"ok": False, "status": "failed", "error": error}), 200
    except Exception as ex:
        return jsonify({"ok": False, "status": "failed",
                         "error": f"Could not reach Plex at {plex_url}: {ex}"}), 200


@app.get("/api/setup/settings")
def setup_get_settings():
    settings = _load_settings()
    return jsonify({"ok": True, "settings": {
        k: (_mask(v) if "key" in k.lower() or "token" in k.lower() else v) for k, v in settings.items()
    }})


@app.post("/api/setup/settings")
def setup_save_settings():
    """Persist wizard-configured values that don't have a dedicated env var
    (e.g. selected AI model). Real secrets should be set via .env / Docker
    secrets, not through this endpoint — this file is not treated as a secret
    store, only as a record of non-secret selections."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400
    settings = _load_settings()
    settings.update(payload)
    _save_settings(settings)
    return jsonify({"ok": True})


@app.post("/api/setup/complete")
def setup_mark_complete():
    """Mark first-run setup as done. Idempotent — safe to call repeatedly."""
    _SETUP_COMPLETE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_COMPLETE_MARKER.write_text("1", encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/health/live")
def health_live():
    """Liveness probe: the process is up and answering HTTP. No dependency
    checks — a failing DB/path should not make Docker/k8s kill the container
    (that's what readiness is for)."""
    return jsonify({"status": "alive", "version": _APP_VERSION})


@app.get("/health/ready")
def health_ready():
    """Readiness probe: same underlying checks as /api/setup/status, boiled
    down to a single status a load balancer/orchestrator can act on."""
    config_check = _check_path(os.environ.get("BEETSDIR", "/config"), require_writable=True)
    downloads_check = _check_path("/data/torrents", require_writable=True)
    beets_config_path = Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml"))
    blocking = []
    if not config_check["writable"]:
        blocking.append("config path not writable")
    if not downloads_check["writable"]:
        blocking.append("downloads path not writable")
    if not beets_config_path.exists():
        blocking.append("beets config missing")
    status = "ready" if not blocking else "warning"
    return jsonify({
        "status": status,
        "version": _APP_VERSION,
        "blocking_reasons": blocking,
    }), (200 if not blocking else 503)


@app.get("/health")
def health_root():
    """Alias for /api/health under the unprefixed convention most container
    orchestrators probe by default."""
    return health_live()
