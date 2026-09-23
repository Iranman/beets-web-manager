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
