"""Beets Adapter — Direct HTTP adapter for stock Beets (beetsplug.web + beetsplug.webmanager).

Communicates over HTTP with the stock LinuxServer Beets container:
- Native reads via beetsplug.web REST endpoints (/stats, /artist/, /item/, /album/)
- Authenticated mutations via beetsplug.webmanager (/webmanager/*)
"""

import base64
import hashlib
import http.client
import os
from pathlib import Path
import json
import logging
import re
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Union

from backend.config_layers import redact_url_userinfo, url_has_userinfo

try:
    from backend.security import OutboundPolicyError
except ImportError:
    class OutboundPolicyError(Exception):
        pass

log = logging.getLogger("beets.adapter")

DEFAULT_BEETS_WEB_URL = "http://beets:8337"

# #208: a BEETS_WEB_URL with userinfo (user:pass@) never worked -- urllib
# sends no Basic header and parses "u:pw@host" as the host -- and the
# /webmanager routes authenticate with the Bearer key anyway. Such a URL is
# refused (fail closed); the message never echoes the credentials.
BEETS_WEB_URL_USERINFO_CODE = "beets_web_url_userinfo"
BEETS_WEB_URL_USERINFO_MESSAGE = (
    "BEETS_WEB_URL contains a user name or password (user:pass@host). Web Manager does not "
    "send URL credentials to Beets; remove them from BEETS_WEB_URL (for example "
    "http://beets:8337) and restart Web Manager."
)


def beets_web_url_has_userinfo(url: str) -> bool:
    """True for a URL that carries userinfo. Any "@" counts, also %40 and
    NFKC forms such as U+FF20: a base URL has no other use for it, and a
    password holding "/" or "?" hides the "@" from urlsplit's netloc."""
    return url_has_userinfo(url)


class BeetsAdapterError(Exception):
    """Base exception for Beets adapter errors.

    The message passed here becomes str(ex) and must be a stable, sanitized
    string safe to reach an HTTP response -- never raw upstream HTTP bodies,
    HTML, stack traces, filesystem paths, credentials, or plugin secrets.
    Raw upstream diagnostic detail belongs only in the server log (see
    `_request()`'s log.warning calls), never in this message or in
    `response_data`, which callers may also surface to users.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_data: Optional[Any] = None,
        error_code: str = "BEETS_ADAPTER_ERROR",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data
        self.error_code = error_code

    def to_public_dict(self) -> Dict[str, Any]:
        """Stable, sanitized fields safe to return directly in an HTTP error response."""
        return {
            "error": str(self),
            "error_code": self.error_code,
            "status_code": self.status_code,
        }


class BeetsAdapterAuthError(BeetsAdapterError):
    """Raised when authentication with Beets webmanager plugin fails (401)."""

    def __init__(self, message: str, status_code: int = 401, response_data: Optional[Any] = None, error_code: str = "BEETS_AUTH_FAILED"):
        super().__init__(message, status_code=status_code, response_data=response_data, error_code=error_code)


class BeetsAdapterNotFoundError(BeetsAdapterError):
    """Raised when a resource is not found (404)."""

    def __init__(self, message: str, status_code: int = 404, response_data: Optional[Any] = None, error_code: str = "BEETS_NOT_FOUND"):
        super().__init__(message, status_code=status_code, response_data=response_data, error_code=error_code)


class BeetsAdapterConnectionError(BeetsAdapterError):
    """Raised when Beets web server is unreachable."""

    def __init__(self, message: str, status_code: int = 0, response_data: Optional[Any] = None, error_code: str = "BEETS_UNREACHABLE"):
        super().__init__(message, status_code=status_code, response_data=response_data, error_code=error_code)


class BeetsAdapterTimeoutError(BeetsAdapterConnectionError):
    """Raised when a request to Beets web server times out."""

    def __init__(self, message: str, status_code: int = 0, response_data: Optional[Any] = None, error_code: str = "BEETS_TIMEOUT"):
        super().__init__(message, status_code=status_code, response_data=response_data, error_code=error_code)


class BeetsAdapterBadRequestError(BeetsAdapterError):
    """Raised when Beets returns HTTP 400 Bad Request."""

    def __init__(self, message: str, status_code: int = 400, response_data: Optional[Any] = None, error_code: str = "BEETS_BAD_REQUEST"):
        super().__init__(message, status_code=status_code, response_data=response_data, error_code=error_code)


class BeetsAdapterPathsUnavailableError(BeetsAdapterError):
    """Raised when Beets returns items but none of them carries a ``path``.

    Stock Beets only serialises item paths when ``web.include_paths: true``
    is set in its config.yaml. Returning an empty path list in that case
    would be indistinguishable from an empty library and could be misread
    by callers (for example as "every file on disk is untracked"), so path
    reads fail loudly with this typed error instead.
    """

    def __init__(
        self,
        message: str = (
            "Beets returned items without paths. Set web.include_paths: true in the "
            "Beets config.yaml (Setup offers a one-click fix) and restart the Beets container."
        ),
        status_code: int = 503,
        response_data: Optional[Any] = None,
        error_code: str = "BEETS_PATHS_UNAVAILABLE",
    ):
        super().__init__(message, status_code=status_code, response_data=response_data, error_code=error_code)


# Exception aliases for backward compatibility
BeetsError = BeetsAdapterError
BeetsAuthError = BeetsAdapterAuthError
BeetsNotFoundError = BeetsAdapterNotFoundError
BeetsBadRequestError = BeetsAdapterBadRequestError
BeetsUnavailableError = BeetsAdapterConnectionError
BeetsCommandError = BeetsAdapterError
BeetsClientError = BeetsAdapterError
BeetsPathsUnavailableError = BeetsAdapterPathsUnavailableError


class _ParsedQuery:
    """Parsed shape of one query term against StockBeetsLibrary's legacy-
    shaped `items()`/`albums()` compatibility fallback below. Defined
    locally in this module (not imported from the retired control-agent
    HTTP client) so this module has zero import dependency on that
    retired module -- both raise BeetsAdapterError, never that other
    module's own exception hierarchy."""

    def __init__(self, target: str, field: Optional[str], value: str, operator: str = "equals"):
        self.target = target  # "items" or "albums"
        self.field = field    # field name e.g. "album_id", "mb_albumid", or None for bare text
        self.value = value
        self.operator = operator  # "equals", "contains", "singleton"

    def __repr__(self):
        return f"<_ParsedQuery target={self.target!r} field={self.field!r} value={self.value!r} op={self.operator!r}>"


def _parse_query_term(term: str, target: str) -> "_ParsedQuery":
    """Parse and validate a query term for target ('items' or 'albums'). Raises BeetsAdapterError on invalid syntax/field."""
    if not isinstance(term, str):
        raise BeetsAdapterError(f"Query term must be a string, got {type(term).__name__}")
    q_str = term.strip()
    if not q_str:
        raise BeetsAdapterError("Query term cannot be empty or whitespace")

    if ":" in q_str:
        field, val = q_str.split(":", 1)
        field = field.strip()
        val = val.strip()
        if not field:
            raise BeetsAdapterError(f"Query field prefix cannot be empty in '{term}'")

        if target == "items":
            allowed_fields = {"album_id", "album", "artist", "title", "path", "mb_trackid", "mbid", "singleton"}
            if field not in allowed_fields:
                raise BeetsAdapterError(f"Unsupported query field '{field}' in '{term}'")
            if not val and field != "singleton":
                raise BeetsAdapterError(f"Query field '{field}' requires a non-empty value in '{term}'")
            if field == "album_id":
                if not val.isdigit():
                    raise BeetsAdapterError(f"album_id must be an integer: {val!r}")
                return _ParsedQuery(target="items", field="album_id", value=val, operator="equals")
            elif field == "singleton":
                if val.lower() not in {"true", "false"}:
                    raise BeetsAdapterError(f"singleton value must be 'true' or 'false': {val!r}")
                return _ParsedQuery(target="items", field="singleton", value=val.lower(), operator="singleton")
            elif field in {"mb_trackid", "mbid"}:
                return _ParsedQuery(target="items", field="mb_trackid", value=val, operator="equals")
            elif field == "path":
                return _ParsedQuery(target="items", field="path", value=val, operator="equals")
            elif field in {"album", "artist", "title"}:
                return _ParsedQuery(target="items", field=field, value=val, operator="contains")

        elif target == "albums":
            allowed_fields = {"mb_albumid", "mb_releasegroupid", "album", "artist", "albumartist"}
            if field not in allowed_fields:
                raise BeetsAdapterError(f"Unsupported query field '{field}' in '{term}'")
            if not val:
                raise BeetsAdapterError(f"Query field '{field}' requires a non-empty value in '{term}'")
            if field == "mb_albumid":
                return _ParsedQuery(target="albums", field="mb_albumid", value=val, operator="equals")
            elif field == "mb_releasegroupid":
                return _ParsedQuery(target="albums", field="mb_releasegroupid", value=val, operator="equals")
            elif field in {"album", "artist", "albumartist"}:
                return _ParsedQuery(target="albums", field=field, value=val, operator="contains")

    # Bare-word query
    return _ParsedQuery(target=target, field=None, value=q_str, operator="contains")


class BeetsAdapter:
    """Client for Stock Beets Web & WebManager Plugin APIs."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_key_file: Optional[str] = None,
        timeout: float = 30.0,
    ):
        # One default everywhere: the stock Beets Compose service. The old
        # fallback (http://127.0.0.1:8337) pointed at Web Manager itself.
        raw_url = (
            base_url
            or (os.environ.get("BEETS_WEB_URL") or "").strip()
            or DEFAULT_BEETS_WEB_URL
        )
        raw_url = raw_url.rstrip("/")
        # #208: every log line and error message uses this redacted form.
        self._display_url = redact_url_userinfo(raw_url)
        # A userinfo URL is refused here, at construction: no request is ever
        # sent with it and base_url (also used by get_item_file_url /
        # get_album_art_url) never holds the credentials. The module-level
        # adapter is built at import, so this is a refused state rather than
        # an exception that would stop Web Manager (and its setup page).
        self.config_error_code = BEETS_WEB_URL_USERINFO_CODE if beets_web_url_has_userinfo(raw_url) else ""
        self.base_url = self._display_url if self.config_error_code else raw_url
        self._api_key = api_key or os.environ.get("BEETS_WEBMANAGER_API_KEY")
        self._api_key_file = (
            api_key_file
            or os.environ.get("BEETS_WEBMANAGER_API_KEY_FILE")
            or "/config/.webmanager_api_key"
        )
        self.timeout = timeout

        # Bounded cache for get_items_page() -- see the note on
        # _ITEMS_PAGE_CACHE_TTL_SECONDS above get_items_page() for why this
        # exists (no real upstream pagination) and why the TTL is short.
        self._items_page_cache: Optional[List[Dict[str, Any]]] = None
        self._items_page_cache_ts: float = 0.0
        self._items_page_cache_lock = threading.Lock()

    @property
    def api_key(self) -> str:
        """Resolve current API key from memory, env, or file."""
        if self._api_key:
            return self._api_key
        env_key = os.environ.get("BEETS_WEBMANAGER_API_KEY", "").strip()
        if env_key:
            return env_key

        candidate_files = []
        if self._api_key_file:
            candidate_files.append(Path(self._api_key_file))
        if os.environ.get("BEETS_WEBMANAGER_API_KEY_FILE"):
            candidate_files.append(Path(os.environ["BEETS_WEBMANAGER_API_KEY_FILE"]))
        if os.environ.get("BEETS_CONFIG"):
            candidate_files.append(Path(os.environ["BEETS_CONFIG"]).parent / ".webmanager_api_key")
        if os.environ.get("BEETSDIR"):
            candidate_files.append(Path(os.environ["BEETSDIR"]) / ".webmanager_api_key")
        candidate_files.append(Path("/config/.webmanager_api_key"))

        for p in candidate_files:
            try:
                if p.is_file() and not p.is_symlink():
                    content = p.read_text(encoding="utf-8").strip()
                    if content:
                        return content
            except Exception as ex:
                log.debug("Could not read API key from %s: %s", p, ex)
        return ""

    def _refuse_if_misconfigured(self) -> None:
        if self.config_error_code:
            raise BeetsAdapterConnectionError(
                BEETS_WEB_URL_USERINFO_MESSAGE, error_code=self.config_error_code.upper()
            )

    def _build_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        self._refuse_if_misconfigured()
        url = self._build_url(path)
        if params:
            query_string = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if query_string:
                url = f"{url}?{query_string}"

        req_headers = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        body = None
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        # WebManager routes require Authorization Bearer token
        if path.startswith("/webmanager") or "/webmanager/" in path:
            token = self.api_key
            if token and "Authorization" not in req_headers:
                req_headers["Authorization"] = f"Bearer {token}"
            elif not token and "Authorization" not in req_headers:
                log.warning("No WebManager API key available when requesting %s", path)

        try:
            # Inside the try: Request() parses the URL and its ValueError
            # can quote it (#208).
            req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                data = resp.read()
                if "application/json" in content_type:
                    try:
                        return json.loads(data.decode("utf-8"))
                    except Exception as json_err:
                        raise BeetsAdapterError(
                            f"Malformed JSON response from Beets server at {redact_url_userinfo(url)}: {json_err}",
                            error_code="MALFORMED_RESPONSE",
                        ) from json_err
                return data
        except urllib.error.HTTPError as ex:
            status = ex.code
            err_body = ex.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
            except Exception:
                err_json = {"raw": err_body}

            # Raw upstream response bodies (potentially raw HTML error pages,
            # internal stack traces, or unexpected content) are logged
            # server-side ONLY -- never embedded in the exception message,
            # which callers may surface directly in an HTTP response.
            log.warning("Beets upstream error %s on %s: %s", status, path, err_body[:2000])

            # If our own webmanager plugin returned a structured error_code,
            # carry it forward -- it is already a sanitized, stable field.
            upstream_error_code = err_json.get("error_code") if isinstance(err_json, dict) else None

            if status == 400:
                raise BeetsAdapterBadRequestError(
                    f"Beets rejected the request on {path} (bad request)",
                    status_code=status,
                    response_data=err_json,
                    error_code=upstream_error_code or "BEETS_BAD_REQUEST",
                )
            if status == 401:
                raise BeetsAdapterAuthError(
                    f"Beets authentication failed on {path}",
                    status_code=status,
                    response_data=err_json,
                    error_code=upstream_error_code or "BEETS_AUTH_FAILED",
                )
            if status == 404:
                raise BeetsAdapterNotFoundError(
                    f"Beets resource not found on {path}",
                    status_code=status,
                    response_data=err_json,
                    error_code=upstream_error_code or "BEETS_NOT_FOUND",
                )
            raise BeetsAdapterError(
                f"Beets request failed on {path} (status {status})",
                status_code=status,
                response_data=err_json,
                error_code=upstream_error_code or "BEETS_UPSTREAM_ERROR",
            )
        except TimeoutError as ex:
            log.warning("Timeout connecting to Beets server at %s (%s): %s", self._display_url, path, ex)
            raise BeetsAdapterTimeoutError(
                f"Timeout connecting to Beets server at {self._display_url}"
            ) from ex
        except urllib.error.URLError as ex:
            reason = getattr(ex, "reason", None)
            log.warning("Beets connection error at %s (%s): %s", self._display_url, path, ex)
            if isinstance(reason, TimeoutError) or "timed out" in str(ex).lower():
                raise BeetsAdapterTimeoutError(
                    f"Timeout connecting to Beets server at {self._display_url}"
                ) from ex
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from ex
        except (OutboundPolicyError, ConnectionError, OSError) as ex:
            log.warning("Beets connection error at %s (%s): %s", self._display_url, path, ex)
            if isinstance(ex, TimeoutError) or "timed out" in str(ex).lower():
                raise BeetsAdapterTimeoutError(
                    f"Timeout connecting to Beets server at {self._display_url}"
                ) from ex
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from ex
        except (http.client.HTTPException, ValueError) as ex:
            # After OutboundPolicyError (a ValueError subclass with fixed,
            # loggable reasons).
            # #208: e.g. InvalidURL("nonnumeric port: '<pw>@host'"). Never
            # log or raise str(ex); it can hold part of the URL.
            log.warning("Beets connection error at %s (%s): %s", self._display_url, path, type(ex).__name__)
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from None

    # -------------------------------------------------------------------------
    # Native Upstream Read Endpoints (beetsplug.web)
    # -------------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Fetch library statistics from GET /stats."""
        res = self._request("GET", "/stats")
        return res if isinstance(res, dict) else {}

    def get_artists(self) -> List[str]:
        """Fetch all artist names from GET /artist/."""
        res = self._request("GET", "/artist/")
        if isinstance(res, dict):
            names = res.get("artist_names") or res.get("artist") or []
            return list(names)
        return []

    def _build_query_path(self, entity: str, query: Optional[Union[str, List[str]]]) -> str:
        """Build upstream query path using slash-separated query terms supported by QueryConverter."""
        if not query:
            return f"/{entity}/"
        if isinstance(query, str):
            q_str = query.strip()
            if not q_str:
                return f"/{entity}/"
            try:
                import shlex
                terms = shlex.split(q_str)
            except Exception:
                terms = q_str.split()
        elif isinstance(query, (list, tuple)):
            terms = [str(t).strip() for t in query if str(t).strip()]
        else:
            terms = [str(query).strip()]

        if not terms:
            return f"/{entity}/"

        encoded = "/".join(urllib.parse.quote(t, safe="") for t in terms)
        return f"/{entity}/query/{encoded}"

    def get_items(self, query: Optional[Union[str, List[str]]] = None) -> List[Dict[str, Any]]:
        """Fetch items matching query (or all items) from GET /item/ or /item/query/<queries>."""
        path = self._build_query_path("item", query)
        res = self._request("GET", path)
        if isinstance(res, dict):
            return res.get("items") or res.get("results") or []
        return []

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single item by ID from GET /item/<id>."""
        try:
            res = self._request("GET", f"/item/{int(item_id)}")
            return res if isinstance(res, dict) else None
        except BeetsAdapterNotFoundError:
            return None

    def get_albums(self, query: Optional[Union[str, List[str]]] = None) -> List[Dict[str, Any]]:
        """Fetch albums matching query (or all albums) from GET /album/ or /album/query/<queries>."""
        path = self._build_query_path("album", query)
        res = self._request("GET", path)
        if isinstance(res, dict):
            return res.get("albums") or res.get("results") or []
        return []

    def get_album(self, album_id: int, expand: bool = True) -> Optional[Dict[str, Any]]:
        """Fetch single album by ID from GET /album/<id>?expand."""
        params = {"expand": ""} if expand else None
        try:
            res = self._request("GET", f"/album/{int(album_id)}", params=params)
            return res if isinstance(res, dict) else None
        except BeetsAdapterNotFoundError:
            return None

    def get_unique_field_values(
        self, entity: str, key: str, sort_key: Optional[str] = None
    ) -> List[Any]:
        """Fetch unique values for a field from GET /{entity}/values/{key}."""
        params = {"sort_key": sort_key} if sort_key else None
        res = self._request("GET", f"/{entity}/values/{key}", params=params)
        if isinstance(res, dict):
            return res.get("values") or []
        return []

    def get_item_file_url(self, item_id: int) -> str:
        """Get the URL for streaming/downloading an item audio file."""
        return self._build_url(f"/item/{int(item_id)}/file")

    def get_album_art_url(self, album_id: int) -> str:
        """Get the URL for album cover art."""
        return self._build_url(f"/album/{int(album_id)}/art")

    def open_item_file(self, item_id: int):
        """Open raw HTTP response stream for an item audio file."""
        self._refuse_if_misconfigured()
        url = self._build_url(f"/item/{int(item_id)}/file")
        try:
            req = urllib.request.Request(url)  # inside the try (#208)
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                raise BeetsAdapterNotFoundError(
                    f"Audio file for item {item_id} not found", status_code=404
                ) from ex
            raise BeetsAdapterError(
                f"Error retrieving audio file for item {item_id}",
                status_code=ex.code,
                error_code="BEETS_UPSTREAM_ERROR",
            ) from ex
        except TimeoutError as ex:
            log.warning("Timeout streaming item %s file from %s: %s", item_id, self._display_url, ex)
            raise BeetsAdapterTimeoutError(
                f"Timeout connecting to Beets server at {self._display_url}"
            ) from ex
        except urllib.error.URLError as ex:
            reason = getattr(ex, "reason", None)
            log.warning("Connection error streaming item %s file from %s: %s", item_id, self._display_url, ex)
            if isinstance(reason, TimeoutError) or "timed out" in str(ex).lower():
                raise BeetsAdapterTimeoutError(
                    f"Timeout connecting to Beets server at {self._display_url}"
                ) from ex
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from ex
        except (OutboundPolicyError, ConnectionError, OSError) as ex:
            log.warning("Connection error streaming item %s file from %s: %s", item_id, self._display_url, ex)
            if isinstance(ex, TimeoutError) or "timed out" in str(ex).lower():
                raise BeetsAdapterTimeoutError(
                    f"Timeout connecting to Beets server at {self._display_url}"
                ) from ex
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from ex
        except (http.client.HTTPException, ValueError) as ex:
            # After OutboundPolicyError (a ValueError subclass with fixed,
            # loggable reasons).
            # #208: e.g. InvalidURL("nonnumeric port: '<pw>@host'"). Never
            # log or raise str(ex); it can hold part of the URL.
            log.warning("Connection error streaming item %s file from %s: %s", item_id, self._display_url, type(ex).__name__)
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from None

    def open_album_art(self, album_id: int):
        """Open raw HTTP response stream for an album cover art."""
        self._refuse_if_misconfigured()
        url = self._build_url(f"/album/{int(album_id)}/art")
        try:
            req = urllib.request.Request(url)  # inside the try (#208)
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                raise BeetsAdapterNotFoundError(
                    f"Art for album {album_id} not found", status_code=404
                ) from ex
            raise BeetsAdapterError(
                f"Error retrieving art for album {album_id}",
                status_code=ex.code,
                error_code="BEETS_UPSTREAM_ERROR",
            ) from ex
        except TimeoutError as ex:
            log.warning("Timeout streaming album %s art from %s: %s", album_id, self._display_url, ex)
            raise BeetsAdapterTimeoutError(
                f"Timeout connecting to Beets server at {self._display_url}"
            ) from ex
        except urllib.error.URLError as ex:
            reason = getattr(ex, "reason", None)
            log.warning("Connection error streaming album %s art from %s: %s", album_id, self._display_url, ex)
            if isinstance(reason, TimeoutError) or "timed out" in str(ex).lower():
                raise BeetsAdapterTimeoutError(
                    f"Timeout connecting to Beets server at {self._display_url}"
                ) from ex
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from ex
        except (OutboundPolicyError, ConnectionError, OSError) as ex:
            log.warning("Connection error streaming album %s art from %s: %s", album_id, self._display_url, ex)
            if isinstance(ex, TimeoutError) or "timed out" in str(ex).lower():
                raise BeetsAdapterTimeoutError(
                    f"Timeout connecting to Beets server at {self._display_url}"
                ) from ex
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from ex
        except (http.client.HTTPException, ValueError) as ex:
            # After OutboundPolicyError (a ValueError subclass with fixed,
            # loggable reasons).
            # #208: e.g. InvalidURL("nonnumeric port: '<pw>@host'"). Never
            # log or raise str(ex); it can hold part of the URL.
            log.warning("Connection error streaming album %s art from %s: %s", album_id, self._display_url, type(ex).__name__)
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self._display_url}"
            ) from None

    # -------------------------------------------------------------------------
    # Integration Plugin Mutation Endpoints (/webmanager/*)
    # -------------------------------------------------------------------------

    def get_plugin_status(self) -> Dict[str, Any]:
        """Fetch WebManager plugin healthcheck & capability status."""
        return self._request("GET", "/webmanager/status")

    def get_operation(self, operation_id: str) -> Dict[str, Any]:
        """Fetch long-running operation status from GET /webmanager/operations/<id>."""
        return self._request("GET", f"/webmanager/operations/{operation_id}")

    def run_import(
        self,
        paths: Union[str, List[str]],
        autotag: bool = False,
        duplicate_action: str = "skip",
        copy: bool = False,
        move: bool = True,
        write: bool = True,
        incremental: bool = False,
        singletons: bool = False,
        pretend: bool = False,
        set_fields: Optional[Dict[str, Any]] = None,
        is_async: bool = False,
        idempotency_key: Optional[str] = None,
        search_ids: Optional[List[str]] = None,
        quiet_fallback: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute a non-interactive import with Beets' own ImportSession.

        ``search_ids`` and ``quiet_fallback`` are Beets' native
        ``import.search_ids`` / ``import.quiet_fallback`` settings (the
        ``--search-id`` flag and quiet-mode fallback). They are only sent
        when given, so a plugin that does not know them never sees them.
        """
        payload = {
            "paths": [paths] if isinstance(paths, str) else paths,
            "autotag": autotag,
            "duplicate_action": duplicate_action,
            "copy": copy,
            "move": move,
            "write": write,
            "incremental": incremental,
            "singletons": singletons,
            "pretend": pretend,
            "set_fields": set_fields or {},
            "async": is_async,
        }
        if search_ids:
            payload["search_ids"] = list(search_ids)
        if quiet_fallback:
            payload["quiet_fallback"] = quiet_fallback
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        return self._request("POST", "/webmanager/import", json_data=payload, headers=headers, timeout=timeout)

    def modify(
        self,
        fields: Dict[str, Any],
        item_ids: Optional[List[int]] = None,
        album_ids: Optional[List[int]] = None,
        query: Optional[str] = None,
        write: bool = True,
        move: bool = True,
    ) -> Dict[str, Any]:
        """Modify metadata fields on items or albums."""
        payload = {
            "fields": fields,
            "item_ids": item_ids or [],
            "album_ids": album_ids or [],
            "query": query,
            "write": write,
            "move": move,
        }
        return self._request("POST", "/webmanager/modify", json_data=payload)

    def remove(
        self,
        item_ids: Optional[List[int]] = None,
        album_ids: Optional[List[int]] = None,
        delete_files: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Remove explicit items/albums via the stock-Beets integration
        plugin. delete_files defaults False -- physical file deletion is
        never implicit."""
        payload = {
            "item_ids": item_ids or [],
            "album_ids": album_ids or [],
            "delete_files": delete_files,
        }
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/remove", json_data=payload, headers=headers)

    def move(
        self,
        item_ids: Optional[List[int]] = None,
        album_ids: Optional[List[int]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move explicit items/albums to their configured library location
        via the stock-Beets integration plugin. Never accepts an arbitrary
        destination path -- Beets' own path templates remain authoritative."""
        payload = {"item_ids": item_ids or [], "album_ids": album_ids or []}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/move", json_data=payload, headers=headers)

    def replace_item_file(
        self,
        target_item_id: int,
        source_item_id: int,
        idempotency_key: Optional[str] = None,
        displace_destination_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make the tracked source item's file the target (album-slot)
        item's file via the stock-Beets integration plugin. The target
        keeps its identity and tags; its old file is quarantined by the
        engine (never deleted); the source row is removed; Beets moves the
        file to its canonical path."""
        payload = {"target_item_id": int(target_item_id), "source_item_id": int(source_item_id)}
        if displace_destination_sha256:
            # An untracked, reviewed occupant of the canonical destination
            # (identified by content hash) may be moved to the engine quarantine.
            payload["displace_destination_sha256"] = displace_destination_sha256
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/replace-item-file", json_data=payload, headers=headers)

    def rollback_replace_item_file(
        self,
        quarantine_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Undo replace_item_file(). The engine restores from its own
        manifest; only the quarantine_id it returned is sent."""
        payload = {"quarantine_id": str(quarantine_id or "")}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/replace-item-file/rollback", json_data=payload, headers=headers)

    def set_album_art(
        self,
        album_id: int,
        image: bytes,
        expected_mb_releasegroupid: str = "",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make ``image`` the album's cover inside Beets (plugin 1.12.0):
        Album.set_art() puts it in the album folder and sets artpath, and
        embedart embeds it when it is loaded. The engine keeps the previous
        cover and embedded images and returns their id (``art_id``)."""
        payload = {"album_id": int(album_id), "image_b64": base64.b64encode(image).decode("ascii"),
                   "image_sha256": hashlib.sha256(image).hexdigest(),
                   "expected_mb_releasegroupid": str(expected_mb_releasegroupid or "")}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        # Embedding rewrites every track of the album.
        return self._request("POST", "/webmanager/album-art", json_data=payload, headers=headers,
                             timeout=max(self.timeout or 0, 300))

    def rollback_album_art(
        self,
        art_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Undo set_album_art(); the engine restores from its own manifest."""
        payload = {"art_id": str(art_id or "")}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/album-art/rollback", json_data=payload, headers=headers,
                             timeout=max(self.timeout or 0, 300))

    def quarantine_remove_items(
        self,
        items: List[Dict[str, Any]],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Remove tracked items through the engine, moving each file into the
        engine quarantine (never deleted). ``items`` is a list of
        {"item_id", "sha256"} plus, to retire a duplicate album row the item is
        the only member of, {"retire_album_id", "sibling_keeper_item_id"}; the
        engine refuses the whole request if any file changed since review."""
        payload = {"items": [{"item_id": int(i["item_id"]), "sha256": str(i["sha256"]),
                              **({"retire_album_id": int(i["retire_album_id"]),
                                  "sibling_keeper_item_id": int(i["sibling_keeper_item_id"])}
                                 if i.get("retire_album_id") else {})} for i in items]}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/quarantine-remove-items", json_data=payload, headers=headers)

    def rollback_quarantine_remove_items(
        self,
        quarantine_id: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Undo quarantine_remove_items() from the engine's own manifest."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/quarantine-remove-items/rollback",
                             json_data={"quarantine_id": str(quarantine_id or "")}, headers=headers)

    def album_row_merge(
        self,
        target_album_id: int,
        source_album_ids: List[int],
        items: List[Dict[str, Any]],
        expected_release_group_id: str,
        expected_release_id: str,
        idempotency_key: str,
        partial: bool = False,
    ) -> Dict[str, Any]:
        """Move every item of the source album rows into the target row
        (ownership only) through the stock-Beets integration plugin; the
        engine re-proves identity, content and slots first (ARCH-020).
        ``partial`` moves only the listed items; a source row is then retired
        only when all of its items moved."""
        payload = {
            "target_album_id": int(target_album_id),
            "source_album_ids": [int(x) for x in source_album_ids],
            "items": items,
            "expected_release_group_id": expected_release_group_id,
            "expected_release_id": expected_release_id,
        }
        if partial:
            payload["partial"] = True
        return self._request("POST", "/webmanager/album-row-merge", json_data=payload,
                             headers={"Idempotency-Key": idempotency_key})

    def get_album_row_merge(self, merge_id: str) -> Dict[str, Any]:
        """The engine's durable record of an album-row merge."""
        return self._request("GET", f"/webmanager/album-row-merge/{merge_id}")

    def rollback_album_row_merge(self, merge_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Undo album_row_merge() from the engine's own manifest."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/album-row-merge/rollback",
                             json_data={"merge_id": str(merge_id or "")}, headers=headers)

    def untracked_attach_album(self, release_id: str, release_group_id: str, files: List[Dict[str, Any]],
                               idempotency_key: str) -> Dict[str, Any]:
        """Add untracked files of one release that has no album row yet as a
        new album row, in place: no tag write, no move (ARCH-021)."""
        payload = {"release_id": release_id, "release_group_id": release_group_id, "files": files}
        return self._request("POST", "/webmanager/untracked/attach-album", json_data=payload,
                             headers={"Idempotency-Key": idempotency_key})

    def untracked_attach(self, path: str, sha256: str, album_id: Optional[int], expected: Dict[str, Any],
                         idempotency_key: str) -> Dict[str, Any]:
        """Add an untracked file to the library (album slot or singleton)
        without writing or moving it (ARCH-021)."""
        payload = {"path": path, "sha256": sha256, "album_id": album_id, "expected": expected}
        return self._request("POST", "/webmanager/untracked/attach", json_data=payload,
                             headers={"Idempotency-Key": idempotency_key})

    def untracked_quarantine(self, files: List[Dict[str, Any]], idempotency_key: str) -> Dict[str, Any]:
        """Move reviewed untracked files into the engine quarantine (ARCH-021)."""
        return self._request("POST", "/webmanager/untracked/quarantine", json_data={"files": files},
                             headers={"Idempotency-Key": idempotency_key})

    def untracked_rollback(self, record_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/webmanager/untracked/rollback", json_data={"record_id": str(record_id or "")},
                             headers=headers)

    def folder_op(self, op: str, idempotency_key: str, **paths: str) -> Dict[str, Any]:
        """One folder_cleanup_v1 step inside Beets (plugin 1.7.0): ``move_file``
        or ``rename_dir`` (source, target), ``remove_empty_dir`` or
        ``create_dir`` (path). Web Manager mounts the library read-only.
        Plugin 1.9.0 answers 503 ``BUSY`` (raised with ``error_code`` BUSY)
        when another mutation holds its lock; the step was not registered
        and did not run, so it is safe to retry later."""
        return self._request("POST", "/webmanager/folder-op", json_data={"op": op, **paths},
                             headers={"Idempotency-Key": idempotency_key})

    def get_untracked_record(self, record_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/webmanager/untracked/{record_id}")

    def mbsync(
        self,
        item_ids: Optional[List[int]] = None,
        album_ids: Optional[List[int]] = None,
        move: bool = False,
        pretend: bool = False,
        write: bool = True,
        is_async: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sync metadata from MusicBrainz for explicit items/albums via the
        real Beets mbsync plugin, through the stock-Beets integration
        plugin."""
        payload = {
            "item_ids": item_ids or [],
            "album_ids": album_ids or [],
            "move": move,
            "pretend": pretend,
            "write": write,
            "async": is_async,
        }
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._request("POST", "/webmanager/mbsync", json_data=payload, headers=headers)

    def fetch_art(
        self,
        album_ids: List[int],
        force: bool = False,
        is_async: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch cover art for explicit albums via the real Beets fetchart
        plugin, through the stock-Beets integration plugin."""
        payload = {"album_ids": album_ids, "force": force, "async": is_async}
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._request("POST", "/webmanager/fetchart", json_data=payload, headers=headers)

    def embed_art(
        self,
        album_ids: List[int],
        is_async: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Embed each album's existing artwork into its items' tags via the
        real Beets embedart plugin, through the stock-Beets integration
        plugin."""
        payload = {"album_ids": album_ids, "async": is_async}
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._request("POST", "/webmanager/embedart", json_data=payload, headers=headers)

    def lastgenre(
        self,
        album_ids: List[int],
        force: bool = False,
        is_async: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Repair genre tags for explicit albums via the real Beets
        lastgenre plugin, through the stock-Beets integration plugin."""
        payload = {"album_ids": album_ids, "force": force, "async": is_async}
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._request("POST", "/webmanager/lastgenre", json_data=payload, headers=headers)

    def mbsubmit(
        self,
        item_ids: List[int],
        api_key: Optional[str] = None,
        is_async: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit AcoustID fingerprints for explicit items via the real
        Beets chroma plugin, through the stock-Beets integration plugin."""
        payload = {"item_ids": item_ids, "api_key": api_key, "async": is_async}
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._request("POST", "/webmanager/mbsubmit", json_data=payload, headers=headers)

    # -------------------------------------------------------------------------
    # Caller Compatibility Helpers
    # -------------------------------------------------------------------------

    # Upstream limitation (A5): stock beetsplug.web has no real server-side
    # pagination -- GET /item/ always returns the whole library. This is not
    # something Web Manager can fix without building a custom SQL/pagination
    # endpoint against Beets' own database, which the architecture invariant
    # forbids (Beets Web Manager does not become a second owner of
    # musiclibrary.blb). get_items_page() therefore genuinely fetches the
    # full item list and slices it in Python -- it does not pretend
    # otherwise. To avoid repeating that full-library fetch on every single
    # page request within one browsing session/workflow (e.g. a UI paging
    # through results, or a job iterating pages), the full list is cached
    # for a short, bounded TTL. This is a latency/load mitigation, not real
    # pagination, and it intentionally stays short so a concurrent
    # import/modify is reflected again within a few seconds.
    _ITEMS_PAGE_CACHE_TTL_SECONDS = 5.0

    def _get_all_items_cached(self) -> List[Dict[str, Any]]:
        with self._items_page_cache_lock:
            now = time.monotonic()
            if (
                self._items_page_cache is not None
                and (now - self._items_page_cache_ts) < self._ITEMS_PAGE_CACHE_TTL_SECONDS
            ):
                return self._items_page_cache

        # Fetch outside the lock -- get_items() is a network call and must
        # not block other threads' cache reads while it's in flight.
        fresh_items = self.get_items()

        with self._items_page_cache_lock:
            self._items_page_cache = fresh_items
            self._items_page_cache_ts = time.monotonic()
            return self._items_page_cache

    def get_items_page(self, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        """Fetch a paginated page of items.

        Upstream beetsplug.web does not expose real pagination (see the
        note above _ITEMS_PAGE_CACHE_TTL_SECONDS) -- this slices a
        short-TTL-cached full item list in Python rather than issuing a
        genuine bounded query, and is truthful about that rather than
        pretending otherwise.
        """
        all_items = self._get_all_items_cached()
        total = len(all_items)
        off = max(0, offset)
        lim = max(1, limit)
        page = all_items[off : off + lim]
        return {
            "items": page,
            "offset": off,
            "limit": lim,
            "returned": len(page),
            "total": total,
        }

    def find_all_items_by_album_id(self, album_id: int) -> List[Dict[str, Any]]:
        """Compatibility helper returning all items for an album."""
        album = self.get_album(int(album_id), expand=True)
        if album and "items" in album and isinstance(album["items"], list):
            return album["items"]
        return self.get_items(f"album_id:{album_id}")

    def find_all_albums_by_albumartist(self, albumartist: str) -> List[Dict[str, Any]]:
        """Compatibility helper returning all albums for an albumartist."""
        return self.get_albums(f"albumartist:{albumartist}")

    def find_all_albums_by_mb_albumid(self, mb_albumid: str) -> List[Dict[str, Any]]:
        """Compatibility helper returning all albums by MusicBrainz album ID."""
        return self.get_albums(f"mb_albumid:{mb_albumid}")

    def find_all_albums_by_releasegroupid(self, rgid: str) -> List[Dict[str, Any]]:
        """Compatibility helper returning all albums by MusicBrainz release group ID."""
        return self.get_albums(f"mb_releasegroupid:{rgid}")

    def find_all_items_by_mbid(self, mbid: str) -> List[Dict[str, Any]]:
        """Compatibility helper returning all items by MusicBrainz track ID."""
        return self.get_items(f"mb_trackid:{mbid}")

    def find_item_by_path(self, path: str) -> Optional[Dict[str, Any]]:
        """Compatibility helper finding a single item by path."""
        items = self.get_items(f"path:{path}")
        return items[0] if items else None

    def find_items_by_query(self, query: str) -> List[Dict[str, Any]]:
        """Compatibility helper finding items matching query."""
        return self.get_items(query)

    def find_albums_by_query(self, query: str) -> List[Dict[str, Any]]:
        """Compatibility helper finding albums matching query."""
        return self.get_albums(query)

    def list_all_items(self) -> List[Dict[str, Any]]:
        """Compatibility helper returning all library items."""
        return self.get_items()

    def list_all_albums(self) -> List[Dict[str, Any]]:
        """Compatibility helper returning all library albums."""
        return self.get_albums()

    def list_distinct_albumartists(self) -> List[str]:
        """Compatibility helper returning list of distinct album artists."""
        artists = self.get_artists()
        if artists:
            return artists
        albums = self.get_albums()
        seen = set()
        result = []
        for a in albums:
            name = (a.get("albumartist") or a.get("artist") or "").strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        return sorted(result)

    @staticmethod
    def _require_item_paths(items: List[Dict[str, Any]]) -> None:
        """Raise BeetsAdapterPathsUnavailableError when items exist but carry no path."""
        if items and not any(it.get("path") for it in items if isinstance(it, dict)):
            raise BeetsAdapterPathsUnavailableError()

    def list_distinct_item_paths(self) -> List[str]:
        """Compatibility helper returning all distinct item paths."""
        items = self.get_items()
        self._require_item_paths(items)
        paths = []
        for i in items:
            p = i.get("path")
            if p:
                if isinstance(p, (bytes, bytearray)):
                    p = p.decode("utf-8", errors="replace")
                paths.append(str(p))
        return paths

    @staticmethod
    def _decode_path(value: Any) -> str:
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    def list_item_paths(self, details: bool = False) -> Any:
        """Fetch distinct item paths, or full item id/album_id/path dicts if details=True.

        Contract-compatible with the legacy BeetsClient.list_item_paths():
        details=False returns a list of distinct path strings; details=True
        returns one {"id", "album_id", "path"} record per item (not
        deduplicated by path).

        Raises BeetsAdapterPathsUnavailableError when Beets returns items
        without paths (``web.include_paths`` disabled) instead of returning
        an empty list that looks like an empty library.
        """
        items = self.get_items()
        self._require_item_paths(items)
        if details:
            records: List[Dict[str, Any]] = []
            for it in items:
                aid = it.get("album_id")
                records.append({
                    "id": it.get("id"),
                    "album_id": int(aid) if aid is not None else None,
                    "path": self._decode_path(it.get("path")),
                })
            return records

        seen = set()
        paths: List[str] = []
        for it in items:
            p = self._decode_path(it.get("path"))
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
        return paths

    def get_artist_counts(self) -> Dict[str, Dict[str, int]]:
        """Fetch album and track counts grouped by albumartist.

        Contract-compatible with the legacy BeetsClient.get_artist_counts():
        {albumartist: {"albums": <distinct album count>, "tracks": <item count>}},
        matching the legacy control agent's
        "SELECT albumartist, COUNT(DISTINCT albums.id), COUNT(items.id) FROM
        albums LEFT JOIN items ON items.album_id = albums.id WHERE
        albumartist != '' GROUP BY albumartist" semantics: track counts are
        items belonging to an album with that albumartist, not items whose
        own artist field happens to match.
        """
        albums = self.get_albums()
        items = self.get_items()

        albumartist_by_album_id: Dict[int, str] = {}
        album_counts: Dict[str, int] = {}
        for a in albums:
            name = (a.get("albumartist") or "").strip()
            if not name:
                continue
            album_counts[name] = album_counts.get(name, 0) + 1
            aid = a.get("id")
            if aid is not None:
                albumartist_by_album_id[int(aid)] = name

        track_counts: Dict[str, int] = {}
        for it in items:
            aid = it.get("album_id")
            if aid is None:
                continue
            name = albumartist_by_album_id.get(int(aid))
            if name:
                track_counts[name] = track_counts.get(name, 0) + 1

        result: Dict[str, Dict[str, int]] = {}
        for name in set(album_counts) | set(track_counts):
            result[name] = {
                "albums": album_counts.get(name, 0),
                "tracks": track_counts.get(name, 0),
            }
        return result

    def find_all_orphan_albums(self) -> List[Dict[str, Any]]:
        """Find albums that have no item tracks."""
        albums = self.get_albums()
        orphans = []
        for a in albums:
            items = a.get("items")
            if items is None:
                aid = a.get("id")
                if aid:
                    items = self.find_all_items_by_album_id(int(aid))
                else:
                    items = []
            if not items:
                orphans.append(a)
        return orphans

    def get_album_cleanup_index(self) -> List[Dict[str, Any]]:
        """Construct joined album-item index for format and cleanup inspection."""
        items = self.get_items()
        albums = {int(a["id"]): a for a in self.get_albums() if a.get("id") is not None}
        rows: List[Dict[str, Any]] = []
        for item in items:
            aid = item.get("album_id")
            album_meta = albums.get(int(aid)) if aid is not None else {}
            row = dict(item)
            row["item_id"] = item.get("id")
            row["item_path"] = item.get("path")
            row["item_track"] = item.get("track")
            row["item_album_id"] = aid
            if album_meta:
                row["album_album"] = album_meta.get("album")
                row["album_albumartist"] = album_meta.get("albumartist")
            rows.append(row)
        return rows

    def update_item_metadata(
        self,
        item_id: int,
        fields: Dict[str, Any],
        force_write_tags: bool = True,
    ) -> Dict[str, Any]:
        """Compatibility helper for updating single item metadata."""
        return self.modify(
            fields=fields,
            item_ids=[item_id],
            write=force_write_tags,
            move=force_write_tags,
        )

    def update_album_metadata(
        self,
        album_id: int,
        fields: Dict[str, Any],
        force_write_tags: bool = True,
    ) -> Dict[str, Any]:
        """Compatibility helper for updating album metadata."""
        return self.modify(
            fields=fields,
            album_ids=[album_id],
            write=force_write_tags,
            move=force_write_tags,
        )


# -----------------------------------------------------------------------------
# Remote Facade & ORM Emulation for Stock Beets
# -----------------------------------------------------------------------------


class DictAttr:
    """Dictionary wrapper providing attribute and item access."""

    def __init__(self, data: Dict[str, Any]):
        object.__setattr__(self, "_data", data or {})

    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if name in data:
            val = data[name]
            if name == "path" and isinstance(val, (bytes, bytearray)):
                return val.decode("utf-8", errors="replace")
            return val
        return ""

    def __setattr__(self, name: str, value: Any) -> None:
        data = object.__getattribute__(self, "_data")
        data[name] = value

    def __getitem__(self, key: str) -> Any:
        return self.__getattr__(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.__setattr__(key, value)

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_data")

    def get(self, key: str, default: Any = "") -> Any:
        data = object.__getattribute__(self, "_data")
        val = data.get(key, default)
        return val if val is not None else default

    def keys(self):
        return object.__getattribute__(self, "_data").keys()

    def values(self):
        return object.__getattribute__(self, "_data").values()

    def items(self):
        return object.__getattribute__(self, "_data").items()

    def to_dict(self) -> Dict[str, Any]:
        return dict(object.__getattribute__(self, "_data"))

    def store(self):
        raise NotImplementedError(
            "Direct ORM .store() is not supported. Use beets_adapter.modify() "
            "to issue explicit updates."
        )

    def save(self):
        raise NotImplementedError(
            "Direct ORM .save() is not supported. Use beets_adapter.modify() "
            "to issue explicit updates."
        )

    def remove(self):
        raise NotImplementedError(
            "Direct ORM .remove() is not supported. Use explicit mutation endpoints."
        )


class RemoteItem(DictAttr):
    """Wrapper for a Beets library item record."""

    pass


class RemoteAlbum(DictAttr):
    """Wrapper for a Beets library album record."""

    def __init__(
        self, data: Dict[str, Any], adapter: Optional[BeetsAdapter] = None
    ):
        super().__init__(data)
        self._adapter = adapter or beets_adapter

    def items(self) -> List[RemoteItem]:
        """Return all item tracks belonging to this album."""
        raw_items = self._data.get("items")
        if isinstance(raw_items, list) and raw_items:
            return [RemoteItem(r) for r in raw_items]
        aid = self.id
        if not aid:
            return []
        if hasattr(self._adapter, "get_items"):
            items_data = self._adapter.get_items(f"album_id:{int(aid)}")
            return [RemoteItem(r) for r in items_data]
        if hasattr(self._adapter, "find_all_items_by_album_id"):
            items_data = self._adapter.find_all_items_by_album_id(int(aid))
            return [RemoteItem(r) for r in items_data]
        if hasattr(self._adapter, "find_items_by_album_id"):
            items_data = self._adapter.find_items_by_album_id(int(aid))
            return [RemoteItem(r) for r in items_data]
        return []


class StockBeetsLibrary:
    """Read-only RemoteLibrary facade backed by BeetsAdapter (stock Beets HTTP API)."""

    def __init__(self, adapter: Any = None):
        self.adapter = adapter or beets_adapter

    def get_item(self, iid: int) -> Optional[RemoteItem]:
        if not iid:
            return None
        data = self.adapter.get_item(int(iid))
        return RemoteItem(data) if data else None

    def get_album(self, aid: int) -> Optional[RemoteAlbum]:
        if not aid:
            return None
        if hasattr(self.adapter, "get_items"):
            data = self.adapter.get_album(int(aid), expand=True)
        elif hasattr(self.adapter, "get_album"):
            data = self.adapter.get_album(int(aid))
        else:
            data = None
        return RemoteAlbum(data, adapter=self.adapter) if data else None

    @staticmethod
    def _format_term(term: str) -> str:
        t = term.strip()
        if not t:
            return ""
        if ":" in t:
            field, val = t.split(":", 1)
            val = val.strip()
            if " " in val and not (
                (val.startswith('"') and val.endswith('"'))
                or (val.startswith("'") and val.endswith("'"))
            ):
                return f'{field}:"{val}"'
            return f"{field}:{val}"
        return t

    def _normalize_item_query(self, query: Any) -> Optional[str]:
        if query is None or query == [] or query == () or query == "":
            return None
        if isinstance(query, list):
            terms = []
            for term in query:
                if isinstance(term, str):
                    t = term.strip()
                    if t:
                        if t.startswith("mbid:"):
                            t = "mb_trackid:" + t[5:]
                        terms.append(self._format_term(t))
            return " ".join(t for t in terms if t) or None
        if isinstance(query, str):
            q = query.strip()
            if not q:
                return None
            if q.startswith("mbid:"):
                q = "mb_trackid:" + q[5:]
            return q
        raise BeetsAdapterError(f"Unsupported query shape: {query!r}")

    def _normalize_album_query(self, query: Any) -> Optional[str]:
        if query is None or query == [] or query == () or query == "":
            return None
        if isinstance(query, list):
            terms = []
            for term in query:
                if isinstance(term, str):
                    t = term.strip()
                    if t:
                        if t.startswith("mbid:"):
                            t = "mb_albumid:" + t[5:]
                        terms.append(self._format_term(t))
            return " ".join(t for t in terms if t) or None
        if isinstance(query, str):
            q = query.strip()
            if not q:
                return None
            if q.startswith("mbid:"):
                q = "mb_albumid:" + q[5:]
            return q
        raise BeetsAdapterError(f"Unsupported query shape: {query!r}")

    def items(self, query: Any = None) -> List[RemoteItem]:
        if hasattr(self.adapter, "get_items"):
            query_str = self._normalize_item_query(query)
            items_data = self.adapter.get_items(query_str)
            return [RemoteItem(r) for r in items_data]

        # Legacy BeetsClient-shaped adapter fallback for legacy unit tests
        if query is None or query == [] or query == () or query == "":
            items_data = self.adapter.list_all_items()
            return [RemoteItem(r) for r in items_data]

        if isinstance(query, list):
            if not query:
                items_data = self.adapter.list_all_items()
                return [RemoteItem(r) for r in items_data]

            parsed_list = [_parse_query_term(term, "items") for term in query]

            first_results = self.items(query[0])
            if not first_results or len(query) == 1:
                return first_results

            matching_ids = {item.id for item in first_results if item.id is not None}
            for term in query[1:]:
                if not matching_ids:
                    break
                term_results = self.items(term)
                term_ids = {item.id for item in term_results if item.id is not None}
                matching_ids = matching_ids & term_ids

            seen = set()
            final_items = []
            for item in first_results:
                if item.id in matching_ids and item.id not in seen:
                    seen.add(item.id)
                    final_items.append(item)
            return final_items

        if isinstance(query, str):
            pq = _parse_query_term(query, "items")
            if pq.field == "album_id":
                items_data = self.adapter.find_all_items_by_album_id(int(pq.value))
            elif pq.field == "mb_trackid":
                items_data = self.adapter.find_all_items_by_mbid(pq.value)
            elif pq.field == "path":
                items_data = self.adapter.find_all_items_by_path(pq.value)
            elif pq.field == "singleton":
                items_data = self.adapter.find_all_items_by_singleton(pq.value == "true")
            elif pq.field in {"album", "artist", "title"}:
                items_data = self.adapter.find_all_items_for_term(f"{pq.field}:{pq.value}")
            else:
                items_data = self.adapter.find_all_items_for_term(pq.value)

            return [RemoteItem(r) for r in items_data]

        raise BeetsAdapterError(f"Unsupported RemoteLibrary items() query shape: {query!r}")

    def albums(self, query: Any = None) -> List[RemoteAlbum]:
        if hasattr(self.adapter, "get_albums"):
            query_str = self._normalize_album_query(query)
            albums_data = self.adapter.get_albums(query_str)
            return [RemoteAlbum(r, adapter=self.adapter) for r in albums_data]

        # Legacy BeetsClient-shaped adapter fallback for legacy unit tests
        if query is None or query == [] or query == ():
            albums_data = self.adapter.list_all_albums()
            return [RemoteAlbum(r) for r in albums_data]

        if isinstance(query, str) and query == "":
            albums_data = self.adapter.list_all_albums()
            return [RemoteAlbum(r) for r in albums_data]

        if isinstance(query, list):
            if not query:
                albums_data = self.adapter.list_all_albums()
                return [RemoteAlbum(r) for r in albums_data]

            parsed_list = [_parse_query_term(term, "albums") for term in query]

            first_results = self.albums(query[0])
            if not first_results or len(query) == 1:
                return first_results

            matching_ids = {album.id for album in first_results if album.id is not None}
            for term in query[1:]:
                if not matching_ids:
                    break
                term_results = self.albums(term)
                term_ids = {album.id for album in term_results if album.id is not None}
                matching_ids = matching_ids & term_ids

            seen = set()
            final_albums = []
            for album in first_results:
                if album.id in matching_ids and album.id not in seen:
                    seen.add(album.id)
                    final_albums.append(album)
            return final_albums

        if isinstance(query, str):
            pq = _parse_query_term(query, "albums")
            if pq.field == "mb_albumid":
                albums_data = self.adapter.find_all_albums_by_mb_albumid(pq.value)
            elif pq.field == "mb_releasegroupid":
                albums_data = self.adapter.find_all_albums_by_releasegroupid(pq.value)
            elif pq.field in {"album", "artist", "albumartist"}:
                albums_data = self.adapter.find_all_albums_for_term(f"{pq.field}:{pq.value}")
            else:
                albums_data = self.adapter.find_all_albums_for_term(pq.value)

            return [RemoteAlbum(r) for r in albums_data]

        raise BeetsAdapterError(f"Unsupported RemoteLibrary albums() query shape: {query!r}")


# Facade aliases
RemoteLibrary = StockBeetsLibrary
BeetsClient = BeetsAdapter

# Global singleton instances
beets_adapter = BeetsAdapter()
stock_lib = StockBeetsLibrary(beets_adapter)
lib = stock_lib
