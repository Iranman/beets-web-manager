"""Web Manager configuration read/persist helpers (ARCH-001).
"""

from __future__ import annotations

import os, re, stat
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional
from backend.app_runtime import _app_logger, _plugin_install_log, _read_beets_plugin_list
from backend.app_runtime import _REDACTED_SECRET
from backend.beets_adapter import BeetsError
from backend import config_layers
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

    Repairs exactly one known-legacy pattern and nothing else a user
    configured: drops the never-installed `plexsync` token from `plugins:`.

    Earlier versions also injected `/app/beetsplug` into `pluginpath:` and
    rewrote an existing `replaygain:` backend to `ffmpeg`.  Both were
    removed in plugin 1.6.0 (BI-5): this config is read by the *stock*
    LinuxServer Beets container, where `/app/beetsplug` does not exist and
    the available replaygain backend is a property of that container (not of
    the Web Manager image this code runs in), so neither rewrite was safe to
    make on the user's behalf.  The replaygain backend is now only written
    when the user explicitly opts in to recommended plugins.

    Idempotent: writes a marker comment on first repair and no-ops on every
    later startup. Backs up the original once, to its own filename (not the
    config editor's /api/config/revert backup), before ever touching it.
    """
    from backend.beets_plugins import _decode_config, _read_config_snapshot, _write_config_text
    from backend.config_manager import get_config_path
    try:
        # get_config_path() refuses a BEETS_CONFIG outside BEETSDIR, and the
        # O_NOFOLLOW snapshot refuses a symlinked config.yaml (S-3).
        path = Path(config_path) if config_path else get_config_path()
        text = _decode_config(_read_config_snapshot(path), path.name)
    except Exception:
        return
    if _LEGACY_BEETS_CONFIG_MIGRATION_MARKER in text:
        return
    original = text  # backup from the same snapshot that is rewritten (F4)

    changed = False

    def _fix_plugins_line(m: "re.Match") -> str:
        nonlocal changed
        tokens = m.group(1).split()
        if "plexsync" not in tokens:
            return m.group(0)
        changed = True
        return "plugins: " + " ".join(t for t in tokens if t != "plexsync")

    text = re.sub(r"(?m)^plugins:[ \t]*(.*)$", _fix_plugins_line, text, count=1)

    if not changed:
        return
    try:
        backup = path.with_name(path.name + ".bak-legacy-plugin-migration")
        try:  # 0600 via O_EXCL: config.yaml can hold tokens. Kept once.
            config_layers.create_private_file(backup, original)
        except FileExistsError:
            # Left by an older version (copy2, often 0644): tighten it, but
            # never chmod through a symlink.
            if stat.S_ISREG(os.lstat(backup).st_mode):
                config_layers.ensure_private_mode(backup)
        if not text.endswith("\n"):
            text += "\n"
        text += f"{_LEGACY_BEETS_CONFIG_MIGRATION_MARKER}\n"
        _write_config_text(path, text, backup_prefix=None)
        print(
            "Repaired legacy config.yaml defaults from before the Issue #14 fix "
            f"(backup saved to {backup.name}).",
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
        from backend.config_manager import get_config_path
        # get_config_path() refuses a BEETS_CONFIG outside BEETSDIR (S-3).
        # Edit the BEETS_CONFIG file itself, not always config.yaml (F7).
        config_path = config_dir / "config.yaml" if config_dir else get_config_path()
        cfg_dir = config_path.parent
        if cfg_dir.exists():
            provision_bundled_plugins(cfg_dir)
            update_config_yaml_plugins(config_path)
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
_CONFIG_SECRET_KEYS = config_layers.SECRET_CONFIG_KEYS


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
