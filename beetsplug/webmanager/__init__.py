"""WebManager integration plugin for Beets.

Enables Beets Web Manager to communicate securely with stock Beets
over HTTP for mutation operations, non-interactive confirmed imports,
and metadata management.
"""

from beets.plugins import BeetsPlugin
from .compat import register_webmanager_blueprint
from .schemas import DEFAULT_ALLOWED_ROOTS, DEFAULT_API_KEY_FILE

__version__ = "1.0.0"


class WebManagerPlugin(BeetsPlugin):
    """Beets plugin exposing authenticated mutation and import endpoints for Beets Web Manager."""

    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "api_key_file": DEFAULT_API_KEY_FILE,
                "allowed_roots": DEFAULT_ALLOWED_ROOTS,
                "async_retention_seconds": 3600,
            }
        )
        register_webmanager_blueprint(self)
