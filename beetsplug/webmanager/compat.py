"""Compatibility and safe Flask blueprint registration guard for stock Beets."""

import logging
from typing import Any

log = logging.getLogger("beets.webmanager.compat")

_REGISTERED = False


def register_webmanager_blueprint(plugin: Any) -> bool:
    """Safely register webmanager blueprint on beetsplug.web Flask app.

    Fails closed and logs warnings if beetsplug.web is missing or structure diverges,
    preventing any fatal crash during Beets startup.
    """
    global _REGISTERED
    if _REGISTERED:
        return True

    try:
        from beetsplug.web import app as beets_web_app
        import flask

        if not isinstance(beets_web_app, flask.Flask):
            log.warning(
                "beetsplug.web.app is not a Flask instance (%s). Skipping WebManager blueprint registration.",
                type(beets_web_app),
            )
            return False

        from .auth import require_webmanager_auth, set_api_key_file
        from .operations import webmanager_bp

        # Configure API key file path if specified in plugin config
        if hasattr(plugin, "config") and "api_key_file" in plugin.config:
            try:
                key_path = plugin.config["api_key_file"].as_filename()
                set_api_key_file(key_path)
            except Exception:
                pass

        # Attach auth guard to blueprint
        webmanager_bp.before_request(require_webmanager_auth)

        # Register blueprint on Beets web app
        beets_web_app.register_blueprint(webmanager_bp)
        _REGISTERED = True
        log.info("Successfully registered WebManager integration blueprint on Beets web server.")
        return True

    except ImportError as e:
        log.warning(
            "beetsplug.web could not be imported (%s). WebManager routes will not be attached.",
            e,
        )
        return False
    except Exception as e:
        log.exception(
            "Unexpected error registering WebManager blueprint on beetsplug.web: %s",
            e,
        )
        return False
