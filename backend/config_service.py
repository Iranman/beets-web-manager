"""Web Manager configuration read/persist helpers (ARCH-001).
"""

from __future__ import annotations

import os, re, stat
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app_runtime import _app_logger, _plugin_install_log, _read_beets_plugin_list
from backend.app_runtime import _REDACTED_SECRET
from backend.beets_adapter import BeetsError
from backend.config_manager import ConfigError
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


class ConfigSecretMergeError(ConfigError):
    """A [REDACTED] placeholder in a submitted config.yaml cannot be mapped
    to exactly one stored secret. Messages name only key paths, never values."""

    def __init__(self, message: str, error_code: str = "config_secret_ambiguous"):
        super().__init__(message, error_code=error_code, status_code=400)


def _config_secret_segments(text: str) -> List[Tuple[Any, str, str, str, str]]:
    """Split config text into (path, head, value, ending, cont) segments.

    path is None for a line that is not a secret key (head is then the whole
    line). For a secret-key line, path is the section path from the root
    (keys as str, list items as their int position), head is everything up
    to and including the key's colon, value is the rest of the line, and
    cont holds any following deeper-indented lines (a multi-line or
    block-scalar value), which belong to the secret too.

    Same no-regex, single-pass design as before: one indentation stack,
    lstrip/partition per line, each line visited once, so O(len(text))."""
    segs: List[Tuple[Any, str, str, str, str]] = []
    stack: List[List[Any]] = []  # [column, key-or-list-index, is_list_item]
    lines = text.splitlines(keepends=True)
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        i += 1
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        rest = body.lstrip(" \t")
        col = len(body) - len(rest)
        if rest.startswith(("---", "...")):
            stack.clear()
        if not rest or rest.startswith(("#", "---", "...")):
            segs.append((None, line, "", "", ""))
            continue
        while rest[:1] == "-" and rest[1:2] in (" ", ""):  # "- " list item(s)
            while stack and stack[-1][0] > col:
                stack.pop()
            idx = 0
            if stack and stack[-1][0] == col and stack[-1][2]:
                idx = stack.pop()[1] + 1
            stack.append([col, idx, True])
            nxt = rest[1:].lstrip(" ")
            col += len(rest) - len(nxt)
            rest = nxt
        key_part, sep, value = rest.partition(":")
        if not sep or (value and value[0] not in " \t"):
            segs.append((None, line, "", "", ""))
            continue
        while stack and stack[-1][0] >= col:
            stack.pop()
        key = key_part.strip(" \t").strip("'\"")
        if key.lower() not in _CONFIG_SECRET_KEYS:
            if _is_empty_config_value(value):
                stack.append([col, key, False])
            segs.append((None, line, "", "", ""))
            continue
        path = tuple(frame[1] for frame in stack) + (key,)
        cont_end = j = i
        while j < n:
            cbody = lines[j].rstrip("\r\n")
            cstripped = cbody.lstrip(" \t")
            if cstripped and len(cbody) - len(cstripped) <= col:
                break
            j += 1
            if cstripped:
                cont_end = j
        segs.append((path, body[:len(body) - len(value)], value, ending, "".join(lines[i:cont_end])))
        i = cont_end
    return segs


def _is_empty_config_value(value: str) -> bool:
    v = value.strip(" \t")
    return v in ("", '""', "''", "~", "null") or v.startswith("#")


_REDACTED_VALUE_FORMS = frozenset({f'"{_REDACTED_SECRET}"', f"'{_REDACTED_SECRET}'", _REDACTED_SECRET})


def _redact_config_content(text: str) -> str:
    """Replace every non-empty secret value with the [REDACTED] placeholder,
    keeping indentation, key spelling and line endings. An empty secret is
    shown as-is: there is nothing to hide, and showing the placeholder there
    made the editor unable to save its own output."""
    if not text:
        return text or ""
    out = []
    for path, head, value, ending, cont in _config_secret_segments(text):
        if path is None or (not cont and _is_empty_config_value(value)):
            out.append(head + value + ending + cont)
        else:
            out.append(f'{head} "{_REDACTED_SECRET}"{ending}')
    return "".join(out)


def _path_label(path: Tuple[Any, ...]) -> str:
    return ".".join(f"[{p}]" if isinstance(p, int) else p for p in path)


def _restore_redacted_config_secrets(submitted: str, stored: str) -> str:
    """Put the stored value back on every secret line the user left as the
    unchanged [REDACTED] placeholder. Lines are matched by full section path
    (plex.token and listenbrainz.token never swap). Refuses, never guesses,
    when a placeholder maps to zero or several stored values, or when the
    placeholder appears anywhere else in a secret value. Callers pass the
    snapshot the revision check ran against (config_manager.save_config)."""
    if _REDACTED_SECRET not in submitted:
        return submitted
    stored_values: Dict[Tuple[Any, ...], List[Tuple[str, str]]] = {}
    for path, _head, value, _ending, cont in _config_secret_segments(stored):
        if path is not None:
            stored_values.setdefault(path, []).append((value, cont))
    segs = _config_secret_segments(submitted)
    counts: Dict[Tuple[Any, ...], int] = {}
    for seg in segs:
        if seg[0] is not None:
            counts[seg[0]] = counts.get(seg[0], 0) + 1
    out = []
    for path, head, value, ending, cont in segs:
        if path is None:
            out.append(head)
            continue
        if cont or value.strip(" \t") not in _REDACTED_VALUE_FORMS:
            if _REDACTED_SECRET in value or _REDACTED_SECRET in cont:
                raise ConfigSecretMergeError(
                    f"Refusing to save redacted secret placeholders: {_path_label(path)}. Replace the whole "
                    f"{_REDACTED_SECRET} placeholder with the new value, or leave it unchanged to keep the stored one.",
                    error_code="config_redacted_placeholder",
                )
            out.append(head + value + ending + cont)
            continue
        found = stored_values.get(path, [])
        if counts[path] != 1 or len(found) != 1:
            raise ConfigSecretMergeError(
                f"{_path_label(path)}: cannot tell which stored secret {_REDACTED_SECRET} stands for "
                f"(found {len(found)} stored, {counts[path]} submitted). Type the value, or reload the page."
            )
        stored_value, stored_cont = found[0]
        if stored_cont and not stored_cont.endswith(("\n", "\r")):
            stored_cont += "\n"
        out.append(head + stored_value + ending + stored_cont)
    return "".join(out)


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
