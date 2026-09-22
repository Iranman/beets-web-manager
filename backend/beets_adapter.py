"""Beets Adapter — Direct HTTP adapter for stock Beets (beetsplug.web + beetsplug.webmanager).

Communicates over HTTP with the stock LinuxServer Beets container:
- Native reads via beetsplug.web REST endpoints (/stats, /artist/, /item/, /album/)
- Authenticated mutations via beetsplug.webmanager (/webmanager/*)
"""

import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger("beets.adapter")


class BeetsAdapterError(Exception):
    """Base exception for Beets adapter errors."""
    def __init__(self, message: str, status_code: int = 0, response_data: Optional[Any] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class BeetsAdapterAuthError(BeetsAdapterError):
    """Raised when authentication with Beets webmanager plugin fails (401)."""
    pass


class BeetsAdapterNotFoundError(BeetsAdapterError):
    """Raised when a resource is not found (404)."""
    pass


class BeetsAdapterConnectionError(BeetsAdapterError):
    """Raised when Beets web server is unreachable."""
    pass


class BeetsAdapter:
    """Client for Stock Beets Web & WebManager Plugin APIs."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_key_file: Optional[str] = None,
        timeout: float = 30.0,
    ):
        raw_url = (
            base_url
            or os.environ.get("BEETS_WEB_URL")
            or os.environ.get("BEETS_API_URL")
            or "http://127.0.0.1:8337"
        )
        self.base_url = raw_url.rstrip("/")
        self._api_key = api_key or os.environ.get("BEETS_WEBMANAGER_API_KEY")
        self._api_key_file = (
            api_key_file
            or os.environ.get("BEETS_WEBMANAGER_API_KEY_FILE")
            or "/config/.webmanager_api_key"
        )
        self.timeout = timeout

    @property
    def api_key(self) -> str:
        """Resolve current API key from memory, env, or file."""
        if self._api_key:
            return self._api_key
        if self._api_key_file and os.path.isfile(self._api_key_file):
            try:
                with open(self._api_key_file, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
        return ""

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

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                data = resp.read()
                if "application/json" in content_type:
                    return json.loads(data.decode("utf-8"))
                return data
        except urllib.error.HTTPError as ex:
            status = ex.code
            err_body = ex.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
            except Exception:
                err_json = {"raw": err_body}

            if status == 401:
                raise BeetsAdapterAuthError(
                    f"Beets auth failed on {path}: {err_body}",
                    status_code=status,
                    response_data=err_json,
                )
            if status == 404:
                raise BeetsAdapterNotFoundError(
                    f"Beets resource not found on {path}: {err_body}",
                    status_code=status,
                    response_data=err_json,
                )
            raise BeetsAdapterError(
                f"Beets request error {status} on {path}: {err_body}",
                status_code=status,
                response_data=err_json,
            )
        except urllib.error.URLError as ex:
            raise BeetsAdapterConnectionError(
                f"Cannot connect to Beets server at {self.base_url}: {ex}"
            ) from ex

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
            return res.get("artist_names") or res.get("artist") or []
        return []

    def get_items(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch items matching query (or all items) from GET /item/ or /item/query/<query>."""
        if query:
            path = f"/item/query/{urllib.parse.quote(query)}"
        else:
            path = "/item/"
        res = self._request("GET", path)
        if isinstance(res, dict):
            return res.get("items") or res.get("results") or []
        return []

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        """Fetch single item by ID from GET /item/<id>."""
        try:
            res = self._request("GET", f"/item/{item_id}")
            return res if isinstance(res, dict) else None
        except BeetsAdapterNotFoundError:
            return None

    def get_albums(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch albums matching query (or all albums) from GET /album/ or /album/query/<query>."""
        if query:
            path = f"/album/query/{urllib.parse.quote(query)}"
        else:
            path = "/album/"
        res = self._request("GET", path)
        if isinstance(res, dict):
            return res.get("albums") or res.get("results") or []
        return []

    def get_album(self, album_id: int, expand: bool = True) -> Optional[Dict[str, Any]]:
        """Fetch single album by ID from GET /album/<id>?expand."""
        params = {"expand": ""} if expand else None
        try:
            res = self._request("GET", f"/album/{album_id}", params=params)
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
        return self._build_url(f"/item/{item_id}/file")

    def get_album_art_url(self, album_id: int) -> str:
        """Get the URL for album cover art."""
        return self._build_url(f"/album/{album_id}/art")

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
    ) -> Dict[str, Any]:
        """Execute non-interactive import inside Beets."""
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
        headers = {}
        if is_async:
            headers["Prefer"] = "respond-async"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        return self._request("POST", "/webmanager/import", json_data=payload, headers=headers)

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
        query: Optional[str] = None,
        delete_files: bool = False,
    ) -> Dict[str, Any]:
        """Remove items or albums from Beets library."""
        payload = {
            "item_ids": item_ids or [],
            "album_ids": album_ids or [],
            "query": query,
            "delete_files": delete_files,
        }
        return self._request("POST", "/webmanager/remove", json_data=payload)

    def move(
        self,
        item_ids: Optional[List[int]] = None,
        album_ids: Optional[List[int]] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move items or albums to target directory structure."""
        payload = {
            "item_ids": item_ids or [],
            "album_ids": album_ids or [],
            "query": query,
        }
        return self._request("POST", "/webmanager/move", json_data=payload)

    def merge_albums(
        self,
        target_album_id: int,
        source_album_ids: List[int],
        track_reassignments: Optional[Dict[str, Dict[str, Any]]] = None,
        write: bool = True,
        move: bool = True,
    ) -> Dict[str, Any]:
        """Merge multiple source albums into a target album."""
        payload = {
            "target_album_id": target_album_id,
            "source_album_ids": source_album_ids,
            "track_reassignments": track_reassignments or {},
            "write": write,
            "move": move,
        }
        return self._request("POST", "/webmanager/merge-album", json_data=payload)

    def fetch_art(
        self,
        album_ids: List[int],
        art_url: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Fetch and associate album art."""
        payload = {
            "album_ids": album_ids,
            "art_url": art_url,
            "force": force,
        }
        return self._request("POST", "/webmanager/fetchart", json_data=payload)

    def embed_art(
        self,
        album_ids: Optional[List[int]] = None,
        item_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Embed album art into audio files."""
        payload = {
            "album_ids": album_ids or [],
            "item_ids": item_ids or [],
        }
        return self._request("POST", "/webmanager/embedart", json_data=payload)

    def sync_mbsync(
        self,
        album_ids: Optional[List[int]] = None,
        item_ids: Optional[List[int]] = None,
        write: bool = True,
        move: bool = True,
    ) -> Dict[str, Any]:
        """Sync metadata with MusicBrainz using existing MBIDs."""
        payload = {
            "album_ids": album_ids or [],
            "item_ids": item_ids or [],
            "write": write,
            "move": move,
        }
        return self._request("POST", "/webmanager/mbsync", json_data=payload)

    # -------------------------------------------------------------------------
    # Caller Compatibility Helpers
    # -------------------------------------------------------------------------

    def find_all_items_by_album_id(self, album_id: int) -> List[Dict[str, Any]]:
        """Compatibility helper returning all items for an album."""
        album = self.get_album(album_id, expand=True)
        if album and "items" in album:
            return album["items"]
        return self.get_items(f"album_id:{album_id}")

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

    def relocate_album(self, album_id: int, mode: str = "move") -> Dict[str, Any]:
        """Compatibility helper for moving album files to match path rules."""
        return self.move(album_ids=[album_id])


# Global singleton instance
beets_adapter = BeetsAdapter()
