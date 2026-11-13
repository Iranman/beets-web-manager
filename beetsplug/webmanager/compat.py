"""Registers the webmanager blueprint on stock Beets' web app (served in-process)."""

import logging
import os
from typing import Any

log = logging.getLogger("beets.webmanager.compat")

_REGISTERED = False


def _missing_album_art_404():
    """Stock ``/album/<id>/art`` calls ``send_file(album.artpath)`` unguarded, so
    an artpath whose file is gone raises FileNotFoundError and logs a full
    traceback as a 500. Answer 404 first; artpath itself is left as it is."""
    import flask

    if flask.request.endpoint != "album_art":
        return None
    lib = getattr(flask.g, "lib", None)
    album_id = (flask.request.view_args or {}).get("album_id")
    album = lib.get_album(album_id) if lib is not None and album_id is not None else None
    artpath = getattr(album, "artpath", None)
    if not artpath:
        return None
    path = os.fsdecode(artpath)
    if os.path.isfile(path) and os.access(path, os.R_OK):
        return None
    log.debug("album %s: artpath file is missing or unreadable; answering 404", album_id)
    return flask.Response(status=404)


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

        from .auth import require_webmanager_auth
        from .operations import webmanager_bp

        # Attach auth guard to blueprint
        webmanager_bp.before_request(require_webmanager_auth)

        # Beets 2.14 expands relative DB paths through a ContextVar only the
        # thread that built Library() has; Werkzeug serves each request on a
        # new thread. Bind it app-wide (after stock web's own before_request
        # sets g.lib) so stock routes -- /item/<id>/file, item and album
        # paths, path: queries, /album/<id>/art -- see absolute paths too.
        from .operations import bind_music_dir
        beets_web_app.before_request(lambda: bind_music_dir(getattr(flask.g, "lib", None)))
        beets_web_app.before_request(_missing_album_art_404)

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
