"""Web Manager configuration read/persist helpers (ARCH-001).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Tuple
from backend.app_runtime import _plugin_install_log, _read_beets_plugin_list
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

    Config text is untrusted, so the scan stays linear: no regex, list-item
    dashes are walked by index (no per-item copy of the line), each line is
    read at most twice (once more only for blank lines after a secret's
    continuation), and a path is built only for a secret line whose stack
    depth is bounded by its own indentation. O(len(text)) overall
    (regression test: test_pathological_dash_runs_stay_linear)."""
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
        p, m = 0, len(rest)
        while rest[p:p + 1] == "-" and rest[p + 1:p + 2] in (" ", ""):  # "- " list item(s)
            while stack and stack[-1][0] > col:
                stack.pop()
            idx = 0
            if stack and stack[-1][0] == col and stack[-1][2]:
                idx = stack.pop()[1] + 1
            stack.append([col, idx, True])
            q = p + 1
            while q < m and rest[q] == " ":  # index walk, no per-item copy: O(len(line))
                q += 1
            col += q - p
            p = q
        rest = rest[p:]
        key_part, sep, value = rest.partition(":")
        if not sep or (value and value[0] not in " \t"):
            segs.append((None, line, "", "", ""))
            continue
        while stack and stack[-1][0] >= col:
            stack.pop()
        key = key_part.strip(" \t").strip("'\"")
        if not config_layers.is_secret_config_key(key):
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
    # #324 F2: the line scan cannot see flow mappings, anchors read through an
    # alias, explicit "? key" entries or JSON; mask every secret value YAML
    # finds. Fails closed (ConfigRedactionError, no content) when that
    # cannot be proven.
    try:
        return config_layers.mask_config_secret_values(text, "".join(out), _REDACTED_SECRET)
    except ValueError:
        raise ConfigRedactionError() from None


CONFIG_REDACTION_FAILED_MESSAGE = "config.yaml holds a secret in a form the editor cannot hide; edit the file directly."


class ConfigRedactionError(ConfigError):
    def __init__(self):
        super().__init__(CONFIG_REDACTION_FAILED_MESSAGE, error_code="config_redaction_failed", status_code=409)


def _config_path_label(path: Tuple[Any, ...]) -> str:
    return ".".join(f"[{p}]" if isinstance(p, int) else p for p in path)


def _restore_redacted_config_secrets(submitted: str, stored: str) -> str:
    """Put the stored value back on every secret line the user left as the
    unchanged [REDACTED] placeholder. Lines are matched by full section path
    (plex.token and listenbrainz.token never swap). Refuses, never guesses,
    when a placeholder maps to zero or several stored values, or when the
    placeholder appears anywhere else in a secret value. Paths come from the
    line scanner, not a YAML parser: a placeholder line moved to a different
    key path is refused, but one pasted inside another key's block scalar
    at a path that still resolves to the same stored secret is restored
    there (accepted in security review of #320: admin-only, the value stays
    in the same file). Callers pass the snapshot the revision check ran
    against (config_manager.save_config)."""
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
    stored_plain = {seg[1] for seg in _config_secret_segments(stored) if seg[0] is None}
    for path, head, value, ending, cont in segs:
        if path is None:
            # A placeholder outside a secret-key line (flow mapping, anchor,
            # JSON, #324 F2) cannot be mapped back: refuse, never write it.
            if _REDACTED_SECRET in head and head not in stored_plain:
                raise ConfigSecretMergeError(
                    f"Refusing to save {_REDACTED_SECRET}: it stands for a secret the editor cannot restore on this "
                    "line. Type the real value there, or edit config.yaml directly.",
                    error_code="config_redacted_placeholder",
                )
            out.append(head)
            continue
        if cont or value.strip(" \t") not in _REDACTED_VALUE_FORMS:
            if _REDACTED_SECRET in value or _REDACTED_SECRET in cont:
                raise ConfigSecretMergeError(
                    f"Refusing to save redacted secret placeholders: {_config_path_label(path)}. Replace the whole "
                    f"{_REDACTED_SECRET} placeholder with the new value, or leave it unchanged to keep the stored one.",
                    error_code="config_redacted_placeholder",
                )
            out.append(head + value + ending + cont)
            continue
        found = stored_values.get(path, [])
        if counts[path] != 1 or len(found) != 1:
            raise ConfigSecretMergeError(
                f"{_config_path_label(path)}: cannot tell which stored secret {_REDACTED_SECRET} stands for "
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
