"""Web Manager configuration read/persist helpers (ARCH-001).
"""

from __future__ import annotations

import os, re, shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional
from backend.app_runtime import _app_logger, _plugin_install_log, _read_beets_plugin_list
from backend.app_runtime import _REDACTED_SECRET
from backend.beets_adapter import BeetsError
from backend.auth_service import _auth_secret_is_usable, _browser_password_is_usable, _security_auth_password, _security_auth_token

# ── ARCH-001 extracted code ──


def _install_optional_plugins():
    """Placeholder — no optional plugins currently enabled."""
    _plugin_install_log.append("[done] no optional plugins to install")


def _security_auth_configured() -> bool:
    return _auth_secret_is_usable(_security_auth_token()) or _browser_password_is_usable(_security_auth_password())


_LEGACY_BEETS_CONFIG_MIGRATION_MARKER = "# beets-web-manager: legacy-plugin-config-migrated"


def _repair_legacy_beets_config(config_path: Optional[str] = None) -> None:
    """First-run safety net for installs whose /config/config.yaml predates
    the Issue #14 packaging fix: setup.sh/setup.ps1 only copy
    config.yaml.example into place when config.yaml does not already exist,
    so an install set up before that fix shipped stays stuck on the old
    broken defaults across every later image update -- a raw `beet` CLI
    invocation inside the container reads this file directly, with none of
    the job-config overrides the web app's own operations already get
    (_BEETS_PLUGINPATH_CONFIG, _JOB_PLUGIN_EXCLUDED).

    Repairs exactly three known-legacy patterns and nothing else a user
    configured:
      1. drops the never-installed `plexsync` token from `plugins:`
      2. adds `/app/beetsplug` to `pluginpath:` (where the bundled discpath
         plugin now lives) if missing
      3. switches an existing `replaygain:` section's backend to `ffmpeg`
         (the one this image actually installs) when it's still pointed at
         mp3gain -- explicitly or by omission, beets' own default -- and
         mp3gain isn't actually available

    Does not create a replaygain: section that isn't already present; that
    narrower scope keeps this a config repair, not a config generator.
    Idempotent: writes a marker comment on first repair and no-ops on every
    later startup. Backs up the original once, to its own filename (not the
    config editor's /api/config/revert backup), before ever touching it.
    """
    path = Path(config_path or os.environ.get("BEETS_CONFIG", "/config/config.yaml"))
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    if _LEGACY_BEETS_CONFIG_MIGRATION_MARKER in text:
        return

    changed = False

    def _fix_plugins_line(m: "re.Match") -> str:
        nonlocal changed
        tokens = m.group(1).split()
        if "plexsync" not in tokens:
            return m.group(0)
        changed = True
        return "plugins: " + " ".join(t for t in tokens if t != "plexsync")

    text = re.sub(r"(?m)^plugins:[ \t]*(.*)$", _fix_plugins_line, text, count=1)

    pluginpath_match = re.search(r"(?m)^pluginpath:[ \t]*(.*)$((?:\n[ \t]+-[ \t]*\S.*$)*)", text)
    if pluginpath_match is None:
        changed = True
        block = "pluginpath:\n  - /config/beetsplug\n  - /app/beetsplug\n"
        if re.search(r"(?m)^plugins:.*$", text):
            text = re.sub(r"(?m)^plugins:.*$\n", lambda m: m.group(0) + block, text, count=1)
        else:
            text = block + text
    else:
        inline_value = pluginpath_match.group(1).strip()
        list_block = pluginpath_match.group(2) or ""
        entries = [inline_value] if inline_value else []
        entries += [ln.split("-", 1)[1].strip() for ln in list_block.splitlines() if ln.strip().startswith("-")]
        if "/app/beetsplug" not in entries:
            changed = True
            new_entries = (entries or ["/config/beetsplug"]) + ["/app/beetsplug"]
            replacement = "pluginpath:\n" + "".join(f"  - {e}\n" for e in new_entries if e)
            text = text[:pluginpath_match.start()] + replacement.rstrip("\n") + text[pluginpath_match.end():]

    replaygain_match = re.search(r"(?m)^replaygain:[ \t]*$((?:\n[ \t]+\S.*$)*)", text)
    if replaygain_match is not None:
        block = replaygain_match.group(1) or ""
        backend_match = re.search(r"(?m)^([ \t]+)backend:[ \t]*(\S+)[ \t]*$", block)
        current_backend = backend_match.group(2) if backend_match else "mp3gain"
        if current_backend != "ffmpeg" and not shutil.which("mp3gain") and shutil.which("ffmpeg"):
            changed = True
            if backend_match:
                indent = backend_match.group(1)
                new_block = block[:backend_match.start()] + f"{indent}backend: ffmpeg" + block[backend_match.end():]
            else:
                indent_search = re.search(r"(?m)^([ \t]+)\S", block)
                indent = indent_search.group(1) if indent_search else "    "
                new_block = block + f"\n{indent}backend: ffmpeg"
            text = text[:replaygain_match.start(1)] + new_block + text[replaygain_match.end(1):]

    if not changed:
        return
    try:
        backup = path.with_name(path.name + ".bak-legacy-plugin-migration")
        if not backup.exists():
            shutil.copy2(str(path), str(backup))
        if not text.endswith("\n"):
            text += "\n"
        text += f"{_LEGACY_BEETS_CONFIG_MIGRATION_MARKER}\n"
        path.write_text(text, encoding="utf-8")
        print(
            "Repaired legacy config.yaml defaults from before the Issue #14 fix "
            f"(backup saved to {backup}).",
            flush=True,
        )
    except Exception:
        pass


def _bootstrap_beets_plugins(config_dir: Optional[Path] = None) -> None:
    """Provision the bundled webmanager plugin files AND write/merge
    config.yaml's plugins:/pluginpath: before this process binds its HTTP
    port (this function runs at module-import time -- see the Dockerfile's
    CMD -- strictly before Flask/Waitress starts listening).

    This ordering is load-bearing for fresh installs: docker-compose.yml
    makes the `beets` service depend on beets-web-manager's healthcheck, so
    stock Beets only starts reading config.yaml on its own first boot AFTER
    this has already written the webmanager plugin into it -- no third
    "wait for it" service or Docker socket required. Only local
    file/config work happens here; verifying the plugin actually loaded
    inside stock Beets is a live HTTP call and happens later (System page /
    setup status), once stock Beets is actually up.
    """
    try:
        from backend.beets_plugins import provision_bundled_plugins, update_config_yaml_plugins
        cfg_dir = config_dir if config_dir else Path(os.environ.get("BEETS_CONFIG", "/config/config.yaml")).parent
        if cfg_dir.exists():
            provision_bundled_plugins(cfg_dir)
            update_config_yaml_plugins(cfg_dir / "config.yaml")
    except Exception as ex:
        try:
            _app_logger.warning("Auto plugin provisioning on startup skipped/failed: %s", ex)
        except Exception:
            pass


# ── Config editor ─────────────────────────────────────────────────────────────
#
# config.yaml lives on the Beets *engine* container's own /config mount, not
# the web manager's -- the two-service architecture cutover (2026-07-28)
# gave each its own separate /config volume. This used to read/write a
# local /config/config.yaml path that never existed in the web-manager
# container in the real deployed topology, so every call 500'd (BUG-1,
# found during the v0.1.11 TrueNAS rollout). Routes now proxy through
# composite_workflows -> the control agent's /config endpoints (BEETSDIR-relative
# on the engine side), matching the same architecture already used for
# library reads/writes. Redaction stays here (unchanged) rather than moving
# to the agent: the agent is the trusted internal boundary and returns raw
# content; the browser-facing boundary is where secrets must never cross.

# Secret-key line matching used to be a regex
# (r"(?im)^(\s*(?:apikey|...)\s*:\s*)(.+)$") -- CodeQL (py/polynomial-redos)
# correctly flagged it as a polynomial-time expression run against
# uncontrolled (user-supplied) config content, e.g. a line with hundreds of
# thousands of spaces before a colon. Config text is untrusted input (it
# round-trips through the browser on every save), so it is replaced with a
# deterministic, single-pass, O(len(text)) line scan below: no regex,
# no backtracking, no pathological input.
_CONFIG_SECRET_KEYS = frozenset({
    "apikey", "api_key", "api_token", "auth_token", "token", "user_token",
    "pass", "password", "secret", "client_secret", "access_token",
    "refresh_token",
})


def _redact_config_line(line: str) -> str:
    """Redact a single YAML-style "key: value" line if its key is a known
    secret key, preserving indentation, original key spelling/case, and
    the line's own ending (LF/CRLF/none). No regex: leading indentation is
    stripped with a fixed-charset lstrip(), the key is isolated with a
    single partition() on the first colon, and the key is matched against
    a fixed set -- every step is O(len(line)), so a single call is
    O(len(line)) and a full-document scan is O(len(text)) overall."""
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    stripped = body.lstrip(" \t")
    indent = body[:len(body) - len(stripped)]
    key_part, sep, _value = stripped.partition(":")
    if not sep or key_part.strip().lower() not in _CONFIG_SECRET_KEYS:
        return line
    return f'{indent}{key_part.rstrip(" \t")}: "{_REDACTED_SECRET}"{ending}'


def _redact_config_content(text: str) -> str:
    if not text:
        return text or ""
    return "".join(_redact_config_line(line) for line in text.splitlines(keepends=True))


def _contains_redacted_config_secret(text: str) -> bool:
    """True if a secret-key line's value carries the literal [REDACTED]
    placeholder -- guards against the browser round-tripping a previously
    redacted GET response straight back into a POST without supplying a
    real secret. The cheap substring check short-circuits the common case
    (no placeholder anywhere) before the linear line scan runs at all."""
    if not text or _REDACTED_SECRET not in text:
        return False
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        key_part, sep, value = stripped.partition(":")
        if sep and key_part.strip().lower() in _CONFIG_SECRET_KEYS and _REDACTED_SECRET in value:
            return True
    return False


# ── Plugin Control Endpoints ─────────────────────────────────────────────────
# Subcommands allowed through /api/plugins/run (prevents arbitrary execution)
_PLUGIN_MODULES = OrderedDict([
    ("ytimport", "beetsplug.ytimport"),
    ("ytupdate", "beetsplug.ytupdate"),
    ("wlg", "beetsplug.wlg"),
    ("aisauce", "beetsplug.aisauce"),
])


def _plugin_installed_map() -> Dict[str, bool]:
    import importlib.util
    return {
        name: importlib.util.find_spec(module) is not None
        for name, module in _PLUGIN_MODULES.items()
    }


def _plugin_status_payload() -> Dict[str, Any]:
    installed = _plugin_installed_map()
    configured_plugins = set(_read_beets_plugin_list())
    enabled = {
        name: name in configured_plugins
        for name in _PLUGIN_MODULES
    }
    status = {
        name: {
            "installed": installed[name],
            "enabled": enabled[name],
            "runnable": installed[name],
        }
        for name in _PLUGIN_MODULES
    }
    return {
        "ok": True,
        "installed": installed,
        "enabled": enabled,
        "status": status,
    }


_PLUGIN_CMDS = set(_PLUGIN_MODULES)
