"""The running build's identity: the one source for every version the app reports.

An image build writes ``BUILD_INFO.json`` next to ``app.py`` from the same
build arguments that set its OCI labels, so the app reports exactly what
``org.opencontainers.image.version`` / ``.revision`` / ``.created`` say:

    {"version": "0.4.0", "revision": "<git sha>", "created": "<RFC 3339>"}

A source checkout without that file reports the ``VERSION`` file and an
``unknown`` revision. Nothing here reads the environment: a deploy-time
variable (e.g. ``BEETS_WEB_MANAGER_VERSION``) can be stale and is never
the running version.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Dict

_ROOT = Path(__file__).resolve().parent.parent
_VALUE_RE = re.compile(r"^[A-Za-z0-9.+_:\-]{1,128}$")


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return text if _VALUE_RE.match(text) else ""


@functools.lru_cache(maxsize=1)
def build_info() -> Dict[str, str]:
    """``{"version", "revision", "created", "source"}``; ``source`` is
    ``image`` (BUILD_INFO.json), ``source-tree`` (VERSION file) or ``none``."""
    try:
        data = json.loads((_ROOT / "BUILD_INFO.json").read_text(encoding="utf-8"))
        version = _clean(data.get("version")) if isinstance(data, dict) else ""
        if version:
            return {"version": version, "revision": _clean(data.get("revision")) or "unknown",
                    "created": _clean(data.get("created")), "source": "image"}
    except (OSError, ValueError):
        pass
    try:
        version = _clean((_ROOT / "VERSION").read_text(encoding="utf-8"))
    except OSError:
        version = ""
    if version:
        return {"version": version, "revision": "unknown", "created": "", "source": "source-tree"}
    return {"version": "unknown", "revision": "unknown", "created": "", "source": "none"}
