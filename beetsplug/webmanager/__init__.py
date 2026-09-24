"""WebManager integration plugin for Beets.

Enables Beets Web Manager to communicate securely with stock Beets
over HTTP for mutation operations, non-interactive confirmed imports,
and metadata management.
"""

from beets.plugins import BeetsPlugin
from .compat import register_webmanager_blueprint
from .schemas import DEFAULT_ALLOWED_ROOTS, DEFAULT_API_KEY_FILE, DEFAULT_IMPORT_ROOTS
from .version import PLUGIN_VERSION, PROTOCOL_VERSION

__version__ = PLUGIN_VERSION
__all__ = ["WebManagerPlugin", "PLUGIN_VERSION", "PROTOCOL_VERSION"]


def _patch_displayable_path_none_handling() -> None:
    """Work around a real upstream Beets defect (verified against 2.14.1):
    `beets.util.displayable_path(None)` raises `TypeError: 'NoneType'
    object is not iterable` instead of returning an empty string, because
    it only special-cases `Path`/`str`/`bytes` before falling through to
    "iterate and join". This is fatal, not cosmetic: stock Beets' own
    bundled `web` plugin (beetsplug.web._rep(), with `include_paths: yes`
    -- required by this project's config -- always calls
    `displayable_path(album.artpath)`) calls this on EVERY album's
    `artpath`, which is `None` for any album without artwork yet (the
    normal state right after import, before fetchart runs). Werkzeug's
    dev server cannot recover once its response generator raises mid-
    stream, so `GET /album/` returns a truncated, unparseable chunked
    response and BeetsAdapter's every album read fails closed.

    This patches only the None case; every other input is delegated to
    the real function unchanged. It is applied once, at plugin-load time,
    to the shared `beets.util` module -- not a modification of the Beets
    image itself, just this plugin defending its own caller (stock
    Beets' web plugin) against a upstream `None`-handling gap."""
    import beets.util as _beets_util

    original = _beets_util.displayable_path
    if getattr(original, "_webmanager_none_patched", False):
        return

    def _displayable_path_safe(path, separator="; "):
        if path is None:
            return ""
        return original(path, separator)

    _displayable_path_safe._webmanager_none_patched = True
    _beets_util.displayable_path = _displayable_path_safe


_patch_displayable_path_none_handling()


class WebManagerPlugin(BeetsPlugin):
    """Beets plugin exposing authenticated mutation and import endpoints for Beets Web Manager."""

    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "api_key_file": DEFAULT_API_KEY_FILE,
                "allowed_roots": DEFAULT_ALLOWED_ROOTS,
                "import_roots": DEFAULT_IMPORT_ROOTS,
                "async_retention_seconds": 3600,
            }
        )
        register_webmanager_blueprint(self)
